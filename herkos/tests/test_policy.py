# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

from herkos.manifest import ManifestRegistry, parse_manifest
from herkos.policy import SessionState, assess
from herkos.request import parse_request
from herkos.risk import Level

FILES = ManifestRegistry.builtin().get("org.vilna.mcp.files")


def req(op, *paths):
    return parse_request(
        {
            "tool": "org.vilna.mcp.files",
            "operation": op,
            "reason": "test",
            "resources": [Path(p).as_uri() for p in paths],
        }
    )


def run(request, *, session=None, roots=(), manifest=FILES, annotations=None, bulk=20):
    return assess(
        request,
        manifest,
        session or SessionState(),
        allowed_roots=roots,
        bulk_threshold=bulk,
        annotations=annotations,
    )


def test_base_levels(tmp_path):
    assert run(req("read", tmp_path / "a"), roots=[tmp_path]).level == Level.R1
    assert run(req("write", tmp_path / "a"), roots=[tmp_path]).level == Level.R2


def test_taint_raises_changes_but_not_reads(tmp_path):
    s = SessionState()
    s.taint("org.vilna.mcp.files.read")
    assert run(req("read", tmp_path / "a"), session=s, roots=[tmp_path]).level == Level.R1
    a = run(req("write", tmp_path / "a"), session=s, roots=[tmp_path])
    assert a.level == Level.R3
    assert "untrusted" in a.raises[0]


def test_out_of_bounds_raises(tmp_path):
    a = run(req("read", "/etc/passwd"), roots=[tmp_path])
    assert a.level == Level.R2
    assert not a.in_bounds


def test_dotdot_cannot_escape(tmp_path):
    docs = tmp_path / "Documents"
    docs.mkdir()
    a = run(req("read", f"{docs}/../secret"), roots=[docs])
    assert not a.in_bounds


def test_symlink_cannot_escape(tmp_path):
    docs = tmp_path / "Documents"
    docs.mkdir()
    (tmp_path / "secret").write_text("x")
    (docs / "link").symlink_to(tmp_path / "secret")
    a = run(req("read", docs / "link"), roots=[docs])
    assert not a.in_bounds


def test_prefix_is_not_containment(tmp_path):
    docs = tmp_path / "Documents"
    docs.mkdir()
    a = run(req("read", f"{docs}-evil/x"), roots=[docs])
    assert not a.in_bounds


def test_bulk_raises(tmp_path):
    paths = [tmp_path / str(i) for i in range(4)]
    assert run(req("read", *paths), roots=[tmp_path], bulk=3).level == Level.R2


def test_modifiers_cap_at_r4(tmp_path):
    s = SessionState()
    s.taint("x")
    paths = [f"/elsewhere/{i}" for i in range(5)]
    a = run(req("delete", *paths), session=s, roots=[tmp_path], bulk=3)
    assert a.level == Level.R4


def test_not_undoable_change_is_at_least_r3():
    m = parse_manifest({"tool": "org.example.t", "operations": {"set": {"level": "R2"}}})
    r = parse_request({"tool": "org.example.t", "operation": "set", "reason": "t"})
    assert run(r, manifest=m).level == Level.R3


def test_egress_is_at_least_r3():
    m = parse_manifest(
        {"tool": "org.example.t", "operations": {"up": {"level": "R1", "egress": True}}}
    )
    r = parse_request({"tool": "org.example.t", "operation": "up", "reason": "t"})
    assert run(r, manifest=m).level == Level.R3


def test_annotations_only_raise(tmp_path):
    a = run(req("write", tmp_path / "a"), roots=[tmp_path], annotations={"readOnlyHint": True})
    assert a.level == Level.R2
    a = run(req("read", tmp_path / "a"), roots=[tmp_path], annotations={"destructiveHint": True})
    assert a.level == Level.R3


def test_unknown_tool_is_r3_or_r4():
    r = parse_request({"tool": "org.example.none", "operation": "x", "reason": "t"})
    assert run(r, manifest=None).level == Level.R4
    assert run(r, manifest=None, annotations={"readOnlyHint": True}).level == Level.R3
    assert not run(r, manifest=None).known_tool


def test_unknown_operation_is_forbidden():
    r = parse_request({"tool": "org.vilna.mcp.files", "operation": "chmod", "reason": "t"})
    a = run(r)
    assert a.forbidden and a.operation is None
