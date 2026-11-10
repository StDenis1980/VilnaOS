# SPDX-License-Identifier: GPL-3.0-or-later
"""Where Gloxi finds Herkos: the session socket, or HERKOS_SOCKET (ADR 0025)."""

from pathlib import Path

from herkos.config import socket_path


def test_session_socket_by_default(monkeypatch):
    monkeypatch.delenv("HERKOS_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert socket_path() == Path("/run/user/1000/vilna/herkos.sock")


def test_gloxid_points_at_the_system_herkos(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("HERKOS_SOCKET", "/run/herkos/herkos.sock")
    assert socket_path() == Path("/run/herkos/herkos.sock")


def test_a_relative_herkos_socket_is_ignored(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("HERKOS_SOCKET", "herkos.sock")
    assert socket_path() == Path("/run/user/1000/vilna/herkos.sock")


def test_tilde_can_be_another_persons_home(tmp_path):
    # gloxid reads the system Herkos's folders for the person it serves (ADR 0025).
    from herkos.config import load_config

    home = tmp_path / "person"
    missing = tmp_path / "none.toml"
    assert load_config(missing, home=home).allowed_roots == (home / "Documents",)
    cfg = tmp_path / "config.toml"
    cfg.write_text('allowed_roots = ["~", "~/Desktop", "/srv/x"]\n')
    assert load_config(cfg, home=home).allowed_roots == (home, home / "Desktop", Path("/srv/x"))
