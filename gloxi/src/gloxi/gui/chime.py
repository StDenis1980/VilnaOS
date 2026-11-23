# SPDX-License-Identifier: GPL-3.0-or-later
"""Gloxi's short signals from the desktop sound theme.

Vilna's sound themes (sounds/, package vilna-sound-themes) have three
Gloxi events: x-vilna-gloxi-listen when the microphone opens,
x-vilna-gloxi-done when an answer is ready while the person looks at
something else, and x-vilna-gloxi-error when a question fails. They are
played through libcanberra's canberra-gtk-play, which follows the chosen
theme and its switched-off sounds. Where that program is missing (the
Flatpak outside Vilna) or the person turned event sounds off, nothing
plays. A signal never blocks or fails the window.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from collections.abc import Callable

LISTEN = "x-vilna-gloxi-listen"
DONE = "x-vilna-gloxi-done"
ERROR = "x-vilna-gloxi-error"
EVENTS = (LISTEN, DONE, ERROR)


def event_sounds_on() -> bool:
    """GNOME's switch for all event sounds (org.gnome.desktop.sound)."""
    try:
        from gi.repository import Gio

        source = Gio.SettingsSchemaSource.get_default()
        if source is None or source.lookup("org.gnome.desktop.sound", True) is None:
            return True
        return Gio.Settings.new("org.gnome.desktop.sound").get_boolean("event-sounds")
    except (ImportError, ValueError):
        return True


def canberra_player(event: str) -> None:
    program = shutil.which("canberra-gtk-play")
    if program is None:
        return
    with contextlib.suppress(OSError):
        subprocess.Popen(
            [program, "-i", event, "-d", "Gloxi"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


class Chime:
    def __init__(
        self,
        player: Callable[[str], None] = canberra_player,
        enabled: Callable[[], bool] = event_sounds_on,
    ) -> None:
        self.player = player
        self.enabled = enabled

    def play(self, event: str) -> None:
        if event not in EVENTS:
            return
        try:
            if self.enabled():
                self.player(event)
        except Exception:  # noqa: BLE001 - a signal must never break the window
            pass

    def listening(self) -> None:
        self.play(LISTEN)

    def answered(self, window_active: bool) -> None:
        # Only when the person is elsewhere: a sound for every answer they
        # watch arrive would be noise.
        if not window_active:
            self.play(DONE)

    def failed(self) -> None:
        self.play(ERROR)
