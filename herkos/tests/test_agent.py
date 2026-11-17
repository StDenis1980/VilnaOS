# SPDX-License-Identifier: GPL-3.0-or-later
"""herkos-agent exits so that systemd knows whether to start it again."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

from herkos import agent


def serve_once(path: Path, handle: Callable[[socket.socket], None]) -> threading.Thread:
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)

    def run() -> None:
        conn, _ = srv.accept()
        with conn:
            handle(conn)
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_account_herkos_does_not_serve(tmp_path: Path) -> None:
    # herkosd closes a connection from an unknown uid without a word.
    path = tmp_path / "herkos.sock"
    t = serve_once(path, lambda conn: None)
    assert agent.main(["--socket", str(path)]) == agent.NOT_SERVED
    t.join(5)


def test_closed_before_the_first_line_is_not_served(tmp_path: Path) -> None:
    # The same, when herkosd has closed before the agent writes (EPIPE).
    ours, theirs = socket.socketpair()
    theirs.close()
    a = agent.Agent(ours, ["true"])
    try:
        a.register()
    except agent.NotServed:
        pass
    else:
        raise AssertionError("register() should raise NotServed")
    finally:
        ours.close()


def test_herkos_goes_away_after_registering(tmp_path: Path) -> None:
    # herkosd restarted (an update): systemd should start the agent again.
    def handle(conn: socket.socket) -> None:
        f = conn.makefile("rb")
        for _ in range(2):
            msg = json.loads(f.readline())
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
            conn.sendall(json.dumps(reply).encode() + b"\n")

    path = tmp_path / "herkos.sock"
    t = serve_once(path, handle)
    assert agent.main(["--socket", str(path)]) == 1
    t.join(5)


def test_no_socket(tmp_path: Path) -> None:
    assert agent.main(["--socket", str(tmp_path / "missing.sock")]) == 1


def test_herkosd_hears_when_the_screen_locks_while_a_window_is_open() -> None:
    # Build15.6: the screen locked while Herkos asked, and the action was
    # refused after two minutes; herkosd now stops the clock while away.
    ours, theirs = socket.socketpair()
    screen = {"locked": True}
    checks = []

    def locked() -> bool:
        checks.append(1)
        return screen["locked"]

    a = agent.Agent(theirs, ["true"], locked=locked, poll=0.02)
    lines = ours.makefile("rb")
    watcher = threading.Thread(target=a.watch, daemon=True)
    watcher.start()
    try:
        # No window open: the screen is not even checked.
        time.sleep(0.2)
        assert checks == []
        with a.windows_lock:
            a.windows["ask-1"] = None  # type: ignore[assignment]
        assert json.loads(lines.readline()) == {
            "jsonrpc": "2.0",
            "method": "herkos.agent.away",
            "params": {"away": True},
        }
        screen["locked"] = False
        assert json.loads(lines.readline())["params"] == {"away": False}
        screen["locked"] = True
        assert json.loads(lines.readline())["params"] == {"away": True}
        # The window closes: back in use as far as herkosd is concerned.
        with a.windows_lock:
            a.windows.clear()
        assert json.loads(lines.readline())["params"] == {"away": False}
    finally:
        a.stop.set()
        watcher.join(2)
        ours.close()
        theirs.close()
