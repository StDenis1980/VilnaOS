# SPDX-License-Identifier: GPL-3.0-or-later
"""The «Дозволи» window without GTK: journal records → rows, and the two backends."""

from __future__ import annotations

import json
import socket
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from herkos import i18n
from herkos import permissions_view as view
from herkos.grants import GrantStore
from herkos.manifest import ManifestRegistry
from herkos.mitos import Journal
from herkos.risk import Level

FILES = "org.vilna.mcp.files"
NOW = datetime(2026, 11, 9, 14, 0, tzinfo=UTC)


def rec(kind, aid, data, when=NOW):
    return {"kind": kind, "action_id": aid, "time": when.isoformat(), "data": data}


def change(aid, name, when=NOW, *, undo=True, reason="you asked"):
    uri = f"file:///home/ola/Documents/{name}"
    return [
        rec(
            "request",
            aid,
            {
                "tool": FILES,
                "operation": "write",
                "resources": [uri],
                "resource_count": 1,
                "reason": reason,
            },
            when,
        ),
        rec("decision", aid, {"decision": "allowed", "code": "allowed", "level": "R2"}, when),
        rec(
            "result",
            aid,
            {
                "ok": True,
                "code": "allowed",
                **({"undo": {"backup_id": "b1", "path": uri[7:]}} if undo else {}),
            },
            when,
        ),
    ]


@pytest.fixture
def ukrainian():
    i18n.use(["uk"])
    yield
    i18n.use([])


def test_pairs_records_newest_first_and_skips_junk():
    records = [
        None,
        "x",
        {"kind": "request"},
        *change("a1", "zvit.odt", NOW - timedelta(hours=2)),
        *change("a2", "plan.md"),
        rec("result", "nobody", {"ok": True}),
    ]
    acts = view.actions(records)
    assert [a.action_id for a in acts] == ["a2", "a1"]
    a = acts[1]
    assert (a.level, a.decision, a.ok, a.has_undo) == ("R2", "allowed", True, True)
    assert a.resources == ["/home/ola/Documents/zvit.odt"]
    assert view.short_path("/home/ola/Documents/a", Path("/home/ola")) == "~/Documents/a"
    assert view.short_path("/home/olah/a", Path("/home/ola")) == "/home/olah/a"
    assert a.can_undo(NOW) and not a.can_undo(NOW + timedelta(days=31))


def test_an_undo_marks_the_action_undone():
    undo = [
        rec(
            "request",
            "u1",
            {
                "tool": FILES,
                "operation": "restore",
                "resources": ["file:///home/ola/Documents/zvit.odt"],
                "reason": "Undo action a1",
            },
        ),
        rec("decision", "u1", {"decision": "allowed", "level": "R2"}),
        rec("result", "u1", {"ok": True, "undo": {"backup_id": "b2", "path": "/x"}}),
    ]
    acts = {a.action_id: a for a in view.actions([*change("a1", "zvit.odt"), *undo])}
    assert acts["a1"].undone_by == "u1" and not acts["a1"].can_undo(NOW)
    assert acts["u1"].undo_of == "a1"


def test_plain_language(ukrainian):
    reg = ManifestRegistry.builtin()
    a = view.actions(change("a1", "zvit.odt"))[0]
    assert view.title(a, reg) == "Gloxi змінив файл: zvit.odt"
    assert view.outcome_text(a) == "Виконано"
    a.decision, a.code = "denied", "denied_user"
    assert view.title(a, reg) == "Gloxi просив змінити файл: zvit.odt"
    assert view.outcome_text(a) == "Ви відмовили"
    a.resource_count = 3
    assert view.object_name(a) == "zvit.odt і ще 2"
    unknown = view.Action("x", NOW, "org.example.notes", "read")
    assert view.title(unknown, reg) == "org.example.notes.read"
    assert view.category_of("org.example.notes") == "programs"


def test_days(ukrainian):
    today = date(2026, 11, 9)
    assert view.day_title(today, today) == "Сьогодні"
    assert view.day_title(date(2026, 11, 8), today) == "Учора"
    assert view.day_title(date(2026, 3, 2), today) == "2 березня"
    assert view.day_title(date(2025, 3, 2), today) == "2 березня 2025"
    acts = view.actions([*change("a1", "a", NOW - timedelta(days=2)), *change("a2", "b")])
    assert [len(items) for _, items in view.by_day(acts)] == [1, 1]
    assert view.by_day(acts, "backup") == []


def test_permissions_rows(ukrainian):
    ps = view.permissions(
        [
            {
                "grant_id": "s1",
                "tool": FILES,
                "operation": "write",
                "roots": ["/home/ola/Documents"],
                "level": "R2",
                "duration": "session",
            },
            {
                "id": "p1",
                "tool": FILES,
                "operation": "create",
                "roots": [],
                "level": "R2",
                "duration": "permanent",
                "created": 1.0,
            },
            {"grant_id": 5},
            "junk",
        ]
    )
    assert [p.grant_id for p in ps] == ["p1", "s1"]
    assert view.permission_subtitle(ps[1]) == "До кінця сеансу · у Documents"
    assert view.permission_subtitle(ps[0]) == "Завжди"
    assert view.permission_title(ps[0], ManifestRegistry.builtin()) == "Створити файл"


def test_lite_reads_and_revokes_its_own_files(tmp_path: Path):
    docs = tmp_path / "Documents"
    store = GrantStore(tmp_path / "permissions.json")
    g = store.add(FILES, "write", [docs], Level.R2, "permanent")
    journal = Journal(tmp_path / "journal.jsonl")
    for r in change("a1", "zvit.odt"):
        journal.append(r["kind"], r["data"], r["action_id"])
    lite = view.LiteHerkos(state_dir=tmp_path, socket=tmp_path / "none.sock")
    assert [p.grant_id for p in lite.permissions()] == [g.id]
    assert [a.action_id for a in view.actions(lite.records())] == ["a1"]
    assert lite.revoke(g.id) and lite.permissions() == []
    with pytest.raises(view.HerkosUnreachable):
        lite.undo("a1")


class FakeHerkosd:
    """A socket that answers like herkosd does to the person's own connection."""

    def __init__(self, path: Path, role: str | None = "agent") -> None:
        self.path, self.role, self.calls = path, role, []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            conn, _ = self.server.accept()
            f = conn.makefile("rwb")
            for line in f:
                msg = json.loads(line)
                self.calls.append(msg["method"])
                result = {
                    "herkos.hello": {
                        "protocol": "0.1",
                        **({"role": self.role} if self.role else {}),
                    },
                    "herkos.permissions": {
                        "permissions": [
                            {
                                "grant_id": "g1",
                                "tool": FILES,
                                "operation": "write",
                                "roots": [],
                                "level": "R2",
                                "duration": "permanent",
                                "created": 1.0,
                            }
                        ]
                    },
                    "herkos.revoke": {"revoked": True},
                    "herkos.journal": {"records": change("a1", "zvit.odt")},
                    "herkos.undo": {"code": "allowed", "action_id": "u1"},
                }[msg["method"]]
                f.write(
                    json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}).encode()
                    + b"\n"
                )
                f.flush()
            conn.close()


def test_system_herkos_through_the_person_s_connection(tmp_path: Path):
    fake = FakeHerkosd(tmp_path / "s.sock")
    h = view.find_herkos(tmp_path / "s.sock")
    assert isinstance(h, view.SystemHerkos)
    assert [p.grant_id for p in h.permissions()] == ["g1"]
    assert h.revoke("g1") is True
    assert view.actions(h.records())[0].action_id == "a1"
    assert view.undo_message(h.undo("a1")) == view.undo_message({"code": "allowed"})
    assert "herkos.revoke" in fake.calls


def test_a_socket_that_is_not_ours_falls_back_to_lite(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    FakeHerkosd(tmp_path / "s.sock", role=None)
    assert isinstance(view.find_herkos(tmp_path / "s.sock"), view.LiteHerkos)
    assert isinstance(view.find_herkos(tmp_path / "missing.sock"), view.LiteHerkos)
