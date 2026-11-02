# SPDX-License-Identifier: GPL-3.0-or-later
"""Decisions of the gate and the Mitos journal (permissions.md 4, 7.1), checked
from outside. Nothing here depends on a tool running or on a person answering:
every case is decided before either is needed, or only its level is checked.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

FILES = "org.vilna.mcp.files"
BACKUP = "org.vilna.mcp.backup"


def action(client, tool, operation, paths=(), arguments=None, **extra):
    params = {
        "tool": tool,
        "operation": operation,
        "reason": "conformance",
        "resources": [Path(p).as_uri() for p in paths],
        **extra,
    }
    if arguments is not None:
        params["arguments"] = arguments
    return client.call("herkos.request_action", params)["result"]


def files(client, operation, path, **extra):
    return action(client, FILES, operation, [path], {"path": str(path)}, **extra)


@pytest.fixture
def hello(client):
    client.hello()
    return client


def test_max_level_is_r2(hello):
    # Neither runs as a separate user yet, so neither carries out R3-R4.
    assert hello.call("herkos.hello", {"protocol": "0.1"})["result"]["max_level"] == "R2"


def test_unknown_tool_is_r4(hello):
    r = action(hello, "org.example.unknown", "read")
    assert (r["decision"], r["code"], r["level"]) == ("denied", "denied_unknown", "R4")


def test_unknown_operation_is_forbidden(hello, herkos):
    r = files(hello, "chmod", herkos.docs / "a.txt")
    assert (r["code"], r["level"]) == ("denied_unknown", "forbidden")


def test_levels_above_r2_are_denied(hello, herkos):
    r = files(hello, "delete", herkos.docs / "a.txt")
    assert (r["code"], r["level"]) == ("denied_level", "R3")
    r = action(hello, BACKUP, "forget")
    assert (r["code"], r["level"]) == ("denied_level", "R4")
    # Not undoable is at least R3, egress too.
    r = action(hello, BACKUP, "configure_destination")
    assert (r["code"], r["level"]) == ("denied_level", "R3")


def test_outside_the_allowed_folders_raises(hello, herkos):
    outside = herkos.docs.parent / "elsewhere.txt"
    r = files(hello, "write", outside)
    assert (r["code"], r["level"]) == ("denied_level", "R3")
    # A symlink inside the folder that points out of it is outside too.
    link = herkos.docs / "link"
    link.symlink_to(herkos.docs.parent)
    r = files(hello, "write", link / "x.txt")
    assert (r["code"], r["level"]) == ("denied_level", "R3")
    # Reading outside: R1 + 1, denied (no person answers here).
    r = files(hello, "read", outside)
    assert (r["decision"], r["level"]) == ("denied", "R2")


def test_bulk_raises(hello, herkos):
    paths = [herkos.docs / f"{i}.txt" for i in range(21)]
    r = action(hello, FILES, "write", paths, {"path": str(paths[0])})
    assert (r["code"], r["level"]) == ("denied_level", "R3")


def test_arguments_must_name_declared_resources(hello, herkos):
    r = action(hello, FILES, "read", [herkos.docs / "a.txt"], {"path": "/etc/passwd"})
    assert r["code"] == "denied_mismatch"
    r = action(hello, FILES, "read", [herkos.docs / "a.txt"])
    assert r["code"] == "denied_mismatch"
    # The same path spelled differently is the same resource.
    r = action(hello, FILES, "delete", [herkos.docs / "a.txt"], {"path": f"{herkos.docs}/./a.txt"})
    assert r["code"] == "denied_level"


def test_in_bounds_read_is_r1(hello, herkos):
    r = files(hello, "read", herkos.docs / "a.txt")
    assert r["level"] == "R1"


def journal(herkos) -> list[dict]:
    path = herkos.state / "herkos" / "journal.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_journal_records_every_action(hello, herkos):
    secret = "do-not-journal-this-value"
    path = herkos.docs / "a.txt"
    r = action(hello, FILES, "delete", [path], {"path": str(path), "note": secret})
    records = journal(herkos)
    assert records[0]["kind"] == "start"
    mine = [rec for rec in records if rec["action_id"] == r["action_id"]]
    assert [rec["kind"] for rec in mine] == ["request", "decision"]
    request, decision = mine[0]["data"], mine[1]["data"]
    assert (request["tool"], request["operation"], request["resource_count"]) == (
        FILES,
        "delete",
        1,
    )
    assert request["arguments"].startswith("sha256:")
    assert (decision["decision"], decision["code"], decision["level"]) == (
        "denied",
        "denied_level",
        "R3",
    )
    # Arguments are journalled as a hash only.
    assert secret not in (herkos.state / "herkos" / "journal.jsonl").read_text(encoding="utf-8")


def test_fields_the_model_sends_do_not_reach_the_journal(hello, herkos):
    r = files(hello, "read", herkos.docs / "a.txt", tainted=True, level="R0")
    request = next(rec for rec in journal(herkos) if rec["action_id"] == r["action_id"])
    assert request["data"]["tainted"] is False


def test_journal_verifies_with_herkos_lite(hello, herkos):
    files(hello, "delete", herkos.docs / "a.txt")
    files(hello, "read", herkos.docs / "ї ü.txt")
    hello.call("herkos.undo", {"action_id": "0123456789abcdef"})
    done = subprocess.run(
        [sys.executable, "-m", "herkos", "journal", "verify"],
        env=herkos.env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(journal(herkos)) >= 5
