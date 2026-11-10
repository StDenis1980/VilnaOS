# SPDX-License-Identifier: GPL-3.0-or-later
"""Translations of the Backups window.

Catalogues are plain gettext ``.po`` files in ``vilna_backup/locale/``, read
directly (no compiled ``.mo`` files, no build step), as in Herkos. The
language comes from ``LANGUAGE``, ``LC_ALL``, ``LC_MESSAGES`` or ``LANG``.
The command line and the MCP server stay in English on purpose.
"""

from __future__ import annotations

import ast
import os
from importlib import resources


def parse_po(text: str) -> dict[str, str]:
    """msgid → msgstr for every translated, non-fuzzy entry of a .po file."""
    catalog: dict[str, str] = {}
    msgid: list[str] | None = None
    msgstr: list[str] | None = None
    current: list[str] | None = None
    fuzzy = False

    def flush() -> None:
        if msgid is not None and msgstr is not None and not fuzzy:
            key, value = "".join(msgid), "".join(msgstr)
            if key and value:
                catalog[key] = value

    for raw in text.splitlines() + [""]:
        line = raw.strip()
        if not line:
            flush()
            msgid = msgstr = current = None
            fuzzy = False
        elif line.startswith("#,") and "fuzzy" in line:
            fuzzy = True
        elif line.startswith("#"):
            continue
        elif line.startswith("msgid "):
            msgid, current = [ast.literal_eval(line[6:])], None
            current = msgid
        elif line.startswith("msgstr "):
            msgstr = [ast.literal_eval(line[7:])]
            current = msgstr
        elif line.startswith('"') and current is not None:
            current.append(ast.literal_eval(line))
    return catalog


def languages() -> list[str]:
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            out = []
            for part in value.split(":"):
                code = part.split(".")[0].split("@")[0]
                if code and code not in ("C", "POSIX"):
                    out += [code, code.split("_")[0]]
            return out
    return []


def load(langs: list[str] | None = None) -> dict[str, str]:
    folder = resources.files("vilna_backup.locale")
    for lang in languages() if langs is None else langs:
        po = folder / f"{lang}.po"
        if po.is_file():
            return parse_po(po.read_text(encoding="utf-8"))
    return {}


_catalog = load()


def _(message: str) -> str:
    return _catalog.get(message, message)


def use(langs: list[str]) -> None:
    """Switch language at run time (tests, or a later settings change)."""
    global _catalog
    _catalog = load(langs)
