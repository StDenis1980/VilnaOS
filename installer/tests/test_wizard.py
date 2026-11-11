# SPDX-License-Identifier: GPL-3.0-or-later
"""The classic wizard without GTK: it fills the same plan, with no model, on synthetic computers."""

import re
from pathlib import Path

import pytest
from vilna_mcp_installer import plan as plans
from vilna_mcp_installer.machine import DemoMachine

from vilna_installer import conversation as conv
from vilna_installer import start
from vilna_installer import wizard as wiz

SRC = Path(__file__).resolve().parents[1] / "src" / "vilna_installer"
GiB = wiz.GiB


def new_wizard(tmp_path, kind="empty", language="uk"):
    path = tmp_path / "plan.json"
    start.write_plan(start.start_plan(language, False, False), path)
    return wiz.Wizard(path, DemoMachine(kind))


def walk(w, layout="replace"):
    """The person goes through every step the usual way."""
    assert w.set_languages(["uk_UA.UTF-8", "en_US.UTF-8"], None) is None
    assert w.set_network("offline") is None
    assert w.set_disk(w.disks()[0]["id"], layout) is None
    assert w.set_encryption("tpm2") is None
    assert w.set_user("Олена Демо", "olena", "olena-vilna") is None
    w.mark_entered("user")
    zone, region = w.suggested_time()
    assert w.set_time(zone, region) is None
    w.seen.add("privacy")


def test_the_simple_path_ends_with_nothing_left_but_the_herkos_screen(tmp_path):
    w = new_wizard(tmp_path)
    assert w.first_open() == "languages"
    walk(w)
    report = w.report()
    # The recovery key is saved on the Herkos screen that follows (alpha item 17).
    assert [p["code"] for p in report["problems"]] == ["recovery_key"]
    assert w.to_do(report) == []
    assert all(w.done(key) for key in wiz.STEPS)
    assert w.first_open() == "summary"
    plan = w.plan
    assert plan["assistant"] is False and plan["confirmed_by_user"] is False
    assert plan["locale"]["timezone"] == "Europe/Kyiv"
    assert plan["user"]["password"] == plans.ENTERED


def test_the_wizard_says_what_is_missing_and_where(tmp_path):
    w = new_wizard(tmp_path)
    missing = {row["field"] for row in w.to_do() if row["code"] == "missing"}
    assert missing == {"languages", "network", "disk", "encryption", "user", "time"}
    assert w.first_open() == "languages"
    w.set_languages(["uk_UA.UTF-8"], None)
    assert w.first_open() == "network"


def test_secrets_are_markers_only(tmp_path):
    w = new_wizard(tmp_path)
    walk(w)
    text = w.plan_path.read_text(encoding="utf-8")
    assert "entered" in text
    assert plans.validate(w.plan, w.survey())["problems"][0]["code"] != "secret_in_plan"


def test_the_values_are_checked_before_the_plan_takes_them(tmp_path):
    w = new_wizard(tmp_path)
    assert w.set_user("", "olena", "pc") == wiz.NAME_NEEDED
    assert w.set_user("Olena", "Olena", "pc") == wiz.BAD_USERNAME
    assert w.set_user("Olena", "root", "pc") == wiz.TAKEN_USERNAME
    assert w.set_user("Olena", "olena", "-pc") == wiz.BAD_HOSTNAME
    assert w.set_languages([], None) == wiz.CHOOSE
    assert "unknown time zone" in w.set_time("Mars/Olympus", "uk_UA.UTF-8")
    assert "user" not in w.plan


def test_the_usb_stick_is_never_offered(tmp_path):
    w = new_wizard(tmp_path)
    assert [d["id"] for d in w.disks()] == ["nvme-Demo_SSD_512_0001"]


@pytest.mark.parametrize(
    ("name", "login"),
    [
        ("Олена Ковальчук", "olena"),
        ("Юрій Щербак", "iurii"),
        ("Łukasz Żółw", "lukasz"),
        ("José", "jose"),
        ("Їжак", "izhak"),
        ("123", ""),  # a login name starts with a letter
        ("Root", ""),  # the system has it
        ("", ""),
    ],
)
def test_a_login_name_is_suggested_from_the_name(name, login):
    assert wiz.suggest_username(name) == login


def test_keyboards_follow_the_languages_with_a_latin_one_always_there():
    assert wiz.keyboards_for(["uk_UA.UTF-8"]) == ["ua", "us"]
    assert wiz.keyboards_for(["uk_UA.UTF-8", "pl_PL.UTF-8"]) == ["ua", "pl"]
    assert wiz.keyboards_for(["de_DE.UTF-8"], "ua") == ["de", "ua"]
    assert wiz.keyboards_for(["pt_BR.UTF-8"]) == ["us"]


def test_replacing_a_disk_with_windows_says_what_is_lost(tmp_path):
    w = new_wizard(tmp_path, "windows")
    disk = w.disks()[0]
    assert wiz.erased_by_replace(disk) == "Windows"
    _ = start.Texts("uk")
    assert "BitLocker" in wiz.disk_subtitle(disk, _)
    walk(w)
    erased = [c for c in w.report()["changes"] if c["action"] == "erase"]
    lines = [wiz.change_text(c, _) for c in erased]
    assert any("nvme0n1p3" in line and "Windows" in line and "стерто" in line for line in lines)


def test_the_partition_editor_suggests_a_layout_next_to_windows(tmp_path):
    w = new_wizard(tmp_path, "windows")
    walk(w, layout="manual")
    assert w.done("disk") is False
    assert "manual_needs_editor" in [p["code"] for p in w.to_do()]
    disk = w.target()
    rows = wiz.editor_suggest(disk)
    efi = [r for r in rows if r.use == "efi"]
    assert len(efi) == 1 and efi[0].partition == "p-efi"  # the EFI partition already there
    system = next(r for r in rows if r.use == "system")
    assert system.partition is None and system.size >= 100 * GiB
    assert w.set_partitions(rows) is None
    report = w.report()
    assert w.to_do(report) == []
    assert not [c for c in report["changes"] if c["action"] == "erase"]  # Windows stays
    assert w.done("disk")
    # Shown again, the editor starts from what was saved.
    again = wiz.editor_rows(w.plan, disk)
    assert [(r.use, r.partition) for r in again if r.use] == [("efi", "p-efi"), ("system", None)]


def test_the_partition_editor_on_an_empty_disk_makes_an_efi_partition(tmp_path):
    w = new_wizard(tmp_path)
    walk(w, layout="manual")
    rows = wiz.editor_suggest(w.target())
    assert [(r.use, r.partition is None) for r in rows] == [("efi", True), ("system", True)]
    assert w.set_partitions(rows) is None
    assert w.to_do() == []


def test_the_system_partition_is_always_formatted_by_the_editor():
    rows = [wiz.EditorRow("system", "p-win", False), wiz.EditorRow(None, "p-rec", True)]
    assert wiz.editor_partitions(rows) == [{"use": "system", "partition": "p-win", "format": True}]


def test_a_broken_layout_is_explained_in_the_person_s_language(tmp_path):
    w = new_wizard(tmp_path, "windows")
    walk(w, layout="manual")
    assert w.set_partitions([wiz.EditorRow("system", None, True, 50 * GiB)]) is None
    _ = start.Texts("uk")
    texts = [wiz.problem_text(p, _) for p in w.to_do()]
    assert "Виберіть рівно один системний розділ EFI." in texts


def test_every_problem_the_wizard_can_meet_has_words():
    source = Path(plans.__file__).read_text(encoding="utf-8")
    codes = set(re.findall(r'r\.error\(\s*f?"([a-z_]+)', source))
    # Codes for parts of the plan the wizard never writes, or the Herkos screen settles.
    other = {
        "not_a_plan",
        "version",
        "unknown_field",
        "secret_in_plan",
        "bad_languages",
        "bad_keyboards",
        "bad_timezone",
        "bad_region",
        "cloud_during_install",
        "bad_layout",
        "keep_and_erase",
        "replace_keeps",
        "alongside_needs_shrink",
        "no_partition",
        "bad_shrink",
        "bitlocker_not_suspended",
        "fs_not_checked",
        "no_backup",
        "migrate_without_disk",
        "bad_migrate",
        "no_source",
        "locked_source",
        "files_would_be_lost",
        "no_room_for_copy",
        "bad_partitions",
        "needs_one_",
        "one_",
        "removable",
        "bad_",
    }
    assert codes - set(wiz.PROBLEM_TEXT) - wiz.LATER_SCREEN - other == set()


@pytest.mark.parametrize("code", ["uk"])
def test_every_new_text_is_in_ukrainian(code):
    catalog = start.catalog(code)
    assert [t for t in wiz.TEXTS if t not in catalog] == []


def test_the_window_uses_only_known_texts():
    window = (SRC / "wizard_window.py").read_text(encoding="utf-8")
    for module, texts in ((wiz, wiz.TEXTS), (conv, conv.TEXTS), (start, start.TEXTS)):
        alias = {wiz: "wiz", conv: "conv", start: "start"}[module]
        names = set(re.findall(rf"_\({alias}\.([A-Z_]+)\)", window))
        assert names, alias
        for name in names:
            assert getattr(module, name) in texts, f"{alias}.{name}"
