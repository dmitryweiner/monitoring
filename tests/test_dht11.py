import pytest

from monitoring import dht11


def frame_edges(frame, preamble=True, release=False, start=1_000_000):
    """Rising/falling edges a DHT11 would produce for these five bytes."""
    edges, t = [], start
    if release:
        edges += [(t, dht11.EVENT_RISING), (t + 30_000, dht11.EVENT_FALLING)]
        t += 30_000
    t += 80_000                        # sensor holds the line low
    if preamble:
        edges += [(t, dht11.EVENT_RISING), (t + 80_000, dht11.EVENT_FALLING)]
    t += 80_000
    for byte in frame:
        for bit in range(7, -1, -1):
            t += 50_000                # low before every bit
            width = 70_000 if byte >> bit & 1 else 27_000
            edges += [(t, dht11.EVENT_RISING), (t + width, dht11.EVENT_FALLING)]
            t += width
    t += 50_000
    edges.append((t, dht11.EVENT_RISING))   # release after the last bit: no falling edge
    return edges


def with_checksum(*data):
    return bytes(data) + bytes([sum(data) & 0xFF])


@pytest.mark.parametrize("preamble,release", [(True, False), (False, False), (True, True)])
def test_decode_valid_frame(preamble, release):
    edges = frame_edges(with_checksum(45, 0, 23, 4), preamble, release)
    assert dht11.decode(edges) == (45.0, 23.4)


def test_decode_negative_temperature():
    assert dht11.decode(frame_edges(with_checksum(60, 0, 5, 0x83))) == (60.0, -5.3)


def test_decode_rejects_checksum_and_truncation():
    bad = bytearray(with_checksum(45, 0, 23, 4))
    bad[4] ^= 1
    with pytest.raises(ValueError, match="checksum"):
        dht11.decode(frame_edges(bytes(bad)))
    with pytest.raises(ValueError, match="high pulses"):
        dht11.decode(frame_edges(with_checksum(45, 0, 23, 4))[:-20])


def test_decode_rejects_impossible_pulses_and_values():
    edges = frame_edges(with_checksum(45, 0, 23, 4))
    ts, kind = edges[-2]
    edges[-2] = (ts + 500_000, kind)       # a 0.5 ms high is not a DHT11 bit
    with pytest.raises(ValueError, match="width"):
        dht11.decode(edges)
    with pytest.raises(ValueError, match="range"):
        dht11.decode(frame_edges(with_checksum(120, 0, 23, 0)))


def test_request_layout_matches_the_kernel_header():
    config = dht11._config(dht11.FLAG_OUTPUT, low=True)
    assert len(config) == 272
    request = dht11._request(119, config)
    assert len(request) == 592
    assert request[:4] == (119).to_bytes(4, "little")


class Finished:
    def __init__(self, stdout, returncode=0):
        self.stdout, self.returncode = stdout, returncode


def test_isolated_read_parses_the_child_answer(monkeypatch):
    import subprocess
    answers = iter([
        Finished('{"humidity_percent": 41.0, "temperature_c": 25.2}\n'),
        Finished('{"error": "no valid frame in 5 attempts", "kind": "ValueError"}\n', 1),
        Finished('{"error": "denied", "kind": "PermissionError"}\n', 1),
        Finished('Traceback (most recent call last):\n', 1),
    ])
    monkeypatch.setattr(dht11.subprocess, "run", lambda *a, **k: next(answers))
    assert dht11.read_isolated() == (41.0, 25.2)
    with pytest.raises(ValueError, match="no valid frame"):
        dht11.read_isolated()
    with pytest.raises(PermissionError):
        dht11.read_isolated()
    with pytest.raises(ValueError, match="no answer"):
        dht11.read_isolated()

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(dht11.subprocess, "run", slow)
    with pytest.raises(ValueError, match="timed out"):
        dht11.read_isolated()


def test_isolated_read_runs_a_real_child(tmp_path):
    # A chip that does not exist fails the same way on every machine.
    with pytest.raises(FileNotFoundError, match="No such file"):
        dht11.read_isolated(chip=str(tmp_path / "gpiochip9"), attempts=3, pause=5)


def test_setup_errors_are_not_retried(monkeypatch):
    calls = []

    def denied(*args):
        calls.append(args)
        raise PermissionError(13, "denied")
    monkeypatch.setattr(dht11, "capture", denied)
    monkeypatch.setattr(dht11.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        dht11.read(attempts=5)
    assert len(calls) == 1


def test_fast_cores_are_real_cpus():
    import os
    assert dht11.fast_cores() <= set(range(os.cpu_count()))


def test_failure_names_every_reason_it_saw(monkeypatch):
    frames = iter([
        [(0, dht11.EVENT_RISING)],                              # too few pulses
        frame_edges(bytes([1, 2, 3, 4, 0])),                    # checksum mismatch
        frame_edges(bytes([1, 2, 3, 4, 0])),                    # and again
    ])
    monkeypatch.setattr(dht11, "capture", lambda *a, **k: (None, next(frames)))
    monkeypatch.setattr(dht11.time, "sleep", lambda seconds: None)
    with pytest.raises(ValueError) as failure:
        dht11.read(attempts=3)
    message = str(failure.value)
    assert "no valid frame in 3 attempts" in message
    assert "2x checksum mismatch" in message
    assert "1x expected 40-42 high pulses, got 0" in message
