# SPDX-License-Identifier: Apache-2.0
"""MCP server over stdio (newline-delimited JSON-RPC 2.0).

    vilna-mcp-apps [--memory]

Started by the system Herkos, never by Gloxi directly (ADR 0012): Herkos
decides whether an operation may run and asks the person before every
install, update or removal; this server only does it. ``--memory`` uses a
small catalogue in memory instead of Flatpak, for tests and demos.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any, TextIO

from vilna_mcp_apps import __version__
from vilna_mcp_apps.apps import (
    ALLOWED_REMOTES,
    MAX_QUERY_CHARS,
    MAX_RESULTS,
    Apps,
    AppsError,
    Installed,
    MemoryBackend,
)
from vilna_mcp_apps.appstream import Component, read_picks

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

_APP = {"type": "string", "description": "Flatpak app id, e.g. org.gnome.Calculator"}
_REMOTE = {
    "type": "string",
    "enum": list(ALLOWED_REMOTES),
    "description": "Where the app comes from; left out, the tool finds it",
}
_LANGUAGE = {"type": "string", "description": "Language of names and summaries, e.g. uk"}


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


_READ = {"readOnlyHint": True, "openWorldHint": True}
_CHANGE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}

TOOLS = [
    _tool(
        "search",
        "Find apps on Flathub and in Vilna's repository. Apps from «Recommended by "
        "Vilna» come first (field pick); suggest those before any other.",
        {
            "query": {"type": "string", "description": "What the app is for, or its name"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS},
            "language": _LANGUAGE,
        },
        ["query"],
        **_READ,
    ),
    _tool(
        "describe",
        "Developer, verification, licence, download size and permissions of one app",
        {"app_id": _APP, "remote": _REMOTE, "language": _LANGUAGE},
        ["app_id"],
        **_READ,
    ),
    _tool(
        "list_installed",
        "Apps installed on this computer",
        {"language": _LANGUAGE},
        [],
        readOnlyHint=True,
    ),
    _tool(
        "install",
        "Install an app for everybody on this computer; the person confirms it",
        {"app_id": _APP, "remote": _REMOTE},
        ["app_id"],
        **_CHANGE,
    ),
    _tool(
        "update",
        "Update one installed app, or all of them when app_id is left out",
        {"app_id": _APP},
        [],
        **_CHANGE,
    ),
    _tool(
        "uninstall",
        "Remove an app; its data in ~/.var/app stays",
        {"app_id": _APP},
        ["app_id"],
        **_CHANGE,
    ),
    _tool(
        "restore",
        "Undo an install or a removal (the undo_arguments of that change)",
        {
            "action": {"type": "string", "enum": ["install", "uninstall"]},
            "app_id": _APP,
            "remote": _REMOTE,
        },
        ["action", "app_id"],
        **_CHANGE,
    ),
]
_SCHEMAS = {t["name"]: t["inputSchema"] for t in TOOLS}
_TYPES: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
}


def check_arguments(name: str, args: Any) -> dict:
    schema = _SCHEMAS[name]
    if not isinstance(args, dict):
        raise AppsError("arguments must be an object")
    unknown = set(args) - set(schema["properties"])
    if unknown:
        raise AppsError(
            f"unknown arguments: {sorted(unknown)}; this tool cannot add app sources "
            "or install from files"
        )
    missing = [k for k in schema["required"] if k not in args]
    if missing:
        raise AppsError(f"missing arguments: {missing}")
    for k, v in args.items():
        prop = schema["properties"][k]
        if not _TYPES[prop["type"]](v):
            raise AppsError(f"'{k}' must be a {prop['type']}")
        if "enum" in prop and v not in prop["enum"]:
            raise AppsError(f"'{k}' must be one of {prop['enum']}")
        if prop["type"] == "string" and len(v) > MAX_QUERY_CHARS + 55:
            raise AppsError(f"'{k}' is too long")
    return args


class Server:
    def __init__(self, apps: Apps) -> None:
        self.apps = apps
        self._handlers: dict[str, Callable[..., dict]] = {
            "search": apps.search,
            "describe": apps.describe,
            "list_installed": apps.list_installed,
            "install": apps.install,
            "update": apps.update,
            "uninstall": apps.uninstall,
            "restore": apps.restore,
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
                    "serverInfo": {"name": "vilna-mcp-apps", "version": __version__},
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
        except AppsError as e:
            return _tool_error(str(e))
        except Exception as e:  # Flatpak errors and the like: report, keep serving
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


def _result(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def demo_apps() -> Apps:
    """Flathub with three apps, two of them «Рекомендовано Vilna»."""

    def app(id_, name, summary, dev, lic, verified=True, keywords=()):
        custom = {"flathub::verification::verified": "true"} if verified else {}
        return Component(
            id=id_,
            names={"": name},
            summaries={"": summary},
            developer=dev,
            license=lic,
            keywords=list(keywords),
            custom=custom,
        )

    flathub = [
        app(
            "org.kde.kdenlive",
            "Kdenlive",
            "Video editor",
            "KDE",
            "GPL-3.0-or-later",
            keywords=("video", "edit"),
        ),
        app(
            "org.shotcut.Shotcut",
            "Shotcut",
            "Video editor",
            "Meltytech",
            "GPL-3.0-or-later",
            keywords=("video",),
        ),
        app(
            "org.example.Unchecked",
            "Unchecked",
            "Notes and video",
            "Somebody",
            "MIT",
            verified=False,
            keywords=("notes", "video"),
        ),
    ]
    backend = MemoryBackend(
        {"flathub": flathub, "vilna": []},
        installed=[Installed("org.mozilla.firefox", "flathub", "f1", "133.0", "Firefox")],
    )
    return Apps(backend, picks={"org.kde.kdenlive": "media", "org.mozilla.firefox": "work"})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vilna-mcp-apps")
    p.add_argument("--memory", action="store_true", help="a small catalogue in memory (tests)")
    args = p.parse_args(argv)
    if args.memory:
        apps = demo_apps()
    else:
        try:
            from vilna_mcp_apps.flatpak import FlatpakBackend

            backend = FlatpakBackend()
        except (ImportError, ValueError) as e:
            print(f"vilna-mcp-apps: libflatpak is not available ({e})", file=sys.stderr)
            return 2
        apps = Apps(backend, picks=read_picks())
    Server(apps).serve(sys.stdin, sys.stdout)
    return 0
