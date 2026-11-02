# SPDX-License-Identifier: GPL-3.0-or-later
"""The restic program, run with ``--json`` and its output read line by line.

Only this module starts restic. The repository password reaches restic through
the key store's environment (``RESTIC_PASSWORD_COMMAND`` with libsecret), and
new passwords go in on stdin, never on the command line.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# restic 0.17 brought the restore --dry-run and the summaries this module reads.
MIN_VERSION = (0, 17)
# restic backup: 3 means "snapshot made, but some files could not be read".
EXIT_PARTIAL = 3
EXIT_NO_REPO = 10
EXIT_LOCKED = 11
EXIT_WRONG_PASSWORD = 12


class ResticError(Exception):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Restic:
    repository: str
    env: Mapping[str, str]
    binary: str = "restic"
    cache_dir: Path | None = None

    def _env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        # Start from a small, known environment: nothing else of the user's
        # session leaks into restic, and no stray RESTIC_PASSWORD wins.
        base = {k: v for k, v in os.environ.items() if k in _PASS_THROUGH}
        base["RESTIC_REPOSITORY"] = self.repository
        base["RESTIC_PROGRESS_FPS"] = "1"
        if self.cache_dir is not None:
            base["RESTIC_CACHE_DIR"] = str(self.cache_dir)
        base.update(self.env)
        if extra:
            base.update(extra)
        return base

    def _cmd(self, args: Sequence[str]) -> list[str]:
        binary = shutil.which(self.binary) or self.binary
        return [binary, *args]

    def run(
        self,
        args: Sequence[str],
        *,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        ok_codes: Sequence[int] = (0,),
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            p = subprocess.run(
                self._cmd(args),
                input=stdin,
                capture_output=True,
                text=True,
                env=self._env(env),
                timeout=timeout,
            )
        except FileNotFoundError:
            raise ResticError("restic is not installed") from None
        except subprocess.TimeoutExpired:
            raise ResticError(f"restic {args[0]} took too long") from None
        if p.returncode not in ok_codes:
            raise ResticError(_explain(p.returncode, p.stderr), p.returncode)
        return p

    def stream(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        ok_codes: Sequence[int] = (0,),
    ) -> Iterator[dict[str, Any]]:
        """Run restic and yield its JSON messages as they come."""
        try:
            proc = subprocess.Popen(
                self._cmd(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                env=self._env(env),
            )
        except FileNotFoundError:
            raise ResticError("restic is not installed") from None
        assert proc.stdout is not None and proc.stderr is not None
        errors: list[dict[str, Any]] = []
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("message_type") == "error":
                    errors.append(msg)
                yield msg
        finally:
            stderr = proc.stderr.read()
            code = proc.wait()
        if code not in ok_codes:
            raise ResticError(_explain(code, stderr), code)

    # -- commands ------------------------------------------------------------

    def version(self) -> tuple[int, int, int]:
        out = self.run(["version"]).stdout.split()
        try:
            parts = out[1].split(".")
            return int(parts[0]), int(parts[1]), int(parts[2].split("-")[0])
        except (IndexError, ValueError):
            raise ResticError(f"cannot read the restic version from {out!r}") from None

    def check_version(self) -> None:
        if self.version()[:2] < MIN_VERSION:
            raise ResticError("restic {}.{} or newer is needed".format(*MIN_VERSION))

    def init(self) -> str:
        msgs = [json.loads(x) for x in self.run(["init", "--json"]).stdout.splitlines() if x]
        return next((m["id"] for m in msgs if m.get("message_type") == "initialized"), "")

    def is_initialised(self) -> bool:
        try:
            self.run(["cat", "config"], ok_codes=(0,), timeout=120)
        except ResticError as e:
            if e.code == EXIT_NO_REPO:
                return False
            if e.code == EXIT_WRONG_PASSWORD:
                return True  # someone's backups are there, just not with our key
            raise
        return True

    def keys(self, env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        return json.loads(self.run(["key", "list", "--json"], env=env).stdout or "[]")

    def current_key_id(self) -> str | None:
        return next((k["id"] for k in self.keys() if k.get("current")), None)

    def add_key(
        self, new_password: str, *, host: str, user: str, env: Mapping[str, str] | None = None
    ) -> str:
        """Add a key; the new password goes in on stdin. Returns the new key id."""
        before = {k["id"] for k in self.keys(env)}
        self.run(
            ["key", "add", "--new-password-file", "/dev/stdin", "--host", host, "--user", user],
            stdin=new_password,
            env=env,
        )
        after = self.keys(env)
        new = [k["id"] for k in after if k["id"] not in before]
        if len(new) != 1:
            raise ResticError("could not find the key just added")
        return new[0]

    def snapshots(self) -> list[dict[str, Any]]:
        return json.loads(self.run(["snapshots", "--json"]).stdout or "[]") or []

    def backup(
        self, files_from: Path, exclude_file: Path, tags: Sequence[str]
    ) -> Iterator[dict[str, Any]]:
        args = [
            "backup",
            "--json",
            "--files-from-verbatim",
            str(files_from),
            "--exclude-file",
            str(exclude_file),
            "--exclude-caches",
        ]
        for t in tags:
            args += ["--tag", t]
        return self.stream(args, ok_codes=(0, EXIT_PARTIAL))

    def check(self, read_data_subset: str | None = None) -> dict[str, Any]:
        args = ["check", "--json"]
        if read_data_subset:
            args.append(f"--read-data-subset={read_data_subset}")
        summary: dict[str, Any] = {}
        for msg in self.stream(args, ok_codes=(0, 1)):
            if msg.get("message_type") == "summary":
                summary = msg
        return summary

    def restore(
        self,
        snapshot: str,
        target: Path,
        *,
        includes: Sequence[str] = (),
        dry_run: bool = False,
        overwrite: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        args = ["restore", snapshot, "--target", str(target), "--json"]
        for inc in includes:
            args += ["--include", inc]
        if dry_run:
            args += ["--dry-run", "-vv"]
        if overwrite:
            args += ["--overwrite", overwrite]
        return self.stream(args)


# Variables restic may need from the session; everything else stays out.
_PASS_THROUGH = {
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",  # secret-tool talks to GNOME Keyring over D-Bus
    "SSH_AUTH_SOCK",  # sftp: the person's ssh agent
}


def _explain(code: int, stderr: str) -> str:
    known = {
        EXIT_NO_REPO: "there is no backup repository at this place",
        EXIT_LOCKED: "the backup repository is busy (locked by another run)",
        EXIT_WRONG_PASSWORD: "the key does not open this backup",
    }
    if code in known:
        return known[code]
    last = [x for x in stderr.strip().splitlines() if x.strip()]
    return f"restic failed ({code}): {last[-1] if last else 'no details'}"
