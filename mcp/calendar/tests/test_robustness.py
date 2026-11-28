# SPDX-License-Identifier: Apache-2.0
"""Clock changes and dates that once moved an event by an hour or showed a Python error."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from vilna_mcp_calendar.calendar import Calendar, Calendars, Event, MemoryStore, local_zone
from vilna_mcp_calendar.server import Server


@pytest.fixture
def kyiv(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Kyiv")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def at(when: datetime):
    """ "Now" as the server takes it, on that day."""
    return lambda: when.astimezone(local_zone())


def test_an_event_after_the_clocks_change_is_shown_at_its_own_hour(kyiv):
    standup = Event(
        "home",
        "Standup",
        datetime(2027, 4, 5, 7, tzinfo=UTC),
        datetime(2027, 4, 5, 8, tzinfo=UTC),
        uid="e",
    )
    store = MemoryStore([Calendar("home", "Home", default=True)], [standup])
    c = Calendars(store, now=at(datetime(2027, 3, 10, 9, 30, tzinfo=UTC)))
    (shown,) = c.search_events("Standup")["events"]
    assert shown["start"].startswith("2027-04-05T10:00")  # 07:00 UTC is 10:00 in Kyiv in April
    made = c.create_event(title="Lunch", start="2027-04-05T10:00")["created"]
    assert made["start"].startswith("2027-04-05T10:00")


def test_an_hour_long_event_lasts_an_hour_when_the_clocks_go_back(kyiv):
    c = Calendars(
        MemoryStore([Calendar("home", "Home", default=True)], []),
        now=at(datetime(2027, 10, 20, 9, tzinfo=UTC)),
    )
    made = c.create_event(title="Night shift", start="2027-10-31T03:30")["created"]
    start, end = datetime.fromisoformat(made["start"]), datetime.fromisoformat(made["end"])
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=1)


def test_the_last_hour_of_a_month_after_the_change_is_in_that_month(kyiv):
    late = Event(
        "home",
        "Late",
        datetime(2027, 10, 31, 21, 30, tzinfo=UTC),  # 23:30 on 31 October in Kyiv
        datetime(2027, 10, 31, 21, 45, tzinfo=UTC),
    )
    c = Calendars(
        MemoryStore([Calendar("home", "Home")], [late]),
        now=at(datetime(2027, 10, 10, 9, tzinfo=UTC)),
    )
    assert [e["title"] for e in c.list_events(period="this_month")["events"]] == ["Late"]


def test_the_last_day_there_is_gives_a_plain_answer():
    s = Server(Calendars(MemoryStore([Calendar("home", "Home")], [])))
    out = s.call("list_events", {"start": "9999-12-31"})
    assert out["isError"] is True and "OverflowError" not in out["content"][0]["text"]
