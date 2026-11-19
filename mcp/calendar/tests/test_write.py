# SPDX-License-Identifier: Apache-2.0
"""Writing to the calendar (plan item 27): single events, every change undoable."""

import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from vilna_mcp_calendar.calendar import (
    MAX_TITLE,
    UNDO_KEEP,
    Calendar,
    CalendarError,
    Calendars,
    DirUndo,
    Event,
    MemoryStore,
)
from vilna_mcp_calendar.server import Server, demo_store

KYIV = timezone(timedelta(hours=2), "EET")
NOW = datetime(2027, 3, 10, 9, 30, tzinfo=KYIV)  # a Wednesday


def cal(store=None, undo=None):
    return Calendars(store or demo_store(NOW), now=lambda: NOW, undo=undo)


def day(c, when="2027-03-12"):
    return c.list_events(start=when)["events"]


def test_create_timed_event_in_the_default_calendar():
    c = cal()
    out = c.create_event(title="  Lunch   with\nOla ", start="2027-03-12T13:00")
    made = out["created"]
    assert made["title"] == "Lunch with Ola"  # one line
    assert made["calendar"] == "Home" and made["calendar_id"] == "home"
    assert (made["start"], made["end"]) == ("2027-03-12T13:00+02:00", "2027-03-12T14:00+02:00")
    assert [e["title"] for e in day(c)] == ["Standup", "Lunch with Ola"]
    assert set(out["undo_arguments"]) == {"token"}


def test_create_all_day_from_a_date_alone():
    c = cal()
    made = c.create_event(title="Trip", start="2027-03-20", days=3, calendar="work")["created"]
    assert made["all_day"] is True
    assert (made["start"], made["end"]) == ("2027-03-20", "2027-03-23")


@pytest.mark.parametrize(
    "args",
    [
        {"title": "", "start": "2027-03-12T13:00"},
        {"title": "x" * (MAX_TITLE + 1), "start": "2027-03-12T13:00"},
        {"title": "A", "start": "next Friday"},
        {"title": "A", "start": "20277-03-12T13:00"},
        {"title": "A", "start": "2300-03-12T13:00"},
        {"title": "A", "start": "2027-03-12T13:00", "end": "2027-03-12T12:00"},
        {"title": "A", "start": "2027-03-12T13:00", "end": "2027-03-12"},
        {"title": "A", "start": "2027-03-12T13:00", "end": "2029-03-12T13:00"},
        {"title": "A", "start": "2027-03-12T13:00", "days": 2},
        {"title": "A", "start": "2027-03-12", "end": "2027-03-14"},
        {"title": "A", "start": "2027-03-12", "all_day": False},
        {"title": "A", "start": "2027-03-12", "days": 0},
        {"title": "A", "start": "2027-03-12T13:00", "calendar": "nope"},
        {"title": "A\x07", "start": "2027-03-12T13:00"},
    ],
)
def test_create_refuses_what_is_unclear(args):
    c = cal()
    with pytest.raises(CalendarError):
        c.create_event(**args)
    assert [e["title"] for e in day(c)] == ["Standup"]


def test_read_only_calendar_is_refused():
    store = MemoryStore([Calendar("h", "Holidays", writable=False)], [])
    with pytest.raises(CalendarError, match="read only"):
        cal(store).create_event(title="A", start="2027-03-12T13:00", calendar="h")
    with pytest.raises(CalendarError, match="no calendar"):
        cal(store).create_event(title="A", start="2027-03-12T13:00")


def test_undo_a_new_event_removes_it_and_redo_brings_it_back():
    c = cal()
    out = c.create_event(title="Lunch", start="2027-03-12T13:00")
    undone = c.restore(**out["undo_arguments"])
    assert undone["removed"] is True
    assert [e["title"] for e in day(c)] == ["Standup"]
    with pytest.raises(CalendarError, match="already used"):
        c.restore(**out["undo_arguments"])  # a token works once
    redone = c.restore(**undone["undo_arguments"])
    assert redone["restored"]["title"] == "Lunch"
    assert [e["title"] for e in day(c)] == ["Standup", "Lunch"]


def _one(title, start, end, **kw):
    return MemoryStore([Calendar("a", "A")], [Event("a", title, start, end, uid="x1", **kw)])


def test_update_keeps_what_is_not_given_and_the_length():
    store = _one(
        "Dentist",
        datetime(2027, 3, 11, 16, tzinfo=KYIV),
        datetime(2027, 3, 11, 16, 45, tzinfo=KYIV),
        location="Clinic",
    )
    c = cal(store)
    out = c.update_event(id="x1", start="2027-03-12T10:00")
    moved = out["changed"]
    assert (moved["start"], moved["end"]) == ("2027-03-12T10:00+02:00", "2027-03-12T10:45+02:00")
    assert moved["location"] == "Clinic" and out["fields"] == ["end", "start"]
    # A date alone moves it to that day at the same time.
    moved = c.update_event(id="x1", start="2027-03-15")["changed"]
    assert moved["start"] == "2027-03-15T10:00+02:00" and moved["all_day"] is False
    # Clearing the place.
    assert "location" not in c.update_event(id="x1", location="")["changed"]
    with pytest.raises(CalendarError, match="nothing to change"):
        c.update_event(id="x1", title="Dentist")


def test_update_then_undo_puts_back_exactly():
    store = _one(
        "Dentist",
        datetime(2027, 3, 11, 16, tzinfo=KYIV),
        datetime(2027, 3, 11, 17, tzinfo=KYIV),
        description="Bring the card",
    )
    c = cal(store)
    before = c.list_events(period="tomorrow")["events"]
    out = c.update_event(id="x1", title="Dentist (moved)", start="2027-03-11T18:00")
    assert c.list_events(period="tomorrow")["events"] != before
    c.restore(**out["undo_arguments"])
    assert c.list_events(period="tomorrow")["events"] == before


def test_delete_then_undo():
    c = cal()
    made = c.create_event(title="Lunch", start="2027-03-12T13:00")["created"]
    out = c.delete_event(id=made["id"])
    assert out["deleted"]["title"] == "Lunch"
    assert [e["title"] for e in day(c)] == ["Standup"]
    back = c.restore(**out["undo_arguments"])
    assert back["restored"]["title"] == "Lunch"
    assert [e["title"] for e in day(c)] == ["Standup", "Lunch"]


def test_repeats_and_guests_are_left_to_the_calendar_app():
    c = cal()  # the demo's Standup repeats every day
    standup = day(c)[0]
    for change in (
        lambda: c.update_event(id=standup["id"], title="X"),
        lambda: c.delete_event(id=standup["id"]),
    ):
        with pytest.raises(CalendarError, match="repeats"):
            change()
    store = _one("Board", NOW, NOW + timedelta(hours=1), attendees=True)
    assert cal(store).list_events(period="today")["events"][0]["invited_others"] is True
    with pytest.raises(CalendarError, match="invited"):
        cal(store).delete_event(id="x1")


def test_unknown_ids_and_tokens():
    c = cal()
    for bad in ("", "nope", "x" * 5000):
        with pytest.raises(CalendarError):
            c.delete_event(id=bad)
    for bad in ("", "../../etc/passwd", "0" * 32, 5):
        with pytest.raises(CalendarError):
            c.restore(token=bad)


def test_undo_files_are_private_and_bounded(tmp_path, monkeypatch):
    undo = DirUndo(tmp_path / "undo")
    c = cal(undo=undo)
    out = c.create_event(title="Lunch", start="2027-03-12T13:00")
    token = out["undo_arguments"]["token"]
    path = tmp_path / "undo" / f"{token}.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "undo").st_mode) == 0o700
    assert json.loads(path.read_text())["snapshot"] is None
    c.restore(token=token)
    assert not path.exists()
    monkeypatch.setattr("vilna_mcp_calendar.calendar.UNDO_KEEP", 3)
    for _ in range(5):
        undo.save({"calendar": "a", "uid": "b", "snapshot": None})
    assert len(list((tmp_path / "undo").glob("*.json"))) <= 3
    assert UNDO_KEEP >= 100


def _call(server, name, arguments):
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return server.handle(msg)["result"]


def test_server_checks_write_arguments():
    server = Server(cal())
    ok = _call(server, "create_event", {"title": "A", "start": "2027-03-12T13:00"})
    assert ok["isError"] is False
    assert "undo_arguments" in ok["structuredContent"]
    for bad in (
        {"title": "A"},
        {"title": "A", "start": "2027-03-12", "days": "2"},
        {"title": "A", "start": "2027-03-12", "days": True},
        {"title": "A", "start": "2027-03-12", "days": 400},
        {"title": "A", "start": "2027-03-12", "all_day": "yes"},
        {"title": "A", "start": "2027-03-12", "guests": ["a@b.c"]},
    ):
        assert _call(server, "create_event", bad)["isError"] is True, bad
    assert _call(server, "update_event", {"id": 5})["isError"] is True
    assert _call(server, "restore", {})["isError"] is True


def test_demo_server_writes_through_stdio():
    lines = [
        {"name": "create_event", "arguments": {"title": "Lunch", "start": "2030-01-02T13:00"}},
        {"name": "search_events", "arguments": {"query": "lunch", "start": "2030-01-02"}},
    ]
    stdin = "".join(
        json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": p}) + "\n"
        for i, p in enumerate(lines)
    )
    done = subprocess.run(
        [sys.executable, "-m", "vilna_mcp_calendar", "--demo"],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    found = json.loads(done.stdout.splitlines()[1])["result"]["structuredContent"]["events"]
    assert [e["title"] for e in found] == ["Lunch"]
