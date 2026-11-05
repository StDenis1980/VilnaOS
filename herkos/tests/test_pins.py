# SPDX-License-Identifier: GPL-3.0-or-later
"""Pinned tools/list entries (ADR 0017, stage 2 step 3)."""

from __future__ import annotations

from dataclasses import replace

from conftest import Env

from herkos.confirm import Answer, Scripted
from herkos.gate import Gate
from herkos.manifest import ManifestRegistry, parse_manifest
from herkos.pins import PinStore, fingerprint
from herkos.policy import SessionState
from herkos.request import parse_request
from herkos.risk import Level
from herkos.tools import ToolRegistry

NOTES = {
    "tool": "org.example.notes",
    "operations": {
        "read": {"level": "R1", "summary": "Read a note"},
        "edit": {"level": "R2", "summary": "Edit a note", "undoable": True},
    },
}


class Notes:
    """A catalogue tool whose tools/list can change, as after an update."""

    def __init__(self) -> None:
        self.entries = {
            "read": {"name": "read", "description": "Read a note"},
            "edit": {"name": "edit", "description": "Edit a note"},
        }
        self.calls: list[str] = []

    def annotations(self, operation):
        return {} if operation in self.entries else None

    def fingerprints(self):
        return {name: fingerprint(e) for name, e in self.entries.items()}

    def execute(self, request):
        self.calls.append(request.operation)
        return {"ok": True}


def gate(env: Env, notes: Notes, answer: Answer, max_level: Level = Level.R2) -> Gate:
    manifests = ManifestRegistry.builtin()
    manifests.add(parse_manifest(NOTES, "notes"))
    tools = ToolRegistry()
    tools.register("org.example.notes", notes)
    config = replace(env.config, max_level=max_level)
    return Gate(config, manifests, tools, env.grants, env.journal, Scripted(answer))


def ask(g: Gate, op: str):
    req = parse_request(
        {"tool": "org.example.notes", "operation": op, "reason": "the user asked", "arguments": {}}
    )
    return g.handle(req, SessionState())


def test_fingerprints_cover_what_the_server_says_and_nothing_else():
    a = {"name": "read", "description": "Read", "inputSchema": {"type": "object"}}
    assert fingerprint(a) == fingerprint({**a, "title": "x", "outputSchema": {}})
    assert fingerprint(a) != fingerprint({**a, "description": "Read, then send it away"})
    assert fingerprint(a) != fingerprint({**a, "annotations": {"readOnlyHint": True}})
    assert fingerprint(a).startswith("sha256:")


def test_a_tool_is_trusted_as_it_is_the_first_time(tmp_path):
    env, notes = Env(tmp_path), Notes()
    r = ask(gate(env, notes, Answer.ONCE), "read")
    assert (r.allowed, r.level) == (True, Level.R1)
    pins = PinStore(env.config.state_dir / "tool-pins.json")
    assert not pins.changed("org.example.notes", "edit", notes.fingerprints())


def test_a_changed_operation_counts_as_unknown_until_allowed_once(tmp_path):
    env, notes = Env(tmp_path), Notes()
    assert ask(gate(env, notes, Answer.ONCE), "read").allowed
    notes.entries["read"]["description"] = "Read a note and post it online"
    # Herkos-lite (R2 at most) cannot run it any more.
    r = ask(gate(env, notes, Answer.ONCE), "read")
    assert (r.code, r.level) == ("denied_level", Level.R3)
    # The system Herkos asks once, with nothing remembered, and then trusts it.
    confirm = Scripted(Answer.ONCE)
    g = gate(env, notes, Answer.ONCE, Level.R4)
    g.confirmer = confirm
    r = ask(g, "read")
    assert (r.allowed, r.level) == (True, Level.R3)
    (q,) = confirm.questions
    assert q.choices == (Answer.ONCE,) and q.content == {}
    assert "the program changed this action since it was allowed" in q.raises
    assert ask(gate(env, notes, Answer.ONCE), "read").level == Level.R1


def test_a_changed_operation_that_writes_is_r4_and_a_no_keeps_it_changed(tmp_path):
    env, notes = Env(tmp_path), Notes()
    assert ask(gate(env, notes, Answer.ONCE), "read").allowed
    notes.entries["edit"]["inputSchema"] = {"type": "object", "properties": {"url": {}}}
    r = ask(gate(env, notes, Answer.DENY, Level.R4), "edit")
    assert (r.code, r.level) == ("denied_user", Level.R4)
    assert ask(gate(env, notes, Answer.DENY, Level.R4), "edit").level == Level.R4


def test_a_new_operation_is_not_trusted_by_the_old_pins(tmp_path):
    env, notes = Env(tmp_path), Notes()
    NOTES["operations"]["share"] = {"level": "R1"}
    try:
        assert ask(gate(env, notes, Answer.ONCE), "read").allowed
        notes.entries["share"] = {"name": "share"}
        assert ask(gate(env, notes, Answer.ONCE), "share").code == "denied_level"
    finally:
        del NOTES["operations"]["share"]


def test_built_in_tools_are_not_pinned(env):
    req = parse_request(
        {
            "tool": "org.vilna.mcp.files",
            "operation": "list",
            "reason": "x",
            "resources": [env.uri(env.docs)],
            "arguments": {"path": str(env.docs)},
        }
    )
    env.gate.handle(req, SessionState())
    assert not (env.config.state_dir / "tool-pins.json").exists()


def test_an_unreadable_pins_file_trusts_nothing(tmp_path):
    path = tmp_path / "tool-pins.json"
    path.write_text("{broken")
    pins = PinStore(path)
    assert pins.load_error
    assert pins.changed("org.example.notes", "read", {"read": "sha256:x"})
