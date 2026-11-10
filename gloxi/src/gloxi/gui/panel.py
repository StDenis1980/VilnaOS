# SPDX-License-Identifier: GPL-3.0-or-later
"""The Gloxi side panel, stage A: sizes, translucency and the owl's states.

Pure logic, without GTK, so it is tested on its own. The window
(``gloxi.gui.window``) draws it.

On Wayland a window cannot place itself. In Vilna the GNOME Shell extension
keeps Gloxi at the right edge (``shell-extension/.../panel.js``); Gloxi only
tells it the size it wants, over D-Bus (``gloxi.gui.shell``). Without the
extension Gloxi still opens at the same size and GNOME chooses where.
The numbers below are the extension's (``lib/geometry.js``); a test checks
that the two agree.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from enum import Enum
from gettext import gettext as _
from pathlib import Path

from gloxi.config import config_path

# The sizes the person can pick. "custom" is the size they dragged an edge to.
SIZES = ("compact", "one-sixth", "one-third", "custom")
FRACTIONS = {"one-sixth": 1 / 6, "one-third": 1 / 3}
MIN_WIDTH = 320
# Compact: the owl, the last two or three lines of the chat and the input.
COMPACT_HEIGHT = 220
MIN_HEIGHT = 160
MARGIN = 8

# Background opacity of the translucent panel. The chat bubbles stay opaque,
# so the text reads on any wallpaper.
DEFAULT_OPACITY = 0.8
MIN_OPACITY = 0.4

# A window this close to a preset size is still that preset, not "custom".
SIZE_SLACK = 8


@dataclass(frozen=True)
class PanelPrefs:
    size: str = "one-sixth"
    custom_width: int = 0
    custom_height: int = 0
    translucent: bool = True
    opacity: float = DEFAULT_OPACITY


def prefs_path(env: dict[str, str] | None = None) -> Path:
    """Next to config.toml: ``~/.config/gloxi/window.json``."""
    return config_path(env).with_name("window.json")


def load_prefs(path: Path | None = None) -> PanelPrefs:
    """The saved panel settings; anything missing or broken gives the defaults."""
    path = prefs_path() if path is None else path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return PanelPrefs()
    if not isinstance(data, dict):
        return PanelPrefs()
    return clean(PanelPrefs(), data)


def clean(base: PanelPrefs, data: dict) -> PanelPrefs:
    """Take only well-formed values from ``data``; the rest stays as in ``base``."""
    out = base
    size = data.get("size")
    if size in SIZES:
        out = replace(out, size=size)
    for key in ("custom_width", "custom_height"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100_000:
            out = replace(out, **{key: value})
    if isinstance(data.get("translucent"), bool):
        out = replace(out, translucent=data["translucent"])
    opacity = data.get("opacity")
    number = isinstance(opacity, int | float) and not isinstance(opacity, bool)
    if number and math.isfinite(opacity):
        out = replace(out, opacity=min(1.0, max(MIN_OPACITY, float(opacity))))
    if out.size == "custom" and not (out.custom_width and out.custom_height):
        out = replace(out, size="one-sixth")
    return out


def save_prefs(prefs: PanelPrefs, path: Path | None = None) -> None:
    """Write atomically; a failure to save is not worth stopping Gloxi for."""
    path = prefs_path() if path is None else path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".window-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(prefs), f, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def _finite(value, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def panel_width(screen_width, size: str) -> int:
    """A sixth or a third of the screen, at least 320 px, at most half (as the strip)."""
    sw = max(0.0, _finite(screen_width))
    fraction = FRACTIONS.get(size, FRACTIONS["one-sixth"])
    return max(1, round(min(max(sw * fraction, MIN_WIDTH), sw / 2)))


def panel_size(prefs: PanelPrefs, screen_width, screen_height) -> tuple[int, int]:
    """The window size for the chosen size on a screen of this size."""
    sh = max(0.0, _finite(screen_height))
    max_h = max(1, round(sh - 2 * MARGIN))
    if prefs.size == "custom" and prefs.custom_width and prefs.custom_height:
        return prefs.custom_width, min(max(prefs.custom_height, MIN_HEIGHT), max_h)
    width = panel_width(screen_width, prefs.size)
    if prefs.size == "compact":
        return width, min(COMPACT_HEIGHT, max_h)
    return width, max_h


def after_resize(
    prefs: PanelPrefs, width: int, height: int, screen_width, screen_height
) -> PanelPrefs:
    """What to remember once the window has this size: the same preset, or "custom".

    The Shell may make a tall panel a little shorter than the screen (the top
    bar), so for the tall sizes only the width counts.
    """
    if width <= 0 or height <= 0:
        return prefs
    if prefs.size != "custom":
        want_w, want_h = panel_size(prefs, screen_width, screen_height)
        same_h = prefs.size != "compact" or abs(height - want_h) <= SIZE_SLACK
        if abs(width - want_w) <= SIZE_SLACK and same_h:
            return prefs
    return replace(prefs, size="custom", custom_width=width, custom_height=height)


def background_opacity(prefs: PanelPrefs, high_contrast: bool) -> float:
    """Translucency is off when the person turns it off, and in high contrast."""
    if high_contrast or not prefs.translucent:
        return 1.0
    return min(1.0, max(MIN_OPACITY, prefs.opacity))


# -- the owl's states (plan of the Gloxi window, "Architecture") ---------------------


class OwlState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    ANSWERING = "answering"
    WAITING = "waiting"
    ERROR = "error"


def state_text(state: OwlState) -> str:
    """For screen readers and the tooltip: the states are never only a picture."""
    return {
        OwlState.IDLE: _("Gloxi is ready"),
        OwlState.LISTENING: _("Gloxi is listening"),
        OwlState.THINKING: _("Gloxi is thinking"),
        OwlState.ANSWERING: _("Gloxi is answering"),
        OwlState.WAITING: _("Gloxi is waiting for you"),
        OwlState.ERROR: _("Something went wrong"),
    }[state]


class OwlStates:
    """One state from what the chat reports. Waiting for the person comes
    first, then an error until the next message, then the conversation."""

    def __init__(self) -> None:
        self.busy = False
        self.answering = False
        self.waiting = 0
        self.error = False
        self.focused = False
        self.mic = False  # the microphone is open (ADR 0015)
        self.speaking = False  # an answer is being read aloud

    @property
    def state(self) -> OwlState:
        if self.waiting:
            return OwlState.WAITING
        if self.error:
            return OwlState.ERROR
        if self.mic:
            return OwlState.LISTENING
        if self.busy:
            return OwlState.ANSWERING if self.answering else OwlState.THINKING
        if self.speaking:
            return OwlState.ANSWERING
        return OwlState.LISTENING if self.focused else OwlState.IDLE

    def set_busy(self, busy: bool) -> None:
        if busy:
            self.error = False
        self.busy = busy
        self.answering = False

    def got_piece(self) -> None:
        if self.busy:
            self.answering = True

    def failed(self) -> None:
        self.error = True

    def ask(self) -> None:
        self.waiting += 1

    def answered(self) -> None:
        self.waiting = max(0, self.waiting - 1)

    def focus(self, focused: bool) -> None:
        self.focused = focused

    def set_mic(self, on: bool) -> None:
        self.mic = on
        if on:
            self.error = False

    def set_speaking(self, on: bool) -> None:
        self.speaking = on
