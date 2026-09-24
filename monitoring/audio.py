"""Sound capture for raised attention: raw PCM in, Ogg/Opus out only when something was heard.

One ffmpeg process streams 16 kHz mono PCM for as long as attention lasts, so
consecutive clips join without gaps. A clip is judged in 100 ms windows: the
camera microphone hisses at about -42.5 dBFS in a quiet room, so a clip is kept
when enough windows rise above the threshold, and a single click is not enough.
"""
import array
import math
import subprocess
import threading

RATE = 16000
WINDOW = RATE // 10            # samples per 100 ms
BYTES_PER_SECOND = RATE * 2    # s16le, mono


def levels(pcm):
    """RMS level of each 100 ms window in dBFS; DC offset removed per window."""
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    result = []
    for start in range(0, len(samples) - WINDOW + 1, WINDOW):
        window = samples[start:start + WINDOW]
        mean = sum(window) / WINDOW
        power = sum((x - mean) ** 2 for x in window) / WINDOW
        result.append(10 * math.log10(power / 32768 ** 2) if power else -120.0)
    return result


def audible(pcm, threshold_dbfs=-30.0, min_windows=3, skip_seconds=0.2):
    """Whether the clip holds sound above the noise floor, and its loudest window."""
    values = levels(pcm[int(skip_seconds * RATE) * 2:])   # ALSA clicks when the device opens
    if not values:
        return False, -120.0
    return sum(v > threshold_dbfs for v in values) >= min_windows, max(values)


def encode(pcm, bitrate="16k", timeout=60):
    """Ogg/Opus, speech tuned: about 120 KB per minute at 16 kbit/s."""
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0",
         "-af", "highpass=f=80", "-c:a", "libopus", "-b:a", bitrate,
         "-application", "voip", "-threads", "1", "-f", "ogg", "pipe:1"],
        input=pcm, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=timeout)
    if not result.stdout.startswith(b"OggS"):
        raise ValueError("invalid Ogg stream")
    return result.stdout


class Recorder:
    """Streams PCM from an ALSA device in a background thread while attention is raised.

    clip() returns everything heard since the previous call; stop() ends the stream.
    """

    def __init__(self, device, max_seconds=120):
        self.device, self.limit = device, max_seconds * BYTES_PER_SECOND
        self.process = None
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.reader = None

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        if self.running:
            return
        self.process = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-f", "alsa", "-channels", "1", "-sample_rate", str(RATE), "-i", self.device,
             "-f", "s16le", "-ac", "1", "-ar", str(RATE), "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.reader = threading.Thread(target=self._read, args=(self.process,), daemon=True)
        self.reader.start()

    def _read(self, process):
        while chunk := process.stdout.read(WINDOW * 2):
            with self.lock:
                self.buffer += chunk
                # Never let a stalled consumer grow memory past the unit's MemoryMax.
                if len(self.buffer) > self.limit:
                    del self.buffer[:len(self.buffer) - self.limit]

    def clip(self):
        with self.lock:
            pcm, self.buffer = bytes(self.buffer), bytearray()
        return pcm

    def stop(self):
        process, self.process = self.process, None
        if process is None:
            return b""
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if self.reader:
            self.reader.join(timeout=5)
        return self.clip()
