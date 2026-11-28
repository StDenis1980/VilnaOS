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

import codecs
import contextlib
import filecmp
import json
import os
import pwd
import secrets
import shutil
import stat
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
        try:
            os.fsencode(path)  # a lone surrogate is no name on any disk
        except UnicodeEncodeError:
            raise FilesError("path has characters no file name can have") from None
        if not os.path.isabs(path):
            raise FilesError("path must be absolute")
        real = Path(os.path.realpath(path))
        if not self._inside_roots(real):
            raise FilesError(f"{path} is outside the allowed folders")
        # Snapshots are Mitos's: read-only, and not for Gloxi to look into.
        if SNAPSHOTS in real.parts:
            raise FilesError(f"{path} is inside a snapshot folder")
        # The undo copies are this server's own: a changed meta.json would
        # make restore write anywhere.
        backups = Path(os.path.realpath(self.backup_dir))
        if real == backups or real.is_relative_to(backups):
            raise FilesError(f"{path} is inside the undo copies of this tool")
        return real

    def _inside_roots(self, real: Path) -> bool:
        return any(real == r or real.is_relative_to(r) for r in self.roots)

    def _open_dir(self, folder: Path) -> int:
        """A descriptor for ``folder``, checked to be inside the roots *after* opening.

        Names are looked up again on every use; between the check in
        :meth:`resolve` and the use, another program could swap a folder
        for a link to elsewhere. Working through the descriptor of a folder
        that was checked once it was open closes that gap.
        """
        try:
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as e:
            raise FilesError(f"cannot open the folder {folder}: {e.strerror}") from None
        if not self._inside_roots(_fd_path(fd)):
            os.close(fd)
            raise FilesError(f"{folder} moved outside the allowed folders")
        return fd

    def _open_file(self, f: Path) -> int:
        """A descriptor of the regular file ``f``, never a FIFO or a device, never via a link."""
        dir_fd = self._open_dir(f.parent)
        try:
            fd = os.open(
                f.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=dir_fd,
            )
        except OSError as e:
            raise FilesError(f"cannot open {f}: {e.strerror}") from None
        finally:
            os.close(dir_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise FilesError(f"{f} is not a regular file")
        return fd

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
        with os.fdopen(self._open_file(f), "rb") as handle:
            data = handle.read(MAX_READ_BYTES + 1)  # never the whole of a huge file
        truncated = len(data) > MAX_READ_BYTES
        try:
            # A cut can fall inside a letter: the decoder keeps such a tail back.
            text = codecs.getincrementaldecoder("utf-8")().decode(
                data[:MAX_READ_BYTES], final=not truncated
            )
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
        unreadable: list[OSError] = []
        for dirpath, dirnames, filenames in os.walk(
            folder, followlinks=False, onerror=unreadable.append
        ):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                p = Path(dirpath) / name
                hit = "name" if needle in name.casefold() else None
                if hit is None:
                    try:
                        st = p.lstat()
                        # Only regular files: a FIFO would block the server for ever.
                        if stat.S_ISREG(st.st_mode) and st.st_size <= MAX_SEARCH_FILE_BYTES:
                            with os.fdopen(self._open_file(p), "rb") as handle:
                                text = handle.read(MAX_SEARCH_FILE_BYTES).decode(
                                    "utf-8", errors="ignore"
                                )
                            if needle in text.casefold():
                                hit = "text"
                    except (OSError, FilesError):
                        continue
                elif p.is_symlink():
                    continue
                if hit:
                    matches.append({"path": str(p), "match": hit})
                    if len(matches) >= MAX_SEARCH_RESULTS:
                        return {"matches": matches, "truncated": True}
        out: dict = {"matches": matches, "truncated": False}
        if unreadable:
            # "Nothing found" must not hide "not allowed to look".
            out["unreadable_folders"] = len(unreadable)
        return out

    # -- changing ----------------------------------------------------------

    def create(self, path: str, text: str) -> dict:
        f = self.resolve(path)
        data = _encode(text)  # refused text leaves no undo copy behind
        if f.exists() or f.is_symlink():
            raise FilesError(f"{path} already exists")
        if not f.parent.is_dir():
            raise FilesError(f"folder {f.parent} does not exist")
        backup = self._backup(f)
        self._write(f, data)
        return {"path": str(f), "backup_id": backup}

    def write(self, path: str, text: str) -> dict:
        f = self.resolve(path)
        data = _encode(text)
        if not f.is_file():
            raise FilesError(f"{path} is not an existing file (use create)")
        if not os.access(f, os.W_OK):
            raise FilesError(f"{path} is read-only")
        backup = self._backup(f)
        self._write(f, data)
        return {"path": str(f), "backup_id": backup}

    def delete(self, path: str) -> dict:
        f = self.resolve(path)
        self._own(path, f)
        if not f.is_file():
            raise FilesError(f"{path} is not a file (folders are not deleted)")
        if not os.access(f, os.W_OK):
            raise FilesError(f"{path} is read-only")
        backup = self._backup(f)
        dir_fd = self._open_dir(f.parent)
        try:
            os.unlink(f.name, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
        return {"path": str(f), "backup_id": backup}

    @staticmethod
    def _own(path: str, real: Path) -> None:
        """A link is not changed through: the change would land on another file."""
        if Path(os.path.normpath(path)) != real and Path(path).is_symlink():
            raise FilesError(f"{path} is a link; change the file it points to by its own name")

    def restore(self, path: str, backup_id: str) -> dict:
        """Put ``path`` back the way it was before the change ``backup_id``.

        The current state is backed up first, so a restore can be undone too.
        """
        if not isinstance(backup_id, str) or not backup_id.isalnum():
            raise FilesError("bad backup_id")
        folder = self.backup_dir / backup_id
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            raise FilesError(
                f"no backup {backup_id} (backups are kept {self.keep_seconds / 86400:g} days)"
            ) from None
        if (
            not isinstance(meta, dict)
            or not isinstance(meta.get("path"), str)
            or not isinstance(meta.get("existed"), bool)
        ):
            raise FilesError(f"backup {backup_id} is damaged")
        target = self.resolve(meta["path"])
        if target != self.resolve(path):
            raise FilesError(f"backup {backup_id} belongs to another file")
        undo = self._backup(target)
        if meta["existed"]:
            if not target.parent.is_dir():
                raise FilesError(f"folder {target.parent} no longer exists")
            tmp = target.with_name(_temp_name())
            try:
                shutil.copy2(folder / "content", tmp)
                os.replace(tmp, target)
            except OSError as e:
                tmp.unlink(missing_ok=True)
                raise FilesError(f"cannot put {target} back: {e.strerror}") from None
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
        real_before = Path(os.path.realpath(before))
        for t in sorted(set(targets), key=lambda t: (len(t.parts), str(t))):
            rel = t.relative_to(self.home)
            src = before / rel
            # A link in the snapshot on the way to the file would copy whatever
            # it pointed to, from outside the home, into the home.
            if not Path(os.path.realpath(src.parent)).is_relative_to(real_before):
                skip(t, "links are not restored")
                continue
            if after is not None and not _same(t, after / rel):
                skip(t, "changed again after the action")
                continue
            todo.append((t, src))
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
                tmp = t.with_name(_temp_name())
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
            with os.fdopen(self._open_file(f), "rb") as src, open(folder / "content", "wb") as dst:
                shutil.copyfileobj(src, dst)
            shutil.copystat(f, folder / "content")
        meta = {"path": str(f), "existed": existed, "time": self.clock()}
        (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return backup_id

    def _write(self, f: Path, data: bytes) -> None:
        dir_fd = self._open_dir(f.parent)
        # A short name of our own: ".<name>.<random>" is too long for a 250-byte name.
        tmp = _temp_name()
        try:
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=dir_fd,
            )
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                try:
                    mode = os.stat(f.name, dir_fd=dir_fd, follow_symlinks=False).st_mode
                    os.chmod(tmp, stat.S_IMODE(mode), dir_fd=dir_fd)
                except FileNotFoundError:
                    pass  # a new file stays private (0600), like the person's other notes
                os.replace(tmp, f.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=dir_fd)
                raise
        except OSError as e:
            raise FilesError(f"cannot write {f}: {e.strerror}") from None
        finally:
            os.close(dir_fd)


def _encode(text: str) -> bytes:
    if not isinstance(text, str):
        raise FilesError("text must be a string")
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        raise FilesError("text has characters that are not Unicode text") from None
    if len(data) > MAX_WRITE_BYTES:
        raise FilesError("text is too large")
    return data


def _temp_name() -> str:
    return f".vilna-{secrets.token_hex(6)}.tmp"


def _fd_path(fd: int) -> Path:
    """Where an open folder really is now, as the kernel sees it."""
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return Path(os.path.realpath(f"/dev/fd/{fd}"))


def _same(a: Path, b: Path) -> bool:
    """``a`` is as ``b``: both missing, both folders, equal links or equal files."""
    if a.is_symlink() or b.is_symlink():
        return a.is_symlink() and b.is_symlink() and os.readlink(a) == os.readlink(b)
    if not a.exists() or not b.exists():
        return not a.exists() and not b.exists()
    if a.is_dir() or b.is_dir():
        return a.is_dir() and b.is_dir()
    return filecmp.cmp(a, b, shallow=False)
