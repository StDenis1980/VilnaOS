# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from herkos.manifest import ManifestError, ManifestRegistry, parse_manifest
from herkos.risk import Level


def test_builtin_files_manifest():
    files = ManifestRegistry.builtin().get("org.vilna.mcp.files")
    assert files is not None
    assert files.operation("read").level == Level.R1
    assert files.operation("read").returns_external
    assert files.operation("write").level == Level.R2
    assert files.operation("write").undoable
    assert files.operation("delete").level == Level.R3
    assert files.operation("format_disk") is None


def test_forbidden_operation():
    m = parse_manifest({"tool": "org.example.t", "operations": {"wipe": {"level": "forbidden"}}})
    assert m.operation("wipe").forbidden


@pytest.mark.parametrize(
    "data",
    [
        {"operations": {"a": {"level": "R0"}}},
        {"tool": "Bad Name", "operations": {"a": {"level": "R0"}}},
        {"tool": "org.example.t"},
        {"tool": "org.example.t", "operations": {"a": {}}},
        {"tool": "org.example.t", "operations": {"a": {"level": "R9"}}},
        {"tool": "org.example.t", "operations": {"a": {"level": "R1", "undoable": "yes"}}},
        {"tool": "org.example.t", "operations": {"a": {"level": "R1", "trusted": True}}},
        {"tool": "org.example.t", "extra": 1, "operations": {"a": {"level": "R1"}}},
    ],
)
def test_malformed_manifests_are_rejected(data):
    with pytest.raises(ManifestError):
        parse_manifest(data)


def test_duplicate_manifest():
    m = parse_manifest({"tool": "org.example.t", "operations": {"a": {"level": "R0"}}})
    with pytest.raises(ManifestError):
        ManifestRegistry([m, m])


def test_a_polkit_action_is_herkos_s_own_and_only_for_r3_and_r4():
    from herkos.manifest import ManifestError, ManifestRegistry, parse_manifest

    def op(**kw):
        return {"tool": "t.x", "operations": {"put": kw}}

    ok = parse_manifest(op(level="R3", polkit="org.vilna.herkos.apps.change"), "t")
    assert ok.operations["put"].polkit == "org.vilna.herkos.apps.change"
    for wrong in (
        op(level="R2", polkit="org.vilna.herkos.apps.change"),
        op(level="R3", polkit="org.freedesktop.systemd1.manage-units"),
        op(level="R3", polkit="org.vilna.herkos."),
        op(level="R3", polkit="org.vilna.herkos.Apps"),
        op(level="R3", polkit="org.vilna.herkos..x"),
        op(level="R4", polkit=3),
        op(level="forbidden", polkit="org.vilna.herkos.x"),
    ):
        with pytest.raises(ManifestError):
            parse_manifest(wrong, "t")
    r = ManifestRegistry.builtin()
    assert r.get("org.vilna.mcp.apps").operations["install"].polkit == (
        "org.vilna.herkos.apps.change"
    )
    assert r.get("org.vilna.mcp.system").operations["rollback"].polkit == (
        "org.vilna.herkos.system.rollback"
    )
    assert r.get("org.vilna.mcp.vpn").operations["killswitch_on"].polkit is None
    assert r.get("org.vilna.mcp.files").operations["delete"].polkit is None


def test_a_preview_names_an_r0_operation_of_the_same_tool():
    from herkos.manifest import ManifestError, ManifestRegistry, parse_manifest

    base = {"tool": "t.x", "operations": {"look": {"level": "R0"}, "put": {"level": "R3"}}}
    ok = parse_manifest(
        {**base, "operations": {**base["operations"], "put": {"level": "R3", "preview": "look"}}},
        "t",
    )
    assert ok.operations["put"].preview == "look"
    for wrong in ("put", "gone", 3):
        data = {
            **base,
            "operations": {**base["operations"], "put": {"level": "R3", "preview": wrong}},
        }
        with pytest.raises(ManifestError):
            parse_manifest(data, "t")
    apps = ManifestRegistry.builtin().get("org.vilna.mcp.apps")
    assert apps.operations["install"].preview == "describe"
    assert apps.operations["add_remote"].forbidden
    assert apps.launch.module is None  # not in Herkos-lite: installing is R3
