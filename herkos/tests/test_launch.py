# SPDX-License-Identifier: GPL-3.0-or-later
"""[launch] in manifests: how Herkos starts a tool (ADR 0017)."""

from __future__ import annotations

import sys

import pytest
from conftest import Env

from herkos.cli import launch_command
from herkos.manifest import Launch, ManifestError, ManifestRegistry, parse_manifest

OPS = {"operations": {"read": {"level": "R1"}}}


def manifest(launch):
    return parse_manifest({"tool": "org.example.t", **OPS, "launch": launch})


def test_builtin_manifests_say_how_to_start_their_tools():
    reg = ManifestRegistry.builtin()
    assert reg.get("org.vilna.mcp.files").launch == Launch(
        "vilna-mcp-files", "vilna_mcp_files", ("backup_dir", "roots")
    )
    assert reg.get("org.vilna.mcp.backup").launch == Launch(
        "vilna-mcp-backup", "vilna_mcp_backup", ()
    )


def test_launch_is_optional():
    assert parse_manifest({"tool": "org.example.t", **OPS}).launch is None


def test_args_come_out_in_one_order():
    assert manifest({"program": "x", "args": ["roots", "backup_dir"]}).launch.args == (
        "backup_dir",
        "roots",
    )


@pytest.mark.parametrize(
    "launch",
    [
        "vilna-mcp-files",
        {},
        {"program": "/usr/bin/sh"},
        {"program": "../sh"},
        {"program": "sh -c"},
        {"program": "X"},
        {"program": "x", "module": "os; import evil"},
        {"program": "x", "module": 3},
        {"program": "x", "args": ["--root", "/"]},
        {"program": "x", "args": ["roots", "roots"]},
        {"program": "x", "args": "roots"},
        {"program": "x", "command": ["sh"]},
    ],
)
def test_bad_launch_is_rejected(launch):
    with pytest.raises(ManifestError):
        manifest(launch)


def test_command_line_from_the_manifest(env: Env):
    files = ManifestRegistry.builtin().get("org.vilna.mcp.files")
    assert launch_command(files, env.config) == [
        sys.executable,
        "-m",
        "vilna_mcp_files",
        "--backup-dir",
        str(env.config.backup_dir),
        "--root",
        str(env.docs),
    ]
    backup = ManifestRegistry.builtin().get("org.vilna.mcp.backup")
    assert launch_command(backup, env.config) == [sys.executable, "-m", "vilna_mcp_backup"]


def test_not_started_without_module_or_folders(env: Env, tmp_path):
    assert launch_command(manifest({"program": "x"}), env.config) is None
    assert launch_command(manifest({"program": "x", "module": "no_such_mod"}), env.config) is None
    assert launch_command(manifest({"program": "x", "module": "no.such.mod"}), env.config) is None
    no_parse = parse_manifest({"tool": "org.example.t", **OPS})
    assert launch_command(no_parse, env.config) is None
    env.docs.rmdir()
    files = ManifestRegistry.builtin().get("org.vilna.mcp.files")
    assert launch_command(files, env.config) is None


PDF = {
    "program": "vilna-mcp-ext-pdf",
    "flatpak": "io.github.stdenis1980.tintapdf",
    "command": "pdfcraft-cli",
    "fixed": ["mcp"],
    "args": ["roots"],
}


def test_a_flatpak_program_is_started_through_its_own_unit():
    from herkos.manifest import Flatpak

    assert manifest(PDF).launch.flatpak == Flatpak(
        "io.github.stdenis1980.tintapdf", "pdfcraft-cli", ("mcp",), ("--root",)
    )
    two = {**PDF, "root_flags": ["--automation-read-root", "--automation-write-root"]}
    assert len(manifest(two).launch.flatpak.root_flags) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"program": "vilna-mcp-files"},
        {"program": "vilna-mcp-ext-"},
        {"flatpak": "tintapdf"},
        {"flatpak": "io.github.x/../y"},
        {"flatpak": "io.github.x\n"},
        {"command": "/usr/bin/pdfcraft-cli"},
        {"command": "pdfcraft-cli\n"},
        {"fixed": ["mcp --root /"]},
        {"fixed": ["--filesystem=host"]},
        {"args": ["backup_dir", "roots"]},
        {"module": "x"},
        {"root_flags": []},
        {"root_flags": ["-r"]},
        {"root_flags": ["--root=x"]},
        {"flatpak": None},
    ],
)
def test_flatpak_launches_take_only_safe_words(change):
    launch = {k: v for k, v in {**PDF, **change}.items() if v is not None}
    with pytest.raises(ManifestError):
        manifest(launch)


def test_a_program_name_with_a_line_break_is_refused():
    with pytest.raises(ManifestError):
        manifest({"program": "x\n"})
