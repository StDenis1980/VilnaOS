# SPDX-License-Identifier: GPL-3.0-or-later
"""Tools started by Herkos itself (permissions.md 9.5), checked from outside:
the files and backup MCP servers (mcp/files, mcp/backup) must be installed,
as they are in the dev environment of herkos/.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from conftest import Client, start_herkos

pytestmark = pytest.mark.skipif(
    not (shutil.which("vilna-mcp-files") and shutil.which("vilna-mcp-backup")),
    reason="the tool servers are not installed",
)


def call(client, tool, operation, paths=(), arguments=None):
    params = {
        "tool": tool,
        "operation": operation,
        "reason": "conformance",
        "resources": [Path(p).as_uri() for p in paths],
    }
    if arguments is not None:
        params["arguments"] = arguments
    return client.call("herkos.request_action", params)["result"]


def test_r0_runs_without_asking(client, herkos):
    client.hello()
    r = call(client, "org.vilna.mcp.backup", "status")
    assert (r["decision"], r["code"], r["level"]) == ("allowed", "allowed", "R0")
    assert "result" in r
    # R0 content is Herkos's own tool reporting, not external: no taint.
    assert client.call("herkos.session")["result"]["tainted"] is False
    path = herkos.state / "herkos" / "journal.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    mine = [rec for rec in records if rec["action_id"] == r["action_id"]]
    assert [rec["kind"] for rec in mine] == ["request", "decision", "result"]
    assert mine[2]["data"]["ok"] is True and mine[2]["data"]["result"].startswith("sha256:")


def test_r1_asks_and_no_means_no(client, herkos):
    client.hello()
    (herkos.docs / "a.txt").write_text("hello", encoding="utf-8")
    r = call(
        client,
        "org.vilna.mcp.files",
        "read",
        [herkos.docs / "a.txt"],
        {"path": str(herkos.docs / "a.txt")},
    )
    assert (r["decision"], r["code"], r["level"]) == ("denied", "denied_user", "R1")
    assert "result" not in r
    assert client.call("herkos.session")["result"]["tainted"] is False


def test_tools_lists_the_running_tools_within_max_level(client):
    """herkos.tools (protocol.md 2, ADR 0017): the same answer from every Herkos."""
    assert client.call("herkos.tools")["error"]["code"] == -32001
    max_level = client.hello()["result"]["max_level"]
    tools = client.call("herkos.tools")["result"]["tools"]
    assert [t["tool"] for t in tools] == ["org.vilna.mcp.backup", "org.vilna.mcp.files"]
    files = next(t for t in tools if t["tool"] == "org.vilna.mcp.files")
    assert files["description"] == "Files in the folders the user allowed"
    ops = {op["name"]: op for op in files["operations"]}
    assert list(ops) == sorted(ops)
    assert ops["read"] == {
        "name": "read",
        "summary": "Read a file",
        "level": "R1",
        "returns_external": True,
    }
    assert ops["write"]["level"] == "R2" and ops["write"]["returns_external"] is False
    # delete is R3: listed only where Herkos may run R3.
    assert ("delete" in ops) == (max_level in {"R3", "R4"})
    assert all(op["level"] <= max_level for t in tools for op in t["operations"])


CATALOGUE_FILES = """tool = "org.example.files"
description = "Files, from the catalogue"
[launch]
program = "vilna-mcp-files"
module = "vilna_mcp_files"
args = ["backup_dir", "roots"]
[operations.read]
level = "R1"
summary = "Read a file"
returns_external = true
resource_args = ["path"]
"""


def test_catalogue_manifests_are_started_and_listed(tmp_path):
    """ADR 0017 step 1: trusted catalogue manifests are known; others stay unknown."""
    catalogue = tmp_path / "manifests"
    catalogue.mkdir(mode=0o755)
    (catalogue / "example-files.toml").write_text(CATALOGUE_FILES, encoding="utf-8")
    (catalogue / "example-files.toml").chmod(0o644)
    loose = CATALOGUE_FILES.replace("org.example.files", "org.example.loose")
    (catalogue / "loose.toml").write_text(loose, encoding="utf-8")
    (catalogue / "loose.toml").chmod(0o666)
    with start_herkos(tmp_path, extra_env={"HERKOS_MANIFEST_DIR": str(catalogue)}) as h:
        client = Client(h.socket)
        try:
            client.hello()
            tools = client.call("herkos.tools")["result"]["tools"]
            assert [t["tool"] for t in tools] == [
                "org.example.files",
                "org.vilna.mcp.backup",
                "org.vilna.mcp.files",
            ]
            assert tools[0]["description"] == "Files, from the catalogue"
            assert tools[0]["operations"] == [
                {"name": "read", "summary": "Read a file", "level": "R1", "returns_external": True}
            ]
            r = call(client, "org.example.loose", "read", [h.docs / "a.txt"], {"path": "a.txt"})
            assert (r["decision"], r["code"]) == ("denied", "denied_unknown")
        finally:
            client.close()
