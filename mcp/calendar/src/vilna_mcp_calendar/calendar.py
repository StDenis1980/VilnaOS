# SPDX-License-Identifier: Apache-2.0
"""The person's calendars: reading (ADR 0023, section 4) and writing (item 27).

The period is worked out here, not by the model: a small local model gets
"tomorrow" and "next Friday" wrong, so ``list_events`` takes a word
(``today``, ``tomorrow``, ``this_week``...) or two ISO 8601 dates, in the
person's time zone. Repeating events are expanded by the store (EDS), and
everything a calendar gives back is bounded before it reaches Gloxi.

Writing is narrow on purpose. Gloxi creates, changes and deletes single
events that only the person is in: a repeating event (which occurrence?)
and an event with other people invited (a change sends them a message,
which is R4 in Herkos) are left to the calendar app. Every change keeps the
event as it was before, so Mitos can undo it through ``restore``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time as clock
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Protocol

MAX_EVENTS = 200
MAX_DAYS = 366
MAX_TITLE = 500
MAX_LOCATION = 500
MAX_DESCRIPTION = 2000
MAX_QUERY = 200
MAX_ID = 1000
# Years an event may be written in: a typo such as 20277 is an error, not an event.
FIRST_YEAR, LAST_YEAR = 1970, 2200
TEXT_FIELDS = ("title", "location", "description")

PERIODS = ("today", "tomorrow", "this_week", "next_week", "this_month", "next_month")


class CalendarError(Exception):
    """Something the caller can fix: a bad period, an unknown calendar."""


@dataclass(frozen=True)
class Calendar:
    id: str
    name: str
    enabled: bool = True
    writable: bool = True
    default: bool = False  # where a new event goes when no calendar is named


@dataclass(frozen=True)
class Event:
    calendar_id: str
    title: str
    start: datetime  # aware
    end: datetime  # aware
    all_day: bool = False
    location: str = ""
    description: str = ""
    timezone: str = ""  # the event's own zone, when it has one
    uid: str = ""
    recurring: bool = False  # repeats, or is one changed occurrence of a repeat
    attendees: bool = False  # other people are invited


@dataclass(frozen=True)
class Draft:
    """A single event as Gloxi writes it: what the person sees, nothing else."""

    title: str
    start: datetime  # aware; all-day: midnight of the first day
    end: datetime  # aware; all-day: midnight after the last day
    all_day: bool = False
    location: str = ""
    description: str = ""


class Store(Protocol):
    def calendars(self) -> list[Calendar]: ...

    def events(self, calendar_ids: Iterable[str], start: datetime, end: datetime) -> list[Event]:
        """Event instances overlapping [start, end), repeats expanded."""
        ...

    def get(self, calendar_id: str, uid: str) -> tuple[Event, str] | None:
        """The event and a snapshot of it (the store's own text, such as iCalendar)."""
        ...

    def create(self, calendar_id: str, draft: Draft) -> str:
        """A new event; returns its uid."""
        ...

    def change(self, calendar_id: str, uid: str, draft: Draft, fields: frozenset[str]) -> None:
        """Set what the draft holds; everything else (reminders...) stays."""
        ...

    def remove(self, calendar_id: str, uid: str) -> None: ...

    def put_back(self, calendar_id: str, uid: str, snapshot: str) -> None:
        """The event exactly as in the snapshot, whether it exists now or not."""
        ...


class MemoryStore:
    """Calendars and events in memory, for tests and demos."""

    def __init__(self, calendars: list[Calendar], events: list[Event]) -> None:
        self._calendars = calendars
        self._events = [e if e.uid else replace(e, uid=f"e{i}") for i, e in enumerate(events)]
        self._next = len(self._events)

    def calendars(self) -> list[Calendar]:
        return list(self._calendars)

    def events(self, calendar_ids: Iterable[str], start: datetime, end: datetime) -> list[Event]:
        ids = set(calendar_ids)
        return [e for e in self._events if e.calendar_id in ids and e.start < end and e.end > start]

    def _find(self, calendar_id: str, uid: str) -> list[int]:
        return [
            i for i, e in enumerate(self._events) if e.calendar_id == calendar_id and e.uid == uid
        ]

    def get(self, calendar_id: str, uid: str) -> tuple[Event, str] | None:
        found = self._find(calendar_id, uid)
        if not found:
            return None
        event = self._events[found[0]]
        if len(found) > 1:  # a repeat: every occurrence is one event
            event = replace(event, recurring=True)
        return event, json.dumps(_event_json(event))

    def create(self, calendar_id: str, draft: Draft) -> str:
        self._next += 1
        uid = f"e{self._next}"
        self._events.append(_from_draft(calendar_id, uid, draft))
        return uid

    def change(self, calendar_id: str, uid: str, draft: Draft, fields: frozenset[str]) -> None:
        found = self._find(calendar_id, uid)
        if len(found) != 1:
            raise CalendarError("no such single event")
        self._events[found[0]] = _from_draft(calendar_id, uid, draft)

    def remove(self, calendar_id: str, uid: str) -> None:
        self._events = [
            e for e in self._events if not (e.calendar_id == calendar_id and e.uid == uid)
        ]

    def put_back(self, calendar_id: str, uid: str, snapshot: str) -> None:
        self.remove(calendar_id, uid)
        self._events.append(_event_from_json(json.loads(snapshot)))


def _from_draft(calendar_id: str, uid: str, d: Draft) -> Event:
    return Event(
        calendar_id, d.title, d.start, d.end, d.all_day, d.location, d.description, uid=uid
    )


def _event_json(e: Event) -> dict:
    return {
        "calendar_id": e.calendar_id,
        "uid": e.uid,
        "title": e.title,
        "start": e.start.isoformat(),
        "end": e.end.isoformat(),
        "all_day": e.all_day,
        "location": e.location,
        "description": e.description,
    }


def _event_from_json(d: dict) -> Event:
    return Event(
        d["calendar_id"],
        d["title"],
        datetime.fromisoformat(d["start"]),
        datetime.fromisoformat(d["end"]),
        d["all_day"],
        d["location"],
        d["description"],
        uid=d["uid"],
    )


# -- keeping what was there, for undo -------------------------------------------

# Snapshots kept for undo: how many, how long, how large each.
UNDO_KEEP = 1000
UNDO_DAYS = 90
MAX_SNAPSHOT = 256 * 1024
_TOKEN = re.compile(r"[0-9a-f]{32}")


class UndoStore(Protocol):
    def save(self, record: dict) -> str:
        """Keep ``{"calendar", "uid", "snapshot"}``; returns a token for ``restore``."""
        ...

    def load(self, token: str) -> dict | None: ...

    def discard(self, token: str) -> None: ...


class MemoryUndo:
    def __init__(self) -> None:
        self._records: dict[str, dict] = {}

    def save(self, record: dict) -> str:
        token = secrets.token_hex(16)
        self._records[token] = dict(record)
        return token

    def load(self, token: str) -> dict | None:
        return dict(self._records[token]) if token in self._records else None

    def discard(self, token: str) -> None:
        self._records.pop(token, None)


class DirUndo:
    """One small JSON file per change, owner-only, in the person's state folder.

    The event's own text is kept here (it is already in the calendar's cache
    on this computer); old files go after ``UNDO_DAYS`` or past ``UNDO_KEEP``.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def default(cls) -> DirUndo:
        # Started by Herkos, the unit's StateDirectory= is the one folder the
        # sandbox lets it write (ProtectHome=read-only keeps the home out).
        given = os.environ.get("STATE_DIRECTORY", "").split(":")[0]
        if given:
            return cls(Path(given) / "undo")
        state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        return cls(Path(state) / "vilna-mcp-calendar" / "undo")

    def _file(self, token: str) -> Path | None:
        return self.path / f"{token}.json" if _TOKEN.fullmatch(token) else None

    def save(self, record: dict) -> str:
        try:
            self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._prune()
            token = secrets.token_hex(16)
            target = self.path / f"{token}.json"
            tmp = target.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False)
            os.replace(tmp, target)
        except OSError as e:
            raise CalendarError(
                f"the calendar cannot keep what it needs to undo this change "
                f"({e.strerror or e}); nothing was changed"
            ) from e
        return token

    def load(self, token: str) -> dict | None:
        path = self._file(token)
        if path is None or not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) else None

    def discard(self, token: str) -> None:
        path = self._file(token)
        if path is not None:
            path.unlink(missing_ok=True)

    def _prune(self) -> None:
        try:
            files = sorted(self.path.glob("*.json"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        too_old = clock.time() - UNDO_DAYS * 86400
        for i, p in enumerate(files):
            try:
                if i < len(files) - (UNDO_KEEP - 1) or p.stat().st_mtime < too_old:
                    p.unlink(missing_ok=True)
            except OSError:
                continue


def resolve_period(
    word: str | None, start: str | None, end: str | None, now: datetime
) -> tuple[datetime, datetime]:
    """The [start, end) a request means, in the zone of ``now``."""
    tz = now.tzinfo
    today = now.date()
    if word is not None:
        if start is not None or end is not None:
            raise CalendarError("give either 'period' or 'start' and 'end', not both")
        if word == "today":
            first, days = today, 1
        elif word == "tomorrow":
            first, days = today + timedelta(days=1), 1
        elif word in ("this_week", "next_week"):
            monday = today - timedelta(days=today.weekday())
            first = monday if word == "this_week" else monday + timedelta(days=7)
            days = 7
        elif word in ("this_month", "next_month"):
            first = today.replace(day=1)
            if word == "next_month":
                first = _next_month(first)
            days = (_next_month(first) - first).days
        else:
            raise CalendarError(f"'period' must be one of {list(PERIODS)}")
        lo = _at_midnight(first, tz)
        return lo, _at_midnight(first + timedelta(days=days), tz)
    if start is None:
        raise CalendarError("give 'period', or 'start' (and maybe 'end') as ISO 8601 dates")
    lo = _parse(start, tz, end_of_day=False)
    hi = _parse(end, tz, end_of_day=True) if end is not None else lo + timedelta(days=1)
    if hi <= lo:
        raise CalendarError("'end' must be after 'start'")
    if hi - lo > timedelta(days=MAX_DAYS):
        raise CalendarError(f"at most {MAX_DAYS} days at a time")
    return lo, hi


def _next_month(first: date) -> date:
    return date(first.year + (first.month == 12), first.month % 12 + 1, 1)


def _at_midnight(d: date, tz: tzinfo | None) -> datetime:
    return datetime.combine(d, time.min, tz)


def _parse(text: str, tz: tzinfo | None, *, end_of_day: bool) -> datetime:
    if not isinstance(text, str) or len(text) > 40:
        raise CalendarError("dates must be ISO 8601, such as 2027-03-05 or 2027-03-05T14:00")
    try:
        if len(text) == 10:
            d = date.fromisoformat(text)
            return _at_midnight(d + timedelta(days=1) if end_of_day else d, tz)
        value = datetime.fromisoformat(text)
    except ValueError:
        raise CalendarError(
            f"not an ISO 8601 date: {text!r} (such as 2027-03-05 or 2027-03-05T14:00)"
        ) from None
    return value if value.tzinfo is not None else value.replace(tzinfo=tz)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Calendars:
    def __init__(self, store: Store, now=None, undo: UndoStore | None = None) -> None:
        self.store = store
        self._now = now or (lambda: datetime.now().astimezone())
        self.undo = undo or MemoryUndo()

    def list_calendars(self) -> dict:
        out = []
        for c in self.store.calendars():
            item = {"id": c.id, "name": c.name, "enabled": c.enabled}
            if not c.writable:
                item["read_only"] = True
            if c.default:
                item["default"] = True
            out.append(item)
        return {"calendars": out}

    def list_events(
        self,
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        calendars: list[str] | None = None,
    ) -> dict:
        now = self._now()
        lo, hi = resolve_period(period, start, end, now)
        return self._events(lo, hi, calendars, now, None)

    def search_events(
        self,
        query: str,
        start: str | None = None,
        end: str | None = None,
        calendars: list[str] | None = None,
    ) -> dict:
        query = query.strip()
        if not query or len(query) > MAX_QUERY:
            raise CalendarError(f"'query' must be 1 to {MAX_QUERY} characters")
        now = self._now()
        if start is None and end is None:
            lo = _at_midnight(now.date(), now.tzinfo)
            hi = lo + timedelta(days=MAX_DAYS - 1)
        else:
            lo, hi = resolve_period(None, start or now.date().isoformat(), end, now)
        return self._events(lo, hi, calendars, now, query.casefold())

    # -- writing (plan item 27) ------------------------------------------------

    def create_event(
        self,
        title: str,
        start: str,
        end: str | None = None,
        all_day: bool | None = None,
        days: int | None = None,
        location: str | None = None,
        description: str | None = None,
        calendar: str | None = None,
    ) -> dict:
        now = self._now()
        target = self._target(calendar)
        draft, _ = _draft(now, None, title, start, end, all_day, days, location, description)
        uid = self.store.create(target.id, draft)
        try:
            token = self.undo.save({"calendar": target.id, "uid": uid, "snapshot": None})
        except Exception:
            # An event nobody can undo is not left behind: asked again, it
            # would be made twice, and the answer would say it was not made.
            self.store.remove(target.id, uid)
            raise
        return {
            "created": self._item_now(target.id, uid, draft, now),
            "undo_arguments": {"token": token},
        }

    def update_event(
        self,
        id: str,
        calendar: str | None = None,
        title: str | None = None,
        start: str | None = None,
        end: str | None = None,
        all_day: bool | None = None,
        days: int | None = None,
        location: str | None = None,
        description: str | None = None,
    ) -> dict:
        now = self._now()
        target, event, snapshot = self._existing(id, calendar)
        draft, fields = _draft(now, event, title, start, end, all_day, days, location, description)
        if not fields:
            raise CalendarError(
                "nothing to change: give 'title', 'start', 'end', 'days', 'all_day', "
                "'location' or 'description' with a new value"
            )
        token = self.undo.save({"calendar": target.id, "uid": event.uid, "snapshot": snapshot})
        try:
            self.store.change(target.id, event.uid, draft, fields)
        except Exception:
            self.undo.discard(token)
            raise
        return {
            "changed": self._item_now(target.id, event.uid, draft, now),
            "fields": sorted(fields),
            "undo_arguments": {"token": token},
        }

    def delete_event(self, id: str, calendar: str | None = None) -> dict:
        now = self._now()
        target, event, snapshot = self._existing(id, calendar)
        token = self.undo.save({"calendar": target.id, "uid": event.uid, "snapshot": snapshot})
        try:
            self.store.remove(target.id, event.uid)
        except Exception:
            self.undo.discard(token)
            raise
        known = {c.id: c for c in self.store.calendars()}
        gone = _item(event, known, now.tzinfo)
        return {
            "deleted": {k: gone[k] for k in ("calendar", "title", "start", "end") if k in gone},
            "undo_arguments": {"token": token},
        }

    def restore(self, token: str) -> dict:
        """Put an event back as it was before a change (Mitos undo, ADR 0023 section 3)."""
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise CalendarError("'token' must be the one a change returned in undo_arguments")
        record = self.undo.load(token)
        if record is None:
            raise CalendarError(
                f"nothing to undo with this token: it was already used, or the change "
                f"is older than {UNDO_DAYS} days"
            )
        cid, uid, before = record.get("calendar"), record.get("uid"), record.get("snapshot")
        if not isinstance(cid, str) or not isinstance(uid, str):
            raise CalendarError("the saved change is damaged; nothing was undone")
        if before is not None and not isinstance(before, str):
            raise CalendarError("the saved change is damaged; nothing was undone")
        known = {c.id: c for c in self.store.calendars()}
        if cid not in known:
            raise CalendarError("the calendar of this event is gone; nothing was undone")
        if not known[cid].writable:
            raise CalendarError(f"the calendar {known[cid].name!r} is read only now")
        got = self.store.get(cid, uid)
        if got is not None and (got[0].recurring or got[0].attendees):
            raise CalendarError(
                "the event was made a repeat or got guests since; undo it in the calendar app"
            )
        now_snapshot = got[1] if got is not None else None
        if before is None:
            if got is not None:
                self.store.remove(cid, uid)
        else:
            self.store.put_back(cid, uid, before)
        again = self.undo.save({"calendar": cid, "uid": uid, "snapshot": now_snapshot})
        self.undo.discard(token)
        after = self.store.get(cid, uid)
        return {
            "restored": _item(after[0], known, self._now().tzinfo) if after else None,
            "removed": after is None,
            "undo_arguments": {"token": again},
        }

    def _target(self, calendar: str | None) -> Calendar:
        """The calendar a new event goes to: the one named, or the person's default."""
        cals = self.store.calendars()
        if calendar is not None:
            found = next((c for c in cals if c.id == calendar), None)
            if found is None:
                raise CalendarError(f"unknown calendar {calendar!r}; see list_calendars")
        else:
            writable = [c for c in cals if c.writable]
            found = next((c for c in writable if c.default), None) or next(
                (c for c in writable if c.enabled), None
            )
            if found is None:
                raise CalendarError("there is no calendar that can be written to")
        if not found.writable:
            raise CalendarError(f"the calendar {found.name!r} is read only")
        return found

    def _existing(self, uid: str, calendar: str | None) -> tuple[Calendar, Event, str]:
        """A single event only the person is in, and a snapshot of it."""
        if not isinstance(uid, str) or not 0 < len(uid) <= MAX_ID:
            raise CalendarError("'id' must be an event id from list_events or search_events")
        cals = {c.id: c for c in self.store.calendars()}
        if calendar is not None and calendar not in cals:
            raise CalendarError(f"unknown calendar {calendar!r}; see list_calendars")
        hits = []
        for cid in [calendar] if calendar is not None else list(cals):
            try:
                got = self.store.get(cid, uid)
            except CalendarError:
                if calendar is not None:
                    raise
                continue  # an account that is offline
            if got is not None:
                hits.append((cals[cid], *got))
        if not hits:
            raise CalendarError(f"no event with id {uid!r}; see list_events or search_events")
        if len(hits) > 1:
            raise CalendarError("several calendars have this id: give 'calendar' too")
        target, event, snapshot = hits[0]
        if event.recurring:
            raise CalendarError(
                "this event repeats; Gloxi changes only single events: "
                "change it in the calendar app"
            )
        if event.attendees:
            raise CalendarError(
                "other people are invited to this event, and a change would send them "
                "a message: change it in the calendar app"
            )
        if not target.writable:
            raise CalendarError(f"the calendar {target.name!r} is read only")
        if len(snapshot) > MAX_SNAPSHOT:
            raise CalendarError("this event is too large to keep for undo: change it in the app")
        return target, event, snapshot

    def _item_now(self, cid: str, uid: str, draft: Draft, now: datetime) -> dict:
        known = {c.id: c for c in self.store.calendars()}
        got = self.store.get(cid, uid)
        event = got[0] if got is not None else _from_draft(cid, uid, draft)
        return _item(event, known, now.tzinfo)

    def _events(self, lo, hi, wanted, now, query) -> dict:
        known = {c.id: c for c in self.store.calendars()}
        if wanted is None:
            ids = [c.id for c in known.values() if c.enabled]
        else:
            if not isinstance(wanted, list) or not all(isinstance(i, str) for i in wanted):
                raise CalendarError("'calendars' must be a list of ids from list_calendars")
            unknown = [i for i in wanted if i not in known]
            if unknown:
                raise CalendarError(f"unknown calendars {unknown}; see list_calendars")
            ids = list(dict.fromkeys(wanted))
        found = self.store.events(ids, lo, hi)
        if query is not None:
            found = [
                e
                for e in found
                if query in e.title.casefold()
                or query in e.location.casefold()
                or query in e.description.casefold()
            ]
        found.sort(key=lambda e: (e.start, e.title))
        tz = now.tzinfo
        events = []
        for e in found[:MAX_EVENTS]:
            events.append(_item(e, known, tz))
        return {
            "from": lo.isoformat(timespec="minutes"),
            "to": hi.isoformat(timespec="minutes"),
            "now": now.isoformat(timespec="minutes"),
            "events": events,
            "truncated": len(found) > MAX_EVENTS,
        }


def _item(e: Event, known: dict[str, Calendar], tz: tzinfo | None) -> dict:
    """An event as Gloxi sees it. ``id`` and ``calendar_id`` name it for a change."""
    item: dict = {
        "calendar": known[e.calendar_id].name if e.calendar_id in known else "",
        "title": _cut(e.title, MAX_TITLE),
        "all_day": e.all_day,
    }
    if e.all_day:
        item["start"] = e.start.date().isoformat()
        item["end"] = e.end.date().isoformat()
    else:
        item["start"] = e.start.astimezone(tz).isoformat(timespec="minutes")
        item["end"] = e.end.astimezone(tz).isoformat(timespec="minutes")
        if e.timezone:
            item["event_timezone"] = e.timezone
    if e.location:
        item["location"] = _cut(e.location, MAX_LOCATION)
    if e.description:
        item["description"] = _cut(e.description, MAX_DESCRIPTION)
    if e.uid:
        item["id"] = e.uid
        item["calendar_id"] = e.calendar_id
    if e.recurring:
        item["recurring"] = True
    if e.attendees:
        item["invited_others"] = True
    return item


def _moment(text: object, tz: tzinfo | None, name: str) -> tuple[datetime, bool]:
    """An ISO 8601 date or date-time, and whether it was a date alone."""
    if not isinstance(text, str) or len(text) > 40:
        raise CalendarError(f"'{name}' must be ISO 8601, such as 2027-03-05 or 2027-03-05T14:00")
    try:
        if len(text) == 10:
            value, date_only = _at_midnight(date.fromisoformat(text), tz), True
        else:
            value, date_only = datetime.fromisoformat(text), False
    except ValueError:
        raise CalendarError(
            f"'{name}' is not an ISO 8601 date: {text!r} (such as 2027-03-05 or 2027-03-05T14:00)"
        ) from None
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    if not FIRST_YEAR <= value.year <= LAST_YEAR:
        raise CalendarError(f"'{name}' must be between {FIRST_YEAR} and {LAST_YEAR}")
    return value, date_only


def _text(value: object, name: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise CalendarError(f"'{name}' must be text")
    value = value.strip()
    if name == "title":
        value = " ".join(value.split())  # one line
    if required and not value:
        raise CalendarError(f"'{name}' must not be empty")
    if len(value) > limit:
        raise CalendarError(f"'{name}' must be at most {limit} characters")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
        raise CalendarError(f"'{name}' has control characters")
    return value


def _draft(
    now: datetime,
    base: Event | None,
    title: str | None,
    start: str | None,
    end: str | None,
    all_day: bool | None,
    days: int | None,
    location: str | None,
    description: str | None,
) -> tuple[Draft, frozenset[str]]:
    """The event after the request, and which of its fields changed.

    A new event needs ``title`` and ``start``. A change keeps what is not
    given, and moving the start keeps the length of the event.
    """
    tz = now.tzinfo
    if base is None and (title is None or start is None):
        raise CalendarError("a new event needs 'title' and 'start'")
    if all_day is not None and not isinstance(all_day, bool):
        raise CalendarError("'all_day' must be true or false")
    if days is not None and (
        isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS
    ):
        raise CalendarError(f"'days' must be a whole number from 1 to {MAX_DAYS}")
    lo_given = _moment(start, tz, "start") if start is not None else None
    if all_day is None:
        # A new event on a date alone lasts the whole day; a change keeps its kind.
        all_day = base.all_day if base is not None else bool(lo_given and lo_given[1])
    if all_day:
        if end is not None:
            raise CalendarError(
                "for an all-day event give 'days' (how many days it lasts), not 'end'"
            )
        if lo_given is not None:
            first = lo_given[0].date()
        elif base is not None:
            first = base.start.astimezone(tz).date() if not base.all_day else base.start.date()
        else:  # pragma: no cover - start is required for a new event
            raise CalendarError("give 'start'")
        if days is None:
            days = (base.end.date() - base.start.date()).days if base and base.all_day else 1
            days = max(1, min(days, MAX_DAYS))
        lo, hi = _at_midnight(first, tz), _at_midnight(first + timedelta(days=days), tz)
    else:
        if days is not None:
            raise CalendarError("'days' is for all-day events; give 'end' instead")
        if lo_given is not None and lo_given[1]:
            if base is None or base.all_day:
                raise CalendarError("give 'start' with a time, such as 2027-03-05T14:00")
            # Moving to another day keeps the time of day.
            at = base.start.astimezone(tz)
            lo = datetime.combine(lo_given[0].date(), at.time(), tz)
        elif lo_given is not None:
            lo = lo_given[0]
        elif base is not None and not base.all_day:
            lo = base.start
        else:
            raise CalendarError("give 'start' with a time, such as 2027-03-05T14:00")
        if end is not None:
            hi, date_only = _moment(end, tz, "end")
            if date_only:
                raise CalendarError("give 'end' with a time, such as 2027-03-05T15:00")
        elif base is not None and not base.all_day:
            hi = lo + (base.end - base.start)
        else:
            hi = lo + timedelta(hours=1)
        if hi <= lo:
            raise CalendarError("'end' must be after 'start'")
        if hi - lo > timedelta(days=MAX_DAYS):
            raise CalendarError(f"an event may last at most {MAX_DAYS} days")
    texts = {
        "title": _text(title, "title", MAX_TITLE, required=True) if title is not None else None,
        "location": _text(location, "location", MAX_LOCATION) if location is not None else None,
        "description": (
            _text(description, "description", MAX_DESCRIPTION) if description is not None else None
        ),
    }
    if base is None:
        draft = Draft(
            texts["title"] or "",
            lo,
            hi,
            all_day,
            texts["location"] or "",
            texts["description"] or "",
        )
        return draft, frozenset(
            {
                "title",
                "start",
                "end",
                "all_day",
                *[k for k in ("location", "description") if texts[k]],
            }
        )
    draft = Draft(
        texts["title"] if texts["title"] is not None else base.title,
        lo,
        hi,
        all_day,
        texts["location"] if texts["location"] is not None else base.location,
        texts["description"] if texts["description"] is not None else base.description,
    )
    fields = {k for k in TEXT_FIELDS if texts[k] is not None and texts[k] != getattr(base, k)}
    if all_day != base.all_day:
        fields |= {"all_day", "start", "end"}
    else:
        if lo != base.start:
            fields.add("start")
        if hi != base.end:
            fields.add("end")
    return draft, frozenset(fields)
