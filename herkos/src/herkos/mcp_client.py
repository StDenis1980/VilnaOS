# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal MCP client over stdio: Herkos starts a tool server and calls it.

Herkos, not Gloxi, owns the tool processes (permissions.md 9.5). One request
at a time per server; a server that dies or answers nonsense is stopped and
every further call fails (and so is a denial for Gloxi, never a bypass).
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any

from herkos import __version__
from herkos.pins import fingerprint
from herkos.request import ActionRequest
from herkos.tools import ToolError

MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_RESPONSE = 16 * 1024 * 1024
CALL_TIMEOUT = 60.0
# Messages without the id waited for (notifications) taken in one call.
MAX_OTHER_MESSAGES = 1000
MAX_ERROR_TEXT = 2000


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
        self._entries: dict[str, dict[str, Any]] = {}
        self._buffer = b""

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
        assert self._proc.stdin is not None
        os.set_blocking(self._proc.stdin.fileno(), False)  # a full pipe never hangs Herkos
        self._buffer = b""
        try:
            self._handshake()
        except McpError:
            self.stop()
            raise
        except Exception as e:  # noqa: BLE001 - an odd server is a server that cannot start
            self.stop()
            raise McpError(f"the tool answered nonsense on start ({type(e).__name__})") from None

    def _handshake(self) -> None:
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
        listed = tools.get("tools") if isinstance(tools, dict) else None
        # An answer of another shape (null, a list, a string) offers nothing.
        for tool in listed if isinstance(listed, list) else []:
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                ann = tool.get("annotations")
                self._annotations[tool["name"]] = ann if isinstance(ann, dict) else {}
                self._fingerprints[tool["name"]] = fingerprint(tool)
                self._entries[tool["name"]] = tool

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.stdin:
            with contextlib.suppress(OSError):  # a request still in a full pipe
                proc.stdin.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()  # a server that does not leave when its input closes
            proc.wait()
        if proc.stdout:
            proc.stdout.close()  # nobody reads it any more: closing cannot wait

    # -- Executor ------------------------------------------------------------

    def annotations(self, operation: str) -> Mapping[str, object] | None:
        return self._annotations.get(operation)

    def entry(self, operation: str) -> Mapping[str, Any] | None:
        """The server's own ``tools/list`` entry: external content."""
        return self._entries.get(operation)

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
            content = out.get("content")
            texts = [
                c["text"]
                for c in (content if isinstance(content, list) else [])
                if isinstance(c, dict) and isinstance(c.get("text"), str)
            ]
            text = "; ".join(t for t in texts if t) or "the tool failed"
            raise ToolError(text[:MAX_ERROR_TEXT])
        if "structuredContent" in out:
            return out["structuredContent"]
        return {"content": out.get("content", [])}

    # -- wire ----------------------------------------------------------------

    def _send(self, msg: dict, deadline: float | None = None) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpError("the tool is not running")
        deadline = time.monotonic() + self.timeout if deadline is None else deadline
        try:
            data = json.dumps(msg, ensure_ascii=False).encode() + b"\n"
        except UnicodeEncodeError:  # a lone surrogate: escaped, which JSON allows
            data = json.dumps(msg).encode() + b"\n"
        fd = self._proc.stdin.fileno()
        try:
            while data:
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([], [fd], [], left)[1]:
                    self.stop()
                    raise McpError("the tool does not take the request")
                data = data[os.write(fd, data) :]
        except OSError as e:
            self.stop()
            raise McpError(f"the tool stopped: {e}") from None

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _rpc(self, method: str, params: dict) -> Any:
        with self._lock:
            # One deadline for the whole call: notifications do not extend it.
            deadline = time.monotonic() + self.timeout
            self._next_id += 1
            want = self._next_id
            self._send({"jsonrpc": "2.0", "id": want, "method": method, "params": params}, deadline)
            for _ in range(MAX_OTHER_MESSAGES + 1):
                msg = self._read(deadline)
                got = msg.get("id")
                if type(got) is int and got == want:  # not 3.0, not true
                    break
                # Requests or notifications from the server are not supported: ignore.
            else:
                self.stop()
                raise McpError("the tool sent too many other messages")
            if "error" in msg:
                err = msg["error"] if isinstance(msg["error"], dict) else {}
                text = err.get("message")
                text = text[:MAX_ERROR_TEXT] if isinstance(text, str) else "error"
                raise McpError(f"{method}: {text}")
            return msg.get("result")

    def _read(self, deadline: float) -> dict:
        assert self._proc is not None and self._proc.stdout is not None
        fd = self._proc.stdout.fileno()
        while b"\n" not in self._buffer:
            if len(self._buffer) > MAX_RESPONSE:
                self.stop()
                raise McpError("the tool's answer is too long")
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                self.stop()
                raise McpError("the tool did not answer")
            try:
                chunk = os.read(fd, 1 << 16)
            except OSError:
                chunk = b""
            if not chunk:
                self.stop()
                raise McpError("the tool did not answer")
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        if len(line) + 1 > MAX_RESPONSE:  # the limit counts the line break too
            self.stop()
            raise McpError("the tool's answer is too long")
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):
            self.stop()
            raise McpError("the tool's answer is not JSON") from None
        if not isinstance(msg, dict):
            self.stop()
            raise McpError("the tool's answer is not a JSON-RPC message")
        return msg
