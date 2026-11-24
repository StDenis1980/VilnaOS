# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from herkos.dialog import CONSENT_SOUND, play_consent_sound


def test_plays_the_consent_sound_without_touching_stdout():
    calls = []

    def popen(args, **kw):
        calls.append((args, kw))

    assert play_consent_sound(lambda _p: "/usr/bin/canberra-gtk-play", popen, lambda: True)
    args, kw = calls[0]
    assert args[:3] == ["/usr/bin/canberra-gtk-play", "-i", CONSENT_SOUND]
    assert kw["stdout"] is not None  # DEVNULL: the answer goes to our stdout


def test_silent_without_program_or_when_sounds_are_off():
    calls = []
    assert not play_consent_sound(lambda _p: None, calls.append, lambda: True)
    assert not play_consent_sound(lambda _p: "/x", calls.append, lambda: False)
    assert calls == []


def test_a_failing_player_does_not_stop_the_question():
    def popen(*_a, **_k):
        raise OSError("no such program")

    def broken():
        raise RuntimeError("no dconf")

    assert not play_consent_sound(lambda _p: "/x", popen, lambda: True)
    play_consent_sound(lambda _p: "/x", lambda *_a, **_k: None, broken)


def test_the_event_is_in_the_vilna_sound_themes():
    maker = Path(__file__).resolve().parents[2] / "sounds" / "make_sounds.py"
    if not maker.exists():
        pytest.skip("not in the repository (the package is built alone)")
    assert f'"{CONSENT_SOUND}"' in maker.read_text(encoding="utf-8")
