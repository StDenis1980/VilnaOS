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
import json
import sys
from collections import deque

from herkos import __version__
from herkos.config import ConfigError, load_config, socket_path
from herkos.confirm import DenyAll, Terminal, default_confirmer
from herkos.gate import Gate
from herkos.grants import GrantStore
from herkos.manifest import ManifestError, ManifestRegistry
from herkos.mitos import Journal, JournalError, read_records, verify
from herkos.server import Server, SocketError
from herkos.tools import ToolRegistry


def _serve(args: argparse.Namespace) -> int:
    config = load_config()
    confirmer = {"terminal": Terminal(), "deny": DenyAll()}.get(args.confirm) or default_confirmer()
    try:
        journal = Journal(config.journal_path)
    except JournalError as e:
        print(f"herkos: the journal is broken, refusing to start: {e}", file=sys.stderr)
        return 1
    grants = GrantStore(config.grants_path)
    if grants.load_error:
        print(f"herkos: ignoring unreadable permissions: {grants.load_error}", file=sys.stderr)
    gate = Gate(config, ManifestRegistry.builtin(), ToolRegistry(), grants, journal, confirmer)
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
    return 0


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
        choices=["auto", "terminal", "deny"],
        default="auto",
        help="how to ask the user (auto: this terminal if there is one, else deny)",
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, ManifestError, SocketError) as e:
        print(f"herkos: {e}", file=sys.stderr)
        return 1
