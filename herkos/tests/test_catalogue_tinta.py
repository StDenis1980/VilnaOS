# SPDX-License-Identifier: GPL-3.0-or-later
"""The catalogue Vilna ships (herkos/catalogue, package herkos-manifests):
the Tinta Office programs (ADR 0017, stage 2, step 7)."""

from pathlib import Path

import pytest

from herkos.manifest import EXT_PREFIX, MAX_MANIFEST_BYTES, ManifestRegistry, load_manifest
from herkos.policy import SessionState, assess
from herkos.request import parse_request
from herkos.risk import Level

CATALOGUE = Path(__file__).resolve().parents[1] / "catalogue"
TINTA = {
    "io.github.stdenis1980.tintatexto": "wordcraft-cli",
    "io.github.stdenis1980.tintacalculo": "gridcraft-cli",
    "io.github.stdenis1980.tintapdf": "pdfcraft-cli",
    "io.github.stdenis1980.tintapresenta": "deckcraft-cli",
    "io.github.stdenis1980.ibercorreo": "ibercorreo-cli",
}


def manifests():
    return {m.tool: m for m in map(load_manifest, sorted(CATALOGUE.glob("*.toml")))}


def test_the_catalogue_holds_the_five_tinta_programs():
    found = manifests()
    assert set(found) == set(TINTA)
    for path in CATALOGUE.glob("*.toml"):
        assert path.stat().st_size <= MAX_MANIFEST_BYTES
        assert load_manifest(path).tool == path.stem
    for tool, m in found.items():
        launch = m.launch
        assert launch is not None and launch.flatpak is not None
        assert launch.flatpak.app == tool
        assert launch.flatpak.command == TINTA[tool]
        assert launch.flatpak.fixed == ("mcp",)
        assert launch.program == EXT_PREFIX + tool.rsplit(".", 1)[-1]
        # No Tinta program sends anything off the device through Gloxi yet.
        assert not any(op.egress for op in m.operations.values())


def test_the_catalogue_loads_beside_the_built_in_manifests():
    registry = ManifestRegistry.builtin()
    for m in manifests().values():
        registry.add(m)
    assert all(not registry.is_builtin(tool) for tool in TINTA)


def test_reading_taints_and_files_are_declared():
    for m in manifests().values():
        for op in m.operations.values():
            assert op.level is not None
            if op.level == Level.R2 and op.name not in {"update_draft", "update_contact"}:
                # A file written, or a change to the mailbox that loses nothing.
                assert op.resource_args or op.reversible, (m.tool, op.name)
            if op.name.startswith(("get_", "read_", "inspect_", "list_messages", "search_")):
                assert op.returns_external, (m.tool, op.name)


def run(tool, operation, arguments):
    request = parse_request(
        {"tool": tool, "operation": operation, "reason": "test", "arguments": arguments}
    )
    return assess(request, manifests()[tool], SessionState(), allowed_roots=(), bulk_threshold=20)


@pytest.mark.parametrize(
    ("tool", "operation", "command", "level"),
    [
        ("tintatexto", "execute", "text.insert", Level.R1),
        ("tintatexto", "execute", "table.insert", Level.R1),
        ("tintatexto", "execute", "view.zoom", Level.R1),
        ("tintatexto", "execute", "file.save", Level.R3),
        ("tintatexto", "execute", "file.encrypt", Level.R3),
        ("tintatexto", "execute", "zotero.insertCitation", Level.R4),
        ("tintatexto", "execute", "file.print", None),
        ("tintatexto", "execute", "review.suggestWords", None),
        ("tintatexto", "execute", "tools.macros", None),
        ("tintacalculo", "execute_command", "cell.set", Level.R1),
        ("tintacalculo", "execute_command", "file.saveAs", Level.R3),
        ("tintacalculo", "execute_command", "data.getData", Level.R4),
        ("tintacalculo", "execute_command", "macro.run", None),
        ("tintacalculo", "execute_command", "automate.runScript", None),
        ("tintacalculo", "execute_command", "link.open", None),
        ("tintapresenta", "run_command", "slide.new", Level.R1),
        ("tintapresenta", "run_command", "file.export", Level.R3),
        ("tintapresenta", "run_command", "show.fromStart", Level.R4),
        ("tintapresenta", "run_command", "media.openExternal", None),
    ],
)
def test_tinta_commands_take_their_levels(tool, operation, command, level):
    a = run(f"io.github.stdenis1980.{tool}", operation, {"command": command})
    assert a.level == level


def test_a_batch_in_texto_is_as_risky_as_its_riskiest_step():
    steps = [{"command": "text.insert"}, {"command": "file.print"}]
    assert run("io.github.stdenis1980.tintatexto", "batch", {"commands": steps}).forbidden
    steps = [{"command": "text.insert"}, {"command": "file.save"}]
    a = run("io.github.stdenis1980.tintatexto", "batch", {"commands": steps})
    assert a.level == Level.R3


def test_sending_mail_is_not_in_the_catalogue_yet():
    correo = manifests()["io.github.stdenis1980.ibercorreo"]
    assert correo.operation("send_draft") is None
    assert correo.launch is not None and correo.launch.args == ()
    assert correo.operation("delete_message").level == Level.R3
