# SPDX-License-Identifier: GPL-3.0-or-later
"""The message field of the Gloxi window: Enter sends, Shift+Enter a new line."""

import pytest

pytest.importorskip("gi")
gi_input = pytest.importorskip("gloxi.gui.input")
Gdk = gi_input.Gdk


def test_enter_sends_shift_enter_does_not():
    assert gi_input.sends(Gdk.KEY_Return, 0)
    assert gi_input.sends(Gdk.KEY_KP_Enter, 0)
    assert not gi_input.sends(Gdk.KEY_Return, Gdk.ModifierType.SHIFT_MASK)
    assert not gi_input.sends(Gdk.KEY_a, 0)
