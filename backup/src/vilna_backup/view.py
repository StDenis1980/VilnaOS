# SPDX-License-Identifier: GPL-3.0-or-later
"""What the Backups window shows, without GTK: texts, sizes, times, the form.

Kept apart from ``gui`` so it runs and is tested without a display.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from vilna_backup import keys
from vilna_backup.i18n import _
from vilna_backup.settings import Destination, SettingsError, parse_destination

# The three choices of the window (design 10.16); "custom" stays for Gloxi
# and the command line, and is shown when someone chose it there.
WINDOW_PRESETS = ("settings", "documents", "home")
SCHEDULE_ORDER = ("daily", "hourly", "weekly")


def preset_title(preset: str) -> str:
    return {
        "settings": _("Settings only"),
        "documents": _("Settings and documents"),
        "home": _("The whole home folder"),
        "custom": _("Chosen by you"),
    }.get(preset, preset)


def preset_detail(preset: str) -> str:
    return {
        "settings": _("Settings, Gloxi, Herkos, passwords"),
        "documents": _("Also Documents, Desktop and Pictures"),
        "home": _("Everything except caches and Downloads"),
        "custom": _("Folders chosen with Gloxi or in the terminal"),
    }.get(preset, "")


def schedule_title(schedule: str) -> str:
    return {
        "hourly": _("Every hour"),
        "daily": _("Every day"),
        "weekly": _("Every week"),
        "off": _("Off"),
    }.get(schedule, schedule)


def human_size(n: int | None) -> str:
    """Decimal units, one decimal below 10, as GNOME Files does."""
    if n is None:
        return "—"
    size = float(max(n, 0))
    for unit in (_("B"), _("kB"), _("MB"), _("GB"), _("TB")):
        if size < 1000 or unit == _("TB"):
            if unit == _("B"):
                return f"{int(size)} {unit}"
            text = f"{size:.1f}" if size < 10 else f"{size:.0f}"
            return f"{text.replace('.', _('.'))} {unit}"
        size /= 1000
    return f"{int(size)} {_('TB')}"  # pragma: no cover


def preset_sizes(groups: list[dict[str, Any]]) -> dict[str, int]:
    """Sizes of the window's choices from :func:`plan.overview` rows."""
    by = {g.get("group"): int(g.get("bytes") or 0) for g in groups}
    settings = by.get("settings", 0)
    documents = settings + sum(by.get(k, 0) for k in ("documents", "desktop", "pictures"))
    return {"settings": settings, "documents": documents, "home": by.get("home", 0)}


def preset_subtitle(preset: str, sizes: dict[str, int] | None) -> str:
    parts = [preset_detail(preset)]
    if sizes and preset in sizes:
        parts.append(_("≈ {size}").format(size=human_size(sizes[preset])))
    if preset == "documents":
        parts.append(_("recommended"))
    return " · ".join(p for p in parts if p)


def when(timestamp: float | str | None, now: float | None = None) -> str:
    """ "today at 15:42", "yesterday at 09:10" or "3 November at 18:00"."""
    if timestamp is None:
        return "—"
    if isinstance(timestamp, str):
        timestamp = _parse_iso(timestamp)
        if timestamp is None:
            return "—"
    now = time.time() if now is None else now
    t = time.localtime(timestamp)
    today = time.localtime(now)
    hm = f"{t.tm_hour:02d}:{t.tm_min:02d}"
    days = _day_number(today) - _day_number(t)
    if days == 0:
        return _("today at {time}").format(time=hm)
    if days == 1:
        return _("yesterday at {time}").format(time=hm)
    month = MONTHS[t.tm_mon - 1]
    return _("{day} {month} at {time}").format(day=t.tm_mday, month=_(month), time=hm)


MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _day_number(t: time.struct_time) -> int:
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 12, 0, 0, 0, 0, -1)) // 86400)


def _parse_iso(text: str) -> float | None:
    """restic's snapshot time, e.g. 2026-11-09T15:42:01.123456789+01:00."""
    from datetime import datetime

    head, _sep, rest = text.partition(".")
    tz = ""
    for sign in ("+", "-"):
        if sign in rest:
            tz = sign + rest.split(sign, 1)[1]
            break
    if rest.endswith("Z"):
        tz = "+00:00"
    try:
        return datetime.fromisoformat(head + (tz or "")).timestamp()
    except ValueError:
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None


@dataclass(frozen=True)
class Summary:
    """The state line at the top of the window."""

    title: str
    detail: str
    ok: bool  # False: something needs the person's attention
    percent: float | None = None  # a backup is running


def summary(status: dict[str, Any], now: float | None = None) -> Summary:
    running = status.get("running")
    if running:
        percent = float(running.get("percent") or 0)
        return Summary(
            _("Backing up…"),
            _("{percent} % done").format(percent=f"{percent:.0f}"),
            True,
            percent,
        )
    if not status.get("enabled"):
        return Summary(_("Backups are off"), _("Turn them on to keep your files safe."), False)
    last = status.get("last_backup")
    schedule = schedule_title(status.get("schedule", "daily")).lower()
    if not last:
        return Summary(
            _("Backups are on"), _("No backup yet · {schedule}").format(schedule=schedule), True
        )
    if not last.get("ok"):
        return Summary(
            _("The last backup failed"),
            _("{when}: {message}").format(
                when=when(last.get("time"), now), message=last.get("message", "")
            ),
            False,
        )
    return Summary(
        _("Backups are on"),
        _("Last backup {when} · {size} · {schedule}").format(
            when=when(last.get("time"), now), size=human_size(last.get("bytes")), schedule=schedule
        ),
        True,
    )


def snapshot_rows(snapshots: list[dict[str, Any]], now: float | None = None) -> list[tuple]:
    """(id, title, subtitle) for the list of backups, newest first."""
    rows = []
    for s in sorted(snapshots, key=lambda s: s.get("time") or "", reverse=True):
        title = when(s.get("time"), now)
        detail = human_size(s.get("bytes"))
        if s.get("files") is not None:
            detail += " · " + _("{n} files").format(n=f"{s['files']:,}".replace(",", " "))
        rows.append((s.get("id", ""), title[:1].upper() + title[1:], detail))
    return rows


def place_text(described: str | None) -> str:
    """:meth:`Destination.describe` ("disk: /run/media/…") in the window's language."""
    if not described:
        return "—"
    kind, sep, where = described.partition(": ")
    names = {
        "disk": _("Disk"),
        "SFTP": "SFTP",
        "S3": "S3",
        "Vilna server": _("Vilna server"),
    }
    if not sep or kind not in names:
        return described
    return f"{names[kind]}: {where}"


def key_for_reading(recovery_key: str) -> str:
    """The recovery key in groups, two lines of four, for writing down by hand."""
    groups = recovery_key.split("-")
    half = (len(groups) + 1) // 2
    return "  ".join(groups[:half]) + "\n" + "  ".join(groups[half:])


def key_looks_complete(typed: str) -> bool:
    try:
        keys.normalise_recovery_key(typed)
    except keys.KeysError:
        return False
    return True


class FormError(Exception):
    """The "where" part of the form is not filled in right; the message is for people."""


def form_destination(
    kind: str, folder: str = "", address: str = "", login: str = "", password: str = ""
) -> tuple[Destination, dict[str, str]]:
    """The place and its credentials from the form: a disk folder or one's own server."""
    if kind == "disk":
        if not folder:
            raise FormError(_("Choose a folder on the external disk."))
        try:
            return parse_destination(folder), {}
        except SettingsError:
            raise FormError(_("Choose a folder on the external disk.")) from None
    if kind == "server":
        address = address.strip()
        if not address:
            raise FormError(
                _("Type the server address: sftp:user@host:/folder, s3:https://… or rest:https://…")
            )
        try:
            dest = parse_destination(address)
        except SettingsError as e:
            raise FormError(_("The server address is not right: {why}").format(why=e)) from None
        if dest.kind == "local":
            raise FormError(_("For a folder on this computer, choose the external disk."))
        names = keys.CREDENTIALS.get(dest.kind)
        if not names:
            return dest, {}
        if not login or not password:
            raise FormError(_("This server needs a login and a password."))
        return dest, {names[0]: login, names[1]: password}
    raise FormError(_("Choose where to keep the backups."))
