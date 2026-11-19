# SPDX-License-Identifier: Apache-2.0
"""MCP server "launcher": opens an installed Flatpak app for the person.

    vilna-mcp-launcher [--memory]

Opening an app is R1 (ADR 0012, section 4). It runs as the person
(vilna-mcp-launcher@.service), not as ``vilna-apps``: the app must open in
the person's own session. It asks the person's systemd user manager to start
``flatpak run <app>`` as its own unit, so the app runs in its own Flatpak
sandbox and not inside this tool's systemd sandbox. Only installed apps, by
their Flatpak id; no arguments, files or commands are passed to the app.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from vilna_mcp_apps import __version__
from vilna_mcp_apps.apps import AppsError
from vilna_mcp_apps.appstream import is_app_id

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SYSTEM_APPS = Path("/var/lib/flatpak/app")

TOOLS = [
    {
        "name": "launch",
        "description": "Open an installed app (an app_id from list_installed of the apps tool)",
        "inputSchema": {
            "type": "object",
            "properties": {"app_id": {"type": "string"}},
            "required": ["app_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    }
]


def installed_dirs() -> list[Path]:
    home = Path(os.environ.get("HOME", "/nonexistent"))
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    return [SYSTEM_APPS, data / "flatpak/app"]


class Launcher:
    def __init__(
        self,
        dirs: list[Path] | None = None,
        run: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.dirs = dirs if dirs is not None else installed_dirs()
        self.run = run or _systemd_run

    def launch(self, app_id: Any) -> dict:
        if not is_app_id(app_id):
            raise AppsError(f"'{app_id}' is not a Flatpak app id")
        if not any((d / app_id / "current").exists() for d in self.dirs):
            raise AppsError(f"{app_id} is not installed; install it first")
        self.run(["flatpak", "run", app_id])
        return {"launched": app_id}


def _systemd_run(command: list[str]) -> None:
    try:
        subprocess.run(
            ["systemd-run", "--user", "--collect", "--quiet", "--no-block", "--", *command],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        err = getattr(e, "stderr", b"") or b""
        raise AppsError(f"the app did not start: {err.decode(errors='replace') or e}") from e


class LauncherServer:
    def __init__(self, launcher: Launcher) -> None:
        self.launcher = launcher

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
            info = {"name": "vilna-mcp-launcher", "version": __version__}
            caps = {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": info}
            return _result(id_, caps)
        if method == "ping":
            return _result(id_, {})
        if method == "tools/list":
            return _result(id_, {"tools": TOOLS})
        if method == "tools/call":
            return _result(id_, self.call(params.get("name"), params.get("arguments", {})))
        return _error(id_, -32601, f"unknown method {method}")

    def call(self, name: Any, args: Any) -> dict:
        if name != "launch":
            return _tool_error(f"unknown tool {name}")
        if not isinstance(args, dict) or set(args) != {"app_id"}:
            return _tool_error("launch takes exactly one argument, app_id")
        try:
            out = self.launcher.launch(args["app_id"])
        except AppsError as e:
            return _tool_error(str(e))
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vilna-mcp-launcher")
    p.add_argument("--memory", action="store_true", help="pretend to start apps (tests)")
    args = p.parse_args(argv)
    launcher = Launcher(dirs=[], run=lambda cmd: None) if args.memory else Launcher()
    LauncherServer(launcher).serve(sys.stdin, sys.stdout)
    return 0
