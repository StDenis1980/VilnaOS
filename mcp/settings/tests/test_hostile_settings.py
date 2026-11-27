# SPDX-License-Identifier: Apache-2.0
"""Hostile tests for the settings MCP server: limits of the values it accepts.

The server may change only its own list of keys (``KEYS``), each with a type
and a range. These tests feed it values that are wrong in every way we could
think of: keys outside the list, schemas that are not there, values of the
wrong kind (a string for a boolean, NaN, 1e309, a negative volume, a bool for
a number), GVariant-looking strings with an injection, and Unicode in names.
Whatever is refused must change nothing; whatever is accepted must come back
through ``undo_arguments``; every change is a level R2 (reversible) operation
of the permission manifest, and only ``get`` is R0.

Run: ``uv run pytest tests/test_hostile_settings.py`` in ``mcp/settings``.
The tests that need PyGObject (the real GSettings backend) run only where
``gi`` and ``glib-compile-schemas`` exist; they use the in-memory GSettings
backend and never touch the person's dconf.
"""

from __future__ import annotations

import copy
import glob
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tomllib
import unicodedata
from pathlib import Path

import pytest

# Never let a test of the real backend reach the person's dconf: the default
# GSettings backend is chosen once per process, at its first use.
os.environ.setdefault("GSETTINGS_BACKEND", "memory")

from vilna_mcp_settings.server import (  # noqa: E402
    TOOLS,
    Server,
    check_arguments,
    demo_backend,
    demo_sound,
)
from vilna_mcp_settings.settings import (  # noqa: E402
    ACCENT_COLORS,
    COLOR,
    COLOR_SCHEMES,
    DESKTOP_SOUND,
    INTERFACE,
    KEYS,
    NOTIFICATIONS,
    SHELL,
    SOUND,
    SOUND_EVENTS,
    SOUND_THEMES,
    TEMPERATURE,
    MemoryBackend,
    MemorySound,
    Settings,
    SettingsError,
)

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "herkos" / "src" / "herkos" / "manifests" / "org.vilna.mcp.settings.toml"

NAN = float("nan")
INF = float("inf")

# What a hostile caller may put where a value is expected.
JUNK = {
    "none": None,
    "true": True,
    "false": False,
    "zero": 0,
    "one": 1,
    "minus-one": -1,
    "fraction": 0.5000001,
    "nan": NAN,
    "inf": INF,
    "minus-inf": -INF,
    "huge-float": 1e308,
    "tiny-float": 1e-308,
    "huge-int": 10**400,
    "minus-huge-int": -(10**400),
    "text": "x",
    "empty": "",
    "numeric-text": "1",
    "true-text": "true",
    "list": [],
    "list-of-one": [1],
    "list-of-lists": [["bell"]],
    "dict": {},
    "gvariant-injection": "'x'); rm -rf ~; ('",
    "gvariant-array": "['bell', 'x']",
    "gvariant-tuple": "(1, 2)",
    "nul": "dark\0",
    "newline": "dark\n",
    "bidi": "\u202edark",
    "zero-width": "da\u200brk",
    "homoglyph": "d\u0430rk",  # Cyrillic a
    "surrogate": "\ud800",
}

# -- helpers -----------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(demo_backend(), demo_sound())


@pytest.fixture
def server(settings: Settings) -> Server:
    return Server(settings)


def snapshot(s: Settings):
    sound = copy.deepcopy(s.sound.values) if s.sound is not None else None
    return copy.deepcopy(s.backend.values), sound


def call(server: Server, name, arguments=None) -> dict:
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": {} if arguments is None else arguments},
    }
    return server.handle(msg)["result"]


def tool_schema(name: str) -> dict:
    return next(t for t in TOOLS if t["name"] == name)["inputSchema"]


VALID = {
    "get": {},
    "set_color_scheme": {"scheme": "dark"},
    "set_accent_color": {"color": "teal"},
    "set_text_scale": {"scale": 1.5},
    "set_night_light": {
        "enabled": True,
        "automatic": False,
        "from_hour": 21,
        "to_hour": 7,
        "temperature": 3000,
    },
    "set_do_not_disturb": {"enabled": True},
    "set_volume": {"percent": 30},
    "set_mute": {"muted": True},
    "set_output": {"output": "headphones"},
    "set_event_sounds": {"enabled": False},
    "set_sound_theme": {"theme": "bell"},
    "set_sound": {"event": "bell", "enabled": False},
    "restore": {"values": {"volume": 10}},
}
WRITERS = [name for name in VALID if name != "get"]
PROPS = [
    (tool["name"], prop)
    for tool in TOOLS
    for prop in tool["inputSchema"]["properties"]
    if tool["name"] != "restore"
]
REQUIRED = [
    (tool["name"], prop)
    for tool in TOOLS
    for prop in tool["inputSchema"]["required"]
    if tool["name"] != "restore"
]


def _legit(prop: dict, v) -> bool:
    """Whether the schema of a property allows the value (an independent oracle)."""
    kind = prop["type"]
    if kind == "boolean":
        return isinstance(v, bool)
    if kind == "string":
        return isinstance(v, str) and ("enum" not in prop or v in prop["enum"])
    if kind in ("integer", "number"):
        if isinstance(v, bool) or not isinstance(v, int | float):
            return False
        if kind == "integer" and not isinstance(v, int):
            return False
        if isinstance(v, float) and not math.isfinite(v):
            return False
        return prop.get("minimum", -INF) <= v <= prop.get("maximum", INF)
    return False


def _wrong_values():
    for name, arg in PROPS:
        prop = tool_schema(name)["properties"][arg]
        for junk, value in JUNK.items():
            if not _legit(prop, value):
                yield pytest.param(name, arg, value, id=f"{name}-{arg}-{junk}")


WRONG = list(_wrong_values())


# -- the tool table is complete ---------------------------------------------------------


def test_every_tool_has_a_valid_call():
    assert set(VALID) == {t["name"] for t in TOOLS}


def test_every_schema_is_closed_and_typed():
    for tool in TOOLS:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])
        for prop in schema["properties"].values():
            assert prop["type"] in {"string", "boolean", "integer", "number", "object"}


@pytest.mark.parametrize("name", list(VALID))
def test_valid_call_changes_state_and_undo_restores_it(name, server, settings):
    before = snapshot(settings)
    out = call(server, name, VALID[name])
    assert out["isError"] is False, out
    if name == "get":
        assert snapshot(settings) == before
        return
    result = out["structuredContent"]
    assert set(result) == {"changed", "before", "undo_arguments"}
    assert snapshot(settings) != before
    assert set(result["undo_arguments"]) == {"values"}
    assert call(server, "restore", result["undo_arguments"])["isError"] is False
    assert snapshot(settings) == before


def test_get_takes_no_arguments_and_changes_nothing(server, settings):
    before = snapshot(settings)
    assert call(server, "get", {"values": {"volume": 1}})["isError"] is True
    assert call(server, "get", {})["isError"] is False
    assert snapshot(settings) == before


# -- wrong kinds of values, for every argument of every tool ---------------------------------


@pytest.mark.parametrize(("name", "arg", "value"), WRONG)
def test_wrong_value_is_refused_and_changes_nothing(name, arg, value, server, settings):
    args = dict(VALID[name])
    args[arg] = value
    before = snapshot(settings)
    out = call(server, name, args)
    assert out["isError"] is True, (name, arg, out)
    assert snapshot(settings) == before


@pytest.mark.parametrize(("name", "arg"), REQUIRED)
def test_missing_required_argument_is_refused(name, arg, server, settings):
    args = {k: v for k, v in VALID[name].items() if k != arg}
    before = snapshot(settings)
    assert call(server, name, args)["isError"] is True
    assert snapshot(settings) == before


@pytest.mark.parametrize("name", list(VALID))
def test_extra_argument_is_refused(name, server, settings):
    before = snapshot(settings)
    args = dict(VALID[name])
    args["lock_enabled"] = False
    assert call(server, name, args)["isError"] is True
    assert snapshot(settings) == before


@pytest.mark.parametrize("name", list(VALID))
@pytest.mark.parametrize("args", [None, [], "x", 5, True, [{"a": 1}]])
def test_arguments_that_are_not_an_object_are_refused(name, args, server, settings):
    before = snapshot(settings)
    out = server.call(name, args)
    assert out["isError"] is True
    assert snapshot(settings) == before


def test_night_light_with_nothing_to_change_is_refused(server, settings):
    before = snapshot(settings)
    assert call(server, "set_night_light", {})["isError"] is True
    assert snapshot(settings) == before


# -- ranges: the edge itself and one step beyond it -------------------------------------------


def _step_out(prop: dict, edge: float, up: bool):
    if prop["type"] == "integer":
        return edge + 1 if up else edge - 1
    return math.nextafter(edge, INF if up else -INF)


RANGED = [(name, arg) for name, arg in PROPS if "minimum" in tool_schema(name)["properties"][arg]]


@pytest.mark.parametrize(("name", "arg"), RANGED)
def test_edges_of_the_range_are_accepted(name, arg, server):
    prop = tool_schema(name)["properties"][arg]
    for edge in (prop["minimum"], prop["maximum"]):
        args = dict(VALID[name])
        args[arg] = edge
        assert call(server, name, args)["isError"] is False, (name, arg, edge)


@pytest.mark.parametrize(("name", "arg"), RANGED)
def test_one_step_beyond_the_range_is_refused(name, arg, server, settings):
    prop = tool_schema(name)["properties"][arg]
    before = snapshot(settings)
    for edge, up in ((prop["minimum"], False), (prop["maximum"], True)):
        args = dict(VALID[name])
        args[arg] = _step_out(prop, edge, up)
        assert call(server, name, args)["isError"] is True, (name, arg, edge)
        assert snapshot(settings) == before


def test_the_published_ranges_match_what_is_enforced(settings):
    """tools/list must not promise a wider or narrower range than the checks give."""
    assert tool_schema("set_text_scale")["properties"]["scale"]["minimum"] == 0.5
    assert tool_schema("set_text_scale")["properties"]["scale"]["maximum"] == 3.0
    assert tool_schema("set_volume")["properties"]["percent"]["maximum"] == 100
    temp = tool_schema("set_night_light")["properties"]["temperature"]
    assert (temp["minimum"], temp["maximum"]) == TEMPERATURE
    for k in ("from_hour", "to_hour"):
        hour = tool_schema("set_night_light")["properties"][k]
        assert (hour["minimum"], hour["maximum"]) == (0, 24)


@pytest.mark.parametrize("scale", [NAN, INF, -INF, 0, -0.5, -1, 0.49, 3.01, 10, 1e309, 1e308, True])
def test_text_scale_out_of_bounds(scale, settings):
    before = snapshot(settings)
    with pytest.raises((SettingsError, OverflowError)):
        settings.set_text_scale(scale)
    assert snapshot(settings) == before


@pytest.mark.parametrize(
    "percent", [-1, -100, 101, 255, 1000, 2**31, 2**63, 10**400, 50.0, NAN, "50"]
)
def test_volume_out_of_bounds(percent, settings):
    before = snapshot(settings)
    with pytest.raises((SettingsError, OverflowError)):
        settings.set_volume(percent)
    assert snapshot(settings) == before


@pytest.mark.parametrize("percent", [0, 1, 50, 99, 100])
def test_volume_accepted_range(percent, settings):
    settings.set_volume(percent)
    assert settings.get()["values"]["volume"] == percent


@pytest.mark.parametrize("temperature", [0, -2700, 1699, 4701, 2**32, 2**32 - 1, 2700.0, "2700"])
def test_temperature_out_of_bounds(temperature, settings):
    before = snapshot(settings)
    with pytest.raises(SettingsError):
        settings.set_night_light(temperature=temperature)
    assert snapshot(settings) == before


@pytest.mark.parametrize("hour", [-0.0001, 24.0001, -1, 25, NAN, INF, -INF, True, "22"])
@pytest.mark.parametrize("field", ["from_hour", "to_hour"])
def test_hour_out_of_bounds(field, hour, settings):
    before = snapshot(settings)
    with pytest.raises(SettingsError):
        settings.set_night_light(**{field: hour})
    assert snapshot(settings) == before


def test_whole_number_for_a_double_key_is_stored_as_double(settings):
    settings.set_night_light(from_hour=22, to_hour=7)
    values = settings.backend.values
    assert isinstance(values[(COLOR, "night-light-schedule-from")], float)
    assert isinstance(values[(COLOR, "night-light-schedule-to")], float)
    settings.set_text_scale(3)
    assert isinstance(values[(INTERFACE, "text-scaling-factor")], float)


def test_hours_mean_a_fixed_schedule_unless_automatic_is_said(settings):
    settings.set_night_light(from_hour=1)
    assert settings.get()["values"]["night_light_automatic"] is False
    settings.set_night_light(from_hour=2, automatic=True)
    assert settings.get()["values"]["night_light_automatic"] is True


# -- values that are words: only the listed ones ---------------------------------------------


ENUMS = [
    ("set_color_scheme", "scheme", ["light", "dark"]),
    ("set_accent_color", "color", list(ACCENT_COLORS)),
    ("set_sound_theme", "theme", list(SOUND_THEMES)),
]
LOOKALIKES = [
    "Dark",
    "DARK",
    " dark",
    "dark ",
    "dark\n",
    "dark\0",
    "d\u0430rk",  # Cyrillic a
    "ｄａｒｋ",  # fullwidth
    "da\u200brk",
    "\u202edark",
    "prefer-dark",
    "default",
    "",
    "*",
    "../dark",
    "dark'); rm -rf ~; ('",
    "'dark'",
    "<dark>",
    "wood\u0301",  # a combining accent after a valid name
]


@pytest.mark.parametrize(("tool", "arg", "members"), ENUMS)
def test_only_listed_words_are_accepted(tool, arg, members, server, settings):
    for member in members:
        assert call(server, tool, {arg: member})["isError"] is False, member
    before = snapshot(settings)
    for word in LOOKALIKES:
        if word in members:
            continue
        assert call(server, tool, {arg: word})["isError"] is True, repr(word)
        assert snapshot(settings) == before


def test_color_scheme_words_are_light_and_dark_only(settings):
    for word in COLOR_SCHEMES:
        with pytest.raises(SettingsError):
            settings.set_color_scheme(word)
    settings.set_color_scheme("light")
    assert settings.backend.values[(INTERFACE, "color-scheme")] == "default"
    settings.set_color_scheme("dark")
    assert settings.backend.values[(INTERFACE, "color-scheme")] == "prefer-dark"


@pytest.mark.parametrize("event", [e + s for e in SOUND_EVENTS[:2] for s in (" ", "\0", "\n", "x")])
def test_unknown_sound_event_is_refused(event, settings):
    before = snapshot(settings)
    with pytest.raises(SettingsError):
        settings.set_sound(event, False)
    assert snapshot(settings) == before


def test_sound_events_keep_the_listed_order_and_never_repeat(settings):
    for event in reversed(SOUND_EVENTS):
        settings.set_sound(event, False)
    value = settings.get()["values"]["disabled_sounds"]
    assert value == list(SOUND_EVENTS)
    settings.set_sound("bell", False)
    assert settings.get()["values"]["disabled_sounds"] == list(SOUND_EVENTS)
    for event in SOUND_EVENTS:
        settings.set_sound(event, True)
    assert settings.get()["values"]["disabled_sounds"] == []


# -- the list of keys: nothing outside it, in any spelling -----------------------------------


NOT_ON_THE_LIST = [
    "lock_enabled",
    "lock-enabled",
    "idle_delay",
    "proxy",
    "mode",
    "autoconfig_url",
    "disable_user_extensions",
    "enabled_extensions",
    "disable-lock-screen",
    "org.gnome.desktop.screensaver",
    "org.gnome.system.proxy mode",
    "org.gnome.desktop.screensaver lock-enabled",
    "org.gnome.desktop.interface/color-scheme",
    "/org/gnome/desktop/screensaver/lock-enabled",
    "/",
    "..",
    "../color_scheme",
    "*",
    "**",
    "color_scheme.*",
    "__proto__",
    "__class__",
    "constructor",
    "KEYS",
    "SOUND",
    "schema",
    "key",
    "color-scheme",  # the GSettings name, not ours
    "night-light-enabled",
    "Color_scheme",
    "COLOR_SCHEME",
    " color_scheme",
    "color_scheme ",
    "color_scheme\0",
    "color_scheme\n",
    "color_scheme\u200b",
    "colоr_scheme",  # Cyrillic o
    "ｖｏｌｕｍｅ",
    "vol\u0075me\u0301",
    "\u202evolume",
    "volume\u202e",
    "'x'); rm -rf ~; ('",
    "volume'); rm -rf ~; ('",
    "volume=1",
    "volume;mute",
    "",
    "\ud800",
    "ключ",
    "音量",
    "🔊",
]


@pytest.mark.parametrize("name", NOT_ON_THE_LIST)
def test_restore_refuses_a_key_that_is_not_on_the_list(name, settings):
    before = snapshot(settings)
    with pytest.raises(SettingsError, match="unknown setting"):
        settings.restore({name: True})
    assert snapshot(settings) == before


@pytest.mark.parametrize("name", NOT_ON_THE_LIST)
def test_a_refused_key_stops_the_whole_restore(name, server, settings):
    """A good key next to a bad one is not written, in either order."""
    before = snapshot(settings)
    for values in ({"volume": 10, name: 1}, {name: 1, "volume": 10}):
        assert call(server, "restore", {"values": values})["isError"] is True
        assert snapshot(settings) == before


def test_unicode_spellings_of_a_real_name_are_not_the_real_name(settings):
    before = snapshot(settings)
    for form in ("NFC", "NFD", "NFKC", "NFKD"):
        for name in ("volume", "text_scale", "night_light_temperature"):
            spelled = unicodedata.normalize(form, name.replace("e", "e\u0301", 1))
            assert spelled != name
            with pytest.raises(SettingsError):
                settings.restore({spelled: 1})
    # NFKC of a fullwidth spelling is the real name; it still must not be accepted raw.
    with pytest.raises(SettingsError):
        settings.restore({"ｖｏｌｕｍｅ": 10})
    assert unicodedata.normalize("NFKC", "ｖｏｌｕｍｅ") == "volume"
    assert snapshot(settings) == before


def test_the_list_of_keys_holds_only_our_own_schemas():
    allowed = {INTERFACE, COLOR, NOTIFICATIONS, SOUND, DESKTOP_SOUND, SHELL}
    assert {k.schema for k in KEYS.values()} <= allowed
    assert len({(k.schema, k.key) for k in KEYS.values()}) == len(KEYS)
    for name, k in KEYS.items():
        assert re.fullmatch(r"[a-z][a-z_]*", name), name
        assert callable(k.check)
        assert k.allowed


def test_nothing_reaches_the_backend_except_listed_keys():
    """Fuzz every tool and ``restore``; record every key the backends see."""
    seen: set[tuple[str, str]] = set()

    class Recording(MemoryBackend):
        def get(self, schema, key):
            seen.add((schema, key))
            return super().get(schema, key)

        def set(self, schema, key, value):
            seen.add((schema, key))
            super().set(schema, key, value)

    class RecordingSound(MemorySound):
        def get(self, schema, key):
            seen.add((schema, key))
            return super().get(schema, key)

        def set(self, schema, key, value):
            seen.add((schema, key))
            super().set(schema, key, value)

    s = Settings(
        Recording(demo_backend().values),
        RecordingSound(50, False, "speakers", [{"name": "speakers", "description": "S"}]),
    )
    srv = Server(s)
    for tool in TOOLS:
        for arg in tool["inputSchema"]["properties"]:
            for value in JUNK.values():
                args = dict(VALID[tool["name"]])
                args[arg] = value
                srv.call(tool["name"], args)
    for name in [*NOT_ON_THE_LIST, *KEYS]:
        for value in JUNK.values():
            srv.call("restore", {"values": {name: value}})
    allowed = {(k.schema, k.key) for k in KEYS.values()}
    assert seen
    assert seen <= allowed, seen - allowed


# -- restore takes the same checks as the tools -----------------------------------------------


BOOL_KEYS = ["night_light", "night_light_automatic", "show_banners", "muted", "event_sounds"]


@pytest.mark.parametrize("name", BOOL_KEYS)
def test_restore_boolean_keys_take_only_booleans(name, settings):
    before = snapshot(settings)
    for junk, value in JUNK.items():
        if isinstance(value, bool):
            continue
        with pytest.raises(SettingsError):
            settings.restore({name: value})
        assert snapshot(settings) == before, junk
    for ok in (True, False):
        settings.restore({name: ok})


RESTORE_REFUSED = {
    "color_scheme": ["light", "dark", "Default", "default\0", "", 0, None, True, ["default"]],
    "accent_color": ["Blue", "magenta", "", 0, None, ["blue"], "blue\n"],
    "text_scale": [0.4999, 3.0001, 0, -1, NAN, INF, True, "1.0", None, [1.0]],
    "night_light_from": [-0.1, 24.1, NAN, INF, True, "20", None, [20]],
    "night_light_to": [-0.1, 24.1, NAN, -INF, False, "6", None, [6]],
    "night_light_temperature": [1699, 4701, 2700.5, 2700.0, True, "2700", None, -1, 2**32],
    "volume": [-1, 101, 50.0, True, "50", None, NAN, [50], 10**400],
    "output": ["", "x" * 257, "nowhere", None, 0, True, ["speakers"], "speakers\0", "Speakers"],
    "sound_theme": ["Wood", "wood ", "", 0, None, ["wood"], "wood\0"],
    "disabled_sounds": [
        "bell",
        None,
        0,
        {},
        ["bell", "bell"],
        ["nope"],
        ["Bell"],
        ["bell\0"],
        [None],
        [1],
        [True],
        "['bell']",
    ],
}


@pytest.mark.parametrize("name", list(RESTORE_REFUSED))
def test_restore_refuses_values_the_tool_would_refuse(name, settings):
    before = snapshot(settings)
    for value in RESTORE_REFUSED[name]:
        with pytest.raises((SettingsError, OverflowError, TypeError)):
            settings.restore({name: value})
        assert snapshot(settings) == before, (name, value)


def test_restore_covers_exactly_the_keys_get_reports(settings):
    values = settings.get()["values"]
    assert set(values) == set(KEYS)
    settings.restore(copy.deepcopy(values))
    assert settings.get()["values"] == values


def test_restore_of_get_is_idempotent_even_for_float_keys(settings):
    values = settings.get()["values"]
    out = settings.restore(copy.deepcopy(values))
    assert out["before"] == values
    assert settings.get()["values"] == values


@pytest.mark.parametrize("values", [None, [], [("volume", 10)], "volume", 0, True, {}])
def test_restore_wants_a_non_empty_object(values, settings):
    before = snapshot(settings)
    with pytest.raises(SettingsError):
        settings.restore(values)
    assert snapshot(settings) == before


def test_restore_keeps_working_with_nested_junk(server, settings):
    nested: object = 1
    for _ in range(100):
        nested = [nested]
    before = snapshot(settings)
    for key in ("volume", "text_scale", "disabled_sounds", "color_scheme"):
        assert call(server, "restore", {"values": {key: nested}})["isError"] is True
    assert snapshot(settings) == before


# -- strings with GVariant syntax and other text that tries to be code --------------------------


INJECTIONS = [
    "x'); rm -rf ~; ('",
    "'x'",
    "']; ['",
    "@s 'x'",
    "{'a': <'b'>}",
    "[('a', 1)]",
    "<'x'>",
    "%s%n",
    "${HOME}",
    "$(id)",
    "`id`",
    "\\'",
    "'\\x00'",
    "a\r\nb",
    "\x1b[2J",
]


def _hostile_outputs(names: list[str]) -> Settings:
    sinks = [{"name": n, "description": "d"} for n in names]
    return Settings(demo_backend(), MemorySound(50, False, names[0], sinks))


@pytest.mark.parametrize("text", INJECTIONS)
def test_injection_as_an_output_name_is_just_an_unknown_name(text, server, settings):
    before = snapshot(settings)
    out = call(server, "set_output", {"output": text})
    assert out["isError"] is True
    assert snapshot(settings) == before
    json.dumps(out)  # the answer is plain data


@pytest.mark.parametrize("text", INJECTIONS)
def test_injection_in_every_text_slot_changes_nothing(text, server, settings):
    before = snapshot(settings)
    for tool, args in (
        ("set_color_scheme", {"scheme": text}),
        ("set_accent_color", {"color": text}),
        ("set_sound_theme", {"theme": text}),
        ("set_sound", {"event": text, "enabled": True}),
        ("restore", {"values": {"disabled_sounds": [text]}}),
        ("restore", {"values": {"sound_theme": text}}),
        ("restore", {"values": {text: text}}),
    ):
        assert call(server, tool, args)["isError"] is True, (tool, text)
        assert snapshot(settings) == before


@pytest.mark.parametrize("text", INJECTIONS)
def test_a_device_with_a_hostile_name_is_handled_as_plain_text(text):
    """A sound output may carry any name; it is stored and undone exactly."""
    s = _hostile_outputs(["speakers", text])
    srv = Server(s)
    out = call(srv, "set_output", {"output": text})
    assert out["isError"] is False
    assert s.sound.values[(SOUND, "output")] == text
    assert out["structuredContent"]["undo_arguments"] == {"values": {"output": "speakers"}}
    got = call(srv, "get")["structuredContent"]
    assert got["values"]["output"] == text
    assert [o["name"] for o in got["outputs"]] == ["speakers", text]
    assert call(srv, "restore", out["structuredContent"]["undo_arguments"])["isError"] is False
    assert s.sound.values[(SOUND, "output")] == "speakers"


def test_output_name_length_is_counted_in_characters(settings):
    name = "ü" * 256
    s = _hostile_outputs(["speakers", name])
    s.set_output(name)
    assert s.sound.values[(SOUND, "output")] == name
    too_long = "ü" * 257
    s2 = _hostile_outputs(["speakers", too_long])
    with pytest.raises(SettingsError):
        s2.set_output(too_long)
    with pytest.raises(SettingsError):
        s2.set_output("")


def test_unicode_output_names_survive_the_json_round_trip():
    names = ["speakers", "Колонки", "スピーカー", "e\u0301", "\u00e9", "🔊", "\u202efoo"]
    s = _hostile_outputs(names)
    srv = Server(s)
    for n in names:
        assert call(srv, "set_output", {"output": n})["isError"] is False
        assert s.sound.values[(SOUND, "output")] == n
    out = io.StringIO()
    srv.serve(
        io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get", "arguments": {}},
                }
            )
            + "\n"
        ),
        out,
    )
    got = json.loads(out.getvalue())["result"]["structuredContent"]
    assert [o["name"] for o in got["outputs"]] == names


def test_nfc_and_nfd_output_names_are_different_outputs():
    nfc, nfd = "\u00e9", "e\u0301"
    s = _hostile_outputs(["speakers", nfc])
    with pytest.raises(SettingsError):
        s.set_output(nfd)
    assert s.sound.values[(SOUND, "output")] == "speakers"


# -- all or nothing across the two backends ---------------------------------------------------


ALL_KEYS = {
    "color_scheme": "prefer-dark",
    "accent_color": "red",
    "text_scale": 2.0,
    "night_light": True,
    "night_light_automatic": False,
    "night_light_from": 1.0,
    "night_light_to": 2.0,
    "night_light_temperature": 3000,
    "show_banners": False,
    "volume": 11,
    "muted": True,
    "output": "headphones",
    "event_sounds": False,
    "sound_theme": "glass",
    "disabled_sounds": ["bell", "complete"],
}


def test_all_keys_table_is_the_whole_list():
    assert set(ALL_KEYS) == set(KEYS)


def _failing(cls, fail_key: str, exc: Exception):
    class Failing(cls):
        def set(self, schema, key, value):
            if key == fail_key and value != self.values.get((schema, key)):
                raise exc
            super().set(schema, key, value)

    return Failing


@pytest.mark.parametrize("fail", [k.key for k in KEYS.values()])
@pytest.mark.parametrize("error", [SettingsError("refused"), RuntimeError("boom"), OSError("io")])
def test_a_write_that_fails_anywhere_leaves_nothing_changed(fail, error):
    backend_cls = _failing(MemoryBackend, fail, error)
    sound_cls = _failing(MemorySound, fail, error)
    outs = [{"name": "speakers", "description": "S"}, {"name": "headphones", "description": "H"}]
    s = Settings(backend_cls(demo_backend().values), sound_cls(50, False, "speakers", outs))
    srv = Server(s)
    before = snapshot(s)
    out = call(srv, "restore", {"values": ALL_KEYS})
    assert out["isError"] is True
    assert snapshot(s) == before
    assert call(srv, "get")["isError"] is False  # and the server keeps serving


def test_a_failed_rollback_does_not_hide_the_first_error():
    class Broken(MemoryBackend):
        def set(self, schema, key, value):
            if key == "night-light-schedule-to":
                raise SettingsError("refused first")
            if key == "night-light-enabled" and value is False:
                raise SettingsError("refused on the way back")
            super().set(schema, key, value)

    values = demo_backend().values
    values[(COLOR, "night-light-enabled")] = False
    s = Settings(Broken(values))
    srv = Server(s)
    out = call(srv, "restore", {"values": {"night_light": True, "night_light_to": 5}})
    assert out["isError"] is True
    assert "refused first" in out["content"][0]["text"]


def test_values_are_read_before_anything_is_written():
    log: list[str] = []

    class Logging(MemoryBackend):
        def get(self, schema, key):
            log.append("get")
            return super().get(schema, key)

        def set(self, schema, key, value):
            log.append("set")
            super().set(schema, key, value)

    s = Settings(Logging(demo_backend().values))
    s.restore({"color_scheme": "prefer-dark", "text_scale": 2.0, "show_banners": False})
    assert log == ["get"] * 3 + ["set"] * 3


def test_a_key_this_system_lacks_stops_the_change_before_any_write():
    values = demo_backend().values
    del values[(INTERFACE, "accent-color")]
    s = Settings(MemoryBackend(values))
    before = snapshot(s)
    with pytest.raises(SettingsError, match="not available"):
        s.restore({"color_scheme": "prefer-dark", "accent_color": "red"})
    assert snapshot(s) == before


def test_without_a_sound_server_sound_keys_change_nothing_else_either():
    s = Settings(demo_backend())
    before = snapshot(s)
    srv = Server(s)
    for name in ("set_volume", "set_mute", "set_output"):
        assert call(srv, name, VALID[name])["isError"] is True
    out = call(srv, "restore", {"values": {"color_scheme": "prefer-dark", "volume": 10}})
    assert out["isError"] is True
    assert snapshot(s) == before
    assert "outputs" not in call(srv, "get")["structuredContent"]


def test_backend_that_cannot_read_a_key_blocks_the_change():
    class Unreadable(MemoryBackend):
        def get(self, schema, key):
            if key == "text-scaling-factor":
                raise RuntimeError("dconf is gone")
            return super().get(schema, key)

    s = Settings(Unreadable(demo_backend().values))
    before = snapshot(s)
    out = call(Server(s), "restore", {"values": {"color_scheme": "prefer-dark", "text_scale": 2}})
    assert out["isError"] is True
    assert snapshot(s) == before


# -- the memory backends refuse what the system would ------------------------------------------


def test_memory_backend_does_not_create_keys():
    b = MemoryBackend({("a", "b"): 1})
    with pytest.raises(SettingsError):
        b.set("a", "c", 1)
    with pytest.raises(SettingsError):
        b.set("x", "b", 1)
    assert b.values == {("a", "b"): 1}
    assert b.get("a", "c") is None


def test_memory_sound_refuses_an_output_that_is_not_listed():
    s = MemorySound(10, False, "a", [{"name": "a", "description": "A"}])
    for name in ("b", "", "A", "a ", "a\0"):
        with pytest.raises(SettingsError):
            s.set(SOUND, "output", name)
    assert s.values[(SOUND, "output")] == "a"


# -- JSON-RPC: junk on the wire does not change anything or stop the server --------------------


def _wire(*lines: str):
    out = io.StringIO()
    return out, io.StringIO("\n".join(lines) + "\n")


def _rpc(name, arguments, id_=1, raw_arguments=None):
    args = raw_arguments if raw_arguments is not None else json.dumps(arguments)
    head = f'{{"jsonrpc":"2.0","id":{id_},"method":"tools/call"'
    return head + f',"params":{{"name":{json.dumps(name)},"arguments":{args}}}}}'


def _replies(srv: Server, *lines: str) -> list[dict]:
    out, src = _wire(*lines)
    srv.serve(src, out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


@pytest.mark.parametrize(
    "token",
    ["NaN", "Infinity", "-Infinity", "1e309", "-1e309", "1E999", "0.5e1000", "99" * 400],
)
def test_json_numbers_that_are_not_numbers_are_refused(token, server, settings):
    before = snapshot(settings)
    for tool, arg in (
        ("set_text_scale", "scale"),
        ("set_volume", "percent"),
        ("set_night_light", "from_hour"),
        ("set_night_light", "temperature"),
    ):
        replies = _replies(server, _rpc(tool, None, raw_arguments=f'{{"{arg}": {token}}}'))
        assert replies[0]["result"]["isError"] is True, (tool, token)
    assert snapshot(settings) == before


@pytest.mark.parametrize("token", ["-0.0", "0.0", "1.0", "1e0", "100.0", "5e1"])
def test_json_whole_numbers_written_as_floats_do_not_pass_for_integers(token, server, settings):
    """JSON text "50.0" is a float; ``volume`` and ``temperature`` want integers."""
    before = snapshot(settings)
    replies = _replies(server, _rpc("set_volume", None, raw_arguments=f'{{"percent": {token}}}'))
    assert replies[0]["result"]["isError"] is True
    assert snapshot(settings) == before


def test_duplicate_json_keys_last_one_wins_and_is_checked(server, settings):
    replies = _replies(
        server, _rpc("set_volume", None, raw_arguments='{"percent": 10, "percent": 500}')
    )
    assert replies[0]["result"]["isError"] is True
    assert settings.sound.values[(SOUND, "volume")] == 50
    replies = _replies(
        server, _rpc("set_volume", None, raw_arguments='{"percent": 500, "percent": 10}')
    )
    assert replies[0]["result"]["isError"] is False
    assert settings.sound.values[(SOUND, "volume")] == 10


def test_restore_with_the_same_key_twice_in_json_takes_the_last(server, settings):
    raw = '{"values": {"volume": 10, "volume": 500}}'
    assert _replies(server, _rpc("restore", None, raw_arguments=raw))[0]["result"]["isError"]
    assert settings.sound.values[(SOUND, "volume")] == 50


def test_a_notification_is_never_executed(server, settings):
    before = snapshot(settings)
    note = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "set_volume", "arguments": {"percent": 1}},
        }
    )
    assert _replies(server, note) == []
    assert snapshot(settings) == before


@pytest.mark.parametrize(
    "message",
    [
        {},
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"jsonrpc": 2.0, "id": 1, "method": "ping"},
        {"id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "method": None},
        {"jsonrpc": "2.0", "id": 1, "method": 5},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "x"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": None}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 5}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": True}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "__init__"}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "Get"}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get "}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get\0"}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "set_vоlume"}},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "set_volume", "arguments": None},
        },
        {"jsonrpc": "2.0", "id": None, "method": "tools/call", "params": {"name": "x"}},
        {"jsonrpc": "2.0", "id": "a", "method": "nope"},
    ],
)
def test_malformed_requests_get_an_answer_and_change_nothing(message, server, settings):
    before = snapshot(settings)
    replies = _replies(
        server, json.dumps(message), json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})
    )
    assert replies[-1] == {"jsonrpc": "2.0", "id": 9, "result": {}}
    assert snapshot(settings) == before


@pytest.mark.parametrize(
    "line",
    [
        "{",
        "}",
        "[",
        "null",
        "true",
        "5",
        '"x"',
        "[]",
        "[1,2]",
        "{}",
        "\x00",
        "{'a': 1}",
        '{"jsonrpc":"2.0","id":1,"method":"ping"} trailing',
        '{"a":' * 50,
    ],
)
def test_garbage_lines_do_not_stop_the_server(line, server):
    replies = _replies(server, line, json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}))
    assert replies[-1]["id"] == 2


def test_a_long_run_of_wrong_calls_leaves_the_server_exactly_as_it_was(server, settings):
    before = snapshot(settings)
    lines = []
    for i, param in enumerate(WRONG):
        name, arg, value = param.values
        if isinstance(value, str) and "\ud800" in value:
            continue  # a lone surrogate has no JSON text form that survives ensure_ascii off
        args = dict(VALID[name])
        args[arg] = value
        lines.append(_rpc(name, args, id_=i))
    replies = _replies(server, *lines)
    assert len(replies) == len(lines)
    assert all(r["result"]["isError"] is True for r in replies)
    assert snapshot(settings) == before


def test_arguments_of_a_megabyte_are_refused_without_a_crash(server, settings):
    before = snapshot(settings)
    big = "x" * 1_000_000
    assert call(server, "set_output", {"output": big})["isError"] is True
    assert call(server, "set_color_scheme", {"scheme": big})["isError"] is True
    assert call(server, "restore", {"values": {"disabled_sounds": [big]}})["isError"] is True
    assert call(server, "restore", {"values": {"output": big}})["isError"] is True
    assert call(server, "set_sound", {"event": big, "enabled": True})["isError"] is True
    assert snapshot(settings) == before


def test_many_keys_in_restore_are_refused_in_reasonable_time(server, settings):
    before = snapshot(settings)
    values = {f"k{i}": i for i in range(50_000)}
    assert call(server, "restore", {"values": values})["isError"] is True
    assert snapshot(settings) == before


def _wrong_kind(prop: dict, v) -> bool:
    """Whether the value is of the wrong JSON type, or a word outside the enum."""
    if isinstance(v, float) and not math.isfinite(v):
        return False  # a number as far as the JSON type goes; the value check refuses it
    bounded = {k: x for k, x in prop.items() if k not in ("minimum", "maximum")}
    return not _legit(bounded, v)


KIND_CASES = [
    pytest.param(name, arg, value, id=f"{name}-{arg}-{i}")
    for name, arg in PROPS
    for i, value in enumerate(JUNK.values())
    if _wrong_kind(tool_schema(name)["properties"][arg], value)
]


@pytest.mark.parametrize(("name", "arg", "value"), KIND_CASES)
def test_the_argument_check_alone_refuses_a_wrong_kind(name, arg, value):
    """The check in front of the handlers must hold even if a handler checked nothing."""
    args = dict(VALID[name])
    args[arg] = value
    with pytest.raises(SettingsError):
        check_arguments(name, args)


@pytest.mark.parametrize("value", [1, 0, "yes", "true", None, [], 1.0])
def test_do_not_disturb_called_directly_wants_a_real_boolean(value, settings):
    before = snapshot(settings)
    with pytest.raises(SettingsError):
        settings.set_do_not_disturb(value)
    with pytest.raises(SettingsError):
        settings.set_sound("bell", value)
    assert snapshot(settings) == before


def test_check_arguments_accepts_nothing_unknown():
    with pytest.raises(SettingsError):
        check_arguments("set_volume", {"percent": 5, "extra": 1})
    with pytest.raises(SettingsError):
        check_arguments("set_volume", {})
    assert check_arguments("set_volume", {"percent": 5}) == {"percent": 5}


def test_the_module_in_a_subprocess_survives_junk_and_answers_in_utf8():
    lines = [
        "not json",
        _rpc("set_volume", None, raw_arguments='{"percent": NaN}'),
        _rpc("set_output", {"output": "音量🔊"}, id_=2),
        _rpc("set_volume", {"percent": 7}, id_=3),
        _rpc("get", {}, id_=4),
    ]
    p = subprocess.run(
        [sys.executable, "-m", "vilna_mcp_settings", "--memory"],
        input=("\n".join(lines) + "\n").encode(),
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert p.returncode == 0, p.stderr
    replies = [json.loads(line) for line in p.stdout.decode("utf-8").splitlines()]
    assert len(replies) == 5
    assert replies[0]["error"]["code"] == -32700
    assert replies[1]["result"]["isError"] is True
    assert replies[2]["result"]["isError"] is True
    assert replies[3]["result"]["isError"] is False
    assert replies[4]["result"]["structuredContent"]["values"]["volume"] == 7


# -- what goes without asking and what goes through a risk level -------------------------------


@pytest.fixture(scope="module")
def manifest() -> dict:
    return tomllib.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_lists_exactly_the_tools_of_the_server(manifest):
    assert set(manifest["operations"]) == {t["name"] for t in TOOLS}


def test_only_reading_is_level_r0(manifest):
    ops = manifest["operations"]
    for tool in TOOLS:
        read_only = tool["annotations"].get("readOnlyHint") is True
        level = ops[tool["name"]]["level"]
        assert re.fullmatch(r"R[0-9]", level), (tool["name"], level)
        assert (level == "R0") == read_only, tool["name"]
    assert [t["name"] for t in TOOLS if t["annotations"].get("readOnlyHint")] == ["get"]


def test_every_change_is_a_reversible_level_and_says_it_is_undoable(manifest):
    for name in WRITERS:
        op = manifest["operations"][name]
        assert op["level"] not in {"R0", "R1"}, name
        assert op.get("undoable") is True, name


def test_restore_is_not_a_lower_level_than_any_change_it_can_make(manifest):
    ops = manifest["operations"]
    top = max(int(ops[n]["level"][1:]) for n in WRITERS)
    assert int(ops["restore"]["level"][1:]) >= top


def test_no_tool_is_marked_destructive_and_all_changes_are_idempotent():
    for tool in TOOLS:
        a = tool["annotations"]
        assert a.get("destructiveHint") in (None, False), tool["name"]
        if tool["name"] != "get":
            assert a["readOnlyHint"] is False
            assert a["idempotentHint"] is True


def test_manifest_launches_the_program_this_package_installs(manifest):
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert manifest["launch"]["program"] in pyproject["project"]["scripts"]
    assert manifest["launch"]["module"] == "vilna_mcp_settings"
    assert manifest["tool"] == "org.vilna.mcp.settings"


def test_every_change_is_applied_the_same_when_repeated(server, settings):
    """Idempotent, as the annotations say: doing it twice equals doing it once."""
    for name in WRITERS:
        if name == "restore":
            continue
        call(server, name, VALID[name])
        once = snapshot(settings)
        assert call(server, name, VALID[name])["isError"] is False
        assert snapshot(settings) == once, name


# -- undo -------------------------------------------------------------------------------------


def test_a_chain_of_changes_is_undone_in_reverse_order(server, settings):
    start = snapshot(settings)
    undo = []
    for name in WRITERS:
        if name == "restore":
            continue
        out = call(server, name, VALID[name])
        assert out["isError"] is False
        undo.append(out["structuredContent"]["undo_arguments"])
    assert snapshot(settings) != start
    for args in reversed(undo):
        assert call(server, "restore", args)["isError"] is False
    assert snapshot(settings) == start


def test_undo_arguments_are_a_copy_not_the_live_state(settings):
    out = settings.set_sound("bell", False)
    saved = copy.deepcopy(out["undo_arguments"])
    settings.set_sound("complete", False)
    assert out["undo_arguments"] == saved


def test_undo_arguments_only_name_keys_on_the_list(settings):
    for name in WRITERS:
        if name == "restore":
            continue
        out = getattr(settings, name)(**VALID[name])
        assert set(out["undo_arguments"]["values"]) <= set(KEYS)
        assert set(out["undo_arguments"]["values"]) == set(out["before"]) == set(out["changed"])


# -- sound through a fake PulseAudio -----------------------------------------------------------


class _Volume:
    def __init__(self, flat: float) -> None:
        self.value_flat = flat


class _Sink:
    def __init__(self, name: str, flat: float = 0.5, mute: int = 0) -> None:
        self.name = name
        self.description = name.title()
        self.volume = _Volume(flat)
        self.mute = mute


def _fake_pulse(sinks, default=None, fail=None):
    import pulsectl

    calls: list[tuple] = []

    class FakePulse:
        def __init__(self, name):
            if fail == "connect":
                raise pulsectl.PulseError("no server")

        def server_info(self):
            class Info:
                default_sink_name = default if default is not None else sinks[0].name

            return Info()

        def get_sink_by_name(self, name):
            for s in sinks:
                if s.name == name:
                    return s
            raise pulsectl.PulseIndexError("no such sink")

        def sink_list(self):
            return list(sinks)

        def sink_default_set(self, sink):
            calls.append(("default", sink.name))

        def volume_set_all_chans(self, sink, value):
            if fail == "refuse":
                raise pulsectl.PulseOperationFailed(1)
            calls.append(("volume", sink.name, value))
            sink.volume.value_flat = value

        def mute(self, sink, flag):
            calls.append(("mute", sink.name, flag))
            sink.mute = int(flag)

    return FakePulse, calls


@pytest.fixture
def pulse(monkeypatch):
    pulsectl = pytest.importorskip("pulsectl")

    def make(sinks, **kw):
        fake, calls = _fake_pulse(sinks, **kw)
        monkeypatch.setattr(pulsectl, "Pulse", fake)
        from vilna_mcp_settings.sound import PulseSound

        return PulseSound, calls

    return make


def test_pulse_volume_edges_and_nothing_beyond(pulse):
    cls, calls = pulse([_Sink("a")])
    s = Settings(demo_backend(), cls())
    s.set_volume(0)
    s.set_volume(100)
    assert [c[2] for c in calls if c[0] == "volume"] == [0.0, 1.0]
    for bad in (-1, 101, True, 50.0, NAN):
        with pytest.raises((SettingsError, OverflowError)):
            s.set_volume(bad)
    assert [c[2] for c in calls if c[0] == "volume"] == [0.0, 1.0]  # the sound server saw no more


def test_pulse_output_must_be_a_real_sink(pulse):
    cls, calls = pulse([_Sink("a"), _Sink("b")])
    s = Settings(demo_backend(), cls())
    for bad in ("c", "", "A", "a\0", "a ", "x'); rm -rf ~; ('"):
        with pytest.raises(SettingsError):
            s.set_output(bad)
    assert not [c for c in calls if c[0] == "default"]
    s.set_output("b")
    assert calls == [("default", "b")]


def test_pulse_without_a_server_says_so(pulse):
    cls, _ = pulse([_Sink("a")], fail="connect")
    with pytest.raises(SettingsError, match="no sound server"):
        cls()


def test_pulse_with_no_default_output_is_not_available(pulse):
    cls, calls = pulse([_Sink("a")], default="ghost")
    s = Settings(demo_backend(), cls())
    assert "volume" not in s.get()["values"]
    with pytest.raises(SettingsError, match="not available"):
        s.set_volume(10)
    assert not calls


def test_pulse_refusal_becomes_a_settings_error_and_rolls_back(pulse):
    cls, calls = pulse([_Sink("a", mute=0)], fail="refuse")
    s = Settings(demo_backend(), cls())
    srv = Server(s)
    out = call(srv, "restore", {"values": {"muted": True, "volume": 10}})
    assert out["isError"] is True
    assert "the sound server refused" in out["content"][0]["text"]
    assert s.sound.get(SOUND, "muted") is False  # the mute that went first was put back


def test_pulse_lists_a_bounded_number_of_outputs(pulse):
    cls, _ = pulse([_Sink(f"sink-{i}") for i in range(500)])
    s = Settings(demo_backend(), cls())
    assert len(s.get()["outputs"]) == 32


def test_pulse_unknown_sound_key_is_refused(pulse):
    cls, calls = pulse([_Sink("a")])
    sound = cls()
    with pytest.raises(SettingsError):
        sound.set(SOUND, "balance", 1)
    assert sound.get(SOUND, "balance") is None
    assert not calls


# -- the real GSettings backend (memory GSettings, no dconf) ------------------------------------


SCHEMA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<schemalist>
  <schema id="org.gnome.desktop.interface" path="/org/gnome/desktop/interface/">
    <key name="color-scheme" type="s">
      <choices><choice value="default"/><choice value="prefer-dark"/>
        <choice value="prefer-light"/></choices>
      <default>'default'</default>
    </key>
    <key name="accent-color" type="s">
      <choices><choice value="blue"/><choice value="teal"/></choices>
      <default>'blue'</default>
    </key>
    <key name="text-scaling-factor" type="d">
      <range min="0.5" max="2.0"/><default>1.0</default>
    </key>
  </schema>
  <schema id="org.gnome.settings-daemon.plugins.color"
          path="/org/gnome/settings-daemon/plugins/color/">
    <key name="night-light-enabled" type="b"><default>false</default></key>
    <key name="night-light-temperature" type="u">
      <range min="2000" max="4000"/><default>2700</default>
    </key>
    <key name="night-light-schedule-automatic" type="b"><default>true</default></key>
    <key name="night-light-schedule-from" type="d"><default>20.0</default></key>
    <key name="night-light-schedule-to" type="d"><default>6.0</default></key>
  </schema>
  <schema id="org.gnome.desktop.notifications" path="/org/gnome/desktop/notifications/">
    <key name="show-banners" type="b"><default>true</default></key>
  </schema>
  <schema id="org.gnome.desktop.sound" path="/org/gnome/desktop/sound/">
    <key name="event-sounds" type="b"><default>true</default></key>
  </schema>
  <schema id="org.gnome.shell.extensions.vilna" path="/org/gnome/shell/extensions/vilna/">
    <key name="sound-theme" type="s"><default>'wood'</default></key>
    <key name="disabled-sounds" type="as"><default>[]</default></key>
  </schema>
  <schema id="org.gnome.desktop.screensaver" path="/org/gnome/desktop/screensaver/">
    <key name="lock-enabled" type="b"><default>true</default></key>
  </schema>
</schemalist>
"""


def _compiler() -> str | None:
    found = shutil.which("glib-compile-schemas")
    if found:
        return found
    for pattern in (
        "/usr/lib/*/glib-2.0/glib-compile-schemas",
        "/usr/libexec/glib-compile-schemas",
    ):
        hits = glob.glob(pattern)
        if hits:
            return hits[0]
    return None


@pytest.fixture
def real(tmp_path, monkeypatch):
    pytest.importorskip("gi")
    compiler = _compiler()
    if compiler is None:
        pytest.skip("no glib-compile-schemas on this machine")
    (tmp_path / "test.gschema.xml").write_text(SCHEMA_XML, encoding="utf-8")
    subprocess.run([compiler, "--strict", str(tmp_path)], check=True)
    monkeypatch.setenv("VILNA_MCP_SETTINGS_SCHEMA_DIR", str(tmp_path))
    from gi.repository import Gio

    default = Gio.SettingsBackend.get_default()
    assert "memory" in type(default).__name__.lower(), "refusing to touch the person's dconf"
    from vilna_mcp_settings.gsettings import GSettingsBackend

    backend = GSettingsBackend()
    _reset(backend)  # the memory backend lives as long as the process
    yield backend
    _reset(backend)


def _reset(backend) -> None:
    for schema_id in (
        INTERFACE,
        COLOR,
        NOTIFICATIONS,
        DESKTOP_SOUND,
        SHELL,
        "org.gnome.desktop.screensaver",
    ):
        schema = backend._source.lookup(schema_id, True)
        settings = backend._lookup(schema_id, schema.list_keys()[0])[0]
        for key in schema.list_keys():
            settings.reset(key)


def _screensaver(backend) -> bool:
    return backend.get("org.gnome.desktop.screensaver", "lock-enabled")


def test_real_unknown_schema_and_key_are_not_available(real):
    assert real.get("no.such.schema", "x") is None
    assert real.get(INTERFACE, "no-such-key") is None
    assert real.get(INTERFACE, "колір") is None
    assert real.get("", "") is None
    with pytest.raises(SettingsError, match="not available"):
        real.set("no.such.schema", "x", 1)
    with pytest.raises(SettingsError, match="not available"):
        real.set(INTERFACE, "no-such-key", "x")


def test_real_tools_cannot_reach_a_key_that_exists_but_is_not_ours(real):
    """The screensaver schema is compiled in, so GSettings could write it; we must not."""
    assert _screensaver(real) is True
    s = Settings(real)
    srv = Server(s)
    for name in ("lock_enabled", "lock-enabled", "org.gnome.desktop.screensaver lock-enabled"):
        assert call(srv, "restore", {"values": {name: False}})["isError"] is True
    for tool in TOOLS:
        for arg in tool["inputSchema"]["properties"]:
            for value in JUNK.values():
                args = dict(VALID[tool["name"]])
                args[arg] = value
                srv.call(tool["name"], args)
    assert _screensaver(real) is True


def test_real_system_range_narrower_than_ours_is_refused_and_rolled_back(real):
    s = Settings(real)
    srv = Server(s)
    before = s.get()["values"]
    # 1800 K is inside our range (1700 to 4700) but outside this system's (2000 to 4000).
    out = call(srv, "set_night_light", {"enabled": True, "temperature": 1800})
    assert out["isError"] is True
    assert "out of range on this system" in out["content"][0]["text"]
    assert s.get()["values"] == before
    out = call(srv, "set_text_scale", {"scale": 2.5})  # ours up to 3.0, this system up to 2.0
    assert out["isError"] is True
    assert s.get()["values"] == before


def test_real_enum_key_refuses_a_word_it_does_not_list(real):
    s = Settings(real)
    before = s.get()["values"]
    with pytest.raises(SettingsError, match="out of range on this system"):
        s.set_accent_color("purple")  # ours, but not in this system's choices
    assert s.get()["values"] == before


def test_real_valid_change_round_trips_with_exact_types(real):
    s = Settings(real)
    out = s.set_night_light(enabled=True, from_hour=22, to_hour=7, temperature=3000)
    values = s.get()["values"]
    assert values["night_light_from"] == 22.0 and isinstance(values["night_light_from"], float)
    assert values["night_light_temperature"] == 3000
    s.restore(**out["undo_arguments"])
    assert s.get()["values"]["night_light"] is False
    assert s.get()["values"]["night_light_temperature"] == 2700
    s.set_sound("bell", False)
    assert s.get()["values"]["disabled_sounds"] == ["bell"]


def _all_real_values(real) -> dict[tuple[str, str], object]:
    pairs = {(k.schema, k.key) for k in KEYS.values() if k.schema != SOUND}
    return {pair: real.get(*pair) for pair in pairs}


@pytest.mark.parametrize("text", INJECTIONS)
def test_real_text_is_stored_as_text_and_touches_no_other_key(real, text):
    """A GVariant-looking string is data: it comes back as typed and nothing else moves."""
    before = _all_real_values(real)
    real.set(SHELL, "sound-theme", text)
    assert real.get(SHELL, "sound-theme") == text
    after = _all_real_values(real)
    before.pop((SHELL, "sound-theme"))
    after.pop((SHELL, "sound-theme"))
    assert after == before
    assert _screensaver(real) is True


def test_real_wrong_kinds_do_not_get_through_the_backend_quietly(real):
    for value in (-1, 2**40, 1.5, True, "2700", None, [2700]):
        with pytest.raises((SettingsError, OverflowError, TypeError)):
            real.set(COLOR, "night-light-temperature", value)
        assert real.get(COLOR, "night-light-temperature") == 2700, value


def test_real_extra_schema_directory_that_is_not_usable_is_ignored(tmp_path, monkeypatch):
    pytest.importorskip("gi")
    from gi.repository import Gio

    assert "memory" in type(Gio.SettingsBackend.get_default()).__name__.lower()
    from vilna_mcp_settings.gsettings import GSettingsBackend

    (tmp_path / "junk.gschema.xml").write_text("<not-compiled/>")
    for target in (str(tmp_path), str(tmp_path / "missing"), "", "/dev/null"):
        monkeypatch.setenv("VILNA_MCP_SETTINGS_SCHEMA_DIR", target)
        assert GSettingsBackend().get(INTERFACE, "no-such-key") is None
