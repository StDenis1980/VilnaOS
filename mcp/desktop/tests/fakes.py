# SPDX-License-Identifier: Apache-2.0
"""Fake accessibility trees and buses for the tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from vilna_mcp_desktop.mcp import ToolError


@dataclass
class FakeNode:
    role_: str
    name_: str = ""
    pid_: int = 0
    kids: list[FakeNode] = field(default_factory=list)
    states_: set[str] = field(default_factory=lambda: {"showing", "enabled"})
    text_: str | None = None
    editable_: bool = False
    actions_: list[str] = field(default_factory=list)
    pressed: list[str] = field(default_factory=list)

    def role(self):
        return self.role_

    def name(self):
        return self.name_

    def pid(self):
        return self.pid_

    def child_count(self):
        return len(self.kids)

    def child(self, i):
        return self.kids[i] if i < len(self.kids) else None

    def states(self):
        return set(self.states_)

    def text(self):
        return self.text_

    def editable(self):
        return self.editable_

    def actions(self):
        return list(self.actions_)

    def do_action(self, i):
        self.pressed.append(self.actions_[i])
        return True

    def set_text(self, value):
        self.text_ = value
        return True


class FakeBus:
    """Answers from a table; records every call."""

    def __init__(self, answers: dict, names: list[str] | None = None) -> None:
        self.answers = answers
        self._names = names or []
        self.calls: list[tuple] = []

    def call(self, bus, name, path, interface, method, signature, args, reply):
        self.calls.append((bus, name, path, f"{interface}.{method}", args))
        answer = self.answers.get(f"{interface}.{method}", ())
        if isinstance(answer, Exception):
            raise answer
        return answer(args) if callable(answer) else answer

    def names(self, bus):
        return list(self._names)


def raise_tool_error(message: str):
    def fail(_args):
        raise ToolError(message)

    return fail
