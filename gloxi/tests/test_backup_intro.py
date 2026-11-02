# SPDX-License-Identifier: GPL-3.0-or-later
"""The first-run talk about backups (backup/docs/design.md 2a)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from gloxi.backup_intro import REMIND_AFTER, BackupIntro
from gloxi.cli import run_intro

GROUPS = [
    {"group": "settings", "paths": ["/h/.config"], "bytes": 40_000_000},
    {"group": "documents", "paths": ["/h/Documents"], "bytes": 3_200_000_000},
    {"group": "pictures", "paths": ["/h/Pictures"], "bytes": 18_000_000_000},
    {"group": "videos", "paths": ["/h/Videos"], "bytes": 120_000_000_000},
    {"group": "home", "paths": ["/h"], "bytes": 150_000_000_000},
]


class FakeHerkos:
    """Answers like Herkos with the backup tool behind it."""

    def __init__(self, set_up=False, fits=True, deny=()):
        self.calls = []
        self.set_up = set_up
        self.fits = list(fits) if isinstance(fits, list) else [fits]
        self.deny = set(deny)

    def __call__(self, operation, arguments, reason, resources):
        self.calls.append((operation, arguments))
        assert reason  # always a reason for the Herkos window
        if operation in self.deny:
            return {"decision": "denied", "code": "denied_user", "message": "the user said no"}
        result = {"ok": True}
        if operation == "status":
            result = {"set_up": self.set_up}
        elif operation == "overview":
            fits = self.fits.pop(0) if len(self.fits) > 1 else self.fits[0]
            current = {"bytes": 21_240_000_000, "limit": 50 * 10**9, "fits": fits}
            if not fits:
                current |= {"suggest_exclude": ["/h/Videos"], "bytes_after": 21 * 10**9}
            result = {"groups": GROUPS, "current": current}
        return {"decision": "allowed", "code": "allowed", "result": result}

    def ops(self):
        return [c for c in self.calls if c[0] not in ("status", "overview")]


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def make(tmp_path):
    def make(**kw):
        herkos = FakeHerkos(**kw)
        clock = Clock()
        return BackupIntro(herkos, path=tmp_path / "intro.json", clock=clock), herkos, clock

    return make


def talk(intro, *answers):
    turns = [intro.start()]
    for a in answers:
        turns.append(intro.answer(a))
    return turns


def test_the_recommended_path(make):
    intro, herkos, _ = make()
    turns = talk(intro, "1", "2", "ні", "no", "1", "3", "1")
    assert "3.2 GB" in turns[1].text and "120.0 GB" in turns[1].text
    assert "recommended" in turns[1].choices[1]
    assert herkos.ops() == [
        ("set_paths", {"preset": "documents"}),
        ("set_paths", {"gloxi_history": False}),
        ("set_paths", {"limit_gb": 50}),
        ("set_schedule", {"schedule": "daily"}),
    ]
    last = turns[-1]
    assert last.done and "vilna-backup setup /run/media" in last.text
    assert "every day" in last.text and "secrets" not in last.text
    assert not intro.due() and not intro.active


def test_words_work_as_well_as_numbers(make):
    intro, herkos, _ = make()
    talk(intro, "так", "Settings and", "yes")
    assert ("set_paths", {"gloxi_history": True}) in herkos.ops()


def test_choose_myself(make):
    intro, herkos, _ = make()
    turns = talk(intro, "1", "4", "1 3", "1")
    assert [c.split(" (")[0] for c in turns[2].choices] == ["Documents", "Pictures", "Videos"]
    assert ("set_paths", {"preset": "custom", "add": ["/h/Documents", "/h/Videos"]}) in herkos.ops()
    assert turns[-1].choices == ("No", "Yes")  # on to Gloxi history


def test_too_large_offers_to_leave_out_folders(make):
    intro, herkos, _ = make(fits=[True, False, True])  # sizes, limit check, recheck
    turns = talk(intro, "1", "3", "1", "1", "1")
    assert "/h/Videos" in turns[-1].text and turns[-1].choices[0].startswith("Yes")
    turns.append(intro.answer("1"))
    assert ("set_paths", {"remove": ["/h/Videos"]}) in herkos.ops()
    assert "Where" in turns[-1].text


def test_own_amount_of_space(make):
    intro, herkos, _ = make()
    talk(intro, "1", "2", "1", "1", "120 GB")
    assert ("set_paths", {"limit_gb": 120.0}) in herkos.ops()


def test_secrets_are_left_to_the_person(make):
    intro, herkos, _ = make()
    turns = talk(intro, "1", "2", "1", "2", "4", "2", "3")
    assert all("system_secrets" not in json.dumps(c) for c in herkos.calls)
    assert "vilna-backup secrets on" in turns[-1].text
    assert "vilna-backup setup s3:" in turns[-1].text
    assert ("set_schedule", {"schedule": "weekly"}) in herkos.ops()


def test_cloud_places_not_in_the_preview(make):
    intro, _, _ = make()
    turns = talk(intro, "1", "2", "1", "1", "1", "1")
    assert "not open yet" in turns[-1].text and len(turns[-1].choices) == 4
    assert intro.answer("4").text.startswith("These are US companies")


def test_later_asks_once_more_after_a_week(make):
    intro, herkos, clock = make()
    assert intro.due()
    turns = talk(intro, "2")
    assert turns[-1].done and herkos.ops() == []
    assert not intro.due()
    clock.now += REMIND_AFTER
    assert intro.due()
    talk(intro, "Later")
    clock.now += 10 * REMIND_AFTER
    assert not intro.due()  # declined twice: never again


def test_already_set_up(make):
    intro, _, _ = make(set_up=True)
    assert intro.start() is None
    assert not intro.due()


def test_restore_points_to_recover(make):
    intro, _, _ = make()
    turns = talk(intro, "3")
    assert turns[-1].done and "vilna-backup recover" in turns[-1].text


def test_a_denial_ends_the_talk_politely(make):
    intro, _, _ = make(deny={"set_paths"})
    turns = talk(intro, "1", "2")
    assert turns[-1].done and "the user said no" in turns[-1].text
    assert not intro.active


def test_herkos_not_running_does_not_count_as_asked(make, tmp_path):
    def broken(*a):
        raise OSError("no socket")

    intro = BackupIntro(broken, path=tmp_path / "intro.json")
    turn = intro.start()
    assert turn.done and "Herkos" in turn.text
    assert intro.due()


def test_bad_answers_ask_again(make):
    intro, _, _ = make()
    intro.start()
    assert "1 to 3" in intro.answer("maybe").text
    assert intro.active


def test_terminal(make):
    intro, _, _ = make()
    answers = iter(["2"])
    out = io.StringIO()
    run_intro(intro, lambda prompt: next(answers), out)
    text = out.getvalue()
    assert "Gloxi: Shall I keep backups" in text and "  1. Yes" in text
    assert "ask once more in a week" in text


# -- end to end: the talk through the real Herkos gate and backup tool ----------


def test_through_herkos(tmp_path, monkeypatch):
    pytest.importorskip("vilna_mcp_backup")
    pytest.importorskip("vilna_backup")
    from herkos.config import Config
    from herkos.confirm import Answer, Scripted
    from herkos.gate import Gate
    from herkos.grants import GrantStore
    from herkos.manifest import ManifestRegistry
    from herkos.mcp_client import McpStdioExecutor
    from herkos.mitos import Journal
    from herkos.policy import SessionState
    from herkos.request import parse_request
    from herkos.tools import ToolRegistry

    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "a.txt").write_text("a")
    for var, rel in (
        ("HOME", ""),
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_DATA_HOME", ".local/share"),
    ):
        monkeypatch.setenv(var, str(home / rel) if rel else str(home))
    monkeypatch.setenv("VILNA_BACKUP_KEYSTORE", f"file:{tmp_path / 'keys'}")
    tool = McpStdioExecutor(
        [sys.executable, "-m", "vilna_mcp_backup", "--command", f"{sys.executable} -m vilna_backup"]
    )
    tool.start()
    try:
        tools = ToolRegistry()
        tools.register("org.vilna.mcp.backup", tool)
        config = Config(allowed_roots=(home / "Documents",), state_dir=tmp_path / "herkos")
        confirmer = Scripted(Answer.SESSION)
        gate = Gate(
            config,
            ManifestRegistry.builtin(),
            tools,
            GrantStore(config.grants_path),
            Journal(config.journal_path),
            confirmer,
        )
        session = SessionState()

        def request(operation, arguments, reason, resources):
            req = parse_request(
                {
                    "tool": "org.vilna.mcp.backup",
                    "operation": operation,
                    "reason": reason,
                    "resources": resources,
                    "arguments": arguments,
                }
            )
            return gate.handle(req, session).to_json()

        intro = BackupIntro(request, path=tmp_path / "intro.json")
        turns = talk(intro, "1", "1", "2", "1", "2", "3", "2")
        assert turns[-1].done, turns[-1].text
        assert "vilna-backup setup /run/media" in turns[-1].text
        settings = json.loads(Path(home / ".config/vilna-backup/settings.json").read_text())
        assert settings["preset"] == "settings"
        assert settings["gloxi_history"] is True
        assert settings["limit_bytes"] == 200 * 1000**3
        assert settings["schedule"] == "hourly"
        # Asked once per kind with "this session": R1 sizes, R2 changes; R0 status never.
        asked = [q.operation for q in confirmer.questions]
        assert asked == ["overview", "set_paths", "set_schedule"]
    finally:
        tool.stop()
