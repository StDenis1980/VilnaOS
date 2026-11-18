# SPDX-License-Identifier: GPL-3.0-or-later
"""Devices: list, add with a code, block, protect after theft (with real restic)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakeSystemd, make_home, needs_restic

from vilna_backup import devices, keys
from vilna_backup.plan import Layout
from vilna_backup.restic import Restic
from vilna_backup.service import Backup, BackupError

pytestmark = needs_restic


def other_device(root: Path, clock=None) -> Backup:
    layout = Layout.from_env({}, home=make_home(root))
    b = Backup(
        layout=layout,
        store=keys.FileStore(root / "keys"),
        settings_file=root / "settings.json",
        state=root / "state",
        run_cmd=FakeSystemd(),
    )
    if clock is not None:
        b.clock = clock
    return b


def joined(ready, tmp_path: Path) -> tuple[Backup, Backup]:
    b, _ = ready
    code = devices.start_pairing(b)["code"]
    new = other_device(tmp_path / "laptop")
    devices.join(new, b.settings.destination, code, name="Новий ноутбук")
    return b, new


def test_codes_are_80_bits_and_typed_forgivingly():
    code = keys.new_pairing_code()
    assert len(code.replace("-", "")) == 16
    assert keys.normalise_pairing_code(code.lower().replace("-", " ")) == code
    with pytest.raises(keys.KeysError):
        keys.normalise_pairing_code("ABCD-EFGH")


def test_list_shows_this_device_and_the_sheet(ready):
    b, _ = ready
    found = devices.list_devices(b)
    assert sorted(d["kind"] for d in found) == ["device", "recovery"]
    assert [d["this_device"] for d in found if d["kind"] == "device"] == [True]


def test_a_new_device_joins_with_a_code_once(ready, tmp_path):
    b, new = joined(ready, tmp_path)
    s = new.settings
    assert s.recovery_confirmed and not s.enabled
    assert s.recovery_key_id == b.settings.recovery_key_id
    names = {d["name"] for d in devices.list_devices(b) if d["kind"] == "device"}
    assert "Новий ноутбук" in names
    # The code is spent: no pairing key is left, the new device has its own key.
    assert all(d["kind"] != "pairing" for d in devices.list_devices(new))
    third = other_device(tmp_path / "third")
    with pytest.raises(BackupError, match="does not open"):
        devices.join(third, b.settings.destination, "0000-0000-0000-0000")


def test_an_expired_code_does_not_join_and_is_tidied(ready, tmp_path):
    b, _ = ready
    code = devices.start_pairing(b)["code"]
    later = b.clock() + devices.PAIRING_TTL + 1
    new = other_device(tmp_path / "late", clock=lambda: later)
    with pytest.raises(BackupError, match="expired"):
        devices.join(new, b.settings.destination, code)
    b.clock = lambda: later
    assert all(d["kind"] != "pairing" for d in devices.list_devices(b))


def test_the_recovery_key_is_not_a_code(ready, tmp_path):
    b, recovery = ready
    # Same shape is refused before restic is even asked: 8 groups, not 4.
    with pytest.raises(BackupError, match="4 groups"):
        devices.join(other_device(tmp_path / "x"), b.settings.destination, recovery)


def test_block_a_device_it_forgets_its_password(ready, tmp_path):
    b, new = joined(ready, tmp_path)
    laptop = next(d for d in devices.list_devices(b) if d["name"] == "Новий ноутбук")
    mine = next(d for d in devices.list_devices(b) if d["this_device"])
    sheet = next(d for d in devices.list_devices(b) if d["kind"] == "recovery")
    with pytest.raises(BackupError, match="device you are using"):
        devices.block(b, mine["id"])
    with pytest.raises(BackupError, match="recovery sheet"):
        devices.block(b, sheet["id"])
    out = devices.block(b, laptop["id"], stolen=True)
    assert out["next"] == "protect"
    assert "Новий ноутбук" not in {d["name"] for d in devices.list_devices(b)}

    new.save(new.settings.with_changes(enabled=True))
    with pytest.raises(BackupError, match="blocked"):
        new.run()
    s = new.settings
    assert not s.enabled and s.device_key_id is None
    assert new.store.get(s.repo_id, keys.REPO_PASSWORD) is None


def test_protect_after_theft_moves_backups_to_a_new_master_key(ready, tmp_path):
    b, new = joined(ready, tmp_path)
    b.run()
    old = b.settings
    place = Path(old.destination.location)
    old_config = (place / "config").read_bytes()
    stolen_password = new.store.get(new.settings.repo_id, keys.REPO_PASSWORD)

    out = devices.protect(b)

    s = b.settings
    assert s.destination == old.destination  # a local disk keeps its folder name
    assert s.repo_id != old.repo_id and not s.recovery_confirmed
    assert (place / "config").read_bytes() != old_config
    assert not list(place.parent.glob("*.new-*"))
    assert len(b.snapshots()) == 1
    # The stolen device's password opens nothing any more.
    r = Restic(str(place), {"RESTIC_PASSWORD": stolen_password})
    with pytest.raises(Exception):  # noqa: B017 - restic says: wrong password
        r.keys()
    # The new sheet opens the backups, and backups wait until it is confirmed.
    with pytest.raises(BackupError, match="recovery sheet"):
        b.run()
    b.confirm_recovery(out["recovery_key"])
    assert b.run()["ok"]
    log = [json.loads(x) for x in b._log_file.read_text().splitlines()]
    assert any(x["kind"] == "protected" for x in log)
