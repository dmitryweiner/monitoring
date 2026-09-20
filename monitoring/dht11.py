"""DHT11 over the GPIO character device, standard library only.

This kernel has no dht11 driver and no IIO subsystem, so the protocol is decoded
here. The line is driven low for the start signal, switched to input, and then
sampled in a tight loop; the high-pulse widths separate zeros (about 27 us) from
ones (about 70 us).

Edge events were tried first and do not work on this board: of the ~84 edges in
a frame only 8-21 arrived, the first 0.5-1.5 ms after release, and nearly every
"pulse" measured the same ~60 us whatever the bit. That is the event path's own
service rate, not the signal. Polling costs about a microsecond per sample,
which resolves the pulses comfortably; a real-time priority keeps the scheduler
from cutting into a 4 ms frame, and the checksum catches the frames it still does.

The GPIO v2 uAPI is used through ioctl directly: the agent takes no third-party
packages, and python3-libgpiod is not installed. Struct sizes and ioctl numbers
were checked against /usr/include/linux/gpio.h on the board.
"""
import argparse
import collections
import fcntl
import gc
import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

GET_LINE = 0xC250B407     # GPIO_V2_GET_LINE_IOCTL, struct gpio_v2_line_request (592 bytes)
SET_CONFIG = 0xC110B40D   # GPIO_V2_LINE_SET_CONFIG_IOCTL, struct gpio_v2_line_config (272)
GET_VALUES = 0xC010B40E   # GPIO_V2_LINE_GET_VALUES_IOCTL, struct gpio_v2_line_values (16)
FLAG_INPUT = 1 << 2
FLAG_OUTPUT = 1 << 3
ATTR_OUTPUT_VALUES = 2
EVENT_RISING = 1
EVENT_FALLING = 2
REQUEST_FD_OFFSET = 588

START_SECONDS = 0.02      # the sensor wants the line low for at least 18 ms
FRAME_EDGES = 85          # release, answer, preamble, 40 bits and the final release
WINDOW_NS = 15_000_000    # a frame lasts about 4 ms
ONE_THRESHOLD_NS = 48_000  # a zero is 26-28 us high, a one about 70 us
PULSE_RANGE_NS = (10_000, 100_000)


def _config(flags, low=False):
    attrs = [(ATTR_OUTPUT_VALUES, 0, 1)] if low else []
    data = struct.pack("=QI20x", flags, len(attrs))
    for attr_id, value, mask in attrs:
        data += struct.pack("=IIQQ", attr_id, 0, value, mask)
    return bytearray(data + bytes(24 * (10 - len(attrs))))


def _request(line, config, consumer=b"monitoring-dht11"):
    data = (struct.pack("=64I", line, *[0] * 63) + consumer.ljust(32, b"\0")
            + bytes(config) + struct.pack("=II20xi", 1, 256, 0))
    return bytearray(data)


def _realtime():
    """Raise this thread to SCHED_FIFO; return (undo, whether it worked).

    Without root this needs RLIMIT_RTPRIO, which the agent unit grants with
    LimitRTPRIO. Failing is not fatal: the capture just runs less protected.
    """
    try:
        previous = os.sched_getscheduler(0), os.sched_getparam(0)
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(50))
    except (OSError, AttributeError):
        return (lambda: None), False
    return (lambda: os.sched_setscheduler(0, previous[0], previous[1])), True


def capture(chip="/dev/gpiochip0", line=119):
    """Send the start signal and return (stats, [(timestamp_ns, kind), ...])."""
    chip_fd = os.open(chip, os.O_RDONLY | os.O_CLOEXEC)
    try:
        request = _request(line, _config(FLAG_OUTPUT, low=True))
        fcntl.ioctl(chip_fd, GET_LINE, request)
    finally:
        os.close(chip_fd)
    fd = struct.unpack_from("=i", request, REQUEST_FD_OFFSET)[0]
    edges = []
    values = bytearray(struct.pack("=QQ", 0, 1))   # bits, mask: line 0 of the request
    ioctl, now_ns = fcntl.ioctl, time.monotonic_ns
    undo, realtime = _realtime()
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        time.sleep(START_SECONDS)
        release = now_ns()
        ioctl(fd, SET_CONFIG, _config(FLAG_INPUT))
        switched = now_ns()
        level, samples, deadline = 0, 0, release + WINDOW_NS
        while True:
            ioctl(fd, GET_VALUES, values)
            now = now_ns()
            samples += 1
            if (values[0] & 1) != level:
                level ^= 1
                edges.append((now, EVENT_RISING if level else EVENT_FALLING))
                if len(edges) >= FRAME_EDGES:
                    break
            if now >= deadline:
                break
    finally:
        if gc_was_enabled:
            gc.enable()
        undo()
        os.close(fd)
    stats = {"realtime": realtime, "switch_us": (switched - release) / 1000, "samples": samples,
             "sample_us": (now - switched) / 1000 / max(samples, 1),
             "first_us": (edges[0][0] - release) / 1000 if edges else None}
    return stats, edges


def pulses(edges):
    """Widths of the high pulses, in order, in nanoseconds."""
    widths, rise = [], None
    for timestamp, kind in edges:
        if kind == EVENT_RISING:
            rise = timestamp
        elif kind == EVENT_FALLING and rise is not None:
            widths.append(timestamp - rise)
            rise = None
    return widths


def decode(edges):
    """Return (humidity_percent, temperature_c) or raise ValueError."""
    widths = pulses(edges)
    # 40 data bits, optionally preceded by the 80 us preamble and by the short
    # high between the host releasing the line and the sensor answering.
    if not 40 <= len(widths) <= 42:
        raise ValueError(f"expected 40-42 high pulses, got {len(widths)}")
    bits = widths[-40:]
    low, high = PULSE_RANGE_NS
    if any(not low <= width <= high for width in bits):
        raise ValueError("pulse width out of range")
    value = 0
    for width in bits:
        value = value << 1 | (width > ONE_THRESHOLD_NS)
    frame = value.to_bytes(5, "big")
    if (sum(frame[:4]) & 0xFF) != frame[4]:
        raise ValueError("checksum mismatch")
    humidity = frame[0] + frame[1] / 10
    # Newer DHT11 revisions report tenths of a degree, with bit 7 as the sign.
    temperature = frame[2] + (frame[3] & 0x7F) / 10
    if frame[3] & 0x80:
        temperature = -temperature
    if not (0 <= humidity <= 100 and -20 <= temperature <= 60):
        raise ValueError("reading outside the sensor's range")
    return humidity, temperature


def fast_cores():
    """The CPUs with the highest capacity; on the A733 that is the two A76 cores."""
    capacities = {}
    for path in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpu_capacity"):
        try:
            capacities[int(path.parent.name[3:])] = int(path.read_text())
        except (OSError, ValueError):
            continue
    if not capacities:
        return set()
    best = max(capacities.values())
    return {cpu for cpu, capacity in capacities.items() if capacity == best}


def read(chip="/dev/gpiochip0", line=119, attempts=8, pause=2.0):
    """A checksum-valid reading, retrying; the sensor needs about 2 s between reads.

    The failure names every reason it saw and how often: a run of checksum
    mismatches points at timing, "no edges" at wiring or power.
    """
    reasons = collections.Counter()
    for attempt in range(attempts):
        if attempt:
            time.sleep(pause)
        try:
            return decode(capture(chip, line)[1])
        except (PermissionError, FileNotFoundError):
            raise  # retrying cannot fix access or a missing chip
        except (OSError, ValueError) as exc:
            reasons[str(exc)] += 1
    summary = "; ".join(f"{count}x {reason}" for reason, count in reasons.most_common())
    raise ValueError(f"no valid frame in {attempts} attempts: {summary}")


def read_isolated(chip="/dev/gpiochip0", line=119, attempts=8, pause=2.0):
    """read() in a child process, for callers that run other Python threads.

    Every sample is an ioctl, and an ioctl releases the GIL. Inside the agent,
    another thread can take it and hold the sampling loop for up to the switch
    interval (5 ms) — longer than the whole frame. In the agent this failed all
    five attempts of a reading about one time in nine. A separate interpreter
    has nobody to hand the GIL to.
    """
    command = [sys.executable, "-m", "monitoring.dht11", "--json", "--chip", chip,
               "--line", str(line), "--attempts", str(attempts), "--pause", str(pause)]
    timeout = attempts * (pause + 1) + 10
    try:
        result = subprocess.run(command, cwd=Path(__file__).resolve().parent.parent,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("sensor read timed out") from exc
    try:
        answer = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"no answer from the reader (exit {result.returncode})") from exc
    if "error" in answer:
        kinds = {"PermissionError": PermissionError, "FileNotFoundError": FileNotFoundError}
        raise kinds.get(answer.get("kind"), ValueError)(answer["error"])
    return answer["humidity_percent"], answer["temperature_c"]


def _json_reading(args):
    cores = fast_cores()
    if cores:
        try:
            os.sched_setaffinity(0, cores)
        except OSError:
            pass
    try:
        humidity, temperature = read(args.chip, args.line, args.attempts, args.pause)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "kind": type(exc).__name__}))
        return 1
    print(json.dumps({"humidity_percent": humidity, "temperature_c": temperature}))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Read a DHT11 once, or show raw timing.")
    parser.add_argument("--chip", default="/dev/gpiochip0")
    parser.add_argument("--line", type=int, default=119)
    parser.add_argument("--count", type=int, default=5, help="readings to take")
    parser.add_argument("--raw", action="store_true", help="print edge timing for each try")
    parser.add_argument("--json", action="store_true",
                        help="one reading with retries, as a JSON line (used by the agent)")
    parser.add_argument("--attempts", type=int, default=8)
    parser.add_argument("--pause", type=float, default=2.0)
    parser.add_argument("--pin", action="store_true", help="run on the fastest cores")
    args = parser.parse_args()
    if args.json:
        return _json_reading(args)
    if args.pin and fast_cores():
        os.sched_setaffinity(0, fast_cores())
    ok = 0
    for attempt in range(args.count):
        if attempt:
            time.sleep(2.0)
        try:
            stats, edges = capture(args.chip, args.line)
        except OSError as exc:
            print(f"#{attempt + 1}: cannot use the line: {exc}")
            continue
        if args.raw:
            widths = [round(w / 1000) for w in pulses(edges)]
            print(f"#{attempt + 1}: realtime {'yes' if stats['realtime'] else 'NO'}, "
                  f"switch {stats['switch_us']:.0f} us, "
                  f"{stats['samples']} samples at {stats['sample_us']:.2f} us, "
                  f"{len(edges)} edges, first at {stats['first_us']} us")
            print(f"    {len(widths)} highs (us): {widths}")
        try:
            humidity, temperature = decode(edges)
            ok += 1
            print(f"#{attempt + 1}: humidity {humidity:.1f} %, temperature {temperature:.1f} °C")
        except ValueError as exc:
            print(f"#{attempt + 1}: invalid frame: {exc}")
    print(f"{ok}/{args.count} valid")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
