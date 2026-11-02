# SPDX-License-Identifier: GPL-3.0-or-later
"""Asking the user (permissions.md 5.2).

The question is shown by Herkos, never by Gloxi, so the model cannot change
its wording or answer it. Herkos-lite has two ways to ask: on the terminal
Herkos itself runs in, or not at all (every question is a "no"). The GTK
dialog comes with Gloxi 0.1's window. No answer within the timeout is a "no".
"""

from __future__ import annotations

import os
import select
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TextIO

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
}


def render(question: Question) -> str:
    lines = [
        "",
        "─── Herkos: Gloxi asks for permission ───",
        f"  {question.summary}  ({question.level.name})",
        f"  Gloxi's reason: {question.reason}",
    ]
    lines += [f"    • {r}" for r in question.resources[:10]]
    if len(question.resources) > 10:
        lines.append(f"    … and {len(question.resources) - 10} more")
    lines.append("  Can be undone." if question.undoable else "  Cannot be undone.")
    for why in question.raises:
        lines.append(f"  Riskier than usual: {why}.")
    if question.taint_sources:
        lines.append("  Untrusted content came from: " + ", ".join(question.taint_sources))
    options = [_LABELS[c] for c in question.choices] + ["[n] deny"]
    lines.append("  " + "   ".join(options))
    return "\n".join(lines) + "\n> "


class Terminal:
    """Ask on Herkos's own controlling terminal (not on Gloxi's)."""

    def __init__(self, tty_path: str = "/dev/tty") -> None:
        self.tty_path = tty_path

    def ask(self, question: Question, timeout: float) -> Answer:
        try:
            fd = os.open(self.tty_path, os.O_RDWR | os.O_NOCTTY)
        except OSError:
            return Answer.DENY
        with os.fdopen(fd, "r+", encoding="utf-8", buffering=1) as tty:
            return self._ask(tty, question, timeout)

    @staticmethod
    def _ask(tty: TextIO, question: Question, timeout: float) -> Answer:
        tty.write(render(question))
        tty.flush()
        ready, _, _ = select.select([tty], [], [], timeout)
        if not ready:
            tty.write("\n  No answer: denied.\n")
            return Answer.DENY
        key = tty.readline().strip().lower()[:1]
        answer = _KEYS.get(key, Answer.DENY)
        if answer not in question.choices:
            answer = Answer.DENY
        tty.write(f"  {'Denied' if answer is Answer.DENY else 'Allowed'}.\n")
        return answer


def default_confirmer() -> Confirmer:
    return Terminal() if sys.stdin.isatty() else DenyAll()
