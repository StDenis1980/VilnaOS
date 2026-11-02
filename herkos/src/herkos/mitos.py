# SPDX-License-Identifier: GPL-3.0-or-later
"""Mitos-lite: the append-only action journal (permissions.md 7.1).

One JSON object per line. Every record carries the SHA-256 of the previous
record, so changing or deleting a record breaks the chain and ``verify``
finds it. The journal holds descriptions and hashes, never file or message
contents. Undo (7.2) is not part of this module yet.

Record fields: ``seq``, ``time`` (UTC, ISO 8601), ``kind``, ``action_id``,
``data``, ``prev`` (hash of the previous record, or 64 zeros), ``hash``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
KINDS = {"start", "request", "decision", "result", "grant"}


class JournalError(Exception):
    """The journal cannot be written or is broken. Herkos then refuses all actions."""


def canonical(record: Mapping[str, Any]) -> bytes:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def record_hash(record: Mapping[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(canonical(body)).hexdigest()


def digest(value: Any) -> str:
    """Hash of a value for the journal, in place of the value itself."""
    return "sha256:" + hashlib.sha256(canonical({"v": value})).hexdigest()


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    records: int
    problem: str | None = None
    line: int | None = None


def read_records(path: Path) -> Iterator[tuple[int, dict[str, Any] | None]]:
    with path.open("rb") as f:
        for n, raw in enumerate(f, start=1):
            try:
                yield n, json.loads(raw)
            except ValueError:
                yield n, None


def verify(path: Path) -> VerifyResult:
    if not path.exists():
        return VerifyResult(ok=True, records=0)
    prev, count = GENESIS, 0
    for n, rec in read_records(path):
        if not isinstance(rec, dict):
            return VerifyResult(False, count, "line is not a JSON object", n)
        if rec.get("prev") != prev:
            return VerifyResult(False, count, "chain broken: a record was changed or removed", n)
        if rec.get("seq") != count:
            return VerifyResult(False, count, "sequence number out of order", n)
        if rec.get("hash") != record_hash(rec):
            return VerifyResult(False, count, "record does not match its hash", n)
        prev, count = rec["hash"], count + 1
    return VerifyResult(ok=True, records=count)


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        result = verify(path)
        if not result.ok:
            raise JournalError(f"{path}, line {result.line}: {result.problem}")
        self._seq = result.records
        self._prev = GENESIS
        if result.records:
            *_, (_, last) = read_records(path)
            assert last is not None
            self._prev = last["hash"]

    def append(self, kind: str, data: Mapping[str, Any], action_id: str | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"unknown journal record kind: {kind}")
        with self._lock:
            record = {
                "seq": self._seq,
                "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "kind": kind,
                "action_id": action_id,
                "data": dict(data),
                "prev": self._prev,
            }
            record["hash"] = record_hash(record)
            line = canonical(record) + b"\n"
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    if os.fstat(fd).st_size and not self._tail_matches(fd):
                        raise JournalError("the journal was changed by someone else")
                    os.write(fd, line)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as e:
                raise JournalError(f"cannot write {self.path}: {e}") from None
            self._seq += 1
            self._prev = record["hash"]
            return record

    def _tail_matches(self, fd: int) -> bool:
        """The last line on disk is still the last record we wrote."""
        size = os.fstat(fd).st_size
        chunk = min(size, 64 * 1024)
        tail = os.pread(fd, chunk, size - chunk).rstrip(b"\n").rsplit(b"\n", 1)[-1]
        try:
            return json.loads(tail).get("hash") == self._prev
        except ValueError:
            return False
