# SPDX-License-Identifier: GPL-3.0-or-later
"""herkos-agent exits so that systemd knows whether to start it again."""

from __future__ import annotations

import json
import socket
import threading
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
