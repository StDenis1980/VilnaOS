# SPDX-License-Identifier: GPL-3.0-or-later
"""The way to the internet for the Gloxi service (ADR 0025, section 3).

``gloxi@<uid>.service`` may only reach this computer: the model and Herkos.
When the person uses the cloud, Gloxi asks Herkos for the action
``org.vilna.gloxi.network.allow_cloud`` (R2), and Herkos asks the person.
Only then does the tool below open the internet to that person's Gloxi.
Gloxi cannot do it itself: the tool runs as root, started by the system
Herkos (``vilna-mcp-gloxi-network@<uid>.service``, polkit rule of Herkos).

Two parts:

* ``Server``: the MCP tool ``vilna-mcp-gloxi-network <uid>``. It changes one
  property of one unit, ``IPAddressAllow`` of ``gloxi@<uid>.service``, with
  ``systemctl set-property``: systemd applies it to the running service at
  once (no restart, the conversation goes on) and keeps it after a reboot.
* ``CloudNetwork``: what gloxid calls before its first message to the cloud
  (``Router(network=...)``).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from collections.abc import Callable
from gettext import gettext as _
from typing import Any, TextIO

from gloxi.router import NetworkRefused

TOOL = "org.vilna.gloxi.network"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LOCAL_ONLY = "localhost"
FIRST_UID = 1000
LAST_UID = 59999


def _tool(name: str, description: str, **annotations: Any) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": annotations,
    }


TOOLS = [
    _tool("status", "Whether the person's Gloxi may reach the internet", readOnlyHint=True),
    _tool(
        "allow_cloud",
        "Let the person's Gloxi reach the internet, for the cloud model",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
    ),
    _tool(
        "block_cloud",
        "Let the person's Gloxi reach only this computer again",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
    ),
]

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=30)


class NetworkError(Exception):
    pass


class Network:
    """IPAddressAllow of one person's gloxi@<uid>.service, and nothing else."""

    def __init__(self, uid: int, runner: Runner = _run) -> None:
        if not FIRST_UID <= uid <= LAST_UID:
            raise ValueError(f"not a person's uid: {uid}")
        self.unit = f"gloxi@{uid}.service"
        self.run = runner

    def _systemctl(self, *args: str) -> str:
        try:
            done = self.run(["systemctl", *args])
        except (OSError, subprocess.SubprocessError) as exc:
            raise NetworkError(f"systemctl: {exc}") from None
        if done.returncode != 0:
            raise NetworkError(
                (done.stderr or "").strip() or f"systemctl failed ({done.returncode})"
            )
        return done.stdout or ""

    def status(self) -> dict:
        allowed = self._systemctl("show", "--property=IPAddressAllow", "--value", self.unit)
        # "any" reads back as "::/0 0.0.0.0/0".
        return {"cloud": "0.0.0.0/0" in allowed.split()}

    def allow_cloud(self) -> dict:
        self._systemctl("set-property", self.unit, "IPAddressAllow=any")
        return self.status()

    def block_cloud(self) -> dict:
        # An empty assignment drops every earlier one; then localhost again,
        # as in the unit itself (the model and Herkos).
        self._systemctl(
            "set-property", self.unit, "IPAddressAllow=", f"IPAddressAllow={LOCAL_ONLY}"
        )
        return self.status()


class Server:
    """MCP over stdio, one JSON-RPC 2.0 message per line."""

    def __init__(self, network: Network) -> None:
        self.network = network
        self._handlers = {
            "status": network.status,
            "allow_cloud": network.allow_cloud,
            "block_cloud": network.block_cloud,
        }

    def handle(self, msg: Any) -> dict | None:
        if not isinstance(msg, dict):
            return _error(None, -32600, "a request must be an object")
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
                    "serverInfo": {"name": "vilna-mcp-gloxi-network", "version": "1"},
                },
            )
        if method == "ping":
            return _result(id_, {})
        if method == "tools/list":
            return _result(id_, {"tools": TOOLS})
        if method == "tools/call":
            return _result(id_, self.call(params.get("name"), params.get("arguments")))
        return _error(id_, -32601, f"unknown method {method}")

    def call(self, name: Any, args: Any) -> dict:
        handler = self._handlers.get(name) if isinstance(name, str) else None
        if handler is None:
            return _tool_error(f"unknown tool {name}")
        if args not in (None, {}):
            return _tool_error(f"{name} takes no arguments")
        try:
            out = handler()
        except NetworkError as exc:
            return _tool_error(str(exc))
        return {
            "content": [{"type": "text", "text": json.dumps(out)}],
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
                stdout.write(json.dumps(response) + "\n")
                stdout.flush()


def _result(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


# -- gloxid's side ----------------------------------------------------------------

Request = Callable[[str, str], Any]  # (operation, reason) -> Herkos's answer


class CloudNetwork:
    """Asks Herkos once per run of gloxid; afterwards the way stays open."""

    def __init__(self, request: Request) -> None:
        self.request = request
        self.open = False
        self._lock = threading.Lock()

    def _ask(self, operation: str, reason: str) -> dict:
        try:
            answer = self.request(operation, reason)
        except Exception:  # Herkos is not running or the connection broke
            raise NetworkRefused(
                _("Herkos is not running, so Gloxi cannot connect to the internet for the cloud.")
            ) from None
        return answer if isinstance(answer, dict) else {}

    def __call__(self) -> None:
        with self._lock:
            if self.open:
                return
            answer = self._ask(
                "allow_cloud",
                _("Gloxi needs the internet to send your message to the cloud model."),
            )
            if answer.get("decision") != "allowed" or answer.get("code") != "allowed":
                raise NetworkRefused(
                    _("Gloxi may not connect to the internet, so the cloud model cannot answer.")
                )
            self.open = True

    def close(self) -> bool:
        """Back to this computer only, when the cloud is off (start of gloxid)."""
        with self._lock:
            self.open = False
            try:
                status = self._ask("status", _("Gloxi checks whether it may reach the internet."))
                if not _result_of(status).get("cloud"):
                    return True
                answer = self._ask("block_cloud", _("The cloud is turned off."))
            except NetworkRefused:
                return False
            return answer.get("decision") == "allowed"


def _result_of(answer: dict) -> dict:
    """The tool's own result inside Herkos's answer, when Herkos allowed the action."""
    if answer.get("decision") != "allowed":
        return {}
    result = answer.get("result")
    if isinstance(result, dict) and isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    return result if isinstance(result, dict) else {}


def herkos_network() -> CloudNetwork | None:
    """The gate over the system Herkos, or None where there is none."""
    try:
        from herkos.client import HerkosClient
        from herkos.config import ConfigError, socket_path
    except ImportError:
        return None
    try:
        sock = socket_path()
    except ConfigError:
        return None
    if not sock.exists():
        return None

    def request(operation: str, reason: str) -> Any:
        client = HerkosClient.connect(sock)
        try:
            return client.request_action(tool=TOOL, operation=operation, reason=reason)
        finally:
            client.close()

    return CloudNetwork(request)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="vilna-mcp-gloxi-network",
        description="Open or close the internet for one person's Gloxi (ADR 0025)",
    )
    p.add_argument("uid", type=int, help="the person's uid (gloxi@<uid>.service)")
    args = p.parse_args(argv)
    try:
        network = Network(args.uid)
    except ValueError as exc:
        print(f"vilna-mcp-gloxi-network: {exc}", file=sys.stderr)
        return 2
    Server(network).serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
