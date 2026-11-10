# SPDX-License-Identifier: Apache-2.0
"""File operations, limited to the roots given at start-up.

The server checks the roots itself, even though Herkos already has: a tool
must not rely on its caller (defence in depth). Every change keeps a backup
first, so it can be undone with ``restore``.

``restore_snapshot`` puts files back from a Mitos snapshot of the home
(ADR 0026): the system Herkos takes a btrfs snapshot before and after an
action the tool that did it cannot undo, and this tool, running as the
person, copies back what the action changed.
"""

from __future__ import annotations

import filecmp
import json
import os
import pwd
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
# Backups for undo are kept as long as OneDrive keeps version history and
# deleted files for personal accounts: 30 days (decision of 2 November 2026).
DEFAULT_KEEP_DAYS = 30
# Where snapper keeps the snapshots of a home that is a subvolume of its own.
SNAPSHOTS = ".snapshots"
MAX_RESTORE = 1000


class FilesError(Exception):
    """A request the server will not or cannot carry out."""


class Files:
    def __init__(
        self,
        roots: Iterable[Path],
        backup_dir: Path,
        *,
        keep_days: float = DEFAULT_KEEP_DAYS,
        clock=time.time,
        home: Path | None = None,
    ) -> None:
        self.roots = tuple(Path(os.path.realpath(r)) for r in roots)
        # The person's home, whose snapshots restore_snapshot reads.
        self.home = Path(os.path.realpath(home or pwd.getpwuid(os.getuid()).pw_dir))
        if not self.roots:
            raise FilesError("at least one root folder is required")
        self.backup_dir = Path(backup_dir)
        self.keep_seconds = keep_days * 86400
        self.clock = clock
        self.prune()

    def prune(self) -> int:
        """Delete backups older than the retention period. Returns how many."""
        if not self.backup_dir.is_dir():
            return 0
        removed, now = 0, self.clock()
        for folder in self.backup_dir.iterdir():
            meta = folder / "meta.json"
            try:
                created = json.loads(meta.read_text(encoding="utf-8"))["time"]
            except (OSError, ValueError, KeyError, TypeError):
                continue  # not ours or half-written: leave it alone
            if now - created > self.keep_seconds:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        return removed

    # -- paths -------------------------------------------------------------

    def resolve(self, path: str) -> Path:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise FilesError("path must be a non-empty string")
        if not os.path.isabs(path):
            raise FilesError("path must be absolute")
        real = Path(os.path.realpath(path))
        if not any(real == r or real.is_relative_to(r) for r in self.roots):
            raise FilesError(f"{path} is outside the allowed folders")
        # Snapshots are Mitos's: read-only, and not for Gloxi to look into.
        if SNAPSHOTS in real.parts:
            raise FilesError(f"{path} is inside a snapshot folder")
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
            raise FilesError(
                f"no backup {backup_id} (backups are kept {self.keep_seconds / 86400:g} days)"
            ) from None
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

    def restore_snapshot(self, pre: int, paths: list[str], post: int | None = None) -> dict:
        """Put ``paths`` back as they were in snapshot ``pre`` of the home.

        A path the action created goes away; one it changed or deleted comes
        back from the snapshot. With ``post`` (the snapshot right after the
        action), a path the person changed again since is left alone and
        reported in ``skipped``, so undo never loses their later work. No
        backup is kept here: the system Herkos takes a snapshot before this
        call, which makes it undoable in turn.
        """
        before = self._snapshot(pre)
        after = self._snapshot(post) if post is not None else None
        if not isinstance(paths, list) or not paths or len(paths) > MAX_RESTORE:
            raise FilesError(f"paths must be a list of 1 to {MAX_RESTORE} files")
        targets = []
        for p in paths:
            target = self.resolve(p)
            if not target.is_relative_to(self.home):
                raise FilesError(f"{p} is not in the home folder the snapshots are of")
            targets.append(target)
        restored: list[str] = []
        skipped: list[dict] = []

        def skip(t: Path, why: str) -> None:
            skipped.append({"path": str(t), "why": why})

        todo = []
        for t in sorted(set(targets), key=lambda t: (len(t.parts), str(t))):
            rel = t.relative_to(self.home)
            if after is not None and not _same(t, after / rel):
                skip(t, "changed again after the action")
                continue
            todo.append((t, before / rel))
        # Folders and files first, from the top down; removals after, from the bottom up.
        for t, src in todo:
            if src.is_symlink():
                skip(t, "links are not restored")
            elif src.is_dir():
                if t.is_dir() and not t.is_symlink():
                    continue
                if t.exists() or t.is_symlink():
                    skip(t, "something else is there now")
                    continue
                t.mkdir(parents=True)
                shutil.copystat(src, t)
                restored.append(str(t))
            elif src.is_file():
                if t.is_dir() and not t.is_symlink():
                    skip(t, "a folder is there now")
                    continue
                t.parent.mkdir(parents=True, exist_ok=True)
                tmp = t.with_name(f".{t.name}.restore-{secrets.token_hex(4)}")
                try:
                    shutil.copy2(src, tmp)
                    os.replace(tmp, t)
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
                restored.append(str(t))
        for t, src in reversed(todo):
            if src.exists() or src.is_symlink():
                continue
            if t.is_symlink() or t.is_file():
                t.unlink()
                restored.append(str(t))
            elif t.is_dir():
                try:
                    t.rmdir()
                    restored.append(str(t))
                except OSError:
                    skip(t, "the folder is not empty")
        return {"snapshot": pre, "restored": sorted(restored), "skipped": skipped}

    def _snapshot(self, number: int) -> Path:
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise FilesError("a snapshot is a positive number")
        folder = self.home / SNAPSHOTS / str(number) / "snapshot"
        if not folder.is_dir():
            raise FilesError(f"no snapshot {number} of the home (it may have been cleaned up)")
        return folder

    def _backup(self, f: Path) -> str:
        self.prune()
        backup_id = f"{int(self.clock())}{secrets.token_hex(6)}"
        folder = self.backup_dir / backup_id
        # Copies of the person's files: only the person may even list them,
        # whatever the umask and the mode of their home.
        self.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.backup_dir.chmod(0o700)
        folder.mkdir(mode=0o700)
        existed = f.is_file()
        if existed:
            shutil.copy2(f, folder / "content")
        meta = {"path": str(f), "existed": existed, "time": self.clock()}
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


def _same(a: Path, b: Path) -> bool:
    """``a`` is as ``b``: both missing, both folders, equal links or equal files."""
    if a.is_symlink() or b.is_symlink():
        return a.is_symlink() and b.is_symlink() and os.readlink(a) == os.readlink(b)
    if not a.exists() or not b.exists():
        return not a.exists() and not b.exists()
    if a.is_dir() or b.is_dir():
        return a.is_dir() and b.is_dir()
    return filecmp.cmp(a, b, shallow=False)
