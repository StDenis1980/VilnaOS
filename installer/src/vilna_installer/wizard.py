# SPDX-License-Identifier: GPL-3.0-or-later
"""The classic wizard without GTK: its steps, its texts and how it fills the plan.

image/docs/installer.md, section 1.7: «Without the assistant» opens the same
steps as an ordinary step-by-step wizard with the same plan. It needs no
model: the person chooses everything with ordinary controls, and the wizard
writes it with the setters of the plan's own module
(``vilna_mcp_installer.plan``), which check every value as they do for
Gloxi. The validator of that module decides at the end, the same as after
the conversation; the Herkos screen (R4) comes next.

The wizard is the only way to make a manual layout: its partition editor
(``editor_*`` here, ``wizard_window.PartitionEditor`` on the screen) writes
``disk.partitions``, which Gloxi cannot set.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vilna_mcp_installer import machine as machines
from vilna_mcp_installer import plan as plans

from vilna_installer import conversation as conv
from vilna_installer import start

GiB = plans.GiB
MiB = plans.MiB
EFI_SIZE = 512 * MiB

# Steps of the wizard, in order: the parts of the plan, then the summary.
STEPS = ("languages", "network", "disk", "encryption", "user", "time", "privacy", "summary")

# Texts (msgids of locale/*.po). New ones are in uk only for now; the
# translation thread adds the other eight.
SUMMARY = "Summary"
STEP_OF = "Step {number} of {count}: {title}"
BACK = "Back"
NEXT = "Next"
LANGUAGES_LEAD = "Which languages do you write in? The first one chosen is the main language."
MAIN_LANGUAGE = "Main language"
KEYBOARDS = "Keyboard layouts"
ANOTHER_KEYBOARD = "Another keyboard layout"
NONE = "None"
NETWORK_LEAD = "A network is not needed: everything for installing is already on the USB stick."
WIFI_NETWORKS = "Wi-Fi networks"
SCAN_AGAIN = "Look for networks again"
NO_WIFI = "No Wi-Fi networks found."
CONNECTED = "Connected"
DISK_LEAD = "Choose the disk for Vilna and what to do with it."
DISKS = "Disks"
WHAT_TO_DO = "What to do with the disk"
ERASE_AND_INSTALL = "Erase the whole disk and install Vilna"
MANUAL_HINT = "Choose the partitions yourself in the partition editor"
EMPTY_DISK = "Empty"
FREE = "{size} free"
WILL_ERASE = "Everything on this disk will be erased: {what}."
OPEN_EDITOR = "Open the partition editor"
EDITOR_TITLE = "Partition editor"
EDITOR_LEAD = (
    "Choose what each partition is for. Partitions you do not use stay as they are. "
    "New partitions go into the largest free space."
)
EXISTING = "Partitions on the disk"
NEW_PARTITIONS = "New partitions"
ADD_PARTITION = "Add a partition"
REMOVE = "Remove"
SUGGEST = "Suggest a layout"
USE = "Use"
FORMAT = "Format (erase)"
SIZE_GIB = "Size, GiB"
NOT_USED = "Not used, keep"
USE_EFI = "EFI system partition"
USE_SYSTEM = "System"
USE_HOME = "Home folders"
USE_SWAP = "Swap"
PARTITIONS_CHOSEN = "Partitions chosen: {count}"
ENCRYPTION_LEAD = "Encryption keeps your files safe if the computer or the disk is lost or stolen."
TPM_HINT = (
    "The disk opens by itself on this computer; a recovery key opens it if something "
    "changes. Recommended."
)
NO_TPM = "This computer has no TPM 2.0 chip, so this way is not available."
PASSPHRASE_HINT = "You type the passphrase every time the computer starts."
NONE_HINT = "Anyone who has the disk can read your files."
KEY_NEXT = "The next screen shows the recovery key; keep it safe."
FULL_NAME = "Your name"
USERNAME = "Login name"
HOSTNAME = "Computer name"
TIME_ZONE = "Time zone"
REGION = "Date and number formats"
PRIVACY_LEAD = (
    "Gloxi works right on this computer, and what you tell it stays here. A cloud model is "
    "off now; you can choose it after the first login, and then the privacy gateway hides "
    "names and numbers before anything leaves the computer."
)
DISK_CHANGES = "What happens to the disk"
STILL_TO_DO = "Still to do"
READY = "The plan is ready. The next screen, from Herkos, shows exactly what will change."
NEEDED_TEXT = "Fill in this step."
CHANGE_ERASE = "{name} ({size}{what}) will be erased"
CHANGE_KEEP = "{name} ({size}{what}) stays as it is"
CHANGE_USE = "{name} ({size}) is used as {use}, its files stay"
CHANGE_CREATE = "A new partition of {size} is created{use}"
CHANGE_MANUAL = "Partitions are chosen in the partition editor"
NAME_NEEDED = "Type your name."
BAD_USERNAME = (
    "A login name starts with a Latin letter and has only small Latin letters, digits, «-» and «_»."
)
TAKEN_USERNAME = "The system already uses this login name; choose another."
BAD_HOSTNAME = "A computer name has only small Latin letters, digits and «-»."
CHOOSE_DISK = "Choose a disk."
CHOOSE = "Choose one of the options."
WIFI_NOT_CONNECTED = "Connect to the Wi-Fi network first, or choose another way."

USE_TEXT = {
    None: NOT_USED,
    "efi": USE_EFI,
    "system": USE_SYSTEM,
    "home": USE_HOME,
    "swap": USE_SWAP,
}

# The validator's codes (vilna_mcp_installer.plan) in words for the person.
PROBLEM_TEXT = {
    "wifi_password": "Type the Wi-Fi password and connect.",
    "user_password": "Type the password for the account.",
    "passphrase": "Type the passphrase for the disk.",
    "disks_changed": "The disks changed (a disk added or removed?). Choose the disk again.",
    "no_target": "The chosen disk is not in the computer now. Choose the disk again.",
    "boot_medium": "This is the USB stick Vilna is running from. Choose another disk.",
    "read_only": "This disk is read-only. Choose another disk.",
    "too_small": "There is not enough room: Vilna needs at least 40 GiB.",
    "not_enough_space": "There is not enough free space for these partitions.",
    "efi_too_small": "The EFI system partition needs at least 256 MiB.",
    "needs_manual": (
        "This disk has RAID, LVM or a filesystem Vilna does not know: only the partition "
        "editor can use it."
    ),
    "manual_needs_editor": "Choose the partitions in the partition editor.",
    "needs_one_efi": "Choose exactly one EFI system partition.",
    "needs_one_system": "Choose exactly one partition for the system.",
    "must_format": "The system and swap partitions are always formatted.",
    "not_efi": "This partition is not an EFI system partition; format it or choose another.",
    "home_unreadable": (
        "This partition cannot be kept for home folders; format it or choose another."
    ),
    "home_not_linux": "Only a Linux partition can be kept for home folders; format it.",
    "partition_twice": "A partition is chosen twice.",
    "one_home": "Choose at most one partition for home folders.",
    "one_swap": "Choose at most one swap partition.",
    "later_stage": "This comes in a later version of Vilna.",
    "reserved_username": TAKEN_USERNAME,
    "bad_username": BAD_USERNAME,
    "bad_hostname": BAD_HOSTNAME,
}
# Settled on the Herkos screen that follows (alpha item 17), not in the wizard.
LATER_SCREEN = frozenset({"recovery_key", "confirmed_invalid"})

TEXTS = (
    SUMMARY,
    STEP_OF,
    BACK,
    NEXT,
    LANGUAGES_LEAD,
    MAIN_LANGUAGE,
    KEYBOARDS,
    ANOTHER_KEYBOARD,
    NONE,
    NETWORK_LEAD,
    WIFI_NETWORKS,
    SCAN_AGAIN,
    NO_WIFI,
    CONNECTED,
    DISK_LEAD,
    DISKS,
    WHAT_TO_DO,
    ERASE_AND_INSTALL,
    MANUAL_HINT,
    EMPTY_DISK,
    FREE,
    WILL_ERASE,
    OPEN_EDITOR,
    EDITOR_TITLE,
    EDITOR_LEAD,
    EXISTING,
    NEW_PARTITIONS,
    ADD_PARTITION,
    REMOVE,
    SUGGEST,
    USE,
    FORMAT,
    SIZE_GIB,
    NOT_USED,
    USE_EFI,
    USE_SYSTEM,
    USE_HOME,
    USE_SWAP,
    PARTITIONS_CHOSEN,
    ENCRYPTION_LEAD,
    TPM_HINT,
    NO_TPM,
    PASSPHRASE_HINT,
    NONE_HINT,
    KEY_NEXT,
    FULL_NAME,
    USERNAME,
    HOSTNAME,
    TIME_ZONE,
    REGION,
    PRIVACY_LEAD,
    DISK_CHANGES,
    STILL_TO_DO,
    READY,
    NEEDED_TEXT,
    CHANGE_ERASE,
    CHANGE_KEEP,
    CHANGE_USE,
    CHANGE_CREATE,
    CHANGE_MANUAL,
    NAME_NEEDED,
    CHOOSE_DISK,
    CHOOSE,
    WIFI_NOT_CONNECTED,
    *(t for t in PROBLEM_TEXT.values() if t not in (TAKEN_USERNAME, BAD_USERNAME, BAD_HOSTNAME)),
    BAD_USERNAME,
    TAKEN_USERNAME,
    BAD_HOSTNAME,
)

# Layouts that type Latin letters; with none of them the wizard adds «us».
NON_LATIN = frozenset({"ua", "ru", "by", "bg", "mk", "rs", "gr", "il", "ge", "am", "ara", "ir"})

# Ukrainian letters in Latin, the official transliteration (Cabinet of
# Ministers resolution 55 of 2010), for suggesting a login name; other
# letters lose their marks.
UK_LATIN = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "h",
    "ґ": "g",
    "д": "d",
    "е": "e",
    "є": "ie",
    "ж": "zh",
    "з": "z",
    "и": "y",
    "і": "i",
    "ї": "i",
    "й": "i",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ь": "",
    "ю": "iu",
    "я": "ia",
    "'": "",
    "’": "",
    "ʼ": "",
    # Latin letters that do not split into a letter and a mark (NFKD).
    "ł": "l",
    "ø": "o",
    "đ": "d",
    "ß": "ss",
    "æ": "ae",
    "œ": "oe",
    "þ": "th",
}


def latin(text: str) -> str:
    """Small Latin letters and digits of a name: «Олена Ковальчук» → «olena kovalchuk»."""
    out = []
    for ch in text.casefold():
        if ch in UK_LATIN:
            out.append(UK_LATIN[ch])
            continue
        base = unicodedata.normalize("NFKD", ch)
        out.append("".join(c for c in base if c.isascii() and (c.isalnum() or c == " ")))
    return " ".join("".join(out).split())


def suggest_username(full_name: str) -> str:
    """The first word of the name in Latin letters, if it makes a valid login name."""
    words = latin(full_name).split()
    name = words[0][:31] if words else ""
    if not name or not plans.USERNAME_RE.match(name) or name in plans.RESERVED_USERS:
        return ""
    return name


def suggest_hostname(username: str) -> str:
    return f"{username}-vilna" if username else "vilna"


def keyboards_for(locales: list[str], extra: str | None = None) -> list[str]:
    """The keyboard of each language, «us» when none types Latin, and one more if chosen."""
    out: list[str] = []
    for loc in locales:
        code = loc.split("_")[0].split(".")[0]
        kbd = machines.KEYBOARD_OF.get(code)
        if kbd and kbd not in out:
            out.append(kbd)
    if not any(k.split("(")[0] not in NON_LATIN for k in out):
        out.append("us")
    if extra and extra not in out:
        out.append(extra)
    return out


def size_text(n: int) -> str:
    if n >= 1000 * GiB:
        return f"{n / (1024 * GiB):.1f} TiB"
    if n >= GiB:
        return f"{n / GiB:.0f} GiB"
    return f"{n / MiB:.0f} MiB"


def disk_title(disk: dict[str, Any]) -> str:
    """«Demo SSD 512 · 512 GiB»; the model is external text (shown, never markup)."""
    model = disk.get("model") or disk.get("name") or disk.get("id")
    return f"{model} · {size_text(disk.get('size', 0))}"


def disk_subtitle(disk: dict[str, Any], _: start.Texts) -> str:
    parts = []
    if disk.get("empty"):
        parts.append(_(EMPTY_DISK))
    parts.extend(disk.get("systems") or [])
    encrypted = sorted({p["encrypted"] for p in disk.get("partitions", []) if p.get("encrypted")})
    parts.extend(e.capitalize() if e != "bitlocker" else "BitLocker" for e in encrypted)
    if not disk.get("empty") and disk.get("largest_free", 0) >= GiB:
        parts.append(_(FREE).format(size=size_text(disk["largest_free"])))
    return " · ".join([disk.get("name", ""), *parts])


def erased_by_replace(disk: dict[str, Any]) -> str | None:
    """What «Erase the whole disk» loses, in a few words; None when nothing."""
    what = list(disk.get("systems") or [])
    holds = [p for p in disk.get("partitions", []) if p.get("holds_data")]
    if holds and not what:
        what = [p.get("label") or p.get("name") for p in holds][:3]
    return ", ".join(w for w in what if w) or None


def partition_title(part: dict[str, Any]) -> str:
    what = [part.get("label"), part.get("system"), part.get("fstype")]
    words = " · ".join(dict.fromkeys(w for w in what if w))
    return f"{part.get('name')} · {size_text(part.get('size', 0))}" + (
        f" · {words}" if words else ""
    )


def change_text(change: dict[str, Any], _: start.Texts) -> str:
    """One line of the validator's ``changes`` in words for the person."""
    action = change.get("action")
    size = size_text(change.get("size") or 0)
    what = change.get("system") or change.get("label") or ""
    what = f", {what}" if what else ""
    use = _(USE_TEXT.get(change.get("use"), NOT_USED))
    if action == "erase":
        return _(CHANGE_ERASE).format(name=change.get("name"), size=size, what=what)
    if action == "keep":
        return _(CHANGE_KEEP).format(name=change.get("name"), size=size, what=what)
    if action == "use":
        return _(CHANGE_USE).format(name=change.get("name"), size=size, use=use)
    if action == "create":
        return _(CHANGE_CREATE).format(size=size, use=f": {use}" if change.get("use") else "")
    return _(CHANGE_MANUAL)


def problem_text(problem: dict[str, str], _: start.Texts) -> str:
    text = PROBLEM_TEXT.get(problem.get("code", ""))
    return _(text) if text else problem.get("message", "")


@dataclass
class EditorRow:
    """One row of the partition editor: an existing partition or a new one."""

    use: str | None
    partition: str | None = None
    format: bool = False
    size: int = 0


def editor_rows(plan: dict[str, Any], disk: dict[str, Any]) -> list[EditorRow]:
    """Every partition of the disk with its use from the plan, then the new ones."""
    chosen = (plan.get("disk") or {}).get("partitions") or []
    by_id = {c["partition"]: c for c in chosen if c.get("partition")}
    rows = []
    for part in disk.get("partitions", []):
        c = by_id.get(part["id"])
        rows.append(
            EditorRow(
                c["use"] if c else None,
                part["id"],
                bool(c and c.get("format")),
                part.get("size", 0),
            )
        )
    rows.extend(
        EditorRow(c["use"], None, True, c["size"]) for c in chosen if not c.get("partition")
    )
    return rows


def editor_suggest(disk: dict[str, Any]) -> list[EditorRow]:
    """The usual layout in the free space: the EFI partition already there, else a new one."""
    rows = [EditorRow(None, p["id"], False, p.get("size", 0)) for p in disk.get("partitions", [])]
    free = disk.get("largest_free", 0)
    efi = next(
        (
            r
            for r, p in zip(rows, disk.get("partitions", []), strict=True)
            if p.get("kind") == "efi"
        ),
        None,
    )
    if efi is not None:
        efi.use = "efi"
    else:
        rows.append(EditorRow("efi", None, True, EFI_SIZE))
        free -= EFI_SIZE
    # Whole GiB, so that the spin button shows exactly what is written.
    rows.append(EditorRow("system", None, True, max(free // GiB, 0) * GiB))
    return rows


def editor_partitions(rows: list[EditorRow]) -> list[dict[str, Any]]:
    """The rows as ``disk.partitions``: unused partitions and empty new rows are left out."""
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.use is None:
            continue
        if row.partition is not None:
            fmt = row.format or row.use in ("system", "swap")
            out.append({"use": row.use, "partition": row.partition, "format": fmt})
        elif row.size > 0:
            out.append({"use": row.use, "size": row.size})
    return out


class Wizard:
    """The plan file, the computer and the steps; the window only shows them."""

    def __init__(self, plan_path: Path, machine: machines.Machine, *, stage: str = "alpha"):
        self.plan_path = plan_path
        self.machine = machine
        self.stage = stage
        self._survey: dict[str, Any] | None = None
        self._hardware: dict[str, Any] | None = None
        self.seen: set[str] = set()  # steps the person has looked at

    # reading

    @property
    def plan(self) -> dict[str, Any]:
        return plans.load(self.plan_path)

    def survey(self, *, again: bool = False) -> dict[str, Any]:
        if self._survey is None or again:
            try:
                self._survey = machines.survey(self.machine.disks())
            except machines.MachineError:
                self._survey = {"fingerprint": None, "disks": []}
        return self._survey

    def disks(self) -> list[dict[str, Any]]:
        """The disks Vilna can go on: not the USB stick it runs from."""
        return [d for d in self.survey()["disks"] if not d.get("boot_medium")]

    def target(self) -> dict[str, Any] | None:
        disk_id = (self.plan.get("disk") or {}).get("target")
        return next((d for d in self.disks() if d["id"] == disk_id), None)

    def tpm(self) -> bool:
        if self._hardware is None:
            self._hardware = self.machine.hardware()
        return self._hardware.get("tpm") == 2

    def networks(self) -> list[dict[str, Any]]:
        try:
            return self.machine.networks()
        except machines.MachineError:
            return []

    def wired(self) -> bool:
        return self.machine.wired()

    def keyboards(self) -> list[dict[str, str]]:
        return self.machine.keyboards()

    def ui_locale(self) -> str:
        return (self.plan.get("ui") or {}).get("locale") or "en_US.UTF-8"

    def language_choices(self) -> list[tuple[str, str]]:
        """(locale, own name): the ten languages, and the person's own when it is another."""
        rows = [(lang.locale, lang.name) for lang in start.LANGUAGES]
        ui = self.ui_locale()
        if ui not in start.MAIN_LOCALES:
            rows.insert(0, (ui, ui.split(".")[0]))
        return rows

    def chosen_languages(self) -> list[str]:
        loc = self.plan.get("locale") or {}
        return list(loc.get("languages") or [self.ui_locale()])

    def suggested_time(self) -> tuple[str, str]:
        main = self.chosen_languages()[0]
        zone = machines.TIMEZONE_OF.get(main.split(".")[0], "Europe/Brussels")
        loc = self.plan.get("locale") or {}
        return loc.get("timezone") or zone, loc.get("region") or main

    # writing: None when saved, else what to tell the person

    def _change(self, fn, *args: Any, **kwargs: Any) -> str | None:
        plan = self.plan
        try:
            fn(plan, *args, **kwargs)
        except plans.PlanError as e:
            return str(e)
        plans.save(plan, self.plan_path)
        return None

    def set_languages(self, locales: list[str], extra_keyboard: str | None) -> str | None:
        if not locales:
            return CHOOSE
        return self._change(plans.set_languages, locales, keyboards_for(locales, extra_keyboard))

    def set_network(self, mode: str, ssid: str | None = None, secured: bool = False) -> str | None:
        return self._change(plans.set_network, mode, ssid, secured)

    def set_disk(self, target: str, layout: str) -> str | None:
        return self._change(plans.set_disk, target, layout, self.survey())

    def set_partitions(self, rows: list[EditorRow]) -> str | None:
        return self._change(plans.set_partitions, self.survey(), editor_partitions(rows))

    def set_encryption(self, method: str) -> str | None:
        return self._change(plans.set_encryption, method)

    def set_user(self, full_name: str, username: str, hostname: str) -> str | None:
        if not full_name.strip():
            return NAME_NEEDED
        if not plans.USERNAME_RE.match(username):
            return BAD_USERNAME
        if username in plans.RESERVED_USERS or username.startswith("gloxi-"):
            return TAKEN_USERNAME
        if not plans.HOSTNAME_RE.match(hostname):
            return BAD_HOSTNAME
        return self._change(plans.set_user, full_name, username, hostname)

    def set_time(self, timezone: str, region: str) -> str | None:
        return self._change(plans.set_time, timezone, region)

    def mark_entered(self, which: str) -> None:
        plan = self.plan
        plans.mark_entered(plan, which)
        plans.save(plan, self.plan_path)

    # the end

    def report(self) -> dict[str, Any]:
        return plans.validate(self.plan, self.survey(), stage=self.stage)

    def to_do(self, report: dict[str, Any] | None = None) -> list[dict[str, str]]:
        """What still blocks the plan here; the Herkos screen settles the rest."""
        report = report or self.report()
        rows = [p for p in report["problems"] if p["code"] not in LATER_SCREEN]
        rows += [{"code": "missing", "field": m, "message": m} for m in report["missing"]]
        return rows

    def done(self, key: str) -> bool:
        """Whether a step is decided (the sidebar ticks it)."""
        plan = self.plan
        loc = plan.get("locale") or {}
        disk = plan.get("disk") or {}
        enc = disk.get("encryption") or {}
        user = plan.get("user") or {}
        net = plan.get("network") or {}
        if key == "languages":
            return bool(loc.get("languages") and loc.get("keyboards"))
        if key == "network":
            return bool(net.get("mode")) and net.get("secret") != plans.NEEDED
        if key == "disk":
            if not (disk.get("target") and disk.get("layout")):
                return False
            return disk["layout"] != "manual" or bool(disk.get("partitions"))
        if key == "encryption":
            return enc.get("method") in ("tpm2", "none") or (
                enc.get("method") == "passphrase" and enc.get("passphrase") == plans.ENTERED
            )
        if key == "user":
            return bool(user.get("username")) and user.get("password") == plans.ENTERED
        if key == "time":
            return bool(loc.get("timezone") and loc.get("region"))
        if key == "privacy":
            return key in self.seen
        return not self.to_do()

    def first_open(self) -> str:
        return next((key for key in STEPS if not self.done(key)), "summary")


def section_title(key: str) -> str:
    return SUMMARY if key == "summary" else conv.SECTIONS[key]


def machine_for(demo: str | None) -> machines.Machine:
    return machines.DemoMachine(demo) if demo else machines.Machine()
