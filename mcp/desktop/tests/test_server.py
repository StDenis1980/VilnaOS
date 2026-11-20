# SPDX-License-Identifier: Apache-2.0
"""The servers speak MCP, and their tools are exactly their Herkos manifests'
operations (ADR 0031): a tool without a level would be unknown to Herkos, an
operation without a tool would never work."""

import io
import json
import tomllib
from pathlib import Path

import pytest
from fakes import FakeBus, FakeNode

from vilna_mcp_desktop import atspi, desktop, portals

MANIFESTS = Path(__file__).resolve().parents[3] / "herkos" / "src" / "herkos" / "manifests"
if not MANIFESTS.is_dir():
    # The package's own tarball (OBS) has no Herkos next to it.
    pytest.skip("needs the Herkos manifests of the repository", allow_module_level=True)
SERVERS = {
    "org.vilna.mcp.desktop": (desktop, lambda: desktop.server(desktop.Desktop(FakeBus({})))),
    "org.vilna.mcp.portals": (portals, lambda: portals.server(portals.Portals(FakeBus({})))),
    "org.vilna.mcp.atspi": (
        atspi,
        lambda: atspi.server(atspi.Apps(lambda: FakeNode("desktop frame"))),
    ),
}


@pytest.mark.parametrize("tool", SERVERS)
def test_tools_match_the_manifest(tool):
    module, _ = SERVERS[tool]
    manifest = tomllib.loads((MANIFESTS / f"{tool}.toml").read_text())
    assert {t["name"] for t in module.TOOLS} == set(manifest["operations"])
    assert manifest["launch"]["program"] == f"vilna-mcp-{tool.rsplit('.', 1)[1]}"
    for t in module.TOOLS:
        op = manifest["operations"][t["name"]]
        # Herkos raises an operation a tool says changes things; it never lowers.
        if op["level"] == "R0":
            assert t["annotations"].get("readOnlyHint") is True, t["name"]


def talk(server, *messages):
    out = io.StringIO()
    server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in messages)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


@pytest.mark.parametrize("tool", SERVERS)
def test_speaks_mcp_and_refuses_bad_arguments(tool):
    _, make = SERVERS[tool]
    first, listing, unknown, bad = talk(
        make(),
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "eval", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": listing_first(tool), "arguments": {"surprise": 1}},
        },
    )
    assert first["result"]["serverInfo"]["name"].startswith("vilna-mcp-")
    assert listing["result"]["tools"]
    assert unknown["result"]["isError"] and bad["result"]["isError"]


def listing_first(tool):
    return SERVERS[tool][0].TOOLS[0]["name"]


def test_a_huge_line_is_answered_not_read_whole():
    server = SERVERS["org.vilna.mcp.desktop"][1]()
    out = io.StringIO()
    server.serve(
        io.StringIO("x" * (2 * 1024 * 1024) + "\n" + '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'),
        out,
    )
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines[0]["error"]["message"] == "line too long"
    assert lines[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}
