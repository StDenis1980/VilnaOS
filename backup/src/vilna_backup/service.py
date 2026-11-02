# SPDX-License-Identifier: GPL-3.0-or-later
"""Backup operations: set up, run, check, list, restore, recover.

The levels of each operation (design.md 5) are enforced by Herkos, not here:
the MCP server ``mcp/backup`` offers Gloxi only the R0-R2 ones. What this
module enforces itself, whoever calls it (defence in depth):

- nothing runs before the recovery sheet is confirmed (design.md 3a.2);
- the assistant cannot add system secrets or folders outside the home folder;
- a restore through the assistant goes only into a new, empty folder.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vilna_backup import keys, plan, schedule
from vilna_backup.restic import Restic, ResticError
from vilna_backup.settings import (
    PRESETS,
    Destination,
    Settings,
    SettingsError,
    load_settings,
    save_settings,
    settings_path,
    state_dir,
)

# restic check reads back this share of the data once a month (design.md 6).
READ_DATA_SUBSET = "5%"
READ_DATA_EVERY = 30 * 86400
MAX_LOG_LINES = 500
TAG = "vilna"


class BackupError(Exception):
    """An operation that cannot go ahead; the message is for people."""


@dataclass
class Backup:
    layout: plan.Layout
    store: keys.KeyStore
    settings_file: Path
    state: Path
    restic_binary: str = "restic"
    clock: Callable[[], float] = time.time
    run_cmd: Callable[..., subprocess.CompletedProcess] = subprocess.run

    @classmethod
    def from_env(cls) -> Backup:
        return cls(
            layout=plan.Layout.from_env(),
            store=keys.default_store(),
            settings_file=settings_path(),
            state=state_dir(),
            restic_binary=os.environ.get("VILNA_BACKUP_RESTIC", "restic"),
        )

    # -- settings ------------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return load_settings(self.settings_file)

    def save(self, settings: Settings) -> Settings:
        save_settings(settings, self.settings_file)
        return settings

    def restic(self, settings: Settings | None = None, *, password_env=None) -> Restic:
        settings = settings or self.settings
        if settings.destination is None:
            raise BackupError("no place for backups is set up yet")
        env = dict(password_env or self.store.password_env(settings.repo_id))
        env.update(keys.credential_env(self.store, settings.repo_id, settings.destination.kind))
        return Restic(
            repository=settings.destination.repository(),
            env=env,
            binary=self.restic_binary,
            cache_dir=self.layout.home / ".cache" / "restic",
        )

    # -- R3, in the terminal: the place and the keys -------------------------

    def set_up(self, destination: Destination, credentials: dict[str, str] | None = None) -> str:
        """Create a new repository at the place. Returns the recovery key, to show once.

        Backups stay off until :meth:`confirm_recovery`.
        """
        current = self.settings
        if current.destination is not None and current.recovery_confirmed:
            raise BackupError("backups are already set up; changing the place is R4")
        # Keep what the person already chose in the first-run dialog (what,
        # how much, how often); only the place and the keys are new.
        settings = Settings(
            destination=destination,
            preset=current.preset,
            include=current.include,
            exclude=current.exclude,
            gloxi_history=current.gloxi_history,
            system_secrets=current.system_secrets,
            limit_bytes=current.limit_bytes,
            schedule=current.schedule,
        )
        for name, value in (credentials or {}).items():
            self.store.put(settings.repo_id, name.lower(), value)
        password = keys.new_repo_password()
        self.store.put(settings.repo_id, keys.REPO_PASSWORD, password)
        r = self.restic(settings)
        r.check_version()
        if r.is_initialised():
            raise BackupError(
                "there are backups at this place already; to use them on this device, "
                "run `vilna-backup recover`"
            )
        r.init()
        device_key = r.current_key_id()
        recovery = keys.new_recovery_key()
        recovery_key_id = r.add_key(recovery, host="recovery-sheet", user="vilna-recovery")
        self.save(settings.with_changes(device_key_id=device_key, recovery_key_id=recovery_key_id))
        return recovery

    def confirm_recovery(self, typed: str) -> Settings:
        """The person typed the recovery key back from the sheet: backups may start."""
        settings = self.settings
        if settings.destination is None:
            raise BackupError("set up a place first")
        try:
            key = keys.normalise_recovery_key(typed)
        except keys.KeysError as e:
            raise BackupError(str(e)) from None
        r = self.restic(settings, password_env={"RESTIC_PASSWORD": key})
        try:
            current = r.current_key_id()
        except ResticError as e:
            raise BackupError(f"this is not the recovery key: {e}") from None
        if current != settings.recovery_key_id:
            raise BackupError("this key opens the backups, but it is not the recovery key")
        return self.save(settings.with_changes(recovery_confirmed=True, enabled=True))

    def recover(
        self, destination: Destination, typed_key: str, credentials: dict[str, str] | None = None
    ) -> Settings:
        """A new device: open existing backups with the recovery key, add a device key."""
        try:
            key = keys.normalise_recovery_key(typed_key)
        except keys.KeysError as e:
            raise BackupError(str(e)) from None
        settings = Settings(destination=destination)
        for name, value in (credentials or {}).items():
            self.store.put(settings.repo_id, name.lower(), value)
        with_recovery = self.restic(settings, password_env={"RESTIC_PASSWORD": key})
        with_recovery.check_version()
        try:
            recovery_key_id = with_recovery.current_key_id()
        except ResticError as e:
            raise BackupError(f"cannot open the backups: {e}") from None
        password = keys.new_repo_password()
        device_key_id = with_recovery.add_key(
            password, host=socket.gethostname() or "vilna", user="vilna-device"
        )
        self.store.put(settings.repo_id, keys.REPO_PASSWORD, password)
        return self.save(
            settings.with_changes(
                device_key_id=device_key_id,
                recovery_key_id=recovery_key_id,
                recovery_confirmed=True,  # they have the sheet: they just used it
                enabled=False,  # the first-run dialog asks again what to back up
            )
        )

    # -- R0-R1 ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        s = self.settings
        out: dict[str, Any] = {
            "enabled": s.enabled,
            "set_up": s.destination is not None,
            "destination": s.destination.describe() if s.destination else None,
            "recovery_sheet_confirmed": s.recovery_confirmed,
            "preset": s.preset,
            "include": list(s.include),
            "exclude": list(s.exclude),
            "gloxi_history": s.gloxi_history,
            "system_secrets": s.system_secrets,
            "limit_bytes": s.limit_bytes,
            "schedule": s.schedule,
            "running": self._progress(),
            "last_backup": self._last("backup"),
            "last_check": self._last("check"),
        }
        return out

    def overview(self) -> dict[str, Any]:
        """Sizes for the first-run dialog (step 2) and the fit for the limit (step 4)."""
        s = self.settings
        current = plan.build_plan(s, self.layout)
        return {
            "groups": plan.overview(self.layout, s),
            "current": plan.fit(current, s.limit_bytes, self.layout),
        }

    def snapshots(self) -> list[dict[str, Any]]:
        out = []
        for snap in self.restic().snapshots():
            summary = snap.get("summary") or {}
            out.append(
                {
                    "id": snap.get("short_id") or snap.get("id", "")[:8],
                    "time": snap.get("time"),
                    "files": summary.get("total_files_processed"),
                    "bytes": summary.get("total_bytes_processed"),
                    "tags": [t for t in snap.get("tags") or [] if not t.startswith("home=")],
                }
            )
        return out

    # -- R2: what, how much, how often; run now ------------------------------

    def set_paths(
        self,
        *,
        preset: str | None = None,
        add: Sequence[str] = (),
        remove: Sequence[str] = (),
        gloxi_history: bool | None = None,
        limit_bytes: int | None | bool = False,
        system_secrets: bool | None = None,
        by_assistant: bool = True,
    ) -> Settings:
        s = self.settings
        if preset is not None and preset not in PRESETS:
            raise BackupError(f"preset must be one of {', '.join(PRESETS)}")
        if system_secrets is not None and by_assistant:
            raise BackupError("system secrets are switched on or off by the person only")
        include = list(s.include)
        exclude = list(s.exclude)
        for raw in add:
            p = self._own_folder(raw, by_assistant)
            if str(p) in exclude:
                exclude.remove(str(p))
            elif str(p) not in include:
                include.append(str(p))
        for raw in remove:
            p = self._own_folder(raw, by_assistant=False)
            if str(p) in include:
                include.remove(str(p))
            elif str(p) not in exclude:
                exclude.append(str(p))
        changes: dict[str, Any] = {"include": tuple(include), "exclude": tuple(exclude)}
        if preset is not None:
            changes["preset"] = preset
        if gloxi_history is not None:
            changes["gloxi_history"] = bool(gloxi_history)
        if system_secrets is not None:
            changes["system_secrets"] = bool(system_secrets)
        if limit_bytes is not False:
            changes["limit_bytes"] = limit_bytes
        try:
            return self.save(s.with_changes(**changes))
        except SettingsError as e:
            raise BackupError(str(e)) from None

    def _own_folder(self, raw: str, by_assistant: bool) -> Path:
        if not isinstance(raw, str) or not os.path.isabs(raw) or "\x00" in raw:
            raise BackupError(f"not an absolute folder: {raw!r}")
        p = Path(os.path.normpath(raw))
        home = self.layout.home
        if by_assistant:
            if not (p == home or p.is_relative_to(home)):
                raise BackupError(f"{p} is outside the home folder")
            for secret in plan.secret_paths(self.layout):
                if p == secret or p.is_relative_to(secret) or secret.is_relative_to(p):
                    raise BackupError(f"{p} holds system secrets: only the person can add it")
        return p

    def set_schedule(self, value: str) -> dict[str, Any]:
        s = self.settings
        try:
            s = self.save(s.with_changes(schedule=value))
        except SettingsError as e:
            raise BackupError(str(e)) from None
        note = schedule.apply(value if s.enabled else "off", run=self.run_cmd)
        return {"schedule": s.schedule, "note": note}

    def start(self) -> dict[str, Any]:
        """Start one backup in the background; :meth:`status` shows progress."""
        self._ready(self.settings)
        if self._progress():
            return {"started": False, "message": "a backup is already running"}
        if schedule.start_now(run=self.run_cmd):
            return {"started": True, "via": "systemd"}
        subprocess.Popen(
            [*schedule.command(), "run"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return {"started": True, "via": "process"}

    # -- running -------------------------------------------------------------

    def _ready(self, s: Settings) -> None:
        if s.destination is None:
            raise BackupError("no place for backups is set up yet")
        if not s.recovery_confirmed:
            raise BackupError("the recovery sheet is not confirmed yet; backups are off")
        if not s.enabled:
            raise BackupError("backups are switched off")

    def run(self, *, scheduled: bool = False) -> dict[str, Any]:
        s = self.settings
        try:
            self._ready(s)
        except BackupError as e:
            if scheduled:
                return self._log("backup", ok=False, message=str(e))
            raise
        with self._lock() as locked:
            if not locked:
                raise BackupError("a backup is already running")
            try:
                return self._run(s)
            except (BackupError, ResticError, OSError) as e:
                self._log("backup", ok=False, message=str(e))
                raise BackupError(str(e)) from None
            finally:
                self._progress_file.unlink(missing_ok=True)

    def _run(self, s: Settings) -> dict[str, Any]:
        p = plan.build_plan(s, self.layout)
        if not p.includes:
            raise BackupError("nothing to back up: none of the chosen folders exists")
        if s.limit_bytes is not None:
            size = plan.plan_size(p)
            if size > s.limit_bytes:
                raise BackupError(
                    f"the backup does not fit the limit ({size} > {s.limit_bytes} bytes); "
                    "leave out some large folders"
                )
        self._export_lists()
        self.state.mkdir(parents=True, exist_ok=True)
        files_from = self.state / "files-from.txt"
        exclude_file = self.state / "exclude.txt"
        files_from.write_text("\n".join(str(x) for x in p.includes) + "\n", encoding="utf-8")
        exclude_file.write_text("\n".join(p.exclude_lines()) + "\n", encoding="utf-8")
        tags = [TAG, f"preset={s.preset}"]
        if "," not in str(self.layout.home):
            tags.append(f"home={self.layout.home}")
        summary: dict[str, Any] = {}
        errors = 0
        started = self.clock()
        for msg in self.restic(s).backup(files_from, exclude_file, tags):
            kind = msg.get("message_type")
            if kind == "status":
                self._write_progress(started, msg)
            elif kind == "summary":
                summary = msg
            elif kind == "error":
                errors += 1
        if not summary.get("snapshot_id"):
            raise BackupError("restic finished without making a snapshot")
        return self._log(
            "backup",
            ok=True,
            snapshot=summary["snapshot_id"][:8],
            files=summary.get("total_files_processed"),
            bytes=summary.get("total_bytes_processed"),
            added=summary.get("data_added_packed", summary.get("data_added")),
            unreadable=errors,
            message="done" if not errors else f"done; {errors} files could not be read",
        )

    def _export_lists(self) -> None:
        """Things not stored as plain files: installed Flatpak apps, dconf settings."""
        out = self.layout.export_dir
        out.mkdir(parents=True, exist_ok=True)
        for name, cmd in (
            ("flatpak-apps.txt", ["flatpak", "list", "--app", "--columns=application"]),
            ("dconf.ini", ["dconf", "dump", "/"]),
        ):
            if shutil.which(cmd[0]) is None:
                continue
            try:
                p = self.run_cmd(cmd, capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if p.returncode == 0:
                (out / name).write_text(p.stdout, encoding="utf-8")

    def check(self, *, auto: bool = False, read_data: bool = False) -> dict[str, Any]:
        """Weekly structure check; with ``auto``, once a month also read back 5%."""
        s = self.settings
        if auto:
            if s.destination is None or not s.enabled:
                return {"ok": True, "skipped": "backups are off"}
            last = self._last("check", where=lambda r: r.get("read_data"))
            read_data = last is None or self.clock() - last.get("time", 0) > READ_DATA_EVERY
        with self._lock() as locked:
            if not locked:
                raise BackupError("a backup is running; check later")
            try:
                summary = self.restic(s).check(READ_DATA_SUBSET if read_data else None)
            except ResticError as e:
                return self._log("check", ok=False, read_data=read_data, message=str(e))
        errors = summary.get("num_errors", 0)
        ok = not errors and not summary.get("broken_packs")
        return self._log(
            "check",
            ok=ok,
            read_data=read_data,
            message="the backups are in order" if ok else f"{errors} problems found",
        )

    # -- restore ---------------------------------------------------------------

    def _snapshot(self, snapshot: str) -> dict[str, Any]:
        snaps = self.restic().snapshots()
        if not snaps:
            raise BackupError("there are no backups yet")
        if snapshot == "latest":
            return max(snaps, key=lambda x: x.get("time", ""))
        for snap in snaps:
            if snap.get("id", "").startswith(snapshot) and len(snapshot) >= 8:
                return snap
        raise BackupError(f"no backup {snapshot}")

    @staticmethod
    def _home_of(snap: dict[str, Any]) -> str:
        for t in snap.get("tags") or []:
            if t.startswith("home="):
                return t.removeprefix("home=")
        paths = snap.get("paths") or ["/"]
        return os.path.commonpath(paths) if len(paths) > 1 else os.path.dirname(paths[0])

    def restore_to_new_folder(
        self, target: str, *, snapshot: str = "latest", path: str | None = None
    ) -> dict[str, Any]:
        """R2: bring back a backup (or one file or folder of it) into a new, empty folder."""
        if not os.path.isabs(target):
            raise BackupError("the target must be an absolute folder")
        dest = Path(os.path.normpath(target))
        home = self.layout.home
        if dest == home or not dest.is_relative_to(home):
            raise BackupError("restore into a new folder inside the home folder")
        if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
            raise BackupError(f"{dest} is not empty; choose a new folder")
        snap = self._snapshot(snapshot)
        root = self._home_of(snap)
        includes = []
        if path:
            rel = path.lstrip("/")
            if not rel or ".." in Path(rel).parts:
                raise BackupError("path must be inside the home folder of the backup")
            includes.append("/" + rel)
        dest.mkdir(parents=True, exist_ok=True)
        summary = self._restore(f"{snap['id']}:{root}", dest, includes)
        return {
            "snapshot": snap["id"][:8],
            "target": str(dest),
            "files": summary.get("files_restored", 0),
            "bytes": summary.get("bytes_restored", 0),
        }

    def restore_in_place(
        self, *, snapshot: str = "latest", confirm: Callable[[list[str], int], bool]
    ) -> dict[str, Any]:
        """R3, the person in the terminal: bring the home folder back as it was.

        Lists the files that would be replaced and asks before touching them.
        """
        snap = self._snapshot(snapshot)
        source = f"{snap['id']}:{self._home_of(snap)}"
        home = self.layout.home
        replaced: list[str] = []
        new = 0
        for msg in self.restic().restore(source, home, dry_run=True, overwrite="if-changed"):
            if msg.get("message_type") != "verbose_status":
                continue
            if msg.get("action") == "updated":
                # Items are relative to the snapshot folder, which is the home folder.
                replaced.append(str(home / msg.get("item", "").lstrip("/")))
            elif msg.get("action") == "restored":
                new += 1
        if not confirm(replaced, new):
            return {"snapshot": snap["id"][:8], "restored": False}
        summary = self._restore(source, home, [], overwrite="if-changed")
        return {
            "snapshot": snap["id"][:8],
            "restored": True,
            "replaced": len(replaced),
            "files": summary.get("files_restored", 0),
        }

    def _restore(
        self, source: str, target: Path, includes: list[str], overwrite: str | None = None
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for msg in self.restic().restore(source, target, includes=includes, overwrite=overwrite):
            if msg.get("message_type") == "summary":
                summary = msg
        return summary

    # -- state files -----------------------------------------------------------

    @property
    def _progress_file(self) -> Path:
        return self.state / "progress.json"

    @property
    def _log_file(self) -> Path:
        return self.state / "runs.jsonl"

    @contextlib.contextmanager
    def _lock(self) -> Iterator[bool]:
        self.state.mkdir(parents=True, exist_ok=True)
        with open(self.state / "run.lock", "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _write_progress(self, started: float, msg: dict[str, Any]) -> None:
        data = {
            "started": started,
            "percent": round(float(msg.get("percent_done") or 0) * 100, 1),
            "bytes_done": msg.get("bytes_done", 0),
            "total_bytes": msg.get("total_bytes", 0),
            "pid": os.getpid(),
        }
        tmp = self._progress_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self._progress_file)

    def _progress(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self._progress_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        pid = data.get("pid")
        if not isinstance(pid, int):
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None  # a run that died: not running
        except PermissionError:
            pass
        return {k: data.get(k) for k in ("started", "percent", "bytes_done", "total_bytes")}

    def _log(self, kind: str, *, ok: bool, **fields: Any) -> dict[str, Any]:
        rec = {"time": self.clock(), "kind": kind, "ok": ok, **fields}
        self.state.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        with contextlib.suppress(FileNotFoundError):
            lines = self._log_file.read_text(encoding="utf-8").splitlines()[-(MAX_LOG_LINES - 1) :]
        lines.append(json.dumps(rec, ensure_ascii=False))
        tmp = self._log_file.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, self._log_file)
        if not ok:
            print(f"vilna-backup: {kind} failed: {fields.get('message')}", file=sys.stderr)
        return rec

    def _last(self, kind: str, where: Callable[[dict], Any] | None = None) -> dict | None:
        try:
            lines = self._log_file.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return None
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == kind and (where is None or where(rec)):
                return rec
        return None
