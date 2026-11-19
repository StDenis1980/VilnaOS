# SPDX-License-Identifier: GPL-3.0-or-later
"""End to end: the gate drives the calendar MCP server (mcp/calendar, ADR 0023)."""

import sys

import pytest

from herkos.config import Config
from herkos.confirm import Answer, Scripted
from herkos.gate import Gate
from herkos.grants import GrantStore
from herkos.manifest import ManifestRegistry
from herkos.mcp_client import McpStdioExecutor
from herkos.mitos import Journal
from herkos.policy import SessionState
from herkos.request import parse_request
from herkos.risk import Level
from herkos.tools import ToolRegistry

pytest.importorskip("vilna_mcp_calendar")
TOOL = "org.vilna.mcp.calendar"


@pytest.fixture
def gate(tmp_path):
    config = Config(allowed_roots=(), state_dir=tmp_path / "state")
    server = McpStdioExecutor([sys.executable, "-m", "vilna_mcp_calendar", "--demo"])
    server.start()
    tools = ToolRegistry()
    tools.register(TOOL, server)
    confirmer = Scripted(Answer.ALWAYS)
    yield (
        Gate(
            config,
            ManifestRegistry.builtin(),
            tools,
            GrantStore(config.grants_path),
            Journal(config.journal_path),
            confirmer,
        ),
        confirmer,
    )
    server.stop()


def call(gate, op, session, **args):
    request = {"tool": TOOL, "operation": op, "reason": "test", "arguments": args}
    return gate.handle(parse_request(request), session)


def test_levels():
    m = ManifestRegistry.builtin().get(TOOL)
    assert {op: m.operation(op).level for op in m.operations} == {
        "list_calendars": Level.R0,
        "list_events": Level.R1,
        "search_events": Level.R1,
        "create_event": Level.R2,
        "update_event": Level.R2,
        "delete_event": Level.R3,  # like deleting a file
        "restore": Level.R2,
    }
    reads = {"list_calendars", "list_events", "search_events"}
    for op in m.operations:
        assert m.operation(op).changes_state == (op not in reads), op
        assert m.operation(op).undoable == (op not in reads), op


def test_reading_events_asks_once_and_taints(gate):
    g, confirmer = gate
    s = SessionState()
    assert call(g, "list_calendars", s).code == "allowed"
    assert confirmer.questions == []  # R0: nothing to ask
    out = call(g, "list_events", s, period="tomorrow")
    assert out.code == "allowed", out.message
    assert [e["title"] for e in out.result["events"]] == ["Standup", "Dentist"]
    assert s.tainted and out.keros["untrusted"] is True
    # Allowed "always" once; the next read of events asks nothing.
    assert call(g, "list_events", s, period="this_week").code == "allowed"
    assert len(confirmer.questions) == 1


def titles(g, when):
    out = call(g, "list_events", SessionState(), start=when)
    assert out.code == "allowed", out.message
    return [e["title"] for e in out.result["events"]]


def test_new_event_asks_once_then_undo_and_redo(gate):
    g, confirmer = gate
    s = SessionState()
    out = call(g, "create_event", s, title="Lunch", start="2030-01-02T13:00")
    assert out.code == "allowed", out.message
    assert out.level is Level.R2
    assert confirmer.questions[-1].undoable is True
    asked = len(confirmer.questions)
    # Allowed "always" once: the next new event asks nothing (permissions.md 5.1).
    assert call(g, "create_event", s, title="Gym", start="2030-01-02T18:00").code == "allowed"
    assert len(confirmer.questions) == asked
    assert titles(g, "2030-01-02") == ["Lunch", "Gym"]

    undone = g.undo(out.action_id, SessionState())
    assert undone.code == "allowed", undone.message
    assert titles(g, "2030-01-02") == ["Gym"]
    redone = g.undo(undone.action_id, SessionState())
    assert redone.code == "allowed", redone.message
    assert titles(g, "2030-01-02") == ["Lunch", "Gym"]


def test_change_and_its_undo(gate):
    g, _ = gate
    made = call(g, "create_event", SessionState(), title="Dentist", start="2030-01-03T16:00")
    event_id = made.result["created"]["id"]
    moved = call(g, "update_event", SessionState(), id=event_id, start="2030-01-04T09:00")
    assert moved.code == "allowed", moved.message
    assert titles(g, "2030-01-03") == [] and titles(g, "2030-01-04") == ["Dentist"]
    assert g.undo(moved.action_id, SessionState()).code == "allowed"
    assert titles(g, "2030-01-03") == ["Dentist"]


def test_after_reading_a_change_shows_exactly_what_will_be_written(gate):
    """Events are someone else's words: a change after reading them is R3, asked once."""
    g, confirmer = gate
    s = SessionState()
    assert call(g, "list_events", s, period="today").code == "allowed"
    assert s.tainted
    out = call(g, "create_event", s, title="Call Ola", start="2030-01-05T10:00")
    assert out.code == "denied_user"  # "always" is not on offer: only "once"
    assert confirmer.questions[-1].choices == (Answer.ONCE,)
    confirmer._answer = Answer.ONCE
    out = call(g, "create_event", s, title="Call Ola", start="2030-01-05T10:00")
    assert out.code == "allowed", out.message
    assert out.level is Level.R3
    question = confirmer.questions[-1]
    assert question.content == {"title": "Call Ola", "start": "2030-01-05T10:00"}
    assert question.taint_sources


def test_deleting_is_r3_and_not_done_by_herkos_lite(gate):
    g, _ = gate
    made = call(g, "create_event", SessionState(), title="Lunch", start="2030-01-02T13:00")
    out = call(g, "delete_event", SessionState(), id=made.result["created"]["id"])
    assert out.level is Level.R3
    assert out.code == "denied_level", out.message
    assert titles(g, "2030-01-02") == ["Lunch"]
