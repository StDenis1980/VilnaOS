# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line of Herkos-lite.

herkos serve                 run the gate on the socket
herkos journal verify        check the journal's hash chain
herkos journal show [-n N]   print the last records
herkos permissions list      list the permissions the user gave
herkos permissions revoke ID take one back
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import socket
import sys
from collections import deque

from herkos import PROTOCOL_VERSION, __version__
from herkos.config import ConfigError, load_config, socket_path
from herkos.confirm import DenyAll, Dialog, Terminal, default_confirmer
from herkos.gate import Gate
from herkos.grants import GrantStore
from herkos.manifest import ManifestError, ManifestRegistry
from herkos.mcp_client import McpError, McpStdioExecutor
from herkos.mitos import Journal, JournalError, read_records, verify
from herkos.server import Server, SocketError
from herkos.tools import ToolRegistry

FILES_TOOL = "org.vilna.mcp.files"


def _serve(args: argparse.Namespace) -> int:
    config = load_config()
    choices = {"gui": Dialog, "terminal": Terminal, "deny": DenyAll}
    confirmer = choices[args.confirm]() if args.confirm in choices else default_confirmer()
    try:
        journal = Journal(config.journal_path)
    except JournalError as e:
        print(f"herkos: the journal is broken, refusing to start: {e}", file=sys.stderr)
        return 1
    grants = GrantStore(config.grants_path)
    if grants.load_error:
        print(f"herkos: ignoring unreadable permissions: {grants.load_error}", file=sys.stderr)
    tools = ToolRegistry()
    files = start_files_tool(config)
    if files is not None:
        tools.register(FILES_TOOL, files)
    gate = Gate(config, ManifestRegistry.builtin(), tools, grants, journal, confirmer)
    path = socket_path()
    journal.append("start", {"server": f"herkos-lite/{__version__}", "socket": str(path)})
    server = Server(gate, path)
    print(f"herkos: listening on {path}", file=sys.stderr)
    try:
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        pass
    finally:
        path.unlink(missing_ok=True)
        if files is not None:
            files.stop()
    return 0


def start_files_tool(config) -> McpStdioExecutor | None:
    """Start the files MCP server (mcp/files) on the allowed folders, if installed."""
    if importlib.util.find_spec("vilna_mcp_files") is None:
        print("herkos: vilna-mcp-files is not installed; no files tool", file=sys.stderr)
        return None
    roots = [r for r in config.allowed_roots if r.is_dir()]
    if not roots:
        print("herkos: none of allowed_roots exists; no files tool", file=sys.stderr)
        return None
    command = [sys.executable, "-m", "vilna_mcp_files", "--backup-dir", str(config.backup_dir)]
    for r in roots:
        command += ["--root", str(r)]
    executor = McpStdioExecutor(command)
    try:
        executor.start()
    except McpError as e:
        print(f"herkos: files tool failed to start: {e}", file=sys.stderr)
        executor.stop()
        return None
    return executor


def _undo(args: argparse.Namespace) -> int:
    path = socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(path))
            f = s.makefile("rwb")
            answer = None
            for i, (method, params) in enumerate(
                [
                    ("herkos.hello", {"protocol": [PROTOCOL_VERSION], "client": "herkos-cli"}),
                    ("herkos.undo", {"action_id": args.action_id}),
                ],
                start=1,
            ):
                msg = {"jsonrpc": "2.0", "id": i, "method": method, "params": params}
                f.write(json.dumps(msg).encode() + b"\n")
                f.flush()
                answer = json.loads(f.readline())
                if "error" in answer:
                    print(f"herkos: {answer['error']['message']}", file=sys.stderr)
                    return 1
    except OSError as e:
        print(f"herkos: is `herkos serve` running? ({e})", file=sys.stderr)
        return 1
    result = answer["result"]
    print(f"{result['decision']}: {result['message']} (action {result['action_id']})")
    return 0 if result["code"] == "allowed" else 1


def _journal_verify(args: argparse.Namespace) -> int:
    path = load_config().journal_path
    result = verify(path)
    if result.ok:
        print(f"{path}: OK, {result.records} records")
        return 0
    print(f"{path}: BROKEN at line {result.line}: {result.problem}", file=sys.stderr)
    return 1


def _journal_show(args: argparse.Namespace) -> int:
    path = load_config().journal_path
    if not path.exists():
        return 0
    for _, rec in deque(read_records(path), maxlen=args.n):
        print(json.dumps(rec, ensure_ascii=False))
    return 0


def _permissions_list(args: argparse.Namespace) -> int:
    store = GrantStore(load_config().grants_path)
    if store.load_error:
        print(f"herkos: {store.load_error}", file=sys.stderr)
        return 1
    for g in store.all():
        roots = ", ".join(g.roots) or "-"
        print(f"{g.id}  {g.tool}.{g.operation}  {g.level}  {g.duration}  {roots}")
    return 0


def _permissions_revoke(args: argparse.Namespace) -> int:
    store = GrantStore(load_config().grants_path)
    if store.revoke(args.id):
        print(f"revoked {args.id}")
        return 0
    print(f"herkos: no permission {args.id}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="herkos", description="Herkos-lite permission gate")
    p.add_argument("--version", action="version", version=f"herkos-lite {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the gate")
    serve.add_argument(
        "--confirm",
        choices=["auto", "gui", "terminal", "deny"],
        default="auto",
        help="how to ask the user (auto: a window if there is a desktop, else this "
        "terminal, else deny)",
    )
    serve.set_defaults(func=_serve)

    journal = sub.add_parser("journal", help="the action journal").add_subparsers(
        dest="journal_command", required=True
    )
    journal.add_parser("verify", help="check the hash chain").set_defaults(func=_journal_verify)
    show = journal.add_parser("show", help="print the last records")
    show.add_argument("-n", type=int, default=20)
    show.set_defaults(func=_journal_show)

    perms = sub.add_parser("permissions", help="permissions the user gave").add_subparsers(
        dest="permissions_command", required=True
    )
    perms.add_parser("list").set_defaults(func=_permissions_list)
    revoke = perms.add_parser("revoke")
    revoke.add_argument("id")
    revoke.set_defaults(func=_permissions_revoke)

    undo = sub.add_parser("undo", help="undo a change by its action id")
    undo.add_argument("action_id")
    undo.set_defaults(func=_undo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, ManifestError, SocketError) as e:
        print(f"herkos: {e}", file=sys.stderr)
        return 1
