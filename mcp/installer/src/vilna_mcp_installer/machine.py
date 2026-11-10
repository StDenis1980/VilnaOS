# SPDX-License-Identifier: Apache-2.0
"""What the installer reads about the computer. Reading only: nothing here writes.

Every probe has a pure parser (tested with synthetic input) and a thin part
that runs a system program or reads ``/proc`` and ``/sys``. ``DemoMachine``
gives fixed synthetic computers for tests and demos; no real disk of anyone
ever goes into a test (installer.md, section 9).

Disk and partition labels, disk models and Wi-Fi names come from outside:
whoever made the disk or runs the network chose them. ``EXTERNAL_FIELDS``
names them, so that the conversation passes them to the model as data
and not as instructions (installer.md, end of section 3).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import zoneinfo
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GiB = 1 << 30
SECTOR = 512

# Text that people outside the installer chose: never instructions.
EXTERNAL_FIELDS = ("label", "partlabel", "model", "ssid")

# GPT partition types (UEFI specification and the Discoverable Partitions
# Specification of the UAPI group), lower case.
EFI = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
MS_BASIC_DATA = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
MS_RESERVED = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
WIN_RECOVERY = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"
LINUX_TYPES = frozenset(
    {
        "0fc63daf-8483-4772-8e79-3d69d8477de4",  # Linux filesystem
        "4f68bce3-e8cd-4db1-96e7-fbcaf984b709",  # root, x86-64
        "b921b045-1df0-41c3-af44-4c6f280d3fae",  # root, aarch64
        "933ac7e1-2eb4-4f13-b844-0e14e2aef915",  # /home
        "e6d6d379-f507-44c2-a23c-238f2a3df928",  # LVM
        "a19d880f-05fc-4d3b-a006-743f0f84911e",  # RAID
        "ca7d7ccb-63ed-4c53-861c-1742536059cc",  # LUKS
        "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f",  # swap
    }
)
LINUX_FS = frozenset({"ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs", "jfs", "reiserfs"})
# Filesystems the simple path understands; anything else means manual layout.
KNOWN_FS = LINUX_FS | {"vfat", "ntfs", "exfat", "swap", "crypto_LUKS", "BitLocker", "iso9660"}
MULTI_DISK = frozenset({"linux_raid_member", "LVM2_member", "zfs_member", "isw_raid_member"})
ENCRYPTED = {"crypto_LUKS": "luks", "BitLocker": "bitlocker", "tcrypt": "veracrypt"}


@dataclass
class Partition:
    id: str  # PARTUUID when there is one: stable across reboots and renames
    name: str  # nvme0n1p3; shown to people, never trusted as the identity
    size: int
    start: int | None = None  # bytes from the start of the disk
    fstype: str | None = None
    label: str | None = None
    partlabel: str | None = None
    parttype: str | None = None
    mounted: bool = False

    @property
    def encrypted(self) -> str | None:
        return ENCRYPTED.get(self.fstype or "")

    @property
    def kind(self) -> str:
        t = (self.parttype or "").lower()
        if t == EFI or (self.fstype == "vfat" and (self.partlabel or "").upper().startswith("EFI")):
            return "efi"
        if t == MS_RESERVED:
            return "reserved"
        if t == WIN_RECOVERY:
            return "recovery"
        if self.fstype == "swap":
            return "swap"
        if self.fstype in MULTI_DISK:
            return "multi_disk"
        if not self.fstype:
            return "unknown" if self.size > 64 << 20 else "empty"
        if self.fstype not in KNOWN_FS:
            return "unknown"
        return "data"

    @property
    def system(self) -> str | None:
        """Which kind of system this partition is part of, as far as it shows."""
        t = (self.parttype or "").lower()
        if t in (MS_RESERVED, WIN_RECOVERY) or self.fstype == "BitLocker":
            return "Windows"
        if t == MS_BASIC_DATA and self.fstype == "ntfs":
            return "Windows"
        if t in LINUX_TYPES or self.fstype in LINUX_FS or self.fstype == "crypto_LUKS":
            return "Linux"
        return None

    def as_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "fstype": self.fstype,
            "label": self.label,
            "kind": self.kind,
            "system": self.system,
            "encrypted": self.encrypted,
            # Erasing it loses something a person may want: files or a system.
            "holds_data": self.kind in ("data", "unknown", "multi_disk") or bool(self.encrypted),
        }
        if self.partlabel:
            out["partlabel"] = self.partlabel
        return out


@dataclass
class Disk:
    id: str  # /dev/disk/by-id name: stable while the same disk is in the computer
    name: str
    path: str
    size: int
    model: str | None = None
    transport: str | None = None
    removable: bool = False
    read_only: bool = False
    boot_medium: bool = False  # the USB stick the live system runs from
    partitions: list[Partition] = field(default_factory=list)
    # A filesystem straight on the disk, without a partition table.
    fstype: str | None = None

    def free_regions(self) -> list[int]:
        """Sizes of the gaps between partitions, largest first (1 MiB alignment ignored)."""
        if not self.partitions:
            return [self.size] if not self.fstype else []
        if any(p.start is None for p in self.partitions):
            used = sum(p.size for p in self.partitions)
            return [max(self.size - used - (2 << 20), 0)]
        gaps, cursor = [], 1 << 20  # GPT and alignment take the first MiB
        for p in sorted(self.partitions, key=lambda p: p.start or 0):
            start = p.start or 0
            if start > cursor:
                gaps.append(start - cursor)
            cursor = max(cursor, start + p.size)
        end = self.size - (1 << 20)  # the backup GPT at the end
        if end > cursor:
            gaps.append(end - cursor)
        return sorted((g for g in gaps if g >= 16 << 20), reverse=True)

    @property
    def systems(self) -> list[str]:
        return sorted({p.system for p in self.partitions if p.system})

    def as_dict(self) -> dict[str, Any]:
        free = self.free_regions()
        return {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "model": self.model,
            "transport": self.transport,
            "removable": self.removable,
            "boot_medium": self.boot_medium,
            "read_only": self.read_only,
            "fstype": self.fstype,
            "systems": self.systems,
            "empty": not self.partitions and not self.fstype,
            "largest_free": free[0] if free else 0,
            "needs_manual_layout": self.needs_manual_layout(),
            "partitions": [p.as_dict() for p in self.partitions],
        }

    def needs_manual_layout(self) -> bool:
        """Unknown filesystems, RAID and LVM: only the partition editor (section 5)."""
        if self.fstype and self.fstype not in KNOWN_FS:
            return True
        return any(p.kind in ("unknown", "multi_disk") for p in self.partitions)


def fingerprint(disks: Iterable[Disk]) -> str:
    """Changes when a disk is added, removed or repartitioned (section 5).

    The plan keeps the fingerprint of the survey it was made from; a plan
    whose fingerprint differs from the computer's now is not valid.
    """
    rows = sorted(
        (d.id, d.size, tuple(sorted((p.id, p.size, p.fstype or "") for p in d.partitions)))
        for d in disks
    )
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]


def survey(disks: list[Disk]) -> dict[str, Any]:
    return {"fingerprint": fingerprint(disks), "disks": [d.as_dict() for d in disks]}


# --- lsblk ----------------------------------------------------------------

LSBLK_COLUMNS = (
    "NAME,PATH,TYPE,SIZE,START,FSTYPE,LABEL,PARTLABEL,PARTTYPE,PARTUUID,"
    "MODEL,SERIAL,WWN,TRAN,RM,RO,MOUNTPOINTS"
)


def _bool(value: Any) -> bool:
    return value in (True, 1, "1", "true")


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _mounted(dev: dict[str, Any]) -> bool:
    points = dev.get("mountpoints") or ([dev["mountpoint"]] if dev.get("mountpoint") else [])
    return any(points)


def parse_lsblk(
    text: str, by_id: dict[str, str] | None = None, boot_names: Iterable[str] = ()
) -> list[Disk]:
    """Disks from ``lsblk -J -b -o LSBLK_COLUMNS``; ``by_id`` maps a name to its by-id link."""
    by_id = by_id or {}
    boot = set(boot_names)
    disks = []
    for dev in json.loads(text).get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        name = str(dev.get("name") or "")
        if not name or name.startswith(("loop", "zram", "ram", "sr")):
            continue
        children = dev.get("children") or []
        parts = []
        for child in children:
            if child.get("type") != "part":
                continue
            child_name = str(child.get("name") or "")
            mounted = _mounted(child) or any(_mounted(c) for c in child.get("children") or [])
            parts.append(
                Partition(
                    id=_text(child.get("partuuid")) or child_name,
                    name=child_name,
                    size=_int(child.get("size")),
                    start=_int(child.get("start")) * SECTOR if child.get("start") else None,
                    fstype=_text(child.get("fstype")),
                    label=_text(child.get("label")),
                    partlabel=_text(child.get("partlabel")),
                    parttype=_text(child.get("parttype")),
                    mounted=mounted,
                )
            )
        disk_id = by_id.get(name) or _stable_id(dev) or name
        any_mounted = _mounted(dev) or any(p.mounted for p in parts)
        disks.append(
            Disk(
                id=disk_id,
                name=name,
                path=str(dev.get("path") or f"/dev/{name}"),
                size=_int(dev.get("size")),
                model=_text(dev.get("model")),
                transport=_text(dev.get("tran")),
                removable=_bool(dev.get("rm")),
                read_only=_bool(dev.get("ro")),
                boot_medium=name in boot or (any_mounted and _bool(dev.get("rm"))),
                partitions=parts,
                fstype=_text(dev.get("fstype")) if not parts else None,
            )
        )
    return disks


def _stable_id(dev: dict[str, Any]) -> str | None:
    if wwn := _text(dev.get("wwn")):
        return f"wwn-{wwn}"
    model, serial = _text(dev.get("model")), _text(dev.get("serial"))
    if model and serial:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", f"{dev.get('tran') or 'disk'}-{model}_{serial}")
    return None


def by_id_links(directory: Path = Path("/dev/disk/by-id")) -> dict[str, str]:
    """Disk name → its most specific by-id link (wwn- and nvme-eui. last)."""
    links: dict[str, str] = {}
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return links
    for link in entries:
        if "-part" in link.name:
            continue
        try:
            target = link.resolve().name
        except OSError:
            continue
        current = links.get(target)
        if current is None or current.startswith(("wwn-", "nvme-eui.")):
            links[target] = link.name
    return links


def live_boot_disks() -> set[str]:
    """Disks the running live system was started from (its root or medium is on them)."""
    names: set[str] = set()
    for mount in ("/run/initramfs/live", "/run/rootfsbase", "/"):
        try:
            out = subprocess.run(
                ["findmnt", "-n", "-o", "SOURCE", "--target", mount],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        dev = Path(out.split("[")[0]).name if out.startswith("/dev/") else ""
        if not dev:
            continue
        node = Path("/sys/class/block") / dev
        try:
            # A partition's directory sits inside its disk's directory.
            disk = node.resolve().parent.name if (node / "partition").exists() else dev
        except OSError:
            disk = dev
        names.add(disk)
    return names


# --- hardware ---------------------------------------------------------------


def parse_meminfo(text: str) -> int:
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            return _int(parts[1]) * 1024 if len(parts) > 1 else 0
    return 0


def parse_cpuinfo(text: str) -> tuple[str | None, int]:
    model, count = None, 0
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key = key.strip()
        if key == "processor":
            count += 1
        elif key in ("model name", "Model") and model is None:
            model = value.strip() or None
    return model, count


def secure_boot(efivars: Path = Path("/sys/firmware/efi/efivars")) -> bool | None:
    var = efivars / "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
    try:
        data = var.read_bytes()
    except OSError:
        return None
    return bool(data[-1]) if data else None


def tpm_version(sys_tpm: Path = Path("/sys/class/tpm/tpm0")) -> int | None:
    try:
        return _int((sys_tpm / "tpm_version_major").read_text().strip()) or None
    except OSError:
        return 1 if sys_tpm.exists() else None


# --- keyboards, time zones, networks ---------------------------------------


def parse_xkb_layouts(text: str) -> list[dict[str, str]]:
    """The ``! layout`` section of xkb's ``base.lst``: code and English name."""
    layouts, inside = [], False
    for line in text.splitlines():
        if line.startswith("!"):
            inside = line.strip() == "! layout"
            continue
        if inside and line.strip():
            code, _, name = line.strip().partition(" ")
            layouts.append({"code": code, "name": name.strip()})
    return layouts


XKB_LISTS = (Path("/usr/share/X11/xkb/rules/base.lst"), Path("/usr/share/X11/xkb/rules/evdev.lst"))

# The ten languages of ADR 0009 and the keyboard each starts with.
KEYBOARD_OF = {
    "uk": "ua",
    "en": "us",
    "es": "es",
    "pl": "pl",
    "de": "de",
    "fr": "fr",
    "it": "it",
    "ro": "ro",
    "cs": "cz",
    "lt": "lt",
}
LOCALE_OF = {
    "uk": "uk_UA.UTF-8",
    "en": "en_US.UTF-8",
    "es": "es_ES.UTF-8",
    "pl": "pl_PL.UTF-8",
    "de": "de_DE.UTF-8",
    "fr": "fr_FR.UTF-8",
    "it": "it_IT.UTF-8",
    "ro": "ro_RO.UTF-8",
    "cs": "cs_CZ.UTF-8",
    "lt": "lt_LT.UTF-8",
}
TIMEZONE_OF = {
    "uk_UA": "Europe/Kyiv",
    "en_GB": "Europe/London",
    "en_IE": "Europe/Dublin",
    "es_ES": "Europe/Madrid",
    "pl_PL": "Europe/Warsaw",
    "de_DE": "Europe/Berlin",
    "de_AT": "Europe/Vienna",
    "fr_FR": "Europe/Paris",
    "it_IT": "Europe/Rome",
    "ro_RO": "Europe/Bucharest",
    "cs_CZ": "Europe/Prague",
    "lt_LT": "Europe/Vilnius",
}


def timezones() -> list[str]:
    zones = zoneinfo.available_timezones()
    return sorted(z for z in zones if "/" in z and not z.startswith(("Etc/", "SystemV/", "US/")))


def parse_nmcli_wifi(text: str) -> list[dict[str, Any]]:
    """``nmcli -t -f SSID,SECURITY,SIGNAL,ACTIVE dev wifi list``; ``\\:`` is an escaped colon."""
    networks: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        fields = [f.replace("\\:", ":").replace("\\\\", "\\") for f in re.split(r"(?<!\\):", line)]
        if len(fields) < 4 or not fields[0]:
            continue
        ssid, security, signal, active = fields[0], fields[1], _int(fields[2]), fields[3]
        row = {
            "ssid": ssid[:64],
            "secured": security not in ("", "--"),
            "signal": signal,
            "connected": active in ("yes", "так"),
        }
        best = networks.get(ssid)
        if best is None or row["signal"] > best["signal"] or row["connected"]:
            networks[ssid] = row
    return sorted(networks.values(), key=lambda n: (-n["connected"], -n["signal"]))[:20]


# --- the machine ------------------------------------------------------------


class Machine:
    """The real computer the live system runs on."""

    def _run(self, *argv: str, timeout: float = 20) -> str:
        try:
            done = subprocess.run(
                list(argv), capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise MachineError(f"{argv[0]} did not run: {e}") from e
        if done.returncode != 0:
            raise MachineError(f"{argv[0]} failed: {done.stderr.strip()[:200]}")
        return done.stdout

    def disks(self) -> list[Disk]:
        text = self._run("lsblk", "-J", "-b", "-o", LSBLK_COLUMNS)
        return parse_lsblk(text, by_id_links(), live_boot_disks())

    def hardware(self) -> dict[str, Any]:
        def read(path: str) -> str:
            try:
                return Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""

        model, cores = parse_cpuinfo(read("/proc/cpuinfo"))
        return {
            "architecture": platform.machine(),
            "cpu": model,
            "cpu_threads": cores or os.cpu_count() or 0,
            "memory": parse_meminfo(read("/proc/meminfo")),
            "uefi": Path("/sys/firmware/efi").is_dir(),
            "secure_boot": secure_boot(),
            "tpm": tpm_version(),
        }

    def keyboards(self) -> list[dict[str, str]]:
        for path in XKB_LISTS:
            try:
                return parse_xkb_layouts(path.read_text(encoding="utf-8"))
            except OSError:
                continue
        return [{"code": code, "name": code} for code in sorted(set(KEYBOARD_OF.values()))]

    def networks(self) -> list[dict[str, Any]]:
        text = self._run(
            "nmcli", "-t", "-f", "SSID,SECURITY,SIGNAL,ACTIVE", "device", "wifi", "list"
        )
        return parse_nmcli_wifi(text)

    def wired(self) -> bool:
        try:
            text = self._run("nmcli", "-t", "-f", "TYPE,STATE", "device")
        except MachineError:
            return False
        return any(line.split(":")[:2] == ["ethernet", "connected"] for line in text.splitlines())


class MachineError(Exception):
    """A system program could not tell what we asked."""


class DemoMachine(Machine):
    """A synthetic computer: ``empty`` (a new disk) or ``windows`` (Windows 11, BitLocker)."""

    def __init__(self, kind: str = "empty") -> None:
        if kind not in DEMOS:
            raise ValueError(f"unknown demo machine {kind!r}; one of {', '.join(DEMOS)}")
        self.kind = kind
        self._disks = DEMOS[kind]()

    def disks(self) -> list[Disk]:
        return self._disks

    def hardware(self) -> dict[str, Any]:
        return {
            "architecture": "x86_64",
            "cpu": "Demo CPU",
            "cpu_threads": 8,
            "memory": 16 * GiB,
            "uefi": True,
            "secure_boot": True,
            "tpm": 2,
        }

    def keyboards(self) -> list[dict[str, str]]:
        names = {"ua": "Ukrainian", "us": "English (US)", "pl": "Polish", "cz": "Czech"}
        return [{"code": c, "name": names.get(c, c)} for c in sorted(set(KEYBOARD_OF.values()))]

    def networks(self) -> list[dict[str, Any]]:
        return [
            {"ssid": "DemoHome", "secured": True, "signal": 82, "connected": False},
            {"ssid": "Cafe Guest", "secured": False, "signal": 40, "connected": False},
        ]

    def wired(self) -> bool:
        return False


def _usb_stick() -> Disk:
    return Disk(
        id="usb-Demo_Stick_0001",
        name="sda",
        path="/dev/sda",
        size=16 * GiB,
        model="Demo Stick",
        transport="usb",
        removable=True,
        boot_medium=True,
        partitions=[
            Partition("sda1", "sda1", 3 * GiB, start=1 << 20, fstype="iso9660", label="VILNA_LIVE")
        ],
    )


def _empty() -> list[Disk]:
    return [
        Disk(
            id="nvme-Demo_SSD_512_0001",
            name="nvme0n1",
            path="/dev/nvme0n1",
            size=512 * GiB,
            model="Demo SSD 512",
            transport="nvme",
        ),
        _usb_stick(),
    ]


def _windows() -> list[Disk]:
    mib = 1 << 20
    parts = [
        Partition("p-efi", "nvme0n1p1", 260 * mib, 1 * mib, "vfat", "SYSTEM", "EFI system", EFI),
        Partition("p-msr", "nvme0n1p2", 16 * mib, 261 * mib, None, None, "MSR", MS_RESERVED),
        Partition(
            "p-win",
            "nvme0n1p3",
            380 * GiB,
            277 * mib,
            "BitLocker",
            None,
            "Basic data partition",
            MS_BASIC_DATA,
        ),
        Partition(
            "p-rec",
            "nvme0n1p4",
            700 * mib,
            277 * mib + 380 * GiB,
            "ntfs",
            "Recovery",
            None,
            WIN_RECOVERY,
        ),
    ]
    return [
        Disk(
            id="nvme-Demo_SSD_512_0002",
            name="nvme0n1",
            path="/dev/nvme0n1",
            size=512 * GiB,
            model="Demo SSD 512",
            transport="nvme",
            partitions=parts,
        ),
        _usb_stick(),
    ]


DEMOS = {"empty": _empty, "windows": _windows}
