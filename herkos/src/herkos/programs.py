# SPDX-License-Identifier: GPL-3.0-or-later
"""Which catalogue programs the person turned on for Gloxi (ADR 0017, step 6).

A program from the catalogue (``/usr/share/herkos/manifests``) is off until
the person turns it on in Gloxi's settings, and Herkos asks them in its own
window first. An off program is not started, not listed in ``herkos.tools``
and its actions are refused. Built-in tools are always on.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

MAX_PROGRAMS_BYTES = 256 * 1024
MAX_PROGRAMS = 1024


class ProgramStore:
    """``programs.json`` in the state folder: ``{"enabled": [tool, ...]}``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._enabled: set[str] = set()
        self.load_error: str | None = None
        try:
            if path.stat().st_size > MAX_PROGRAMS_BYTES:
                raise ValueError("too big")
            data = json.loads(path.read_text(encoding="utf-8"))
            enabled = data.get("enabled") if isinstance(data, dict) else None
            if not isinstance(enabled, list):
                raise ValueError("'enabled' must be a list")
            self._enabled = {t for t in enabled[:MAX_PROGRAMS] if isinstance(t, str)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            # A broken file turns nothing on.
            self.load_error = f"{path}: {e}"
            self._enabled = set()

    def enabled(self, tool: str) -> bool:
        with self._lock:
            return tool in self._enabled

    def set(self, tool: str, on: bool) -> None:
        with self._lock:
            if on:
                if len(self._enabled) >= MAX_PROGRAMS:
                    raise ValueError("too many programs turned on")
                self._enabled.add(tool)
            else:
                self._enabled.discard(tool)
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".programs.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"enabled": sorted(self._enabled)}, f, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
