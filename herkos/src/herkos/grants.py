# SPDX-License-Identifier: GPL-3.0-or-later
"""Permissions the user gave (permissions.md 5.3).

A permission covers one class of actions (tool + operation) within the
allowed folders it was given for, for a duration: once, this session, or
permanently (R1-R2 only). Permanent permissions are stored in
``permissions.json`` and can be listed and revoked from the command line;
Gloxi has no protocol method to create or revoke them.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from herkos.config import SESSION_IDLE_SECONDS
from herkos.risk import Level

Duration = Literal["session", "permanent"]
PERMANENT_MAX_LEVEL = Level.R2


class GrantStoreError(Exception):
    """The permissions file is unreadable; Herkos treats it as empty and says so."""


@dataclass(frozen=True)
class Grant:
    id: str
    tool: str
    operation: str
    roots: tuple[str, ...]  # allowed folders the permission is limited to
    level: str  # level the user saw when giving it, e.g. "R2"
    duration: Duration
    created: float

    def covers(self, tool: str, operation: str, roots: Iterable[Path | None], level: Level) -> bool:
        if (tool, operation) != (self.tool, self.operation):
            return False
        if level > Level.parse(self.level):
            return False  # the action got riskier than what the user allowed
        allowed = set(self.roots)
        return all(r is not None and str(r) in allowed for r in roots)


class GrantStore:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self._session: dict[str, tuple[Grant, float]] = {}  # id -> (grant, last use)
        self.load_error: str | None = None
        self._stamp: tuple[int, int] | None = None
        self._permanent: dict[str, Grant] = {}
        self._refresh()

    # -- persistence -------------------------------------------------------

    def _refresh(self) -> None:
        """Re-read the file if it changed, e.g. after ``herkos permissions revoke``."""
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            stamp = None
        if stamp != self._stamp or stamp is None:
            self.load_error = None
            self._permanent = self._load()
            self._stamp = stamp

    def _load(self) -> dict[str, Grant]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            grants = {}
            for item in raw.get("permissions", []):
                item["roots"] = tuple(item["roots"])
                g = Grant(**item)
                if g.duration == "permanent" and Level.parse(g.level) <= PERMANENT_MAX_LEVEL:
                    grants[g.id] = g
            return grants
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
            # Fail closed: an unreadable file means no permissions, not all.
            self.load_error = f"{self.path}: {e}"
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {
            "version": 1,
            "permissions": [asdict(g) for g in self._permanent.values()],
        }
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".permissions-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        st = self.path.stat()
        self._stamp = (st.st_mtime_ns, st.st_size)

    # -- queries -----------------------------------------------------------

    def find(
        self, tool: str, operation: str, roots: Iterable[Path | None], level: Level
    ) -> Grant | None:
        roots = tuple(roots)
        now = self.clock()
        self._refresh()
        for gid, (g, last) in list(self._session.items()):
            if now - last > SESSION_IDLE_SECONDS:
                del self._session[gid]
            elif g.covers(tool, operation, roots, level):
                self._session[gid] = (g, now)
                return g
        for g in self._permanent.values():
            if g.covers(tool, operation, roots, level):
                return g
        return None

    def all(self) -> list[Grant]:
        self._refresh()
        return [g for g, _ in self._session.values()] + list(self._permanent.values())

    # -- changes -----------------------------------------------------------

    def add(
        self,
        tool: str,
        operation: str,
        roots: Iterable[Path],
        level: Level,
        duration: Duration,
    ) -> Grant:
        if duration == "permanent" and level > PERMANENT_MAX_LEVEL:
            raise ValueError(f"a permanent permission is not possible for {level.name}")
        g = Grant(
            id=secrets.token_hex(6),
            tool=tool,
            operation=operation,
            roots=tuple(sorted({str(r) for r in roots})),
            level=level.name,
            duration=duration,
            created=self.clock(),
        )
        if duration == "session":
            self._session[g.id] = (g, self.clock())
        else:
            self._refresh()
            self._permanent[g.id] = g
            self._save()
        return g

    def revoke(self, grant_id: str) -> bool:
        if self._session.pop(grant_id, None):
            return True
        self._refresh()
        if self._permanent.pop(grant_id, None):
            self._save()
            return True
        return False
