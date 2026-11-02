# SPDX-License-Identifier: GPL-3.0-or-later
"""The permission gate: one way in for every action of Gloxi (permissions.md 3).

Steps for each request:

1. Parse it and find the manifest (unknown means denied).
2. Assess the level (``policy``); taint is Herkos's own session state.
3. Look for a permission the user gave, or ask the user.
4. Write the request and the decision to the journal. If the journal cannot
   be written, nothing is carried out.
5. Carry it out through the tool and journal the result.

Every failure along the way is a denial, never an allowance.
"""

from __future__ import annotations

import posixpath
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from herkos.config import Config
from herkos.confirm import Answer, Confirmer, Question, choices_for
from herkos.grants import GrantStore
from herkos.manifest import ManifestRegistry, Operation
from herkos.mitos import Journal, JournalError, digest, read_records
from herkos.policy import Assessment, SessionState, assess
from herkos.request import ActionRequest, parse_request
from herkos.risk import Level
from herkos.tools import ToolError, ToolRegistry

JOURNAL_MAX_RESOURCES = 50


def undo_info(request: ActionRequest, op: Operation, result: Any) -> dict | None:
    """What Mitos needs to undo a change later: the tool's backup id and the file."""
    if not op.undoable or not isinstance(result, dict):
        return None
    backup_id = result.get("backup_id")
    path = request.arguments.get(op.resource_args[0]) if op.resource_args else None
    if isinstance(backup_id, str) and backup_id.isalnum() and isinstance(path, str):
        return {"backup_id": backup_id, "path": posixpath.normpath(path)}
    return None


@dataclass(frozen=True)
class Outcome:
    action_id: str
    allowed: bool
    level: Level | None
    # Machine-readable (herkos/docs/protocol.md): allowed, tool_error (allowed,
    # but the tool failed), denied_unknown, denied_forbidden, denied_level,
    # denied_user, denied_no_tool, denied_mismatch, denied_journal, denied_error.
    code: str
    message: str
    result: Any = None
    # For results with external content: where it came from and what Keros saw.
    keros: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "action_id": self.action_id,
            "decision": "allowed" if self.allowed else "denied",
            "code": self.code,
            "level": self.level.name if self.level is not None else "forbidden",
            "message": self.message,
        }
        if self.code == "allowed":
            out["result"] = self.result
            if self.keros is not None:
                out["keros"] = self.keros
        return out


def undeclared_resource(request: ActionRequest, op: Operation) -> str | None:
    """Name of the first resource argument that is not a declared file resource."""
    declared = {r.path for r in request.resources if r.path is not None}
    for name in op.resource_args:
        value = request.arguments.get(name)
        if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
            return name
        if posixpath.normpath(value) not in declared:
            return name
    return None


def new_action_id() -> str:
    # Time-ordered and unique enough for one journal: ms timestamp + random.
    return f"{int(time.time() * 1000):013x}{secrets.token_hex(6)}"


class Gate:
    def __init__(
        self,
        config: Config,
        manifests: ManifestRegistry,
        tools: ToolRegistry,
        grants: GrantStore,
        journal: Journal,
        confirmer: Confirmer,
    ) -> None:
        self.config = config
        self.manifests = manifests
        self.tools = tools
        self.grants = grants
        self.journal = journal
        self.confirmer = confirmer
        # One question on screen at a time.
        self._ask_lock = threading.Lock()

    def handle(self, request: ActionRequest, session: SessionState) -> Outcome:
        action_id = new_action_id()
        try:
            self.journal.append("request", self._describe(request, session), action_id)
        except JournalError as e:
            return Outcome(action_id, False, None, "denied_journal", f"journal unavailable: {e}")

        try:
            outcome = self._decide(action_id, request, session)
        except JournalError as e:
            outcome = Outcome(action_id, False, None, "denied_journal", str(e))
        except Exception as e:  # any bug in deciding is a denial
            outcome = Outcome(
                action_id, False, None, "denied_error", f"internal error: {type(e).__name__}"
            )
        try:
            self.journal.append(
                "decision",
                {
                    "decision": "allowed" if outcome.allowed else "denied",
                    "code": outcome.code,
                    "level": outcome.level.name if outcome.level is not None else "forbidden",
                    "message": outcome.message,
                },
                action_id,
            )
        except JournalError as e:
            return Outcome(action_id, False, outcome.level, "denied_journal", str(e))
        if not outcome.allowed:
            return outcome
        return self._execute(action_id, request, session, outcome)

    # -- deciding ----------------------------------------------------------

    def _decide(self, action_id: str, request: ActionRequest, session: SessionState) -> Outcome:
        manifest = self.manifests.get(request.tool)
        executor = self.tools.get(request.tool)
        annotations = executor.annotations(request.operation) if executor else None
        a = assess(
            request,
            manifest,
            session,
            allowed_roots=self.config.allowed_roots,
            bulk_threshold=self.config.bulk_threshold,
            annotations=annotations,
        )
        if manifest is None:
            return Outcome(
                action_id, False, a.level, "denied_unknown", f"unknown tool {request.tool}"
            )
        if a.operation is None:
            return Outcome(
                action_id,
                False,
                None,
                "denied_unknown",
                f"{request.tool} has no operation {request.operation}",
            )
        if a.forbidden:
            return Outcome(
                action_id, False, None, "denied_forbidden", "this action is never allowed"
            )
        mismatch = undeclared_resource(request, a.operation)
        if mismatch:
            return Outcome(
                action_id,
                False,
                a.level,
                "denied_mismatch",
                f"argument '{mismatch}' names something not declared in resources",
            )
        assert a.level is not None
        if a.level > self.config.max_level:
            why = "; ".join(a.raises) or "its base level"
            return Outcome(
                action_id,
                False,
                a.level,
                "denied_level",
                f"{a.level.name} actions need the system Herkos (raised by: {why})",
            )
        if executor is None:
            return Outcome(
                action_id, False, a.level, "denied_no_tool", f"{request.tool} is not running"
            )
        if a.level == Level.R0:
            return Outcome(action_id, True, a.level, "allowed", "R0: no permission needed")

        grant = self.grants.find(request.tool, request.operation, a.roots, a.level)
        if grant is not None:
            return Outcome(
                action_id, True, a.level, "allowed", f"permission {grant.id} ({grant.duration})"
            )
        return self._ask(action_id, request, session, a)

    def _ask(
        self, action_id: str, request: ActionRequest, session: SessionState, a: Assessment
    ) -> Outcome:
        assert a.level is not None and a.operation is not None
        question = Question(
            summary=a.operation.summary,
            reason=request.reason,
            tool=request.tool,
            operation=request.operation,
            resources=tuple(r.uri for r in request.resources),
            level=a.level,
            undoable=(
                a.operation.undoable or a.operation.reversible or not a.operation.changes_state
            ),
            raises=a.raises,
            taint_sources=tuple(session.taint_sources),
            choices=choices_for(a.level, a.in_bounds),
        )
        with self._ask_lock:
            try:
                answer = self.confirmer.ask(question, self.config.confirm_timeout)
            except Exception:  # a broken dialog is a "no"
                answer = Answer.DENY
        if answer is Answer.DENY or answer not in question.choices:
            return Outcome(action_id, False, a.level, "denied_user", "the user said no")
        if answer in (Answer.SESSION, Answer.ALWAYS):
            duration = "session" if answer is Answer.SESSION else "permanent"
            roots = [r for r in a.roots if r is not None]
            grant = self.grants.add(request.tool, request.operation, roots, a.level, duration)
            try:
                self.journal.append(
                    "grant",
                    {
                        "grant_id": grant.id,
                        "tool": grant.tool,
                        "operation": grant.operation,
                        "roots": list(grant.roots),
                        "level": grant.level,
                        "duration": grant.duration,
                    },
                    action_id,
                )
            except JournalError:
                self.grants.revoke(grant.id)
                raise
        return Outcome(action_id, True, a.level, "allowed", f"the user allowed ({answer.value})")

    # -- carrying out ------------------------------------------------------

    def _execute(
        self, action_id: str, request: ActionRequest, session: SessionState, outcome: Outcome
    ) -> Outcome:
        executor = self.tools.get(request.tool)
        manifest = self.manifests.get(request.tool)
        assert executor is not None and manifest is not None
        op = manifest.operation(request.operation)
        assert op is not None
        try:
            result = executor.execute(request)
            ok, code, message = True, "allowed", outcome.message
        except ToolError as e:
            result, ok, code, message = None, False, "tool_error", str(e)
        except Exception as e:  # the tool must not take the gate down with it
            name = type(e).__name__
            result, ok, code, message = None, False, "tool_error", f"tool failed: {name}"
        keros = None
        record: dict[str, Any] = {"ok": ok, "code": code, "result": digest(result) if ok else None}
        if ok and op.returns_external:
            source = f"{request.tool}.{request.operation}"
            session.taint(source)
            findings = session.keros.inspect(source, result)
            keros = {
                "source": source,
                "untrusted": True,
                "findings": [f.to_json() for f in findings],
            }
            record["keros"] = [f.rule for f in findings]
        undo = undo_info(request, op, result) if ok else None
        if undo:
            record["undo"] = undo
        try:
            self.journal.append("result", record, action_id)
        except JournalError as e:
            # Already done, but Gloxi gets nothing it cannot account for.
            return Outcome(action_id, False, outcome.level, "denied_journal", str(e))
        return Outcome(action_id, True, outcome.level, code, message, result if ok else None, keros)

    def undo(self, action_id: str, session: SessionState) -> Outcome:
        """Undo an earlier action by its journal id: a new ``restore`` request.

        The undo goes through the gate like any other action (and is
        journalled), so it is confirmed and can itself be undone.
        """
        tool, undo = None, None
        if self.config.journal_path.exists():
            for _, rec in read_records(self.config.journal_path):
                if not rec or rec.get("action_id") != action_id:
                    continue
                data = rec.get("data") or {}
                if rec.get("kind") == "request":
                    tool = data.get("tool")
                elif rec.get("kind") == "result":
                    undo = data.get("undo")
        if not tool or not isinstance(undo, dict):
            return Outcome(new_action_id(), False, None, "denied_unknown", "nothing to undo")
        request = parse_request(
            {
                "tool": tool,
                "operation": "restore",
                "resources": [Path(undo["path"]).as_uri()],
                "reason": f"Undo action {action_id}",
                "arguments": {"path": undo["path"], "backup_id": undo["backup_id"]},
            }
        )
        return self.handle(request, session)

    @staticmethod
    def _describe(request: ActionRequest, session: SessionState) -> dict[str, Any]:
        uris = [r.uri for r in request.resources]
        return {
            "tool": request.tool,
            "operation": request.operation,
            "resources": uris[:JOURNAL_MAX_RESOURCES],
            "resource_count": len(uris),
            "resources_digest": digest(uris),
            "reason": request.reason,
            "user_turn": request.user_turn,
            "arguments": digest(request.arguments),
            "tainted": session.tainted,
        }
