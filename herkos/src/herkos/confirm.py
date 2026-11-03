# SPDX-License-Identifier: GPL-3.0-or-later
"""Asking the user (permissions.md 5.2).

The question is shown by Herkos, never by Gloxi, so the model cannot change
its wording or answer it. Herkos-lite can ask in its own GTK window
(``Dialog``, a separate process), on the terminal Herkos itself runs in, or
not at all (every question is a "no"). No answer within the timeout is a "no".
"""

from __future__ import annotations

import importlib.util
import json
import os
import select
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TextIO

from herkos.i18n import _
from herkos.risk import Level


class Answer(Enum):
    DENY = "deny"
    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"


@dataclass(frozen=True)
class Question:
    summary: str  # what will be done, in plain words
    reason: str  # why Gloxi wants it (the model's words, shown as such)
    tool: str
    operation: str
    resources: tuple[str, ...]
    level: Level
    undoable: bool
    raises: tuple[str, ...]  # why the level is higher than usual
    taint_sources: tuple[str, ...]
    choices: tuple[Answer, ...]  # what the user may pick besides DENY
    # The exact arguments, when "allow" means "allow exactly this".
    content: dict | None = None


def choices_for(level: Level, in_bounds: bool) -> tuple[Answer, ...]:
    """Durations the user may pick (permissions.md 5.1, 5.3)."""
    if level >= Level.R4:
        return (Answer.ONCE,)
    if level == Level.R3 or not in_bounds:
        return (Answer.ONCE, Answer.SESSION)
    return (Answer.ONCE, Answer.SESSION, Answer.ALWAYS)


class Confirmer(Protocol):
    def ask(self, question: Question, timeout: float) -> Answer: ...


class DenyAll:
    """No way to ask the user: every question is answered "no"."""

    def ask(self, question: Question, timeout: float) -> Answer:
        return Answer.DENY


class Scripted:
    """Answers from a list or a function. For tests only."""

    def __init__(self, answer: Answer | Callable[[Question], Answer]) -> None:
        self._answer = answer
        self.questions: list[Question] = []

    def ask(self, question: Question, timeout: float) -> Answer:
        self.questions.append(question)
        return self._answer(question) if callable(self._answer) else self._answer


_KEYS = {"o": Answer.ONCE, "s": Answer.SESSION, "a": Answer.ALWAYS}
_LABELS = {
    Answer.ONCE: "[o] allow once",
    Answer.SESSION: "[s] allow for this session",
    Answer.ALWAYS: "[a] always allow",
}  # translated when shown


def render(question: Question) -> str:
    lines = [
        "",
        f"─── {_('Herkos: Gloxi asks for permission')} ───",
        f"  {question.summary}  ({question.level.name})",
        "  " + _("Gloxi's reason: ") + question.reason,
    ]
    lines += [f"    • {r}" for r in question.resources[:10]]
    if len(question.resources) > 10:
        lines.append("    " + _("… and {n} more").format(n=len(question.resources) - 10))
    lines.append("  " + (_("Can be undone.") if question.undoable else _("Cannot be undone.")))
    for why in question.raises:
        lines.append("  " + _("Riskier than usual: {why}.").format(why=why))
    if question.taint_sources:
        src = ", ".join(question.taint_sources)
        lines.append("  " + _("Untrusted content came from: {src}").format(src=src))
    if question.content is not None:
        lines.append("  " + _("Exactly this will be done:"))
        shown = json.dumps(question.content, ensure_ascii=False, indent=2).splitlines()
        lines += [f"    {line}" for line in shown]
    options = [_(_LABELS[c]) for c in question.choices] + [_("[n] deny")]
    lines.append("  " + "   ".join(options))
    return "\n".join(lines) + "\n> "


class Terminal:
    """Ask on a terminal: Herkos's own controlling one, or one handed over.

    ``fd`` is a terminal Herkos was given when it started (Gloxi's, when
    Gloxi runs Herkos-lite as its child in a new session: such a child has no
    controlling terminal). The person types the answer; the model cannot.
    """

    def __init__(self, tty_path: str = "/dev/tty", fd: int | None = None) -> None:
        self.tty_path = tty_path
        self.fd = fd

    def ask(self, question: Question, timeout: float) -> Answer:
        try:
            if self.fd is not None:
                if not os.isatty(self.fd):
                    return Answer.DENY
                fd = os.dup(self.fd)
            else:
                fd = os.open(self.tty_path, os.O_RDWR | os.O_NOCTTY)
        except OSError:
            return Answer.DENY
        # A terminal cannot seek, so it gets a reader and a writer (not "r+").
        with (
            os.fdopen(fd, "r", encoding="utf-8") as reader,
            os.fdopen(os.dup(fd), "w", encoding="utf-8", buffering=1) as writer,
        ):
            return self._ask(reader, writer, question, timeout)

    @staticmethod
    def _ask(reader: TextIO, writer: TextIO, question: Question, timeout: float) -> Answer:
        writer.write(render(question))
        writer.flush()
        ready, _unused, _none = select.select([reader], [], [], timeout)
        if not ready:
            writer.write("\n  " + _("No answer: denied.") + "\n")
            return Answer.DENY
        key = reader.readline().strip().lower()[:1]
        answer = _KEYS.get(key, Answer.DENY)
        if answer not in question.choices:
            answer = Answer.DENY
        writer.write("  " + (_("Denied.") if answer is Answer.DENY else _("Allowed.")) + "\n")
        return answer


def question_to_json(question: Question) -> dict:
    return {
        "summary": question.summary,
        "reason": question.reason,
        "tool": question.tool,
        "operation": question.operation,
        "resources": list(question.resources),
        "level": question.level.name,
        "undoable": question.undoable,
        "raises": list(question.raises),
        "taint_sources": list(question.taint_sources),
        "choices": [c.value for c in question.choices],
        "content": question.content,
    }


class Dialog:
    """Ask in Herkos's own window: a separate process (``herkos.dialog``)."""

    def __init__(self, command: list[str] | None = None) -> None:
        self.command = command or [sys.executable, "-m", "herkos.dialog"]

    def ask(self, question: Question, timeout: float) -> Answer:
        try:
            done = subprocess.run(
                self.command,
                input=json.dumps(question_to_json(question)).encode(),
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return Answer.DENY  # run() kills the window on timeout
        lines = done.stdout.decode(errors="replace").strip().splitlines()
        try:
            answer = Answer(lines[-1].strip()) if lines else Answer.DENY
        except ValueError:
            return Answer.DENY
        return answer if answer in question.choices else Answer.DENY


def gui_available() -> bool:
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return False
    try:
        if importlib.util.find_spec("gi") is None:
            return False
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
    except (ImportError, ValueError):
        return False
    return True


def default_confirmer(tty_fd: int | None = None) -> Confirmer:
    """The window if there is a desktop, else a terminal, else always "no".

    The terminal is ``tty_fd`` if Herkos was handed one, else its own stdin's.
    """
    if gui_available():
        return Dialog()
    if tty_fd is not None and os.isatty(tty_fd):
        return Terminal(fd=tty_fd)
    return Terminal() if sys.stdin.isatty() else DenyAll()
