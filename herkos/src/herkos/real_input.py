# SPDX-License-Identifier: GPL-3.0-or-later
"""Buttons of Herkos windows that only a person at the computer can press.

docs/adr/0029-herkos-real-input.md. GTK lets accessibility software press
any button: an AT-SPI "click" action emits ``clicked`` without any key or
pointer event. Orca needs the accessibility bus, so it stays on, but then
every program of the same user can press «Allow» or «Install» too. So the
buttons that allow something irreversible count a press only when the
window got a real one, from the keyboard or the pointer, on that very
button, just before ``clicked``:

- a pointer press on the button;
- Enter or Space while the button has the focus, and only when the focus
  got there by a real Tab, arrow or pointer press: a program can move the
  focus over AT-SPI too and wait for the next Enter.

On GNOME (Wayland) a program cannot make real input events for another
window: that needs the remote desktop portal, which asks the person, or
``/dev/uinput``, which only root may open. Orca users are not hindered:
Orca passes their Tab, Enter and Space on to the window as real keys.

This file is copied as is into the installer
(``installer/src/vilna_installer/real_input.py``); a test there checks that
both are the same.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

# A real press counts for this long: GTK emits ``clicked`` at once for the
# pointer and within 250 ms of Enter or Space.
PRESS_SECONDS = 1.0
# The focus is the person's when it moved this soon after their Tab, arrow
# or pointer press.
FOCUS_SECONDS = 0.5

# Keys that press a focused button, and keys that move the focus (GDK names).
PRESS_KEYS = frozenset({"Return", "KP_Enter", "ISO_Enter", "space", "KP_Space"})
FOCUS_KEYS = frozenset(
    {
        "Tab",
        "ISO_Left_Tab",
        "KP_Tab",
        "Left",
        "Right",
        "Up",
        "Down",
        "KP_Left",
        "KP_Right",
        "KP_Up",
        "KP_Down",
        "Home",
        "End",
    }
)


class RealInput:
    """What the window saw from the real keyboard and pointer (no GTK here)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._moved_focus: float | None = None
        self._focus_ours: dict[int, bool] = {}
        self._pressed: dict[int, float] = {}

    # --- what the window tells -----------------------------------------------

    def window_key(self, keyname: str | None) -> None:
        """A real key press anywhere in the window."""
        if keyname in FOCUS_KEYS:
            self._moved_focus = self.clock()

    def window_pointer(self) -> None:
        """A real pointer press anywhere in the window."""
        self._moved_focus = self.clock()

    def focus_entered(self, target: Any) -> None:
        """``target`` got the focus: by the person, or by a program?"""
        moved = self._moved_focus
        ours = moved is not None and self.clock() - moved <= FOCUS_SECONDS
        self._focus_ours[id(target)] = ours

    def focus_left(self, target: Any) -> None:
        self._focus_ours.pop(id(target), None)

    def pointer_pressed(self, target: Any) -> None:
        """A real pointer press on ``target``."""
        self._focus_ours[id(target)] = True
        self._pressed[id(target)] = self.clock()

    def key_pressed(self, target: Any, keyname: str | None) -> None:
        """A real key press while ``target`` has the focus."""
        if keyname in PRESS_KEYS and self._focus_ours.get(id(target), False):
            self._pressed[id(target)] = self.clock()

    # --- what the button asks -----------------------------------------------

    def allows(self, target: Any) -> bool:
        """Was ``clicked`` on ``target`` a real press? Each press counts once."""
        at = self._pressed.pop(id(target), None)
        return at is not None and 0 <= self.clock() - at <= PRESS_SECONDS


def watch_window(window: Any, gate: RealInput) -> None:
    """Tell ``gate`` about real key and pointer presses anywhere in ``window``."""
    from gi.repository import Gdk, Gtk

    keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
    keys.connect(
        "key-pressed",
        lambda _c, keyval, _code, _state: gate.window_key(Gdk.keyval_name(keyval)) or False,
    )
    window.add_controller(keys)
    clicks = Gtk.GestureClick(button=0, propagation_phase=Gtk.PropagationPhase.CAPTURE)
    clicks.connect("pressed", lambda *_a: gate.window_pointer())
    window.add_controller(clicks)


def guard(button: Any, gate: RealInput) -> None:
    """Tell ``gate`` about real presses on ``button``; ``gate.allows(button)`` asks."""
    from gi.repository import Gdk, Gtk

    clicks = Gtk.GestureClick(button=0, propagation_phase=Gtk.PropagationPhase.CAPTURE)
    clicks.connect("pressed", lambda *_a: gate.pointer_pressed(button))
    button.add_controller(clicks)
    keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
    keys.connect(
        "key-pressed",
        lambda _c, keyval, _code, _state: (
            gate.key_pressed(button, Gdk.keyval_name(keyval)) or False
        ),
    )
    button.add_controller(keys)
    focus = Gtk.EventControllerFocus()
    focus.connect("enter", lambda *_a: gate.focus_entered(button))
    focus.connect("leave", lambda *_a: gate.focus_left(button))
    button.add_controller(focus)
