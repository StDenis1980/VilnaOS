# SPDX-License-Identifier: Apache-2.0
import io
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from vilna_mcp_calendar.calendar import (
    MAX_DESCRIPTION,
    MAX_EVENTS,
    Calendar,
    CalendarError,
    Calendars,
    Event,
    MemoryStore,
    resolve_period,
)
from vilna_mcp_calendar.server import Server, demo_store

KYIV = timezone(timedelta(hours=2), "EET")
# Wednesday 10 March 2027, 09:30 in Kyiv.
NOW = datetime(2027, 3, 10, 9, 30, tzinfo=KYIV)


def cal(store=None):
    return Calendars(store or demo_store(NOW), now=lambda: NOW)


@pytest.mark.parametrize(
    ("word", "start", "end"),
    [
        ("today", "2027-03-10T00:00", "2027-03-11T00:00"),
        ("tomorrow", "2027-03-11T00:00", "2027-03-12T00:00"),
        ("this_week", "2027-03-08T00:00", "2027-03-15T00:00"),
        ("next_week", "2027-03-15T00:00", "2027-03-22T00:00"),
        ("this_month", "2027-03-01T00:00", "2027-04-01T00:00"),
        ("next_month", "2027-04-01T00:00", "2027-05-01T00:00"),
    ],
)
def test_period_words(word, start, end):
    lo, hi = resolve_period(word, None, None, NOW)
    assert lo.isoformat(timespec="minutes").startswith(start)
    assert hi.isoformat(timespec="minutes").startswith(end)
    assert lo.tzinfo is KYIV


def test_december_rolls_into_january():
    lo, hi = resolve_period("next_month", None, None, NOW.replace(month=12))
    assert (lo.year, lo.month, hi.year, hi.month) == (2028, 1, 2028, 2)


def test_dates():
    lo, hi = resolve_period(None, "2027-03-05", "2027-03-05", NOW)
    assert hi - lo == timedelta(days=1)  # an end date counts the whole day
    lo, hi = resolve_period(None, "2027-03-05T14:00", None, NOW)
    assert lo.tzinfo is KYIV and hi - lo == timedelta(days=1)


@pytest.mark.parametrize(
    ("word", "start", "end"),
    [
        ("someday", None, None),
        ("today", "2027-03-01", None),
        (None, None, None),
        (None, "5 March", None),
        (None, "2027-03-05", "2027-03-01"),
        (None, "2027-01-01", "2028-06-01"),
        (None, "x" * 1000, None),
    ],
)
def test_bad_periods(word, start, end):
    with pytest.raises(CalendarError):
        resolve_period(word, start, end, NOW)


def test_tomorrow_with_repeats():
    out = cal().list_events(period="tomorrow")
    titles = [e["title"] for e in out["events"]]
    assert titles == ["Standup", "Dentist"]
    assert out["events"][0]["start"] == "2027-03-11T10:00+02:00"
    assert out["events"][0]["calendar"] == "Work"
    assert out["truncated"] is False


def test_all_day_event_has_dates():
    out = cal().list_events(period="this_week")
    holiday = next(e for e in out["events"] if e["title"] == "Holiday")
    assert holiday == {
        "calendar": "Home",
        "title": "Holiday",
        "all_day": True,
        "start": "2027-03-13",
        "end": "2027-03-14",
    }


def test_other_zone_shown_in_local_time():
    ny = timezone(timedelta(hours=-5), "EST")
    e = Event(
        "w",
        "Call",
        datetime(2027, 3, 10, 9, 0, tzinfo=ny),
        datetime(2027, 3, 10, 10, tzinfo=ny),
        timezone="America/New_York",
    )
    out = cal(MemoryStore([Calendar("w", "Work")], [e])).list_events(period="today")
    assert out["events"][0]["start"] == "2027-03-10T16:00+02:00"
    assert out["events"][0]["event_timezone"] == "America/New_York"


def test_hidden_calendars_and_filter():
    store = MemoryStore(
        [Calendar("a", "A"), Calendar("b", "B", enabled=False)],
        [Event("b", "Secret", NOW, NOW + timedelta(hours=1))],
    )
    assert cal(store).list_events(period="today")["events"] == []
    assert len(cal(store).list_events(period="today", calendars=["b"])["events"]) == 1
    with pytest.raises(CalendarError, match="unknown calendars"):
        cal(store).list_events(period="today", calendars=["zzz"])


def test_limits():
    many = [
        Event(
            "a",
            f"E{i}",
            NOW + timedelta(minutes=i),
            NOW + timedelta(minutes=i + 1),
            description="d" * 10_000,
        )
        for i in range(MAX_EVENTS + 50)
    ]
    out = cal(MemoryStore([Calendar("a", "A")], many)).list_events(period="today")
    assert len(out["events"]) == MAX_EVENTS and out["truncated"] is True
    assert len(out["events"][0]["description"]) == MAX_DESCRIPTION


def test_search():
    out = cal().search_events(query="dent")
    assert [e["title"] for e in out["events"]] == ["Dentist"]
    assert cal().search_events(query="room 2")["events"][0]["title"] == "Standup"
    for bad in ("", "   ", "x" * 500):
        with pytest.raises(CalendarError):
            cal().search_events(query=bad)


def _call(server, name, arguments):
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return server.handle(msg)["result"]


def test_server_checks_arguments():
    server = Server(cal())
    assert _call(server, "list_events", {"period": "tomorrow"})["isError"] is False
    assert _call(server, "list_events", {"period": "yesterday"})["isError"] is True
    assert _call(server, "list_events", {"calendars": "work", "period": "today"})["isError"]
    assert _call(server, "list_events", {"when": "today"})["isError"] is True
    assert _call(server, "search_events", {})["isError"] is True
    names = [
        t["name"]
        for t in server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"][
            "tools"
        ]
    ]
    assert names == ["list_calendars", "list_events", "search_events"]


def test_server_survives_junk():
    out = io.StringIO()
    lines = ["{", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1]})]
    Server(cal()).serve(io.StringIO("\n".join(lines) + "\n"), out)
    assert len(out.getvalue().splitlines()) == 2


def test_runs_as_module():
    msg = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_calendars", "arguments": {}},
        }
    )
    done = subprocess.run(
        [sys.executable, "-m", "vilna_mcp_calendar", "--demo"],
        input=msg + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    names = [c["name"] for c in json.loads(done.stdout)["result"]["structuredContent"]["calendars"]]
    assert names == ["Work", "Home"]
