# SPDX-License-Identifier: GPL-3.0-or-later
"""Only a real press allows in a Herkos window (docs/adr/0029); no GTK needed.

The same gate guards the installer's Herkos screen; its tests there cover
the focus rules in detail.
"""

from herkos import i18n
from herkos.dialog import NOT_REAL
from herkos.real_input import PRESS_SECONDS, RealInput


class Clock:
    now = 10.0

    def __call__(self) -> float:
        return self.now


def test_allow_needs_a_real_press_on_that_button():
    clock = Clock()
    gate = RealInput(clock)
    allow, deny = object(), object()
    assert not gate.allows(allow)  # an AT-SPI «click»
    gate.pointer_pressed(deny)
    assert not gate.allows(allow)
    gate.pointer_pressed(allow)
    assert gate.allows(allow)
    assert not gate.allows(allow)  # counted once
    gate.pointer_pressed(allow)
    clock.now += PRESS_SECONDS + 0.5
    assert not gate.allows(allow)


def test_enter_counts_only_where_the_person_put_the_focus():
    gate = RealInput(Clock())
    allow = object()
    gate.focus_entered(allow)  # moved by a program
    gate.key_pressed(allow, "Return")
    assert not gate.allows(allow)
    gate.focus_left(allow)
    gate.window_key("Tab")
    gate.focus_entered(allow)
    gate.key_pressed(allow, "Return")
    assert gate.allows(allow)


def test_the_window_says_what_to_do_in_ukrainian():
    assert NOT_REAL in i18n.load(["uk"])
