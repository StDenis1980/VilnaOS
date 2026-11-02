# SPDX-License-Identifier: GPL-3.0-or-later
"""Client for the Herkos socket, for Gloxi (herkos/docs/protocol.md).

    with HerkosClient.connect() as herkos:
        outcome = herkos.request_action(
            tool="org.vilna.mcp.files", operation="read",
            resources=["file:///home/u/Documents/a.txt"],
            arguments={"path": "/home/u/Documents/a.txt"},
            reason="The user asked to summarise a.txt", user_turn=3,
        )
        if outcome["decision"] == "allowed": ...

Blocking and line-based on purpose: a call can wait for the user's answer
in the Herkos window, so run it off the UI thread.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from herkos import PROTOCOL_VERSION
from herkos.config import socket_path

DEFAULT_TIMEOUT = 300.0  # the user may take a while to answer


class HerkosUnavailable(Exception):
    """Herkos is not running or the connection broke. Treat as a denial."""


class HerkosError(Exception):
    """Herkos answered with a JSON-RPC error (a bad request)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class HerkosClient:
    def __init__(self, sock: socket.socket, client_name: str) -> None:
        self._sock = sock
        self._file = sock.makefile("rwb")
        self._next_id = 0
        self.server: dict[str, Any] = self._call(
            "herkos.hello", {"protocol": [PROTOCOL_VERSION], "client": client_name}
        )

    @classmethod
    def connect(
        cls,
        path: Path | None = None,
        *,
        client_name: str = "gloxi",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HerkosClient:
        path = path or socket_path()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(str(path))
            return cls(sock, client_name)
        except OSError as e:
            sock.close()
            raise HerkosUnavailable(f"cannot reach Herkos at {path}: {e}") from None

    def request_action(
        self,
        *,
        tool: str,
        operation: str,
        reason: str,
        resources: Sequence[str] = (),
        arguments: Mapping[str, Any] | None = None,
        user_turn: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "tool": tool,
            "operation": operation,
            "reason": reason,
            "resources": list(resources),
            "arguments": dict(arguments or {}),
        }
        if user_turn is not None:
            params["origin"] = {"user_turn": user_turn}
        return self._call("herkos.request_action", params)

    def undo(self, action_id: str) -> dict[str, Any]:
        return self._call("herkos.undo", {"action_id": action_id})

    def session(self) -> dict[str, Any]:
        return self._call("herkos.session", {})

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            self._sock.close()

    def __enter__(self) -> HerkosClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _call(self, method: str, params: dict) -> Any:
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        try:
            self._file.write(json.dumps(msg, ensure_ascii=False).encode() + b"\n")
            self._file.flush()
            line = self._file.readline()
        except OSError as e:
            raise HerkosUnavailable(f"connection to Herkos broke: {e}") from None
        if not line:
            raise HerkosUnavailable("Herkos closed the connection")
        try:
            answer = json.loads(line)
        except ValueError:
            raise HerkosUnavailable("Herkos sent something that is not JSON") from None
        if "error" in answer:
            err = answer["error"]
            raise HerkosError(err.get("code", 0), err.get("message", "error"))
        return answer.get("result")
