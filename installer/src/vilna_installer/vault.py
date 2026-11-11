# SPDX-License-Identifier: GPL-3.0-or-later
"""What the person typed into protected fields, and the disk's recovery key.

Kept in the memory of the installer process only, never in the plan, never
on disk and never in the model's context (image/docs/installer.md, 1.5).
The plan holds only the marker ``"entered"``. The conversation and the
classic wizard share one ``Vault`` in the same process; the Herkos screen
adds the recovery key it showed; the installation engine takes each secret
once.
"""

from __future__ import annotations

# Which secrets there are: the Wi-Fi password, the account password, the
# disk passphrase, and the recovery key of the encrypted disk.
NAMES = ("network", "user", "passphrase", "recovery_key")


class Vault:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def put(self, which: str, value: str) -> None:
        if which not in NAMES:
            raise ValueError(f"unknown secret {which!r}; one of {', '.join(NAMES)}")
        self._values[which] = value

    def has(self, which: str) -> bool:
        return which in self._values

    def peek(self, which: str) -> str | None:
        """For a protected field of the installer itself (the Herkos screen)."""
        return self._values.get(which)

    def take(self, which: str) -> str | None:
        """For the installation engine: hands the secret over once and forgets it."""
        return self._values.pop(which, None)

    def forget(self, which: str) -> None:
        self._values.pop(which, None)

    def __repr__(self) -> str:  # never the values, even in a traceback or a log
        return f"Vault({sorted(self._values)})"
