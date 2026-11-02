# SPDX-License-Identifier: Apache-2.0
"""MCP server over stdio for Vilna Backup (newline-delimited JSON-RPC 2.0).

    vilna-mcp-backup [--command "vilna-backup"]

It offers Gloxi only what Herkos-lite may carry out (R0-R2, design.md 5):
status, backups list, sizes, what and how often to back up, run now, and a
restore into a new folder. Setting up the place, the recovery key, restoring
over files and anything with keys stay with the person (``vilna-backup`` in a
terminal, later the Herkos window).

The work is done by the ``vilna-backup`` program, run as a separate process
with ``--json``: this server never sees the repository password, which restic
fetches from the key store itself.

In Vilna it is started by Herkos, never by Gloxi directly.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from typing import Any, TextIO

from vilna_mcp_backup import __version__

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
PRESETS = ["settings", "documents", "home", "custom"]
SCHEDULES = ["hourly", "daily", "weekly", "off"]
CALL_TIMEOUT = 50.0  # below Herkos's 60 s for one tool call
MAX_LIST = 200

_FOLDERS = {"type": "array", "items": {"type": "string"}, "description": "Absolute folders"}


class ToolFailure(Exception):
    """A request the server will not or cannot carry out."""


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


_READ = {"readOnlyHint": True}
# Changes that lose nothing: a new snapshot (the place is append-only), a
# setting that can be set back, files restored into a new folder. Data that
# leaves the device is encrypted on it and goes only to the place the person
# chose (ADR 0007), so this is not "open world".
_CHANGE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}

TOOLS = [
    _tool(
        "status", "Whether backups are on, where, what, how often, the last run", {}, [], **_READ
    ),
    _tool("list_snapshots", "List the backups made so far", {}, [], **_READ),
    _tool(
        "overview",
        "Sizes of what could be backed up (settings, documents, pictures, ...) and whether "
        "the current choice fits the limit, with large folders to leave out if not",
        {},
        [],
        **_READ,
    ),
    _tool("run_now", "Start one backup now, in the background", {}, [], **_CHANGE),
    _tool(
        "set_schedule",
        "How often to back up",
        {"schedule": {"type": "string", "enum": SCHEDULES}},
        ["schedule"],
        **_CHANGE,
    ),
    _tool(
        "set_paths",
        "What to back up: a preset, folders to add or leave out, Gloxi history, a size limit",
        {
            "preset": {"type": "string", "enum": PRESETS},
            "add": _FOLDERS,
            "remove": _FOLDERS,
            "gloxi_history": {"type": "boolean"},
            "limit_gb": {"type": "number", "description": "0 for no limit"},
        },
        [],
        **_CHANGE,
    ),
    _tool(
        "restore",
        "Bring a backup back into a new, empty folder (never over existing files)",
        {
            "target": {"type": "string", "description": "Absolute path of a new folder"},
            "snapshot": {"type": "string", "description": "Backup id, or latest"},
            "path": {"type": "string", "description": "Only this file or folder (relative)"},
        },
        ["target"],
        **_CHANGE,
    ),
]
_SCHEMAS = {t["name"]: t["inputSchema"] for t in TOOLS}
_JSON_TYPES = {"string": str, "boolean": bool, "number": (int, float), "array": list}


def check_arguments(name: str, args: Any) -> dict:
    schema = _SCHEMAS[name]
    if not isinstance(args, dict):
        raise ToolFailure("arguments must be an object")
    unknown = set(args) - set(schema["properties"])
    if unknown:
        raise ToolFailure(f"unknown arguments: {sorted(unknown)}")
    missing = [k for k in schema["required"] if k not in args]
    if missing:
        raise ToolFailure(f"missing arguments: {missing}")
    for k, v in args.items():
        prop = schema["properties"][k]
        want = _JSON_TYPES[prop["type"]]
        if not isinstance(v, want) or (prop["type"] == "number" and isinstance(v, bool)):
            raise ToolFailure(f"'{k}' must be a {prop['type']}")
        if "enum" in prop and v not in prop["enum"]:
            raise ToolFailure(f"'{k}' must be one of {prop['enum']}")
        if prop["type"] == "array" and (
            len(v) > MAX_LIST or not all(isinstance(x, str) and x.startswith("/") for x in v)
        ):
            raise ToolFailure(f"'{k}' must be a list of absolute folders")
        if prop["type"] == "string" and ("\x00" in v or v.startswith("-")):
            raise ToolFailure(f"'{k}' is not a valid value")
    return args


def to_command(name: str, args: dict) -> list[str]:
    """The ``vilna-backup`` arguments for one tool call."""
    if name == "status":
        return ["status"]
    if name == "list_snapshots":
        return ["snapshots"]
    if name == "overview":
        return ["overview"]
    if name == "run_now":
        return ["start"]
    if name == "set_schedule":
        return ["schedule", args["schedule"]]
    if name == "set_paths":
        cmd = ["set"]
        if "preset" in args:
            cmd += ["--preset", args["preset"]]
        for folder in args.get("add", []):
            cmd += ["--add", folder]
        for folder in args.get("remove", []):
            cmd += ["--remove", folder]
        if "gloxi_history" in args:
            cmd += ["--history", "yes" if args["gloxi_history"] else "no"]
        if "limit_gb" in args:
            if args["limit_gb"] < 0:
                raise ToolFailure("'limit_gb' must not be negative")
            cmd += ["--limit-gb", str(args["limit_gb"])]
        return cmd
    if name == "restore":
        cmd = ["restore", "--target", args["target"]]
        if "snapshot" in args:
            cmd += ["--snapshot", args["snapshot"]]
        if "path" in args:
            cmd += ["--path", args["path"]]
        return cmd
    raise ToolFailure(f"unknown tool {name}")


Runner = Callable[..., subprocess.CompletedProcess]


def default_program() -> list[str]:
    exe = shutil.which("vilna-backup")
    if exe:
        return [exe]
    if importlib.util.find_spec("vilna_backup") is not None:
        return [sys.executable, "-m", "vilna_backup"]
    return ["vilna-backup"]


class Server:
    def __init__(self, program: Sequence[str], run: Runner = subprocess.run) -> None:
        self.program = list(program)
        self.run = run

    def handle(self, msg: Any) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, -32600, "not a JSON-RPC 2.0 message")
        method, id_ = msg.get("method"), msg.get("id")
        if "id" not in msg:
            return None
        params = msg.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(
                id_,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "vilna-mcp-backup", "version": __version__},
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
        if name not in _SCHEMAS:
            return _tool_error(f"unknown tool {name}")
        try:
            out = self.invoke(to_command(name, check_arguments(name, args)))
        except ToolFailure as e:
            return _tool_error(str(e))
        return {
            "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out,
            "isError": False,
        }

    def invoke(self, args: list[str]) -> dict:
        try:
            p = self.run(
                [*self.program, "--json", *args],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=CALL_TIMEOUT,
            )
        except FileNotFoundError:
            raise ToolFailure("vilna-backup is not installed") from None
        except subprocess.TimeoutExpired:
            raise ToolFailure("vilna-backup took too long") from None
        try:
            out = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else None
        except ValueError:
            out = None
        if isinstance(out, dict) and "error" in out:
            raise ToolFailure(str(out["error"]))
        if p.returncode != 0 or not isinstance(out, dict):
            detail = p.stderr.strip().splitlines()[-1:] or [f"exit {p.returncode}"]
            raise ToolFailure(f"vilna-backup failed: {detail[0]}")
        return out

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
    p = argparse.ArgumentParser(prog="vilna-mcp-backup")
    p.add_argument("--command", help="how to run vilna-backup (default: find it)")
    args = p.parse_args(argv)
    program = shlex.split(args.command) if args.command else default_program()
    Server(program).serve(sys.stdin, sys.stdout)
    return 0
