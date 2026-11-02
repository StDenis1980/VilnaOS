# SPDX-License-Identifier: GPL-3.0-or-later
"""End to end with the real restic program (skipped where it is not installed)."""

from __future__ import annotations

import json

import pytest
from conftest import needs_restic

from vilna_backup import keys
from vilna_backup.service import Backup, BackupError
from vilna_backup.settings import parse_destination

pytestmark = needs_restic


def test_nothing_runs_before_the_recovery_sheet_is_confirmed(backup, tmp_path):
    backup.set_up(parse_destination(str(tmp_path / "usb")))
    assert not backup.settings.enabled
    with pytest.raises(BackupError, match="recovery sheet"):
        backup.run()
    with pytest.raises(BackupError, match="recovery sheet"):
        backup.start()
    rec = backup.run(scheduled=True)  # a timer firing early only logs
    assert rec["ok"] is False


def test_only_the_recovery_key_confirms(backup, tmp_path):
    recovery = backup.set_up(parse_destination(str(tmp_path / "usb")))
    with pytest.raises(BackupError):
        backup.confirm_recovery(keys.new_recovery_key())
    password = backup.store.get(backup.settings.repo_id, keys.REPO_PASSWORD)
    with pytest.raises(BackupError):
        backup.confirm_recovery(password)  # the device key is not the sheet
    s = backup.confirm_recovery(recovery.lower())
    assert s.enabled and s.recovery_confirmed
    assert s.device_key_id and s.recovery_key_id and s.device_key_id != s.recovery_key_id


def test_password_lives_in_the_key_store_only(ready, tmp_path):
    backup, recovery = ready
    password = backup.store.get(backup.settings.repo_id, keys.REPO_PASSWORD)
    assert password
    text = backup.settings_file.read_text()
    assert password not in text and recovery not in text
    for f in (tmp_path / "usb").rglob("*"):
        if f.is_file():
            data = f.read_bytes()
            assert password.encode() not in data and recovery.encode() not in data


def test_set_up_twice_is_refused(ready, tmp_path):
    backup, _ = ready
    with pytest.raises(BackupError, match="R4"):
        backup.set_up(parse_destination(str(tmp_path / "other")))


def test_existing_backups_need_recover(backup, tmp_path):
    first = Backup(**{**backup.__dict__, "settings_file": tmp_path / "other.json"})
    first.set_up(parse_destination(str(tmp_path / "usb")))
    with pytest.raises(BackupError, match="recover"):
        backup.set_up(parse_destination(str(tmp_path / "usb")))


def test_backup_and_restore_into_a_new_folder(ready, layout):
    backup, _ = ready
    rec = backup.run()
    assert rec["ok"] and rec["snapshot"]
    assert backup.status()["last_backup"]["snapshot"] == rec["snapshot"]
    snaps = backup.snapshots()
    assert len(snaps) == 1 and "vilna" in snaps[0]["tags"]

    target = layout.home / "Documents" / "Відновлено"
    out = backup.restore_to_new_folder(str(target))
    assert out["files"] > 0
    assert (target / "Documents" / "zvit.txt").read_text() == "звіт"
    assert (target / "Passwords.kdbx").exists()
    assert (target / ".config" / "dconf" / "user").exists()
    # Left out by the preset and the rules:
    assert not (target / "Videos").exists()
    assert not (target / "Downloads").exists()
    assert not (target / ".ssh").exists()
    assert not (target / ".local/share/gloxi/history").exists()
    assert not (target / "Documents/models/qwen.gguf").exists()
    assert not (target / ".var/app/org.gnome.Maps/cache").exists()


def test_restore_one_file(ready, layout):
    backup, _ = ready
    backup.run()
    target = layout.home / "Documents" / "one"
    backup.restore_to_new_folder(str(target), path="Documents/zvit.txt")
    files = [p for p in target.rglob("*") if p.is_file()]
    assert files == [target / "Documents" / "zvit.txt"]


def test_restore_never_overwrites(ready, layout):
    backup, _ = ready
    backup.run()
    with pytest.raises(BackupError, match="not empty"):
        backup.restore_to_new_folder(str(layout.home / "Documents"))
    with pytest.raises(BackupError, match="inside the home"):
        backup.restore_to_new_folder("/tmp/elsewhere")
    with pytest.raises(BackupError):
        backup.restore_to_new_folder(str(layout.home / "x"), path="../../etc")


def test_restore_in_place_asks_with_the_list(ready, layout):
    backup, _ = ready
    backup.run()
    zvit = layout.home / "Documents" / "zvit.txt"
    zvit.write_text("зіпсовано")
    (layout.home / "Pictures" / "2024" / "b.jpg").unlink()
    seen = {}

    def no(replaced, new):
        seen.update(replaced=replaced, new=new)
        return False

    assert backup.restore_in_place(confirm=no)["restored"] is False
    assert seen["replaced"] == [str(zvit)]
    assert seen["new"] >= 1
    assert zvit.read_text() == "зіпсовано"

    out = backup.restore_in_place(confirm=lambda r, n: True)
    assert out["restored"] and out["replaced"] == 1
    assert zvit.read_text() == "звіт"
    assert (layout.home / "Pictures" / "2024" / "b.jpg").exists()


def test_limit_is_respected(ready):
    backup, _ = ready
    backup.set_paths(limit_bytes=100)
    with pytest.raises(BackupError, match="does not fit"):
        backup.run()
    assert backup.status()["last_backup"]["ok"] is False


def test_new_device_with_the_recovery_key(ready, tmp_path, layout, systemd):
    old, recovery = ready
    old.run()
    new = Backup(
        layout=layout,
        store=keys.FileStore(tmp_path / "new-keys"),
        settings_file=tmp_path / "new-settings.json",
        state=tmp_path / "new-state",
        run_cmd=systemd,
    )
    with pytest.raises(BackupError):
        new.recover(old.settings.destination, keys.new_recovery_key())
    s = new.recover(old.settings.destination, recovery)
    assert s.recovery_confirmed and not s.enabled
    assert s.recovery_key_id == old.settings.recovery_key_id
    assert s.device_key_id not in (old.settings.device_key_id, s.recovery_key_id)
    assert len(new.snapshots()) == 1


def test_check(ready):
    backup, _ = ready
    backup.run()
    rec = backup.check(auto=True)
    assert rec["ok"] and rec["read_data"]  # first check reads data back
    rec = backup.check(auto=True)
    assert rec["ok"] and not rec["read_data"]  # then only once a month


def test_assistant_limits(ready, layout):
    backup, _ = ready
    with pytest.raises(BackupError, match="secrets"):
        backup.set_paths(add=[str(layout.home / ".ssh")])
    with pytest.raises(BackupError, match="secrets"):
        backup.set_paths(system_secrets=True)
    with pytest.raises(BackupError, match="outside"):
        backup.set_paths(add=["/etc"])
    s = backup.set_paths(
        add=[str(layout.home / "Videos")], remove=[str(layout.home / "Pictures/2019")]
    )
    assert s.include == (str(layout.home / "Videos"),)
    assert s.exclude == (str(layout.home / "Pictures/2019"),)
    s = backup.set_paths(system_secrets=True, by_assistant=False)
    assert s.system_secrets


def test_schedule_follows_enabled(ready, systemd):
    backup, _ = ready
    out = backup.set_schedule("hourly")
    assert out["schedule"] == "hourly" and out["note"] == "timer on: hourly"
    with pytest.raises(BackupError):
        backup.set_schedule("every minute")


def test_start_uses_systemd(ready, systemd):
    backup, _ = ready
    assert backup.start() == {"started": True, "via": "systemd"}
    assert ["systemctl", "--user", "start", "--no-block", "vilna-backup.service"] in systemd.calls


def test_run_log_keeps_no_paths(ready):
    backup, _ = ready
    backup.run()
    log = (backup.state / "runs.jsonl").read_text()
    rec = json.loads(log.splitlines()[-1])
    assert set(rec) >= {"time", "kind", "ok", "snapshot"}
    assert "zvit" not in log
