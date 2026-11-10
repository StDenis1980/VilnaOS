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
