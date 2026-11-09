# SPDX-License-Identifier: Apache-2.0
import io
import json
import subprocess
import sys

import pytest

from vilna_mcp_settings.server import Server, demo_backend, demo_sound
from vilna_mcp_settings.settings import INTERFACE, MemoryBackend, Settings, SettingsError


@pytest.fixture
def settings():
    return Settings(demo_backend(), demo_sound())


def test_dark_and_back(settings):
    out = settings.set_color_scheme("dark")
    assert settings.backend.values[(INTERFACE, "color-scheme")] == "prefer-dark"
    assert out["undo_arguments"] == {"values": {"color_scheme": "default"}}
    settings.restore(**out["undo_arguments"])
    assert settings.backend.values[(INTERFACE, "color-scheme")] == "default"


def test_undo_of_undo(settings):
    first = settings.set_text_scale(1.25)
    second = settings.restore(**first["undo_arguments"])
    assert settings.get()["values"]["text_scale"] == 1.0
    settings.restore(**second["undo_arguments"])
    assert settings.get()["values"]["text_scale"] == 1.25


def test_whole_number_for_double_key_stays_double(settings):
    settings.set_text_scale(2)
    assert isinstance(settings.backend.values[(INTERFACE, "text-scaling-factor")], float)


def test_night_light_hours_mean_fixed_schedule(settings):
    out = settings.set_night_light(enabled=True, from_hour=22, to_hour=7)
    v = settings.get()["values"]
    assert v["night_light"] is True and v["night_light_automatic"] is False
    assert v["night_light_from"] == 22.0 and v["night_light_to"] == 7.0
    assert set(out["undo_arguments"]["values"]) == {
        "night_light",
        "night_light_automatic",
        "night_light_from",
        "night_light_to",
    }


def test_do_not_disturb_hides_banners(settings):
    settings.set_do_not_disturb(True)
    assert settings.get()["values"]["show_banners"] is False


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.set_color_scheme("pink"),
        lambda s: s.set_accent_color("magenta"),
        lambda s: s.set_text_scale(10),
        lambda s: s.set_text_scale(float("nan")),
        lambda s: s.set_night_light(temperature=10000),
        lambda s: s.set_night_light(),
        lambda s: s.restore({"lock_enabled": False}),
        lambda s: s.restore({"night_light": "yes"}),
        lambda s: s.restore({}),
        lambda s: s.restore("x"),
    ],
)
def test_bad_values_change_nothing(settings, call):
    before = dict(settings.backend.values)
    with pytest.raises(SettingsError):
        call(settings)
    assert settings.backend.values == before


def test_key_missing_on_this_gnome():
    # GNOME 46: no accent colour yet.
    s = Settings(MemoryBackend({(INTERFACE, "color-scheme"): "default"}))
    with pytest.raises(SettingsError, match="not available"):
        s.set_accent_color("teal")
    assert s.get() == {"values": {"color_scheme": "default"}}


def test_all_or_nothing_when_a_write_fails():
    class Failing(MemoryBackend):
        def set(self, schema, key, value):
            if key == "night-light-schedule-to":
                raise SettingsError("refused")
            super().set(schema, key, value)

    s = Settings(Failing(demo_backend().values))
    before = dict(s.backend.values)
    with pytest.raises(SettingsError):
        s.set_night_light(from_hour=21, to_hour=6)
    assert s.backend.values == before


def _call(server, name, arguments):
    return server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )["result"]


def test_server_checks_arguments():
    server = Server(Settings(demo_backend()))
    assert _call(server, "set_color_scheme", {"scheme": "dark"})["isError"] is False
    assert _call(server, "set_color_scheme", {"scheme": 1})["isError"] is True
    assert _call(server, "set_color_scheme", {})["isError"] is True
    assert _call(server, "set_night_light", {"enabled": "on"})["isError"] is True
    assert _call(server, "set_night_light", {"surprise": True})["isError"] is True
    assert _call(server, "nope", {})["isError"] is True
    out = _call(server, "get", {})
    assert out["structuredContent"]["values"]["color_scheme"] == "prefer-dark"


def test_server_survives_junk():
    server = Server(Settings(demo_backend()))
    out = io.StringIO()
    lines = [
        "not json",
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "x"}),
        json.dumps([1, 2]),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]
    server.serve(io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(replies) == 4
    assert [t["name"] for t in replies[-1]["result"]["tools"]][0] == "get"


def test_runs_as_module_in_memory():
    msg = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get", "arguments": {}},
        }
    )
    done = subprocess.run(
        [sys.executable, "-m", "vilna_mcp_settings", "--memory"],
        input=msg + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    reply = json.loads(done.stdout)
    assert reply["result"]["structuredContent"]["values"]["night_light"] is False


def test_volume_mute_output_and_undo(settings):
    out = settings.set_volume(20)
    assert settings.get()["values"]["volume"] == 20
    settings.restore(**out["undo_arguments"])
    assert settings.get()["values"]["volume"] == 50
    settings.set_mute(True)
    out = settings.set_output("headphones")
    assert settings.get()["values"]["output"] == "headphones"
    assert out["undo_arguments"] == {"values": {"output": "speakers"}}
    assert [o["name"] for o in settings.get()["outputs"]] == ["speakers", "headphones"]


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.set_volume(101),
        lambda s: s.set_volume(-1),
        lambda s: s.set_volume(True),
        lambda s: s.set_output("hdmi-9"),
        lambda s: s.set_output(""),
        lambda s: s.restore({"volume": 50, "output": "nowhere"}),
    ],
)
def test_bad_sound_changes_nothing(settings, call):
    before = dict(settings.sound.values)
    with pytest.raises(SettingsError):
        call(settings)
    assert settings.sound.values == before


def test_no_sound_server():
    s = Settings(demo_backend())
    with pytest.raises(SettingsError, match="not available"):
        s.set_volume(30)
    assert "volume" not in s.get()["values"]
