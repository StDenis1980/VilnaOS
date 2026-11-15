# SPDX-License-Identifier: GPL-3.0-or-later
"""The installation engine on a synthetic computer: what it runs, in which order, with what.

No disk is touched: the commands go to a fake that answers like losetup,
lsblk, blkid and mount would, and «mounting» the new system puts a few
files of the Vilna image into a directory.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest
from vilna_mcp_installer import machine as machines
from vilna_mcp_installer import plan as plans

from vilna_installer import confirm, encryption
from vilna_installer import engine as eng
from vilna_installer.vault import Vault

KEY = encryption.recovery_key()
LOOP = "/dev/loop7"
LINUX = "0fc63daf-8483-4772-8e79-3d69d8477de4"
DISK = "/dev/nvme0n1"
IMAGE_FSTAB = (
    "UUID=1111-2222 / btrfs ro 0 0\n"
    "UUID=1111-2222 /home btrfs subvol=/@/home 0 0\n"
    "UUID=1111-2222 /var btrfs subvol=/@/var 0 0\n"
    "UUID=1111-2222 /.snapshots btrfs subvol=/@/.snapshots 0 0\n"
    "/etc /etc none bind,x-initrd.mount 0 0\n"
    "UUID=AAAA-BBBB /boot/efi vfat utf8,umask=0077 0 2\n"
)


def lsblk(name, children):
    return json.dumps({"blockdevices": [{"name": name, "type": "disk", "children": children}]})


class FakeSystem:
    """Answers commands like the live system would; records every call."""

    def __init__(self, root, fail=None):
        self.root = root
        self.calls = []
        self.fail = fail
        self.tuned = False

    def __call__(self, argv, input=None, text=False, **kw):
        self.calls.append((list(argv), input, kw.get("cwd")))
        out = ""
        prog = argv[0]
        if self.fail and self.fail in " ".join(argv):
            return subprocess.CompletedProcess(
                argv, 1, b"" if not text else "", b"boom" if not text else "boom"
            )
        if prog == "losetup" and "--show" in argv:
            out = LOOP + "\n"
        elif prog == "lsblk" and argv[-1] == LOOP:
            out = lsblk(LOOP, [
                {"name": LOOP + "p1", "type": "part", "parttype": eng.ESP_TYPE, "fstype": "vfat"},
                {"name": LOOP + "p2", "type": "part", "parttype": LINUX, "fstype": "btrfs"},
            ])  # fmt: skip
        elif prog == "lsblk" and argv[-1] == DISK:
            out = lsblk(DISK, [
                {"name": DISK + "p1", "type": "part", "partlabel": "ESP", "uuid": "C0DE-0001"},
                {"name": DISK + "p2", "type": "part", "partlabel": "vilna",
                 "uuid": "0b0e8c5a-1111-4222-8333-944455556666"},
            ])  # fmt: skip
        elif prog == "blkid":
            out = ("7c7c7c7c-1111-4222-8333-944455556666" if self.tuned else "1111-2222") + "\n"
        elif prog == "btrfstune":
            self.tuned = True
        elif prog == "mount" and len(argv) == 3 and argv[2] == str(self.root):
            # The default subvolume of the copied image: snapshot 1.
            (self.root / "etc/kernel").mkdir(parents=True)
            (self.root / "etc/fstab").write_text(IMAGE_FSTAB)
            (self.root / "etc/kernel/cmdline").write_text("root=UUID=1111-2222 quiet splash")
        if text:
            return subprocess.CompletedProcess(argv, 0, out, "")
        return subprocess.CompletedProcess(argv, 0, out.encode(), b"")

    def argvs(self):
        return [a for a, _, _ in self.calls]

    def first(self, *words):
        for i, argv in enumerate(self.argvs()):
            if all(w in argv for w in words):
                return i
        raise AssertionError(f"no call with {words}")


class Machine(machines.DemoMachine):
    def __init__(self):
        super().__init__("windows")
        for d in self._disks:
            if d.id == "nvme-Demo_SSD_512_0002":
                d.path = DISK


@pytest.fixture
def setup(tmp_path):
    machine = Machine()
    survey = machines.survey(machine.disks())
    plan = plans.new_plan()
    plans.set_languages(plan, ["uk_UA.UTF-8", "en_US.UTF-8"], ["ua", "us"])
    plans.set_network(plan, "wifi", "DemoHome", secured=True)
    plans.mark_entered(plan, "network")
    plans.set_disk(plan, "nvme-Demo_SSD_512_0002", "replace", survey=survey)
    plans.set_encryption(plan, "tpm2")
    plans.set_user(plan, "Олена Демо", "olena", "olena-pc")
    plans.mark_entered(plan, "user")
    plans.set_time(plan, "Europe/Kyiv", "uk_UA.UTF-8")
    plans.acknowledge_recovery_key(plan)
    plan["confirmed_by_user"] = True
    path = tmp_path / "vilna-installer" / "plan.json"
    plans.save(plan, path)
    assert plans.validate(plan, survey)["valid"], plans.validate(plan, survey)
    image = tmp_path / "Vilna.raw"
    image.write_bytes(b"\0" * 1024)
    models = tmp_path / "models"
    models.mkdir()
    (models / "Qwen3-4B-Q4_K_M.gguf").write_bytes(b"weights")
    (models / "default.gguf").symlink_to("Qwen3-4B-Q4_K_M.gguf")
    (models / "default.gguf.sha256").write_text("x  Qwen3-4B-Q4_K_M.gguf\n")
    return tmp_path, path, machine, image, models


def make(setup, secrets=None, fail=None, tpm=True, digest=None, **plan_change):
    tmp_path, path, machine, image, models = setup
    if plan_change:
        plan = plans.load(path)
        for key, value in plan_change.items():
            plan["disk"][key] = value
        plans.save(plan, path)
    work = tmp_path / "work"
    system = FakeSystem(work / "root", fail=fail)
    secrets = {"recovery_key": KEY, "user": "correct horse"} if secrets is None else secrets
    request = eng.Request(path, digest or confirm.sha256(path), secrets, None)
    progress = eng.Progress(path.with_name("progress.json"))
    lines = []
    engine = eng.Engine(
        request,
        image=image,
        run=system,
        machine=machine,
        progress=progress,
        work=work,
        model_dir=models,
        tpm=lambda: tpm,
        log=lines.append,
    )
    return engine, system, progress, lines


def test_the_whole_install_with_tpm2(setup):
    engine, system, progress, lines = make(setup)
    engine.install()
    calls = system.argvs()
    # The image fills the system partition block by block, inside LUKS2.
    defs = (engine.work / "repart.d" / "10-root.conf").read_text()
    assert f"CopyBlocks={LOOP}p2" in defs and "Encrypt=key-file" in defs
    assert "Format=" not in defs and "GrowFileSystem=no" in defs
    repart = calls[system.first("systemd-repart")]
    assert repart[-1] == DISK and any(a.startswith("--key-file=") for a in repart)
    # Order: layout, open, grow, model, account, boot loader, keys, seal.
    order = [
        system.first("wipefs", "--all", DISK),
        system.first("systemd-repart"),
        system.first("cryptsetup", "open"),
        system.first("btrfs", "resize", "max", str(engine.root)),
        system.first("cp", "-a"),
        system.first("sha256sum"),
        system.first("useradd"),
        system.first("chpasswd"),
        system.first("sdbootutil", "install"),
        system.first("--method=recovery-key", "enroll"),
        # TPM2 is sealed on the first boot: here a slot for this chip only,
        # slot 0 marked for the first boot to remove, its key for this TPM.
        system.first("systemd-cryptenroll", "--tpm2-device=auto", "--tpm2-pcrs="),
        system.first("cryptsetup", "token", "import"),
        system.first("systemd-creds", "encrypt", "--name=sdbootutil-enroll.current-pw"),
        system.first("systemd-creds", "encrypt", "--name=sdbootutil-enroll.recovery-pin"),
        system.first("systemctl", "enable", encryption.ENROLL_SERVICE),
        system.first("mkinitrd"),
        system.first("btrfs", "property", "set", "-ts", str(engine.root), "ro", "true"),
        system.first("sync"),
        system.first("keyctl", "purge", "user", "sdbootutil-recovery-pin"),
    ]
    assert order == sorted(order)
    assert not any("--method=tpm2" in a for a in calls)
    assert not any("--wipe-slot=0" in a for a in calls)  # the first boot wipes it
    _argv, token, _ = system.calls[system.first("cryptsetup", "token", "import")]
    assert json.loads(token) == {"type": "enrollment-key", "keyslots": ["0"]}
    _argv, pin, _ = system.calls[system.first("--name=sdbootutil-enroll.recovery-pin")]
    assert pin == KEY.encode()
    # The model is checked where it was copied to.
    _argv, _input, cwd = system.calls[system.first("sha256sum")]
    assert cwd.startswith(str(engine.root)) and cwd.endswith("/models")
    root = engine.root
    assert (root / "etc/kernel/cmdline").read_text() == (
        "quiet splash root=UUID=7c7c7c7c-1111-4222-8333-944455556666\n"
    )
    fstab = (root / "etc/fstab").read_text()
    assert "UUID=C0DE-0001 /boot/efi" in fstab and "1111-2222" not in fstab
    assert "UUID=7c7c7c7c-1111-4222-8333-944455556666 /home" in fstab
    # The image is let go before its copy, with the same UUID, is mounted.
    assert (
        system.first("losetup", "--detach")
        < system.first("btrfstune")
        < system.first("mount", str(engine.root))
    )
    crypttab = (root / "etc/crypttab").read_text()
    assert crypttab.startswith("cr_root UUID=0b0e8c5a")
    assert crypttab.endswith(" none x-initrd.attach,tpm2-device=auto\n")
    assert (root / "etc/credstore/sdbootutil-enroll.tpm2").read_text() == "1\n"
    dropin = (root / encryption.ENROLL_DROPIN).read_text()
    assert "ConditionPathExists=/etc/credstore.encrypted/sdbootutil-enroll.current-pw" in dropin
    assert "ExecStartPost=/usr/bin/rm -f /etc/credstore.encrypted/" in dropin
    assert (root / "etc/sysconfig/fde-tools").read_text() == "FDE_SEAL_PCR_LIST=4,5,7,9\n"
    assert not (root / encryption.DRACUT_OVERRIDE).exists()
    assert (root / "etc/locale.conf").read_text() == "LANG=uk_UA.UTF-8\n"
    assert '"XkbLayout" "ua,us"' in (root / "etc/X11/xorg.conf.d/00-keyboard.conf").read_text()
    assert (root / "etc/vconsole.conf").read_text() == "KEYMAP=us\n"
    assert (root / "etc/hostname").read_text() == "olena-pc\n"
    assert str((root / "etc/localtime").readlink()) == "../usr/share/zoneinfo/Europe/Kyiv"
    assert (root / "home/olena/.config/gnome-initial-setup-done").exists()
    assert (root / "etc/selinux/.autorelabel").exists()
    # Subvolumes of the image's fstab, not / and /etc.
    mounts = [a for a in calls if a[0] == "mount" and a[1:2] == ["-o"]]
    assert [a[2] for a in mounts] == ["subvol=/@/home", "subvol=/@/var", "subvol=/@/.snapshots"]
    # Everything opened is closed again, the key file is gone.
    assert system.first("cryptsetup", "close") > system.first("umount")
    assert not list((engine.work / "keys").iterdir())
    data = eng.read_progress(progress.path)
    assert data["state"] == "done" and data["percent"] == 100


def test_secrets_reach_programs_only_on_standard_input(setup):
    engine, system, progress, lines = make(setup)
    engine.install()
    for argv, _given, _ in system.calls:
        joined = " ".join(argv)
        assert KEY not in joined and "correct horse" not in joined
    given = {a[0] if a[0] != "chroot" else a[2]: i for a, i, _ in system.calls if i}
    assert given["chpasswd"] == b"olena:correct horse\n"
    assert KEY.encode() in given.values()
    text = "\n".join(lines) + progress.path.read_text()
    assert KEY not in text and "correct horse" not in text


def test_a_passphrase_install_adds_it_and_builds_the_initrd(setup):
    tmp_path, path, *_ = setup
    plan = plans.load(path)
    plans.set_encryption(plan, "passphrase")
    plans.mark_entered(plan, "passphrase")
    plans.acknowledge_recovery_key(plan)
    plan["confirmed_by_user"] = True
    plans.save(plan, path)
    engine, system, *_ = make(
        setup, {"recovery_key": KEY, "user": "pw", "passphrase": "долгий шлях"}
    )
    engine.install()
    add = system.calls[system.first("luksAddKey")]
    assert add[1] == "долгий шлях".encode() and add[0][-1] == DISK + "p2"
    assert system.first("mkinitrd") > system.first("luksAddKey")
    assert system.first("--wipe-slot=0") > system.first("mkinitrd")
    assert "tpm2-device" not in (engine.root / "etc/crypttab").read_text()
    assert not any("--method=tpm2" in a for a in system.argvs())


def test_without_encryption_no_luks_and_no_keys(setup):
    tmp_path, path, *_ = setup
    plan = plans.load(path)
    plans.set_encryption(plan, "none")
    plan["confirmed_by_user"] = True
    plans.save(plan, path)
    engine, system, *_ = make(setup, {"user": "pw"})
    engine.install()
    calls = " | ".join(" ".join(a) for a in system.argvs())
    assert "cryptsetup" not in calls and "keyctl" not in calls and "--key-file" not in calls
    assert "Encrypt=" not in (engine.work / "repart.d" / "10-root.conf").read_text()
    assert f"mount {DISK}p2 {engine.root}" in calls
    assert "mkinitrd" in calls


@pytest.mark.parametrize(
    "change, secrets, tpm, why",
    [
        ({}, None, False, "no TPM 2.0"),
        ({}, {"user": "pw"}, True, "recovery key"),
        ({"layout": "manual"}, None, True, "Erase the whole disk"),
    ],
)
def test_nothing_is_written_when_the_plan_cannot_be_installed(setup, change, secrets, tpm, why):
    engine, system, progress, _ = make(setup, secrets, tpm=tpm, **change)
    with pytest.raises(eng.EngineError) as e:
        engine.install()
    assert why in str(e.value) and not e.value.touched
    assert not any(a[0] in ("systemd-repart", "losetup", "keyctl") for a in system.argvs())
    data = eng.read_progress(progress.path)
    assert data["state"] == "failed" and data["disk_changed"] is False


def test_a_plan_changed_after_the_herkos_screen_is_refused(setup):
    engine, system, *_ = make(setup, digest="0" * 64)
    with pytest.raises(eng.EngineError, match="not the one confirmed"):
        engine.install()
    assert system.calls == []


def test_the_stick_vilna_runs_from_is_refused(setup):
    plan = plans.new_plan()
    plan["disk"] = {"target": "usb-Demo_Stick_0003", "layout": "replace"}
    survey = machines.survey(Machine().disks())
    stick = next(d for d in survey["disks"] if d["boot_medium"])
    plan["disk"]["target"] = stick["id"]
    problems = eng.preflight(plan, survey, digest="a", expected="a", tpm=True, secrets={},
                             image=setup[3])  # fmt: skip
    assert "the one Vilna runs from" in " ".join(problems)


def test_a_failed_step_says_where_and_closes_everything(setup):
    engine, system, progress, _ = make(setup, fail="--tpm2-pcrs=")
    with pytest.raises(eng.EngineError) as e:
        engine.install()
    assert "open the disk with this TPM" in str(e.value) and e.value.touched
    assert system.first("cryptsetup", "close") > system.first("--tpm2-pcrs=")
    # The keys in root's keyring go after a failure too.
    for name in encryption.KEYRING_ENTRIES:
        assert system.first("keyctl", "purge", "user", name) > system.first("cryptsetup", "close")
    assert not list((engine.work / "keys").iterdir())
    assert eng.read_progress(progress.path)["disk_changed"] is True


def test_the_request_from_the_window_hands_each_secret_over_once(tmp_path):
    vault = Vault()
    vault.put("recovery_key", KEY)
    vault.put("user", "pw")
    vault.put("network", "wifi-pass")
    line = eng.request_line(tmp_path / "plan.json", "a" * 64, vault, 1000)
    request = eng.Request.parse(line)
    assert request.secrets == {"recovery_key": KEY, "user": "pw"}
    assert request.owner == 1000
    assert not vault.has("recovery_key") and vault.has("network")  # NM has the Wi-Fi password
    assert KEY not in repr(request)
    for bad in ("", "[]", '{"plan": "rel", "plan_sha256": "' + "a" * 64 + '"}',
                '{"plan": "/p", "plan_sha256": "nope"}'):  # fmt: skip
        with pytest.raises(eng.EngineError):
            eng.Request.parse(bad)


def test_small_parsers():
    assert eng.cmdline_without_root("root=UUID=x quiet  splash\n") == "quiet splash\n"
    assert (
        eng.subvolume_mounts(
            "UUID=x /srv/../etc btrfs subvol=/@/x 0 0\n#UUID=x /a btrfs subvol=a\n"
        )
        == []
    )
    assert eng.fstab_with_esp("UUID=OLD /boot/efi vfat umask=0077 0 2", "NEW") == (
        "UUID=NEW /boot/efi vfat umask=0077 0 2\n"
    )
    files = eng.system_files({"locale": {"languages": ["pl_PL.UTF-8"], "region": "de_DE.UTF-8"}})
    assert "LC_TIME=de_DE.UTF-8" in files["etc/locale.conf"] and "etc/hostname" not in files


def test_the_wifi_profile_of_the_live_session(tmp_path):
    (tmp_path / "Home.nmconnection").write_text("[wifi]\nssid=DemoHome\n")
    (tmp_path / "Other.nmconnection").write_text("[wifi]\nssid=DemoHomeX\n")
    assert eng.wifi_connection("DemoHome", tmp_path).name == "Home.nmconnection"
    assert eng.wifi_connection("Nope", tmp_path) is None


TOOLS = ("systemd-repart", "mkfs.btrfs", "mkfs.vfat", "btrfs", "btrfstune", "losetup", "sfdisk")


@pytest.mark.skipif(
    any(shutil.which(t) is None for t in TOOLS) or os.geteuid() != 0,
    reason="needs root, systemd-repart, btrfs-progs and loop devices",
)
def test_copy_blocks_keeps_the_images_subvolumes_and_snapshot(tmp_path):
    """The real tools on files: the image's btrfs, copied by repart, grows and keeps its layout."""

    def sh(*argv, **kw):
        return subprocess.run(argv, check=True, capture_output=True, text=True, **kw).stdout.strip()

    mib = 1 << 20
    image = tmp_path / "Vilna.raw"
    with open(image, "wb") as f:
        f.truncate(1200 * mib)
    sh("sfdisk", "-q", str(image), input="label: gpt\nsize=64M, type=U\ntype=L\n")
    table = json.loads(sh("sfdisk", "-J", str(image)))["partitiontable"]["partitions"]
    start, size = table[1]["start"] * 512, table[1]["size"] * 512
    # Without partition scanning (containers), a loop device on the partition itself.
    src = sh("losetup", "--find", "--show", "-o", str(start), "--sizelimit", str(size), str(image))
    mnt = tmp_path / "m"
    mnt.mkdir()
    try:
        sh("mkfs.btrfs", "-q", "-f", src)
        sh("mount", src, str(mnt))
        for sub in ("@", "@/.snapshots", "@/home"):
            sh("btrfs", "-q", "subvolume", "create", str(mnt / sub))
        (mnt / "@/.snapshots/1").mkdir()
        snap = mnt / "@/.snapshots/1/snapshot"
        sh("btrfs", "-q", "subvolume", "create", str(snap))
        sh("btrfs", "-q", "subvolume", "create", str(snap / "etc"))
        (snap / "etc/fstab").write_text("UUID=x /home btrfs subvol=/@/home 0 0\n")
        (mnt / "@/home/marker").write_text("hello")
        snap_id = sh("btrfs", "inspect-internal", "rootid", str(snap))
        sh("btrfs", "subvolume", "set-default", snap_id, str(mnt))
        sh("btrfs", "property", "set", "-ts", str(snap), "ro", "true")
        sh("umount", str(mnt))
        sh("losetup", "--detach", src)
        src = sh("losetup", "--find", "--show", "--read-only", "-o", str(start), "--sizelimit",
                 str(size), str(image))  # fmt: skip
        defs = tmp_path / "defs"
        defs.mkdir()
        for name, text in encryption.repart_definitions("none", copy_blocks=src).items():
            (defs / name).write_text(text)
        disk = tmp_path / "disk.raw"
        with open(disk, "wb") as f:
            f.truncate(4096 * mib)
        sh("systemd-repart", "--no-pager", "--pretty=no", "--empty=force", "--dry-run=no",
           "--offline=yes", f"--definitions={defs}", str(disk))  # fmt: skip
        sh("losetup", "--detach", src)
        parts = json.loads(sh("sfdisk", "-J", str(disk)))["partitiontable"]["partitions"]
        root = next(p for p in parts if p.get("name") == "vilna")
        src = sh("losetup", "--find", "--show", "-o", str(root["start"] * 512), "--sizelimit",
                 str(root["size"] * 512), str(disk))  # fmt: skip
        before = sh("blkid", "-s", "UUID", "-o", "value", src)
        sh("btrfstune", "-f", "-m", src)
        assert sh("blkid", "-s", "UUID", "-o", "value", src) != before
        sh("mount", src, str(mnt))
        assert sh("btrfs", "property", "get", "-ts", str(mnt), "ro") == "ro=true"
        assert (mnt / "etc/fstab").exists()  # the default subvolume is snapshot 1
        sh("btrfs", "property", "set", "-ts", str(mnt), "ro", "false")
        sh("btrfs", "filesystem", "resize", "max", str(mnt))
        assert int(sh("stat", "-f", "-c", "%b", str(mnt))) * 4096 > 3000 * mib
        for subvol, point in eng.subvolume_mounts((mnt / "etc/fstab").read_text()):
            (mnt / point.lstrip("/")).mkdir(exist_ok=True)
            sh("mount", "-o", f"subvol={subvol}", src, str(mnt) + point)
        assert (mnt / "home/marker").read_text() == "hello"
    finally:
        subprocess.run(["umount", "--recursive", str(mnt)], capture_output=True)
        subprocess.run(["losetup", "--detach", src], capture_output=True)


def test_the_image_is_found_where_obs_packages_it(tmp_path, monkeypatch):
    monkeypatch.delenv("VILNA_INSTALLER_IMAGE", raising=False)
    assert eng.find_image(tmp_path / "none") == tmp_path / "none/tik-osimage-Vilna.raw"
    (tmp_path / "tik-osimage-Vilna.20261115.raw").write_bytes(b"")
    (tmp_path / "tik-osimage-Aeon.1.raw").write_bytes(b"")
    assert eng.find_image(tmp_path) == tmp_path / "tik-osimage-Vilna.20261115.raw"
    monkeypatch.setenv("VILNA_INSTALLER_IMAGE", "/srv/Vilna.raw")
    assert eng.find_image(tmp_path) == eng.Path("/srv/Vilna.raw")


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root: a mount namespace and a keyring")
def test_the_engine_has_mounts_and_keys_of_its_own():
    code = (
        "import os, subprocess\n"
        "before = os.readlink('/proc/self/ns/mnt')\n"
        "from vilna_installer import engine\n"
        "engine.isolate()\n"
        "assert os.readlink('/proc/self/ns/mnt') != before\n"
        "keys = subprocess.run(['cat', '/proc/keys'], capture_output=True, text=True).stdout\n"
        "assert any(' _ses: 1' in line for line in keys.splitlines())\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
