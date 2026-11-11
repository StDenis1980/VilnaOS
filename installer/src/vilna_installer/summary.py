# SPDX-License-Identifier: GPL-3.0-or-later
"""The plan in the person's words: one line per part, one line per change of a disk.

Step 12 of image/docs/installer.md. The conversation, the classic wizard
and the Herkos confirmation screen show the same text, made here from the
plan and from what ``vilna_mcp_installer.plan.validate`` reports. Nothing
here comes from the model: the lines are translated texts filled with the
plan's values and the survey of the disks.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from vilna_mcp_installer import plan as plans

from vilna_installer import conversation as conv
from vilna_installer import start

Texts = Callable[[str], str]

# Units. Disk sizes in binary units, as the partition tools show them.
GIB = "{n} GiB"
MIB = "{n} MiB"

# What happens to a partition or a disk (``changes`` of the validator).
ERASE = "Erased: {what}"
ERASE_FOR = "Formatted for {use}, everything on it erased: {what}"
KEEP = "Kept as it is: {what}"
SHRINK = "Shrunk to {to}: {what}"
USE = "Used for {use} without formatting: {what}"
CREATE = "New: Vilna, {size}"
CREATE_ENCRYPTED = "New: Vilna, {size}, encrypted"
CREATE_FOR = "New partition for {use}: {size}"
MANUAL_DISK = "Your own layout of {disk}"
DISK = "{model} ({name}, {size})"
UNNAMED = "Disk {name}, {size}"
ENCRYPTED_WITH = "encrypted with {kind}"
UNKNOWN_FS = "unknown contents"
EMPTY = "empty"

USES = {
    "efi": "the boot partition (EFI)",
    "system": "the system",
    "home": "home folders",
    "swap": "swap",
}
ENCRYPTION_KINDS = {"bitlocker": "BitLocker", "luks": "LUKS", "veracrypt": "VeraCrypt"}

# The validator's problems, as the person reads them. A code not here shows
# its own (English) message after GENERIC, so nothing is ever hidden.
PROBLEMS = {
    "disks_changed": (
        "The disks changed since they were looked at (a USB stick put in or taken out?). "
        "Choose the disk again."
    ),
    "no_target": "The chosen disk is not in the computer now. Choose the disk again.",
    "boot_medium": "This is the USB stick Vilna is running from. Choose another disk.",
    "read_only": "The chosen disk cannot be written to.",
    "too_small": "The disk is too small: Vilna needs at least 40 GiB.",
    "needs_manual": (
        "The disk has RAID, LVM or a filesystem Vilna does not know: only your own layout "
        "in the partition editor is possible."
    ),
    "later_stage": "This way of installing comes in a later version of Vilna.",
    "manual_needs_editor": "Make the layout in the partition editor first.",
    "wifi_password": "The Wi-Fi password is not entered yet.",
    "user_password": "The account password is not entered yet.",
    "passphrase": "The disk passphrase is not entered yet.",
    "not_enough_space": "There is not enough free space for Vilna on the disk.",
    "no_tpm": (
        "This computer has no TPM 2.0 chip that can keep the disk key. Choose encryption "
        "with a passphrase instead."
    ),
    # Warnings: they do not block.
    "no_encryption": "Without encryption anyone who has the disk can read your files.",
    "removable": "The chosen disk is removable, such as a USB disk.",
}
GENERIC = "The plan cannot be installed yet: {message}"
NOT_DECIDED_YET = "Not decided yet: {parts}"
# Steps the validator reports as missing, by the plan section they fill.
MISSING_SECTION = {
    "languages": "languages",
    "network": "network",
    "disk": "disk",
    "encryption": "encryption",
    "user": "user",
    "time": "time",
}
# Problems the Herkos screen solves itself, so they never block reaching it.
SOLVED_ON_SCREEN = frozenset({"recovery_key", "confirmed_invalid"})

TEXTS = (
    GIB,
    MIB,
    ERASE,
    ERASE_FOR,
    KEEP,
    SHRINK,
    USE,
    CREATE,
    CREATE_ENCRYPTED,
    CREATE_FOR,
    MANUAL_DISK,
    DISK,
    UNNAMED,
    ENCRYPTED_WITH,
    UNKNOWN_FS,
    EMPTY,
    *USES.values(),
    *PROBLEMS.values(),
    GENERIC,
    NOT_DECIDED_YET,
)


@dataclass(frozen=True)
class Line:
    key: str  # a section of the plan (conv.SECTIONS)
    title: str
    text: str | None  # None: not decided yet


@dataclass(frozen=True)
class Change:
    action: str  # erase, keep, shrink, use, create, manual
    text: str
    # Something on the disk is lost: shown first and marked as such.
    loses_data: bool = False


@dataclass
class Summary:
    lines: list[Line]
    changes: list[Change]
    problems: list[str]
    warnings: list[str] = field(default_factory=list)
    # The disk that is written to, in words; None when not chosen.
    disk: str | None = None
    # True when the plan erases something (the screen asks for a tick then).
    erases: bool = False

    @property
    def ready(self) -> bool:
        return not self.problems


def size(n: Any, _: Texts) -> str:
    if not isinstance(n, int) or n < 0:
        return "?"
    if n >= plans.GiB:
        return _(GIB).format(n=round(n / plans.GiB))
    return _(MIB).format(n=round(n / (1 << 20)))


def describe(plan: dict[str, Any], _: Texts) -> dict[str, str | None]:
    """One line per part of the plan, in the person's language; None: not decided."""
    loc = plan.get("locale") or {}
    net = plan.get("network") or {}
    disk = plan.get("disk") or {}
    enc = disk.get("encryption") or {}
    user = plan.get("user") or {}
    out: dict[str, str | None] = dict.fromkeys(conv.SECTIONS)
    if loc.get("languages"):
        names = {lang.locale: lang.name for lang in start.LANGUAGES}
        langs = ", ".join(names.get(lang, lang.split(".")[0]) for lang in loc["languages"])
        out["languages"] = f"{langs} · {', '.join(loc.get('keyboards') or [])}"
    mode = net.get("mode")
    if mode == "wifi":
        entered = f" · {_(conv.ENTERED_TEXT)}" if net.get("secret") == plans.ENTERED else ""
        out["network"] = f"Wi‑Fi «{net.get('wifi')}»{entered}"
    elif mode:
        out["network"] = _(conv.WIRED) if mode == "wired" else _(conv.OFFLINE)
    if disk.get("layout"):
        what = _(conv.ERASE_ALL) if disk["layout"] == "replace" else _(conv.MANUAL)
        out["disk"] = f"{what} · {disk.get('target')}"
    method = enc.get("method")
    if method in ("tpm2", "passphrase", "none"):
        out["encryption"] = {
            "tpm2": _(conv.ENCRYPT_TPM),
            "passphrase": _(conv.ENCRYPT_PASS),
            "none": _(conv.NOT_ENCRYPTED),
        }[method]
    if user.get("username"):
        entered = f" · {_(conv.ENTERED_TEXT)}" if user.get("password") == plans.ENTERED else ""
        who = f"{user['username']}@{user.get('hostname')}"
        out["user"] = f"{user.get('full_name')} ({who}){entered}"
    if loc.get("timezone"):
        out["time"] = f"{loc['timezone']} · {(loc.get('region') or '').split('.')[0]}"
    out["privacy"] = _(conv.CLOUD_OFF)
    return out


def disk_name(disk: dict[str, Any] | None, _: Texts) -> str | None:
    if not disk:
        return None
    if disk.get("model"):
        return _(DISK).format(
            model=disk["model"], name=disk.get("name"), size=size(disk.get("size"), _)
        )
    return _(UNNAMED).format(name=disk.get("name"), size=size(disk.get("size"), _))


def _partition(line: dict[str, Any], _: Texts) -> str:
    """nvme0n1p3 · 380 GiB · Windows, encrypted with BitLocker · «Recovery»"""
    parts = [str(line.get("name") or line.get("partition")), size(line.get("size"), _)]
    about = []
    if line.get("system"):
        about.append(str(line["system"]))
    kind = ENCRYPTION_KINDS.get(str(line.get("encrypted") or ""))
    if kind:
        about.append(_(ENCRYPTED_WITH).format(kind=kind))
    if line.get("kind") == "efi":
        about.append(_(USES["efi"]))
    if not about:
        about.append(_(EMPTY) if not line.get("holds_data") else _(UNKNOWN_FS))
    parts.append(", ".join(about))
    if line.get("label"):
        parts.append(f"«{line['label']}»")
    return " · ".join(parts)


def _use(value: Any, _: Texts) -> str:
    return _(USES.get(str(value), str(value)))


def change_text(
    change: dict[str, Any], plan: dict[str, Any], survey: dict[str, Any], _: Texts
) -> Change:
    """One line of the validator's ``changes`` in words."""
    action = change.get("action")
    # The validator's line, with what the survey knows of the partition (its encryption).
    known = _partition_in(survey, change.get("partition"))
    change = {**{k: v for k, v in known.items() if k in ("encrypted", "fstype", "kind")}, **change}
    disk = plan.get("disk") or {}
    encrypted = ((disk.get("encryption") or {}).get("method")) in ("tpm2", "passphrase")
    if action == "erase":
        if change.get("use"):
            text = _(ERASE_FOR).format(use=_use(change["use"], _), what=_partition(change, _))
        else:
            text = _(ERASE).format(what=_partition(change, _))
        return Change("erase", text, loses_data=True)
    if action == "keep":
        return Change("keep", _(KEEP).format(what=_partition(change, _)))
    if action == "shrink":
        to = size(change.get("to"), _)
        return Change("shrink", _(SHRINK).format(to=to, what=_partition(change, _)))
    if action == "use":
        return Change(
            "use", _(USE).format(use=_use(change.get("use"), _), what=_partition(change, _))
        )
    if action == "create":
        if change.get("use") and change.get("use") != "system":
            text = _(CREATE_FOR).format(
                use=_use(change["use"], _), size=size(change.get("size"), _)
            )
        else:
            template = CREATE_ENCRYPTED if encrypted else CREATE
            text = _(template).format(size=size(change.get("size"), _))
        return Change("create", text)
    if action == "manual":
        target = _find(survey, change.get("disk"))
        return Change(
            "manual", _(MANUAL_DISK).format(disk=disk_name(target, _) or change.get("disk"))
        )
    return Change(str(action), f"{action}: {change}")


def problem_text(problem: dict[str, Any], _: Texts) -> str:
    code = problem.get("code", "")
    if code in PROBLEMS:
        return _(PROBLEMS[code])
    return _(GENERIC).format(message=problem.get("message", code))


def _partition_in(survey: dict[str, Any], part_id: Any) -> dict[str, Any]:
    for disk in survey.get("disks", []):
        for part in disk.get("partitions", []):
            if part_id is not None and part.get("id") == part_id:
                return part
    return {}


def _find(survey: dict[str, Any], disk_id: Any) -> dict[str, Any] | None:
    for disk in survey.get("disks", []):
        if disk.get("id") == disk_id:
            return disk
    return None


def summarize(
    plan: dict[str, Any],
    report: dict[str, Any],
    survey: dict[str, Any],
    _: Texts,
    *,
    extra_problems: list[dict[str, Any]] | None = None,
) -> Summary:
    """The plan, its changes in the order that matters (losses first), and what blocks it.

    ``extra_problems`` are checks only the installer can make (the TPM chip);
    the recovery key and the confirmation itself are solved on the Herkos
    screen and never block reaching it.
    """
    described = describe(plan, _)
    lines = [Line(key, _(title), described.get(key)) for key, title in conv.SECTIONS.items()]
    changes = [change_text(c, plan, survey, _) for c in report.get("changes", [])]
    order = {"erase": 0, "shrink": 1, "use": 2, "create": 3, "manual": 3, "keep": 4}
    changes.sort(key=lambda c: order.get(c.action, 5))
    problems = []
    seen = set()
    for p in [*report.get("problems", []), *(extra_problems or [])]:
        if p.get("code") in SOLVED_ON_SCREEN:
            continue
        text = problem_text(p, _)
        if text not in seen:
            seen.add(text)
            problems.append(text)
    missing = [
        _(conv.SECTIONS[MISSING_SECTION[m]])
        for m in report.get("missing", [])
        if m in MISSING_SECTION
    ]
    if missing:
        problems.insert(0, _(NOT_DECIDED_YET).format(parts=", ".join(dict.fromkeys(missing))))
    warnings = [problem_text(w, _) for w in report.get("warnings", []) if w.get("code") in PROBLEMS]
    target = _find(survey, (plan.get("disk") or {}).get("target"))
    return Summary(
        lines=lines,
        changes=changes,
        problems=problems,
        warnings=warnings,
        disk=disk_name(target, _),
        erases=any(c.loses_data for c in changes),
    )
