# SPDX-License-Identifier: GPL-3.0-or-later
"""Risk levels R0-R4 (permissions.md, section 4)."""

from __future__ import annotations

from enum import IntEnum


class Level(IntEnum):
    R0 = 0  # neutral
    R1 = 1  # reading personal data
    R2 = 2  # reversible changes
    R3 = 3  # hard to reverse or system-wide
    R4 = 4  # irreversible, or acting outwards on the user's behalf

    @classmethod
    def parse(cls, text: str) -> Level:
        try:
            return cls[text.strip().upper()]
        except KeyError:
            raise ValueError(f"unknown risk level: {text!r}") from None

    def raised(self, steps: int = 1) -> Level:
        """Return this level raised by ``steps``, capped at R4."""
        return Level(min(self + steps, Level.R4))

    def at_least(self, other: Level) -> Level:
        return Level(max(self, other))
