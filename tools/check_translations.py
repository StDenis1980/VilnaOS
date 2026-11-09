#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fail when a Vilna program is not fully translated into the main languages (ADR 0009).

    python3 tools/check_translations.py

English is the source language. Every other main language must be complete:
the alpha ships all ten (decision of 2026-11-09), so an empty, fuzzy or
missing translation is an error, not a fallback to English.

- The .po catalogues of Herkos, Backups and the installer: each language has
  a catalogue with exactly the messages of the Ukrainian one (whose tests
  check that it covers every message the code shows), every message
  translated, not fuzzy, with the same {placeholders}.
- Desktop files: Comment, Keywords and GenericName, and Name when it is
  translated at all (a brand name such as Gloxi is not), in every language.
- AppStream metainfo: the summary and every paragraph and list item of the
  description (release notes may stay in English, as on Flathub).

Gloxi's own catalogues are checked by gloxi/tools/i18n.py in the gloxi job.
Stdlib only, so it runs anywhere CI does.
"""

from __future__ import annotations

import ast
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LANGUAGES = ("uk", "es", "pl", "de", "fr", "it", "ro", "cs", "lt")
REFERENCE = "uk"
CATALOGUES = (
    ROOT / "herkos" / "src" / "herkos" / "locale",
    ROOT / "backup" / "src" / "vilna_backup" / "locale",
    ROOT / "installer" / "src" / "vilna_installer" / "locale",
)
DESKTOP_KEYS = ("Name", "GenericName", "Comment", "Keywords")
# Name may stay untranslated (a brand name); the others never.
ALWAYS = {"GenericName", "Comment", "Keywords"}
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
SKIP_DIRS = {".git", ".venv", "node_modules", "target", "tests"}


def parse_po(text: str) -> dict[str, tuple[str, bool]]:
    """msgid → (msgstr, fuzzy) for every entry, translated or not; the header is left out."""
    entries: dict[str, tuple[str, bool]] = {}
    msgid: list[str] | None = None
    msgstr: list[str] | None = None
    current: list[str] | None = None
    fuzzy = False

    def flush() -> None:
        if msgid is not None and msgstr is not None and "".join(msgid):
            entries["".join(msgid)] = ("".join(msgstr), fuzzy)

    for raw in text.splitlines() + [""]:
        line = raw.strip()
        if not line:
            flush()
            msgid = msgstr = current = None
            fuzzy = False
        elif line.startswith("#,"):
            fuzzy = fuzzy or "fuzzy" in line
        elif line.startswith("#"):
            continue
        elif line.startswith("msgid "):
            if msgid is not None:
                flush()
                msgstr, fuzzy = None, False
            msgid = current = [ast.literal_eval(line[6:])]
        elif line.startswith("msgstr "):
            msgstr = current = [ast.literal_eval(line[7:])]
        elif line.startswith('"') and current is not None:
            current.append(ast.literal_eval(line))
    return entries


def _rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def placeholders(text: str) -> list[str]:
    return sorted(re.findall(r"\{[^{}]*\}", text))


def check_catalogues(folder: Path) -> list[str]:
    rel = _rel(folder)
    ref_path = folder / f"{REFERENCE}.po"
    if not ref_path.is_file():
        return [f"{rel}: no {REFERENCE}.po to compare with"]
    reference = parse_po(ref_path.read_text(encoding="utf-8"))
    problems = []
    for lang in LANGUAGES:
        path = folder / f"{lang}.po"
        if not path.is_file():
            problems.append(f"{rel}/{lang}.po: missing (ADR 0009 main language)")
            continue
        entries = parse_po(path.read_text(encoding="utf-8"))
        for msgid in sorted(reference.keys() - entries.keys()):
            problems.append(f"{rel}/{lang}.po: no entry for {msgid!r}")
        for msgid in sorted(entries.keys() - reference.keys()):
            problems.append(f"{rel}/{lang}.po: {msgid!r} is not in {REFERENCE}.po")
        for msgid, (msgstr, fuzzy) in entries.items():
            if not msgstr.strip():
                problems.append(f"{rel}/{lang}.po: not translated: {msgid!r}")
            elif fuzzy:
                problems.append(f"{rel}/{lang}.po: fuzzy: {msgid!r}")
            elif placeholders(msgid) != placeholders(msgstr):
                problems.append(f"{rel}/{lang}.po: placeholders differ in {msgid!r}")
    return problems


def files(pattern: str) -> list[Path]:
    return sorted(p for p in ROOT.rglob(pattern) if not SKIP_DIRS & set(p.relative_to(ROOT).parts))


def check_desktop(path: Path) -> list[str]:
    rel = _rel(path)
    values: dict[str, dict[str | None, str]] = {}
    group = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("["):
            group = line.strip()
            continue
        m = re.match(r"([A-Za-z]+)(?:\[([A-Za-z_@]+)\])?=(.*)", line)
        if group == "[Desktop Entry]" and m and m.group(1) in DESKTOP_KEYS:
            values.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
    problems = []
    for key, by_lang in values.items():
        if key not in ALWAYS and set(by_lang) == {None}:
            continue  # an untranslated brand name
        for lang in LANGUAGES:
            if not by_lang.get(lang, "").strip():
                problems.append(f"{rel}: no {key}[{lang}]")
    return problems


def check_metainfo(path: Path) -> list[str]:
    rel = _rel(path)
    root = ET.parse(path).getroot()
    problems = []
    groups = [("summary", root.findall("summary"))]
    description = root.find("description")
    if description is not None:
        # A translated paragraph follows its English one: group them by position.
        current: list[ET.Element] = []
        for el in description.iter():
            if el.tag not in ("p", "li"):
                continue
            if el.get(XML_LANG) is None:
                current = [el]
                groups.append(
                    (f"description {el.tag} {' '.join((el.text or '').split())[:40]!r}", current)
                )
            else:
                current.append(el)
    for what, elements in groups:
        have = {el.get(XML_LANG) for el in elements if (el.text or "").strip()}
        for lang in LANGUAGES:
            if lang not in have:
                problems.append(f"{rel}: {what} has no xml:lang={lang!r}")
    return problems


def main() -> int:
    problems: list[str] = []
    for folder in CATALOGUES:
        problems += check_catalogues(folder)
    for path in files("*.desktop") + files("*.desktop.in"):
        problems += check_desktop(path)
    for path in files("*.metainfo.xml"):
        problems += check_metainfo(path)
    for p in problems:
        print(f"translations: {p}", file=sys.stderr)
    if not problems:
        print(f"translations: complete in {', '.join(LANGUAGES)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
