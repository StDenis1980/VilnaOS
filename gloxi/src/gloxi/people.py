# SPDX-License-Identifier: GPL-3.0-or-later
"""vilna-people-sync: a Gloxi for every person on this computer (ADR 0025).

Runs as root, on every boot and after every change of ``/etc/passwd``
(``vilna-people-sync.path``). For each person (uid 1000-59999 with a login
shell) it makes sure that:

1. the system user ``gloxi-<uid>`` exists (group ``gloxi``), so that the
   person's Gloxi runs apart from them and from everybody else;
2. ``/etc/herkos/system.toml`` lists the person and their Gloxi, so the
   system Herkos knows whom a connection speaks for; herkosd reads the file
   only when it starts, so it is restarted when the list changes;
3. the socket ``gloxi@<uid>.socket`` is enabled, so the window finds Gloxi.

For a person who is gone it undoes all three. The person's own data in
``/var/lib/gloxi/<uid>/`` and ``/var/lib/herkos/<uid>/`` is left for whoever
removes the account.

Nothing here needs a running systemd: inside an image build (a chroot) it
only enables units and starts nothing.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

FIRST_UID = 1000
LAST_UID = 59999
GROUP = "gloxi"
NO_LOGIN = {"", "/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false"}
GLOXI_USER = re.compile(r"^gloxi-(\d{1,10})$")
NAME = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}\$?$", re.IGNORECASE)

HEADER = """\
# SPDX-License-Identifier: GPL-3.0-or-later
# /etc/herkos/system.toml: who the system Herkos answers.
#
# Written by vilna-people-sync (gloxi package) from /etc/passwd: one
# [[person]] per person on this computer, and the system user their Gloxi
# runs as. Changes made by hand are overwritten.
"""


@dataclass(frozen=True)
class Person:
    name: str
    uid: int


def parse_passwd(text: str) -> list[tuple[str, int, str]]:
    """(name, uid, shell) of each well-formed line; anything odd is skipped."""
    out = []
    for line in text.splitlines():
        if not line or line.startswith(("#", "+", "-")):
            continue
        fields = line.split(":")
        if len(fields) != 7:
            continue
        name, _pw, uid, _gid, _gecos, _home, shell = fields
        if not uid.isdigit() or len(uid) > 10:
            continue
        out.append((name, int(uid), shell.strip()))
    return out


def people(text: str) -> list[Person]:
    found = []
    for name, uid, shell in parse_passwd(text):
        if not FIRST_UID <= uid <= LAST_UID or shell in NO_LOGIN:
            continue
        if not NAME.match(name):
            continue  # a name system.toml could not hold safely
        found.append(Person(name, uid))
    return sorted(found, key=lambda p: p.uid)


def gloxi_users(text: str) -> set[int]:
    """The people (by uid) who already have a gloxi-<uid> user."""
    out = set()
    for name, _uid, _shell in parse_passwd(text):
        m = GLOXI_USER.match(name)
        if m:
            out.add(int(m.group(1)))
    return out


def system_toml(found: Sequence[Person]) -> str:
    lines = [HEADER]
    for p in found:
        lines.append(f'\n[[person]]\nuser = "{p.name}"\ngloxi = "gloxi-{p.uid}"\n')
    return "".join(lines)


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".system.toml.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


Runner = Callable[[list[str]], int]


def run(cmd: list[str]) -> int:
    return subprocess.run(cmd, check=False).returncode


class Sync:
    def __init__(
        self,
        *,
        passwd: Path = Path("/etc/passwd"),
        config: Path = Path("/etc/herkos/system.toml"),
        runner: Runner = run,
        systemd_running: Callable[[], bool] = lambda: Path("/run/systemd/system").is_dir(),
    ) -> None:
        self.passwd = passwd
        self.config = config
        self.run = runner
        self.systemd_running = systemd_running
        self.failed: list[str] = []

    def _do(self, *cmd: str) -> bool:
        code = self.run(list(cmd))
        if code != 0:
            self.failed.append(" ".join(cmd))
        return code == 0

    def apply(self) -> int:
        text = self.passwd.read_text(encoding="utf-8", errors="replace")
        found = people(text)
        have = gloxi_users(text)
        live = self.systemd_running()
        wanted = {p.uid for p in found}

        for p in found:
            if p.uid not in have:
                self._do(
                    "useradd",
                    "--system",
                    "--gid",
                    GROUP,
                    "--home-dir",
                    f"/var/lib/gloxi/{p.uid}",
                    "--no-create-home",
                    "--shell",
                    "/usr/sbin/nologin",
                    "--comment",
                    f"Gloxi for uid {p.uid}",
                    f"gloxi-{p.uid}",
                )
            self._do("systemctl", "enable", f"gloxi@{p.uid}.socket")
            if live:
                self._do("systemctl", "start", f"gloxi@{p.uid}.socket")

        for uid in sorted(have - wanted):
            self._do("systemctl", "disable", f"gloxi@{uid}.socket")
            if live:
                self._do("systemctl", "stop", f"gloxi@{uid}.socket", f"gloxi@{uid}.service")
            # The internet opened for the cloud (gloxi.network) goes with them.
            self._do("systemctl", "revert", f"gloxi@{uid}.service")
            self._do("userdel", f"gloxi-{uid}")

        new = system_toml(found)
        try:
            old = self.config.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            old = None
        if new != old:
            write_atomic(self.config, new)
            if live:
                # herkosd reads the list only when it starts; agents reconnect.
                self._do("systemctl", "try-restart", "herkosd.service")

        for cmd in self.failed:
            print(f"vilna-people-sync: failed: {cmd}", file=sys.stderr)
        return 1 if self.failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vilna-people-sync", description="A Gloxi for every person (ADR 0025)"
    )
    parser.add_argument("--passwd", type=Path, default=Path("/etc/passwd"))
    parser.add_argument("--config", type=Path, default=Path("/etc/herkos/system.toml"))
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("vilna-people-sync: must run as root", file=sys.stderr)
        return 1
    return Sync(passwd=args.passwd, config=args.config).apply()


if __name__ == "__main__":
    raise SystemExit(main())
