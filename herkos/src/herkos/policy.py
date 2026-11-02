# SPDX-License-Identifier: GPL-3.0-or-later
"""Risk assessment: the level of an action, set by Herkos alone (permissions.md 4).

The model's opinion never enters here. The level comes from the tool manifest,
may be raised by the tool's own MCP annotations, and is then raised by the
modifiers of section 4.2. Nothing lowers it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from herkos.keros import Keros
from herkos.manifest import Manifest, Operation
from herkos.request import ActionRequest, Resource
from herkos.risk import Level


@dataclass(frozen=True)
class Assessment:
    level: Level | None  # None: forbidden
    base: Level | None
    operation: Operation | None
    # Human-readable reasons for every raise, for the dialog and the journal.
    raises: tuple[str, ...] = ()
    # Allowed root that holds each file resource; None if out of bounds.
    roots: tuple[Path | None, ...] = ()
    known_tool: bool = True

    @property
    def forbidden(self) -> bool:
        return self.level is None

    @property
    def in_bounds(self) -> bool:
        return all(r is not None for r in self.roots)


@dataclass
class SessionState:
    """What Herkos knows about one Gloxi connection."""

    tainted: bool = False
    taint_sources: list[str] = field(default_factory=list)
    keros: Keros = field(default_factory=Keros)

    def taint(self, source: str) -> None:
        self.tainted = True
        if source not in self.taint_sources:
            self.taint_sources.append(source)


def resolve_path(path: str) -> Path:
    """Resolve symlinks of the longest existing prefix (the rest may not exist yet)."""
    return Path(os.path.realpath(path))


def containing_root(resource: Resource, roots: Iterable[Path]) -> Path | None:
    if resource.scheme != "file" or resource.path is None:
        return None
    real = resolve_path(resource.path)
    for root in roots:
        real_root = resolve_path(str(root))
        if real == real_root or real.is_relative_to(real_root):
            return root
    return None


def level_from_annotations(annotations: Mapping[str, object]) -> Level:
    """Map MCP tool annotations to a level. They can only raise, never lower."""
    if annotations.get("openWorldHint") is True:
        return Level.R3
    if annotations.get("destructiveHint") is True:
        return Level.R3
    if annotations.get("readOnlyHint") is True:
        return Level.R0
    return Level.R2


def assess(
    request: ActionRequest,
    manifest: Manifest | None,
    session: SessionState,
    *,
    allowed_roots: Iterable[Path],
    bulk_threshold: int,
    annotations: Mapping[str, object] | None = None,
) -> Assessment:
    allowed_roots = tuple(allowed_roots)
    roots = tuple(containing_root(r, allowed_roots) for r in request.resources)

    if manifest is None:
        # A tool without a manifest: R3 for reading, R4 for the rest (4.1.3).
        read_only = bool(annotations and annotations.get("readOnlyHint") is True)
        base = Level.R3 if read_only else Level.R4
        return Assessment(
            level=base,
            base=base,
            operation=None,
            raises=("the tool has no Herkos manifest",),
            roots=roots,
            known_tool=False,
        )

    op = manifest.operation(request.operation)
    if op is None:
        # Unknown operation of a known tool: denied outright (principle 8).
        return Assessment(level=None, base=None, operation=None, roots=roots)
    if op.forbidden:
        return Assessment(level=None, base=None, operation=op, roots=roots)

    base = op.level
    level = base
    raises: list[str] = []

    if annotations:
        hinted = level_from_annotations(annotations)
        if hinted > level:
            level = hinted
            raises.append("the tool describes this operation as riskier")

    if session.tainted and level >= Level.R2:
        level = level.raised()
        raises.append("untrusted content is in the conversation")
    if session.keros.suspicious and level >= Level.R1:
        level = level.raised()
        kinds = sorted({f.description for _, f in session.keros.findings})
        raises.append("content read earlier " + "; ".join(kinds))
    if not all(r is not None for r in roots):
        level = level.raised()
        raises.append("it touches something outside the allowed folders")
    if len(request.resources) > bulk_threshold:
        level = level.raised()
        raises.append(f"it touches more than {bulk_threshold} items at once")
    if op.changes_state and not op.undoable and level < Level.R3:
        level = Level.R3
        raises.append("it cannot be undone")
    if op.egress and level < Level.R3:
        level = Level.R3
        raises.append("data leaves this device")
    if op.egress and session.keros.leaks(request.arguments):
        level = Level.R4
        raises.append("it sends data to an address that came from untrusted content")

    return Assessment(level=level, base=base, operation=op, raises=tuple(raises), roots=roots)
