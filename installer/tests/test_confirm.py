# SPDX-License-Identifier: GPL-3.0-or-later
"""Step 12 without GTK: the summary, the Herkos screen's checks, disk encryption."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from vilna_mcp_installer import machine as machines
from vilna_mcp_installer import plan as plans

from vilna_installer import confirm, encryption, start, summary
from vilna_installer.vault import Vault

UK = start.Texts("uk")
EN = start.Texts("en")


def full_plan(path: Path, kind: str = "windows", method: str = "tpm2") -> dict:
    """A plan made the way the conversation makes it, with every secret entered."""
    machine = machines.DemoMachine(kind)
    survey = machines.survey(machine.disks())
    target = next(d["id"] for d in survey["disks"] if not d["boot_medium"])
    plan = start.start_plan("uk", False, True)
    plans.set_languages(plan, ["uk_UA.UTF-8", "en_US.UTF-8"], ["ua", "us"])
    plans.set_network(plan, "offline")
    plans.set_disk(plan, target, "replace", survey)
    plans.set_encryption(plan, method)
    if method == "passphrase":
        plans.mark_entered(plan, "passphrase")
    plans.set_user(plan, "Олена Коваль", "olena", "olena-laptop")
    plans.mark_entered(plan, "user")
    plans.set_time(plan, "Europe/Kyiv", "uk_UA.UTF-8")
    plans.save(plan, path)
    return plans.load(path)


# --- the summary ---------------------------------------------------------------------


def test_summary_puts_what_is_lost_first_in_the_persons_language(tmp_path):
    plan = full_plan(tmp_path / "plan.json")
    survey = machines.survey(machines.DemoMachine("windows").disks())
    report = plans.validate(plan, survey)
    s = summary.summarize(plan, report, survey, UK)
    assert s.erases
    assert [c.action for c in s.changes][:4] == ["erase"] * 4
    assert s.changes[-1].action == "create"
    windows = next(c.text for c in s.changes if "nvme0n1p3" in c.text)
    assert windows == "Буде стерто: nvme0n1p3 · 380 ГіБ · Windows, зашифровано BitLocker"
    assert s.changes[-1].text == "Нове: Vilna, 512 ГіБ, зашифровано"
    assert s.disk == "Demo SSD 512 (nvme0n1, 512 ГіБ)"
    # The recovery key is the Herkos screen's own step: it never blocks reaching it.
    assert [p["code"] for p in report["problems"]] == ["recovery_key"]
    assert s.ready and s.problems == []
    lines = {line.key: line.text for line in s.lines}
    assert lines["encryption"] == "Зашифровано, з ключем відновлення"


def test_summary_says_what_blocks_and_what_is_missing():
    plan = start.start_plan("uk", False, True)
    survey = machines.survey(machines.DemoMachine("empty").disks())
    plans.set_disk(plan, "usb-Demo_Stick_0001", "replace", survey)
    report = plans.validate(plan, survey)
    s = summary.summarize(plan, report, survey, UK)
    assert not s.ready
    assert s.problems[0].startswith("Ще не вирішено: Мови й клавіатура")
    assert "З цієї флешки зараз працює Vilna. Виберіть інший диск." in s.problems
    unknown = summary.problem_text({"code": "brand_new", "message": "something"}, EN)
    assert unknown == "The plan cannot be installed yet: something"


def test_summary_knows_the_manual_layouts_changes():
    # The classic wizard's partition editor (item 16) reports «use» and partition roles.
    plan = {"disk": {"encryption": {"method": "none"}}}
    survey = machines.survey(machines.DemoMachine("windows").disks())
    efi = {"action": "use", "use": "efi", "partition": "p-efi", "name": "nvme0n1p1",
           "size": 260 << 20, "holds_data": False}  # fmt: skip
    # The validator's line has no "encrypted": the summary finds it in the survey.
    fmt = {"action": "erase", "use": "system", "partition": "p-win", "name": "nvme0n1p3",
           "size": 380 << 30, "system": "Windows", "holds_data": True}  # fmt: skip
    new = {"action": "create", "use": "swap", "size": 8 << 30}
    texts = [summary.change_text(c, plan, survey, EN).text for c in (efi, fmt, new)]
    assert texts == [
        "Used for the boot partition (EFI) without formatting: nvme0n1p1 · 260 MiB · "
        "the boot partition (EFI)",
        "Formatted for the system, everything on it erased: nvme0n1p3 · 380 GiB · Windows, "
        "encrypted with BitLocker",
        "New partition for swap: 8 GiB",
    ]


def test_every_new_text_is_translated_to_ukrainian():
    catalog = start.catalog("uk")
    assert [t for t in (*summary.TEXTS, *confirm.TEXTS) if t not in catalog] == []


# --- the Herkos screen -----------------------------------------------------------------


def test_the_screen_finds_a_tpm_that_cannot_keep_the_key(tmp_path):
    full_plan(tmp_path / "plan.json")
    machine = machines.DemoMachine("windows")
    ok = confirm.check(tmp_path / "plan.json", machine, EN, tpm=True)
    assert ok.summary.ready
    old = confirm.check(tmp_path / "plan.json", machine, EN, tpm=False)
    assert old.summary.problems == [summary.PROBLEMS["no_tpm"]]


def test_confirm_sets_the_marks_only_after_the_key_was_typed_back(tmp_path):
    path = tmp_path / "plan.json"
    full_plan(path)
    machine = machines.DemoMachine("windows")
    seen = confirm.check(path, machine, EN).plan
    with pytest.raises(confirm.NotConfirmed):
        confirm.confirm(path, machine, seen=seen, key_typed_back=False)
    assert plans.load(path)["confirmed_by_user"] is False
    digest = confirm.confirm(path, machine, seen=seen, key_typed_back=True)
    plan = plans.load(path)
    assert plan["confirmed_by_user"] is True
    assert plan["disk"]["encryption"]["recovery_key_acknowledged"] is True
    assert digest == confirm.sha256(path)
    assert plans.validate(plan, machines.survey(machine.disks()))["valid"]


def test_confirm_refuses_a_plan_that_changed_behind_the_screen(tmp_path):
    path = tmp_path / "plan.json"
    full_plan(path)
    machine = machines.DemoMachine("windows")
    seen = confirm.check(path, machine, EN).plan
    plan = plans.load(path)
    plans.set_user(plan, "Інша Людина", "insha", "insha-pc")  # Gloxi changed it meanwhile
    plans.mark_entered(plan, "user")
    plans.save(plan, path)
    with pytest.raises(confirm.NotConfirmed, match="changed"):
        confirm.confirm(path, machine, seen=seen, key_typed_back=True)
    assert plans.load(path)["confirmed_by_user"] is False


def test_confirm_refuses_when_the_disks_changed(tmp_path):
    path = tmp_path / "plan.json"
    full_plan(path)
    seen = confirm.check(path, machines.DemoMachine("windows"), EN).plan
    other = machines.DemoMachine("windows")
    other._disks = other._disks[:1]  # the USB stick was taken out meanwhile
    with pytest.raises(confirm.NotConfirmed, match="disks_changed"):
        confirm.confirm(path, other, seen=seen, key_typed_back=True)


def test_a_plan_without_encryption_needs_no_key(tmp_path):
    path = tmp_path / "plan.json"
    full_plan(path, "empty", method="none")
    machine = machines.DemoMachine("empty")
    c = confirm.check(path, machine, EN)
    assert c.summary.ready and not c.summary.erases
    assert c.summary.warnings == [summary.PROBLEMS["no_encryption"]]
    confirm.confirm(path, machine, seen=c.plan, key_typed_back=False)
    assert plans.load(path)["confirmed_by_user"] is True


def test_the_answer_between_the_screen_and_the_installer():
    key = encryption.recovery_key()
    sent = confirm.answer(confirm.Outcome(True, "a" * 64, key))
    assert confirm.parse("Gtk-WARNING: noise\n" + sent) == confirm.Outcome(True, "a" * 64, key)
    back = confirm.parse(confirm.answer(confirm.Outcome(False, recovery_key=key)))
    assert back == confirm.Outcome(False, recovery_key=key)
    # A crash, a closed pipe or a forged answer is «back», never «confirmed».
    assert confirm.parse("") == confirm.Outcome(False)
    assert not confirm.parse('{"answer": "confirmed", "plan_sha256": "x"}').confirmed
    forged = json.dumps({"answer": "confirmed", "plan_sha256": "a" * 64, "recovery_key": "1"})
    assert not confirm.parse(forged).confirmed


def test_the_screen_is_its_own_process():
    argv = confirm.command(Path("/run/p.json"), demo="windows", window=True)
    assert argv[1:3] == ["-m", "vilna_installer.confirm_window"]
    assert argv[3:] == ["--plan", "/run/p.json", "--demo", "windows", "--window"]


# --- the vault ---------------------------------------------------------------------------


def test_the_vault_never_shows_its_secrets():
    vault = Vault()
    vault.put("user", "hunter22hunter")
    assert "hunter" not in repr(vault) and "hunter" not in str(vault)
    assert vault.take("user") == "hunter22hunter"
    assert not vault.has("user")
    with pytest.raises(ValueError):
        vault.put("bank", "1234")


# --- encryption ---------------------------------------------------------------------------


def test_the_recovery_key_has_systemds_format():
    key = encryption.recovery_key(lambda n: bytes(range(n)))
    assert key == ("cccbcdce-cfcgchci-cjckclcn-crctcucv-bcbbbdbe-bfbgbhbi-bjbkblbn-brbtbubv")
    assert encryption.RECOVERY_RE.match(encryption.recovery_key())
    assert encryption.recovery_key() != encryption.recovery_key()
    assert encryption.check_group(key, " BRBT-BUBV ", 7)
    assert not encryption.check_group(key, "brbtbubc", 7)
    assert not encryption.check_group(key, "brbtbubv", 8)
    assert encryption.spoken(key).startswith("c c c b c d c e. c f c g")


def test_secrets_reach_programs_on_standard_input_only(tmp_path):
    key = encryption.recovery_key()
    todo = encryption.steps(
        "passphrase",
        crypt_part="/dev/nvme0n1p2",
        crypt_uuid="0b0e8c5a-1111-4222-8333-944455556666",
        root_uuid="7c7c7c7c-1111-4222-8333-944455556666",
        root=str(tmp_path),
        key_file=tmp_path / "k",
    )
    shown = "\n".join(s.shown() for s in todo)
    assert "passphrase" in shown and "< recovery_key" in shown
    assert key not in shown
    calls = []

    def run(argv, input=None, **_):
        calls.append((argv, input))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    (tmp_path / "etc/kernel").mkdir(parents=True)
    (tmp_path / "etc/kernel/cmdline").write_text("quiet splash")
    vault = Vault()
    vault.put("recovery_key", key)
    vault.put("passphrase", "correct horse battery")
    encryption.run_steps(todo, vault.peek, root=tmp_path, key_file=tmp_path / "k", run=run)
    assert all(key not in " ".join(argv) for argv, _ in calls)
    assert all("correct horse" not in " ".join(argv) for argv, _ in calls)
    given = {argv[0] + argv[3] if argv[0] == "keyctl" else argv[0]: i for argv, i in calls if i}
    assert given == {"keyctlsdbootutil": key.encode(), "cryptsetup": b"correct horse battery"}
    assert (tmp_path / "etc/crypttab").read_text() == (
        "cr_root UUID=0b0e8c5a-1111-4222-8333-944455556666 none x-initrd.attach\n"
    )
    assert (tmp_path / "etc/kernel/cmdline").read_text() == (
        "quiet splash root=UUID=7c7c7c7c-1111-4222-8333-944455556666\n"
    )
    assert calls[-3][0][:3] == ["systemd-cryptenroll", f"--unlock-key-file={tmp_path / 'k'}",
                                "--wipe-slot=0"]  # fmt: skip


def test_tpm_steps_seal_the_measurements_aeon_uses(tmp_path):
    todo = encryption.steps(
        "tpm2",
        crypt_part="/dev/sda2",
        crypt_uuid="0b0e8c5a-1111-4222-8333-944455556666",
        root_uuid="7c7c7c7c-1111-4222-8333-944455556666",
        root="/mnt",
        key_file=Path("/run/k"),
    )
    pcrs = next(s for s in todo if s.path == "etc/sysconfig/fde-tools")
    assert pcrs.text == "FDE_SEAL_PCR_LIST=4,5,7,9\n"
    enroll = [s.argv[-2:] for s in todo if "enroll" in s.argv]
    assert enroll == [("--method=recovery-key", "enroll"), ("--method=tpm2", "enroll")]
    assert not any(s.stdin == "passphrase" for s in todo)
    with pytest.raises(ValueError):
        encryption.steps("tpm2", crypt_part="/dev/sda2", crypt_uuid="$(rm -rf /)",
                         root_uuid="x", root="/mnt", key_file=Path("/run/k"))  # fmt: skip


def test_a_failing_step_stops_without_its_secret_in_the_error(tmp_path):
    def run(argv, input=None, **_):
        return subprocess.CompletedProcess(argv, 1, b"", b"No key available with this passphrase.")

    step = encryption.Step("add the passphrase", ("cryptsetup", "luksAddKey"), stdin="passphrase")
    with pytest.raises(encryption.EncryptionError) as e:
        encryption.run_steps([step], {"passphrase": "s3cret-pass"}.get, root=tmp_path,
                             key_file=tmp_path / "k", run=run)  # fmt: skip
    assert "s3cret" not in str(e.value) and "No key available" in str(e.value)


def test_tpm_check(tmp_path):
    dev = tmp_path / "tpmrm0"
    assert encryption.tpm_can_seal(device=dev) is False
    dev.touch()
    new = subprocess.CompletedProcess([], 0, "  commandIndex: 0x192\n", "")
    old = subprocess.CompletedProcess([], 0, "  commandIndex: 0x191\n", "")
    assert encryption.tpm_can_seal(lambda *a, **k: new, dev) is True
    assert encryption.tpm_can_seal(lambda *a, **k: old, dev) is False

    def missing(*_a, **_k):
        raise FileNotFoundError("tpm2_getcap")

    assert encryption.tpm_can_seal(missing, dev) is None


def test_key_file_is_root_only_and_wiped(tmp_path):
    path = encryption.make_key_file(tmp_path / "keys")
    assert path.stat().st_mode & 0o777 == 0o400
    assert len(path.read_bytes()) == encryption.KEY_FILE_BYTES
    encryption.wipe_key_file(path)
    assert not path.exists()


NEEDED = ("systemd-repart", "mkfs.vfat", "mkfs.btrfs", "cryptsetup")


@pytest.mark.skipif(
    any(shutil.which(t) is None for t in NEEDED) or os.geteuid() != 0,
    reason="needs root, systemd-repart and mkfs",
)
def test_repart_makes_luks2_with_the_key_file(tmp_path):
    """The definitions on a disk image file: an ESP and a LUKS2 system partition."""
    defs = tmp_path / "defs"
    defs.mkdir()
    for name, text in encryption.repart_definitions("tpm2").items():
        (defs / name).write_text(text)
    key = encryption.make_key_file(tmp_path / "keys")
    disk = tmp_path / "disk.img"
    with open(disk, "wb") as f:
        f.truncate(3 << 30)
    step = encryption.repart_command(str(disk), defs, key)
    done = subprocess.run([*step.argv, "--offline=yes"], capture_output=True, timeout=600)
    assert done.returncode == 0, done.stderr.decode()
    table = json.loads(subprocess.run(["sfdisk", "-J", str(disk)], capture_output=True).stdout)
    esp, root = table["partitiontable"]["partitions"]
    assert esp["size"] * 512 == 512 << 20
    with open(disk, "rb") as f:
        f.seek(root["start"] * 512)
        assert f.read(6) == b"LUKS\xba\xbe"
    disk.unlink()
