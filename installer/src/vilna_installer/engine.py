# SPDX-License-Identifier: GPL-3.0-or-later
"""The installation engine: puts Vilna on the disk the person confirmed.

image/docs/installer.md, sections 1.1, 2 and 8a. Gloxi never reaches this
code: it runs only after the Herkos screen, as root, for the one plan whose
SHA-256 the person confirmed. It is deterministic and does only what the
plan says:

1. **Check** the plan again: confirmed, the same file the person saw, valid
   for the disks as they are now, the chosen disk is not the stick Vilna runs
   from, and a TPM2 plan has a chip that can keep the key.
2. **Lay out** the disk with ``systemd-repart``, as Aeon's tik does: an ESP
   and the system partition, LUKS2 with a one-time key file in slot 0
   (``encryption``), filled block by block from the system partition of the
   **Vilna disk image** that the live medium carries (``CopyBlocks=``). That
   is the same image OBS builds and the VM tests boot, with its btrfs
   subvolumes, snapshot 1 and SELinux labels; the engine does not rebuild it.
3. **Open** the new system, grow its btrfs to the whole partition and mount
   it with its subvolumes.
4. **Copy the model** from the live system (``cp -a``) and check it with
   ``sha256sum -c`` against the sum its package wrote: the image is built
   without the weights, so the ISO does not carry them twice.
5. **Set up** the system from the plan: languages, keyboards, time zone,
   computer name, the account (the password on standard input to
   ``chpasswd``), the Wi-Fi connection the live session made, and a SELinux
   relabel on the first boot.
6. **Boot and keys** (``encryption.steps``): the boot loader with
   ``sdbootutil``, the recovery key, TPM2 or the passphrase, slot 0 wiped,
   and a firmware boot entry.
7. **Finish**: the root snapshot read-only again, everything unmounted and
   closed, the key file wiped.

Progress goes to ``progress.json`` next to the plan (step 13 of the
conversation and the progress screen read it); the log, with no secret in
it, to ``engine.log`` beside it. Secrets come on standard input once and
reach programs only on their standard input (``encryption.run_steps``).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vilna_mcp_installer import machine as machines
from vilna_mcp_installer import plan as plans

from vilna_installer import confirm, encryption
from vilna_installer.encryption import EncryptionError, Step

# Where the live medium keeps the Vilna disk image and the model. OBS wraps
# the image tik-osimage-Vilna into an RPM of the same name, as it does for
# Aeon's tik-osimage-Aeon (containment-tik-img-to-rpm), which puts it in
# /usr/lib/tik/img/<name>.<version>.raw (image/docs/installer.md, 10.1).
IMAGE_DIR = Path("/usr/lib/tik/img")
IMAGE_GLOB = "tik-osimage-Vilna*.raw"


def find_image(directory: Path = IMAGE_DIR) -> Path:
    """The Vilna disk image on the live medium (``VILNA_INSTALLER_IMAGE`` wins).

    Without one the path still names where it should be, so that the check
    before any write says plainly what is missing.
    """
    if env := os.environ.get("VILNA_INSTALLER_IMAGE"):
        return Path(env)
    found = sorted(directory.glob(IMAGE_GLOB)) if directory.is_dir() else []
    return found[-1] if found else directory / "tik-osimage-Vilna.raw"


MODEL_DIR = Path("/usr/share/vilna/models")
MODEL_LINK = "default.gguf"
MODEL_SUM = "default.gguf.sha256"
WORK = Path("/run/vilna-install")
NM_CONNECTIONS = Path("/etc/NetworkManager/system-connections")

ESP_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
BOOT_ENTRY = "Vilna Boot Manager"
# Groups the account joins: wheel gives sudo and polkit admin (Vilna-release).
GROUPS = "wheel"
# Where the live system's settings must not end up in the new one.
LIVE_ONLY_USER = "live"

# The phases and how much of the time each takes (copying the image and the
# model is most of it); the progress screen shows them in this order.
PHASES: tuple[tuple[str, int], ...] = (
    ("check", 2),
    ("layout", 55),
    ("open", 3),
    ("model", 20),
    ("system", 5),
    ("boot", 12),
    ("finish", 3),
)
PHASE_KEYS = tuple(k for k, _ in PHASES)

# What the progress screen says for each phase (msgids of locale/*.po).
PHASE_TEXT = {
    "check": "Checking the plan and the disk",
    "layout": "Writing Vilna to the disk",
    "open": "Opening the new system",
    "model": "Copying Gloxi's model",
    "system": "Setting up languages, time and your account",
    "boot": "Setting up start-up and encryption",
    "finish": "Finishing",
}


# The progress screen (install_window).
TITLE = "Installing Vilna"
LEAD = "This takes a few minutes. Do not switch the computer off and do not pull out the stick."
DONE = "Vilna is installed"
DONE_LEAD = (
    "Take out the stick Vilna started from, then restart the computer. Vilna starts from the disk."
)
FAILED = "The installation stopped"
FAILED_LEAD = "Nothing on the disk has changed yet. You can close this window and try again."
FAILED_TOUCHED = (
    "The disk was already partly written: Vilna will not start from it, and what was "
    "on it before is erased. Close this window and start the installation again."
)
RESTART = "Restart"
CLOSE = "Close"
WHAT_HAPPENED = "What went wrong (for a bug report):"
TEXTS = (TITLE, LEAD, DONE, DONE_LEAD, FAILED, FAILED_LEAD, FAILED_TOUCHED, RESTART, CLOSE,
         WHAT_HAPPENED, *PHASE_TEXT.values())  # fmt: skip


class EngineError(Exception):
    """The installation stopped; the message says where, never with a secret."""

    def __init__(self, message: str, *, touched: bool = False) -> None:
        super().__init__(message)
        self.touched = touched  # the disk was already changed


# --- progress ---------------------------------------------------------------------


@dataclass
class Progress:
    """``progress.json``: written whole each time, readable by the live user."""

    path: Path | None
    owner: int | None = None
    state: str = "running"
    phase: str = "check"
    detail: str = ""
    error: str | None = None
    touched: bool = False
    started: float = field(default_factory=time.time)

    def percent(self) -> int:
        if self.state == "done":
            return 100
        done = 0
        for key, weight in PHASES:
            if key == self.phase:
                break
            done += weight
        return min(99, done)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "state": self.state,
            "phase": self.phase,
            "phases": list(PHASE_KEYS),
            "percent": self.percent(),
            "detail": self.detail,
            "error": self.error,
            "disk_changed": self.touched,
            "seconds": int(time.time() - self.started),
        }

    def write(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.as_dict(), ensure_ascii=False) + "\n", encoding="utf-8")
        if self.owner is not None:
            with contextlib.suppress(OSError):
                os.chown(tmp, self.owner, -1)
        os.replace(tmp, self.path)

    def go(self, phase: str) -> None:
        self.phase, self.detail = phase, ""
        self.write()

    def note(self, detail: str) -> None:
        self.detail = detail[:200]
        self.write()


def read_progress(path: Path) -> dict[str, Any] | None:
    """For the window: the engine's last word, or None before it wrote one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("version") == 1 else None


# --- the request from the installer window ------------------------------------------


@dataclass
class Request:
    """One JSON line on the engine's standard input, from the installer process."""

    plan: Path
    plan_sha256: str
    secrets: dict[str, str]
    owner: int | None = None

    @classmethod
    def parse(cls, line: str) -> Request:
        try:
            data = json.loads(line)
        except ValueError as e:
            raise EngineError("the request is not JSON") from e
        if not isinstance(data, dict):
            raise EngineError("the request is not an object")
        plan, digest = data.get("plan"), data.get("plan_sha256")
        if not isinstance(plan, str) or not plan.startswith("/"):
            raise EngineError("the request has no absolute plan path")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise EngineError("the request has no SHA-256 of the confirmed plan")
        given = data.get("secrets") or {}
        if not isinstance(given, dict):
            raise EngineError("the secrets are not an object")
        secrets = {
            k: v for k, v in given.items() if k in ("recovery_key", "passphrase", "user") and v
        }
        if not all(isinstance(v, str) for v in secrets.values()):
            raise EngineError("a secret is not text")
        owner = data.get("owner")
        return cls(Path(plan), digest, secrets, owner if isinstance(owner, int) else None)

    def __repr__(self) -> str:  # never the secrets, even in a traceback
        return f"Request({self.plan}, {self.plan_sha256[:12]}…, {sorted(self.secrets)})"


def request_line(plan: Path, plan_sha256: str, vault: Any, owner: int | None) -> str:
    """What the installer window sends; the vault hands each secret over once."""
    secrets = {}
    for name in ("recovery_key", "passphrase", "user"):
        value = vault.take(name)
        if value:
            secrets[name] = value
    return json.dumps(
        {"plan": str(plan), "plan_sha256": plan_sha256, "secrets": secrets, "owner": owner}
    )


# --- checks before anything is written ------------------------------------------------


def preflight(
    plan: dict[str, Any],
    survey: dict[str, Any],
    *,
    digest: str,
    expected: str,
    tpm: bool | None,
    secrets: dict[str, str],
    image: Path,
    stage: str = "alpha",
) -> list[str]:
    """Why this plan cannot be installed now, in plain words; empty when it can."""
    out = []
    if digest != expected:
        out.append("the plan file is not the one confirmed on the Herkos screen")
    if plan.get("confirmed_by_user") is not True:
        out.append("the plan was not confirmed on the Herkos screen")
    report = plans.validate(plan, survey, stage=stage)
    for problem in report["problems"]:
        out.append(f"the plan check says: {problem.get('code')}")
    disk = plan.get("disk") or {}
    if disk.get("layout") != "replace":
        # The partition editor's plans go through the same validator, but this
        # engine writes only «Erase the whole disk» so far (see README).
        out.append("only «Erase the whole disk» can be installed by this version")
    target = next((d for d in survey.get("disks", []) if d.get("id") == disk.get("target")), None)
    if target is None:
        out.append("the chosen disk is not there any more")
    elif target.get("boot_medium"):
        out.append("the chosen disk is the one Vilna runs from")
    method = (disk.get("encryption") or {}).get("method", "none")
    if method == "tpm2" and tpm is False:
        out.append("this computer has no TPM 2.0 that can keep the key; choose a passphrase")
    if method in ("tpm2", "passphrase") and not secrets.get("recovery_key"):
        out.append("the recovery key shown on the Herkos screen is missing")
    if method == "passphrase" and not secrets.get("passphrase"):
        out.append("the disk passphrase is missing")
    if (plan.get("user") or {}).get("password") == plans.ENTERED and not secrets.get("user"):
        out.append("the account password is missing")
    if "\n" in secrets.get("user", "") or "\n" in secrets.get("passphrase", ""):
        out.append("a password has a line break")
    if not image.is_file():
        out.append(f"the Vilna system image is missing ({image})")
    return out


# --- what the engine reads from the system ---------------------------------------


Run = Callable[..., Any]


def printable(text: str) -> str:
    """Control characters (a NUL from pcrlock cuts a GTK label) as spaces; lines stay."""
    return "".join(c if c == "\n" or c.isprintable() else " " for c in text)


def output(run: Run, *argv: str) -> str:
    try:
        done = run(list(argv), capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        raise EngineError(f"{argv[0]}: {e}") from e
    if done.returncode != 0:
        err = (done.stderr or "").strip().splitlines()[-1:] or ["failed"]
        raise EngineError(f"{shlex.join(argv)}: {err[0]}")
    return done.stdout or ""


def partitions(run: Run, device: str) -> list[dict[str, Any]]:
    """The partitions of ``device`` (a disk or a loop device), from lsblk."""
    text = output(
        run, "lsblk", "--json", "--paths", "-o", "NAME,TYPE,PARTTYPE,PARTLABEL,FSTYPE,UUID",
        device,
    )  # fmt: skip
    try:
        tree = json.loads(text)
    except ValueError as e:
        raise EngineError(f"lsblk {device}: not JSON") from e
    out = []
    for dev in tree.get("blockdevices", []):
        for child in dev.get("children") or []:
            if child.get("type") == "part":
                out.append(child)
    return out


def image_partitions(parts: list[dict[str, Any]]) -> tuple[str, str]:
    """The ESP and the btrfs system partition of the Vilna disk image."""
    esp = next((p["name"] for p in parts if (p.get("parttype") or "").lower() == ESP_TYPE), None)
    root = next((p["name"] for p in parts if p.get("fstype") == "btrfs"), None)
    if not esp or not root:
        raise EngineError("the Vilna system image has no EFI or no btrfs partition")
    return esp, root


def new_partitions(parts: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The two partitions repart made (labels from ``encryption.repart_definitions``)."""
    esp = next((p for p in parts if p.get("partlabel") == "ESP"), None)
    root = next((p for p in parts if p.get("partlabel") == "vilna"), None)
    if esp is None or root is None:
        raise EngineError("systemd-repart did not make the ESP and the system partition")
    return esp, root


def subvolume_mounts(fstab: str) -> list[tuple[str, str]]:
    """``(subvolume, mount point)`` of the image's fstab, but / and /etc (inside /)."""
    out = []
    for line in fstab.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0].startswith("#") or fields[2] != "btrfs":
            continue
        point = fields[1]
        subvol = next((o[7:] for o in fields[3].split(",") if o.startswith("subvol=")), None)
        if not subvol or point in ("/", "/etc") or not point.startswith("/"):
            continue
        if ".." in point.split("/"):
            continue
        out.append((subvol, point))
    return out


def fstab_with_esp(fstab: str, esp_uuid: str) -> str:
    """The image's ``/boot/efi`` line, with the UUID of the new ESP (as tik does)."""
    out = []
    for line in fstab.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "/boot/efi" and not fields[0].startswith("#"):
            line = re.sub(r"^\S+", f"UUID={esp_uuid}", line)
        out.append(line)
    return "\n".join(out) + "\n"


def cmdline_without_root(text: str) -> str:
    """``/etc/kernel/cmdline`` without ``root=``: ``encryption.steps`` adds the new one."""
    words = [w for w in text.split() if not w.startswith("root=")]
    return " ".join(words) + "\n"


def wifi_connection(ssid: str, directory: Path = NM_CONNECTIONS) -> Path | None:
    """The NetworkManager profile the live session saved for ``ssid``."""
    try:
        files = sorted(directory.glob("*.nmconnection"))
    except OSError:
        return None
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if re.search(rf"^ssid={re.escape(ssid)}$", text, re.MULTILINE):
            return path
    return None


# --- the configuration from the plan ---------------------------------------------


def system_files(plan: dict[str, Any]) -> dict[str, str]:
    """Files of the new system's /etc that come straight from the plan."""
    loc = plan.get("locale") or {}
    languages = loc.get("languages") or ["en_US.UTF-8"]
    region = loc.get("region") or languages[0]
    keyboards = loc.get("keyboards") or ["us"]
    user = plan.get("user") or {}
    lang = languages[0]
    locale = [f"LANG={lang}"]
    if region != lang:
        locale += [
            f"{k}={region}"
            for k in ("LC_NUMERIC", "LC_TIME", "LC_MONETARY", "LC_PAPER", "LC_MEASUREMENT")
        ]
    # The passphrase prompt and the console stay Latin: the installer typed
    # every password in a Latin layout too (password-layout).
    layouts = ",".join(keyboards)
    files = {
        "etc/locale.conf": "\n".join(locale) + "\n",
        "etc/vconsole.conf": "KEYMAP=us\n",
        "etc/X11/xorg.conf.d/00-keyboard.conf": (
            'Section "InputClass"\n'
            '    Identifier "system-keyboard"\n'
            '    MatchIsKeyboard "on"\n'
            f'    Option "XkbLayout" "{layouts}"\n'
            "EndSection\n"
        ),
    }
    if user.get("hostname"):
        files["etc/hostname"] = f"{user['hostname']}\n"
    files.update(crash_report_files((plan.get("privacy") or {}).get("crash_reports")))
    return files


def crash_report_files(answer: Any) -> dict[str, str]:
    """The person's answer about crash reports for vilna-reportd and Tinta Office.

    ADR 0018 section 3. The same three files, with the same text, as
    ``vilna-reportd consent`` writes (``reports/client/src/consent.rs``):
    Tinta knows only yes or no, so «ask» is «no» for it. No answer, no files:
    then nothing is sent (the validator does not let such a plan through).
    """
    if answer not in plans.CRASH_REPORTS:
        return {}
    tinta = "true" if answer == "auto" else "false"
    return {
        "etc/vilna/reports.conf": (
            '# The answer to "If a program crashes, send us a crash report?" (ADR 0018).\n'
            "# auto: send by itself; ask: ask about each crash; never: keep on this computer.\n"
            f"crash_reports={answer}\n"
        ),
        "etc/environment.d/60-vilna-reports.conf": (
            "# Vilna's answer about crash reports, for Tinta Office (ADR 0018).\n"
            f"TINTA_OFFICE_REPORTS={tinta}\n"
        ),
        "etc/xdg/tinta-office/reports.conf": (
            f"# Vilna's answer about crash reports (ADR 0018).\nenabled={tinta}\n"
        ),
    }


# Gloxi's state of the VPN talk in the new account (gloxi/src/gloxi/vpn_intro.py).
VPN_STATE = ".local/state/gloxi/vpn-intro.json"


def vpn_state(choice: Any, now: float) -> str | None:
    """The person's VPN choice from the wizard, for Gloxi's first start (ADR 0027 section 2).

    A provider: Gloxi opens its app (or Proton's sign-up page) on the first
    start instead of asking again. «Skip»: Gloxi does not ask; /vpn still
    works. No choice (the conversation): no file, so Gloxi asks itself.
    """
    if choice == "skip":
        return json.dumps({"status": "declined", "time": int(now)})
    if choice in plans.VPN_CHOICES:
        return json.dumps({"status": "chosen", "choice": choice})
    return None


def _safe(value: Any, pattern: str, what: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise EngineError(f"the plan's {what} {value!r} is not allowed")
    return value


ZONE_RE = r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*"


# --- the engine ---------------------------------------------------------------------


class Engine:
    def __init__(
        self,
        request: Request,
        *,
        image: Path | None = None,
        run: Run = subprocess.run,
        machine: machines.Machine | None = None,
        progress: Progress | None = None,
        work: Path = WORK,
        model_dir: Path = MODEL_DIR,
        tpm: Callable[[], bool | None] = encryption.tpm_can_seal,
        log: Callable[[str], None] = lambda _line: None,
        stage: str = "alpha",
    ) -> None:
        self.request = request
        self.image = image or find_image()
        self.run = run
        self.machine = machine or machines.Machine()
        self.progress = progress or Progress(request.plan.with_name("progress.json"))
        self.work = work
        self.model_dir = model_dir
        self.tpm = tpm
        self.log = log
        self.stage = stage
        self.root = work / "root"
        self.key_file: Path | None = None
        self._undo: list[list[str]] = []  # what to unmount and close, last first
        self._snapshot_rw = False

    # the parts

    def install(self) -> None:
        try:
            plan = self.check()
            self.progress.touched = True
            self.layout_and_open(plan)
            self.copy_model()
            self.set_up(plan)
            self.boot(plan)
            self.progress.go("finish")
            self.finish()
        except (EngineError, EncryptionError) as e:
            self.progress.state, self.progress.error = "failed", printable(str(e))
            self.log(f"failed: {e}")
            self.cleanup()
            self.progress.write()
            raise EngineError(str(e), touched=self.progress.touched) from e
        finally:
            if self.key_file is not None:
                encryption.wipe_key_file(self.key_file)
                self.key_file = None
                self.forget_keys()
        self.progress.state = "done"
        self.progress.write()

    def check(self) -> dict[str, Any]:
        self.progress.go("check")
        path = self.request.plan
        try:
            digest = confirm.sha256(path)
            plan = plans.load(path)
        except (OSError, ValueError) as e:
            raise EngineError(f"the plan cannot be read: {e}") from e
        try:
            disks = self.machine.disks()
            survey = machines.survey(disks)
        except machines.MachineError as e:
            raise EngineError(f"the disks cannot be read: {e}") from e
        method = ((plan.get("disk") or {}).get("encryption") or {}).get("method")
        tpm = self.tpm() if method == "tpm2" else None
        problems = preflight(
            plan,
            survey,
            digest=digest,
            expected=self.request.plan_sha256,
            tpm=tpm,
            secrets=self.request.secrets,
            image=self.image,
            stage=self.stage,
        )
        if problems:
            raise EngineError("; ".join(problems))
        target = (plan.get("disk") or {})["target"]
        disk = next(d for d in disks if d.id == target)
        # The survey found it through /dev/disk/by-id a moment ago.
        self.disk = _safe(disk.path, r"/dev/[A-Za-z0-9_./-]+", "disk")
        return plan

    def _steps(self, todo: list[Step]) -> None:
        def secret(name: str) -> str | None:
            if name == "chpasswd":
                user = getattr(self, "_username", None)
                password = self.request.secrets.get("user")
                return f"{user}:{password}\n" if user and password else None
            return self.request.secrets.get(name)

        def log(line: str) -> None:
            self.log(line)
            self.progress.note(line)

        encryption.run_steps(
            todo, secret, root=self.root, key_file=self.key_file, run=self.run, log=log
        )

    def _cmd(self, why: str, *argv: str, cwd: str | None = None) -> None:
        self._steps([Step(why, tuple(argv), cwd=cwd)])

    def layout_and_open(self, plan: dict[str, Any]) -> None:
        method = plan["disk"]["encryption"]["method"]
        self.method = method
        self.work.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The image, read-only, with its partitions as loop devices.
        loop = output(self.run, "losetup", "--read-only", "--partscan", "--find", "--show",
                      str(self.image)).strip()  # fmt: skip
        self._undo.append(["losetup", "--detach", loop])
        self._cmd("wait for the image's partitions", "udevadm", "settle")
        _img_esp, img_root = image_partitions(partitions(self.run, loop))

        self.progress.go("layout")
        defs = self.work / "repart.d"
        defs.mkdir(exist_ok=True, mode=0o700)
        for old in defs.glob("*.conf"):
            old.unlink()
        for name, text in encryption.repart_definitions(method, copy_blocks=img_root).items():
            (defs / name).write_text(text, encoding="utf-8")
        # Whatever was on the disk goes first: with the old partition table
        # gone the kernel lets go of the old partitions, which systemd-repart
        # otherwise finds busy (an installation over an earlier one).
        todo = [
            Step("forget what was on the disk", ("wipefs", "--all", "--force", self.disk)),
            Step("wait for the disk", ("udevadm", "settle")),
        ]
        if method != "none":
            self.key_file = encryption.make_key_file(self.work / "keys")
            todo += encryption.before_layout()
        todo.append(
            encryption.repart_command(self.disk, defs, self.key_file)
            if self.key_file
            else Step(
                "lay out the disk",
                (
                    "systemd-repart",
                    "--no-pager",
                    "--pretty=no",
                    "--empty=force",
                    "--dry-run=no",
                    f"--definitions={defs}",
                    self.disk,
                ),
            )  # fmt: skip
        )
        todo.append(Step("wait for the new partitions", ("udevadm", "settle")))
        self._steps(todo)

        self.progress.go("open")
        esp, part = new_partitions(partitions(self.run, self.disk))
        self.esp = esp
        self.crypt_part = part["name"]
        # repart sets the GPT attribute "grow file system" (bit 59) even with
        # GrowFileSystem=no after CopyBlocks=, and systemd-growfs-root then
        # fails on the read-only snapshot at the first boot (VM test,
        # 15.11.2026). The engine grew the filesystem already.
        number = re.search(r"(\d+)$", part["name"])
        if number:
            self._cmd("no growing at the first boot",
                      "sfdisk", "--part-attrs", self.disk, number.group(1), "")  # fmt: skip
        if method != "none":
            self.crypt_uuid = (
                part.get("uuid")
                or output(self.run, "blkid", "-s", "UUID", "-o", "value", part["name"]).strip()
            )
            self._cmd(
                "open the system partition",
                "cryptsetup", "open", f"--key-file={self.key_file}", part["name"],
                encryption.MAPPER,
            )  # fmt: skip
            self._undo.append(["cryptsetup", "close", encryption.MAPPER])
            device = f"/dev/mapper/{encryption.MAPPER}"
        else:
            self.crypt_uuid = None
            device = part["name"]
        self.device = device
        # The image's partitions are not needed any more; detach them before
        # the copy is mounted, since both carry the same btrfs UUID.
        self._undo.remove(["losetup", "--detach", loop])
        self._cmd("let go of the image", "losetup", "--detach", loop)
        image_uuid = output(self.run, "blkid", "-s", "UUID", "-o", "value", device).strip()
        # Every computer gets its own filesystem UUID, not the image's: two
        # disks with Vilna in one computer would clash. btrfstune -m changes
        # only the UUID shown (metadata_uuid) and takes no time.
        self._cmd("a filesystem UUID of its own", "btrfstune", "-f", "-m", device)
        self.root_uuid = output(self.run, "blkid", "-s", "UUID", "-o", "value", device).strip()
        self.root.mkdir(parents=True, exist_ok=True)
        # The default subvolume is snapshot 1, read-only like on every boot;
        # the engine writes /usr (the model) once and seals it again.
        self._cmd("mount the new system", "mount", device, str(self.root))
        self._undo.append(["umount", "--recursive", "--lazy", str(self.root)])
        self._cmd("let the engine write the snapshot",
                  "btrfs", "property", "set", "-ts", str(self.root), "ro", "false")  # fmt: skip
        self._snapshot_rw = True
        self._cmd("use the whole partition", "btrfs", "filesystem", "resize", "max", str(self.root))
        fstab_path = self.root / "etc/fstab"
        try:
            fstab = fstab_path.read_text(encoding="utf-8")
        except OSError as e:
            raise EngineError(f"the image has no /etc/fstab: {e}") from e
        for subvol, point in subvolume_mounts(fstab):
            self._cmd(f"mount {point}", "mount", "-o", f"subvol={subvol}", device,
                      str(self.root) + point)  # fmt: skip
        esp_uuid = (
            esp.get("uuid")
            or output(self.run, "blkid", "-s", "UUID", "-o", "value", esp["name"]).strip()
        )
        if image_uuid and self.root_uuid:
            fstab = fstab.replace(image_uuid, self.root_uuid)
        fstab_path.write_text(fstab_with_esp(fstab, esp_uuid), encoding="utf-8")
        (self.root / "boot/efi").mkdir(parents=True, exist_ok=True)
        self._cmd("mount the EFI partition", "mount", esp["name"], str(self.root / "boot/efi"))
        for what, how in (("dev", "--rbind"), ("sys", "--rbind"), ("run", "--bind")):
            self._cmd(f"/{what} for the new system", "mount", how, f"/{what}",
                      str(self.root / what))  # fmt: skip
            self._cmd(f"/{what} for the new system", "mount", "--make-rslave",
                      str(self.root / what))  # fmt: skip
        self._cmd("/proc for the new system", "mount", "-t", "proc", "proc",
                  str(self.root / "proc"))  # fmt: skip

    def copy_model(self) -> None:
        self.progress.go("model")
        link = self.model_dir / MODEL_LINK
        try:
            weights = link.resolve(strict=True)
        except OSError as e:
            raise EngineError(f"the live system has no model ({link}): {e}") from e
        target = self.root / str(self.model_dir).lstrip("/")
        target.mkdir(parents=True, exist_ok=True)
        self._cmd("copy the model", "cp", "-a", str(weights), str(self.model_dir / MODEL_SUM),
                  str(link), str(target) + "/")  # fmt: skip
        self._cmd("check the copied model", "sha256sum", "--check", "--strict", MODEL_SUM,
                  cwd=str(target))  # fmt: skip

    def set_up(self, plan: dict[str, Any]) -> None:
        self.progress.go("system")
        for rel, text in system_files(plan).items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        zone = _safe((plan.get("locale") or {}).get("timezone"), ZONE_RE, "time zone")
        localtime = self.root / "etc/localtime"
        localtime.unlink(missing_ok=True)
        localtime.symlink_to(f"../usr/share/zoneinfo/{zone}")
        user = plan.get("user") or {}
        name = _safe(user.get("username"), r"[a-z_][a-z0-9_-]{0,30}", "login name")
        if name == LIVE_ONLY_USER or name == "root":
            raise EngineError(f"the login name {name!r} is taken by the system")
        full = user.get("full_name") or ""
        if not isinstance(full, str) or any(c in full for c in ":\n\r,"):
            raise EngineError("the full name has a character an account cannot hold")
        self._username = name
        vpn = vpn_state((plan.get("privacy") or {}).get("vpn"), time.time())
        owned = [f"/home/{name}/.config"] + ([f"/home/{name}/.local"] if vpn else [])
        self._steps([
            Step("create the account",
                 ("chroot", str(self.root), "useradd", "--create-home", "--groups", GROUPS,
                  "--comment", full, name)),
            Step("set the account password", ("chroot", str(self.root), "chpasswd"),
                 stdin="chpasswd"),
            # Everything gnome-initial-setup asks is in the plan already.
            Step("skip the first-start questions",
                 path=f"home/{name}/.config/gnome-initial-setup-done", text="yes"),
            *([Step("keep the VPN choice for Gloxi", path=f"home/{name}/{VPN_STATE}",
                    text=vpn)] if vpn else []),
            Step("the account owns its settings",
                 ("chroot", str(self.root), "chown", "-R", f"{name}:", *owned)),
            # Files written here have no SELinux label yet; the image does the same.
            Step("label the files on the first start", path="etc/selinux/.autorelabel", text=""),
        ])  # fmt: skip
        net = plan.get("network") or {}
        if net.get("mode") == "wifi" and net.get("wifi"):
            source = wifi_connection(str(net["wifi"]))
            if source is not None:
                target = self.root / "etc/NetworkManager/system-connections"
                target.mkdir(parents=True, exist_ok=True)
                self._cmd("keep the Wi-Fi connection", "install", "-m", "0600", str(source),
                          str(target) + "/")  # fmt: skip

    def boot(self, plan: dict[str, Any]) -> None:
        self.progress.go("boot")
        cmdline = self.root / "etc/kernel/cmdline"
        try:
            old = cmdline.read_text(encoding="utf-8")
        except OSError:
            old = ""
        cmdline.write_text(cmdline_without_root(old), encoding="utf-8")
        self._steps(
            encryption.steps(
                self.method,
                crypt_part=self.crypt_part,
                crypt_uuid=self.crypt_uuid,
                root_uuid=self.root_uuid,
                root=str(self.root),
                key_file=self.key_file,
            )
        )
        if Path("/sys/firmware/efi").is_dir():
            loader = next(
                (rel for rel in ("EFI/systemd/shim.efi", "EFI/systemd/systemd-bootx64.efi")
                 if (self.root / "boot/efi" / rel).is_file()),
                None,
            )  # fmt: skip
            number = re.search(r"(\d+)$", self.esp["name"])
            if loader and number:
                self._cmd("a start entry in the firmware", "efibootmgr", "--quiet", "--create",
                          "--disk", self.disk, "--part", number.group(1), "--label", BOOT_ENTRY,
                          "--loader", "\\" + loader.replace("/", "\\"))  # fmt: skip

    def finish(self) -> None:
        if self._snapshot_rw:
            self._cmd("the system snapshot read-only again",
                      "btrfs", "property", "set", "-ts", str(self.root), "ro", "true")  # fmt: skip
            self._snapshot_rw = False
        self.cleanup(strict=True)
        self._cmd("write everything to the disk", "sync")

    def forget_keys(self) -> None:
        """Drop this installation's keyring entries, also after a failure (none may be there)."""
        for step in encryption.forget_keys():
            self.log(step.shown())
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                self.run(list(step.argv), capture_output=True, timeout=60, check=False)

    def cleanup(self, strict: bool = False) -> None:
        """Unmount and close what the engine opened, last first."""
        while self._undo:
            argv = self._undo.pop()
            try:
                done = self.run(argv, capture_output=True, timeout=300, check=False)
                failed = done.returncode != 0
            except (OSError, subprocess.SubprocessError):
                failed = True
            if failed and strict:
                raise EngineError(f"{argv[0]} {argv[-1]} failed while finishing")


# --- a pretend installation, for --demo --------------------------------------------


def simulate(progress: Progress, pause: Callable[[float], None] = time.sleep,
             seconds: float = 12.0) -> None:  # fmt: skip
    """Walk through the phases without touching anything (screenshots, the window's tests)."""
    total = sum(w for _, w in PHASES)
    for key, weight in PHASES:
        progress.go(key)
        progress.note(PHASE_TEXT[key])
        pause(seconds * weight / total)
    progress.state = "done"
    progress.write()


def command(*, demo: bool, progress: Path) -> list[str]:
    """How the installer window starts the engine: as root, without a password prompt.

    In the live image the live user may use sudo without a password (Vilna-Live
    config.sh); ``-n`` makes sudo fail instead of asking anywhere else.
    """
    if demo:
        return [sys.executable, "-m", "vilna_installer.engine", "--demo", "--progress",
                str(progress)]  # fmt: skip
    return ["sudo", "-n", "/usr/bin/vilna-installer-engine", "--progress", str(progress)]


# --- the command ------------------------------------------------------------------


def isolate() -> None:
    """Before anything is touched: mounts and keys of the engine's own.

    The new system's mounts must not reach the namespaces of services
    (udevd, logind…): there they keep cr_root busy, so that it cannot be
    closed and the live system has to restart before another try (VM test,
    15.11.2026). And under sudo the keys must go to root's keyring
    (``encryption.own_session_keyring``).
    """
    if hasattr(os, "unshare"):  # Python 3.12+
        os.unshare(os.CLONE_NEWNS)
    else:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.unshare(0x00020000) != 0:  # CLONE_NEWNS
            raise OSError(ctypes.get_errno(), "a mount namespace of its own")
    subprocess.run(["mount", "--make-rprivate", "/"], check=True, capture_output=True)
    encryption.own_session_keyring()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="vilna-installer-engine",
        description="Install the confirmed plan (run by the installer as root).",
    )
    p.add_argument("--progress", type=Path, help="progress.json (default: next to the plan)")
    p.add_argument("--image", type=Path, default=None)
    p.add_argument("--demo", action="store_true", help="pretend: no root, nothing written")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    if args.demo:
        sys.stdin.readline()  # the request, ignored: nothing is installed
        simulate(Progress(args.progress))
        return 0
    try:
        request = Request.parse(sys.stdin.readline())
    except EngineError as e:
        print(f"vilna-installer-engine: {e}", file=sys.stderr)
        return 2
    progress = Progress(args.progress or request.plan.with_name("progress.json"), request.owner)
    if os.geteuid() != 0:
        progress.state, progress.error = "failed", "the engine must run as root"
        progress.write()
        print("vilna-installer-engine: must run as root", file=sys.stderr)
        return 2
    log_path = progress.path.with_name("engine.log") if progress.path else None
    log_file = open(log_path, "a", encoding="utf-8") if log_path else None  # noqa: SIM115
    if log_file is not None and request.owner is not None:
        with contextlib.suppress(OSError):
            os.chown(log_path, request.owner, -1)  # type: ignore[arg-type]

    def log(line: str) -> None:
        if log_file is not None:
            log_file.write(f"{time.strftime('%H:%M:%S')} {line}\n")
            log_file.flush()

    try:
        isolate()
    except (OSError, subprocess.SubprocessError, EncryptionError) as e:
        progress.state, progress.error = "failed", f"the engine could not set itself up: {e}"
        progress.write()
        print(f"vilna-installer-engine: {progress.error}", file=sys.stderr)
        return 1
    try:
        Engine(request, image=args.image, progress=progress, log=log).install()
    except EngineError as e:
        print(f"vilna-installer-engine: {e}", file=sys.stderr)
        return 1
    finally:
        if log_file is not None:
            log_file.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
