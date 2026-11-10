# SPDX-License-Identifier: GPL-3.0-or-later
"""Session agent: shows the questions of the system Herkos on the person's screen.

The system ``herkosd`` (ADR 0013, step 6) runs as its own user and cannot
reach anybody's screen, so each person's session runs ``herkos-agent``. It
connects to ``/run/herkos/herkos.sock`` as the person, registers, and for
each ``herkos.agent.ask`` request opens the same window as Herkos-lite
(``herkos.dialog``: the question as JSON on stdin, the answer on stdout).
``herkos.agent.cancel`` closes the window: the time to answer is over.

The agent decides nothing. ``herkosd`` checks the answer against the choices
it offered, and anything else, or no answer, is a "no".
"""

from __future__ import annotations

import argparse
import contextlib
import json
import socket
import subprocess
import sys
import threading
from typing import Any

from herkos import PROTOCOL_VERSION

SYSTEM_SOCKET = "/run/herkos/herkos.sock"
# herkosd gives up long before this (confirm_timeout is at most an hour).
MAX_WAIT = 3600.0
MAX_LINE = 1024 * 1024
# herkosd closed the connection without a word: this account is not a person
# in /etc/herkos/system.toml (the login screen, GNOME Initial Setup, an
# account made after installation). herkos-agent.service does not restart on
# this status, so the agent does not knock every few seconds all session.
NOT_SERVED = 77


class NotServed(ConnectionError):
    """herkosd answers nobody on this account."""


def default_window() -> list[str]:
    return [sys.executable, "-m", "herkos.dialog"]


class Agent:
    def __init__(self, sock: socket.socket, window: list[str]) -> None:
        self.sock = sock
        self.file = sock.makefile("rb")
        self.window = window
        self.write_lock = threading.Lock()
        self.windows: dict[str, subprocess.Popen[bytes]] = {}
        self.windows_lock = threading.Lock()
        self.next_id = 0

    def send(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, ensure_ascii=False).encode() + b"\n"
        with self.write_lock:
            self.sock.sendall(line)

    def read(self) -> dict[str, Any] | None:
        line = self.file.readline(MAX_LINE + 1)
        if not line:
            return None
        msg = json.loads(line)
        return msg if isinstance(msg, dict) else {}

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """A request of ours, before any question can arrive."""
        self.next_id += 1
        try:
            # herkosd may close the connection of an unknown uid before our
            # first line arrives: then writing fails, not reading.
            self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
            reply = self.read()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise NotServed("Herkos closed the connection without an answer") from e
        if reply is None:
            raise NotServed("Herkos closed the connection without an answer")
        if "error" in reply:
            raise ConnectionError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result") or {}

    def register(self) -> None:
        self.call("herkos.hello", {"protocol": PROTOCOL_VERSION, "client": "herkos-agent"})
        self.call("herkos.agent.register", {})

    def ask(self, ask_id: str, question: dict[str, Any]) -> None:
        answer = "deny"
        try:
            proc = subprocess.Popen(
                self.window,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            proc = None
        if proc is not None:
            with self.windows_lock:
                self.windows[ask_id] = proc
            try:
                out, _ = proc.communicate(json.dumps(question).encode(), timeout=MAX_WAIT)
                lines = out.decode(errors="replace").strip().splitlines()
                answer = lines[-1].strip() if lines else "deny"
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            finally:
                with self.windows_lock:
                    self.windows.pop(ask_id, None)
        with contextlib.suppress(OSError):
            self.send({"jsonrpc": "2.0", "id": ask_id, "result": {"answer": answer}})

    def cancel(self, ask_id: str) -> None:
        with self.windows_lock:
            proc = self.windows.get(ask_id)
        if proc is not None:
            proc.kill()

    def serve(self) -> None:
        while (msg := self.read()) is not None:
            method = msg.get("method")
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            if method == "herkos.agent.ask" and isinstance(msg.get("id"), str):
                threading.Thread(target=self.ask, args=(msg["id"], params), daemon=True).start()
            elif method == "herkos.agent.cancel" and isinstance(params.get("id"), str):
                self.cancel(params["id"])
            elif "id" in msg and method is not None:
                err = {"code": -32601, "message": f"unknown method {method}"}
                self.send({"jsonrpc": "2.0", "id": msg["id"], "error": err})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="herkos-agent", description="Show the system Herkos's questions in this session."
    )
    parser.add_argument("--socket", default=SYSTEM_SOCKET)
    parser.add_argument(
        "--window-command", metavar="PATH", help="a window program other than herkos.dialog"
    )
    args = parser.parse_args(argv)
    window = [args.window_command] if args.window_command else default_window()
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(args.socket)
        agent = Agent(sock, window)
        agent.register()
    except NotServed as e:
        print(f"herkos-agent: {args.socket}: {e}: this account is not served", file=sys.stderr)
        return NOT_SERVED
    except (OSError, ConnectionError, ValueError) as e:
        print(f"herkos-agent: {args.socket}: {e}", file=sys.stderr)
        return 1
    print(f"herkos-agent: registered with {args.socket}", file=sys.stderr)
    try:
        agent.serve()
        print(f"herkos-agent: {args.socket} closed the connection", file=sys.stderr)
    except (OSError, ValueError) as e:
        print(f"herkos-agent: {e}", file=sys.stderr)
    # Herkos went away (restart, update): systemd starts the agent again.
    return 1


if __name__ == "__main__":
    sys.exit(main())
