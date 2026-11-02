# SPDX-License-Identifier: GPL-3.0-or-later
"""Tool executors: how the gate carries out an allowed action.

In Gloxi 0.1 tools are MCP servers started by Herkos (permissions.md 9.5);
the MCP client lands together with the "files" server. Until then this is
the interface the gate calls, and tests plug in their own executors.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from herkos.request import ActionRequest


class ToolError(Exception):
    """The tool ran but failed. The message goes back to Gloxi."""


class Executor(Protocol):
    def annotations(self, operation: str) -> Mapping[str, object] | None:
        """MCP annotations the tool declares for an operation, if any."""
        ...

    def execute(self, request: ActionRequest) -> Any:
        """Carry out the action and return a JSON-serialisable result."""
        ...


class ToolRegistry:
    def __init__(self) -> None:
        self._executors: dict[str, Executor] = {}

    def register(self, tool: str, executor: Executor) -> None:
        self._executors[tool] = executor

    def get(self, tool: str) -> Executor | None:
        return self._executors.get(tool)
