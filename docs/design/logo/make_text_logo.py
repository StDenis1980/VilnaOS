#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Make the wide Vilna logo for Settings → About (vilna-logo-text icons).

    make_text_logo.py FONT_BOLD FONT_MEDIUM OUT_DIR

GNOME Settings 46 shows the distribution logo on its About page and
looks for icons named after LOGO= in os-release: in a dark style first
"<LOGO>-text-dark", then "<LOGO>-text", "<LOGO>-dark", "<LOGO>". This
draws the two text variants (vilna-logo-text.svg, vilna-logo-text-dark.svg):
the Vilna logo, the word "Vilna" and under it the release layer, the
sign and the name of the current release (Blavet). It also draws
vilna-logo-login.svg, the logo and the word in one row for the bottom
of the (always dark) login screen.

The words are outlines taken from Inter (OFL-1.1, the face Adwaita Sans
is built from), so the icons need no font at run time. Only this script
needs fontTools and the font files (Debian/Ubuntu: fonts-inter, e.g.
/usr/share/fonts/opentype/inter/InterDisplay-Bold.otf and
Inter-Medium.otf); the SVG files it writes are committed, so package
builds do not run it.

Release layer: RELEASE_NAME and RELEASE_MARK_* below. The next release
changes them and runs the script again.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LOGO_LIGHT = ROOT / "docs/design/logo/Vilna_logo_color.svg"
LOGO_DARK = ROOT / "docs/design/logo/Vilna_logo_dark-bg.svg"

# Release layer
RELEASE_NAME = "Blavet"
RELEASE_MARK_LIGHT = ROOT / "installer/src/vilna_installer/data/blavet-mark-light.svg"
RELEASE_MARK_DARK = ROOT / "installer/src/vilna_installer/data/blavet-mark-dark.svg"

NIGHT = "#1F345C"
CREAM = "#F2E8D5"
MUTED_LIGHT = "#55607A"
MUTED_DARK = "#9AA6BF"

HEIGHT = 200  # px; the width follows the words. The About page scales it to fit
LOGO_BOX = 184  # the logo is drawn in a square of this size
WORD_SIZE = 96  # cap-to-baseline size of "Vilna", px
RELEASE_SIZE = 30  # the release name, px
MARK_SIZE = 40
LOGIN_HEIGHT = 40  # px, the logo at the bottom of the login screen


def words(
    font_path: Path, text: str, size: float, x: float, baseline: float
) -> tuple[str, float]:
    """The outline of TEXT as one SVG path and the width it takes."""
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.ttLib import TTFont

    font = TTFont(str(font_path))
    scale = size / font["head"].unitsPerEm
    cmap = font.getBestCmap()
    glyphs = font.getGlyphSet()
    hmtx = font["hmtx"]
    pen = SVGPathPen(glyphs, ntos=lambda v: f"{v:.1f}".rstrip("0").rstrip("."))
    cursor = 0.0
    for ch in text:
        name = cmap.get(ord(ch))
        if name is None:
            raise SystemExit(f"{font_path.name}: no glyph for {ch!r}")
        glyphs[name].draw(
            TransformPen(pen, (scale, 0, 0, -scale, x + cursor, baseline))
        )
        cursor += hmtx[name][0] * scale
    return pen.getCommands(), cursor


def inner(svg_path: Path, x: float, y: float, size: float) -> str:
    """An SVG file placed as a nested <svg> in a square."""
    svg = svg_path.read_text(encoding="utf-8")
    view = re.search(r'viewBox="([^"]+)"', svg)
    body = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.DOTALL)
    if not view or not body:
        raise SystemExit(f"{svg_path.name}: not a simple SVG file")
    content = re.sub(r"<title>.*?</title>", "", body.group(1), flags=re.DOTALL)
    return (
        f'<svg x="{x}" y="{y}" width="{size}" height="{size}" viewBox="{view.group(1)}">'
        f"{content}</svg>"
    )


def text_logo(bold: Path, medium: Path, *, dark: bool) -> str:
    ink = CREAM if dark else NIGHT
    muted = MUTED_DARK if dark else MUTED_LIGHT
    logo = LOGO_DARK if dark else LOGO_LIGHT
    mark = RELEASE_MARK_DARK if dark else RELEASE_MARK_LIGHT
    left = (HEIGHT - LOGO_BOX) / 2
    text_x = left + LOGO_BOX + 28
    word_baseline = HEIGHT / 2 + 16
    word, word_w = words(bold, "Vilna", WORD_SIZE, text_x, word_baseline)
    mark_y = word_baseline + 18
    release, release_w = words(
        medium,
        RELEASE_NAME,
        RELEASE_SIZE,
        text_x + MARK_SIZE + 10,
        mark_y + MARK_SIZE / 2 + RELEASE_SIZE * 0.36,
    )
    width = round(text_x + max(word_w, MARK_SIZE + 10 + release_w) + left)
    # Mark ids would clash with the logo's if both had gradients; prefix them.
    mark_svg = inner(mark, text_x, mark_y, MARK_SIZE)
    mark_svg = re.sub(r'id="([^"]+)"', r'id="rel-\1"', mark_svg)
    mark_svg = re.sub(r"url\(#([^)]+)\)", r"url(#rel-\1)", mark_svg)
    title = f"Vilna {RELEASE_NAME}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{HEIGHT}" '
        f'viewBox="0 0 {width} {HEIGHT}"><title>{title}</title>'
        f"{inner(logo, left, left, LOGO_BOX)}"
        f'<path fill="{ink}" d="{word}"/>'
        f'<g class="release">{mark_svg}<path fill="{muted}" d="{release}"/></g>'
        "</svg>\n"
    )


def login_logo(bold: Path) -> str:
    """The logo and the word Vilna in one row, for the dark login screen.

    GNOME Shell draws the login screen logo at its natural size, so the
    file says LOGIN_HEIGHT px high.
    """
    left = (HEIGHT - LOGO_BOX) / 2
    text_x = left + LOGO_BOX + 28
    word, word_w = words(
        bold, "Vilna", WORD_SIZE, text_x, HEIGHT / 2 + WORD_SIZE * 0.36
    )
    width = round(text_x + word_w + left)
    shown = round(width * LOGIN_HEIGHT / HEIGHT)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{shown}" height="{LOGIN_HEIGHT}" '
        f'viewBox="0 0 {width} {HEIGHT}"><title>Vilna</title>'
        f"{inner(LOGO_DARK, left, left, LOGO_BOX)}"
        f'<path fill="{CREAM}" d="{word}"/>'
        "</svg>\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    bold, medium, out = Path(argv[0]), Path(argv[1]), Path(argv[2])
    out.mkdir(parents=True, exist_ok=True)
    (out / "vilna-logo-text.svg").write_text(
        text_logo(bold, medium, dark=False), "utf-8"
    )
    (out / "vilna-logo-text-dark.svg").write_text(
        text_logo(bold, medium, dark=True), "utf-8"
    )
    (out / "vilna-logo-login.svg").write_text(login_logo(bold), "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
