# SPDX-License-Identifier: GPL-3.0-or-later
"""Devices: list, add with a one-time code, block, and protect after theft.

herkos/docs/devices.md and design.md 4a. In the alpha there is no Vilna
server, so the list of devices is the list of restic keys in the repository
itself: every device has its own key, the recovery sheet has one, and a code
that adds a device is a short-lived key of its own.

- **Add a device** (R4): a trusted device makes a pairing key whose password
  is a one-time code (80 bits) and whose host field says until when it is
  valid. The new device opens the repository with the code, adds its own key
  and removes the pairing key at once. No password crosses Gloxi or a
  network; the code is shown only on the trusted device's screen.
- **Block a device** (R3): its key is removed from the repository. It can no
  longer open the backups, and when it next tries it forgets its password.
- **Protect after theft** (R4): a blocked device may have known the master
  key of the repository, so a new repository with a new master key takes the
  snapshots over (``restic copy``), gets a new recovery key, and the old one
  is deleted once the copy is checked. Other devices join again with a code.

Who may do what (the levels) is Herkos's job; these functions are what the
person runs in the terminal or the window, never Gloxi.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import math
import os
import shutil
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vilna_backup import keys
from vilna_backup.restic import EXIT_WRONG_PASSWORD, Restic, ResticError
from vilna_backup.settings import Destination, Settings

if TYPE_CHECKING:
    from vilna_backup.service import Backup

RECOVERY_USER = "vilna-recovery"
DEVICE_USER = "vilna-device"
PAIRING_USER = "vilna-pairing"
PAIRING_TTL = 15 * 60
_UNTIL = "until-"
# Short ids people may type, like git's: enough to be unique in one repository.
MIN_ID_PREFIX = 8
MAX_NAME = 64


def _error(message: str) -> Exception:
    from vilna_backup.service import BackupError

    return BackupError(message)


def _ready(b: Backup) -> Settings:
    s = b.settings
    if s.destination is None:
        raise _error("no place for backups is set up yet")
    if not s.recovery_confirmed:
        raise _error("the recovery sheet is not confirmed yet")
    return s


def _pairing_until(key: dict[str, Any]) -> float | None:
    host = str(key.get("hostName") or "")
    if not host.startswith(_UNTIL):
        return None
    try:
        until = float(host.removeprefix(_UNTIL))
    except ValueError:
        return None
    # "until-nan" or "until-inf" would make a code that never runs out.
    return until if math.isfinite(until) else None


def _kind(key: dict[str, Any], s: Settings) -> str:
    if key.get("id") == s.recovery_key_id or key.get("userName") == RECOVERY_USER:
        return "recovery"
    if key.get("userName") == PAIRING_USER:
        return "pairing"
    return "device"


def _keys(b: Backup, s: Settings) -> list[dict[str, Any]]:
    try:
        return b.restic(s).keys()
    except ResticError as e:
        raise _error(f"cannot read the list of devices: {e}") from None


def shown_name(name: Any) -> str:
    """A device name as another device wrote it, safe to show: one short line of text.

    A key added with restic directly never went through :func:`clean_name`.
    """
    text = "".join(c if c.isprintable() else " " for c in str(name or ""))
    text = " ".join(text.split())[:MAX_NAME]
    return text or "?"


def clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name or len(name) > MAX_NAME or any(not c.isprintable() for c in name):
        raise _error(f"a device name is 1 to {MAX_NAME} printable characters")
    if name.startswith(_UNTIL):
        raise _error(f"a device name cannot start with {_UNTIL!r}")
    return name


# -- R1: the list ------------------------------------------------------------


def list_devices(b: Backup) -> list[dict[str, Any]]:
    """Devices with access to the backups. Expired codes are tidied up on the way."""
    s = _ready(b)
    now = b.clock()
    out = []
    for k in _keys(b, s):
        kind = _kind(k, s)
        until = _pairing_until(k) if kind == "pairing" else None
        if kind == "pairing" and (until is None or until <= now) and not k.get("current"):
            _remove_quietly(b.restic(s), k["id"])
            continue
        entry = {
            "id": str(k.get("id", ""))[:MIN_ID_PREFIX],
            "kind": kind,
            "name": {"recovery": "recovery sheet", "pairing": "code to add a device"}.get(
                kind, shown_name(k.get("hostName"))
            ),
            "added": shown_name(k.get("created"))[:40] if k.get("created") else None,
            "this_device": bool(k.get("current")),
        }
        if until is not None:
            entry["valid_until"] = until
        out.append(entry)
    return out


def _remove_quietly(r: Restic, key_id: str) -> None:
    # Someone else tidied it, or the place is busy: next time.
    with contextlib.suppress(ResticError):
        r.remove_key(key_id)


# -- R4: add a device ----------------------------------------------------------


def start_pairing(b: Backup) -> dict[str, Any]:
    """On a trusted device: a one-time code for a new device. Show it once."""
    s = _ready(b)
    code = keys.new_pairing_code()
    until = int(b.clock()) + PAIRING_TTL
    try:
        key_id = b.restic(s).add_key(code, host=f"{_UNTIL}{until}", user=PAIRING_USER)
    except ResticError as e:
        raise _error(f"cannot make a code: {e}") from None
    b._log("device-code", ok=True, key=key_id[:MIN_ID_PREFIX], until=until)
    return {"code": code, "valid_until": until, "id": key_id[:MIN_ID_PREFIX]}


def cancel_pairing(b: Backup) -> int:
    """Withdraw every code not used yet. Returns how many."""
    s = _ready(b)
    r = b.restic(s)
    gone = 0
    for k in _keys(b, s):
        if _kind(k, s) == "pairing" and not k.get("current"):
            r.remove_key(k["id"])
            gone += 1
    return gone


def join(
    b: Backup,
    destination: Destination,
    typed_code: str,
    *,
    name: str | None = None,
    credentials: dict[str, str] | None = None,
) -> Settings:
    """On the new device: open the backups with the code, add this device's own key."""
    try:
        code = keys.normalise_pairing_code(typed_code)
    except keys.KeysError as e:
        raise _error(str(e)) from None
    name = clean_name(name or socket.gethostname() or "vilna")
    current = b.settings
    if current.destination is not None and current.recovery_confirmed:
        raise _error("backups are already set up on this device")
    settings = Settings(destination=destination)
    for cred, value in (credentials or {}).items():
        b.store.put(settings.repo_id, cred.lower(), value)
    with_code = b.restic(settings, password_env={"RESTIC_PASSWORD": code})
    with_code.check_version()
    try:
        all_keys = with_code.keys()
    except ResticError as e:
        if e.code == EXIT_WRONG_PASSWORD:
            raise _error("this code does not open the backups (or was already used)") from None
        raise _error(f"cannot open the backups: {e}") from None
    mine = next((k for k in all_keys if k.get("current")), None)
    until = _pairing_until(mine) if mine and mine.get("userName") == PAIRING_USER else None
    if mine is None or until is None:
        raise _error("this is not a code to add a device")
    if until <= b.clock():
        _remove_quietly(with_code, mine["id"])
        raise _error("this code has expired; make a new one on your other device")
    recovery_key_id = next((k["id"] for k in all_keys if k.get("userName") == RECOVERY_USER), None)
    password = keys.new_repo_password()
    device_key_id = with_code.add_key(password, host=name, user=DEVICE_USER)
    b.store.put(settings.repo_id, keys.REPO_PASSWORD, password)
    # The code is spent: remove it with this device's own key (restic does
    # not let a key remove itself).
    b.restic(settings).remove_key(mine["id"])
    settings = b.save(
        settings.with_changes(
            device_key_id=device_key_id,
            recovery_key_id=recovery_key_id,
            # The sheet was confirmed on the device that made the code.
            recovery_confirmed=True,
            enabled=False,  # the first-run dialog asks what to back up here
        )
    )
    b._log("device-joined", ok=True, key=device_key_id[:MIN_ID_PREFIX])
    return settings


# -- R3: block -------------------------------------------------------------------


def _find(all_keys: list[dict[str, Any]], key_id: str) -> dict[str, Any]:
    key_id = key_id.strip().lower()
    if len(key_id) < MIN_ID_PREFIX:
        raise _error(f"give at least {MIN_ID_PREFIX} characters of the device id")
    found = [k for k in all_keys if str(k.get("id", "")).startswith(key_id)]
    if len(found) != 1:
        raise _error("no such device" if not found else "this id matches more than one device")
    return found[0]


def block(b: Backup, key_id: str, *, stolen: bool = False) -> dict[str, Any]:
    """Remove a device's key. It can no longer open the backups."""
    s = _ready(b)
    k = _find(_keys(b, s), key_id)
    kind = _kind(k, s)
    if k.get("current"):
        raise _error("this is the device you are using; block it from another one")
    if kind == "recovery":
        raise _error("the recovery sheet cannot be blocked; protect the backups instead")
    try:
        b.restic(s).remove_key(k["id"])
    except ResticError as e:
        raise _error(f"cannot block the device: {e}") from None
    name = shown_name(k.get("hostName"))
    b._log("device-blocked", ok=True, key=k["id"][:MIN_ID_PREFIX], stolen=stolen)
    out: dict[str, Any] = {"blocked": k["id"][:MIN_ID_PREFIX], "name": name}
    if stolen:
        # devices.md 5: blocking does not change the master key.
        out["next"] = "protect"
    return out


def forget_if_blocked(b: Backup, error: ResticError) -> bool:
    """On a blocked device: restic says our key no longer opens the backups.

    Forget the repository password and storage logins and stop backups
    (devices.md 4.4). Files on the disk are not touched.
    """
    if error.code != EXIT_WRONG_PASSWORD:
        return False
    s = b.settings
    if s.destination is None:
        return False
    b.store.delete(s.repo_id, keys.REPO_PASSWORD)
    for var in keys.CREDENTIALS.get(s.destination.kind, ()):
        b.store.delete(s.repo_id, var.lower())
    b.save(s.with_changes(enabled=False, device_key_id=None))
    b._log(
        "device-blocked-here",
        ok=False,
        message="this device was blocked from another one; backups are off",
    )
    return True


# -- R4: protect the backups after theft ---------------------------------------


def _fresh_destination(old: Destination, stamp: str) -> Destination:
    if old.kind == "rest":
        raise _error("on the Vilna server this is done by the server itself")
    loc = old.location.rstrip("/")
    return Destination(old.kind, f"{loc}.new-{stamp}" if old.kind == "local" else f"{loc}-{stamp}")


def protect(b: Backup) -> dict[str, Any]:
    """A new repository with a new master key; the snapshots move over.

    Returns the new recovery key, to show once; backups stay off until the
    new sheet is confirmed. Other devices have to join again with a code.
    """
    old = _ready(b)
    assert old.destination is not None  # _ready
    stamp = dt.datetime.fromtimestamp(b.clock(), dt.UTC).strftime("%Y%m%d-%H%M%S")
    new_dest = _fresh_destination(old.destination, stamp)
    with b._lock() as locked:
        if not locked:
            raise _error("a backup is running; try again when it finishes")
        new = Settings(
            destination=new_dest,
            enabled=old.enabled,
            preset=old.preset,
            include=old.include,
            exclude=old.exclude,
            gloxi_history=old.gloxi_history,
            system_secrets=old.system_secrets,
            limit_bytes=old.limit_bytes,
            schedule=old.schedule,
        )
        for var in keys.CREDENTIALS.get(new_dest.kind, ()):
            value = b.store.get(old.repo_id, var.lower())
            if value:
                b.store.put(new.repo_id, var.lower(), value)
        b.store.put(new.repo_id, keys.REPO_PASSWORD, keys.new_repo_password())
        old_r = b.restic(old)
        new_r = b.restic(new)
        # Storage logins (S3) are the same for both places: the same server.
        from_env = keys.as_from_env(b.store.password_env(old.repo_id))
        try:
            if new_r.is_initialised():
                raise _error("there is something at the new place already")
            new_r.init_copying_chunker(old.destination.repository(), from_env)
            new_r.copy_from(old.destination.repository(), from_env)
            _same_snapshots(old_r, new_r)
            summary = new_r.check()
            if summary and summary.get("num_errors"):
                raise _error("the new backups did not pass the check")
            device_key_id = new_r.current_key_id()
            recovery = keys.new_recovery_key()
            recovery_key_id = new_r.add_key(recovery, host="recovery-sheet", user=RECOVERY_USER)
        except BaseException as e:
            _drop_new(b, new)
            if isinstance(e, ResticError):
                raise _error(f"the backups were not moved: {e}") from None
            raise
        # Point at the new place first, so that nothing is lost if we stop
        # half way through deleting the old one.
        new = b.save(
            new.with_changes(
                device_key_id=device_key_id,
                recovery_key_id=recovery_key_id,
                recovery_confirmed=False,  # a new sheet: backups wait for it
            )
        )
        left = _drop_old(b, old)
        final = new_dest
        if not left:
            # A local disk: the new repository takes the old folder's name.
            os.replace(new_dest.location, old.destination.location)
            final = old.destination
            b.save(new.with_changes(destination=final))
    b._log("protected", ok=True, old_left=bool(left))
    out: dict[str, Any] = {"recovery_key": recovery, "place": final.describe()}
    if left:
        out["delete_old"] = old.destination.describe()
    return out


def _same_snapshots(old_r: Restic, new_r: Restic) -> None:
    before = {x["id"] for x in old_r.snapshots()}
    after = {x.get("original") or x["id"] for x in new_r.snapshots()}
    if before - after:
        raise _error(f"{len(before - after)} backups did not reach the new place")


def _key_items() -> tuple[str, ...]:
    return (keys.REPO_PASSWORD, *(v.lower() for k in keys.CREDENTIALS.values() for v in k))


def _drop_new(b: Backup, new: Settings) -> None:
    for item in _key_items():
        b.store.delete(new.repo_id, item)
    if new.destination and new.destination.kind == "local":
        shutil.rmtree(new.destination.location, ignore_errors=True)


def _drop_old(b: Backup, old: Settings) -> bool:
    """Delete the old repository where we can. True if it is left for the person."""
    for item in _key_items():
        b.store.delete(old.repo_id, item)
    assert old.destination is not None
    if old.destination.kind != "local":
        return True  # SFTP or S3 of the person's own: they delete the folder
    path = Path(old.destination.location)
    if not (path / "config").is_file() or not (path / "keys").is_dir():
        return True  # not what a restic repository looks like: do not touch
    shutil.rmtree(path)
    return False
