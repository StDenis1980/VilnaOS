# SPDX-License-Identifier: Apache-2.0
"""Undos that once could not be done, and arguments that once came back as Python errors."""

from __future__ import annotations

import io
import json

import pytest

from vilna_mcp_settings.server import Server, demo_backend, demo_sound
from vilna_mcp_settings.settings import SHELL, SOUND, Settings


def test_a_louder_than_full_volume_can_be_put_back():
    sound = demo_sound()
    sound.values[(SOUND, "volume")] = 150
    s = Settings(demo_backend(), sound)
    out = s.set_volume(30)
    s.restore(**out["undo_arguments"])
    assert sound.values[(SOUND, "volume")] == 150


def test_a_sound_a_newer_shell_turned_off_survives_a_change_and_its_undo():
    backend = demo_backend()
    backend.values[(SHELL, "disabled-sounds")] = ["bell", "future-sound"]
    s = Settings(backend, demo_sound())
    out = s.set_sound("bell", True)
    assert "future-sound" in backend.values[(SHELL, "disabled-sounds")]
    s.restore(**out["undo_arguments"])
    assert backend.values[(SHELL, "disabled-sounds")] == ["bell", "future-sound"]


@pytest.mark.parametrize(
    "name,args",
    [
        ("set_text_scale", {"scale": 10**400}),
        ("set_night_light", {"enabled": True, "from_hour": 10**400}),
        ("restore", {"values": {"disabled_sounds": [["a"]]}}),
    ],
)
def test_odd_numbers_and_lists_get_a_plain_answer(name, args):
    out = Server(Settings(demo_backend(), demo_sound())).call(name, args)
    assert out["isError"] is True
    assert not any(e in out["content"][0]["text"] for e in ("OverflowError", "TypeError"))


def test_a_huge_unknown_name_is_not_sent_back_whole():
    server = Server(Settings(demo_backend(), demo_sound()))
    for out in (
        server.call("restore", {"values": {"k" * 10**6: 1}}),
        server.call("x" * 10**6, {}),
    ):
        assert out["isError"] is True and len(out["content"][0]["text"]) < 1000


@pytest.mark.parametrize(
    "line",
    [
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":["a"]}}',
        "[" * 1000 + "]" * 1000,
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"\\ud800","arguments":{}}}',
    ],
)
def test_one_line_never_ends_the_server(line):
    out = io.StringIO()
    ping = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    Server(Settings(demo_backend(), demo_sound())).serve(
        io.StringIO(line + "\n" + ping + "\n"), out
    )
    out.getvalue().encode("utf-8")
    assert json.loads(out.getvalue().splitlines()[-1])["result"] == {}
