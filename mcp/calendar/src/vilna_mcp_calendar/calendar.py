# SPDX-License-Identifier: Apache-2.0
"""Reading the person's calendars (ADR 0023, section 4). Nothing is written.

The period is worked out here, not by the model: a small local model gets
"tomorrow" and "next Friday" wrong, so ``list_events`` takes a word
(``today``, ``tomorrow``, ``this_week``...) or two ISO 8601 dates, in the
person's time zone. Repeating events are expanded by the store (EDS), and
everything a calendar gives back is bounded before it reaches Gloxi.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Protocol

MAX_EVENTS = 200
MAX_DAYS = 366
MAX_TITLE = 500
MAX_LOCATION = 500
MAX_DESCRIPTION = 2000
MAX_QUERY = 200

PERIODS = ("today", "tomorrow", "this_week", "next_week", "this_month", "next_month")


class CalendarError(Exception):
    """Something the caller can fix: a bad period, an unknown calendar."""


@dataclass(frozen=True)
class Calendar:
    id: str
    name: str
    enabled: bool = True


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


class Store(Protocol):
    def calendars(self) -> list[Calendar]: ...

    def events(self, calendar_ids: Iterable[str], start: datetime, end: datetime) -> list[Event]:
        """Event instances overlapping [start, end), repeats expanded."""
        ...


class MemoryStore:
    """A fixed list of calendars and events, for tests and demos."""

    def __init__(self, calendars: list[Calendar], events: list[Event]) -> None:
        self._calendars = calendars
        self._events = events

    def calendars(self) -> list[Calendar]:
        return list(self._calendars)

    def events(self, calendar_ids: Iterable[str], start: datetime, end: datetime) -> list[Event]:
        ids = set(calendar_ids)
        return [e for e in self._events if e.calendar_id in ids and e.start < end and e.end > start]


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
    def __init__(self, store: Store, now=None) -> None:
        self.store = store
        self._now = now or (lambda: datetime.now().astimezone())

    def list_calendars(self) -> dict:
        return {
            "calendars": [
                {"id": c.id, "name": c.name, "enabled": c.enabled} for c in self.store.calendars()
            ]
        }

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
            item = {
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
            events.append(item)
        return {
            "from": lo.isoformat(timespec="minutes"),
            "to": hi.isoformat(timespec="minutes"),
            "now": now.isoformat(timespec="minutes"),
            "events": events,
            "truncated": len(found) > MAX_EVENTS,
        }
