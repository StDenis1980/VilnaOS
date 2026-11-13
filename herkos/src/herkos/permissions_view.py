# SPDX-License-Identifier: GPL-3.0-or-later
"""What the «Дозволи» window shows, without GTK (permissions.md 5.3, 7.1).

The window has two parts: the permissions the person gave (each can be
taken back) and the journal of Gloxi's actions in plain language (a change
can be undone for 30 days). This module turns journal records and
permissions into rows, and talks to Herkos:

- the system ``herkosd`` (Vilna): the person's own connection to
  ``/run/herkos/herkos.sock`` has ``herkos.permissions``, ``herkos.revoke``,
  ``herkos.journal`` and ``herkos.undo``. Gloxi, another user, has none of
  them except ``herkos.undo`` for its own session.
- Herkos-lite (the preview in the person's session): the window reads the
  permissions and the journal from ``$XDG_STATE_HOME/herkos/`` itself, as
  ``herkos permissions`` and ``herkos journal show`` do, and undoes through
  a clean connection to the socket, as ``herkos undo`` does.
"""

from __future__ import annotations

import json
import socket
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from herkos import PROTOCOL_VERSION
from herkos.confirm import readable_resource
from herkos.i18n import _
from herkos.manifest import ManifestRegistry

SYSTEM_SOCKET = Path("/run/herkos/herkos.sock")
UNDO_DAYS = 30  # how long the files tool keeps what it replaced (permissions.md 7.2)
JOURNAL_LIMIT = 500
FILES = "org.vilna.mcp.files"
BACKUP = "org.vilna.mcp.backup"
SYSTEM = "org.vilna.mcp.system"
VPN = "org.vilna.mcp.vpn"
UNDO_PREFIX = "Undo action "
# The operations Herkos itself sends to undo an action (herkos.undo).
UNDO_OPERATIONS = ("restore", "restore_snapshot")

# What Gloxi did, in the past tense, for the built-in tools. A catalogue
# program's operation shows its manifest summary instead.
DONE: dict[tuple[str, str], str] = {
    (FILES, "list"): "Gloxi looked into a folder",
    (FILES, "search"): "Gloxi searched files",
    (FILES, "read"): "Gloxi read a file",
    (FILES, "create"): "Gloxi created a file",
    (FILES, "write"): "Gloxi changed a file",
    (FILES, "delete"): "Gloxi deleted a file",
    (FILES, "restore"): "Gloxi brought back an earlier version of a file",
    (FILES, "restore_snapshot"): "Gloxi brought back files from a snapshot",
    (SYSTEM, "list_snapshots"): "Gloxi looked at the saved states of the system",
    (SYSTEM, "rollback"): "Gloxi returned the system to a saved state",
    (SYSTEM, "restore"): "Gloxi undid a return of the system to a saved state",
    (VPN, "status"): "Gloxi looked at the VPN kill switch and the DNS",
    (VPN, "killswitch_on"): "Gloxi turned on the VPN kill switch",
    (VPN, "killswitch_off"): "Gloxi turned off the VPN kill switch",
    (VPN, "allow_lan"): "Gloxi let a local network through the VPN kill switch",
    (VPN, "forget_lan"): "Gloxi stopped letting a local network through the VPN kill switch",
    (VPN, "open_portal"): "Gloxi opened the VPN kill switch to sign in to a Wi-Fi network",
    (VPN, "set_dns"): "Gloxi changed the encrypted DNS",
    (VPN, "start_provider"): "Gloxi started the service of the VPN provider",
    (BACKUP, "status"): "Gloxi checked the backups",
    (BACKUP, "list_snapshots"): "Gloxi listed the backups",
    (BACKUP, "overview"): "Gloxi looked at how much space the folders take",
    (BACKUP, "run_now"): "Gloxi started a backup",
    (BACKUP, "set_schedule"): "Gloxi changed how often backups are made",
    (BACKUP, "set_paths"): "Gloxi changed what goes into backups",
    (BACKUP, "restore"): "Gloxi brought a backup back into a new folder",
}
# The same as asked, not done: for a denied action.
ASKED = "Gloxi asked to {what}"

CATEGORIES = ("all", "files", "backup", "programs")
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def category_title(category: str) -> str:
    return {
        "all": _("All"),
        "files": _("Files"),
        "backup": _("Backups"),
        "programs": _("Programs"),
    }[category]


def category_of(tool: str) -> str:
    if tool == FILES:
        return "files"
    if tool == BACKUP:
        return "backup"
    return "programs"


# -- the journal --------------------------------------------------------------


@dataclass
class Action:
    action_id: str
    when: datetime
    tool: str
    operation: str
    resources: list[str] = field(default_factory=list)
    resource_count: int = 0
    reason: str = ""
    level: str | None = None
    decision: str | None = None  # "allowed", "denied"; None: no answer yet
    code: str | None = None
    ok: bool | None = None  # the tool's result; None: not carried out
    has_undo: bool = False
    undo_of: str | None = None  # this action undid that one
    undone_by: str | None = None  # a later action undid this one

    @property
    def category(self) -> str:
        return category_of(self.tool)

    def can_undo(self, now: datetime) -> bool:
        return (
            self.ok is True
            and self.has_undo
            and self.undone_by is None
            and now - self.when <= timedelta(days=UNDO_DAYS)
        )


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def short_path(path: str, home: Path | None = None) -> str:
    """``/home/ola/Documents/a.txt`` → ``~/Documents/a.txt`` for the person's own home."""
    home = home or Path.home()
    prefix = str(home).rstrip("/") + "/"
    return "~/" + path[len(prefix) :] if path.startswith(prefix) else path


def actions(records: Iterable[Mapping[str, Any] | None]) -> list[Action]:
    """Requests with their decisions and results, newest first.

    Records Herkos did not write the expected way are skipped, not trusted:
    the window shows what it can read and never fails on a strange line.
    """
    by_id: dict[str, Action] = {}
    for rec in records:
        if not isinstance(rec, Mapping):
            continue
        aid, kind, data = rec.get("action_id"), rec.get("kind"), rec.get("data")
        if not isinstance(aid, str) or not isinstance(data, Mapping):
            continue
        if kind == "request":
            when = _time(rec.get("time"))
            tool, op = data.get("tool"), data.get("operation")
            if when is None or not isinstance(tool, str) or not isinstance(op, str):
                continue
            uris = data.get("resources")
            uris = [u for u in uris if isinstance(u, str)] if isinstance(uris, list) else []
            count = data.get("resource_count")
            reason = data.get("reason") if isinstance(data.get("reason"), str) else ""
            a = Action(
                action_id=aid,
                when=when,
                tool=tool,
                operation=op,
                resources=[short_path(readable_resource(u)) for u in uris],
                resource_count=count if isinstance(count, int) else len(uris),
                reason=reason,
            )
            if op in UNDO_OPERATIONS and reason.startswith(UNDO_PREFIX):
                a.undo_of = reason[len(UNDO_PREFIX) :].strip() or None
            by_id[aid] = a
        elif (a := by_id.get(aid)) is None:
            continue
        elif kind == "decision":
            d = data.get("decision")
            a.decision = d if d in ("allowed", "denied") else None
            level = data.get("level")
            a.level = level if isinstance(level, str) else None
            code = data.get("code")
            a.code = code if isinstance(code, str) else None
        elif kind == "result":
            a.ok = data.get("ok") is True
            code = data.get("code")
            if isinstance(code, str):
                a.code = code
            a.has_undo = can_be_undone(data.get("undo"))
    for a in by_id.values():
        if a.undo_of and a.ok and (target := by_id.get(a.undo_of)) is not None:
            target.undone_by = a.action_id
    return sorted(by_id.values(), key=lambda a: a.when, reverse=True)


def can_be_undone(undo: Any) -> bool:
    """The journal says how to undo: a backup of the files tool, the tool's own
    arguments for ``restore`` (ADR 0023) or a pair of Mitos snapshots (ADR 0026)."""
    if not isinstance(undo, Mapping):
        return False
    snapshot = undo.get("snapshot")
    return (
        bool(undo.get("backup_id"))
        or isinstance(undo.get("arguments"), Mapping)
        or (isinstance(snapshot, Mapping) and isinstance(snapshot.get("pre"), int))
    )


def _summary(registry: ManifestRegistry | None, tool: str, operation: str) -> str:
    manifest = registry.get(tool) if registry is not None else None
    op = manifest.operation(operation) if manifest is not None else None
    return _(op.summary) if op is not None else f"{tool}.{operation}"


def title(action: Action, registry: ManifestRegistry | None = None) -> str:
    """«Gloxi змінив файл zvit.odt»: what happened, in plain language."""
    done = DONE.get((action.tool, action.operation))
    if action.decision == "denied" or action.ok is False or done is None:
        what = _summary(registry, action.tool, action.operation)
        if action.decision == "denied":
            text = _(ASKED).format(what=what[:1].lower() + what[1:])
        else:
            text = what
    else:
        text = _(done)
    name = object_name(action)
    return f"{text}: {name}" if name else text


def object_name(action: Action) -> str:
    if not action.resources:
        return ""
    first = PurePosixPath(action.resources[0]).name or action.resources[0]
    more = action.resource_count - 1
    return first if more <= 0 else _("{name} and {n} more").format(name=first, n=more)


def outcome_text(action: Action) -> str:
    if action.decision is None:
        return _("No answer recorded")
    if action.decision == "denied":
        return {
            "denied_user": _("You said no"),
            "denied_forbidden": _("Not allowed at all"),
            "denied_timeout": _("Nobody answered in time"),
        }.get(action.code or "", _("Herkos did not allow it"))
    if action.ok is False:
        return _("Allowed, but the tool failed")
    if action.undone_by:
        return _("Done, then undone")
    return _("Done")


def time_text(when: datetime) -> str:
    return when.astimezone().strftime("%H:%M")


def day_title(day: date, today: date) -> str:
    if day == today:
        return _("Today")
    if day == today - timedelta(days=1):
        return _("Yesterday")
    month = _(MONTHS[day.month - 1])
    text = _("{day} {month}").format(day=day.day, month=month)
    return text if day.year == today.year else f"{text} {day.year}"


def by_day(items: Iterable[Action], category: str = "all") -> list[tuple[date, list[Action]]]:
    """Newest day first; actions of one category, or all."""
    out: dict[date, list[Action]] = {}
    for a in items:
        if category == "all" or a.category == category:
            out.setdefault(a.when.astimezone().date(), []).append(a)
    return sorted(out.items(), reverse=True)


# -- permissions ----------------------------------------------------------------


@dataclass(frozen=True)
class Permission:
    grant_id: str
    tool: str
    operation: str
    roots: tuple[str, ...]
    level: str
    duration: str  # "session" or "permanent"
    created: float | None = None


def permissions(items: Iterable[Any]) -> list[Permission]:
    out = []
    for p in items:
        if not isinstance(p, Mapping):
            continue
        gid, tool, op = p.get("grant_id", p.get("id")), p.get("tool"), p.get("operation")
        level, duration = p.get("level"), p.get("duration")
        roots = p.get("roots")
        if not all(isinstance(x, str) for x in (gid, tool, op, level, duration)):
            continue
        roots = tuple(r for r in roots if isinstance(r, str)) if isinstance(roots, list) else ()
        created = p.get("created")
        out.append(
            Permission(
                gid,
                tool,
                op,
                roots,
                level,
                duration,
                created if isinstance(created, int | float) else None,
            )
        )
    # Lasting ones first, then by what they allow.
    return sorted(out, key=lambda p: (p.duration != "permanent", p.tool, p.operation))


def permission_title(p: Permission, registry: ManifestRegistry | None = None) -> str:
    return _summary(registry, p.tool, p.operation)


def permission_subtitle(p: Permission) -> str:
    how = _("Always") if p.duration == "permanent" else _("Until the session ends")
    if not p.roots:
        return how
    where = ", ".join(PurePosixPath(r).name or r for r in p.roots)
    return _("{how} · in {where}").format(how=how, where=where)


# -- talking to Herkos ----------------------------------------------------------


class HerkosUnreachable(Exception):
    """Herkos is not running, or does not serve this account."""


class Connection:
    """One JSON-RPC connection to the socket, line by line (protocol.md)."""

    def __init__(self, path: Path, timeout: float = 300.0) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(str(path))
        except OSError as e:
            self.sock.close()
            raise HerkosUnreachable(f"{path}: {e}") from None
        self.file = self.sock.makefile("rwb")
        self.next_id = 0
        self.hello = self.call(
            "herkos.hello", {"protocol": [PROTOCOL_VERSION], "client": "herkos-permissions"}
        )

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        try:
            self.file.write(json.dumps(msg, ensure_ascii=False).encode() + b"\n")
            self.file.flush()
            line = self.file.readline()
        except OSError as e:
            raise HerkosUnreachable(str(e)) from None
        if not line:
            raise HerkosUnreachable("Herkos closed the connection")
        try:
            answer = json.loads(line)
        except ValueError:
            raise HerkosUnreachable("Herkos sent something that is not JSON") from None
        if "error" in answer:
            raise HerkosUnreachable(str(answer["error"].get("message", answer["error"])))
        return answer.get("result")

    def close(self) -> None:
        try:
            self.file.close()
        finally:
            self.sock.close()


class SystemHerkos:
    """The system ``herkosd``: everything through the person's own connection."""

    def __init__(self, path: Path = SYSTEM_SOCKET) -> None:
        self.path = path

    def _call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        c = Connection(self.path)
        try:
            if not isinstance(c.hello, Mapping) or c.hello.get("role") != "agent":
                raise HerkosUnreachable(f"{self.path}: this account is not served")
            return c.call(method, params)
        finally:
            c.close()

    def permissions(self) -> list[Permission]:
        answer = self._call("herkos.permissions")
        return permissions(answer.get("permissions", []) if isinstance(answer, Mapping) else [])

    def revoke(self, grant_id: str) -> bool:
        answer = self._call("herkos.revoke", {"grant_id": grant_id})
        return isinstance(answer, Mapping) and answer.get("revoked") is True

    def records(self) -> list[Any]:
        answer = self._call("herkos.journal", {"limit": JOURNAL_LIMIT})
        recs = answer.get("records") if isinstance(answer, Mapping) else None
        return recs if isinstance(recs, list) else []

    def undo(self, action_id: str) -> dict[str, Any]:
        answer = self._call("herkos.undo", {"action_id": action_id})
        return answer if isinstance(answer, dict) else {}


class LiteHerkos:
    """Herkos-lite: its files are the person's own, and undo goes through its socket."""

    def __init__(self, state_dir: Path | None = None, socket: Path | None = None) -> None:
        from herkos.config import load_config, socket_path

        self.state_dir = state_dir or load_config().state_dir
        self.socket = socket
        self._socket_path = socket_path

    def permissions(self) -> list[Permission]:
        from herkos.grants import GrantStore

        store = GrantStore(self.state_dir / "permissions.json")
        # Session permissions live in the running Herkos-lite and end with it.
        return permissions({**g.__dict__, "roots": list(g.roots)} for g in store.all())

    def revoke(self, grant_id: str) -> bool:
        from herkos.grants import GrantStore

        return GrantStore(self.state_dir / "permissions.json").revoke(grant_id)

    def records(self) -> list[Any]:
        from herkos.mitos import read_records

        path = self.state_dir / "journal.jsonl"
        if not path.exists():
            return []
        return [rec for _, rec in deque(read_records(path), maxlen=JOURNAL_LIMIT)]

    def undo(self, action_id: str) -> dict[str, Any]:
        # A clean connection: Gloxi's own may be tainted, and undo there is R3.
        c = Connection(self.socket or self._socket_path())
        try:
            answer = c.call("herkos.undo", {"action_id": action_id})
        finally:
            c.close()
        return answer if isinstance(answer, dict) else {}


def find_herkos(system_socket: Path = SYSTEM_SOCKET) -> SystemHerkos | LiteHerkos:
    """The system Herkos when it serves this account, else Herkos-lite's files."""
    if system_socket.exists():
        try:
            c = Connection(system_socket, timeout=5.0)
        except HerkosUnreachable:
            pass
        else:
            role = c.hello.get("role") if isinstance(c.hello, Mapping) else None
            c.close()
            if role == "agent":
                return SystemHerkos(system_socket)
    return LiteHerkos()


def undo_message(answer: Mapping[str, Any]) -> str:
    code = answer.get("code")
    if code == "allowed":
        return _("Undone. The earlier version is back.")
    if code == "denied_user":
        return _("Not undone: you said no.")
    if code == "denied_unknown":
        return _("There is nothing left to undo for this action.")
    return _("Not undone: {why}").format(why=answer.get("message", code))
