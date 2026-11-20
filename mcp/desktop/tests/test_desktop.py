# SPDX-License-Identifier: Apache-2.0
import pytest
from fakes import FakeBus

from vilna_mcp_desktop.desktop import Desktop
from vilna_mcp_desktop.mcp import ToolError


def test_no_battery():
    bus = FakeBus({"org.freedesktop.DBus.Properties.GetAll": ({"IsPresent": False},)})
    assert Desktop(bus).battery() == {"battery": False}


def test_notifications_are_plain_text_from_gloxi():
    bus = FakeBus({"org.freedesktop.Notifications.Notify": (7,)})
    Desktop(bus).notify("Hi", "<a href='https://x'>click</a> & more")
    (_bus, _name, _path, method, args) = bus.calls[0]
    assert method == "org.freedesktop.Notifications.Notify"
    app, _replaces, _icon, title, body, actions, hints, _timeout = args
    assert (app, title, actions, hints) == ("Gloxi", "Hi", [], {})
    assert "<a" not in body and body == "&lt;a href='https://x'&gt;click&lt;/a&gt; &amp; more"


def test_media_control_needs_a_player_when_there_are_several():
    bus = FakeBus({}, names=["org.mpris.MediaPlayer2.a", "org.mpris.MediaPlayer2.b", "org.other"])
    d = Desktop(bus)
    with pytest.raises(ToolError, match="say which player"):
        d.media_control("pause")
    with pytest.raises(ToolError, match="no player 'c'"):
        d.media_control("pause", "c")
    assert d.media_control("pause", "b") == {"player": "b", "done": "pause"}
    assert bus.calls[-1][1:4] == (
        "org.mpris.MediaPlayer2.b",
        "/org/mpris/MediaPlayer2",
        "org.mpris.MediaPlayer2.Player.Pause",
    )
    with pytest.raises(ToolError, match="no player is open"):
        Desktop(FakeBus({})).media_control("play")
