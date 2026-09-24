"""Frame change detection that ignores overall brightness, without third-party packages.

ffmpeg shrinks a JPEG to a small grey thumbnail; averaging over the area already
hides most sensor noise. Each thumbnail is then normalised by its own mean and
standard deviation, so auto exposure, a cloud or a dimmer lamp leave it unchanged,
while anything that moves the picture's structure does not.
"""
import math
import subprocess

WIDTH, HEIGHT = 64, 36
# A thumbnail flatter than this carries no structure to compare: a dark room at
# night or a covered lens. Its normalised pixels would be pure amplified noise.
FLAT_DEVIATION = 4.0


def thumbnail(jpeg, timeout=15):
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "image2pipe",
         "-i", "pipe:0", "-vf", f"scale={WIDTH}:{HEIGHT}:flags=area,format=gray",
         "-frames:v", "1", "-f", "rawvideo", "pipe:1"],
        input=jpeg, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=timeout)
    if len(result.stdout) != WIDTH * HEIGHT:
        raise ValueError("unexpected thumbnail size")
    return result.stdout


def _normalised(pixels):
    mean = sum(pixels) / len(pixels)
    deviation = math.sqrt(sum((p - mean) ** 2 for p in pixels) / len(pixels))
    if deviation < FLAT_DEVIATION:
        return None
    return [(p - mean) / deviation for p in pixels]


def changed_fraction(previous, current, pixel_threshold=0.5):
    """Share of pixels whose brightness-independent value moved by more than the threshold."""
    if len(previous) != len(current):
        raise ValueError("thumbnails differ in size")
    a, b = _normalised(previous), _normalised(current)
    if a is None and b is None:
        return 0.0      # both dark: nothing to see, nothing changed
    if a is None or b is None:
        return 1.0      # light switched on in a dark room, or the lens covered
    return sum(abs(x - y) > pixel_threshold for x, y in zip(a, b)) / len(a)


class Attention:
    """Raised attention starts at a significant change and ends after `calm` quiet frames."""

    def __init__(self, threshold=0.10, calm=2):
        self.threshold, self.calm = threshold, calm
        self.alert = False
        self.quiet = 0

    def observe(self, fraction):
        """Feed one frame's change fraction; None means no comparison was possible."""
        if fraction is not None and fraction > self.threshold:
            self.alert, self.quiet = True, 0
        elif self.alert:
            self.quiet += 1
            if self.quiet >= self.calm:
                self.alert, self.quiet = False, 0
        return self.alert
