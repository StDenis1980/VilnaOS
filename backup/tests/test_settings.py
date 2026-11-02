# SPDX-License-Identifier: GPL-3.0-or-later
import json

import pytest

from vilna_backup.settings import (
    Settings,
    SettingsError,
    load_settings,
    parse_destination,
    save_settings,
)


def test_defaults_follow_the_first_run_dialog():
    s = Settings()
    assert s.preset == "documents"  # recommended in design.md 2a
    assert s.schedule == "daily"
    assert not s.gloxi_history and not s.system_secrets
    assert not s.enabled and not s.recovery_confirmed


@pytest.mark.parametrize(
    ("text", "kind", "repo"),
    [
        ("/media/usb/vilna", "local", "/media/usb/vilna"),
        ("sftp:me@nas:/srv/vilna", "sftp", "sftp:me@nas:/srv/vilna"),
        ("s3:https://s3.example.eu/bucket/vilna", "s3", "s3:https://s3.example.eu/bucket/vilna"),
        ("rest:https://backup.example.eu/me/", "rest", "rest:https://backup.example.eu/me/"),
    ],
)
def test_destinations(text, kind, repo):
    d = parse_destination(text)
    assert d.kind == kind and d.repository() == repo


@pytest.mark.parametrize(
    "text",
    ["", "vilna", "ftp:host/x", "rest:http://example.eu/", "rest:https://me:pw@example.eu/"],
)
def test_bad_destinations(text):
    with pytest.raises(SettingsError):
        parse_destination(text)


def test_round_trip_has_no_secrets(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(destination=parse_destination("/media/usb"), include=("/home/a/Music",))
    save_settings(s, path)
    assert load_settings(path) == s
    data = json.loads(path.read_text())
    assert "password" not in json.dumps(data).lower()


def test_unknown_keys_are_refused(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"version": 1, "repo_password": "x"}))
    with pytest.raises(SettingsError):
        load_settings(path)


def test_bad_values():
    with pytest.raises(SettingsError):
        Settings().with_changes(preset="everything")
    with pytest.raises(SettingsError):
        Settings().with_changes(include=("relative/path",))
    with pytest.raises(SettingsError):
        Settings().with_changes(limit_bytes=0)
