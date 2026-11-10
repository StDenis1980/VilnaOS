# SPDX-License-Identifier: GPL-3.0-or-later
"""vilna-home-setup: finishing a home useradd left unlabeled (ADR 0026).

The script runs as root under SELinux; here the SELinux and ownership
commands are stand-ins on PATH, so the test runs anywhere. The labels and
owners the stand-in stat reports live in a small table.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "systemd" / "vilna-home-setup"

PASSWD = """\
root:x:0:0:root:/root:/bin/bash
olena:x:1000:1000:Olena:{home}/olena:/bin/bash
taras:x:1001:1001:Taras:{home}/taras:/bin/bash
"""

STUBS = {
    "selinuxenabled": "exit 0",
    "pgrep": "exit 1",
    "getent": 'cat "$STUB_DIR/passwd"',
    # Labels and owners from the table "path type uid"; anything else is real stat.
    "stat": r"""
fmt=$2; path=$4
case "$fmt" in
  %C|%u)
    line=$(awk -v p="$path" '$1 == p' "$STUB_DIR/table")
    if [ -n "$line" ]; then
      set -- $line
      if [ "$fmt" = %C ]; then echo "system_u:object_r:$2:s0"; else echo "$3"; fi
      exit 0
    fi
    [ "$fmt" = %C ] && echo "system_u:object_r:user_home_t:s0" || /usr/bin/stat -c %u -- "$path"
    ;;
  *) exec /usr/bin/stat "$@" ;;
esac
""",
    "restorecon": 'echo "restorecon $*" >>"$STUB_DIR/log"',
    "chown": 'shift 2; for f in "$@"; do echo "chown $f" >>"$STUB_DIR/log"; done',
}


@pytest.fixture
def env(tmp_path: Path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in STUBS.items():
        p = stubs / name
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    (stubs / "passwd").write_text(PASSWD.format(home=home))
    skel = tmp_path / "skel"
    (skel / ".config").mkdir(parents=True)
    (skel / ".bashrc").write_text("# skel\n")
    defs = tmp_path / "login.defs"
    defs.write_text("UMASK 022\nHOME_MODE 0700\n")

    def run(table: dict[str, tuple[str, int]]) -> tuple[subprocess.CompletedProcess, list[str]]:
        (stubs / "table").write_text(
            "".join(f"{home / name} {kind} {uid}\n" for name, (kind, uid) in table.items())
        )
        log = stubs / "log"
        log.write_text("")
        result = subprocess.run(
            ["sh", str(SCRIPT)],
            env={
                **os.environ,
                "PATH": f"{stubs}:{os.environ['PATH']}",
                "STUB_DIR": str(stubs),
                "VILNA_HOME_ROOT": str(home),
                "VILNA_SKEL_DIRS": str(skel),
                "VILNA_LOGIN_DEFS": str(defs),
                "VILNA_USERADD_WAIT": "0",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        return result, log.read_text().splitlines()

    return home, run


def test_unlabeled_root_home_is_finished(env) -> None:
    home, run = env
    (home / "olena").mkdir(mode=0o755)
    result, log = run({"olena": ("unlabeled_t", 0)})
    assert result.returncode == 0, result.stderr
    olena = home / "olena"
    assert (olena / ".bashrc").read_text() == "# skel\n"
    assert (olena / ".config").is_dir()
    assert oct(olena.stat().st_mode & 0o777) == "0o700"
    chowned = [line for line in log if line.startswith("chown ")]
    assert f"chown {olena}" in chowned
    assert f"chown {olena / '.bashrc'}" in chowned
    assert f"restorecon -RF -- {olena}" in log


def test_skeleton_never_replaces_files(env) -> None:
    home, run = env
    olena = home / "olena"
    olena.mkdir()
    (olena / ".bashrc").write_text("mine\n")
    run({"olena": ("unlabeled_t", 0)})
    assert (olena / ".bashrc").read_text() == "mine\n"


def test_snapshots_are_left_alone(env) -> None:
    home, run = env
    olena = home / "olena"
    (olena / ".snapshots" / "1").mkdir(parents=True)
    _, log = run({"olena": ("unlabeled_t", 0)})
    assert not any(".snapshots" in line for line in log if line.startswith("chown "))
    assert f"restorecon -RF -e {olena / '.snapshots'} -- {olena}" in log


def test_labelled_homes_are_not_touched(env) -> None:
    home, run = env
    (home / "olena").mkdir()
    (home / "taras").mkdir()
    result, log = run({"olena": ("user_home_dir_t", 0), "taras": ("user_home_dir_t", 1001)})
    assert result.returncode == 0
    assert log == []
    assert not (home / "olena" / ".bashrc").exists()


def test_unlabeled_home_with_an_owner_is_only_labelled(env) -> None:
    home, run = env
    (home / "taras").mkdir()
    _, log = run({"taras": ("unlabeled_t", 1001)})
    assert not (home / "taras" / ".bashrc").exists()
    assert log == [f"restorecon -RF -- {home / 'taras'}"]


def test_home_of_nobody_is_only_labelled(env) -> None:
    home, run = env
    (home / "lost").mkdir()
    _, log = run({"lost": ("unlabeled_t", 0)})
    assert log == [f"restorecon -F -- {home / 'lost'}"]
    assert not (home / "lost" / ".bashrc").exists()


def test_nothing_happens_without_selinux(env, tmp_path: Path) -> None:
    home, run = env
    (tmp_path / "stubs" / "selinuxenabled").write_text("#!/bin/sh\nexit 1\n")
    (home / "olena").mkdir()
    result, log = run({"olena": ("unlabeled_t", 0)})
    assert result.returncode == 0
    assert log == []
