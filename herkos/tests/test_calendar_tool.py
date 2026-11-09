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


def test_read_only_and_levels():
    m = ManifestRegistry.builtin().get(TOOL)
    assert {op: m.operation(op).level for op in m.operations} == {
        "list_calendars": Level.R0,
        "list_events": Level.R1,
        "search_events": Level.R1,
    }
    assert not any(m.operation(op).changes_state for op in m.operations)


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
