# SPDX-License-Identifier: Apache-2.0
"""A small MCP server over stdio (newline-delimited JSON-RPC 2.0).

Started by Herkos, never by Gloxi (ADR 0031): Herkos decides whether an
action may happen, these servers only carry it out, and check their own
arguments again.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from vilna_mcp_desktop import __version__

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
# A longer line closes nothing but is answered with an error.
MAX_LINE = 1024 * 1024


class ToolError(Exception):
    """The action could not be done; the message goes back to Herkos and Gloxi."""


def tool(name: str, description: str, props: dict, required: list[str], **annotations) -> dict:
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


READ = {"readOnlyHint": True}
CHANGE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}

_TYPES: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def check_arguments(schema: Mapping[str, Any], args: Any) -> dict:
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    unknown = set(args) - set(schema["properties"])
    if unknown:
        raise ToolError(f"unknown arguments: {sorted(unknown)}")
    missing = [k for k in schema["required"] if k not in args]
    if missing:
        raise ToolError(f"missing arguments: {missing}")
    for k, v in args.items():
        prop = schema["properties"][k]
        if not _TYPES[prop["type"]](v):
            raise ToolError(f"'{k}' must be a {prop['type']}")
        if "enum" in prop and v not in prop["enum"]:
            raise ToolError(f"'{k}' must be one of {prop['enum']}")
        if "maxItems" in prop and len(v) > prop["maxItems"]:
            raise ToolError(f"'{k}' has more than {prop['maxItems']} items")
        if "maxLength" in prop and len(v) > prop["maxLength"]:
            raise ToolError(f"'{k}' is longer than {prop['maxLength']} characters")
        if "minimum" in prop and v < prop["minimum"]:
            raise ToolError(f"'{k}' must be at least {prop['minimum']}")
        if "maximum" in prop and v > prop["maximum"]:
            raise ToolError(f"'{k}' must be at most {prop['maximum']}")
    return args


class Server:
    def __init__(self, name: str, tools: list[dict], handlers: Mapping[str, Callable[..., dict]]):
        assert {t["name"] for t in tools} == set(handlers), "every tool has a handler"
        self.name = name
        self.tools = tools
        self._schemas = {t["name"]: t["inputSchema"] for t in tools}
        self._handlers = dict(handlers)

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
                    "serverInfo": {"name": self.name, "version": __version__},
                },
            )
        if method == "ping":
            return _result(id_, {})
        if method == "tools/list":
            return _result(id_, {"tools": self.tools})
        if method == "tools/call":
            return _result(id_, self.call(params.get("name"), params.get("arguments", {})))
        return _error(id_, -32601, f"unknown method {method}")

    def call(self, name: Any, args: Any) -> dict:
        if not isinstance(name, str) or name not in self._handlers:
            return _tool_error(f"unknown tool {name}")
        try:
            out = self._handlers[name](**check_arguments(self._schemas[name], args))
        except ToolError as e:
            return _tool_error(str(e))
        except Exception as e:  # GLib errors and the like: report, keep serving
            return _tool_error(f"{type(e).__name__}: {e}")
        return {
            "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out,
            "isError": False,
        }

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        while True:
            line = stdin.readline(MAX_LINE + 1)
            if not line:
                return
            if len(line) > MAX_LINE:
                # Skip the rest of the long line.
                while line and not line.endswith("\n"):
                    line = stdin.readline(MAX_LINE)
                response = _error(None, -32700, "line too long")
            elif not line.strip():
                continue
            else:
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
