# SPDX-License-Identifier: Apache-2.0
"""The validator's rules (installer.md, section 5) on synthetic computers only."""

import copy

import pytest

from vilna_mcp_installer import plan as plans
from vilna_mcp_installer.machine import DemoMachine, Disk, GiB, Partition, survey


def surveyed(kind="empty"):
    return survey(DemoMachine(kind).disks())


def complete(kind="empty", *, method="tpm2"):
    """A plan the person finished: everything Gloxi and the person set."""
    s = surveyed(kind)
    p = plans.new_plan()
    plans.set_languages(p, ["uk_UA.UTF-8", "en_US.UTF-8"], ["ua", "us"])
    plans.set_network(p, "offline")
    plans.set_disk(p, s["disks"][0]["id"], "replace", s)
    plans.set_encryption(p, method)
    plans.set_user(p, "Олена Демо", "olena", "olena-laptop")
    plans.set_time(p, "Europe/Kyiv", "uk_UA.UTF-8")
    plans.mark_entered(p, "user")
    if method != "none":
        plans.acknowledge_recovery_key(p)
    return p, s


def codes(report):
    return [p["code"] for p in report["problems"]]


def test_a_new_plan_misses_every_step():
    report = plans.validate(plans.new_plan(), surveyed())
    assert report["missing"] == ["languages", "time", "network", "disk", "encryption", "user"]
    assert not report["valid"]


def test_the_simple_path_on_an_empty_disk_is_valid():
    p, s = complete()
    report = plans.validate(p, s)
    assert report["valid"], report
    assert report["changes"] == [
        {"action": "create", "disk": "nvme-Demo_SSD_512_0001", "size": 512 * GiB}
    ]


def test_replace_lists_every_partition_it_erases():
    p, s = complete("windows")
    report = plans.validate(p, s)
    assert report["valid"], report
    erased = [c for c in report["changes"] if c["action"] == "erase"]
    assert [c["name"] for c in erased] == ["nvme0n1p1", "nvme0n1p2", "nvme0n1p3", "nvme0n1p4"]
    windows = next(c for c in erased if c["name"] == "nvme0n1p3")
    assert windows["system"] == "Windows" and windows["holds_data"]


def test_a_secret_never_gets_into_the_plan():
    p, s = complete()
    p["user"]["password"] = "hunter2"
    assert "secret_in_plan" in codes(plans.validate(p, s))
    p, s = complete()
    p["network"] = {"mode": "wifi", "wifi": "Home", "secret": "my wifi key"}
    assert "secret_in_plan" in codes(plans.validate(p, s))


def test_passwords_must_be_entered_by_the_person():
    p, s = complete()
    p["user"]["password"] = plans.NEEDED
    assert "user_password" in codes(plans.validate(p, s))
    plans.set_network(p, "wifi", "Home", secured=True)
    assert p["network"]["secret"] == plans.NEEDED
    assert "wifi_password" in codes(plans.validate(p, s))
    plans.mark_entered(p, "network")
    plans.mark_entered(p, "user")
    assert plans.validate(p, s)["valid"]


def test_choosing_the_same_network_again_keeps_the_entered_password():
    p = plans.new_plan()
    plans.set_network(p, "wifi", "Home", secured=True)
    plans.mark_entered(p, "network")
    plans.set_network(p, "wifi", "Home", secured=True)
    assert p["network"]["secret"] == plans.ENTERED
    plans.set_network(p, "wifi", "Other", secured=True)
    assert p["network"]["secret"] == plans.NEEDED


def test_encryption_needs_the_recovery_key_saved():
    p, s = complete()
    del p["disk"]["encryption"]["recovery_key_acknowledged"]
    assert "recovery_key" in codes(plans.validate(p, s))


def test_a_passphrase_is_entered_like_a_password():
    p, s = complete(method="passphrase")
    assert "passphrase" in codes(plans.validate(p, s))
    plans.mark_entered(p, "passphrase")
    assert plans.validate(p, s)["valid"]


def test_no_encryption_is_allowed_with_a_warning():
    p, s = complete(method="none")
    report = plans.validate(p, s)
    assert report["valid"]
    assert [w["code"] for w in report["warnings"]] == ["no_encryption"]


def test_a_plan_from_another_set_of_disks_is_invalid():
    p, s = complete()
    plugged = DemoMachine("empty").disks() + [
        Disk("usb-Other_1", "sdb", "/dev/sdb", 64 * GiB, removable=True)
    ]
    assert "disks_changed" in codes(plans.validate(p, survey(plugged)))


def test_the_live_usb_stick_is_never_the_target():
    s = surveyed()
    stick = next(d for d in s["disks"] if d["boot_medium"])
    p, _ = complete()
    plans.set_disk(p, stick["id"], "replace", s)
    assert "boot_medium" in codes(plans.validate(p, s))


def test_too_small_disk():
    disks = [Disk("ata-Tiny_1", "sda", "/dev/sda", 20 * GiB)]
    s = survey(disks)
    p, _ = complete()
    plans.set_disk(p, "ata-Tiny_1", "replace", s)
    assert "too_small" in codes(plans.validate(p, s))


def test_raid_or_unknown_filesystems_need_the_partition_editor():
    disk = Disk(
        "ata-Raid_1",
        "sda",
        "/dev/sda",
        500 * GiB,
        partitions=[Partition("r1", "sda1", 400 * GiB, 1 << 20, "linux_raid_member")],
    )
    s = survey([disk])
    p, _ = complete()
    plans.set_disk(p, "ata-Raid_1", "replace", s)
    assert "needs_manual" in codes(plans.validate(p, s))
    p["disk"]["layout"] = "manual"
    found = codes(plans.validate(p, s))
    assert "manual_needs_editor" in found and "needs_manual" not in found


def test_the_alpha_takes_only_the_simple_path():
    p, s = complete("windows")
    p["disk"]["layout"] = "alongside"
    assert "later_stage" in codes(plans.validate(p, s, stage="alpha"))
    assert "later_stage" not in codes(plans.validate(p, s, stage="beta"))


def test_shrinking_windows_needs_bitlocker_suspended_a_check_and_a_backup():
    p, s = complete("windows")
    p["disk"]["layout"] = "alongside"
    p["disk"]["shrink"] = {"partition": "p-win", "to": 300 * GiB}
    found = codes(plans.validate(p, s, stage="beta"))
    assert {"bitlocker_not_suspended", "fs_not_checked", "no_backup"} <= set(found)
    p["disk"]["shrink"].update(bitlocker="suspended", checked=True, backup_confirmed=True)
    report = plans.validate(p, s, stage="beta")
    assert report["valid"], report
    assert {c["action"] for c in report["changes"]} == {"keep", "shrink", "create"}


def test_files_on_an_erased_partition_need_a_copy_to_another_disk_first():
    p, s = complete("windows")
    p["migrate"] = [{"source": "p-win", "secret": "entered", "folders": ["Documents"], "size": 1}]
    assert "files_would_be_lost" in codes(plans.validate(p, s, stage="beta"))
    p["migrate"][0]["copy_before_erase"] = "nvme-Demo_SSD_512_0002"  # the same disk
    assert "files_would_be_lost" in codes(plans.validate(p, s, stage="beta"))


def test_free_space_keeps_windows_and_needs_room():
    p, s = complete("windows")
    p["disk"]["layout"] = "free_space"
    report = plans.validate(p, s, stage="beta")
    assert report["valid"], report
    assert all(c["action"] in ("keep", "create") for c in report["changes"])
    full = [
        Disk(
            "ata-Full_1",
            "sda",
            "/dev/sda",
            100 * GiB,
            partitions=[Partition("f1", "sda1", 90 * GiB, 1 << 20, "ext4")],
        )
    ]
    plans.set_disk(p, "ata-Full_1", "free_space", survey(full))
    assert "not_enough_space" in codes(plans.validate(p, survey(full), stage="beta"))


def test_the_cloud_stays_off_and_unknown_fields_are_refused():
    p, s = complete()
    p["privacy"]["cloud"] = "enabled"
    p["install_now"] = True
    assert {"cloud_during_install", "unknown_field"} <= set(codes(plans.validate(p, s)))


def test_a_change_after_confirmation_needs_the_person_to_look_again():
    p, s = complete()
    p["confirmed_by_user"] = True
    plans.set_time(p, "Europe/Warsaw", "pl_PL.UTF-8")
    assert p["confirmed_by_user"] is False


def test_changing_the_disk_asks_for_the_recovery_key_again():
    p, s = complete()
    stick = next(d for d in s["disks"] if d["boot_medium"])
    plans.set_disk(p, stick["id"], "replace", s)
    assert p["disk"]["encryption"] == {"method": "tpm2"}


@pytest.mark.parametrize(
    ("setter", "kwargs"),
    [
        ("set_languages", {"languages": ["Ukrainian"], "keyboards": ["ua"]}),
        ("set_languages", {"languages": [], "keyboards": ["ua"]}),
        ("set_network", {"mode": "bluetooth"}),
        ("set_encryption", {"method": "rot13"}),
        ("set_user", {"full_name": "A", "username": "Root!", "hostname": "pc"}),
        ("set_user", {"full_name": "A", "username": "root", "hostname": "pc"}),
        ("set_user", {"full_name": "A", "username": "gloxi-1000", "hostname": "pc"}),
        ("set_user", {"full_name": "A", "username": "olena", "hostname": "-pc"}),
        ("set_user", {"full_name": " ", "username": "olena", "hostname": "pc"}),
        ("set_time", {"timezone": "Mars/Base", "region": "uk_UA.UTF-8"}),
        ("set_time", {"timezone": "Europe/Kyiv", "region": "uk"}),
    ],
)
def test_bad_values_are_refused_with_a_reason(setter, kwargs):
    p = plans.new_plan()
    before = copy.deepcopy(p)
    with pytest.raises(plans.PlanError) as e:
        plans.SETTERS[setter](p, **kwargs)
    assert str(e.value)
    assert p == before


def test_set_disk_refuses_unknown_disks_and_partitions():
    s = surveyed("windows")
    with pytest.raises(plans.PlanError, match="no disk"):
        plans.set_disk(plans.new_plan(), "nvme-Someone_else", "replace", s)
    with pytest.raises(plans.PlanError, match="no partitions"):
        plans.set_disk(plans.new_plan(), s["disks"][0]["id"], "alongside", s, keep_partitions=["x"])


def test_save_is_private_and_load_reads_it_back(tmp_path):
    p, _ = complete()
    path = tmp_path / "vilna-installer" / "plan.json"
    plans.save(p, path)
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    assert plans.load(path) == p
    assert plans.load(tmp_path / "none.json")["confirmed_by_user"] is False
    path.write_text("[1]")
    with pytest.raises(plans.PlanError):
        plans.load(path)


def test_step_zero_plans_are_read(tmp_path):
    """The first screen (vilna_installer.start) writes this; the plan goes on from it."""
    path = tmp_path / "plan.json"
    path.write_text(
        '{"version": 1, "ui": {"language": "uk", "locale": "uk_UA.UTF-8", '
        '"translated": true, "screen_reader": false}, "assistant": true, '
        '"confirmed_by_user": false}'
    )
    report = plans.validate(plans.load(path), surveyed())
    assert report["problems"] == []
    assert "languages" in report["missing"]


# --- the partition editor of the classic wizard (manual layout) --------------------


def manual(kind="windows"):
    p, s = complete(kind)
    plans.set_disk(p, s["disks"][0]["id"], "manual", s)
    return p, s


def test_manual_layout_next_to_windows_keeps_what_the_person_did_not_choose():
    p, s = manual()
    plans.set_partitions(
        p,
        s,
        [
            {"use": "efi", "partition": "p-efi"},
            {"use": "system", "size": 100 * GiB},
            {"use": "swap", "size": 8 * GiB},
        ],
    )
    report = plans.validate(p, s)
    assert report["valid"], report
    actions = {(c["action"], c.get("name") or c.get("use")) for c in report["changes"]}
    assert ("use", "nvme0n1p1") in actions  # the EFI partition, shared, not erased
    assert ("keep", "nvme0n1p3") in actions  # Windows stays
    assert ("create", "system") in actions and ("create", "swap") in actions
    assert not [c for c in report["changes"] if c["action"] == "erase"]


def test_manual_layout_says_which_partitions_it_erases():
    p, s = manual()
    plans.set_partitions(
        p,
        s,
        [
            {"use": "efi", "partition": "p-efi", "format": True},
            {"use": "system", "partition": "p-win", "format": True},
        ],
    )
    report = plans.validate(p, s)
    assert report["valid"], report
    erased = {c["name"]: c["use"] for c in report["changes"] if c["action"] == "erase"}
    assert erased == {"nvme0n1p1": "efi", "nvme0n1p3": "system"}


@pytest.mark.parametrize(
    ("partitions", "code"),
    [
        ([{"use": "system", "size": 100 * GiB}], "needs_one_efi"),
        ([{"use": "efi", "partition": "p-efi"}], "needs_one_system"),
        (
            [
                {"use": "efi", "partition": "p-efi"},
                {"use": "system", "partition": "p-win"},  # not formatted
            ],
            "must_format",
        ),
        (
            [
                {"use": "efi", "partition": "p-rec"},  # NTFS recovery, not EFI
                {"use": "system", "size": 100 * GiB},
            ],
            "not_efi",
        ),
        (
            [
                {"use": "efi", "partition": "p-efi"},
                {"use": "system", "size": 20 * GiB},
            ],
            "too_small",
        ),
        (
            [
                {"use": "efi", "partition": "p-efi"},
                {"use": "system", "size": 300 * GiB},  # about 131 GiB is free
            ],
            "not_enough_space",
        ),
        (
            [
                {"use": "efi", "partition": "p-efi"},
                {"use": "system", "size": 100 * GiB},
                {"use": "home", "partition": "p-win"},  # BitLocker kept as home
            ],
            "home_unreadable",
        ),
        (
            [
                {"use": "efi", "partition": "p-efi"},
                {"use": "system", "size": 60 * GiB},
                {"use": "home", "partition": "p-efi", "format": True},
            ],
            "partition_twice",
        ),
    ],
)
def test_the_validator_refuses_a_broken_manual_layout(partitions, code):
    p, s = manual()
    plans.set_partitions(p, s, partitions)
    assert code in codes(plans.validate(p, s))


def test_partitions_are_only_for_the_manual_layout_and_this_disk():
    p, s = complete("windows")
    with pytest.raises(plans.PlanError, match="manual"):
        plans.set_partitions(p, s, [{"use": "system", "size": 50 * GiB}])
    p, s = manual()
    with pytest.raises(plans.PlanError, match="no partition"):
        plans.set_partitions(p, s, [{"use": "home", "partition": "sda1"}])
    with pytest.raises(plans.PlanError, match="not both"):
        plans.set_partitions(p, s, [{"use": "home", "partition": "p-win", "size": GiB}])
    with pytest.raises(plans.PlanError, match="use"):
        plans.set_partitions(p, s, [{"use": "boot", "size": GiB}])


def test_choosing_the_disk_again_keeps_the_editor_s_work_only_for_the_same_disk():
    p, s = manual()
    plans.set_partitions(
        p, s, [{"use": "efi", "partition": "p-efi"}, {"use": "system", "size": 100 * GiB}]
    )
    plans.set_disk(p, s["disks"][0]["id"], "manual", s)
    assert len(p["disk"]["partitions"]) == 2
    plans.set_disk(p, s["disks"][0]["id"], "replace", s)
    assert "partitions" not in p["disk"]


def test_gloxi_cannot_choose_partitions():
    assert "set_partitions" not in plans.SETTERS
