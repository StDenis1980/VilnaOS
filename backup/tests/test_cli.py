# SPDX-License-Identifier: GPL-3.0-or-later
import json

import pytest

from vilna_backup import cli


@pytest.fixture
def env(tmp_path, home, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local/state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    monkeypatch.setenv("VILNA_BACKUP_KEYSTORE", f"file:{tmp_path / 'keys'}")
    return home


def run(capsys, *args):
    code = cli.main(["--json", *args])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def test_status_before_setup(env, capsys):
    code, data = run(capsys, "status")
    assert code == 0
    assert data["set_up"] is False and data["enabled"] is False


def test_set_and_overview(env, capsys):
    code, data = run(
        capsys,
        "set",
        "--preset",
        "settings",
        "--add",
        str(env / "Videos"),
        "--history",
        "yes",
        "--limit-gb",
        "50",
    )
    assert code == 0
    assert data["preset"] == "settings" and data["gloxi_history"] is True
    assert data["limit_bytes"] == 50 * 1000**3
    code, data = run(capsys, "overview")
    groups = {g["group"] for g in data["groups"]}
    assert {"settings", "documents", "pictures", "videos", "home"} <= groups
    assert data["current"]["fits"] is True


def test_errors_are_json(env, capsys):
    code, data = run(capsys, "set", "--add", "/etc")
    assert code == 1 and "outside" in data["error"]
    code, data = run(capsys, "start")
    assert code == 1 and "no place" in data["error"]


def test_person_only_commands_refuse_json(env, capsys):
    assert cli.main(["--json", "secrets", "on"]) == 1
    assert cli.main(["--json", "restore", "--in-place"]) == 1
