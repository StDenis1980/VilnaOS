# SPDX-License-Identifier: Apache-2.0
"""File operations, limited to the roots given at start-up.

The server checks the roots itself, even though Herkos already has: a tool
must not rely on its caller (defence in depth). Every change keeps a backup
first, so it can be undone with ``restore``.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

MAX_READ_BYTES = 1024 * 1024
MAX_WRITE_BYTES = 10 * 1024 * 1024
MAX_LIST = 1000
MAX_SEARCH_RESULTS = 200
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024


class FilesError(Exception):
    """A request the server will not or cannot carry out."""


class Files:
    def __init__(self, roots: Iterable[Path], backup_dir: Path) -> None:
        self.roots = tuple(Path(os.path.realpath(r)) for r in roots)
        if not self.roots:
            raise FilesError("at least one root folder is required")
        self.backup_dir = Path(backup_dir)

    # -- paths -------------------------------------------------------------

    def resolve(self, path: str) -> Path:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise FilesError("path must be a non-empty string")
        if not os.path.isabs(path):
            raise FilesError("path must be absolute")
        real = Path(os.path.realpath(path))
        if not any(real == r or real.is_relative_to(r) for r in self.roots):
            raise FilesError(f"{path} is outside the allowed folders")
        return real

    # -- reading -----------------------------------------------------------

    def list(self, path: str) -> dict:
        folder = self.resolve(path)
        if not folder.is_dir():
            raise FilesError(f"{path} is not a folder")
        entries = []
        for i, entry in enumerate(sorted(folder.iterdir(), key=lambda p: p.name)):
            if i >= MAX_LIST:
                break
            try:
                st = entry.lstat()
            except OSError:
                continue
            kind = "folder" if entry.is_dir() and not entry.is_symlink() else "file"
            entries.append({"name": entry.name, "type": kind, "size": st.st_size})
        return {"path": str(folder), "entries": entries, "truncated": len(entries) >= MAX_LIST}

    def read(self, path: str) -> dict:
        f = self.resolve(path)
        if not f.is_file():
            raise FilesError(f"{path} is not a file")
        data = f.read_bytes()[: MAX_READ_BYTES + 1]
        truncated = len(data) > MAX_READ_BYTES
        try:
            text = data[:MAX_READ_BYTES].decode("utf-8")
        except UnicodeDecodeError:
            raise FilesError(f"{path} is not a UTF-8 text file") from None
        return {"path": str(f), "text": text, "truncated": truncated}

    def search(self, path: str, query: str) -> dict:
        folder = self.resolve(path)
        if not folder.is_dir():
            raise FilesError(f"{path} is not a folder")
        if not isinstance(query, str) or not query.strip():
            raise FilesError("query must be a non-empty string")
        needle = query.casefold()
        matches = []
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                p = Path(dirpath) / name
                if p.is_symlink():
                    continue
                hit = "name" if needle in name.casefold() else None
                if hit is None:
                    try:
                        if p.stat().st_size <= MAX_SEARCH_FILE_BYTES:
                            text = p.read_text(encoding="utf-8", errors="ignore")
                            if needle in text.casefold():
                                hit = "text"
                    except OSError:
                        continue
                if hit:
                    matches.append({"path": str(p), "match": hit})
                    if len(matches) >= MAX_SEARCH_RESULTS:
                        return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    # -- changing ----------------------------------------------------------

    def create(self, path: str, text: str) -> dict:
        f = self.resolve(path)
        if f.exists() or f.is_symlink():
            raise FilesError(f"{path} already exists")
        if not f.parent.is_dir():
            raise FilesError(f"folder {f.parent} does not exist")
        backup = self._backup(f)
        self._write(f, text)
        return {"path": str(f), "backup_id": backup}

    def write(self, path: str, text: str) -> dict:
        f = self.resolve(path)
        if not f.is_file():
            raise FilesError(f"{path} is not an existing file (use create)")
        backup = self._backup(f)
        self._write(f, text)
        return {"path": str(f), "backup_id": backup}

    def delete(self, path: str) -> dict:
        f = self.resolve(path)
        if not f.is_file():
            raise FilesError(f"{path} is not a file (folders are not deleted)")
        backup = self._backup(f)
        f.unlink()
        return {"path": str(f), "backup_id": backup}

    def restore(self, path: str, backup_id: str) -> dict:
        """Put ``path`` back the way it was before the change ``backup_id``.

        The current state is backed up first, so a restore can be undone too.
        """
        if not isinstance(backup_id, str) or not backup_id.isalnum():
            raise FilesError("bad backup_id")
        folder = self.backup_dir / backup_id
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise FilesError(f"no backup {backup_id}") from None
        target = self.resolve(meta["path"])
        if target != self.resolve(path):
            raise FilesError(f"backup {backup_id} belongs to another file")
        undo = self._backup(target)
        if meta["existed"]:
            if not target.parent.is_dir():
                raise FilesError(f"folder {target.parent} no longer exists")
            tmp = target.with_name(f".{target.name}.restore-{secrets.token_hex(4)}")
            shutil.copy2(folder / "content", tmp)
            os.replace(tmp, target)
        elif target.exists():
            target.unlink()
        return {"path": str(target), "restored": backup_id, "backup_id": undo}

    def _backup(self, f: Path) -> str:
        backup_id = f"{int(time.time())}{secrets.token_hex(6)}"
        folder = self.backup_dir / backup_id
        folder.mkdir(parents=True, mode=0o700)
        existed = f.is_file()
        if existed:
            shutil.copy2(f, folder / "content")
        meta = {"path": str(f), "existed": existed, "time": time.time()}
        (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return backup_id

    @staticmethod
    def _write(f: Path, text: str) -> None:
        if not isinstance(text, str):
            raise FilesError("text must be a string")
        data = text.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise FilesError("text is too large")
        fd, tmp = tempfile.mkstemp(dir=f.parent, prefix=f".{f.name}.")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            if f.exists():
                shutil.copymode(f, tmp)
            os.replace(tmp, f)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
