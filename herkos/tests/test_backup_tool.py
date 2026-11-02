# SPDX-License-Identifier: GPL-3.0-or-later
"""The backup tool (mcp/backup) through the gate: levels of ADR 0007 and design.md 5."""

import json
import sys
from pathlib import Path

import pytest

from herkos.config import Config
from herkos.confirm import Answer, Scripted
from herkos.gate import Gate
from herkos.grants import GrantStore
from herkos.manifest import ManifestRegistry
from herkos.mcp_client import McpStdioExecutor
from herkos.mitos import Journal
from herkos.policy import SessionState, assess
from herkos.request import parse_request
from herkos.risk import Level
from herkos.tools import ToolRegistry

TOOL = "org.vilna.mcp.backup"
MANIFEST = ManifestRegistry.builtin().get(TOOL)


def request(op, resources=(), **arguments):
    return parse_request(
        {
            "tool": TOOL,
            "operation": op,
            "reason": "Людина попросила",
            "resources": [Path(p).as_uri() for p in resources],
            "arguments": arguments,
        }
    )


def level(op, resources=(), roots=(), session=None, **arguments):
    a = assess(
        request(op, resources, **arguments),
        MANIFEST,
        session or SessionState(),
        allowed_roots=roots,
        bulk_threshold=20,
    )
    return a.level


@pytest.mark.parametrize(
    ("op", "want"),
    [
        ("status", Level.R0),
        ("list_snapshots", Level.R0),
        ("overview", Level.R1),
        ("run_now", Level.R2),  # the ADR 0007 exception: not raised by egress or undo
        ("set_schedule", Level.R2),
        ("set_paths", Level.R2),
        ("configure_destination", Level.R3),
        ("restore_in_place", Level.R3),
        ("forget", Level.R4),
        ("rotate_keys", Level.R4),
        ("reissue_repository", Level.R4),
    ],
)
def test_levels(op, want):
    assert level(op) == want


def test_restore_into_a_new_folder_inside_allowed_roots_is_r2(tmp_path):
    docs = tmp_path / "Documents"
    target = docs / "Restored"
    assert level("restore", [target], roots=[docs], target=str(target)) == Level.R2
    # Elsewhere it is raised to R3, which Herkos-lite does not carry out.
    assert level("restore", [tmp_path / "x"], roots=[docs], target=str(tmp_path / "x")) == Level.R3


def test_after_untrusted_content_a_backup_needs_more():
    s = SessionState()
    s.taint("org.vilna.mcp.files.read")
    assert level("run_now", session=s) == Level.R3


@pytest.fixture
def gate(tmp_path, monkeypatch):
    pytest.importorskip("vilna_mcp_backup")
    pytest.importorskip("vilna_backup")
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    for var, rel in (
        ("HOME", ""),
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_DATA_HOME", ".local/share"),
    ):
        monkeypatch.setenv(var, str(home / rel) if rel else str(home))
    monkeypatch.setenv("VILNA_BACKUP_KEYSTORE", f"file:{tmp_path / 'keys'}")
    config = Config(allowed_roots=(home / "Documents",), state_dir=tmp_path / "herkos")
    command = f"{sys.executable} -m vilna_backup"
    backup = McpStdioExecutor([sys.executable, "-m", "vilna_mcp_backup", "--command", command])
    backup.start()
    tools = ToolRegistry()
    tools.register(TOOL, backup)
    confirmer = Scripted(Answer.ONCE)
    g = Gate(
        config,
        ManifestRegistry.builtin(),
        tools,
        GrantStore(config.grants_path),
        Journal(config.journal_path),
        confirmer,
    )
    yield g, home, confirmer
    backup.stop()


def test_end_to_end(gate):
    g, home, confirmer = gate
    out = g.handle(request("status"), SessionState())
    assert out.code == "allowed", out.message
    assert out.result["set_up"] is False
    assert confirmer.questions == []  # R0: nobody is asked

    out = g.handle(request("set_paths", preset="settings", limit_gb=50), SessionState())
    assert out.code == "allowed", out.message
    assert len(confirmer.questions) == 1  # R2: asked the first time
    settings = json.loads((home / ".config/vilna-backup/settings.json").read_text())
    assert settings["preset"] == "settings" and settings["limit_bytes"] == 50 * 1000**3

    # Not set up yet: the tool refuses, and Gloxi gets the reason.
    out = g.handle(request("run_now"), SessionState())
    assert out.code == "tool_error" and "no place" in out.message

    # The person's actions are not for Gloxi in Herkos-lite.
    out = g.handle(request("configure_destination", place="/media/usb"), SessionState())
    assert out.code == "denied_level"
