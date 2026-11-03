# SPDX-License-Identifier: GPL-3.0-or-later
"""Herkos-lite bundled in the Flatpak: folders the person chose, Herkos as a child."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from gloxi import herkos_local
from gloxi.herkos_local import (
    HerkosStartError,
    LocalHerkos,
    add_folder,
    allowed_folders,
    ensure_config,
    remove_folder,
)
from gloxi.tools import FileTools


def load_config(path):
    # Herkos is not installed in the RPM build of Gloxi alone.
    return pytest.importorskip("herkos.config").load_config(path)


def test_folders_round_trip_and_herkos_reads_them(tmp_path):
    cfg = tmp_path / "herkos" / "config.toml"
    ensure_config(cfg)
    assert allowed_folders(cfg) == []
    assert load_config(cfg).allowed_roots == ()  # not Herkos's ~/Documents

    doc = Path('/run/user/1000/doc/ab12cd/Документи "quoted"')
    add_folder(doc, cfg)
    add_folder(doc, cfg)  # twice is once
    add_folder(Path("/run/user/1000/doc/ef34/Music"), cfg)
    assert allowed_folders(cfg) == [doc, Path("/run/user/1000/doc/ef34/Music")]
    assert load_config(cfg).allowed_roots == (doc, Path("/run/user/1000/doc/ef34/Music"))

    remove_folder(doc, cfg)
    assert allowed_folders(cfg) == [Path("/run/user/1000/doc/ef34/Music")]


def test_other_herkos_settings_are_kept(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('allowed_roots = []\nkeros_model = "off"\nconfirm_timeout = 60\n')
    add_folder(tmp_path, cfg)
    config = load_config(cfg)
    assert config.keros_model is None and config.confirm_timeout == 60.0
    assert config.allowed_roots == (tmp_path,)


def test_not_in_flatpak_nothing_starts(monkeypatch):
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setattr(herkos_local.Path, "exists", lambda self: False)
    assert herkos_local.start_if_needed(True) is None


def test_no_folders_no_tools():
    tools = FileTools(lambda: None, (), available=lambda: True)
    assert not tools.available()
    tools.set_roots([Path("/tmp")])
    assert tools.available()


@pytest.fixture
def sandbox(tmp_path):
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    env = dict(os.environ)
    env.update(
        XDG_RUNTIME_DIR=str(runtime),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_STATE_HOME=str(tmp_path / "state"),
    )
    env.pop("WAYLAND_DISPLAY", None)
    env.pop("DISPLAY", None)
    return env


def test_real_herkos_starts_restarts_and_stops(sandbox, tmp_path):
    pytest.importorskip("herkos")
    cfg = Path(sandbox["XDG_CONFIG_HOME"]) / "herkos" / "config.toml"
    ensure_config(cfg)
    (tmp_path / "docs").mkdir()
    add_folder(tmp_path / "docs", cfg)
    command = [sys.executable, "-m", "herkos", "serve", "--confirm", "deny"]
    herkos = LocalHerkos(command, env=sandbox)
    herkos.start()
    try:
        assert herkos_local.alive(herkos.socket)
        # A second Gloxi in the same sandbox uses the running one.
        other = LocalHerkos(command, env=sandbox)
        other.start()
        assert not other.running()
        herkos.restart()
        assert herkos.running() and herkos_local.alive(herkos.socket)
    finally:
        herkos.stop()
    assert not herkos_local.alive(herkos.socket)


def test_a_crash_is_reported(sandbox):
    herkos = LocalHerkos([sys.executable, "-c", "raise SystemExit(4)"], env=sandbox)
    with pytest.raises(HerkosStartError, match="exit 4"):
        herkos.start()


def test_without_a_desktop_herkos_asks_on_gloxis_terminal(sandbox, tmp_path):
    """The chat in a terminal: Herkos runs in its own session and asks on Gloxi's tty."""
    pytest.importorskip("herkos")
    pytest.importorskip("vilna_mcp_files")
    import pty
    import select

    from herkos.client import HerkosClient

    cfg = Path(sandbox["XDG_CONFIG_HOME"]) / "herkos" / "config.toml"
    ensure_config(cfg)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.txt").write_text("synthetic note\n")
    add_folder(docs, cfg)
    parent, child = pty.openpty()
    herkos = LocalHerkos(env=sandbox, confirm_fd=child)
    herkos.start()
    try:
        os.write(parent, b"o\n")  # typed ahead: "allow once"
        with HerkosClient.connect(herkos.socket) as client:
            result = client.request_action(
                tool="org.vilna.mcp.files",
                operation="read",
                reason="the person asked",
                resources=[(docs / "note.txt").as_uri()],
                arguments={"path": str(docs / "note.txt")},
            )
        shown = b""
        while select.select([parent], [], [], 0.2)[0]:
            shown += os.read(parent, 65536)
        assert result["code"] == "allowed", result
        assert result["result"]["text"] == "synthetic note\n"
        assert "Herkos" in shown.decode(errors="replace")
    finally:
        herkos.stop()
        os.close(parent)
        os.close(child)


def test_the_flatpak_offers_no_backup_talk(monkeypatch):
    """Backups are part of Vilna: the Flatpak has no backup tool to talk about."""
    from gloxi.backup_intro import backup_intro

    monkeypatch.setenv("FLATPAK_ID", "io.github.stdenis1980.Gloxi")
    assert backup_intro(True) is None
