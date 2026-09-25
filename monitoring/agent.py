import argparse
import json
import logging
import math
import os
import random
import signal
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import audio, bmp280, dht11, motion
from .common import canonical
from .spool import Spool

LOG = logging.getLogger(__name__)
# Cloudflare blocks the default urllib agent string at the edge (error 1010).
USER_AGENT = "monitoring-agent/1"


def clock_synchronized():
    try:
        return subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                              capture_output=True, text=True, timeout=3, check=True).stdout.strip() == "yes"
    except (OSError, subprocess.SubprocessError):
        return False


def event(device_id, kind, source, values=None, status="ok", synchronized=None):
    return dict(schema_version=1, device_id=device_id, event_id=str(uuid.uuid4()),
                observed_at=time.time(), kind=kind, source=source, status=status,
                values=values or {}, clock_synchronized=(clock_synchronized()
                if synchronized is None else synchronized))


def read_sources(config):
    """Generic kernel/sysfs channels also support future IIO/hwmon sensors."""
    sources = config.get("sources", [dict(name="cpu", fields={
        "cpu_temperature_c": dict(path="/sys/class/thermal/thermal_zone0/temp", scale=0.001)})])
    for source in sources:
        if source.get("type") == "dht11":
            yield read_dht11(config, source)
            continue
        if source.get("type") == "bmp280":
            yield read_bmp280(config, source)
            continue
        values = {}
        status = "ok"
        for name, channel in source["fields"].items():
            try:
                value = float(Path(channel["path"]).read_text()) * channel.get("scale", 1)
                if not math.isfinite(value):
                    raise ValueError("nonfinite reading")
                values[name] = value
            except (OSError, ValueError):
                status = "error"
        yield event(config["device_id"], "measurement", source["name"], values, status)


def read_dht11(config, source):
    """A DHT11 on a GPIO line; a frame that never checks out becomes status=error."""
    try:
        humidity, temperature = dht11.read_isolated(source.get("chip", "/dev/gpiochip0"),
                                                    source.get("line", 119),
                                                    attempts=source.get("attempts", 8))
    except (OSError, ValueError) as exc:
        # The reason decides the fix, so log it: a missing permission, a bad
        # frame and a silent line each call for something different.
        LOG.warning("%s unavailable: %s: %s", source["name"], type(exc).__name__, exc)
        return event(config["device_id"], "measurement", source["name"], status="error")
    return event(config["device_id"], "measurement", source["name"],
                 {"humidity_percent": humidity, "temperature_c": temperature})


def read_bmp280(config, source):
    """A BMP280 on an I2C bus; one retry covers a transient bus error."""
    error = None
    for attempt in range(source.get("attempts", 2)):
        if attempt:
            time.sleep(0.5)
        try:
            temperature, pressure = bmp280.read(source.get("bus", 0), source.get("address", 0x76))
        except (PermissionError, FileNotFoundError) as exc:
            error = exc
            break   # retrying cannot fix access or a missing bus
        except (OSError, ValueError) as exc:
            error = exc
            continue
        return event(config["device_id"], "measurement", source["name"],
                     {"temperature_c": temperature, "pressure_hpa": pressure})
    LOG.warning("%s unavailable: %s: %s", source["name"], type(error).__name__, error)
    return event(config["device_id"], "measurement", source["name"], status="error")


def capture(config):
    camera = config["camera"]
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "v4l2"]
    if camera.get("input_format"):
        command += ["-input_format", camera["input_format"]]
    command += ["-video_size", camera.get("size", "1280x720"), "-i", camera["device"],
                "-ss", "0.5", "-frames:v", "1", "-threads", "1"]
    if camera.get("input_format") == "mjpeg":
        command += ["-c:v", "copy"]
    else:
        command += ["-q:v", "3"]
    command += ["-f", "image2pipe", "pipe:1"]
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=camera.get("timeout_seconds", 30))
    # This USB camera pads MJPEG packets with NUL bytes after the EOI marker.
    # Strip only NUL padding, never silently accept an image lacking an EOI.
    data = result.stdout.rstrip(b"\x00")
    if len(data) > 4 * 1024**2:
        raise ValueError("photo exceeds 4 MiB")
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise ValueError("invalid JPEG")
    return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward device credentials to another origin.


class Uploader:
    def __init__(self, config, spool):
        self.spool = spool
        self.url = config["server_url"].rstrip("/")
        parts = urllib.parse.urlsplit(self.url)
        if parts.scheme != "https" or not parts.netloc or parts.username or parts.query or parts.fragment:
            raise ValueError("server_url must be an HTTPS origin (no credentials/query)")
        self.token = Path(config["token_file"]).read_text().strip()
        if len(self.token) < 32:
            raise ValueError("device token too short")
        # TUN handles routing; inherited shell proxies must not change the transport.
        self.client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, data, headers=None):
        req = urllib.request.Request(self.url + path, data=data, method="POST", headers={
            "Authorization": "Bearer " + self.token, "User-Agent": USER_AGENT, **(headers or {})})
        with self.client.open(req, timeout=30) as response:
            return json.loads(response.read(65537))

    def send_once(self):
        rejected = False
        measurements = self.spool.batch("measurement", 12)
        if measurements:
            result = self.request("/v1/measurements", canonical({"events": [x[0] for x in measurements]}).encode(),
                                  {"Content-Type": "application/json"})
            sent = {x[0]["event_id"] for x in measurements}
            # Retention-expired items are explicitly acknowledged by the server.
            acknowledged = [r["event_id"] for r in result["results"]
                            if r["event_id"] in sent and r["status"] in {"stored", "duplicate", "expired"}]
            self.spool.acknowledge(acknowledged)
            if len(acknowledged) != len(sent):
                rejected = True
        for kind, path, content_type in (("photo", "/v1/photos", "image/jpeg"),
                                         ("audio", "/v1/audio", "audio/ogg")):
            for metadata, blob in self.spool.batch(kind, 1):
                result = self.request(path, blob, {"Content-Type": content_type,
                                      "X-Event": canonical(metadata)})
                if result["event_id"] == metadata["event_id"] and result["status"] in {"stored", "duplicate", "expired"}:
                    self.spool.acknowledge([metadata["event_id"]])
                else:
                    rejected = True
        if rejected:
            raise ValueError("server rejected some events")


def periodic(stop, interval, callback, offset=0):
    """Monotonic scheduling, no concurrent runs or catch-up storm after suspension."""
    if offset:
        stop.wait(offset)
    while not stop.is_set():
        started = time.monotonic()
        try:
            callback()
        except Exception as exc:
            LOG.warning("task failed: %s", type(exc).__name__)
        stop.wait(max(1, interval - (time.monotonic() - started)))


class Camera:
    """Photos at the normal pace, or every minute while the picture keeps changing.

    Each photo is compared with the one before it, brightness aside. A change above
    the threshold raises attention; two quiet photos in a row lower it again. While
    attention is raised the microphone records, and each minute that holds sound
    above the noise floor is queued as an Ogg/Opus clip.
    """

    def __init__(self, config, spool, listen=True):
        self.config, self.spool = config, spool
        self.camera = config.get("camera", {})
        self.options = config.get("attention", {})
        self.attention = motion.Attention(self.options.get("change_threshold", 0.06),
                                          self.options.get("calm_frames", 2))
        self.previous = None
        self.sound = config.get("audio", {})
        self.recorder = None
        if listen and self.options.get("enabled", True) and self.sound.get("enabled", True):
            self.recorder = audio.Recorder(self.sound.get("device", "hw:CARD=Camera,DEV=0"))
        self.clip_started = None

    @property
    def alert(self):
        return self.attention.alert

    def interval(self):
        if self.alert:
            return self.options.get("interval_seconds", 60)
        return self.config.get("interval_seconds", 600)

    def compare(self, jpeg):
        try:
            current = motion.thumbnail(jpeg)
        except (OSError, ValueError, subprocess.SubprocessError):
            LOG.warning("photo comparison unavailable")
            return None
        previous, self.previous = self.previous, current
        if previous is None:
            return None
        return motion.changed_fraction(previous, current, self.options.get("pixel_threshold", 0.5))

    def shot(self):
        if not self.camera.get("enabled", True):
            return
        metadata = event(self.config["device_id"], "photo", "camera")
        try:
            jpeg = capture(self.config)
        except (OSError, ValueError, subprocess.SubprocessError):
            self.spool.enqueue(event(self.config["device_id"], "measurement", "camera", status="error"))
            LOG.warning("camera unavailable; measurements continue")
            # A silent camera is no evidence of change: let raised attention run out.
            self.attention.observe(None)
        else:
            fraction = self.compare(jpeg) if self.options.get("enabled", True) else None
            was_alert = self.alert
            self.attention.observe(fraction)
            if fraction is not None:
                metadata["values"]["changed_percent"] = round(fraction * 100, 1)
            # The photo that raised attention carries the flag, and so does the last one of it.
            metadata["values"]["attention"] = int(self.alert or was_alert)
            self.spool.enqueue(metadata, jpeg)
        self.listen()

    def listen(self):
        if self.recorder is None:
            return
        if self.alert:
            if not self.recorder.running:
                if self.clip_started is not None:
                    self.keep(self.recorder.stop())   # the stream died: keep what it heard
                self.clip_started = time.time()
                self.recorder.start()
                return
            started, self.clip_started = self.clip_started, time.time()
            self.keep(self.recorder.clip(), started)
        elif self.clip_started is not None:
            self.close()

    def keep(self, pcm, started=None):
        started = self.clip_started if started is None else started
        seconds = len(pcm) / audio.BYTES_PER_SECOND
        if seconds < 1:
            if started is not None and time.time() - started > 5:
                self.spool.enqueue(event(self.config["device_id"], "measurement", "microphone",
                                         status="error"))
                LOG.warning("microphone unavailable")
            return
        heard, peak = audio.audible(pcm, self.sound.get("threshold_dbfs", -30.0),
                                    self.sound.get("min_windows", 3))
        if not heard:
            return
        try:
            clip = audio.encode(pcm, self.sound.get("bitrate", "16k"))
        except (OSError, ValueError, subprocess.SubprocessError):
            LOG.warning("audio encoding failed")
            return
        metadata = event(self.config["device_id"], "audio", "microphone",
                         {"duration_seconds": round(seconds, 1), "peak_dbfs": round(peak, 1)})
        if started is not None:
            metadata["observed_at"] = started
        self.spool.enqueue(metadata, clip)

    def close(self):
        if self.recorder is not None and self.clip_started is not None:
            self.keep(self.recorder.stop())
            self.clip_started = None


def camera_loop(stop, camera, offset):
    """Photos on slots `offset` + k·step from one fixed start.

    The attention interval divides the normal one, so every slot keeps the same
    distance from the sensor reads at the start of each measurement cycle.
    """
    due = time.monotonic() + offset
    try:
        while not stop.wait(max(0, due - time.monotonic())):
            try:
                camera.shot()
            except Exception as exc:
                LOG.warning("task failed: %s", type(exc).__name__)
            step = camera.interval()
            due += step
            while due <= time.monotonic():
                due += step     # no catch-up storm after a stall or suspension
    finally:
        camera.close()


def run(config, once=False, collect_only=False):
    spool = Spool(config["state_dir"], **config.get("queue", {}))
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())

    def measurements():
        for sample in read_sources(config):
            spool.enqueue(sample)
        spool.enqueue(event(config["device_id"], "measurement", "agent", spool.status()))

    camera = Camera(config, spool, listen=not once)

    if once:
        measurements()
        camera.shot()
        if not collect_only:
            Uploader(config, spool).send_once()
        print(canonical(spool.status()))
        return

    interval = config.get("interval_seconds", 600)
    # The camera and the photo upload are the busiest moments of a cycle; keep
    # them clear of the sensor reads, which a loaded CPU makes fail.
    photo_offset = config.get("camera", {}).get("offset_seconds", 30)
    threads = [threading.Thread(target=periodic, args=(stop, interval, measurements), daemon=True),
               threading.Thread(target=camera_loop, args=(stop, camera, photo_offset), daemon=True)]
    threads.append(threading.Thread(target=periodic, args=(stop, 60, spool.prune), daemon=True))
    for thread in threads:
        thread.start()
    uploader = None if collect_only else Uploader(config, spool)
    failures = 0
    while not stop.is_set():
        try:
            if uploader:
                uploader.send_once()
            failures = 0
            stop.wait(5)
        except Exception as exc:
            failures = min(failures + 1, 8)
            status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
            # Log exception type/status only; URLs, headers and credentials stay private.
            LOG.warning("upload failed: %s status=%s", type(exc).__name__, status)
            delay = 300 if status in (401, 403) else min(300, 2 ** failures + random.uniform(0, 3))
            stop.wait(delay)
    for thread in threads:
        thread.join(timeout=35)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/monitoring/agent.toml")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--collect-only", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(args.config, "rb") as stream:
        config = tomllib.load(stream)
    if config.get("interval_seconds", 600) < 10:
        parser.error("interval_seconds must be at least 10")
    fast = config.get("attention", {}).get("interval_seconds", 60)
    if fast < 10 or config.get("interval_seconds", 600) % fast:
        parser.error("attention.interval_seconds must be at least 10 and divide interval_seconds")
    run(config, args.once, args.collect_only)


if __name__ == "__main__":
    main()
