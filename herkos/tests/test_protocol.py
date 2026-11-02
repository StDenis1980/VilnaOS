# SPDX-License-Identifier: GPL-3.0-or-later
"""Protocol tests over a real Unix socket (herkos/docs/protocol.md).

They talk to Herkos only through the socket, the way Gloxi does, so they can
later run against the Rust Herkos as conformance tests.
"""

import asyncio
import json
import os
import socket
import stat
import threading

import pytest
from conftest import Env

from herkos import PROTOCOL_VERSION
from herkos.confirm import Answer
from herkos.server import Server, SocketError, prepare_socket_dir


class Client:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(str(path))
        self.file = self.sock.makefile("rwb")
        self.next_id = 0

    def send_raw(self, data: bytes):
        self.file.write(data)
        self.file.flush()

    def recv(self):
        line = self.file.readline()
        return json.loads(line) if line else None

    def call(self, method, params=None):
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg).encode() + b"\n")
        return self.recv()

    def hello(self):
        return self.call("herkos.hello", {"protocol": [PROTOCOL_VERSION], "client": "test"})

    def close(self):
        self.sock.close()


@pytest.fixture
def served(tmp_path):
    """Run Herkos on a socket in a background event loop; yield (env, path, server)."""
    env = Env(tmp_path, Answer.ONCE)
    path = tmp_path / "run" / "vilna" / "herkos.sock"
    server = Server(env.gate, path)
    loop = asyncio.new_event_loop()

    started = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.start())
        started.set()
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert started.wait(5)
    yield env, path, server
    asyncio.run_coroutine_threadsafe(server.close(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)


def test_socket_permissions(served):
    _, path, _ = served
    assert stat.S_ISSOCK(os.lstat(path).st_mode)
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(path.parent).st_mode) == 0o700


def test_hello_is_required(served):
    _, path, _ = served
    c = Client(path)
    r = c.call("herkos.request_action", {})
    assert r["error"]["code"] == -32001
    r = c.call("herkos.hello", {"protocol": ["9.9"]})
    assert r["error"]["code"] == -32002
    r = c.hello()
    assert r["result"]["protocol"] == PROTOCOL_VERSION
    assert r["result"]["max_level"] == "R2"
    c.close()


def test_request_action_roundtrip(served):
    env, path, _ = served
    c = Client(path)
    c.hello()
    r = c.call(
        "herkos.request_action",
        {
            "tool": "org.vilna.mcp.files",
            "operation": "read",
            "resources": [(env.docs / "a.txt").as_uri()],
            "reason": "the user asked to summarise a.txt",
            "origin": {"user_turn": 3},
            "level": "R0",
            "tainted": False,
        },
    )["result"]
    assert r["decision"] == "allowed" and r["level"] == "R1"
    assert r["result"] == {"ok": True, "operation": "read"}
    assert c.call("herkos.session")["result"]["tainted"] is True
    c.close()


def test_denial_is_a_result_not_an_error(served):
    env, path, _ = served
    c = Client(path)
    c.hello()
    r = c.call(
        "herkos.request_action",
        {"tool": "org.example.none", "operation": "x", "reason": "r"},
    )
    assert r["result"]["decision"] == "denied" and r["result"]["code"] == "denied_unknown"
    c.close()


def test_errors(served):
    _, path, _ = served
    c = Client(path)
    c.send_raw(b"{not json\n")
    assert c.recv()["error"]["code"] == -32700
    c.send_raw(b'{"id": 1, "method": "herkos.hello"}\n')
    assert c.recv()["error"]["code"] == -32600
    c.hello()
    assert c.call("herkos.request_action", {"tool": "x"})["error"]["code"] == -32602
    assert c.call("herkos.grant_myself")["error"]["code"] == -32601
    # A notification gets no answer; the next call still works.
    c.send_raw(b'{"jsonrpc": "2.0", "method": "herkos.session"}\n')
    assert "result" in c.call("herkos.session")
    c.close()


def test_taint_is_per_connection(served):
    env, path, _ = served
    a, b = Client(path), Client(path)
    a.hello()
    b.hello()
    a.call(
        "herkos.request_action",
        {
            "tool": "org.vilna.mcp.files",
            "operation": "read",
            "resources": [(env.docs / "a").as_uri()],
            "reason": "r",
        },
    )
    assert a.call("herkos.session")["result"]["tainted"] is True
    assert b.call("herkos.session")["result"]["tainted"] is False
    a.close()
    b.close()


def test_too_long_message_closes_connection(served):
    _, path, _ = served
    c = Client(path)
    c.send_raw(b'{"x": "' + b"a" * (1024 * 1024 + 10) + b'"}\n')
    r = c.recv()
    assert r["error"]["code"] == -32600
    assert c.recv() is None
    c.close()


def test_other_uid_is_refused(tmp_path):
    env = Env(tmp_path, Answer.ONCE)
    path = tmp_path / "s" / "herkos.sock"

    async def scenario():
        server = Server(env.gate, path, allowed_uid=os.getuid() + 1)
        await server.start()
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b'{"jsonrpc":"2.0","id":1,"method":"herkos.hello","params":{}}\n')
        await writer.drain()
        try:
            data = await asyncio.wait_for(reader.read(), 5)
        except ConnectionResetError:
            data = b""
        writer.close()
        await server.close()
        return data

    assert asyncio.run(scenario()) == b""


def test_socket_dir_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "vilna" / "herkos.sock"
    path.parent.mkdir()
    monkeypatch.setattr(os, "getuid", lambda: os.geteuid() + 1)
    with pytest.raises(SocketError):
        prepare_socket_dir(path)


def test_regular_file_in_place_of_socket_is_refused(tmp_path):
    path = tmp_path / "vilna" / "herkos.sock"
    path.parent.mkdir(mode=0o700)
    path.write_text("not a socket")
    with pytest.raises(SocketError):
        prepare_socket_dir(path)
