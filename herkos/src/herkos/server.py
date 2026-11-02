# SPDX-License-Identifier: GPL-3.0-or-later
"""The Gloxi <-> Herkos socket (ADR 0005, herkos/docs/protocol.md).

JSON-RPC 2.0, one message per line, over a Unix stream socket. Herkos checks
the peer with ``SO_PEERCRED`` and accepts only the expected uid (in
Herkos-lite: its own). The first call on a connection must be
``herkos.hello``; until then every other method fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import stat
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

from herkos import PROTOCOL_VERSION, __version__
from herkos.gate import Gate
from herkos.policy import SessionState
from herkos.request import RequestError, parse_request

MAX_LINE = 1024 * 1024  # bytes; a longer message closes the connection

# JSON-RPC 2.0 error codes, plus Herkos's own in the -32000..-32099 range.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HELLO_REQUIRED = -32001
PROTOCOL_MISMATCH = -32002


class SocketError(Exception):
    """The socket folder is unsafe or the socket cannot be created."""


def peer_credentials(sock: socket.socket) -> tuple[int, int, int]:
    """(pid, uid, gid) of the process at the other end of a Unix socket."""
    fmt = "3i"
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(fmt))
    pid, uid, gid = struct.unpack(fmt, raw)
    return pid, uid, gid


def prepare_socket_dir(path: Path) -> None:
    """Create the socket's folder as 0700 and make sure nobody else owns it."""
    folder = path.parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(folder)
    if not stat.S_ISDIR(st.st_mode):
        raise SocketError(f"{folder} is not a folder")
    if st.st_uid != os.getuid():
        raise SocketError(f"{folder} belongs to another user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(folder, 0o700)
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise SocketError(f"{path} exists and is not a socket")
        path.unlink()  # stale socket from an earlier run


def _result(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


class Connection:
    def __init__(self, gate: Gate, peer: tuple[int, int, int]) -> None:
        self.gate = gate
        self.peer = peer
        self.session = SessionState()
        self.hello = False

    def dispatch(self, msg: Any) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        id_ = msg.get("id")
        is_notification = "id" not in msg
        if (id_ is not None and not isinstance(id_, str | int)) or isinstance(id_, bool):
            return _error(None, INVALID_REQUEST, "id must be a string or an integer")
        method, params = msg.get("method"), msg.get("params", {})
        if not isinstance(method, str):
            return _error(id_, INVALID_REQUEST, "method must be a string")
        try:
            response = self._call(id_, method, params)
        except Exception as e:  # never let a bug turn into an allowance or a crash
            response = _error(id_, INTERNAL_ERROR, f"internal error: {type(e).__name__}")
        return None if is_notification else response

    def _call(self, id_: Any, method: str, params: Any) -> dict:
        if method == "herkos.hello":
            return self._hello(id_, params)
        if not self.hello:
            return _error(id_, HELLO_REQUIRED, "call herkos.hello first")
        if method == "herkos.request_action":
            try:
                request = parse_request(params)
            except RequestError as e:
                return _error(id_, INVALID_PARAMS, str(e))
            outcome = self.gate.handle(request, self.session)
            return _result(id_, outcome.to_json())
        if method == "herkos.session":
            return _result(
                id_,
                {"tainted": self.session.tainted, "taint_sources": self.session.taint_sources},
            )
        return _error(id_, METHOD_NOT_FOUND, f"unknown method {method}")

    def _hello(self, id_: Any, params: Any) -> dict:
        if not isinstance(params, dict):
            return _error(id_, INVALID_PARAMS, "params must be an object")
        versions = params.get("protocol")
        if isinstance(versions, str):
            versions = [versions]
        if not isinstance(versions, list) or PROTOCOL_VERSION not in versions:
            return _error(id_, PROTOCOL_MISMATCH, f"this Herkos speaks protocol {PROTOCOL_VERSION}")
        self.hello = True
        return _result(
            id_,
            {
                "protocol": PROTOCOL_VERSION,
                "server": f"herkos-lite/{__version__}",
                "max_level": self.gate.config.max_level.name,
            },
        )


class Server:
    def __init__(
        self,
        gate: Gate,
        path: Path,
        *,
        allowed_uid: int | None = None,
        credentials: Callable[[socket.socket], tuple[int, int, int]] = peer_credentials,
    ) -> None:
        self.gate = gate
        self.path = path
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self.credentials = credentials
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        prepare_socket_dir(self.path)
        old = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(
                self._serve, path=str(self.path), limit=MAX_LINE
            )
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        try:
            peer = self.credentials(sock)
        except OSError:
            writer.close()
            return
        if peer[1] != self.allowed_uid:
            writer.close()  # not ours: no answer at all
            return
        conn = Connection(self.gate, peer)
        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    writer.write(self._encode(_error(None, INVALID_REQUEST, "message too long")))
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    response = _error(None, PARSE_ERROR, "not valid JSON")
                else:
                    # The gate may block on the user's answer: keep the loop free.
                    response = await asyncio.to_thread(conn.dispatch, msg)
                if response is not None:
                    writer.write(self._encode(response))
                    await writer.drain()
        except ConnectionError:
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    @staticmethod
    def _encode(msg: dict) -> bytes:
        return json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
