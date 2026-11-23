# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from gloxi.gui.chime import DONE, ERROR, EVENTS, LISTEN, Chime


def make(enabled=True):
    played = []
    return Chime(player=played.append, enabled=lambda: enabled), played


def test_signals():
    chime, played = make()
    chime.listening()
    chime.failed()
    assert played == [LISTEN, ERROR]


def test_done_only_when_the_person_looks_elsewhere():
    chime, played = make()
    chime.answered(window_active=True)
    assert played == []
    chime.answered(window_active=False)
    assert played == [DONE]


def test_event_sounds_off_plays_nothing():
    chime, played = make(enabled=False)
    chime.listening()
    chime.answered(window_active=False)
    assert played == []


def test_unknown_event_and_broken_player_do_nothing():
    chime, played = make()
    chime.play("../../etc/passwd")
    assert played == []

    def broken(_event):
        raise RuntimeError("no sound server")

    Chime(player=broken, enabled=lambda: True).failed()  # does not raise


def test_events_exist_in_the_vilna_sound_themes():
    maker = Path(__file__).resolve().parents[2] / "sounds" / "make_sounds.py"
    if not maker.exists():
        pytest.skip("not in the repository (the package is built alone)")
    text = maker.read_text(encoding="utf-8")
    for event in EVENTS:
        assert f'"{event}"' in text, event
