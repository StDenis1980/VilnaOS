# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from herkos.request import RequestError, parse_request

BASE = {"tool": "org.vilna.mcp.files", "operation": "read", "reason": "user asked"}


def test_minimal_request():
    r = parse_request(BASE)
    assert r.tool == "org.vilna.mcp.files"
    assert r.resources == ()


def test_file_uri_is_normalised():
    r = parse_request({**BASE, "resources": ["file:///home/u/Documents/../Private/x%20y.txt"]})
    assert r.resources[0].path == "/home/u/Private/x y.txt"


def test_model_cannot_set_level_or_taint():
    r = parse_request({**BASE, "level": "R0", "tainted": False, "action_id": "mine"})
    assert not hasattr(r, "level")
    assert not hasattr(r, "tainted")


@pytest.mark.parametrize(
    "params",
    [
        [],
        {**BASE, "tool": ""},
        {**BASE, "tool": "../etc"},
        {**BASE, "reason": "  "},
        {**BASE, "reason": "x" * 501},
        {**BASE, "resources": "file:///a"},
        {**BASE, "resources": ["relative/path"]},
        {**BASE, "resources": ["file://remote.host/a"]},
        {**BASE, "resources": ["file:///a%00b"]},
        {**BASE, "resources": [42]},
        {**BASE, "origin": {"user_turn": -1}},
        {**BASE, "origin": {"user_turn": True}},
        {**BASE, "arguments": []},
        {**BASE, "sudo": True},
    ],
)
def test_malformed_requests(params):
    with pytest.raises(RequestError):
        parse_request(params)
