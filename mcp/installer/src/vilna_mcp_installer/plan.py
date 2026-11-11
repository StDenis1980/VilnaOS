# SPDX-License-Identifier: Apache-2.0
"""The installation plan: its format, the changes Gloxi may make, and the validator.

The plan is the one result of the conversation (installer.md, section 4) and
of the classic wizard alike. Gloxi only fills it; a deterministic engine
installs only a plan that this validator passed and that the person
confirmed on the Herkos screen (section 1).

Three kinds of fields:

- **Gloxi may set** them through the MCP server (``SETTERS``): languages,
  keyboards, network, disk, encryption method, account names, time.
- **Only the person** sets them, in the installer's own protected fields or
  on the Herkos screen, never through the model: the markers that a secret
  was entered (``network.secret``, ``user.password``,
  ``disk.encryption.passphrase``), ``disk.encryption.recovery_key_acknowledged``,
  ``disk.shrink.backup_confirmed`` and ``confirmed_by_user``. A plan holds
  only the marker ``"entered"``, never a password or a key. The partitions
  of a manual layout (``disk.partitions``, ``set_partitions``) come only
  from the partition editor of the classic wizard, where the person sees
  every partition they choose.
- **Fixed** for the installation: ``privacy.cloud`` is ``"disabled"``; the
  choice of a cloud model comes after the first login (section 3, step 11).

The validator does not depend on the model and does not trust it: it checks
the plan against the computer as surveyed now (``machine.survey``), and its
rules are those of section 5.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import zoneinfo
from collections.abc import Callable
from pathlib import Path
from typing import Any

VERSION = 1
GiB = 1 << 30
# What Vilna needs on disk: the image, the model in it (ADR 0025) and room to grow.
MIN_SYSTEM = 40 * GiB
ENTERED = "entered"
NEEDED = "needed"

LAYOUTS = ("replace", "alongside", "free_space", "manual")
# installer.md, section 8: the alpha takes the simple path only. Next to
# Windows, shrinking and moving files come in the beta, after testers.
STAGE_LAYOUTS = {"alpha": ("replace", "manual"), "beta": LAYOUTS}
ENCRYPTION = ("tpm2", "passphrase", "none")
# What a partition of a manual layout is for: the EFI system partition, the
# system itself (the image is written on it), the home folders, swap.
USES = ("efi", "system", "home", "swap")
MiB = 1 << 20
MIN_EFI = 256 * MiB
MAX_PARTITIONS = 8
NETWORK = ("offline", "wired", "wifi")
TOP_LEVEL = (
    "version",
    "ui",
    "assistant",
    "locale",
    "network",
    "disk",
    "migrate",
    "user",
    "privacy",
    "confirmed_by_user",
)
# Steps of the conversation (installer.md, section 3) that put something in the plan.
STEP_FIELDS = {
    "languages": 2,
    "network": 3,
    "disk": 5,
    "encryption": 8,
    "user": 9,
    "time": 10,
}

LOCALE_RE = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?\.UTF-8(@[a-z]+)?$")
KEYBOARD_RE = re.compile(r"^[a-z]{2,8}(\([a-z0-9_-]{1,24}\))?$")
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,30}$")
HOSTNAME_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
# Names the system already uses (ADR 0025 and the usual system accounts).
RESERVED_USERS = frozenset(
    {
        "root",
        "bin",
        "daemon",
        "nobody",
        "messagebus",
        "polkitd",
        "gdm",
        "vilna-llm",
        "herkos",
        "admin",
        "live",
        "linux",
        "systemd-network",
        "systemd-resolve",
        "systemd-timesync",
    }
)
MAX_TEXT = 128


class PlanError(Exception):
    """A change the plan does not take; the message says what is allowed."""


def new_plan() -> dict[str, Any]:
    return {
        "version": VERSION,
        "ui": {"language": "en", "locale": "en_US.UTF-8", "translated": True},
        "assistant": True,
        "privacy": {"cloud": "disabled"},
        "confirmed_by_user": False,
    }


# --- reading and writing ------------------------------------------------------


def load(path: Path) -> dict[str, Any]:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return new_plan()
    except (OSError, ValueError) as e:
        raise PlanError(f"cannot read the plan {path}: {e}") from e
    if not isinstance(plan, dict):
        raise PlanError(f"the plan {path} is not a JSON object")
    plan.setdefault("version", VERSION)
    plan.setdefault("privacy", {"cloud": "disabled"})
    plan.setdefault("confirmed_by_user", False)
    return plan


def save(plan: dict[str, Any], path: Path) -> None:
    """Atomically, readable by the person only (the plan holds their names)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".plan-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def public(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan as the model may see it: the same, there are no secrets in it."""
    return copy.deepcopy(plan)


# --- changes Gloxi may make ------------------------------------------------------


def _text(value: Any, name: str, *, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"'{name}' must be a non-empty string")
    value = " ".join(value.split())
    if len(value) > limit:
        raise PlanError(f"'{name}' is longer than {limit} characters")
    return value


def _strings(value: Any, name: str, pattern: re.Pattern[str], *, most: int = 8) -> list[str]:
    if not isinstance(value, list) or not value:
        raise PlanError(f"'{name}' must be a non-empty list")
    if len(value) > most:
        raise PlanError(f"'{name}' takes at most {most} items")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not pattern.match(item):
            raise PlanError(f"'{item}' is not a valid item of '{name}'")
        if item not in out:
            out.append(item)
    return out


def _unconfirm(plan: dict[str, Any]) -> None:
    # Any change after the Herkos screen means the person must look again.
    plan["confirmed_by_user"] = False


def set_languages(
    plan: dict[str, Any], languages: list[str], keyboards: list[str], **_: Any
) -> dict[str, Any]:
    """Languages of the system (locales; the first is the main one) and keyboard layouts."""
    langs = _strings(languages, "languages", LOCALE_RE)
    kbds = _strings(keyboards, "keyboards", KEYBOARD_RE)
    loc = plan.setdefault("locale", {})
    loc["languages"], loc["keyboards"] = langs, kbds
    loc.setdefault("region", langs[0])
    _unconfirm(plan)
    return loc


def set_network(
    plan: dict[str, Any],
    mode: str,
    ssid: str | None = None,
    secured: bool = False,
    **_: Any,
) -> dict[str, Any]:
    """Offline, wired or a Wi-Fi network; its password goes into a protected field."""
    if mode not in NETWORK:
        raise PlanError(f"'mode' must be one of {list(NETWORK)}")
    net: dict[str, Any] = {"mode": mode}
    if mode == "wifi":
        net["wifi"] = _text(ssid, "ssid", limit=64)
        if secured:
            old = plan.get("network") or {}
            same = old.get("wifi") == net["wifi"] and old.get("secret") == ENTERED
            net["secret"] = ENTERED if same else NEEDED
    elif ssid:
        raise PlanError("'ssid' is only for mode 'wifi'")
    plan["network"] = net
    _unconfirm(plan)
    return net


def set_disk(
    plan: dict[str, Any],
    target: str,
    layout: str,
    survey: dict[str, Any],
    keep_partitions: list[str] | None = None,
    erase_partitions: list[str] | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Which disk and what to do with it; the survey's fingerprint goes along."""
    disk = _find_disk(survey, target)
    if disk is None:
        ids = [d["id"] for d in survey.get("disks", []) if not d.get("boot_medium")]
        raise PlanError(f"no disk {target!r} in the survey; disks: {ids}")
    if layout not in LAYOUTS:
        raise PlanError(f"'layout' must be one of {list(LAYOUTS)}")
    old = plan.get("disk") or {}
    new: dict[str, Any] = {
        "target": disk["id"],
        "layout": layout,
        "survey": survey.get("fingerprint"),
    }
    known = {p["id"] for p in disk.get("partitions", [])}
    for key, value in (
        ("keep_partitions", keep_partitions),
        ("erase_partitions", erase_partitions),
    ):
        if value:
            bad = [v for v in value if v not in known]
            if bad:
                raise PlanError(f"no partitions {bad} on {disk['id']}; partitions: {sorted(known)}")
            new[key] = list(dict.fromkeys(value))
    same = old.get("target") == disk["id"] and old.get("survey") == survey.get("fingerprint")
    if layout == "manual" and old.get("layout") == "manual" and same and old.get("partitions"):
        new["partitions"] = old["partitions"]
    if old.get("encryption") and old.get("target") == disk["id"]:
        new["encryption"] = old["encryption"]
    elif old.get("encryption"):
        # Another disk: keep the method, but the recovery key must be confirmed again.
        new["encryption"] = {"method": old["encryption"].get("method", "tpm2")}
    plan["disk"] = new
    _unconfirm(plan)
    return new


def set_encryption(plan: dict[str, Any], method: str, **_: Any) -> dict[str, Any]:
    """TPM2 with a recovery key (recommended), a passphrase, or no encryption."""
    if method not in ENCRYPTION:
        raise PlanError(f"'method' must be one of {list(ENCRYPTION)}")
    disk = plan.setdefault("disk", {})
    old = disk.get("encryption") or {}
    enc: dict[str, Any] = {"method": method}
    if method != "none" and old.get("method") == method:
        for key in ("recovery_key_acknowledged", "passphrase"):
            if key in old:
                enc[key] = old[key]
    if method == "passphrase" and enc.get("passphrase") != ENTERED:
        enc["passphrase"] = NEEDED
    disk["encryption"] = enc
    _unconfirm(plan)
    return enc


def set_user(
    plan: dict[str, Any], full_name: str, username: str, hostname: str, **_: Any
) -> dict[str, Any]:
    """The person's name, login name and the computer's name; the password is separate."""
    name = _text(full_name, "full_name")
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        raise PlanError(
            "'username' must start with a Latin letter and have only lower-case Latin "
            "letters, digits, '-' and '_' (at most 31)"
        )
    if username in RESERVED_USERS or username.startswith("gloxi-"):
        raise PlanError(f"the system already uses the name {username!r}; choose another")
    if not isinstance(hostname, str) or not HOSTNAME_RE.match(hostname):
        raise PlanError(
            "'hostname' must have only lower-case Latin letters, digits and '-' (at most 63), "
            "and not start or end with '-'"
        )
    old = plan.get("user") or {}
    user = {"full_name": name, "username": username, "hostname": hostname}
    user["password"] = old.get("password") if old.get("password") == ENTERED else NEEDED
    plan["user"] = user
    _unconfirm(plan)
    return user


def set_time(plan: dict[str, Any], timezone: str, region: str, **_: Any) -> dict[str, Any]:
    """Time zone (Area/City) and the region whose date and number formats to use."""
    if not isinstance(timezone, str) or timezone not in zoneinfo.available_timezones():
        raise PlanError(f"unknown time zone {timezone!r}; use Area/City such as Europe/Kyiv")
    if not isinstance(region, str) or not LOCALE_RE.match(region):
        raise PlanError(f"region {region!r} must be a locale such as uk_UA.UTF-8")
    loc = plan.setdefault("locale", {})
    loc["timezone"], loc["region"] = timezone, region
    _unconfirm(plan)
    return {"timezone": timezone, "region": region}


def set_partitions(
    plan: dict[str, Any], survey: dict[str, Any], partitions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The partitions of a manual layout, from the classic wizard's partition editor.

    Each item is for one use (``USES``) and is either a partition of the
    target disk (``partition``: its id; ``format``: whether it is erased)
    or a new one in the free space (``size`` in bytes). Not in ``SETTERS``:
    Gloxi never chooses partitions, the person does, seeing every one.
    The checks that need the whole plan are the validator's (``_check_manual``).
    """
    disk = plan.get("disk") or {}
    if disk.get("layout") != "manual":
        raise PlanError("partitions are only for the manual layout; choose it first")
    target = _find_disk(survey, disk.get("target"))
    if target is None:
        raise PlanError("choose the disk again: it is not in the survey")
    if not isinstance(partitions, list) or not partitions:
        raise PlanError("'partitions' must be a non-empty list")
    if len(partitions) > MAX_PARTITIONS:
        raise PlanError(f"at most {MAX_PARTITIONS} partitions")
    known = {p["id"] for p in target.get("partitions", [])}
    out: list[dict[str, Any]] = []
    for item in partitions:
        if not isinstance(item, dict) or item.get("use") not in USES:
            raise PlanError(f"each partition needs 'use', one of {list(USES)}")
        row: dict[str, Any] = {"use": item["use"]}
        if item.get("partition") is not None:
            if item["partition"] not in known:
                raise PlanError(f"no partition {item['partition']!r} on {target['id']}")
            if "size" in item and item["size"] is not None:
                raise PlanError("a partition is either an existing one or a new 'size', not both")
            row["partition"] = item["partition"]
            row["format"] = item.get("format") is True
        else:
            size = item.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                raise PlanError("a new partition needs a 'size' in bytes")
            row["size"] = size
        out.append(row)
    disk["partitions"] = out
    plan["disk"] = disk
    _unconfirm(plan)
    return out


SETTERS: dict[str, Callable[..., dict[str, Any]]] = {
    "set_languages": set_languages,
    "set_network": set_network,
    "set_disk": set_disk,
    "set_encryption": set_encryption,
    "set_user": set_user,
    "set_time": set_time,
}


# --- what only the person sets (installer window, Herkos screen) ---------------

SECRET_FIELDS = {
    "network": ("network", "secret"),
    "user": ("user", "password"),
    "passphrase": ("disk", "encryption", "passphrase"),
}


def mark_entered(plan: dict[str, Any], which: str) -> None:
    """The person typed this secret into a protected field; the plan keeps only that fact."""
    if which not in SECRET_FIELDS:
        raise PlanError(f"unknown secret {which!r}")
    *parents, key = SECRET_FIELDS[which]
    node = plan
    for part in parents:
        node = node.setdefault(part, {})
    node[key] = ENTERED
    _unconfirm(plan)


def acknowledge_recovery_key(plan: dict[str, Any]) -> None:
    """The person said they saved the recovery key (shown by the engine, never by Gloxi)."""
    enc = plan.setdefault("disk", {}).setdefault("encryption", {"method": "tpm2"})
    enc["recovery_key_acknowledged"] = True
    _unconfirm(plan)


# --- the validator ------------------------------------------------------------------


def _find_disk(survey: dict[str, Any], disk_id: Any) -> dict[str, Any] | None:
    for disk in survey.get("disks", []):
        if disk.get("id") == disk_id:
            return disk
    return None


def _size(n: int) -> str:
    return f"{n / GiB:.0f} GiB" if n >= GiB else f"{n / (1 << 20):.0f} MiB"


class _Report:
    def __init__(self) -> None:
        self.problems: list[dict[str, str]] = []
        self.warnings: list[dict[str, str]] = []
        self.missing: list[str] = []
        self.changes: list[dict[str, Any]] = []

    def error(self, code: str, field: str, message: str) -> None:
        self.problems.append({"code": code, "field": field, "message": message})

    def warn(self, code: str, field: str, message: str) -> None:
        self.warnings.append({"code": code, "field": field, "message": message})


def validate(plan: Any, survey: dict[str, Any], *, stage: str = "alpha") -> dict[str, Any]:
    """Check the plan against the computer as it is now.

    Returns ``valid`` (nothing blocks installing), ``problems`` (what blocks
    it), ``warnings``, ``missing`` (steps not decided yet: the conversation
    uses them) and ``changes``: every partition that is erased, kept, shrunk
    or created, one line each, for the Herkos confirmation screen.
    """
    r = _Report()
    if not isinstance(plan, dict):
        r.error("not_a_plan", "", "the plan must be a JSON object")
        return _result(r)
    if plan.get("version") != VERSION:
        r.error("version", "version", f"only plan version {VERSION} is known")
    for key in plan:
        if key not in TOP_LEVEL:
            r.error("unknown_field", key, f"unknown field {key!r}; fields: {list(TOP_LEVEL)}")
    _check_secrets(plan, r, "")
    _check_locale(plan.get("locale"), r)
    _check_network(plan.get("network"), r)
    _check_disk(plan, survey, r, stage)
    _check_user(plan.get("user"), r)
    privacy = plan.get("privacy")
    if not isinstance(privacy, dict) or privacy.get("cloud") != "disabled":
        r.error(
            "cloud_during_install",
            "privacy.cloud",
            "the cloud stays off while installing; the person chooses after the first login",
        )
    if plan.get("confirmed_by_user") is True and r.problems:
        r.error(
            "confirmed_invalid", "confirmed_by_user", "a plan with problems cannot be confirmed"
        )
    return _result(r)


def _result(r: _Report) -> dict[str, Any]:
    return {
        "valid": not r.problems and not r.missing,
        "problems": r.problems,
        "warnings": r.warnings,
        "missing": r.missing,
        "changes": r.changes,
    }


SECRET_KEYS = ("secret", "password", "passphrase", "key", "recovery_key", "psk")


def _check_secrets(node: Any, r: _Report, path: str) -> None:
    """A secret's value must never be in the plan: only the markers."""
    if isinstance(node, dict):
        for key, value in node.items():
            sub = f"{path}.{key}" if path else str(key)
            if key in SECRET_KEYS and value not in (ENTERED, NEEDED, None):
                r.error("secret_in_plan", sub, "a plan holds only the marker 'entered', no secret")
            else:
                _check_secrets(value, r, sub)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _check_secrets(value, r, f"{path}[{i}]")


def _check_locale(loc: Any, r: _Report) -> None:
    if not isinstance(loc, dict) or not loc.get("languages") or not loc.get("keyboards"):
        r.missing.append("languages")
    else:
        for key, pattern in (("languages", LOCALE_RE), ("keyboards", KEYBOARD_RE)):
            items = loc.get(key)
            if not isinstance(items, list) or not all(
                isinstance(i, str) and pattern.match(i) for i in items
            ):
                r.error("bad_" + key, f"locale.{key}", f"'{key}' has an invalid item")
    if not isinstance(loc, dict) or not loc.get("timezone") or not loc.get("region"):
        r.missing.append("time")
        return
    if loc["timezone"] not in zoneinfo.available_timezones():
        r.error("bad_timezone", "locale.timezone", f"unknown time zone {loc['timezone']!r}")
    if not isinstance(loc["region"], str) or not LOCALE_RE.match(loc["region"]):
        r.error("bad_region", "locale.region", "the region must be a locale such as uk_UA.UTF-8")


def _check_network(net: Any, r: _Report) -> None:
    if not isinstance(net, dict) or net.get("mode") not in NETWORK:
        r.missing.append("network")
        return
    if net["mode"] == "wifi" and net.get("secret") == NEEDED:
        r.error(
            "wifi_password",
            "network.secret",
            "the Wi-Fi password is not entered yet; the person types it in the protected field",
        )


def _check_user(user: Any, r: _Report) -> None:
    if not isinstance(user, dict) or not all(
        user.get(k) for k in ("full_name", "username", "hostname")
    ):
        r.missing.append("user")
        return
    if not isinstance(user["username"], str) or not USERNAME_RE.match(user["username"]):
        r.error("bad_username", "user.username", "invalid login name")
    elif user["username"] in RESERVED_USERS or user["username"].startswith("gloxi-"):
        r.error("reserved_username", "user.username", "the system already uses this name")
    if not isinstance(user["hostname"], str) or not HOSTNAME_RE.match(user["hostname"]):
        r.error("bad_hostname", "user.hostname", "invalid computer name")
    if user.get("password") != ENTERED:
        r.error(
            "user_password",
            "user.password",
            "the password is not entered yet; the person types it in the protected field",
        )


def _check_disk(plan: dict[str, Any], survey: dict[str, Any], r: _Report, stage: str) -> None:
    disk = plan.get("disk")
    if not isinstance(disk, dict) or not disk.get("target") or not disk.get("layout"):
        r.missing.append("disk")
        if isinstance(disk, dict) and disk.get("encryption"):
            _check_encryption(disk.get("encryption"), r)
        else:
            r.missing.append("encryption")
        if plan.get("migrate"):
            r.error("migrate_without_disk", "migrate", "choose the disk first")
        return
    layout = disk["layout"]
    allowed = STAGE_LAYOUTS.get(stage, LAYOUTS)
    if layout not in LAYOUTS:
        r.error("bad_layout", "disk.layout", f"'layout' must be one of {list(LAYOUTS)}")
        return
    if layout not in allowed:
        r.error(
            "later_stage",
            "disk.layout",
            f"'{layout}' comes in a later version; now: {list(allowed)} (installer.md, section 8)",
        )
    if disk.get("survey") != survey.get("fingerprint"):
        r.error(
            "disks_changed",
            "disk.survey",
            "the disks changed since the survey (a disk added or removed?); survey them again "
            "and choose the disk again",
        )
    target = _find_disk(survey, disk["target"])
    if target is None:
        r.error("no_target", "disk.target", f"disk {disk['target']!r} is not in the computer now")
        return
    if target.get("boot_medium"):
        r.error("boot_medium", "disk.target", "this is the disk Vilna is running from now")
    if target.get("read_only"):
        r.error("read_only", "disk.target", "the disk is read-only")
    if target.get("size", 0) < MIN_SYSTEM:
        r.error(
            "too_small",
            "disk.target",
            f"Vilna needs at least {_size(MIN_SYSTEM)}; the disk has {_size(target['size'])}",
        )
    if layout != "manual" and target.get("needs_manual_layout"):
        r.error(
            "needs_manual",
            "disk.layout",
            "the disk has RAID, LVM or a filesystem Vilna does not know: only manual layout",
        )
    if target.get("removable"):
        r.warn("removable", "disk.target", "the target is a removable disk")
    _check_encryption(disk.get("encryption"), r)
    if layout == "manual":
        if not disk.get("partitions"):
            r.changes.append({"action": "manual", "disk": target["id"]})
            r.error(
                "manual_needs_editor",
                "disk.partitions",
                "manual layout is made in the partition editor of the classic wizard",
            )
        else:
            _check_manual(disk, target, r)
    else:
        _changes(disk, target, r)
    _check_migrate(plan, survey, target, r, stage)


def _check_encryption(enc: Any, r: _Report) -> None:
    if not isinstance(enc, dict) or enc.get("method") not in ENCRYPTION:
        r.missing.append("encryption")
        return
    if enc["method"] == "none":
        r.warn(
            "no_encryption",
            "disk.encryption",
            "without encryption anyone with the disk can read the files",
        )
        return
    if enc.get("recovery_key_acknowledged") is not True:
        r.error(
            "recovery_key",
            "disk.encryption.recovery_key_acknowledged",
            "encryption needs the person to confirm the recovery key is saved",
        )
    if enc["method"] == "passphrase" and enc.get("passphrase") != ENTERED:
        r.error(
            "passphrase",
            "disk.encryption.passphrase",
            "the disk passphrase is not entered yet; the person types it in the protected field",
        )


def _changes(disk: dict[str, Any], target: dict[str, Any], r: _Report) -> None:
    layout = disk["layout"]
    keep = set(disk.get("keep_partitions") or [])
    erase = set(disk.get("erase_partitions") or [])
    if keep & erase:
        r.error(
            "keep_and_erase", "disk", f"partitions both kept and erased: {sorted(keep & erase)}"
        )
    for part in target.get("partitions", []):
        pid = part["id"]
        line = {
            "partition": pid,
            "name": part.get("name"),
            "size": part.get("size"),
            "system": part.get("system"),
            "label": part.get("label"),
            "holds_data": part.get("holds_data"),
        }
        if layout == "replace":
            if pid in keep:
                r.error("replace_keeps", "disk.keep_partitions", "'replace' erases the whole disk")
            r.changes.append({"action": "erase", **line})
        elif pid in erase:
            r.changes.append({"action": "erase", **line})
        elif shrink := _shrink_of(disk, pid):
            r.changes.append({"action": "shrink", "to": shrink, **line})
        else:
            r.changes.append({"action": "keep", **line})
    if layout == "replace":
        r.changes.append({"action": "create", "disk": target["id"], "size": target.get("size")})
        return
    space = target.get("largest_free", 0)
    shrink = disk.get("shrink")
    if layout == "alongside":
        if not isinstance(shrink, dict) or not shrink.get("partition"):
            r.error(
                "alongside_needs_shrink", "disk.shrink", "'alongside' needs a partition to shrink"
            )
            return
        part = next(
            (p for p in target.get("partitions", []) if p["id"] == shrink["partition"]), None
        )
        if part is None:
            r.error("no_partition", "disk.shrink.partition", "no such partition on the disk")
            return
        to = shrink.get("to")
        if not isinstance(to, int) or to <= 0 or to >= part.get("size", 0):
            r.error("bad_shrink", "disk.shrink.to", "the new size must be smaller than now")
            return
        if part.get("encrypted") == "bitlocker" and shrink.get("bitlocker") != "suspended":
            r.error(
                "bitlocker_not_suspended",
                "disk.shrink",
                "suspend BitLocker in Windows before shrinking its partition",
            )
        if shrink.get("checked") is not True:
            r.error("fs_not_checked", "disk.shrink", "the filesystem must be checked first")
        if shrink.get("backup_confirmed") is not True:
            r.error(
                "no_backup",
                "disk.shrink.backup_confirmed",
                "the person must confirm they have a backup before a partition is shrunk",
            )
        space += part["size"] - to
    if space < MIN_SYSTEM:
        r.error(
            "not_enough_space",
            "disk.layout",
            f"Vilna needs {_size(MIN_SYSTEM)} of free space; there is {_size(space)}",
        )
    r.changes.append({"action": "create", "disk": target["id"], "size": space})
    for part in target.get("partitions", []):
        if part["id"] in erase and part.get("holds_data"):
            r.warn("erases_data", "disk.erase_partitions", f"{part.get('name')} holds data")


def _check_manual(disk: dict[str, Any], target: dict[str, Any], r: _Report) -> None:
    """The partition editor's choices: one EFI and one system partition, room, no surprises."""
    items = disk.get("partitions")
    if not isinstance(items, list) or len(items) > MAX_PARTITIONS:
        r.error("bad_partitions", "disk.partitions", "'partitions' must be a short list")
        return
    parts = {p["id"]: p for p in target.get("partitions", [])}
    used: dict[str, dict[str, Any]] = {}
    count = dict.fromkeys(USES, 0)
    new_total = 0
    for i, item in enumerate(items):
        where = f"disk.partitions[{i}]"
        if not isinstance(item, dict) or item.get("use") not in USES:
            r.error("bad_partitions", where, f"each partition needs 'use', one of {list(USES)}")
            continue
        use = item["use"]
        count[use] += 1
        pid = item.get("partition")
        if pid is None:
            size = item.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                r.error("bad_partitions", where, "a new partition needs a 'size' in bytes")
                continue
            new_total += size
            _check_use_size(use, size, where, r)
            continue
        part = parts.get(pid)
        if part is None:
            r.error("no_partition", where, f"no partition {pid!r} on the disk")
            continue
        if pid in used:
            r.error("partition_twice", where, f"{part.get('name')} is chosen twice")
            continue
        used[pid] = item
        fmt = item.get("format") is True
        _check_use_size(use, part.get("size", 0), where, r)
        if use in ("system", "swap") and not fmt:
            r.error(
                "must_format",
                where,
                f"the {use} partition is always formatted; its old contents are erased",
            )
        elif use == "efi" and not fmt and part.get("kind") != "efi":
            r.error("not_efi", where, f"{part.get('name')} is not an EFI system partition")
        elif use == "home" and not fmt and (part.get("encrypted") or part.get("kind") != "data"):
            r.error(
                "home_unreadable",
                where,
                f"{part.get('name')} cannot be kept as home: format it or choose another",
            )
        elif use == "home" and not fmt and part.get("system") != "Linux":
            r.error("home_not_linux", where, "only a Linux filesystem can be kept as home")
    for use, how_many in count.items():
        if use in ("efi", "system") and how_many != 1:
            r.error(
                f"needs_one_{use}",
                "disk.partitions",
                f"the layout needs exactly one {use} partition (now {how_many})",
            )
        elif how_many > 1:
            r.error(f"one_{use}", "disk.partitions", f"at most one {use} partition")
    if new_total > target.get("largest_free", 0):
        r.error(
            "not_enough_space",
            "disk.partitions",
            f"the new partitions take {_size(new_total)}; the largest free space is "
            f"{_size(target.get('largest_free', 0))}",
        )
    for part in target.get("partitions", []):
        line = {
            "partition": part["id"],
            "name": part.get("name"),
            "size": part.get("size"),
            "system": part.get("system"),
            "label": part.get("label"),
            "holds_data": part.get("holds_data"),
        }
        item = used.get(part["id"])
        if item is None:
            r.changes.append({"action": "keep", **line})
        elif item.get("format") is True:
            r.changes.append({"action": "erase", "use": item["use"], **line})
        else:
            r.changes.append({"action": "use", "use": item["use"], **line})
    for item in items:
        if isinstance(item, dict) and item.get("partition") is None and item.get("use") in USES:
            size = item.get("size")
            if isinstance(size, int) and not isinstance(size, bool) and size > 0:
                r.changes.append(
                    {"action": "create", "use": item["use"], "disk": target["id"], "size": size}
                )


def _check_use_size(use: str, size: int, where: str, r: _Report) -> None:
    if use == "system" and size < MIN_SYSTEM:
        r.error(
            "too_small",
            where,
            f"the system partition needs at least {_size(MIN_SYSTEM)}; it has {_size(size)}",
        )
    elif use == "efi" and size < MIN_EFI:
        r.error("efi_too_small", where, f"the EFI partition needs at least {_size(MIN_EFI)}")


def _shrink_of(disk: dict[str, Any], pid: str) -> int | None:
    shrink = disk.get("shrink")
    if isinstance(shrink, dict) and shrink.get("partition") == pid:
        to = shrink.get("to")
        return to if isinstance(to, int) else None
    return None


def _check_migrate(
    plan: dict[str, Any],
    survey: dict[str, Any],
    target: dict[str, Any],
    r: _Report,
    stage: str,
) -> None:
    moves = plan.get("migrate") or []
    if not moves:
        return
    if stage == "alpha":
        r.error("later_stage", "migrate", "moving old files comes in a later version")
    if not isinstance(moves, list):
        r.error("bad_migrate", "migrate", "'migrate' must be a list")
        return
    erased = {c["partition"] for c in r.changes if c.get("action") == "erase"}
    for i, move in enumerate(moves):
        where = f"migrate[{i}]"
        if not isinstance(move, dict) or not move.get("source"):
            r.error("bad_migrate", where, "each move needs a 'source' partition")
            continue
        source = next(
            (
                p
                for d in survey.get("disks", [])
                for p in d.get("partitions", [])
                if p["id"] == move["source"]
            ),
            None,
        )
        if source is None:
            r.error("no_source", where, f"no partition {move['source']!r}")
            continue
        if source.get("encrypted") and move.get("secret") != ENTERED:
            r.error("locked_source", where, "the old encrypted volume is not unlocked yet")
        if source["id"] not in erased:
            continue
        # Files on a partition that is erased: only with a copy to another disk first.
        copy_to = move.get("copy_before_erase")
        other = _find_disk(survey, copy_to) if copy_to else None
        if other is None or other["id"] == target["id"]:
            r.error(
                "files_would_be_lost",
                where,
                "the files are on a partition that is erased; copy them to another disk first",
            )
        elif other.get("largest_free", 0) < int(move.get("size") or 0):
            r.error("no_room_for_copy", where, "the other disk has no room for the copy")
