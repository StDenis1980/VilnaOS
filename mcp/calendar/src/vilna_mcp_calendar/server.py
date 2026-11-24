# SPDX-License-Identifier: Apache-2.0
"""MCP server over stdio (newline-delimited JSON-RPC 2.0).

    vilna-mcp-calendar [--demo]

Started by Herkos, never by Gloxi directly (ADR 0023). It reads calendars
and, since plan item 27, creates, changes and deletes single events; Herkos
sets the level of each operation and asks the person. ``--demo`` serves a
calendar in memory instead of Evolution Data Server, for tests and demos.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

from vilna_mcp_calendar import __version__
from vilna_mcp_calendar.calendar import (
    MAX_DAYS,
    MAX_DESCRIPTION,
    MAX_LOCATION,
    MAX_TITLE,
    PERIODS,
    Calendar,
    CalendarError,
    Calendars,
    DirUndo,
    Event,
    MemoryStore,
)

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

_DATE = {"type": "string", "description": "ISO 8601 date or date-time, e.g. 2027-03-05"}
_CALENDARS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Calendar ids from list_calendars; all shown calendars if left out",
}
_READ = {"readOnlyHint": True, "openWorldHint": False}
_WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
_ID = {"type": "string", "description": "The event's id from list_events or search_events"}
_CALENDAR = {
    "type": "string",
    "description": "Calendar id from list_calendars; the event's own calendar if left out",
}
_WHEN = {
    "type": "string",
    "description": "ISO 8601 in the person's time zone: 2027-03-05T14:00, or 2027-03-05 "
    "for an all-day event",
}
_EVENT = {
    "title": {"type": "string", "maxLength": MAX_TITLE},
    "start": _WHEN,
    "end": {
        "type": "string",
        "description": "ISO 8601 date-time; one hour after start if left out "
        "(not for all-day events)",
    },
    "all_day": {"type": "boolean"},
    "days": {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_DAYS,
        "description": "How many days an all-day event lasts (1 if left out)",
    },
    "location": {"type": "string", "maxLength": MAX_LOCATION},
    "description": {"type": "string", "maxLength": MAX_DESCRIPTION, "description": "Notes"},
}


def _tool(name, description, props, required, **annotations):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": annotations,
    }


TOOLS = [
    _tool("list_calendars", "List the person's calendars", {}, [], **_READ),
    _tool(
        "list_events",
        "Events in a period: a word such as 'tomorrow', or start and end dates "
        f"(at most {MAX_DAYS} days). Times are in the person's time zone.",
        {
            "period": {"type": "string", "enum": list(PERIODS)},
            "start": _DATE,
            "end": _DATE,
            "calendars": _CALENDARS,
        },
        [],
        **_READ,
    ),
    _tool(
        "search_events",
        "Find events whose title, place or notes contain the words "
        "(from today for a year, unless start and end are given)",
        {"query": {"type": "string"}, "start": _DATE, "end": _DATE, "calendars": _CALENDARS},
        ["query"],
        **_READ,
    ),
    _tool(
        "create_event",
        "Add an event to the person's calendar (only the person, no guests)",
        {
            **_EVENT,
            "calendar": {
                "type": "string",
                "description": "Calendar id from list_calendars; the default calendar if left out",
            },
        },
        ["title", "start"],
        **_WRITE,
    ),
    _tool(
        "update_event",
        "Change a single event: give its id and only what changes. A new start "
        "keeps the event's length; a date alone moves it to that day at the same time.",
        {"id": _ID, "calendar": _CALENDAR, **_EVENT},
        ["id"],
        **_WRITE,
    ),
    _tool(
        "delete_event",
        "Delete a single event (it can be undone)",
        {"id": _ID, "calendar": _CALENDAR},
        ["id"],
        readOnlyHint=False,
        destructiveHint=True,
        openWorldHint=False,
    ),
    _tool(
        "restore",
        "Undo an earlier change to the calendar (Herkos sends the token it kept)",
        {"token": {"type": "string"}},
        ["token"],
        **_WRITE,
    ),
]
_SCHEMAS = {t["name"]: t["inputSchema"] for t in TOOLS}
_TYPES: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "array": lambda v: isinstance(v, list) and len(v) <= 64 and all(isinstance(i, str) for i in v),
}


def check_arguments(name: str, args: Any) -> dict:
    schema = _SCHEMAS[name]
    if not isinstance(args, dict):
        raise CalendarError("arguments must be an object")
    unknown = set(args) - set(schema["properties"])
    if unknown:
        raise CalendarError(f"unknown arguments: {sorted(unknown)}")
    missing = [k for k in schema["required"] if k not in args]
    if missing:
        raise CalendarError(f"missing arguments: {missing}")
    for k, v in args.items():
        prop = schema["properties"][k]
        if not _TYPES[prop["type"]](v):
            raise CalendarError(f"'{k}' must be a {prop['type']}")
        if "enum" in prop and v not in prop["enum"]:
            raise CalendarError(f"'{k}' must be one of {prop['enum']}")
        if "maxLength" in prop and len(v) > prop["maxLength"]:
            raise CalendarError(f"'{k}' must be at most {prop['maxLength']} characters")
        if "minimum" in prop and not prop["minimum"] <= v <= prop["maximum"]:
            raise CalendarError(f"'{k}' must be from {prop['minimum']} to {prop['maximum']}")
    return args


class Server:
    def __init__(self, calendars: Calendars) -> None:
        self.calendars = calendars
        self._handlers: dict[str, Callable[..., dict]] = {
            "list_calendars": calendars.list_calendars,
            "list_events": calendars.list_events,
            "search_events": calendars.search_events,
            "create_event": calendars.create_event,
            "update_event": calendars.update_event,
            "delete_event": calendars.delete_event,
            "restore": calendars.restore,
        }

    def handle(self, msg: Any) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, -32600, "not a JSON-RPC 2.0 message")
        method, id_ = msg.get("method"), msg.get("id")
        if "id" not in msg:
            return None
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return _error(id_, -32602, "params must be an object")
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(
                id_,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "vilna-mcp-calendar", "version": __version__},
                },
            )
        if method == "ping":
            return _result(id_, {})
        if method == "tools/list":
            return _result(id_, {"tools": TOOLS})
        if method == "tools/call":
            return _result(id_, self.call(params.get("name"), params.get("arguments", {})))
        return _error(id_, -32601, f"unknown method {method}")

    def call(self, name: Any, args: Any) -> dict:
        if name not in self._handlers:
            return _tool_error(f"unknown tool {name}")
        try:
            out = self._handlers[name](**check_arguments(name, args))
        except CalendarError as e:
            return _tool_error(str(e))
        except Exception as e:  # GLib errors and the like: report, keep serving
            return _tool_error(f"{type(e).__name__}: {e}")
        return {
            "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out,
            "isError": False,
        }

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                response = _error(None, -32700, "not valid JSON")
            else:
                response = self.handle(msg)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()


def _result(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def demo_store(now: datetime | None = None) -> MemoryStore:
    """Two calendars around today, with an event that repeats."""
    now = now or datetime.now().astimezone()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = [
        Event(
            "work",
            "Standup",
            day + timedelta(days=d, hours=10),
            day + timedelta(days=d, hours=10, minutes=15),
            location="Room 2",
            uid="standup",
            recurring=True,
        )
        for d in range(0, 5)
    ]
    events += [
        Event(
            "home", "Dentist", day + timedelta(days=1, hours=16), day + timedelta(days=1, hours=17)
        ),
        Event("home", "Holiday", day + timedelta(days=3), day + timedelta(days=4), all_day=True),
    ]
    calendars = [
        Calendar("work", "Work"),
        Calendar("home", "Home", default=True),
    ]
    return MemoryStore(calendars, events)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vilna-mcp-calendar")
    p.add_argument("--demo", action="store_true", help="a calendar in memory (tests, demos)")
    p.add_argument(
        "--undo-dir",
        type=Path,
        help="where events are kept for undo "
        "(default: $STATE_DIRECTORY/undo, else $XDG_STATE_HOME/vilna-mcp-calendar/undo; "
        "--demo: in memory)",
    )
    args = p.parse_args(argv)
    undo = DirUndo(args.undo_dir) if args.undo_dir is not None else None
    if args.demo:
        store = demo_store()
    else:
        undo = undo or DirUndo.default()
        try:
            from vilna_mcp_calendar.eds import EdsStore

            store = EdsStore()
        except (ImportError, ValueError, CalendarError) as e:
            print(f"vilna-mcp-calendar: no calendars ({e})", file=sys.stderr)
            return 2
    Server(Calendars(store, undo=undo)).serve(sys.stdin, sys.stdout)
    return 0
