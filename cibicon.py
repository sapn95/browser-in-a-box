"""The launcher's icon, drawn rather than borrowed.

Chrome's own icns tops out at 256 px, so copying it across leaves the Dock
upscaling — and it says nothing about what the launcher actually opens, which is
a *second* Chrome in a box. So this draws the thing the name describes: the
browser coming up out of a cardboard box, with the box's front panel in front of
it.

Vector, as a small hand-written PDF, because `sips` rasterises PDF at any size
with anti-aliasing and keeps the transparency outside the artwork. That gives a
crisp 1024 with no image library anywhere near it.
"""

from __future__ import annotations

import math

SIZE = 1024

WHITE = (1.0, 1.0, 1.0)

# Kept for the tests and for anything that wants the default without importing the
# browser table. The palette a drawing actually uses comes in as an argument.
RED = (0.918, 0.263, 0.208)
YELLOW = (0.984, 0.737, 0.020)
GREEN = (0.204, 0.659, 0.325)
BLUE = (0.259, 0.522, 0.957)
DEFAULT_PALETTE = (RED, GREEN, YELLOW, BLUE)

# Corrugated cardboard, lit from above: the flaps catch more light than the face.
CARTON_FACE = (0.776, 0.541, 0.275)
CARTON_FLAP = (0.867, 0.651, 0.384)
CARTON_EDGE = (0.659, 0.435, 0.184)
CARTON_INSIDE = (0.549, 0.353, 0.145)
PLATE = (0.949, 0.941, 0.925)


def _arc(cx: float, cy: float, r: float, start: float, end: float) -> list[str]:
    """Bezier segments along a circle. Split so no single one exceeds a quarter turn,
    which is where the usual approximation starts to visibly drift."""
    segments = []
    steps = max(1, math.ceil(abs(end - start) / (math.pi / 2)))
    for step in range(steps):
        a = start + (end - start) * step / steps
        b = start + (end - start) * (step + 1) / steps
        k = 4 / 3 * math.tan((b - a) / 4)
        ax, ay = cx + r * math.cos(a), cy + r * math.sin(a)
        bx, by = cx + r * math.cos(b), cy + r * math.sin(b)
        segments.append(
            f"{ax - k * r * math.sin(a):.2f} {ay + k * r * math.cos(a):.2f} "
            f"{bx + k * r * math.sin(b):.2f} {by - k * r * math.cos(b):.2f} "
            f"{bx:.2f} {by:.2f} c"
        )
    return segments


def _fill(colour: tuple[float, float, float]) -> str:
    return f"{colour[0]:.3f} {colour[1]:.3f} {colour[2]:.3f} rg"


def _circle(cx: float, cy: float, r: float, colour) -> list[str]:
    return [_fill(colour), f"{cx + r:.2f} {cy:.2f} m", *_arc(cx, cy, r, 0, 2 * math.pi), "h", "f"]


def _wedge(cx: float, cy: float, r: float, start: float, end: float, colour) -> list[str]:
    return [
        _fill(colour),
        f"{cx:.2f} {cy:.2f} m",
        f"{cx + r * math.cos(start):.2f} {cy + r * math.sin(start):.2f} l",
        *_arc(cx, cy, r, start, end),
        "h",
        "f",
    ]


def _polygon(points: list[tuple[float, float]], colour) -> list[str]:
    head, *rest = points
    return [
        _fill(colour),
        f"{head[0]:.2f} {head[1]:.2f} m",
        *[f"{x:.2f} {y:.2f} l" for x, y in rest],
        "h",
        "f",
    ]


def _rounded_rect(x: float, y: float, w: float, h: float, r: float, colour) -> list[str]:
    return [
        _fill(colour),
        f"{x + r:.2f} {y:.2f} m",
        f"{x + w - r:.2f} {y:.2f} l",
        *_arc(x + w - r, y + r, r, -math.pi / 2, 0),
        f"{x + w:.2f} {y + h - r:.2f} l",
        *_arc(x + w - r, y + h - r, r, 0, math.pi / 2),
        f"{x + r:.2f} {y + h:.2f} l",
        *_arc(x + r, y + h - r, r, math.pi / 2, math.pi),
        f"{x:.2f} {y + r:.2f} l",
        *_arc(x + r, y + r, r, math.pi, 3 * math.pi / 2),
        "h",
        "f",
    ]


def _flap(x: float, y: float, base: float, reach: float, lean: tuple[float, float]) -> list[tuple]:
    """A folded-open flap: the box's top edge, swung out along `lean`.

    Built as a parallelogram off the real top edge rather than a free-floating
    wedge, so it stays visibly hinged to the box at every size.
    """
    length = math.hypot(*lean)
    dx, dy = lean[0] / length * reach, lean[1] / length * reach
    return [(x, y), (x + base, y), (x + base + dx, y + dy), (x + dx, y + dy)]


def _wheel(cx: float, cy: float, r: float, palette: tuple) -> list[str]:
    """Chrome's, and Chromium's: three sectors around a ringed centre."""
    top, lower_left, lower_right, centre = palette
    return [
        *_wedge(cx, cy, r, math.radians(30), math.radians(150), top),
        *_wedge(cx, cy, r, math.radians(150), math.radians(270), lower_left),
        *_wedge(cx, cy, r, math.radians(270), math.radians(390), lower_right),
        *_circle(cx, cy, r * 0.46, WHITE),
        *_circle(cx, cy, r * 0.37, centre),
    ]


def _flame(cx: float, cy: float, r: float, palette: tuple) -> list[str]:
    """Firefox's: a tail swept round a globe, open where the tail ends.

    Not a wheel with different colours. The gap at the lower right is the whole
    difference — it is what makes the ring read as something wrapped around the
    globe rather than a pie chart of it.
    """
    top, lower_left, lower_right, centre = palette
    return [
        # The tail, in three tones, warm at the top and cool where it trails off.
        *_wedge(cx, cy, r, math.radians(-40), math.radians(60), lower_right),
        *_wedge(cx, cy, r, math.radians(60), math.radians(165), top),
        *_wedge(cx, cy, r, math.radians(165), math.radians(250), lower_left),
        *_circle(cx, cy, r * 0.60, centre),
    ]


MARKS = {"wheel": _wheel, "flame": _flame}


def _artwork(palette: tuple = (), mark: str = "wheel") -> list[str]:
    """Back to front: plate, the flaps behind, the browser, then the box in front.

    The mark is the browser's own shape, not a recolour of one shape: Chrome and
    Chromium really are the same wheel in different colours, but Firefox is not a
    wheel at all, and colouring one orange would just look like a broken Chrome.
    """
    ball_x, ball_y, ball_r = 512.0, 636.0, 196.0
    box_left, box_right = 246.0, 778.0
    box_bottom, box_top = 214.0, 524.0

    ops = _rounded_rect(48, 48, SIZE - 96, SIZE - 96, 228, PLATE)

    # Flaps first, so the browser sits in front of them — which is what makes it
    # read as coming *out* of the box rather than standing behind one.
    ops += _polygon(_flap(box_left, box_top, 132, 178, (-0.78, 0.63)), CARTON_FLAP)
    ops += _polygon(_flap(box_right - 132, box_top, 132, 178, (0.78, 0.63)), CARTON_FLAP)

    # The inside of the far wall, seen through the opening behind the browser.
    ops += _polygon(
        [
            (box_left + 18, box_top),
            (box_right - 18, box_top),
            (box_right - 18, box_top + 40),
            (box_left + 18, box_top + 40),
        ],
        CARTON_INSIDE,
    )

    ops += MARKS[mark](ball_x, ball_y, ball_r, palette or DEFAULT_PALETTE)

    # The near wall last, over the browser, so the bottom of it is inside the box.
    ops += _polygon(
        [
            (box_left, box_bottom),
            (box_right, box_bottom),
            (box_right, box_top),
            (box_left, box_top),
        ],
        CARTON_FACE,
    )
    # A lip along the top of the near wall: without it the card has no thickness and
    # the browser looks pasted on rather than standing in something.
    ops += _polygon(
        [
            (box_left, box_top),
            (box_right, box_top),
            (box_right, box_top - 26),
            (box_left, box_top - 26),
        ],
        CARTON_EDGE,
    )
    return ops


def pdf(palette: tuple = (), mark: str = "wheel") -> bytes:
    """The whole icon as a one-page PDF."""
    content = "\n".join(_artwork(palette, mark)).encode()
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 {SIZE} {SIZE}]/Contents 4 0 R>>".encode(),
        b"<</Length " + str(len(content)).encode() + b">>stream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj".encode() + body + b"endobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{start}\n%%EOF\n".encode()
    return bytes(out)
