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
METHODS = ("tpm2", "passphrase", "none")


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


def repart_definitions(method: str, copy_files: str | None = None) -> dict[str, str]:
    """systemd-repart definitions for «Erase the whole disk»: the ESP and the system.

    ``copy_files`` is what fills the system partition (``CopyFiles=``); the
    engine that writes the image chooses it. Encryption adds one line.
    """
    if method not in METHODS:
        raise ValueError(f"unknown encryption method {method!r}; one of {', '.join(METHODS)}")
    esp = (
        "[Partition]\n"
        "Type=esp\n"
        "Label=ESP\n"
        "Format=vfat\n"
        f"SizeMinBytes={ESP_SIZE}\n"
        f"SizeMaxBytes={ESP_SIZE}\n"
    )
    root = "[Partition]\nType=root\nLabel=vilna\nFormat=btrfs\n"
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
    # A file of the new system (relative to its root) and the text it gets.
    path: str | None = None
    text: str | None = None
    append: bool = False

    def shown(self) -> str:
        """For the log and the progress screen: never a secret, only its name."""
        if self.path:
            return f"{'>>' if self.append else '>'} /{self.path}"
        line = shlex.join(self.argv)
        return f"{line} < {self.stdin}" if self.stdin else line


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


def steps(
    method: str,
    *,
    crypt_part: str,
    crypt_uuid: str,
    root_uuid: str,
    root: str,
    key_file: Path,
) -> list[Step]:
    """After the layout, with the new system mounted at ``root`` (ESP at /boot/efi).

    ``crypt_part`` is the LUKS2 partition, ``crypt_uuid`` its UUID and
    ``root_uuid`` the UUID of the filesystem inside it.
    """
    if method not in ("tpm2", "passphrase"):
        raise ValueError("steps are only for an encrypted system")
    for name, value in (("crypt_uuid", crypt_uuid), ("root_uuid", root_uuid)):
        if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", value):
            raise ValueError(f"{name} {value!r} is not a UUID")
    chroot = ("chroot", root)
    sdb = (*chroot, "sdbootutil", "--esp-path", "/boot/efi")
    out = [
        # sdbootutil reads both files to seal and to update the initrd.
        Step(
            "open the system partition at boot",
            path="etc/crypttab",
            text=f"{MAPPER} UUID={crypt_uuid} none x-initrd.attach\n",
        ),
        Step(
            "the system on the opened partition",
            path="etc/kernel/cmdline",
            text=f" root=UUID={root_uuid}",
            append=True,
        ),
        Step(
            "the recovery key for sdbootutil",
            ("keyctl", "padd", "user", "sdbootutil", "@u"),
            stdin="recovery_key",
        ),
        Step("install the boot loader", (*sdb, "--no-variables", "install")),
        Step("add the recovery key", (*sdb, "--method=recovery-key", "enroll")),
    ]
    if method == "tpm2":
        out += [
            Step(
                "which measurements the TPM checks",
                path="etc/sysconfig/fde-tools",
                text=f"FDE_SEAL_PCR_LIST={PCRS}\n",
            ),
            Step("seal the key in the TPM", (*sdb, "--method=tpm2", "enroll")),
        ]
    else:
        out += [
            Step(
                "add the passphrase",
                (
                    "cryptsetup",
                    "luksAddKey",
                    f"--key-file={key_file}",
                    "--batch-mode",
                    "--force-password",
                    crypt_part,
                ),
                stdin="passphrase",
            ),
            # Without a TPM nothing above built the initrd.
            Step("build the initrd", (*sdb, "mkinitrd")),
        ]
    out += [
        Step(
            "remove the installer's key from slot 0",
            ("systemd-cryptenroll", f"--unlock-key-file={key_file}", "--wipe-slot=0", crypt_part),
        ),
        Step("forget the keys in the keyring", ("keyctl", "purge", "user", "cryptenroll")),
        Step("forget the keys in the keyring", ("keyctl", "purge", "user", "sdbootutil")),
    ]
    return out


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
    key_file: Path,
    run: Callable[..., Any] = subprocess.run,
    log: Callable[[str], None] = lambda _line: None,
) -> None:
    """Run the steps in order; stop at the first that fails."""
    for step in todo:
        log(step.shown())
        if step.path:
            target = root / step.path
            target.parent.mkdir(parents=True, exist_ok=True)
            if step.append:
                old = target.read_text(encoding="utf-8") if target.exists() else ""
                target.write_text(old.rstrip("\n") + (step.text or "") + "\n", encoding="utf-8")
            else:
                target.write_text(step.text or "", encoding="utf-8")
            continue
        given: bytes | None = None
        if step.stdin == "keyfile":
            given = key_file.read_bytes()
        elif step.stdin:
            value = secrets_of(step.stdin)
            if not value:
                raise EncryptionError(f"{step.why}: the {step.stdin} is missing")
            given = value.encode("utf-8")
        try:
            done = run(list(step.argv), input=given, capture_output=True, timeout=600, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            raise EncryptionError(f"{step.why}: {e}") from e
        if done.returncode != 0:
            err = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
            raise EncryptionError(f"{step.why}: {step.argv[0]} failed: {' / '.join(err)}")
