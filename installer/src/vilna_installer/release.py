# SPDX-License-Identifier: GPL-3.0-or-later
"""The release layer of the installer: the sign and the name of the current release.

Vilna 1 is Blavet (a kingfisher looking out of a hollow). The next
release changes ``NAME`` and the file names in ``mark``, and nothing
else in the installer windows that use this module.
"""

from __future__ import annotations

from pathlib import Path

NAME = "Blavet"
DATA = Path(__file__).resolve().parent / "data"


def mark(dark: bool) -> Path:
    """The release sign for a dark or a light background."""
    return DATA / f"blavet-mark-{'dark' if dark else 'light'}.svg"
