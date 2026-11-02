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
