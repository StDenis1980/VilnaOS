# SPDX-License-Identifier: GPL-3.0-or-later
"""Interface language (ADR 0009).

Gloxi's own strings are English in the source and translated with gettext
(domain ``gloxi``). The catalogues live in ``gloxi/po`` and are compiled into
``gloxi/locale`` inside the package by ``tools/i18n.py``. The language comes
from the usual environment (``LANGUAGE``, ``LC_ALL``, ``LC_MESSAGES``,
``LANG``), as in every GNOME application.

Modules use ``from gettext import gettext as _`` and call it when the text
is shown, so ``setup()`` only has to run once at start-up. ``N_`` marks a
string for translation where it is defined, for constants.
"""

from __future__ import annotations

import gettext
from pathlib import Path

DOMAIN = "gloxi"
LOCALE_DIR = Path(__file__).resolve().parent / "locale"


def N_(text: str) -> str:  # noqa: N802 (gettext convention)
    return text


def setup() -> None:
    """Use the catalogues shipped in the package, or the system ones."""
    if LOCALE_DIR.is_dir():
        gettext.bindtextdomain(DOMAIN, str(LOCALE_DIR))
    gettext.textdomain(DOMAIN)
