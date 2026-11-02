# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

import pytest

from herkos.config import Config
from herkos.confirm import Answer, Scripted
from herkos.gate import Gate
from herkos.grants import GrantStore
from herkos.manifest import ManifestRegistry
from herkos.mitos import Journal
from herkos.request import ActionRequest
from herkos.tools import ToolError, ToolRegistry


class FakeFiles:
    """Stands in for the files MCP server."""

    def __init__(self, annotations: dict | None = None) -> None:
        self.calls: list[ActionRequest] = []
        self._annotations = annotations
        self.fail = False

    def annotations(self, operation: str):
        return self._annotations

    def execute(self, request: ActionRequest):
        if self.fail:
            raise ToolError("disk full")
        self.calls.append(request)
        return {"ok": True, "operation": request.operation}


class Env:
    def __init__(self, tmp: Path, answer: Answer = Answer.DENY) -> None:
        self.home = tmp / "home"
        self.docs = self.home / "Documents"
        self.docs.mkdir(parents=True)
        self.other = self.home / "Private"
        self.other.mkdir()
        self.config = Config(allowed_roots=(self.docs,), bulk_threshold=3, state_dir=tmp / "state")
        self.confirmer = Scripted(answer)
        self.files = FakeFiles()
        tools = ToolRegistry()
        tools.register("org.vilna.mcp.files", self.files)
        self.grants = GrantStore(self.config.grants_path)
        self.journal = Journal(self.config.journal_path)
        self.gate = Gate(
            self.config,
            ManifestRegistry.builtin(),
            tools,
            self.grants,
            self.journal,
            self.confirmer,
        )

    def uri(self, path: Path) -> str:
        return path.as_uri()


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)
