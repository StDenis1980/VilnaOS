# SPDX-License-Identifier: GPL-3.0-or-later
"""What a tool said about each operation when it was first trusted (ADR 0017, step 3).

A tool's ``tools/list`` entry for an operation (its name, description, input
schema and annotations) is reduced to a fingerprint. The first time Herkos
runs a tool from the catalogue it keeps the fingerprints of all its
operations. After an update of the program a new or changed operation is
treated like one without a manifest (permissions.md 4.1.3) until the person
allows it once in the Herkos window; then its new fingerprint is kept.
Built-in tools ship with Herkos and are not pinned.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MAX_PINS_BYTES = 4 * 1024 * 1024


def fingerprint(entry: Mapping[str, Any]) -> str:
    """sha256 of the operation's ``tools/list`` entry, as canonical JSON."""
    keep = {
        k: entry[k] for k in ("name", "description", "inputSchema", "annotations") if k in entry
    }
    text = json.dumps(keep, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class PinStore:
    """``tool-pins.json`` in the state folder: ``{tool: {operation: fingerprint}}``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._pins: dict[str, dict[str, str]] = {}
        self.load_error: str | None = None
        try:
            if path.stat().st_size > MAX_PINS_BYTES:
                raise ValueError("too big")
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            for tool, ops in data.items():
                if isinstance(tool, str) and isinstance(ops, dict):
                    self._pins[tool] = {
                        o: f for o, f in ops.items() if isinstance(o, str) and isinstance(f, str)
                    }
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            # A broken file trusts nothing: every operation counts as changed.
            self.load_error = f"{path}: {e}"
            self._pins = {}
            self._broken = True
            return
        self._broken = False

    def changed(self, tool: str, operation: str, current: Mapping[str, str]) -> bool:
        """Whether this operation differs from what was trusted.

        A tool seen for the first time is trusted as it is now (all of
        ``current`` is kept), unless the file was unreadable.
        """
        with self._lock:
            pinned = self._pins.get(tool)
            if pinned is None:
                if self._broken:
                    return True
                self._pins[tool] = dict(current)
                self._save()
                return False
            fp = current.get(operation)
            return fp is None or pinned.get(operation) != fp

    def pin(self, tool: str, operation: str, fp: str) -> None:
        with self._lock:
            self._pins.setdefault(tool, {})[operation] = fp
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tool-pins.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._pins, f, sort_keys=True, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
