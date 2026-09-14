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
        for metadata, photo in self.spool.batch("photo", 1):
            result = self.request("/v1/photos", photo, {"Content-Type": "image/jpeg",
                                  "X-Event": canonical(metadata)})
            if result["event_id"] == metadata["event_id"] and result["status"] in {"stored", "duplicate", "expired"}:
                self.spool.acknowledge([metadata["event_id"]])
            else:
                rejected = True
        if rejected:
            raise ValueError("server rejected some events")


def periodic(stop, interval, callback):
    """Monotonic scheduling, no concurrent runs or catch-up storm after suspension."""
    while not stop.is_set():
        started = time.monotonic()
        try:
            callback()
        except Exception as exc:
            LOG.warning("task failed: %s", type(exc).__name__)
        stop.wait(max(1, interval - (time.monotonic() - started)))


def run(config, once=False, collect_only=False):
    spool = Spool(config["state_dir"], **config.get("queue", {}))
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())

    def measurements():
        for sample in read_sources(config):
            spool.enqueue(sample)
        spool.enqueue(event(config["device_id"], "measurement", "agent", spool.status()))

    def photo():
        if not config.get("camera", {}).get("enabled", True):
            return
        metadata = event(config["device_id"], "photo", "camera")
        try:
            spool.enqueue(metadata, capture(config))
        except (OSError, ValueError, subprocess.SubprocessError):
            spool.enqueue(event(config["device_id"], "measurement", "camera", status="error"))
            LOG.warning("camera unavailable; measurements continue")

    if once:
        measurements()
        photo()
        if not collect_only:
            Uploader(config, spool).send_once()
        print(canonical(spool.status()))
        return

    threads = [threading.Thread(target=periodic, args=(stop, config.get("interval_seconds", 600), fn),
                                daemon=True) for fn in (measurements, photo)]
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
    run(config, args.once, args.collect_only)


if __name__ == "__main__":
    main()
