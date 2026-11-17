# SPDX-License-Identifier: GPL-3.0-or-later
"""Only a real press counts on the Herkos screen (docs/adr/0029); no GTK needed."""

from pathlib import Path

import pytest

from vilna_installer import real_input, speech
from vilna_installer.real_input import FOCUS_SECONDS, PRESS_SECONDS, RealInput


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


BUTTON = object()


def gate():
    clock = Clock()
    return RealInput(clock), clock


def test_a_click_with_no_real_input_is_not_a_press():
    g, _ = gate()
    assert not g.allows(BUTTON)  # an AT-SPI «click»: clicked, no event


def test_a_pointer_press_counts_once_and_only_shortly():
    g, clock = gate()
    g.pointer_pressed(BUTTON)
    assert g.allows(BUTTON)
    assert not g.allows(BUTTON)  # a second «click» from a program right after
    g.pointer_pressed(BUTTON)
    clock.now += PRESS_SECONDS + 0.1
    assert not g.allows(BUTTON)


def test_a_press_on_another_button_does_not_count():
    g, _ = gate()
    g.pointer_pressed(object())
    assert not g.allows(BUTTON)


def test_enter_counts_when_the_person_moved_the_focus_there():
    g, clock = gate()
    g.window_key("Tab")
    clock.now += 0.05
    g.focus_entered(BUTTON)
    g.key_pressed(BUTTON, "Return")
    assert g.allows(BUTTON)
    g.key_pressed(BUTTON, "space")
    assert g.allows(BUTTON)
    g.key_pressed(BUTTON, "a")  # not a key that presses a button
    assert not g.allows(BUTTON)


def test_enter_does_not_count_when_a_program_moved_the_focus():
    """A program grabs the focus over AT-SPI and waits for the person's next Enter."""
    g, clock = gate()
    g.window_key("o")  # the person is typing the key group
    g.focus_entered(BUTTON)  # no Tab, no pointer: grab_focus from outside
    g.key_pressed(BUTTON, "Return")
    assert not g.allows(BUTTON)
    # A Tab long ago does not make it the person's either.
    g.focus_left(BUTTON)
    g.window_key("Tab")
    clock.now += FOCUS_SECONDS + 0.1
    g.focus_entered(BUTTON)
    g.key_pressed(BUTTON, "Return")
    assert not g.allows(BUTTON)
    # A real pointer press on the button makes the focus the person's.
    g.pointer_pressed(BUTTON)
    assert g.allows(BUTTON)
    g.key_pressed(BUTTON, "Return")
    assert g.allows(BUTTON)


def test_a_pointer_press_anywhere_lets_the_focus_follow_it():
    g, _ = gate()
    g.window_pointer()
    g.focus_entered(BUTTON)
    g.key_pressed(BUTTON, "KP_Enter")
    assert g.allows(BUTTON)


def test_the_installer_has_the_same_gate_as_herkos():
    """One file, two packages: real_input.py is copied, never changed in one place only."""
    herkos = Path(__file__).parents[2] / "herkos" / "src" / "herkos" / "real_input.py"
    if not herkos.is_file():  # the RPM build has only the installer's sources
        pytest.skip("not in the repository")
    assert Path(real_input.__file__).read_bytes() == herkos.read_bytes()


class FakeClient:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.language: str | None = None
        self.closed = False

    def set_language(self, code: str) -> None:
        self.language = code

    def set_rate(self, _rate: int) -> None:
        pass

    def speak(self, text: str) -> None:
        self.said.append(text)

    def close(self) -> None:
        self.closed = True


def test_the_voice_says_the_text_in_the_screens_language_and_closes():
    client = FakeClient()
    voice = speech.Voice(lambda: client)
    assert voice.available()
    assert voice.say("a b c", "uk")
    assert client.said == ["a b c"] and client.language == "uk"
    voice.close()
    assert client.closed


def test_no_speech_dispatcher_is_not_an_error():
    def broken():
        raise OSError("no speech-dispatcher socket")

    assert not speech.Voice(broken).say("a b c")
