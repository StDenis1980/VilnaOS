# SPDX-License-Identifier: GPL-3.0-or-later
"""Saying a secret aloud without putting it on the accessibility bus.

docs/adr/0029-herkos-real-input.md. The Herkos screen does not show the
recovery key to accessibility software: everything there can be read by
every program of the same user. For a person with a screen reader the
screen says the key itself, through speech-dispatcher (the voice Orca uses
too), and only when they press «Read the key aloud» themselves.

Speech-dispatcher has no way for one client to read what another one says;
what can still reach others is the sound itself, as with any voice.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

CLIENT = "vilna-installer-confirm"


def _speechd_client() -> Any:
    import speechd  # python3-speechd, with Orca

    client = speechd.SSIPClient(CLIENT)
    client.set_priority(speechd.Priority.IMPORTANT)
    return client


class Voice:
    """One connection to speech-dispatcher, opened when first needed."""

    def __init__(self, connect: Callable[[], Any] = _speechd_client) -> None:
        self._connect = connect
        self._client: Any = None

    def available(self) -> bool:
        try:
            import speechd  # noqa: F401
        except ImportError:
            return self._connect is not _speechd_client
        return True

    def say(self, text: str, language: str | None = None) -> bool:
        """Say ``text`` slowly; False when there is no speech-dispatcher."""
        try:
            if self._client is None:
                self._client = self._connect()
            if language:
                self._client.set_language(language)
            self._client.set_rate(-40)
            self._client.speak(text)
            return True
        except Exception:  # noqa: BLE001 — no voice is not a reason to stop the screen
            self.close()
            return False

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
