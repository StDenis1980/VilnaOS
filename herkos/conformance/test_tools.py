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
