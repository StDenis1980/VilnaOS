# SPDX-License-Identifier: GPL-3.0-or-later
"""Disk encryption of the new system: LUKS2, the recovery key, TPM2 or a passphrase.

image/docs/installer.md, sections 3 (step 8) and 8. The way follows openSUSE
Aeon, whose base Vilna uses (its installer tik and the Aeon encryption
guide), so that updates seal the key again the same way:

1. The engine creates a random key file (1 KiB, in ``/run``, readable by root
   only) and puts it in the kernel keyring as ``cryptenroll``.
2. ``systemd-repart`` lays out the disk and makes the system partition
   LUKS2 with that key file in slot 0 (``Encrypt=key-file``,
   ``repart_definitions``).
3. With the new system mounted, ``steps`` writes ``/etc/crypttab``, the
   kernel command line and the PCR list, installs systemd-boot with
   ``sdbootutil``, and adds the keys that stay: the **recovery key** (the one
   the person saw and typed back on the Herkos screen, through the keyring as
   ``sdbootutil``) and either the **TPM2** seal or the **passphrase**.
4. Slot 0 is wiped, the key file overwritten and removed, the keyring entries
   dropped.

TPM2 is sealed on the **first boot** of the new system, not from the live
system: ``systemd-pcrlock`` predicts the measurements from the event log of
the running boot, and the live boot (shim, GRUB, an ISO) is not the one the
new system makes, so a seal from there keeps no PCR at all (VM test,
15.11.2026). The installer leaves the way Aeon's own first boot takes
(``sdbootutil-enroll.service``): a TPM2 slot bound to this chip only, so
that the first boot opens the disk by itself, slot 0 marked as the
transient "enrollment-key", and its key as a credential that only this TPM
can decrypt. On the first boot sdbootutil seals the key with the real
measurements (``--wipe-slot=tpm2`` drops the chip-only slot), wipes slot 0
and the engine's drop-in removes the credentials. Until then the disk
opens only in this computer.

Secrets never go on a command line: they reach a program on its standard
input only (``Step.stdin`` names the secret), and the log shows the
commands without them.

The recovery key has the format of ``systemd-cryptenroll --recovery-key``:
256 random bits as 64 "modhex" letters in eight groups, typed at the disk
prompt like a passphrase. Modhex letters are the same on every Latin
keyboard layout, so the key can be typed in any of them.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODHEX = "cbdefghijklnrtuv"
KEY_BYTES = 32
GROUP = 8  # letters in a group
RECOVERY_RE = re.compile(rf"^[{MODHEX}]{{{GROUP}}}(-[{MODHEX}]{{{GROUP}}}){{7}}$")

# The name of the opened system partition (/dev/mapper/…), as in Aeon.
MAPPER = "cr_root"
# PCRs the TPM2 seal checks, as Aeon chose them:
#   4 the boot loader and its drivers (updates re-seal it),
#   5 the partition table (it does not change),
#   7 Secure Boot on or off (a change asks for the recovery key),
#   9 the initrd (updates re-seal it).
# Not 0-3: firmware updates, settings and plug-in hardware (an external
# graphics card) change them too often.
PCRS = "4,5,7,9"
# TPM2_PolicyAuthorizeNV (command 0x192), which sealing with updates needs;
# chips older than TPM 2.0 rev. 1.38 lack it.
POLICY_AUTHORIZE_NV = "commandIndex: 0x192"
ESP_SIZE = "512M"  # as the Vilna disk image (obs/images/Vilna, efipartsize)
KEY_FILE_BYTES = 1024
METHODS = ("tpm2", "tpm2-pin", "passphrase", "none")
ENCRYPTED = ("tpm2", "tpm2-pin", "passphrase")
# The disk opens with the TPM: by itself (tpm2), or with a PIN at every start.
WITH_TPM = ("tpm2", "tpm2-pin")
# Keyring entries the engine and sdbootutil create; dropped whatever happens.
KEYRING_ENTRIES = ("cryptenroll", "sdbootutil", "sdbootutil-recovery-pin")
# The first boot's enrollment (sdbootutil-enroll.service, ImportCredential=).
ENROLL_SERVICE = "sdbootutil-enroll.service"
ENROLL_CREDENTIALS = (
    "etc/credstore.encrypted/sdbootutil-enroll.current-pw",
    "etc/credstore.encrypted/sdbootutil-enroll.recovery-pin",
    "etc/credstore/sdbootutil-enroll.tpm2",
)
# tpm2-pin: the PIN itself, which sdbootutil-enroll reads instead of ".tpm2"
# and enrolls with `sdbootutil enroll --method=tpm2+pin`.
ENROLL_PIN_CREDENTIAL = "etc/credstore.encrypted/sdbootutil-enroll.tpm2+pin"
ENROLL_DROPIN = f"etc/systemd/system/{ENROLL_SERVICE}.d/50-vilna-installer.conf"
# The slot repart puts the engine's key file in (Encrypt=key-file).
INSTALLER_SLOT = "0"


# --- the recovery key --------------------------------------------------------------


def recovery_key(random: Callable[[int], bytes] = secrets.token_bytes) -> str:
    """A new recovery key: 64 modhex letters in eight groups of eight."""
    raw = random(KEY_BYTES)
    if len(raw) != KEY_BYTES:
        raise ValueError("the random source gave too few bytes")
    letters = "".join(MODHEX[b >> 4] + MODHEX[b & 15] for b in raw)
    return "-".join(letters[i : i + GROUP] for i in range(0, len(letters), GROUP))


def groups(key: str) -> list[str]:
    return key.split("-")


def normalize(typed: str) -> str:
    """What the person typed, without spaces and dashes, in lower case."""
    return re.sub(r"[\s\-‐‑–—]+", "", typed).lower()


def check_group(key: str, typed: str, index: int) -> bool:
    """The person typed group ``index`` of the key (counted from 0) back correctly."""
    parts = groups(key)
    if not 0 <= index < len(parts):
        return False
    return secrets.compare_digest(normalize(typed), parts[index])


def spoken(key: str) -> str:
    """The key for a screen reader: letter by letter, a pause between groups."""
    return ". ".join(" ".join(group) for group in groups(key))


# --- the TPM ----------------------------------------------------------------------


def tpm_can_seal(
    run: Callable[..., Any] = subprocess.run,
    device: Path = Path("/dev/tpmrm0"),
) -> bool | None:
    """True: a TPM 2.0 that can keep the key across updates; False: none or too old.

    None when it cannot be told (``tpm2_getcap`` is missing): the engine
    checks again before sealing.
    """
    if not device.exists():
        return False
    try:
        out = run(
            ["tpm2_getcap", "commands"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(out, "returncode", 1) != 0:
        return None
    return POLICY_AUTHORIZE_NV in (out.stdout or "")


# --- the layout ---------------------------------------------------------------------


def repart_definitions(
    method: str, copy_files: str | None = None, copy_blocks: str | None = None
) -> dict[str, str]:
    """systemd-repart definitions for «Erase the whole disk»: the ESP and the system.

    What fills the system partition is the engine's choice: ``copy_blocks``
    (a partition of the Vilna disk image, with its btrfs subvolumes and
    snapshots as they are) or ``copy_files`` (a directory). With
    ``copy_blocks`` repart copies the filesystem instead of making one
    (``CopyBlocks=`` and ``Format=`` exclude each other). Encryption adds
    one line either way.
    """
    if method not in METHODS:
        raise ValueError(f"unknown encryption method {method!r}; one of {', '.join(METHODS)}")
    if copy_files and copy_blocks:
        raise ValueError("either copy_files or copy_blocks")
    esp = (
        "[Partition]\n"
        "Type=esp\n"
        "Label=ESP\n"
        "Format=vfat\n"
        f"SizeMinBytes={ESP_SIZE}\n"
        f"SizeMaxBytes={ESP_SIZE}\n"
    )
    # The engine grows the filesystem itself; systemd-growfs-root would fail
    # on the read-only snapshot at the first boot.
    root = "[Partition]\nType=root\nLabel=vilna\nGrowFileSystem=no\n"
    root += f"CopyBlocks={copy_blocks}\n" if copy_blocks else "Format=btrfs\n"
    if method != "none":
        # LUKS2 with the engine's key file in slot 0; the keys that stay come later.
        root += "Encrypt=key-file\n"
    if copy_files:
        root += f"CopyFiles={copy_files}\n"
    return {"00-esp.conf": esp, "10-root.conf": root}


# --- the steps after the layout --------------------------------------------------


@dataclass(frozen=True)
class Step:
    """A command, or a line written to a file of the new system."""

    why: str
    argv: tuple[str, ...] = ()
    # The name of the secret given on standard input: "recovery_key",
    # "passphrase" (from the vault) or "keyfile" (the engine's key file).
    stdin: str | None = None
    # Or a text that is no secret, on standard input.
    stdin_text: str | None = None
    # A file of the new system (relative to its root) and the text it gets.
    path: str | None = None
    text: str | None = None
    append: bool = False
    remove: bool = False  # delete ``path`` instead of writing it
    cwd: str | None = None  # run the command in this directory

    def shown(self) -> str:
        """For the log and the progress screen: never a secret, only its name."""
        if self.path:
            return (
                f"rm /{self.path}"
                if self.remove
                else f"{'>>' if self.append else '>'} /{self.path}"
            )
        line = shlex.join(self.argv)
        return f"{line} < {self.stdin or 'text'}" if self.stdin or self.stdin_text else line


def before_layout() -> list[Step]:
    """Before systemd-repart: the key file goes into the keyring for sdbootutil."""
    return [
        Step(
            "the key of slot 0 for sdbootutil",
            ("keyctl", "padd", "user", "cryptenroll", "@u"),
            stdin="keyfile",
        )
    ]


def repart_command(disk: str, definitions: Path, key_file: Path) -> Step:
    return Step(
        "lay out the disk and encrypt the system partition",
        (
            "systemd-repart",
            "--no-pager",
            "--pretty=no",
            "--empty=force",
            "--dry-run=no",
            f"--definitions={definitions}",
            f"--key-file={key_file}",
            disk,
        ),
    )


# Dracut builds a host-only initrd and sometimes takes the live system's
# kernel command line for it; tik switches that off while it installs.
# Host-only also leaves out virtio_input, so in a VM with a virtio keyboard
# nobody could type the passphrase or the recovery key at boot (VM test,
# 17.11.2026). Later initrds are built on the installed system itself.
DRACUT_OVERRIDE = "etc/dracut.conf.d/99-vilna-installer.conf"
DRACUT_SETTINGS = 'hostonly_cmdline="no"\nadd_drivers+=" virtio_input "\n'


def steps(
    method: str,
    *,
    crypt_part: str | None = None,
    crypt_uuid: str | None = None,
    root_uuid: str,
    root: str,
    key_file: Path | None = None,
) -> list[Step]:
    """After the layout, with the new system mounted at ``root`` (ESP at /boot/efi).

    The boot loader for every method; for an encrypted system also the keys.
    ``crypt_part`` is the LUKS2 partition, ``crypt_uuid`` its UUID and
    ``root_uuid`` the UUID of the filesystem (inside it, when encrypted).
    The engine drops any old ``root=`` from ``/etc/kernel/cmdline`` first.
    """
    if method not in METHODS:
        raise ValueError(f"unknown encryption method {method!r}; one of {', '.join(METHODS)}")
    encrypted = method != "none"
    checks = [("root_uuid", root_uuid)] + ([("crypt_uuid", crypt_uuid)] if encrypted else [])
    for name, value in checks:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", value):
            raise ValueError(f"{name} {value!r} is not a UUID")
    if encrypted and (not crypt_part or key_file is None):
        raise ValueError("an encrypted system needs crypt_part and key_file")
    chroot = ("chroot", root)
    sdb = (*chroot, "sdbootutil", "--esp-path", "/boot/efi")
    if method in WITH_TPM:
        # With tpm2-device in crypttab sdbootutil would predict the PCRs from
        # the live boot's event log and keep none (VM test, 15.11.2026); the
        # first boot predicts them (sdbootutil-enroll).
        sdb = (*sdb, "--disable-predictions")
    out: list[Step] = []
    if encrypted:
        # sdbootutil reads both files to seal and to update the initrd.
        out.append(
            Step(
                "open the system partition at boot",
                path="etc/crypttab",
                text=f"{MAPPER} UUID={crypt_uuid} none x-initrd.attach"
                + (",tpm2-device=auto" if method in WITH_TPM else "")
                + "\n",
            )
        )
    out += [
        Step(
            "the system on the opened partition" if encrypted else "where the system is",
            path="etc/kernel/cmdline",
            text=f" root=UUID={root_uuid}",
            append=True,
        ),
        Step(
            "an initrd without the live system's command line",
            path=DRACUT_OVERRIDE,
            text=DRACUT_SETTINGS,
        ),
    ]
    if encrypted:
        out.append(
            Step(
                "the recovery key for sdbootutil",
                ("keyctl", "padd", "user", "sdbootutil", "@u"),
                stdin="recovery_key",
            )
        )
    out.append(Step("install the boot loader", (*sdb, "--no-variables", "install")))
    if encrypted:
        out.append(Step("add the recovery key", (*sdb, "--method=recovery-key", "enroll")))
    if method in WITH_TPM:
        out += first_boot_tpm2(
            root, crypt_part=str(crypt_part), key_file=key_file, with_pin=method == "tpm2-pin"
        )
    elif method == "passphrase":
        out.append(
            Step(
                "add the passphrase",
                (
                    "cryptsetup",
                    "luksAddKey",
                    f"--key-file={key_file}",
                    "--batch-mode",
                    "--force-password",
                    str(crypt_part),
                ),
                stdin="passphrase",
            )
        )
    # crypttab and the kernel command line are final: the initrd of the new system.
    out.append(Step("build the initrd", (*sdb, "mkinitrd")))
    out.append(Step("the installer's dracut setting goes", path=DRACUT_OVERRIDE, remove=True))
    if encrypted and method not in WITH_TPM:
        out.append(
            Step(
                "remove the installer's key from slot 0",
                (
                    "systemd-cryptenroll",
                    f"--unlock-key-file={key_file}",
                    f"--wipe-slot={INSTALLER_SLOT}",
                    str(crypt_part),
                ),
            )
        )
    return out


def first_boot_tpm2(
    root: str, *, crypt_part: str, key_file: Path | None, with_pin: bool = False
) -> list[Step]:
    """TPM2 for the first boot to seal (see the module's text): what the live system leaves.

    With ``with_pin`` the first boot seals with TPM2 and the person's PIN
    (``--method=tpm2+pin``): the disk then asks for it at every start.
    """
    chroot = ("chroot", root)
    # The new system's own systemd-creds, not the live one: a credential from
    # a newer systemd is "Unknown encryption format" to an older one, and the
    # first boot kept the installer's key (V2 VM test of Build10.19: live
    # systemd 262.2, installed 261.4). The chroot has /dev, so the TPM too.
    encrypt = (*chroot, "systemd-creds", "encrypt", "--with-key=tpm2", "--tpm2-pcrs=")
    current, pin, wanted = ENROLL_CREDENTIALS
    return [
        Step(
            "which measurements the TPM checks",
            path="etc/sysconfig/fde-tools",
            text=f"FDE_SEAL_PCR_LIST={PCRS}\n",
        ),
        Step(
            "open the disk with this TPM until the first boot seals it",
            (
                "systemd-cryptenroll",
                f"--unlock-key-file={key_file}",
                "--tpm2-device=auto",
                "--tpm2-pcrs=",
                crypt_part,
            ),
        ),
        Step(
            "mark the installer's key as the one the first boot removes",
            ("cryptsetup", "token", "import", "--json-file=/dev/stdin", crypt_part),
            stdin_text=f'{{"type":"enrollment-key","keyslots":["{INSTALLER_SLOT}"]}}',
        ),
        Step(
            "a place for the credentials",
            (
                "install",
                "-d",
                "-m",
                "0700",
                f"{root}/etc/credstore.encrypted",
                f"{root}/etc/credstore",
            ),
        ),
        Step(
            "the installer's key, for this TPM only",
            (*encrypt, f"--name={Path(current).name}", "-", f"/{current}"),
            stdin="keyfile",
        ),
        # The recovery key re-authorizes the TPM policy later (sdbootutil's
        # recovery PIN), so the person has one secret, not two.
        Step(
            "the recovery key as the TPM's recovery PIN, for this TPM only",
            (*encrypt, f"--name={Path(pin).name}", "-", f"/{pin}"),
            stdin="recovery_key",
        ),
        (
            Step(
                "the start PIN, for this TPM only",
                (
                    *encrypt,
                    f"--name={Path(ENROLL_PIN_CREDENTIAL).name}",
                    "-",
                    f"/{ENROLL_PIN_CREDENTIAL}",
                ),
                stdin="pin",
            )
            if with_pin
            else Step("seal with the TPM on the first boot", path=wanted, text="1\n")
        ),
        Step(
            "the credentials go once the first boot sealed the key",
            path=ENROLL_DROPIN,
            text=(
                "# Written by the Vilna installer (vilna_installer.encryption).\n"
                "[Unit]\n"
                f"ConditionPathExists=/{current}\n"
                # sdbootutil reads and writes the ESP; without this it ran
                # 0.2 s before /boot/efi was mounted, failed and kept the
                # enrollment key (VM test of milestone V2, Build12.2).
                "RequiresMountsFor=/boot/efi\n"
                "[Service]\n"
                "ExecStartPost=/usr/bin/rm -f "
                + " ".join(f"/{c}" for c in (*ENROLL_CREDENTIALS, ENROLL_PIN_CREDENTIAL))
                + "\n"
            ),
        ),
        Step(
            "seal with the TPM on the first boot", (*chroot, "systemctl", "enable", ENROLL_SERVICE)
        ),
    ]


def forget_keys() -> list[Step]:
    """The keyring entries of this installation, whether it worked or not."""
    return [
        Step("forget the keys in the keyring", ("keyctl", "purge", "user", name))
        for name in KEYRING_ENTRIES
    ]


# Linux keyctl(2), x86_64 and aarch64 numbers.
_SYS_KEYCTL = {"x86_64": 250, "aarch64": 219}
_KEYCTL_JOIN_SESSION_KEYRING = 1
_KEYCTL_LINK = 8
_KEY_SPEC_SESSION_KEYRING = -3
_KEY_SPEC_USER_KEYRING = -4


def own_session_keyring() -> None:
    """A session keyring of root's own, with root's user keyring in it.

    Under sudo the engine inherits the live user's session keyring, which
    root may not link or change keys in: sdbootutil then cannot set the
    permissions and timeout of its keys and does not find ``cryptenroll``
    (VM test, 15.11.2026). A root login has exactly this.
    """
    import ctypes
    import platform

    number = _SYS_KEYCTL.get(platform.machine())
    if number is None:
        raise EncryptionError(f"keyctl is not known on {platform.machine()}")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    if libc.syscall(number, _KEYCTL_JOIN_SESSION_KEYRING, None) < 0:
        raise EncryptionError(f"a keyring of its own: {os.strerror(ctypes.get_errno())}")
    if libc.syscall(number, _KEYCTL_LINK, _KEY_SPEC_USER_KEYRING, _KEY_SPEC_SESSION_KEYRING) < 0:
        raise EncryptionError(f"root's keys in it: {os.strerror(ctypes.get_errno())}")


# --- running them -------------------------------------------------------------------


class EncryptionError(Exception):
    """A step failed; the message names the step, never a secret."""


def make_key_file(directory: Path) -> Path:
    """1 KiB of random text (it can be typed as a passphrase too), root-only."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"vilna-key-{secrets.token_hex(8)}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "wb") as f:
        f.write(base64.b64encode(secrets.token_bytes(KEY_FILE_BYTES))[:KEY_FILE_BYTES])
    return path


def wipe_key_file(path: Path) -> None:
    """Overwrite, then remove (on tmpfs the overwrite is the whole story)."""
    try:
        size = path.stat().st_size
        os.chmod(path, 0o600)
        with open(path, "r+b") as f:
            f.write(b"\0" * size)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    path.unlink(missing_ok=True)


def run_steps(
    todo: list[Step],
    secrets_of: Callable[[str], str | None],
    *,
    root: Path,
    key_file: Path | None,
    run: Callable[..., Any] = subprocess.run,
    log: Callable[[str], None] = lambda _line: None,
) -> None:
    """Run the steps in order; stop at the first that fails."""
    for step in todo:
        log(step.shown())
        if step.path:
            target = root / step.path
            if step.remove:
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if step.append:
                old = target.read_text(encoding="utf-8") if target.exists() else ""
                target.write_text(old.rstrip("\n") + (step.text or "") + "\n", encoding="utf-8")
            else:
                target.write_text(step.text or "", encoding="utf-8")
            continue
        given: bytes | None = step.stdin_text.encode("utf-8") if step.stdin_text else None
        if step.stdin == "keyfile":
            if key_file is None:
                raise EncryptionError(f"{step.why}: there is no key file")
            given = key_file.read_bytes()
        elif step.stdin:
            value = secrets_of(step.stdin)
            if not value:
                raise EncryptionError(f"{step.why}: the {step.stdin} is missing")
            given = value.encode("utf-8")
        extra = {"cwd": step.cwd} if step.cwd else {}
        try:
            done = run(
                list(step.argv),
                input=given,
                capture_output=True,
                timeout=3600,
                check=False,
                **extra,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise EncryptionError(f"{step.why}: {e}") from e
        if done.returncode != 0:
            err = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-6:]
            raise EncryptionError(f"{step.why}: {step.argv[0]} failed: {' / '.join(err)}")
