# SPDX-License-Identifier: Apache-2.0
"""Calendars of GNOME through Evolution Data Server (decision of 2026-11-09).

EDS holds what GNOME Calendar and GNOME Online Accounts (Google, Nextcloud,
Microsoft, CalDAV) show. We only read: no write call is made here. PyGObject
and the EDS typelibs come from the system or the Flatpak runtime, so this
module is imported only when the server really starts.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

import gi

gi.require_version("EDataServer", "1.2")
gi.require_version("ECal", "2.0")
gi.require_version("ICalGLib", "3.0")
from gi.repository import ECal, EDataServer, GLib  # noqa: E402

from vilna_mcp_calendar.calendar import Calendar, CalendarError, Event  # noqa: E402

CONNECT_TIMEOUT = 10  # seconds EDS may take to open a calendar
# Instances read from one calendar before we stop: a daily event repeating
# for years must not fill the memory.
MAX_INSTANCES = 5000


class EdsStore:
    def __init__(self) -> None:
        try:
            self._registry = EDataServer.SourceRegistry.new_sync(None)
        except GLib.Error as e:
            raise CalendarError(f"the calendar store of GNOME is not reachable: {e}") from e
        self._clients: dict[str, ECal.Client] = {}

    def _sources(self):
        return self._registry.list_sources(EDataServer.SOURCE_EXTENSION_CALENDAR)

    def calendars(self) -> list[Calendar]:
        out = []
        for source in self._sources():
            ext = source.get_extension(EDataServer.SOURCE_EXTENSION_CALENDAR)
            enabled = bool(source.get_enabled() and ext.get_selected())
            out.append(Calendar(source.get_uid(), source.get_display_name() or "", enabled))
        return out

    def _client(self, uid: str):
        if uid not in self._clients:
            source = self._registry.ref_source(uid)
            if source is None:
                raise CalendarError(f"unknown calendar {uid!r}")
            try:
                self._clients[uid] = ECal.Client.connect_sync(
                    source, ECal.ClientSourceType.EVENTS, CONNECT_TIMEOUT, None
                )
            except GLib.Error as e:
                raise CalendarError(f"calendar {source.get_display_name()!r}: {e.message}") from e
        return self._clients[uid]

    def events(self, calendar_ids: Iterable[str], start: datetime, end: datetime) -> list[Event]:
        out: list[Event] = []
        for uid in calendar_ids:
            try:
                client = self._client(uid)
            except CalendarError:
                continue  # an account that is offline: the others still answer
            found: list[Event] = []

            def take(comp, istart, iend, *_data, uid=uid, found=found) -> bool:
                event = _event(uid, comp, istart, iend)
                if event is not None:
                    found.append(event)
                return len(found) < MAX_INSTANCES

            try:
                client.generate_instances_sync(
                    int(start.timestamp()), int(end.timestamp()), None, take
                )
            except GLib.Error:
                continue
            out.extend(found)
        return out


def _event(uid: str, comp, istart, iend) -> Event | None:
    start = _when(istart)
    end = _when(iend) if iend is not None and not iend.is_null_time() else None
    if start is None:
        return None
    all_day = bool(istart.is_date())
    if end is None or end <= start:
        end = start + (timedelta(days=1) if all_day else timedelta(0))
    tzid = "" if all_day or istart.is_utc() else (istart.get_tzid() or "")
    return Event(
        calendar_id=uid,
        title=comp.get_summary() or "",
        start=start,
        end=end,
        all_day=all_day,
        location=comp.get_location() or "",
        description=comp.get_description() or "",
        timezone=tzid,
    )


def _when(t) -> datetime | None:
    """An ICalGLib.Time as an aware datetime (all-day: midnight, local)."""
    if t is None or t.is_null_time() or not t.is_valid_time():
        return None
    if t.is_date():
        d = date(t.get_year(), t.get_month(), t.get_day())
        return datetime(d.year, d.month, d.day).astimezone()
    zone = t.get_timezone()
    if t.is_utc() or zone is not None:
        return datetime.fromtimestamp(t.as_timet_with_zone(zone), UTC)
    # Floating time: the person's own zone.
    naive = datetime(
        t.get_year(), t.get_month(), t.get_day(), t.get_hour(), t.get_minute(), t.get_second()
    )
    return naive.astimezone()
