# SPDX-License-Identifier: Apache-2.0
"""MCP server over stdio (newline-delimited JSON-RPC 2.0).

    vilna-mcp-settings [--memory]

Started by Herkos, never by Gloxi directly (ADR 0023): Herkos decides whether
a change may happen, this server only makes it, for its own list of keys.
``--memory`` keeps the settings in memory instead of GSettings, for tests
and demos.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any, TextIO

from vilna_mcp_settings import __version__
from vilna_mcp_settings.settings import (
    ACCENT_COLORS,
    COLOR,
    INTERFACE,
    NOTIFICATIONS,
    TEMPERATURE,
    MemoryBackend,
    Settings,
    SettingsError,
)

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

_BOOL = {"type": "boolean"}
_HOUR = {"type": "number", "minimum": 0, "maximum": 24, "description": "Hour, e.g. 22.5 = 22:30"}


def _tool(name, description, props, required, **annotations):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": annotations,
    }


_CHANGE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}

TOOLS = [
    _tool(
        "get",
        "Current appearance, night light and notification settings",
        {},
        [],
        readOnlyHint=True,
    ),
    _tool(
        "set_color_scheme",
        "Switch between the light and the dark style",
        {"scheme": {"type": "string", "enum": ["light", "dark"]}},
        ["scheme"],
        **_CHANGE,
    ),
    _tool(
        "set_accent_color",
        "Change the accent colour (GNOME 47 and later)",
        {"color": {"type": "string", "enum": list(ACCENT_COLORS)}},
        ["color"],
        **_CHANGE,
    ),
    _tool(
        "set_text_scale",
        "Make text larger or smaller (1.0 is normal)",
        {"scale": {"type": "number", "minimum": 0.5, "maximum": 3.0}},
        ["scale"],
        **_CHANGE,
    ),
    _tool(
        "set_night_light",
        "Turn night light on or off, set when it runs and how warm it is",
        {
            "enabled": _BOOL,
            "automatic": {"type": "boolean", "description": "From sunset to sunrise"},
            "from_hour": _HOUR,
            "to_hour": _HOUR,
            "temperature": {
                "type": "integer",
                "minimum": TEMPERATURE[0],
                "maximum": TEMPERATURE[1],
                "description": "Kelvin; lower is warmer",
            },
        },
        [],
        **_CHANGE,
    ),
    _tool(
        "set_do_not_disturb",
        "Turn Do Not Disturb (no notification banners) on or off",
        {"enabled": _BOOL},
        ["enabled"],
        **_CHANGE,
    ),
    _tool(
        "restore",
        "Undo a change: put settings back as they were before",
        {"values": {"type": "object"}},
        ["values"],
        **_CHANGE,
    ),
]
_SCHEMAS = {t["name"]: t["inputSchema"] for t in TOOLS}
_TYPES: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
}


def check_arguments(name: str, args: Any) -> dict:
    schema = _SCHEMAS[name]
    if not isinstance(args, dict):
        raise SettingsError("arguments must be an object")
    unknown = set(args) - set(schema["properties"])
    if unknown:
        raise SettingsError(f"unknown arguments: {sorted(unknown)}")
    missing = [k for k in schema["required"] if k not in args]
    if missing:
        raise SettingsError(f"missing arguments: {missing}")
    for k, v in args.items():
        prop = schema["properties"][k]
        if not _TYPES[prop["type"]](v):
            raise SettingsError(f"'{k}' must be a {prop['type']}")
        if "enum" in prop and v not in prop["enum"]:
            raise SettingsError(f"'{k}' must be one of {prop['enum']}")
    return args


class Server:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._handlers: dict[str, Callable[..., dict]] = {
            "get": settings.get,
            "set_color_scheme": settings.set_color_scheme,
            "set_accent_color": settings.set_accent_color,
            "set_text_scale": settings.set_text_scale,
            "set_night_light": settings.set_night_light,
            "set_do_not_disturb": settings.set_do_not_disturb,
            "restore": settings.restore,
        }

    def handle(self, msg: Any) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, -32600, "not a JSON-RPC 2.0 message")
        method, id_ = msg.get("method"), msg.get("id")
        if "id" not in msg:
            return None
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return _error(id_, -32602, "params must be an object")
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(
                id_,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "vilna-mcp-settings", "version": __version__},
                },
            )
        if method == "ping":
            return _result(id_, {})
        if method == "tools/list":
            return _result(id_, {"tools": TOOLS})
        if method == "tools/call":
            return _result(id_, self.call(params.get("name"), params.get("arguments", {})))
        return _error(id_, -32601, f"unknown method {method}")

    def call(self, name: Any, args: Any) -> dict:
        if name not in self._handlers:
            return _tool_error(f"unknown tool {name}")
        try:
            out = self._handlers[name](**check_arguments(name, args))
        except SettingsError as e:
            return _tool_error(str(e))
        except Exception as e:  # GLib errors and the like: report, keep serving
            return _tool_error(f"{type(e).__name__}: {e}")
        return {
            "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out,
            "isError": False,
        }

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                response = _error(None, -32700, "not valid JSON")
            else:
                response = self.handle(msg)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()


def _result(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def demo_backend() -> MemoryBackend:
    """A GNOME 47 desktop as Vilna ships it: light, blue, night light off."""
    return MemoryBackend(
        {
            (INTERFACE, "color-scheme"): "default",
            (INTERFACE, "accent-color"): "blue",
            (INTERFACE, "text-scaling-factor"): 1.0,
            (COLOR, "night-light-enabled"): False,
            (COLOR, "night-light-schedule-automatic"): True,
            (COLOR, "night-light-schedule-from"): 20.0,
            (COLOR, "night-light-schedule-to"): 6.0,
            (COLOR, "night-light-temperature"): 2700,
            (NOTIFICATIONS, "show-banners"): True,
        }
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vilna-mcp-settings")
    p.add_argument("--memory", action="store_true", help="keep settings in memory (tests, demos)")
    args = p.parse_args(argv)
    if args.memory:
        backend = demo_backend()
    else:
        try:
            from vilna_mcp_settings.gsettings import GSettingsBackend
        except (ImportError, ValueError) as e:
            print(f"vilna-mcp-settings: GSettings is not available ({e})", file=sys.stderr)
            return 2
        backend = GSettingsBackend()
    Server(Settings(backend)).serve(sys.stdin, sys.stdout)
    return 0
