# SPDX-License-Identifier: GPL-3.0-or-later
"""The key of the local model server that Gloxi starts (ADR 0016, point 1).

llama-server listens on 127.0.0.1, and the Flatpak shares the network, so
without a key any program on the computer, and any web page that reaches
localhost, could use the model past the router and the privacy gateway.
Gloxi makes a new random key each time it runs and keeps it only in memory;
the server reads it from a private file that is deleted once it has started.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

KEY = secrets.token_urlsafe(32)

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def is_local_url(url: str) -> bool:
    return (urlsplit(url).hostname or "127.0.0.1") in LOOPBACK


def headers_for(url: str, api_key: str | None = None) -> dict[str, str]:
    """Authorization for an endpoint: its own key, or ours if it is local.

    A local server that the person started themselves without a key ignores
    the header, so sending it is always safe.
    """
    key = api_key or (KEY if is_local_url(url) else None)
    return {"Authorization": f"Bearer {key}"} if key else {}


@contextmanager
def key_file() -> Iterator[Path]:
    """A file only this user can read, holding the key; deleted afterwards."""
    folder = os.environ.get("XDG_RUNTIME_DIR") or None
    if folder and not os.path.isdir(folder):
        folder = None
    fd, name = tempfile.mkstemp(prefix="gloxi-key-", dir=folder)  # mode 0600
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(KEY + "\n")
        yield path
    finally:
        path.unlink(missing_ok=True)
