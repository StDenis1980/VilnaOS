# SPDX-License-Identifier: Apache-2.0
"""MCP server over stdio (newline-delimited JSON-RPC 2.0).

    vilna-mcp-files --root ~/Documents [--root ...] --backup-dir DIR

In Vilna it is started by Herkos, never by Gloxi directly: Herkos decides
whether a call may happen, this server only does it inside its roots.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from vilna_mcp_files import __version__
from vilna_mcp_files.files import DEFAULT_KEEP_DAYS, Files, FilesError

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

_PATH = {"type": "string", "description": "Absolute path"}
_TEXT = {"type": "string", "description": "Full new contents (UTF-8 text)"}


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


TOOLS = [
    _tool("list", "List a folder", {"path": _PATH}, ["path"], readOnlyHint=True),
    _tool(
        "search",
        "Find files whose name or text contains the query",
        {"path": _PATH, "query": {"type": "string"}},
        ["path", "query"],
        readOnlyHint=True,
    ),
    _tool("read", "Read a text file", {"path": _PATH}, ["path"], readOnlyHint=True),
    _tool(
        "create",
        "Create a new text file",
        {"path": _PATH, "text": _TEXT},
        ["path", "text"],
        readOnlyHint=False,
        destructiveHint=False,
    ),
    _tool(
        "write",
        "Replace the contents of a text file (a backup is kept)",
        {"path": _PATH, "text": _TEXT},
        ["path", "text"],
        readOnlyHint=False,
        destructiveHint=False,
    ),
    _tool(
        "delete",
        "Delete a file (a backup is kept)",
        {"path": _PATH},
        ["path"],
        readOnlyHint=False,
        destructiveHint=True,
    ),
    _tool(
        "restore",
        "Undo a change: put a file back as it was before",
        {"path": _PATH, "backup_id": {"type": "string"}},
        ["path", "backup_id"],
        readOnlyHint=False,
        destructiveHint=False,
    ),
]
_SCHEMAS = {t["name"]: t["inputSchema"] for t in TOOLS}


def check_arguments(name: str, args: Any) -> dict:
    schema = _SCHEMAS[name]
    if not isinstance(args, dict):
        raise FilesError("arguments must be an object")
    unknown = set(args) - set(schema["properties"])
    if unknown:
        raise FilesError(f"unknown arguments: {sorted(unknown)}")
    missing = [k for k in schema["required"] if k not in args]
    if missing:
        raise FilesError(f"missing arguments: {missing}")
    for k, v in args.items():
        if not isinstance(v, str):
            raise FilesError(f"'{k}' must be a string")
    return args


class Server:
    def __init__(self, files: Files) -> None:
        self.files = files
        self._handlers: dict[str, Callable[..., dict]] = {
            "list": files.list,
            "search": files.search,
            "read": files.read,
            "create": files.create,
            "write": files.write,
            "delete": files.delete,
            "restore": files.restore,
        }

    def handle(self, msg: Any) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, -32600, "not a JSON-RPC 2.0 message")
        method, id_ = msg.get("method"), msg.get("id")
        if "id" not in msg:
            return None  # notifications (e.g. notifications/initialized)
        params = msg.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(
                id_,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "vilna-mcp-files", "version": __version__},
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
        except FilesError as e:
            return _tool_error(str(e))
        except OSError as e:
            return _tool_error(f"{type(e).__name__}: {e.strerror or e}")
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
    p = argparse.ArgumentParser(prog="vilna-mcp-files")
    p.add_argument("--root", action="append", required=True, help="allowed folder (repeatable)")
    p.add_argument("--backup-dir", required=True, help="where backups for undo are kept")
    p.add_argument(
        "--keep-days",
        type=float,
        default=DEFAULT_KEEP_DAYS,
        help=f"how long backups are kept (default {DEFAULT_KEEP_DAYS}, as OneDrive)",
    )
    args = p.parse_args(argv)
    if args.keep_days <= 0:
        p.error("--keep-days must be positive")
    try:
        files = Files(
            [Path(r).expanduser() for r in args.root],
            Path(args.backup_dir),
            keep_days=args.keep_days,
        )
    except FilesError as e:
        print(f"vilna-mcp-files: {e}", file=sys.stderr)
        return 2
    Server(files).serve(sys.stdin, sys.stdout)
    return 0
