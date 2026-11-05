# SPDX-License-Identifier: GPL-3.0-or-later
"""herkos.tools: what Gloxi may offer the model (protocol.md 2, ADR 0017)."""

from __future__ import annotations

from dataclasses import replace

from conftest import Env

from herkos.risk import Level
from herkos.tools import ToolRegistry


class SomeOps:
    """A files server that has only some operations."""

    def __init__(self, ops):
        self.ops = set(ops)

    def annotations(self, operation):
        return {} if operation in self.ops else None

    def execute(self, request):  # pragma: no cover - never called here
        raise AssertionError


def names(tools):
    return [op["name"] for op in tools[0]["operations"]]


def test_lists_what_the_manifest_allows_and_the_server_has(env: Env):
    tools = ToolRegistry()
    tools.register("org.vilna.mcp.files", SomeOps({"read", "write", "delete", "chmod"}))
    env.gate.tools = tools
    listed = env.gate.available_tools()
    # Backup is not running; files is.
    assert [t["tool"] for t in listed] == ["org.vilna.mcp.files"]
    assert listed[0]["description"] == "Files in the folders the user allowed"
    # delete is R3, above Herkos-lite's R2; search is not on the server;
    # chmod is on the server but not in the manifest.
    assert names(listed) == ["read", "write"]
    assert listed[0]["operations"][0] == {
        "name": "read",
        "summary": "Read a file",
        "level": "R1",
        "returns_external": True,
    }


def test_max_level_decides_which_operations_are_listed(env: Env):
    tools = ToolRegistry()
    tools.register("org.vilna.mcp.files", SomeOps({"read", "delete"}))
    env.gate.tools = tools
    env.gate.config = replace(env.config, max_level=Level.R4)
    assert names(env.gate.available_tools()) == ["delete", "read"]


def test_a_tool_with_no_manifest_is_never_listed(env: Env):
    env.gate.tools.register("org.example.unknown", SomeOps({"read"}))
    assert all(t["tool"] != "org.example.unknown" for t in env.gate.available_tools())


def test_a_tool_that_reports_no_operations_is_not_listed(env: Env):
    # FakeFiles has no annotations: it reports no operations at all.
    assert env.gate.available_tools() == []
