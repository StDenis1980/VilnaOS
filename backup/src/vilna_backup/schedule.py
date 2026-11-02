# SPDX-License-Identifier: GPL-3.0-or-later
"""Timers: systemd user units for regular backups and checks (design.md 3, 6).

Two timers: ``vilna-backup.timer`` (hourly, daily or weekly, as chosen) and
``vilna-backup-check.timer`` (weekly ``restic check``; once a month it also
reads back 5% of the data, see ``service.check``).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from vilna_backup.settings import SCHEDULES

UNIT = "vilna-backup"
CHECK_UNIT = "vilna-backup-check"
CALENDAR = {"hourly": "hourly", "daily": "daily", "weekly": "weekly"}

Runner = Callable[..., subprocess.CompletedProcess]


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base and os.path.isabs(base) else Path.home() / ".config"
    return root / "systemd" / "user"


def command() -> list[str]:
    """How systemd should start us: the installed script, else this interpreter."""
    exe = shutil.which("vilna-backup")
    return [exe] if exe else [sys.executable, "-m", "vilna_backup"]


def service_text(description: str, args: Sequence[str], *, keystore_env: str | None) -> str:
    env = ""
    if keystore_env:
        env = f"Environment={shlex.quote('VILNA_BACKUP_KEYSTORE=' + keystore_env)}\n"
    return (
        "# Written by vilna-backup; changes are overwritten.\n"
        "[Unit]\n"
        f"Description={description}\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={shlex.join([*command(), *args])}\n"
        f"{env}"
        "Nice=10\n"
        "IOSchedulingClass=idle\n"
    )


def timer_text(description: str, calendar: str, delay: str) -> str:
    return (
        "# Written by vilna-backup; changes are overwritten.\n"
        "[Unit]\n"
        f"Description={description}\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar={calendar}\n"
        "Persistent=true\n"
        f"RandomizedDelaySec={delay}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def write_units(schedule: str, folder: Path | None = None) -> list[Path]:
    if schedule not in SCHEDULES:
        raise ValueError(f"unknown schedule {schedule}")
    folder = folder or unit_dir()
    folder.mkdir(parents=True, exist_ok=True)
    keystore = os.environ.get("VILNA_BACKUP_KEYSTORE") or None
    files = {
        f"{UNIT}.service": service_text(
            "Vilna Backup", ["run", "--scheduled"], keystore_env=keystore
        ),
        f"{CHECK_UNIT}.service": service_text(
            "Vilna Backup check", ["check", "--auto"], keystore_env=keystore
        ),
        f"{CHECK_UNIT}.timer": timer_text("Weekly check of Vilna backups", "weekly", "6h"),
    }
    if schedule != "off":
        files[f"{UNIT}.timer"] = timer_text(
            f"Vilna Backup ({schedule})",
            CALENDAR[schedule],
            "10min" if schedule == "hourly" else "1h",
        )
    written = []
    for name, text in files.items():
        path = folder / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    if schedule == "off":
        (folder / f"{UNIT}.timer").unlink(missing_ok=True)
    return written


def apply(schedule: str, *, run: Runner = subprocess.run, folder: Path | None = None) -> str:
    """Write the units and (re)start the timers. Returns a note for the person."""
    write_units(schedule, folder)
    steps: list[list[str]] = [["systemctl", "--user", "daemon-reload"]]
    if schedule == "off":
        steps.append(["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"])
    else:
        steps.append(["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"])
    steps.append(["systemctl", "--user", "enable", "--now", f"{CHECK_UNIT}.timer"])
    for cmd in steps:
        try:
            p = run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"units written, but systemd is not reachable: {e}"
        if p.returncode != 0 and "disable" not in cmd:
            return f"units written, but {' '.join(cmd[2:])} failed: {p.stderr.strip()}"
    return "timer off" if schedule == "off" else f"timer on: {schedule}"


def start_now(*, run: Runner = subprocess.run) -> bool:
    """Start one backup in the background through systemd. False if systemd cannot."""
    try:
        p = run(
            ["systemctl", "--user", "start", "--no-block", f"{UNIT}.service"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0
