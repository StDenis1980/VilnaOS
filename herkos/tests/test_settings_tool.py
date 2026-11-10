# SPDX-License-Identifier: GPL-3.0-or-later
"""End to end: the gate drives the settings MCP server (mcp/settings, ADR 0023)."""

import json
import sys

import pytest

from herkos.config import Config
from herkos.confirm import Answer, Scripted
from herkos.gate import UNDO_ARGUMENTS_MAX, Gate, undo_info
from herkos.grants import GrantStore
from herkos.manifest import ManifestRegistry
from herkos.mcp_client import McpStdioExecutor
from herkos.mitos import Journal, verify
from herkos.policy import SessionState
from herkos.request import parse_request
from herkos.risk import Level
from herkos.tools import ToolRegistry

pytest.importorskip("vilna_mcp_settings")
TOOL = "org.vilna.mcp.settings"


@pytest.fixture
def setup(tmp_path):
    config = Config(allowed_roots=(), state_dir=tmp_path / "state")
    server = McpStdioExecutor([sys.executable, "-m", "vilna_mcp_settings", "--memory"])
    server.start()
    tools = ToolRegistry()
    tools.register(TOOL, server)
    confirmer = Scripted(Answer.ONCE)
    gate = Gate(
        config,
        ManifestRegistry.builtin(),
        tools,
        GrantStore(config.grants_path),
        Journal(config.journal_path),
        confirmer,
    )
    yield gate, config, confirmer
    server.stop()


def call(gate, op, **args):
    return gate.handle(
        parse_request({"tool": TOOL, "operation": op, "reason": "test", "arguments": args}),
        SessionState(),
    )


def scheme(gate):
    out = call(gate, "get")
    assert out.code == "allowed", out.message
    return out.result["values"]["color_scheme"]


def test_levels():
    m = ManifestRegistry.builtin().get(TOOL)
    assert m.operation("get").level is Level.R0
    for op in m.operations:
        if op != "get":
            assert m.operation(op).level is Level.R2, op
            assert m.operation(op).undoable, op


def test_dark_then_undo_then_redo(setup):
    gate, config, confirmer = setup
    out = call(gate, "set_color_scheme", scheme="dark")
    assert out.code == "allowed", out.message
    assert out.level is Level.R2
    assert scheme(gate) == "prefer-dark"

    undone = gate.undo(out.action_id, SessionState())
    assert undone.code == "allowed", undone.message
    assert scheme(gate) == "default"
    # The undo is an action too, so it can be undone.
    redone = gate.undo(undone.action_id, SessionState())
    assert redone.code == "allowed", redone.message
    assert scheme(gate) == "prefer-dark"
    assert verify(config.journal_path).ok


def test_refused_change_leaves_nothing_to_undo(setup):
    gate, _, confirmer = setup
    confirmer._answer = Answer.DENY
    out = call(gate, "set_night_light", enabled=True)
    assert out.code == "denied_user"
    assert gate.undo(out.action_id, SessionState()).code == "denied_unknown"


def test_tool_error_is_not_undoable(setup):
    gate, _, _ = setup
    out = call(gate, "set_text_scale", scale=40)
    assert out.code == "tool_error"
    assert gate.undo(out.action_id, SessionState()).code == "denied_unknown"


def test_undo_info_limits():
    op = ManifestRegistry.builtin().get(TOOL).operation("set_color_scheme")
    request = parse_request({"tool": TOOL, "operation": "set_color_scheme", "reason": "t"})
    ok = {"undo_arguments": {"values": {"color_scheme": "default"}}}
    assert undo_info(request, op, ok) == {"arguments": ok["undo_arguments"]}
    huge = {"undo_arguments": {"values": {"x": "a" * UNDO_ARGUMENTS_MAX}}}
    assert undo_info(request, op, huge) is None
    for bad in ({"undo_arguments": []}, {"undo_arguments": {}}, {"undo_arguments": "x"}, {}):
        assert undo_info(request, op, bad) is None
    assert json.dumps(ok)  # what goes in the journal is plain JSON
