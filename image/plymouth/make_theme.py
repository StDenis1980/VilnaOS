#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Make the images of the Vilna Plymouth theme.

    make_theme.py LOGO OUT [--svg-only]

LOGO is the Vilna logo for dark backgrounds (docs/design/logo/
Vilna_logo_dark-bg.svg): one cream path for the brush circle and three
paths for the swallow. OUT gets the PNG files that vilna.script loads,
drawn by rsvg-convert from SVG made here (--svg-only keeps the SVG and
skips rsvg-convert).

Everything except the logo is drawn here: the glow, the dots, the
password field and the corner motifs. The motifs are abstract brush
strokes in the manner of Petrykivka painting, not copies of any work
(docs/design/brief.md, section 5): real Petrykivka by contracted
masters replaces them later.

Only the Python standard library is needed, so the RPM builds it with
python3 and rsvg-convert.
"""

from __future__ import annotations

import argparse
import itertools
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Palette (docs/design/brief.md, section 2)
NIGHT = "#1F345C"
BLUE = "#2D6BFF"
GOLD = "#F5C24A"
ORANGE = "#F0963A"
RUST = "#C2581E"
CREAM = "#F2E8D5"
BERRY = "#B4473A"
LEAF = "#5E9C76"

CIRCLE_FRAMES = 30  # the brush closes into a circle
BLOOM_FRAMES = 24  # a corner motif grows
LOGO_HEIGHT = 480  # px; vilna.script scales down (and up on very large screens)
BLOOM_SIZE = 560
CIRCLE_COLOR = CREAM.upper()


def logo_paths(svg: str) -> tuple[str, str, list[tuple[str, str]]]:
    """The viewBox, the circle path and the swallow paths (fill, d) of the logo."""
    view = re.search(r'viewBox="([^"]+)"', svg)
    if not view:
        raise SystemExit("logo: no viewBox")
    paths = re.findall(r'<path\s+fill="(#[0-9A-Fa-f]{6})"[^>]*?\sd="([^"]+)"', svg)
    circle = [d for fill, d in paths if fill.upper() == CIRCLE_COLOR]
    bird = [(fill, d) for fill, d in paths if fill.upper() != CIRCLE_COLOR]
    if len(circle) != 1 or not bird:
        raise SystemExit(f"logo: expected one {CIRCLE_COLOR} circle path and the swallow")
    return view.group(1), circle[0], bird


def bbox(d: str) -> tuple[float, float, float, float]:
    """Bounding box of the points of an absolute path (good enough for a centre)."""
    pts = [
        (float(x), float(y)) for x, y in re.findall(r"(-?\d+(?:\.\d+)?)[ ,](-?\d+(?:\.\d+)?)", d)
    ]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def ease_in_out(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u)


def ease_out(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return 1 - (1 - u) ** 3


def wedge(cx: float, cy: float, r: float, start: float, sweep: float) -> str:
    """A pie wedge from angle start (degrees, screen coordinates) turning
    anticlockwise on screen by sweep degrees, as the brush moves."""
    if sweep <= 0:
        return ""
    if sweep >= 360:
        return f"M{cx - r},{cy - r}h{2 * r}v{2 * r}h{-2 * r}Z"
    steps = max(2, int(sweep / 4) + 1)
    pts = [(cx, cy)]
    for i in range(steps + 1):
        a = math.radians(start - sweep * i / steps)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return "M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + "Z"


def svg_doc(view: str, width: int, height: int, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" '
        f'width="{width}" height="{height}">{body}</svg>\n'
    )


# The brush starts at the thin tail at the top right of the circle and
# runs anticlockwise (on screen) round the left and the bottom.
BRUSH_START = -72.0
BRUSH_SWEEP = 372.0


def circle_frames(view: str, circle: str) -> list[str]:
    vx, vy, vw, vh = (float(v) for v in view.split())
    x0, y0, x1, y1 = bbox(circle)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    r = max(vw, vh) * 1.5
    width = round(LOGO_HEIGHT * vw / vh)
    frames = []
    for i in range(CIRCLE_FRAMES):
        p = ease_in_out(i / (CIRCLE_FRAMES - 1))
        sweep = BRUSH_SWEEP * p
        # The head of the brush is soft: two thin wedges ahead of it,
        # fainter, as paint thinning at the end of a stroke.
        head = 16 * (1 - p)
        mask = "".join(
            f'<path d="{wedge(cx, cy, r, BRUSH_START, sweep + head * k / 6)}" '
            f'fill="#fff" fill-opacity="{0.16 if k else 1}"/>'
            for k in range(6, -1, -1)
            if sweep + head * k / 6 > 0
        )
        body = (
            f'<mask id="m" maskUnits="userSpaceOnUse" x="{vx}" y="{vy}" width="{vw}" height="{vh}">'
            f"{mask}</mask>"
            f'<path fill="{CREAM}" fill-rule="evenodd" mask="url(#m)" d="{circle}"/>'
        )
        frames.append(svg_doc(view, width, LOGO_HEIGHT, body))
    return frames


def swallow(view: str, bird: list[tuple[str, str]]) -> str:
    _, _, vw, vh = (float(v) for v in view.split())
    width = round(LOGO_HEIGHT * vw / vh)
    body = "".join(f'<path fill="{fill}" fill-rule="evenodd" d="{d}"/>' for fill, d in bird)
    return svg_doc(view, width, LOGO_HEIGHT, body)


def glow() -> str:
    return svg_doc(
        "0 0 100 100",
        768,
        768,
        '<defs><radialGradient id="g" cx="50" cy="50" r="50" gradientUnits="userSpaceOnUse">'
        f'<stop offset="0" stop-color="{BLUE}" stop-opacity="0.42"/>'
        f'<stop offset="0.45" stop-color="{BLUE}" stop-opacity="0.16"/>'
        f'<stop offset="1" stop-color="{BLUE}" stop-opacity="0"/>'
        '</radialGradient></defs><rect width="100" height="100" fill="url(#g)"/>',
    )


def dot() -> str:
    """A gold node with a ring, as in the network of the Vilna artwork."""
    return svg_doc(
        "0 0 48 48",
        48,
        48,
        f'<circle cx="24" cy="24" r="17" fill="none" stroke="{GOLD}" stroke-opacity="0.55" stroke-width="3"/>'
        f'<circle cx="24" cy="24" r="9" fill="{GOLD}"/>',
    )


def entry() -> str:
    return svg_doc(
        "0 0 640 72",
        640,
        72,
        f'<rect x="2" y="2" width="636" height="68" rx="34" fill="#16264A" fill-opacity="0.92" '
        f'stroke="{GOLD}" stroke-width="3"/>',
    )


def bullet() -> str:
    return svg_doc("0 0 24 24", 24, 24, f'<circle cx="12" cy="12" r="8" fill="{CREAM}"/>')


# Corner motif ------------------------------------------------------------


def bezier(p: list[tuple[float, float]], t: float) -> tuple[float, float]:
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = p
    m = 1 - t
    return (
        m**3 * x0 + 3 * m * m * t * x1 + 3 * m * t * t * x2 + t**3 * x3,
        m**3 * y0 + 3 * m * m * t * y1 + 3 * m * t * t * y2 + t**3 * y3,
    )


def tangent(p: list[tuple[float, float]], t: float) -> float:
    """Direction of a cubic Bézier at t, in degrees."""
    a = bezier(p, max(0.0, t - 0.01))
    b = bezier(p, min(1.0, t + 0.01))
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))


def length(p: list[tuple[float, float]]) -> float:
    pts = [bezier(p, i / 200) for i in range(201)]
    return sum(math.dist(a, b) for a, b in itertools.pairwise(pts))


def grain(length_: float, width: float) -> str:
    """The Petrykivka 'grain' stroke: a teardrop with a curved tail, from
    the origin along +x."""
    L, W = length_, width
    return (
        f"M0,0C{0.3 * L:.1f},{-0.55 * W:.1f} {0.8 * L:.1f},{-0.5 * W:.1f} {L:.1f},0"
        f"C{0.75 * L:.1f},{0.2 * W:.1f} {0.35 * L:.1f},{0.45 * W:.1f} 0,0Z"
    )


def petal(length_: float, width: float) -> str:
    L, W = length_, width
    return (
        f"M0,0C{0.15 * L:.1f},{-0.65 * W:.1f} {0.95 * L:.1f},{-0.7 * W:.1f} {L:.1f},0"
        f"C{0.95 * L:.1f},{0.7 * W:.1f} {0.15 * L:.1f},{0.65 * W:.1f} 0,0Z"
    )


def place(
    shape: str, x: float, y: float, angle: float, scale: float, fill: str, opacity: float = 1
) -> str:
    if scale <= 0.01:
        return ""
    return (
        f'<path d="{shape}" fill="{fill}" fill-opacity="{opacity:.2f}" '
        f'transform="translate({x:.1f},{y:.1f}) rotate({angle:.1f}) scale({scale:.3f})"/>'
    )


def stem(p: list[tuple[float, float]], drawn: float, width: float, color: str) -> str:
    if drawn <= 0:
        return ""
    total = length(p)
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = p
    return (
        f'<path d="M{x0},{y0}C{x1},{y1} {x2},{y2} {x3},{y3}" fill="none" stroke="{color}" '
        f'stroke-width="{width}" stroke-linecap="round" '
        f'stroke-dasharray="{total * drawn:.1f} {total + 10:.1f}"/>'
    )


def branch(
    p: list[tuple[float, float]],
    u: float,
    leaves: list[tuple[float, int, float, str]],
    width: float,
) -> str:
    """A stem drawn up to u (0..1) with grains along it: (t, side, size, colour)."""
    out = [stem(p, u, width, GOLD)]
    for t, side, size, color in leaves:
        grow = ease_out((u - t) / 0.18)
        if grow <= 0:
            continue
        x, y = bezier(p, t)
        angle = tangent(p, t) + side * 52
        out.append(place(grain(size, size * 0.5), x, y, angle, grow, color, 0.92))
    return "".join(out)


def flower(x: float, y: float, facing: float, u: float, size: float) -> str:
    """A fan of petals opening one after another around a gold centre."""
    out = []
    count = 9
    for i in range(count):
        a = facing - 95 + 190 * i / (count - 1)
        start = 0.05 * min(i, count - 1 - i)  # the middle petals open last
        grow = ease_out((u - start) / 0.6)
        out.append(place(petal(size, size * 0.36), x, y, a, grow, ORANGE if i % 2 else GOLD, 0.95))
    for i in range(7):
        a = facing - 75 + 150 * i / 6
        grow = ease_out((u - 0.25 - 0.04 * i) / 0.5)
        out.append(place(petal(size * 0.58, size * 0.3), x, y, a, grow, RUST))
    core = ease_out((u - 0.15) / 0.5)
    if core > 0:
        r = size * 0.2 * core
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{GOLD}"/>')
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r * 0.55:.1f}" fill="{BERRY}"/>')
    return "".join(out)


def berries(x: float, y: float, u: float, size: float) -> str:
    grow = ease_out(u)
    if grow <= 0:
        return ""
    r = size * grow
    return "".join(
        f'<circle cx="{x + dx * size:.1f}" cy="{y + dy * size:.1f}" r="{r:.1f}" fill="{BERRY}"/>'
        f'<circle cx="{x + dx * size - r * 0.3:.1f}" cy="{y + dy * size - r * 0.3:.1f}" '
        f'r="{r * 0.25:.1f}" fill="{CREAM}" fill-opacity="0.7"/>'
        for dx, dy in ((0, 0), (1.9, -0.6), (0.9, 1.4))
    )


# The motif sits in the bottom-left corner: the main stem rises from the
# corner and ends in a flower, a side stem carries berries.
MAIN = [(20.0, 1010.0), (90.0, 720.0), (260.0, 440.0), (520.0, 330.0)]
SIDE = [(118.0, 690.0), (300.0, 640.0), (480.0, 620.0), (680.0, 700.0)]
MAIN_LEAVES = [
    (0.08, 1, 190, ORANGE),
    (0.14, -1, 175, LEAF),
    (0.24, 1, 165, GOLD),
    (0.32, -1, 150, ORANGE),
    (0.42, 1, 140, LEAF),
    (0.51, -1, 128, GOLD),
    (0.61, 1, 115, ORANGE),
    (0.70, -1, 100, LEAF),
    (0.79, 1, 85, GOLD),
]
SIDE_LEAVES = [
    (0.22, -1, 115, GOLD),
    (0.40, 1, 105, LEAF),
    (0.58, -1, 92, ORANGE),
    (0.75, 1, 80, GOLD),
]


def bloom_frames() -> list[str]:
    frames = []
    for i in range(BLOOM_FRAMES):
        u = i / (BLOOM_FRAMES - 1)
        main_u = ease_in_out(u / 0.65)
        side_u = ease_in_out((u - 0.25) / 0.55)
        x, y = MAIN[-1]
        body = (
            branch(SIDE, side_u, SIDE_LEAVES, 7)
            + berries(*SIDE[-1], (u - 0.75) / 0.25, 22)
            + branch(MAIN, main_u, MAIN_LEAVES, 11)
            + flower(x, y, tangent(MAIN, 1.0) - 20, (u - 0.55) / 0.45, 230)
        )
        frames.append(svg_doc("0 0 1000 1000", BLOOM_SIZE, BLOOM_SIZE, body))
    return frames


def write(out: Path, name: str, svg: str, svg_only: bool) -> None:
    src = out / f"{name}.svg"
    src.write_text(svg, encoding="utf-8")
    if svg_only:
        return
    subprocess.run(["rsvg-convert", "-o", str(out / f"{name}.png"), str(src)], check=True)
    src.unlink()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("logo", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--svg-only", action="store_true")
    args = ap.parse_args(argv)
    if not args.svg_only and not shutil.which("rsvg-convert"):
        print("rsvg-convert not found", file=sys.stderr)
        return 1
    view, circle, bird = logo_paths(args.logo.read_text(encoding="utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)
    for i, frame in enumerate(circle_frames(view, circle)):
        write(args.out, f"circle-{i}", frame, args.svg_only)
    write(args.out, "swallow", swallow(view, bird), args.svg_only)
    for i, frame in enumerate(bloom_frames()):
        write(args.out, f"bloom-{i}", frame, args.svg_only)
    write(args.out, "glow", glow(), args.svg_only)
    write(args.out, "dot", dot(), args.svg_only)
    write(args.out, "entry", entry(), args.svg_only)
    write(args.out, "bullet", bullet(), args.svg_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
