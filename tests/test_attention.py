import array
import io
import math
import random
import sqlite3
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw

from monitoring import agent, audio, motion
from monitoring.agent import Camera, Uploader, camera_loop, event
from monitoring.spool import Spool

MIGRATIONS = Path(__file__).parent.parent / "cloud" / "migrations"


def scene(brightness=1.0, intruder=False, dark=False):
    image = Image.new("L", (320, 180), 90)
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 30, 90, 150), fill=200)       # a door
    draw.rectangle((150, 60, 230, 120), fill=40)      # a shelf
    draw.ellipse((250, 20, 300, 70), fill=160)        # a lamp
    if intruder:
        draw.rectangle((100, 20, 200, 175), fill=10)  # someone walks in
    if dark:
        image = Image.new("L", (320, 180), 1)
    image = image.point(lambda p: min(255, int(p * brightness)))
    stream = io.BytesIO()
    image.convert("RGB").save(stream, format="JPEG", quality=90)
    return stream.getvalue()


def test_change_ignores_brightness_but_not_movement():
    base = motion.thumbnail(scene())
    assert motion.changed_fraction(base, motion.thumbnail(scene())) == 0
    # Auto exposure or a cloud: every pixel scales, the structure stays.
    assert motion.changed_fraction(base, motion.thumbnail(scene(brightness=0.55))) < 0.02
    assert motion.changed_fraction(base, motion.thumbnail(scene(intruder=True))) > 0.10
    night = motion.thumbnail(scene(dark=True))
    assert motion.changed_fraction(night, night) == 0
    assert motion.changed_fraction(night, base) == 1.0


def test_attention_rises_on_change_and_falls_after_two_quiet_frames():
    attention = motion.Attention(threshold=0.10, calm=2)
    seen = [attention.observe(f) for f in (None, 0.02, 0.3, 0.05, 0.4, 0.01, None, 0.2, 0.1)]
    assert seen == [False, False, True, True, True, True, False, True, True]
    assert not attention.observe(0.0)


def pcm(level_dbfs=None, seconds=3.0, click=False):
    rng = random.Random(1)
    samples = array.array("h", [rng.randint(-200, 200) for _ in range(int(seconds * audio.RATE))])
    if level_dbfs is not None:
        amplitude = 32768 * 10 ** (level_dbfs / 20) * math.sqrt(2)
        for i in range(audio.RATE, 2 * audio.RATE):
            samples[i] = int(max(-32768, min(32767, samples[i] + amplitude * math.sin(i / 5))))
    if click:
        samples[audio.RATE] = 32767
    return samples.tobytes()


def test_sound_above_noise_floor_is_audible_and_encoded():
    quiet, peak = audio.audible(pcm())
    assert not quiet and peak < -40
    assert not audio.audible(pcm(click=True))[0]      # one click is not a sound
    heard, peak = audio.audible(pcm(-20))
    assert heard and -22 < peak < -18
    clip = audio.encode(pcm(-20))
    assert clip.startswith(b"OggS") and len(clip) < 20000


class FakeRecorder:
    def __init__(self):
        self.running = False
        self.heard = pcm()

    def start(self):
        self.running = True

    def clip(self):
        return self.heard

    def stop(self):
        self.running = False
        return self.heard


def test_camera_speeds_up_records_and_calms_down(tmp_path, monkeypatch):
    spool = Spool(tmp_path, reserve_bytes=0)
    config = {"device_id": "home", "interval_seconds": 600, "camera": {"device": "x"}}
    camera = Camera(config, spool)
    camera.recorder = recorder = FakeRecorder()
    frames = iter([scene(), scene(brightness=0.6), scene(intruder=True), scene(intruder=True),
                   scene(intruder=True)])
    monkeypatch.setattr(agent, "capture", lambda config: next(frames))

    camera.shot()
    camera.shot()                   # darker, same room
    assert camera.interval() == 600 and not recorder.running
    camera.shot()                   # someone came in
    assert camera.interval() == 60 and recorder.running
    camera.clip_started -= 60
    recorder.heard = pcm(-20)
    camera.shot()                   # sitting still, talking: first quiet frame
    assert camera.interval() == 60 and recorder.running
    recorder.heard = pcm()
    camera.shot()                   # still, silent: second quiet frame, back to normal
    assert camera.interval() == 600 and not recorder.running

    photos = [e for e, _ in spool.batch("photo", 10)]
    assert [p["values"].get("attention") for p in photos] == [0, 0, 1, 1, 1]
    assert "changed_percent" not in photos[0]["values"]
    assert photos[1]["values"]["changed_percent"] < 10 < photos[2]["values"]["changed_percent"]
    clips = spool.batch("audio", 10)
    assert len(clips) == 1          # only the minute with sound above the floor
    metadata, clip = clips[0]
    assert metadata["source"] == "microphone" and clip.startswith(b"OggS")
    assert metadata["values"]["peak_dbfs"] > -25 and metadata["values"]["duration_seconds"] == 3.0


def test_camera_loop_keeps_slots_and_skips_missed_ones():
    stop = threading.Event()
    started = time.monotonic()
    calls = []

    class Fake:
        def shot(self):
            calls.append(time.monotonic() - started)
            if len(calls) == 2:
                time.sleep(0.35)        # a stalled capture spans several slots
            if len(calls) == 4:
                stop.set()

        def interval(self):
            return 0.1

        def close(self):
            calls.append("closed")
    camera_loop(stop, Fake(), 0.2)
    assert calls[-1] == "closed"
    times = calls[:-1]
    assert times[0] >= 0.2
    assert times[2] - times[1] > 0.3        # no burst of catch-up shots
    for t in times:                         # every shot stays on the 0.2 + k·0.1 grid
        assert abs((t - 0.2) / 0.1 - round((t - 0.2) / 0.1)) < 0.3


def test_uploader_sends_audio_as_ogg(tmp_path):
    spool = Spool(tmp_path / "spool", reserve_bytes=0)
    clip = event("home", "audio", "microphone", {"peak_dbfs": -20.0}, synchronized=True)
    spool.enqueue(clip, b"OggS" + b"\0" * 40)
    token = tmp_path / "token"
    token.write_text("d" * 40)
    uploader = Uploader({"server_url": "https://api.example", "token_file": str(token)}, spool)
    sent = []

    def request(path, data, headers):
        sent.append((path, headers["Content-Type"], data[:4]))
        return {"event_id": clip["event_id"], "status": "stored"}
    uploader.request = request
    uploader.send_once()
    assert sent == [("/v1/audio", "audio/ogg", b"OggS")]
    assert spool.status()["queued"] == 0


def apply(db, name):
    for statement in (MIGRATIONS / name).read_text().split("-- statement-breakpoint"):
        db.execute(statement.strip())


def test_audio_migration_keeps_rows_counters_and_latest():
    db = sqlite3.connect(":memory:")
    apply(db, "0001_initial.sql")
    now = time.time()
    rows = [("m1", now - 60, "measurement", "cpu", None, 0), ("p1", now - 30, "photo", "camera", "home/p1.jpg", 5000)]
    for id, observed, kind, source, key, size in rows:
        db.execute("INSERT INTO events VALUES ('home',?,?,?,?,?,'{}','f',?,?,'ready')",
                   (id, observed, now, kind, source, key, size))
    before = {t: db.execute(f"SELECT * FROM {t} ORDER BY 1,2").fetchall() for t in ("events", "latest", "daily")}
    usage = db.execute("SELECT photo_bytes FROM usage").fetchone()
    apply(db, "0002_audio.sql")
    for table, content in before.items():
        assert db.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall() == content
    assert db.execute("SELECT photo_bytes FROM usage").fetchone() == usage == (5000,)
    db.execute("INSERT INTO events VALUES ('home','a1',?,?,'audio','microphone','{}','f','home/a1.ogg',700,'ready')",
               (now, now))
    assert db.execute("SELECT photo_bytes FROM usage").fetchone() == (5700,)
    assert db.execute("SELECT id FROM latest WHERE kind='audio'").fetchone() == ("a1",)
    db.execute("DELETE FROM events WHERE id='p1'")
    assert db.execute("SELECT photo_bytes FROM usage").fetchone() == (700,)
    assert db.execute("SELECT id FROM latest WHERE kind='photo'").fetchone() is None
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE tbl_name='events'")}
    assert {"timeline", "by_source", "retention", "pending_age", "object_lookup", "latest_insert",
            "latest_ready", "latest_delete", "check_quota", "event_usage", "release_usage"} <= names
