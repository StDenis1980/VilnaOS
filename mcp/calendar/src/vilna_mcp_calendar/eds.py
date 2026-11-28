# SPDX-License-Identifier: Apache-2.0
"""Calendars of GNOME through Evolution Data Server (decision of 2026-11-09).

EDS holds what GNOME Calendar and GNOME Online Accounts (Google, Nextcloud,
Microsoft, CalDAV) show. Reading is ADR 0023; writing (plan item 27) touches
only single events with no guests, and changes only the properties Gloxi was
asked to change, so reminders, categories and the like stay as they were.
PyGObject and the EDS typelibs come from the system or the Flatpak runtime,
so this module is imported only when the server really starts.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

import gi

gi.require_version("EDataServer", "1.2")
gi.require_version("ECal", "2.0")
gi.require_version("ICalGLib", "3.0")
from gi.repository import ECal, EDataServer, GLib, ICalGLib  # noqa: E402

from vilna_mcp_calendar.calendar import (  # noqa: E402
    MAX_DESCRIPTION,
    MAX_ID,
    MAX_LOCATION,
    MAX_TITLE,
    Calendar,
    CalendarError,
    Draft,
    Event,
    _cut,
)

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
        default = self._registry.ref_default_calendar()
        default_uid = default.get_uid() if default is not None else ""
        out = []
        for source in self._sources():
            ext = source.get_extension(EDataServer.SOURCE_EXTENSION_CALENDAR)
            enabled = bool(source.get_enabled() and ext.get_selected())
            uid = source.get_uid()
            # Whether a calendar takes changes is known once it is open; an
            # open one says so, the others are checked when written to.
            client = self._clients.get(uid)
            writable = client is None or not client.is_readonly()
            out.append(
                Calendar(
                    uid, source.get_display_name() or "", enabled, writable, uid == default_uid
                )
            )
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
                try:
                    event = _event(uid, comp, istart, iend)
                except Exception:  # noqa: BLE001 - one odd event never hides the others
                    return True
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

    # -- writing (plan item 27) --------------------------------------------------

    def _writable(self, calendar_id: str):
        client = self._client(calendar_id)
        if client.is_readonly():
            raise CalendarError("this calendar is read only")
        return client

    def _component(self, client, uid: str):
        try:
            got = client.get_object_sync(uid, None, None)
        except GLib.Error as e:
            if _not_found(e):
                return None
            raise CalendarError(f"cannot read the event: {e.message}") from e
        comp = got[1] if isinstance(got, tuple) else got
        return comp

    def get(self, calendar_id: str, uid: str) -> tuple[Event, str] | None:
        client = self._client(calendar_id)
        comp = self._component(client, uid)
        if comp is None:
            return None
        event = _event(calendar_id, comp, comp.get_dtstart(), _end_of(comp))
        if event is None:
            return None
        return event, comp.as_ical_string()

    def create(self, calendar_id: str, draft: Draft) -> str:
        client = self._writable(calendar_id)
        comp = ICalGLib.Component.new_vevent()
        uid = f"{uuid.uuid4()}@vilna"
        comp.set_uid(uid)
        stamp = _ical_time(datetime.now(UTC), all_day=False)
        comp.set_dtstamp(stamp)
        _apply(comp, draft, frozenset({"title", "start", "end", "location", "description"}))
        try:
            got = client.create_object_sync(comp, ECal.OperationFlags.NONE, None)
        except GLib.Error as e:
            raise CalendarError(f"cannot add the event: {e.message}") from e
        made = got[1] if isinstance(got, tuple) else got
        return made or uid

    def change(self, calendar_id: str, uid: str, draft: Draft, fields: frozenset[str]) -> None:
        client = self._writable(calendar_id)
        comp = self._component(client, uid)
        if comp is None:
            raise CalendarError("the event is gone")
        _apply(comp, draft, fields)
        _bump_sequence(comp)
        try:
            client.modify_object_sync(comp, ECal.ObjModType.THIS, ECal.OperationFlags.NONE, None)
        except GLib.Error as e:
            raise CalendarError(f"cannot change the event: {e.message}") from e

    def remove(self, calendar_id: str, uid: str) -> None:
        client = self._writable(calendar_id)
        try:
            client.remove_object_sync(
                uid, None, ECal.ObjModType.ALL, ECal.OperationFlags.NONE, None
            )
        except GLib.Error as e:
            if _not_found(e):
                return
            raise CalendarError(f"cannot delete the event: {e.message}") from e

    def put_back(self, calendar_id: str, uid: str, snapshot: str) -> None:
        client = self._writable(calendar_id)
        comp = ICalGLib.Component.new_from_string(snapshot)
        if comp is None or comp.get_uid() != uid:
            raise CalendarError("the saved event is damaged; nothing was undone")
        try:
            if self._component(client, uid) is None:
                client.create_object_sync(comp, ECal.OperationFlags.NONE, None)
            else:
                client.modify_object_sync(comp, ECal.ObjModType.ALL, ECal.OperationFlags.NONE, None)
        except GLib.Error as e:
            raise CalendarError(f"cannot put the event back: {e.message}") from e


def _not_found(e: GLib.Error) -> bool:
    return e.domain == "e-cal-client-error-quark" and e.code == ECal.ClientError.OBJECT_NOT_FOUND


_TEXT = {
    "title": (ICalGLib.PropertyKind.SUMMARY_PROPERTY, "set_summary"),
    "location": (ICalGLib.PropertyKind.LOCATION_PROPERTY, "set_location"),
    "description": (ICalGLib.PropertyKind.DESCRIPTION_PROPERTY, "set_description"),
}


def _apply(comp, draft: Draft, fields: frozenset[str]) -> None:
    """Set only the properties in ``fields``; an empty text removes its property."""
    for name, (kind, setter) in _TEXT.items():
        if name not in fields:
            continue
        _remove_all(comp, kind)
        value = getattr(draft, name)
        if value:
            getattr(comp, setter)(value)
    if fields & {"start", "end", "all_day"}:
        for kind in (
            ICalGLib.PropertyKind.DTSTART_PROPERTY,
            ICalGLib.PropertyKind.DTEND_PROPERTY,
            ICalGLib.PropertyKind.DURATION_PROPERTY,
        ):
            _remove_all(comp, kind)
        comp.set_dtstart(_ical_time(draft.start, all_day=draft.all_day))
        comp.set_dtend(_ical_time(draft.end, all_day=draft.all_day))


def _remove_all(comp, kind) -> None:
    prop = comp.get_first_property(kind)
    while prop is not None:
        comp.remove_property(prop)
        prop = comp.get_first_property(kind)


def _bump_sequence(comp) -> None:
    """A changed event gets a higher SEQUENCE (RFC 5545), so servers take the change."""
    comp.set_sequence(max(0, comp.get_sequence()) + 1)


def _ical_time(when: datetime, *, all_day: bool):
    """All-day: a DATE. Otherwise UTC: one instant, shown in each viewer's zone."""
    if all_day:
        t = ICalGLib.Time.new_null_time()
        t.set_date(when.year, when.month, when.day)
        t.set_is_date(True)
        return t
    return ICalGLib.Time.new_from_timet_with_zone(
        int(when.timestamp()), 0, ICalGLib.Timezone.get_utc_timezone()
    )


def _end_of(comp):
    end = comp.get_dtend()
    if end is not None and not end.is_null_time():
        return end
    duration = comp.get_duration()
    start = comp.get_dtstart()
    if duration is None or start is None or start.is_null_time():
        return None
    return start.add(duration) if hasattr(start, "add") else None


def _event(uid: str, comp, istart, iend) -> Event | None:
    start = _when(istart)
    end = _when(iend) if iend is not None and not iend.is_null_time() else None
    if start is None:
        return None
    all_day = bool(istart.is_date())
    if end is None or end <= start:
        end = start + (timedelta(days=1) if all_day else timedelta(0))
    tzid = "" if all_day or istart.is_utc() else (istart.get_tzid() or "")
    tzid = _cut(tzid.removeprefix(_LIBICAL_ZONES), MAX_ZONE)
    event_uid = comp.get_uid() or ""
    # Texts are cut here, once, not in each of a few thousand repeats.
    return Event(
        calendar_id=uid,
        title=_cut(comp.get_summary() or "", MAX_TITLE),
        start=start,
        end=end,
        all_day=all_day,
        location=_cut(comp.get_location() or "", MAX_LOCATION),
        description=_cut(comp.get_description() or "", MAX_DESCRIPTION),
        timezone=tzid,
        uid=event_uid if len(event_uid) <= MAX_ID else "",  # too long to name in a change
        recurring=any(comp.count_properties(kind) > 0 for kind in _REPEATS),
        attendees=comp.count_properties(ICalGLib.PropertyKind.ATTENDEE_PROPERTY) > 0,
    )


_REPEATS = (
    ICalGLib.PropertyKind.RRULE_PROPERTY,
    ICalGLib.PropertyKind.RDATE_PROPERTY,
    ICalGLib.PropertyKind.RECURRENCEID_PROPERTY,
)


_LIBICAL_ZONES = "/freeassociation.sourceforge.net/"
MAX_ZONE = 100


def _when(t) -> datetime | None:
    """An ICalGLib.Time as an aware datetime (all-day: midnight, local)."""
    if t is None or t.is_null_time() or not t.is_valid_time():
        return None
    try:
        return _datetime(t)
    except (ValueError, OverflowError, OSError):  # 29 February 2027, year 0 or 10000
        return None


def _datetime(t) -> datetime:
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
