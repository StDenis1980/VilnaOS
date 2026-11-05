# SPDX-License-Identifier: GPL-3.0-or-later
"""«Programs for Gloxi»: catalogue programs the person turns on or off (ADR 0017, step 6).

Herkos keeps the switches (``herkos.programs``, ``herkos.set_program``):
turning a program on, Herkos asks the person in its own window first, so
neither Gloxi nor the model can turn one on alone. An off program is not
started and its actions are refused. The page also shows the actions Herkos
hides from the model, and why (step 4). This module has no GTK in it: the
window calls it from a worker thread.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from gettext import gettext as _
from typing import Any

METHOD_NOT_FOUND = -32601


@dataclass(frozen=True)
class Hidden:
    operation: str
    why: tuple[str, ...]


@dataclass(frozen=True)
class Program:
    tool: str
    title: str
    enabled: bool
    running: bool
    hidden: tuple[Hidden, ...] = ()

    def status(self) -> str:
        if not self.enabled:
            return _("Off: Gloxi cannot use it")
        if not self.running:
            return _("On: starts when Herkos starts again")
        return _("On")


@dataclass(frozen=True)
class Change:
    """What Herkos answered to a switch."""

    enabled: bool
    refused: bool = False  # the person said no in the Herkos window
    restart_needed: bool = False
    error: str | None = None


def _herkos_text(text: str) -> str:
    """Herkos's reasons, in the person's language when Herkos has them translated."""
    try:
        from herkos.i18n import _ as herkos_
    except ImportError:
        return text
    return herkos_(text)


def _program(raw: Any) -> Program | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("tool"), str):
        return None
    hidden = []
    for h in raw.get("hidden") or []:
        if isinstance(h, dict) and isinstance(h.get("operation"), str):
            why = tuple(_herkos_text(w) for w in h.get("why") or [] if isinstance(w, str))
            hidden.append(Hidden(h["operation"], why))
    return Program(
        tool=raw["tool"],
        title=str(raw.get("description") or raw["tool"]),
        enabled=raw.get("enabled") is True,
        running=raw.get("running") is True,
        hidden=tuple(hidden),
    )


class Programs:
    """Each call on a short connection of its own: it carries no conversation."""

    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect

    def list(self) -> list[Program]:
        """The catalogue programs; none when Herkos is missing or too old to say."""
        from herkos.client import HerkosError, HerkosUnavailable

        try:
            client = self._connect()
        except HerkosUnavailable:
            return []
        try:
            raw = client.programs()
        except (HerkosError, HerkosUnavailable):
            return []
        finally:
            client.close()
        return [p for p in (_program(r) for r in raw) if p is not None]

    def set(self, tool: str, on: bool) -> Change:
        from herkos.client import HerkosError, HerkosUnavailable

        reason = _("You turned this program on in Gloxi's settings")
        try:
            client = self._connect()
        except HerkosUnavailable as exc:
            return Change(not on, error=str(exc))
        try:
            answer = client.set_program(tool, on, reason if on else "")
        except (HerkosError, HerkosUnavailable) as exc:
            return Change(not on, error=str(exc))
        finally:
            client.close()
        enabled = answer.get("enabled") is True
        return Change(
            enabled=enabled,
            refused=answer.get("code") == "denied_user",
            restart_needed=answer.get("restart_needed") is True,
        )


def programs(enabled: bool) -> Programs | None:
    """Programs wired to the real Herkos, or None if tools are off or Herkos is missing."""
    if not enabled:
        return None
    try:
        from herkos.client import HerkosClient
        from herkos.config import ConfigError, socket_path
    except ImportError:
        return None
    try:
        sock = socket_path()
    except ConfigError:
        return None
    return Programs(lambda: HerkosClient.connect(sock))
