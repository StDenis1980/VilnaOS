# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal MCP client over stdio: Herkos starts a tool server and calls it.

Herkos, not Gloxi, owns the tool processes (permissions.md 9.5). One request
at a time per server; a server that dies or answers nonsense is stopped and
every further call fails (and so is a denial for Gloxi, never a bypass).
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from herkos import __version__
from herkos.pins import fingerprint
from herkos.request import ActionRequest
from herkos.tools import ToolError

MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_RESPONSE = 16 * 1024 * 1024
CALL_TIMEOUT = 60.0


class McpError(Exception):
    """The tool server cannot be started or broke the protocol."""


class McpStdioExecutor:
    def __init__(self, command: Sequence[str], *, timeout: float = CALL_TIMEOUT) -> None:
        self.command = list(command)
        self.timeout = timeout
        self._lock = threading.Lock()
        self._next_id = 0
        self._proc: subprocess.Popen[bytes] | None = None
        self._annotations: dict[str, Mapping[str, object]] = {}
        self._fingerprints: dict[str, str] = {}

    # -- life cycle ----------------------------------------------------------

    def start(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as e:
            raise McpError(f"cannot start {self.command[0]}: {e}") from None
        info = self._rpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "herkos-lite", "version": __version__},
            },
        )
        if not isinstance(info, dict):
            raise McpError("bad initialize answer")
        self._notify("notifications/initialized")
        tools = self._rpc("tools/list", {})
        for tool in (tools or {}).get("tools", []):
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                ann = tool.get("annotations")
                self._annotations[tool["name"]] = ann if isinstance(ann, dict) else {}
                self._fingerprints[tool["name"]] = fingerprint(tool)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout):
            if stream:
                stream.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    # -- Executor ------------------------------------------------------------

    def annotations(self, operation: str) -> Mapping[str, object] | None:
        return self._annotations.get(operation)

    def fingerprints(self) -> Mapping[str, str]:
        """What the server said about each operation (pins.py)."""
        return dict(self._fingerprints)

    def execute(self, request: ActionRequest) -> Any:
        if request.operation not in self._annotations:
            raise ToolError(f"the tool has no operation {request.operation}")
        try:
            out = self._rpc(
                "tools/call", {"name": request.operation, "arguments": dict(request.arguments)}
            )
        except McpError as e:
            raise ToolError(str(e)) from None
        if not isinstance(out, dict):
            raise ToolError("bad answer from the tool")
        if out.get("isError"):
            texts = [c.get("text", "") for c in out.get("content", []) if isinstance(c, dict)]
            raise ToolError("; ".join(t for t in texts if t) or "the tool failed")
        if "structuredContent" in out:
            return out["structuredContent"]
        return {"content": out.get("content", [])}

    # -- wire ----------------------------------------------------------------

    def _send(self, msg: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpError("the tool is not running")
        try:
            self._proc.stdin.write(json.dumps(msg, ensure_ascii=False).encode() + b"\n")
            self._proc.stdin.flush()
        except OSError as e:
            self.stop()
            raise McpError(f"the tool stopped: {e}") from None

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _rpc(self, method: str, params: dict) -> Any:
        with self._lock:
            self._next_id += 1
            want = self._next_id
            self._send({"jsonrpc": "2.0", "id": want, "method": method, "params": params})
            while True:
                msg = self._read()
                if msg.get("id") == want:
                    break
                # Requests or notifications from the server are not supported: ignore.
            if "error" in msg:
                err = msg["error"] if isinstance(msg["error"], dict) else {}
                raise McpError(f"{method}: {err.get('message', 'error')}")
            return msg.get("result")

    def _read(self) -> dict:
        assert self._proc is not None and self._proc.stdout is not None
        result: list[bytes] = []
        reader = threading.Thread(
            target=lambda: result.append(self._proc.stdout.readline(MAX_RESPONSE + 1)),
            daemon=True,
        )
        reader.start()
        reader.join(self.timeout)
        if reader.is_alive() or not result or not result[0]:
            self.stop()
            raise McpError("the tool did not answer")
        line = result[0]
        if len(line) > MAX_RESPONSE:
            self.stop()
            raise McpError("the tool's answer is too long")
        try:
            msg = json.loads(line)
        except ValueError:
            self.stop()
            raise McpError("the tool's answer is not JSON") from None
        if not isinstance(msg, dict):
            self.stop()
            raise McpError("the tool's answer is not a JSON-RPC message")
        return msg
