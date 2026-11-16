# SPDX-License-Identifier: GPL-3.0-or-later
"""The branch that grows while Vilna installs, and the release sign at its end.

A decorative picture above the progress bar (the bar and the list of
steps carry the meaning; screen readers skip this). The stem grows with
the engine's percent, leaves and cornflowers open as it passes them, and
when the installation is done the sign of the current release sits at
the end of the branch.

Two layers can be replaced without touching the window:

- the drawing: until the designer's animation arrives this module draws
  an abstract branch in code. If ``data/branch/light/`` and
  ``data/branch/dark/`` (or ``data/branch/`` for both styles) hold
  numbered PNG frames (``branch-01.png`` …), the window shows the frame
  for the current percent instead, scaled to fit;
- the release layer (``release.py``): the sign and the name of the
  current release.

With animations turned off (Settings → Accessibility → Reduce
animation, ``gtk-enable-animations``) the picture jumps to each new
percent instead of growing smoothly.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from vilna_installer import release  # noqa: E402

FRAMES = Path(__file__).resolve().parent / "data" / "branch"
MAX_FRAMES = 240
GROW_PER_SECOND = 0.6  # how fast the drawn branch catches up with the percent

# Palette (docs/design/brief.md, section 2)
GOLD = (0.961, 0.761, 0.290)
ORANGE = (0.941, 0.588, 0.227)
LEAF = (0.369, 0.612, 0.463)
CORNFLOWER = (0.176, 0.420, 1.0)
CORNFLOWER_DARK = (0.357, 0.549, 1.0)
CREAM = (0.949, 0.910, 0.835)
NIGHT = (0.122, 0.204, 0.361)

# The stem: a cubic Bézier in a 1000 × 300 box, from bottom left to the right.
STEM = ((40.0, 250.0), (300.0, 290.0), (600.0, 60.0), (900.0, 120.0))
# Where along the stem leaves (t, side, colour) and cornflowers (t) open.
LEAVES = (
    (0.10, 1, GOLD),
    (0.18, -1, LEAF),
    (0.28, 1, ORANGE),
    (0.40, -1, GOLD),
    (0.50, 1, LEAF),
    (0.62, -1, ORANGE),
    (0.72, 1, GOLD),
    (0.84, -1, LEAF),
)
FLOWERS = (0.34, 0.6, 0.8)


def frames(folder: Path = FRAMES) -> list[Path]:
    """The designer's frames in the order of the number in their names, or none."""
    if not folder.is_dir():
        return []

    def number(path: Path) -> int:
        found = re.findall(r"\d+", path.stem)
        return int(found[-1]) if found else -1

    found = [p for p in folder.glob("*.png") if number(p) >= 0]
    return sorted(found, key=number)[:MAX_FRAMES]


def point(t: float) -> tuple[float, float]:
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = STEM
    u = 1 - t
    return (
        u**3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t**3 * x3,
        u**3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t**3 * y3,
    )


def angle(t: float) -> float:
    a, b = point(max(0.0, t - 0.01)), point(min(1.0, t + 0.01))
    return math.atan2(b[1] - a[1], b[0] - a[0])


def opened(at: float, grown: float, span: float = 0.06) -> float:
    """How open (0–1) a leaf or flower at ``at`` is when the stem reached ``grown``."""
    return min(1.0, max(0.0, (grown - at) / span))


class Branch(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__(accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self.set_content_height(150)
        self.set_content_width(480)
        self.set_hexpand(True)
        self.target = 0.0  # the engine's fraction
        self.shown = 0.0  # what is drawn now
        self.done = False
        self.mark_alpha = 0.0
        self._last_time: int | None = None
        self._tick: int | None = None
        self._frame_sets: dict[bool, list[GdkPixbuf.Pixbuf]] = {}
        self._mark: dict[bool, GdkPixbuf.Pixbuf | None] = {}
        self.set_draw_func(self._draw)

    # what the window says

    def set_fraction(self, fraction: float) -> None:
        self.target = min(1.0, max(0.0, fraction))
        self._move()

    def finish(self) -> None:
        """The installation is done: the branch is whole and the release sign sits on it."""
        self.done = True
        self.target = 1.0
        self._move()

    @property
    def animate(self) -> bool:
        settings = Gtk.Settings.get_default()
        return bool(settings is None or settings.get_property("gtk-enable-animations"))

    def _move(self) -> None:
        if not self.animate:
            self.shown = self.target
            self.mark_alpha = 1.0 if self.done else 0.0
            self.queue_draw()
            return
        if self._tick is None:
            self._last_time = None
            self._tick = self.add_tick_callback(self._on_tick)

    def _on_tick(self, _widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        now = clock.get_frame_time()
        step = 0.0 if self._last_time is None else (now - self._last_time) / 1_000_000
        self._last_time = now
        self.shown = min(self.target, self.shown + step * GROW_PER_SECOND)
        if self.target < self.shown:  # never shrinks, but follow a lower target
            self.shown = self.target
        if self.done and self.shown >= 1.0:
            self.mark_alpha = min(1.0, self.mark_alpha + step * 2.5)
        self.queue_draw()
        if self.shown >= self.target and (not self.done or self.mark_alpha >= 1.0):
            self._tick = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    # drawing

    @staticmethod
    def _pixbuf(path: Path) -> GdkPixbuf.Pixbuf | None:
        try:
            return GdkPixbuf.Pixbuf.new_from_file(str(path))
        except GLib.Error:
            return None

    def _mark_pixbuf(self, dark: bool, size: int) -> GdkPixbuf.Pixbuf | None:
        if dark not in self._mark:
            try:
                self._mark[dark] = GdkPixbuf.Pixbuf.new_from_file_at_size(
                    str(release.mark(dark)), size, size
                )
            except GLib.Error:
                self._mark[dark] = None
        return self._mark[dark]

    def frames_for(self, dark: bool) -> list[GdkPixbuf.Pixbuf]:
        """The designer's frames for this style, loaded once; none means draw."""
        if dark not in self._frame_sets:
            folder = FRAMES / ("dark" if dark else "light")
            paths = frames(folder) or frames(FRAMES)
            loaded = [self._pixbuf(p) for p in paths]
            self._frame_sets[dark] = [f for f in loaded if f is not None]
        return self._frame_sets[dark]

    def _draw(self, _area: Gtk.DrawingArea, cr, width: int, height: int) -> None:
        dark = Adw.StyleManager.get_default().get_dark()
        if self.frames_for(dark):
            self._draw_frame(cr, width, height, self.frames_for(dark))
        else:
            self._draw_branch(cr, width, height, dark)
        if self.mark_alpha > 0:
            self._draw_mark(cr, width, height, dark)

    def _box(self, width: int, height: int) -> tuple[float, float, float]:
        """Scale and offsets that fit the 1000 × 300 box into the widget, centred."""
        scale = min(width / 1000, height / 300)
        return scale, (width - 1000 * scale) / 2, (height - 300 * scale) / 2

    def _draw_frame(self, cr, width: int, height: int, frames: list) -> None:
        index = round(self.shown * (len(frames) - 1))
        pixbuf = frames[index]
        fw, fh = pixbuf.get_width(), pixbuf.get_height()
        scale = min(width / fw, height / fh)
        cr.save()
        cr.translate((width - fw * scale) / 2, (height - fh * scale) / 2)
        cr.scale(scale, scale)
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
        cr.paint()
        cr.restore()

    def _draw_branch(self, cr, width: int, height: int, dark: bool) -> None:
        scale, ox, oy = self._box(width, height)
        grown = self.shown
        if grown <= 0:
            return
        cr.save()
        cr.translate(ox, oy)
        cr.scale(scale, scale)
        # Stem, drawn up to ``grown`` as short segments, thinner towards the tip.
        steps = max(2, int(80 * grown))
        stem = CREAM if dark else NIGHT
        cr.set_source_rgb(*stem)
        cr.set_line_cap(1)  # round
        prev = point(0.0)
        for i in range(1, steps + 1):
            t = grown * i / steps
            p = point(t)
            cr.set_line_width(9 - 5 * t)
            cr.move_to(*prev)
            cr.line_to(*p)
            cr.stroke()
            prev = p
        for at, side, colour in LEAVES:
            self._leaf(cr, at, side, colour, opened(at, grown))
        for at in FLOWERS:
            self._flower(cr, at, opened(at, grown, 0.08), dark)
        cr.restore()

    @staticmethod
    def _leaf(cr, at: float, side: int, colour: tuple[float, float, float], u: float) -> None:
        if u <= 0:
            return
        x, y = point(at)
        length = 70 * u
        cr.save()
        cr.translate(x, y)
        cr.rotate(angle(at) - side * 0.9)
        # A grain-shaped leaf: two arcs meeting in points, as in Petrykivka strokes.
        cr.move_to(0, 0)
        cr.curve_to(length * 0.3, -length * 0.28, length * 0.75, -length * 0.22, length, 0)
        cr.curve_to(length * 0.75, length * 0.12, length * 0.3, length * 0.14, 0, 0)
        cr.set_source_rgb(*colour)
        cr.fill()
        cr.restore()

    @staticmethod
    def _flower(cr, at: float, u: float, dark: bool) -> None:
        if u <= 0:
            return
        x, y = point(at)
        y -= 34 * u
        radius = 30 * u
        petal = CORNFLOWER_DARK if dark else CORNFLOWER
        cr.save()
        cr.translate(x, y)
        for i in range(8):
            cr.save()
            cr.rotate(i * math.pi / 4)
            cr.move_to(radius * 0.25, 0)
            cr.curve_to(radius * 0.6, -radius * 0.35, radius, -radius * 0.25, radius, 0)
            cr.curve_to(radius, radius * 0.25, radius * 0.6, radius * 0.35, radius * 0.25, 0)
            cr.set_source_rgb(*petal)
            cr.fill()
            cr.restore()
        cr.arc(0, 0, radius * 0.28, 0, 2 * math.pi)
        cr.set_source_rgb(*GOLD)
        cr.fill()
        cr.restore()

    def _draw_mark(self, cr, width: int, height: int, dark: bool) -> None:
        scale, ox, oy = self._box(width, height)
        size = max(24, int(110 * scale))
        pixbuf = self._mark_pixbuf(dark, size)
        if pixbuf is None:
            return
        x, y = point(1.0)
        cx, cy = ox + x * scale, oy + y * scale
        # Lands from a little above while it fades in.
        lift = (1 - self.mark_alpha) * size * 0.4
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, cx - size / 2, cy - size * 0.9 - lift)
        cr.paint_with_alpha(self.mark_alpha)
