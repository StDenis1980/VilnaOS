#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_translations.py.

Run: python3 tools/test_check_translations.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_translations as ct  # noqa: E402 (after sys.path.insert above)

HEADER = 'msgid ""\nmsgstr ""\n"Language: {lang}\\n"\n\n'


def po(lang: str, entries: dict[str, str], fuzzy: str | None = None) -> str:
    out = HEADER.format(lang=lang)
    for msgid, msgstr in entries.items():
        if msgid == fuzzy:
            out += "#, fuzzy\n"
        out += f'msgid "{msgid}"\nmsgstr "{msgstr}"\n\n'
    return out


class Catalogues(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.full = {"Send": "x", "{n} files": "{n} x"}
        for lang in ct.LANGUAGES:
            (self.dir / f"{lang}.po").write_text(po(lang, self.full), encoding="utf-8")

    def write(self, lang: str, text: str) -> None:
        (self.dir / f"{lang}.po").write_text(text, encoding="utf-8")

    def test_complete_catalogues_pass(self):
        self.assertEqual(ct.check_catalogues(self.dir), [])

    def test_empty_translation_fails(self):
        self.write("de", po("de", {"Send": "", "{n} files": "{n} Dateien"}))
        problems = ct.check_catalogues(self.dir)
        self.assertEqual(len(problems), 1)
        self.assertIn("de.po: not translated: 'Send'", problems[0])

    def test_fuzzy_missing_and_broken_placeholders_fail(self):
        self.write("fr", po("fr", self.full, fuzzy="Send"))
        self.write("lt", po("lt", {"Send": "Siųsti"}))
        self.write("pl", po("pl", {"Send": "Wyślij", "{n} files": "pliki"}))
        (self.dir / "cs.po").unlink()
        text = "\n".join(ct.check_catalogues(self.dir))
        self.assertIn("fr.po: fuzzy: 'Send'", text)
        self.assertIn("lt.po: no entry for '{n} files'", text)
        self.assertIn("pl.po: placeholders differ in '{n} files'", text)
        self.assertIn("cs.po: missing", text)

    def test_extra_entries_are_allowed(self):
        self.write("es", po("es", {**self.full, "Read the calendar": "Leer el calendario"}))
        self.assertEqual(ct.check_catalogues(self.dir), [])

    def test_multiline_msgstr_counts_as_translated(self):
        self.write(
            "it",
            HEADER.format(lang="it")
            + 'msgid "Send"\nmsgstr ""\n"Invia"\n\nmsgid "{n} files"\nmsgstr "{n} file"\n',
        )
        self.assertEqual(ct.check_catalogues(self.dir), [])


class DesktopAndMetainfo(unittest.TestCase):
    def test_desktop_needs_every_comment_but_not_a_brand_name(self):
        path = Path(tempfile.mkdtemp()) / "a.desktop"
        lines = ["[Desktop Entry]", "Name=Gloxi", "Comment=Hi", "Keywords=a;"]
        lines += [f"Comment[{lang}]=x" for lang in ct.LANGUAGES]
        lines += [f"Keywords[{lang}]=x;" for lang in ct.LANGUAGES if lang != "ro"]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(ct.check_desktop(path), [f"{path.as_posix()}: no Keywords[ro]"])

    def test_desktop_translated_name_must_be_complete(self):
        path = Path(tempfile.mkdtemp()) / "a.desktop"
        path.write_text("[Desktop Entry]\nName=Backups\nName[uk]=Копії\n", encoding="utf-8")
        self.assertEqual(len(ct.check_desktop(path)), len(ct.LANGUAGES) - 1)

    def test_metainfo_paragraph_without_a_language_fails(self):
        path = Path(tempfile.mkdtemp()) / "a.metainfo.xml"
        langs = [lang for lang in ct.LANGUAGES if lang != "cs"]
        summary = "".join(f'<summary xml:lang="{lang}">s</summary>' for lang in ct.LANGUAGES)
        para = "".join(f'<p xml:lang="{lang}">p</p>' for lang in langs)
        release = (
            "<releases><release><description><p>Only English</p></description></release></releases>"
        )
        path.write_text(
            f"<component><summary>S</summary>{summary}<description><p>P</p>{para}</description>{release}</component>",
            encoding="utf-8",
        )
        problems = ct.check_metainfo(path)
        self.assertEqual(len(problems), 1)
        self.assertIn("xml:lang='cs'", problems[0])


class Repository(unittest.TestCase):
    def test_the_repository_is_complete(self):
        self.assertEqual(ct.main(), 0)


if __name__ == "__main__":
    unittest.main()
