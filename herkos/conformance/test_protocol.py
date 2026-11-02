# SPDX-License-Identifier: GPL-3.0-or-later
"""Protocol v0.1 (herkos/docs/protocol.md), checked from outside."""

from __future__ import annotations

import os
import stat

import pytest
from conftest import PROTOCOL, Client

LEVELS = {"R0", "R1", "R2", "R3", "R4"}


def test_socket_and_folder_are_private(herkos):
    assert stat.S_ISSOCK(os.lstat(herkos.socket).st_mode)
    assert stat.S_IMODE(os.lstat(herkos.socket).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(herkos.socket.parent).st_mode) == 0o700


def test_hello_comes_first(client):
    assert client.call("herkos.request_action", {})["error"]["code"] == -32001
    assert client.call("herkos.session")["error"]["code"] == -32001
    assert client.call("herkos.undo", {"action_id": "abc"})["error"]["code"] == -32001


def test_hello(client):
    assert client.call("herkos.hello", {"protocol": ["9.9"]})["error"]["code"] == -32002
    assert client.call("herkos.hello", [PROTOCOL])["error"]["code"] == -32602
    r = client.hello()["result"]
    assert r["protocol"] == PROTOCOL
    assert r["max_level"] in LEVELS
    assert isinstance(r["server"], str) and r["server"]


def test_hello_accepts_a_single_version(client):
    assert client.call("herkos.hello", {"protocol": PROTOCOL})["result"]["protocol"] == PROTOCOL


@pytest.mark.parametrize(
    "raw, code",
    [
        (b"{not json\n", -32700),
        (b'{"id": 1, "method": "herkos.hello"}\n', -32600),  # no "jsonrpc"
        (b'{"jsonrpc": "1.0", "id": 1, "method": "herkos.hello"}\n', -32600),
        (b'{"jsonrpc": "2.0", "id": true, "method": "herkos.hello"}\n', -32600),
        (b'{"jsonrpc": "2.0", "id": 1, "method": 5}\n', -32600),
        (b"[1, 2]\n", -32600),
    ],
)
def test_malformed_messages(client, raw, code):
    client.send_raw(raw)
    assert client.recv()["error"]["code"] == code


def test_unknown_method_and_bad_params(client):
    client.hello()
    assert client.call("herkos.grant_myself")["error"]["code"] == -32601
    assert client.call("herkos.request_action", {"tool": "x"})["error"]["code"] == -32602
    assert client.call("herkos.request_action", [1])["error"]["code"] == -32602
    assert client.call("herkos.undo", {"action_id": "../x"})["error"]["code"] == -32602


@pytest.mark.parametrize(
    "params",
    [
        {"tool": "org.example.x", "operation": "y"},  # no reason
        {"tool": "org.example.x", "operation": "y", "reason": "   "},
        {"tool": "Org.Example", "operation": "y", "reason": "r"},
        {"tool": "org.example.x", "operation": "y", "reason": "r" * 501},
        {"tool": "org.example.x", "operation": "y", "reason": "r", "extra": 1},
        {"tool": "org.example.x", "operation": "y", "reason": "r", "resources": "file:///a"},
        {"tool": "org.example.x", "operation": "y", "reason": "r", "resources": ["file:a"]},
        {"tool": "org.example.x", "operation": "y", "reason": "r", "resources": ["//x/a"]},
        {"tool": "o.e", "operation": "y", "reason": "r", "resources": ["file://host/a"]},
        {"tool": "org.example.x", "operation": "y", "reason": "r", "origin": {"user_turn": -1}},
        {"tool": "org.example.x", "operation": "y", "reason": "r", "origin": {"user_turn": True}},
        {"tool": "org.example.x", "operation": "y", "reason": "r", "arguments": []},
    ],
)
def test_malformed_requests_are_invalid_params(client, params):
    client.hello()
    assert client.call("herkos.request_action", params)["error"]["code"] == -32602


def test_fields_herkos_sets_itself_are_ignored(client):
    client.hello()
    r = client.call(
        "herkos.request_action",
        {
            "tool": "org.example.none",
            "operation": "x",
            "reason": "r",
            "level": "R0",
            "tainted": False,
            "action_id": "mine",
            "risk": 0,
        },
    )["result"]
    assert r["action_id"] != "mine"


def test_unknown_tool_is_denied_as_a_result(client):
    client.hello()
    r = client.call(
        "herkos.request_action", {"tool": "org.example.none", "operation": "x", "reason": "r"}
    )["result"]
    assert r["decision"] == "denied" and r["code"] == "denied_unknown"
    assert isinstance(r["action_id"], str) and r["action_id"]
    assert isinstance(r["message"], str)
    assert "result" not in r


def test_notification_gets_no_answer(client):
    client.hello()
    client.send_raw(b'{"jsonrpc": "2.0", "method": "herkos.session"}\n')
    assert "result" in client.call("herkos.session")


def test_fresh_session(client):
    client.hello()
    assert client.call("herkos.session")["result"] == {
        "tainted": False,
        "taint_sources": [],
        "findings": [],
    }


def test_nothing_to_undo(client):
    client.hello()
    r = client.call("herkos.undo", {"action_id": "0123456789abcdef"})["result"]
    assert r["decision"] == "denied" and r["code"] == "denied_unknown"


def test_too_long_message_closes_the_connection(client):
    client.send_raw(b'{"x": "' + b"a" * (1024 * 1024 + 10) + b'"}\n')
    assert client.recv()["error"]["code"] == -32600
    assert client.recv() is None


def test_connections_are_independent(herkos):
    a, b = Client(herkos.socket), Client(herkos.socket)
    a.hello()
    assert b.call("herkos.session")["error"]["code"] == -32001
    b.hello()
    assert b.call("herkos.session")["result"]["tainted"] is False
    a.close()
    b.close()
