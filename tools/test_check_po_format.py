#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_po_format.py (the format check of the .po and .pot catalogues).

Every test builds small catalogues in memory or in a temporary folder and checks them the
way ``main()`` does; there is a test for every rule and one for the good catalogues, with
Ukrainian, Lithuanian and Catalan strings.

Run: python3 tools/test_check_po_format.py
"""

from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_po_format as cp  # noqa: E402  (after sys.path.insert above)

PLURAL_FORMS = {
    "uk": "nplurals=3; plural=n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && "
    "(n%100<10 || n%100>=20) ? 1 : 2;",
    "lt": "nplurals=3; plural=n%10==1 && n%100!=11 ? 0 : "
    "n%10>=2 && (n%100<10 || n%100>=20) ? 1 : 2;",
    "ca": "nplurals=2; plural=n != 1;",
    "ca@valencia": "nplurals=2; plural=n != 1;",
}


def header(lang: str = "uk", *, plural: str | None = None, extra: str = "") -> str:
    """A good header entry for the language ``lang``."""
    forms = PLURAL_FORMS[lang] if plural is None else plural
    lines = [f"Language: {lang}\\n", "Content-Type: text/plain; charset=UTF-8\\n"]
    if forms:
        lines.append(f"Plural-Forms: {forms}\\n")
    body = "".join(f'"{line}"\n' for line in lines)
    return f'msgid ""\nmsgstr ""\n{body}{extra}\n'


def entry(msgid: str, msgstr: str, *, flags: str = "", ctx: str | None = None) -> str:
    """A singular entry; ``msgid`` and ``msgstr`` are written as they are between quotes."""
    out = f"#, {flags}\n" if flags else ""
    if ctx is not None:
        out += f'msgctxt "{ctx}"\n'
    return out + f'msgid "{msgid}"\nmsgstr "{msgstr}"\n\n'


def plural(msgid: str, msgid_plural: str, *forms: str, flags: str = "") -> str:
    out = f"#, {flags}\n" if flags else ""
    out += f'msgid "{msgid}"\nmsgid_plural "{msgid_plural}"\n'
    return out + "".join(f'msgstr[{i}] "{f}"\n' for i, f in enumerate(forms)) + "\n"


def check(text: str, path: str = "demo/po/uk.po") -> list[cp.Problem]:
    return cp.check_catalog(path, text.encode("utf-8"))


def rules(problems: list[cp.Problem], *, warning: bool | None = None) -> list[str]:
    return [p.rule for p in problems if warning is None or p.warning == warning]


class UnquoteTests(unittest.TestCase):
    def test_escapes(self) -> None:
        raw = r'"a\nb\tc\"d\\e\101\x42\x4a\r"'
        self.assertEqual(cp.unquote(raw), ('a\nb\tc"d\\eABJ\r', None))

    def test_cyrillic_and_lithuanian_text_is_kept(self) -> None:
        self.assertEqual(
            cp.unquote('"Вітаємо у Vilna, ąčęėįšųūž"'), ("Вітаємо у Vilna, ąčęėįšųūž", None)
        )

    def test_bad_strings(self) -> None:
        for raw, word in (
            ('"abc', "closed"),
            ('"a"b"', "quote"),
            ('"a\\qb"', "unknown escape"),
            ('"abc\\"', "lone backslash"),
            ('"\\xzz"', "hexadecimal"),
            ('"abc\\\\\\"', "lone backslash"),
        ):
            with self.subTest(raw=raw):
                text, err = cp.unquote(raw)
                self.assertIsNotNone(err)
                self.assertIn(word, err or "")
                self.assertEqual(text, "")


class ParseTests(unittest.TestCase):
    def test_continuation_lines_context_and_flags(self) -> None:
        cat = cp.parse(
            "# translator\n#: src/a.py\n#, fuzzy, python-brace-format\n"
            'msgctxt "menu"\nmsgid ""\n"Open "\n"{name}\\n"\nmsgstr ""\n"Відкрити "\n"{name}\\n"\n'
        )
        (one,) = cat.entries
        self.assertEqual((one.msgctxt, one.msgid), ("menu", "Open {name}\n"))
        self.assertEqual(one.msgstr, {0: "Відкрити {name}\n"})
        self.assertEqual(one.flags, {"fuzzy", "python-brace-format"})
        self.assertTrue(one.fuzzy)
        self.assertEqual(cat.syntax, [])

    def test_plural_entry(self) -> None:
        cat = cp.parse(plural("{n} file", "{n} files", "{n} fitxer", "{n} fitxers"))
        (one,) = cat.entries
        self.assertEqual(one.msgid_plural, "{n} files")
        self.assertEqual(one.msgstr, {0: "{n} fitxer", 1: "{n} fitxers"})

    def test_flags_belong_to_the_next_entry_even_without_a_blank_line(self) -> None:
        cat = cp.parse('msgid "a"\nmsgstr "b"\n#, fuzzy\nmsgid "c"\nmsgstr "d"\n')
        self.assertEqual([e.fuzzy for e in cat.entries], [False, True])

    def test_obsolete_entries_are_counted_not_read(self) -> None:
        text = (
            entry("a", "б")
            + '#~ msgid "old"\n#~ msgstr "старе"\n\n#~ msgid_plural "x"\n#~| msgid "p"\n'
        )
        cat = cp.parse(text)
        self.assertEqual(cat.obsolete, 1)
        self.assertEqual([e.msgid for e in cat.entries], ["a"])

    def test_crlf_and_unusual_line_separators_inside_text(self) -> None:
        cat = cp.parse('msgid "a\u2028b"\r\nmsgstr "c\x0bd"\r\n')
        (one,) = cat.entries
        self.assertEqual((one.msgid, one.msgstr[0]), ("a\u2028b", "c\x0bd"))
        self.assertEqual(cat.syntax, [])

    def test_syntax_errors(self) -> None:
        cases = {
            'msgstr "x"\n': "msgstr before msgid",
            'msgid "x"\nmsgstr "y"\nthis is not po\n': "not valid .po syntax",
            'msgid "x"\n\n': "entry without msgstr",
            '"alone"\n': "without a keyword",
            'msgid "a"\nmsgid "b"\nmsgstr "c"\n': "second msgid",
            'msgid "a"\nmsgstr[0] "b"\n': "msgstr[n] without msgid_plural",
            'msgid "a"\nmsgid_plural "b"\nmsgstr "c"\n': "msgstr in a plural entry",
            'msgid "a"\nmsgid_plural "b"\nmsgstr[0] "c"\nmsgstr[0] "d"\n': "twice",
            'msgid "a"\nmsgstr "b"\nmsgid_plural "c"\n': "misplaced msgid_plural",
            'msgid "a"\nmsgctxt "c"\nmsgstr "b"\n': "msgctxt after",
            'msgid "a\nmsgstr "b"\n': "not in quotes",
            'msgid "a\\q"\nmsgstr "b"\n': "unknown escape",
            'msgid "a"\nmsgstr "b"\n"c\\q"\n': "unknown escape",
        }
        for text, word in cases.items():
            with self.subTest(text=text):
                messages = [m for _, m in cp.parse(text).syntax]
                self.assertTrue(any(word in m for m in messages), messages)


class GoodCatalogueTests(unittest.TestCase):
    def test_ukrainian_lithuanian_and_catalan_catalogues_pass(self) -> None:
        cases = {
            "uk": (
                entry("Welcome to Vilna", "Вітаємо у Vilna")
                + entry("Gloxi is thinking…", "Gloxi думає…")
                + entry("Open <b>{name}</b> (%d)", "Відкрити <b>{name}</b> (%d)")
                + plural("{n} file", "{n} files", "{n} файл", "{n} файли", "{n} файлів")
            ),
            "lt": (
                entry("Welcome to Vilna", "Sveiki atvykę į Vilna")
                + entry("Herkos asks", "„Herkos“ prašo leidimo")
                + entry("Press %s", "Spauskite %s")
                + plural("{n} file", "{n} files", "{n} failas", "{n} failai", "{n} failų")
            ),
            "ca": (
                entry("Welcome to Vilna", "Et donem la benvinguda a Vilna")
                + entry("Gloxi's reason: ", "Motiu de Gloxi: ")
                + plural("{n} file", "{n} files", "{n} fitxer", "{n} fitxers")
            ),
            "ca@valencia": (
                entry("Welcome to Vilna", "Et donem la benvinguda a Vilna")
                + plural("{n} file", "{n} files", "{n} fitxer", "{n} fitxers")
            ),
        }
        for lang, body in cases.items():
            with self.subTest(lang=lang):
                self.assertEqual(check(header(lang) + body, f"x/{lang}.po"), [])

    def test_untranslated_entries_are_not_checked(self) -> None:
        text = header() + entry("Hello {name}", "") + plural("{n} file", "{n} files", "", "", "")
        self.assertEqual(check(text), [])

    def test_a_catalogue_without_plural_forms_is_an_error(self) -> None:
        self.assertEqual(rules(check(header(plural=""))), ["header-plural"])


class PlaceholderTests(unittest.TestCase):
    def problems(self, msgid: str, msgstr: str, flags: str = "") -> list[cp.Problem]:
        return check(header() + entry(msgid, msgstr, flags=flags))

    def test_same_placeholders_in_another_order_pass(self) -> None:
        for msgid, msgstr in (
            ("{name} is {size}", "{size} — це {name}"),
            ("%s of %d", "%d з %s"),
            ("%(name)s is %(size)d", "%(size)d — %(name)s"),
            ("%2$s then %1$s", "%1$s, потім %2$s"),
            ("{0} and {1}", "{1} і {0}"),
            ("{} and {}", "{} і {}"),
            ("{n:02d} of {total}", "{n:02d} з {total}"),
            ("{obj.name} {items[0]}", "{items[0]} {obj.name}"),
        ):
            with self.subTest(msgid=msgid):
                self.assertEqual(self.problems(msgid, msgstr), [])

    def test_missing_extra_and_renamed_placeholders_are_errors(self) -> None:
        for msgid, msgstr, word in (
            ("Delete {name}?", "Видалити?", "missing {name}"),
            ("Delete?", "Видалити {name}?", "extra {name}"),
            ("Delete {name}?", "Видалити {nom}?", "missing {name}"),
            ("Delete {name}?", "Видалити {nom}?", "extra {nom}"),
            ("{n} of {n}", "{n} з", "missing {n}"),
            ("Done: %d", "Gata: %s", "missing %d"),
            ("Done: %(a)s", "Hecho: %(b)s", "extra %(b)s"),
            ("Press %s", "Spauskite", "missing %s"),
            ("{0} {1}", "{0} {2}", "missing {1}"),
            ("{}", "{0}", "missing {}"),
            ("Size {size:>5}", "Mida {size}", "missing {size:>5}"),
        ):
            with self.subTest(msgid=msgid, msgstr=msgstr):
                found = self.problems(msgid, msgstr)
                self.assertEqual(rules(found, warning=False), ["placeholders"], found)
                self.assertIn(word, str(found[0]))
                self.assertTrue(str(found[0]).startswith("demo/po/uk.po: line "))

    def test_percent_percent_and_doubled_braces_are_not_placeholders(self) -> None:
        for msgid, msgstr in (
            ("100%% sure", "Упевнений на 100%%"),
            ("Use {{name}} here", "Використайте {{назва}} тут"),
            ("{percent}% done", "Gata {percent}%"),
            ("50% of it", "50 % d'això"),
            ("Free: 20% left", "Resta un 20% lliure"),
        ):
            with self.subTest(msgid=msgid):
                self.assertEqual(self.problems(msgid, msgstr), [])

    def test_text_that_looks_like_a_placeholder_after_percent_percent_is_not_one(self) -> None:
        self.assertEqual(self.problems("Type %%d, got %d", "Отримано %d"), [])
        self.assertEqual(self.problems("Write {{n}}, got {n}", "Gauta {n}"), [])

    def test_a_placeholder_hidden_behind_percent_percent_is_still_counted(self) -> None:
        found = self.problems("%%%s", "%%")
        self.assertEqual(rules(found, warning=False), ["placeholders"])

    def test_no_format_flags_turn_a_kind_off(self) -> None:
        self.assertEqual(self.problems("{a}", "{b}", "no-python-brace-format"), [])
        self.assertEqual(self.problems("%s", "x", "no-c-format"), [])
        self.assertEqual(
            rules(self.problems("%s", "x", "no-python-brace-format")), ["placeholders"]
        )

    def test_plural_forms_are_compared_with_msgid_plural(self) -> None:
        good = plural("One file", "{n} files", "Vienas failas", "{n} failai", "{n} failų")
        self.assertEqual(rules(check(header("lt") + good, "x/lt.po")), ["plural-placeholders"])
        self.assertTrue(check(header("lt") + good, "x/lt.po")[0].warning)
        for forms, word in (
            (("{n} файл", "файли", "{n} файлів"), "msgstr[1]"),
            (("{n} файл", "{n} файли", "{n} {x} файлів"), "msgstr[2]"),
        ):
            with self.subTest(forms=forms):
                found = check(header() + plural("{n} file", "{n} files", *forms))
                self.assertEqual(rules(found, warning=False), ["placeholders"], found)
                self.assertIn(word, str(found[0]))

    def test_a_non_numeric_placeholder_missing_in_msgstr0_is_an_error(self) -> None:
        text = header() + plural(
            "{name}: {n} file", "{name}: {n} files", "{n} файл", "{name}: {n}", "{name}: {n}"
        )
        found = check(text)
        self.assertEqual(rules(found, warning=False), ["placeholders"])
        self.assertIn("missing {name}", str(found[0]))
        self.assertEqual(rules(found, warning=True), [])

    def test_numeric_placeholders_of_plural_entries(self) -> None:
        for token in ("%d", "{n}", "{count}", "{0}", "{}", "{x:d}", "%5.2f", "%1$d"):
            with self.subTest(token=token):
                self.assertTrue(cp.is_numeric(token))
        for token in ("%s", "{name}", "{x:>5}", "%(who)s", "%c"):
            with self.subTest(token=token):
                self.assertFalse(cp.is_numeric(token))

    def test_msgstr0_may_repeat_a_placeholder_of_the_singular(self) -> None:
        text = header() + plural(
            "{name} file", "{name} files {n}", "{name}", "{name} {n}", "{name} {n}"
        )
        self.assertEqual(rules(check(text)), ["plural-placeholders"])

    def test_msgstr0_may_have_a_placeholder_that_only_the_singular_has(self) -> None:
        text = header() + plural("{name} file", "files", "{name} файл", "файли", "файлів")
        self.assertEqual(check(text), [])
        text = header() + plural("{name} file", "files", "файл", "{name} файли", "файлів")
        self.assertEqual(rules(check(text), warning=False), ["placeholders"])

    def test_wrong_number_of_plural_forms_is_a_warning(self) -> None:
        text = header() + plural("{n} file", "{n} files", "{n} файл", "{n} файли")
        found = check(text)
        self.assertEqual(rules(found), ["plural-count"])
        self.assertTrue(found[0].warning)
        self.assertIn("nplurals=3", str(found[0]))

    def test_the_check_is_skipped_for_fuzzy_entries(self) -> None:
        found = check(header() + entry("Hello {name}", "Привіт", flags="fuzzy"))
        self.assertEqual(rules(found), ["fuzzy"])


class MarkupTests(unittest.TestCase):
    def problems(self, msgid: str, msgstr: str) -> list[cp.Problem]:
        return check(header() + entry(msgid, msgstr))

    def test_same_tags_pass(self) -> None:
        for msgid, msgstr in (
            ("<b>Bold</b> and <i>it</i>", "<i>Курсив</i> і <b>жирний</b>"),
            (
                'See <a href=\\"https://x.org\\">docs</a>',
                'Див. <a href=\\"https://x.org\\">документацію</a>',
            ),
            ("Run <tt>vilna</tt>", "Paleiskite <tt>vilna</tt>"),
            ("Line<br/>two", "Línia<br/>dues"),
            ("<B>x</B>", "<b>x</b>"),
        ):
            with self.subTest(msgid=msgid):
                self.assertEqual(self.problems(msgid, msgstr), [])

    def test_missing_extra_or_unclosed_tags_are_errors(self) -> None:
        for msgid, msgstr, word in (
            ("<b>Bold</b>", "<b>Жирний", "missing /b"),
            ("Bold", "<b>Жирний</b>", "extra /b, b"),
            ("<b>x</b> <i>y</i>", "<b>x</b> y", "missing /i, i"),
            ("<b>x</b>", "</b>x<b>", ""),
            ("<tt>x</tt>", "<TT>x", "missing /tt"),
            ("<b>x</b>", "<b><b>x</b>", "extra b"),
        ):
            with self.subTest(msgid=msgid, msgstr=msgstr):
                found = self.problems(msgid, msgstr)
                if word:
                    self.assertEqual(rules(found, warning=False), ["markup"], found)
                    self.assertIn(word, str(found[0]))
                else:  # the same tags in a wrong order are not told apart: only counts are compared
                    self.assertEqual(found, [])

    def test_a_changed_attribute_is_a_warning(self) -> None:
        found = self.problems(
            '<a href=\\"https://a.org\\">x</a>', '<a href=\\"https://b.org\\">x</a>'
        )
        self.assertEqual(rules(found), ["markup-attributes"])
        self.assertTrue(found[0].warning)

    def test_text_in_angle_brackets_is_not_markup(self) -> None:
        found = self.problems("Use /local <text> now", "Використайте /local <текст> зараз")
        self.assertEqual(found, [])


class EdgeTests(unittest.TestCase):
    def problems(self, msgid: str, msgstr: str) -> list[cp.Problem]:
        return check(header() + entry(msgid, msgstr))

    def test_same_edges_pass(self) -> None:
        for msgid, msgstr in (
            ("Reason: ", "Причина: "),
            ("\\nDone\\n", "\\nГотово\\n"),
            (" x ", " ґ "),
            ("Done", "Gata"),
        ):
            with self.subTest(msgid=msgid):
                self.assertEqual(self.problems(msgid, msgstr), [])

    def test_each_edge_that_differs_is_a_warning(self) -> None:
        for msgid, msgstr, word in (
            ("\\nDone", "Готово", "newline at the start"),
            ("Done\\n", "Готово", "newline at the end"),
            ("Done", "Gata\\n", "msgid has no newline at the end"),
            ("Done", "\\nGata", "msgid has no newline at the start"),
            (" Done", "Gata", "space at the start"),
            ("Done ", "Gata", "space at the end"),
            ("Done", "Gata ", "msgid has no space at the end"),
            ("Done", " Gata", "msgid has no space at the start"),
        ):
            with self.subTest(msgid=msgid, msgstr=msgstr):
                found = self.problems(msgid, msgstr)
                self.assertEqual(rules(found), ["whitespace"], found)
                self.assertTrue(found[0].warning)
                self.assertIn(word, str(found[0]))

    def test_a_space_is_not_a_newline(self) -> None:
        found = self.problems("Done\\n", "Gata ")
        self.assertEqual(len(found), 2, found)


class NameTests(unittest.TestCase):
    def problems(self, msgid: str, msgstr: str, lang: str = "uk") -> list[cp.Problem]:
        return check(header(lang) + entry(msgid, msgstr), f"x/{lang}.po")

    def test_declined_names_keep_their_stem(self) -> None:
        for msgid, msgstr, lang in (
            ("Welcome to Vilna", "Sveiki atvykę į Vilniaus sistemą", "lt"),
            ("Ask Gloxi", "Запитайте в Gloxi", "uk"),
            ("Herkos asks", "HERKOS demana", "ca"),
            ("The Blavet release", "Versió Blavet", "ca"),
            ("Tinta bundles", "Tintas paketai", "lt"),
            ("No names here", "Тут немає назв", "uk"),
        ):
            with self.subTest(msgid=msgid):
                self.assertEqual(self.problems(msgid, msgstr, lang), [])

    def test_a_translated_name_is_a_warning(self) -> None:
        for msgid, msgstr, stem in (
            ("Welcome to Vilna", "Вітаємо у Вільна", "Viln"),
            ("Ask Gloxi", "Запитайте Глокі", "Glox"),
            ("Herkos asks", "Геркос питає", "Herk"),
            ("Blavet release", "Випуск Блавет", "Blavet"),
            ("Tinta bundles", "Набори Тінта", "Tinta"),
        ):
            with self.subTest(msgid=msgid):
                found = self.problems(msgid, msgstr)
                self.assertEqual(rules(found), ["names"], found)
                self.assertTrue(found[0].warning)
                self.assertIn(f"no {stem}", str(found[0]))

    def test_every_missing_name_is_listed(self) -> None:
        found = self.problems("Gloxi in Vilna", "Глокі у Вільній")
        self.assertEqual(rules(found), ["names", "names"])


class CatalogueRuleTests(unittest.TestCase):
    def test_a_duplicate_is_an_error(self) -> None:
        found = check(header() + entry("Save", "Зберегти") + entry("Save", "Записати"))
        self.assertEqual(rules(found), ["duplicate"])
        self.assertFalse(found[0].warning)
        self.assertIn("duplicate of line", str(found[0]))

    def test_the_same_msgid_with_another_context_is_not_a_duplicate(self) -> None:
        text = (
            header() + entry("Open", "Відкрити", ctx="verb") + entry("Open", "Відкритий", ctx="adj")
        )
        self.assertEqual(check(text), [])
        text += entry("Open", "Відкрити ще", ctx="verb") + entry("Open", "Відчинено")
        self.assertEqual(rules(check(text)), ["duplicate"])

    def test_a_duplicate_in_a_pot_is_an_error(self) -> None:
        text = header_pot() + entry("Save", "") + entry("Save", "")
        self.assertEqual(rules(check(text, "x/demo.pot")), ["duplicate"])

    def test_a_fuzzy_entry_is_a_warning(self) -> None:
        found = check(header() + entry("Save", "Зберегти", flags="fuzzy, python-format"))
        self.assertEqual(rules(found), ["fuzzy"])
        self.assertTrue(found[0].warning)

    def test_a_fuzzy_header_is_a_warning_too(self) -> None:
        found = check("#, fuzzy\n" + header())
        self.assertEqual(rules(found), ["fuzzy"])

    def test_obsolete_entries_are_one_warning_with_their_count(self) -> None:
        old = '#~ msgid "a"\n#~ msgstr "б"\n\n#~ msgid "c"\n#~ msgstr "д"\n'
        found = check(header() + entry("Save", "Зберегти") + old)
        self.assertEqual(rules(found), ["obsolete"])
        self.assertTrue(found[0].warning)
        self.assertIn("obsolete (#~) entries: 2", str(found[0]))

    def test_syntax_errors_are_reported_with_the_line(self) -> None:
        found = check(header() + 'msgid "x"\nmsgstr "y"\nrubbish\n')
        self.assertEqual(rules(found), ["syntax"])
        self.assertIn("demo/po/uk.po: line 9: not valid .po syntax", str(found[0]))

    def test_invalid_utf8_is_an_error(self) -> None:
        found = cp.check_catalog("x/uk.po", b'msgid "a"\nmsgstr "\xff\xfe"\n')
        self.assertEqual([str(p) for p in found], ["x/uk.po: the file is not valid UTF-8"])

    def test_a_bom_is_one_clear_error_and_the_rest_is_still_checked(self) -> None:
        found = check("\ufeff" + header() + entry("Hi {n}", "Привіт {n}"))
        self.assertEqual(rules(found), ["encoding"])
        self.assertIn("BOM", str(found[0]))

    def test_a_catalogue_without_a_header_is_an_error(self) -> None:
        self.assertEqual(rules(check(entry("Save", "Зберегти"))), ["header"])


class HeaderTests(unittest.TestCase):
    def test_language_must_match_the_file_name(self) -> None:
        for lang, path in (
            ("uk", "x/pl.po"),
            ("ca", "x/ca@valencia.po"),
            ("ca@valencia", "x/ca.po"),
        ):
            with self.subTest(path=path):
                found = check(header(lang), path)
                self.assertEqual(rules(found, warning=False), ["header-language"], found)
                self.assertIn(f"Language is {lang!r}", str(found[0]))
        self.assertEqual(check(header("ca@valencia"), "x/ca@valencia.po"), [])

    def test_language_missing_or_empty_is_an_error(self) -> None:
        missing = (
            'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
            '"Plural-Forms: nplurals=3; plural=n;\\n"\n'
        )
        found = check(missing)
        self.assertEqual(rules(found), ["header-language"])
        self.assertIn("no Language", str(found[0]))
        empty = missing.replace('"Content', '"Language: \\n"\n"Content')
        self.assertEqual(rules(check(empty)), ["header-language"])
        self.assertIn("Language is ''", str(check(empty)[0]))

    def test_content_type_must_be_utf8(self) -> None:
        for value, ok in (
            ("text/plain; charset=UTF-8", True),
            ("text/plain; charset=utf-8", True),
            ("text/plain; charset=ISO-8859-2", False),
            ("text/plain; charset=CHARSET", False),
            ("text/plain", False),
        ):
            with self.subTest(value=value):
                text = header().replace("text/plain; charset=UTF-8", value)
                self.assertEqual(rules(check(text)), [] if ok else ["header-charset"])

    def test_content_type_missing_is_an_error(self) -> None:
        text = 'msgid ""\nmsgstr ""\n"Language: uk\\n"\n"Plural-Forms: nplurals=3; plural=n;\\n"\n'
        found = check(text)
        self.assertEqual(rules(found), ["header-charset"])
        self.assertIn("no Content-Type", str(found[0]))

    def test_plural_forms_must_exist_and_be_well_formed(self) -> None:
        self.assertEqual(rules(check(header(plural=""))), ["header-plural"])
        for bad in ("plural=n;", "nplurals=3;", "nplurals=x; plural=n;", "nplurals=3 plural=n;"):
            with self.subTest(bad=bad):
                found = check(header(plural=bad))
                self.assertEqual(rules(found, warning=False), ["header-plural"], found)

    def test_nplurals_must_fit_the_language(self) -> None:
        sure = {
            "uk": 3,
            "pl": 3,
            "cs": 3,
            "ro": 3,
            "lt": 3,
            "de": 2,
            "es": 2,
            "fr": 2,
            "it": 2,
            "pt": 2,
        }
        for lang, count in sure.items():
            path = f"x/{lang}.po"
            with self.subTest(lang=lang):
                good = (
                    header(lang, plural=f"nplurals={count}; plural=n != 1;")
                    if lang in cp.PLURALS
                    else ""
                )
                self.assertEqual(check(good, path), [])
                bad = header(lang, plural=f"nplurals={count % 3 + 1}; plural=n != 1;")
                found = check(bad, path)
                self.assertEqual(rules(found, warning=False), ["header-nplurals"], found)
                self.assertIn(f"{lang} has {count}", str(found[0]))

    def test_semicolon_after_the_plural_expression_is_optional(self) -> None:
        found = check(header("ca", plural="nplurals=2; plural=(n != 1)"), "x/ca.po")
        self.assertEqual(found, [])

    def test_nplurals_of_languages_that_are_not_certain_is_a_warning(self) -> None:
        for lang in ("ca", "ca@valencia", "gl", "eu"):
            with self.subTest(lang=lang):
                found = check(header(lang, plural="nplurals=3; plural=n;"), f"x/{lang}.po")
                self.assertEqual(rules(found), ["header-nplurals"], found)
                self.assertTrue(found[0].warning)
                self.assertIn("not certain", str(found[0]))

    def test_a_language_without_a_table_is_a_warning(self) -> None:
        text = header("uk", plural="nplurals=2; plural=n != 1;").replace(
            "Language: uk", "Language: xx"
        )
        found = check(text, "x/xx.po")
        self.assertEqual(rules(found), ["header-nplurals"])
        self.assertTrue(found[0].warning)

    def test_a_pot_needs_neither_language_nor_plural_forms_but_utf8(self) -> None:
        self.assertEqual(check(header_pot(), "x/demo.pot"), [])
        bad = header_pot().replace("UTF-8", "ASCII")
        self.assertEqual(rules(check(bad, "x/demo.pot")), ["header-charset"])

    def test_a_pot_is_not_checked_for_placeholders(self) -> None:
        text = header_pot() + entry("Hello {name}", "Привіт")
        self.assertEqual(check(text, "x/demo.pot"), [])


def header_pot() -> str:
    return 'msgid ""\nmsgstr ""\n"Language: \\n"\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'


class LegacyTests(unittest.TestCase):
    def test_a_listed_violation_becomes_a_warning_and_nothing_else_does(self) -> None:
        legacy = frozenset({("x/uk.po", "header-plural")})
        text = header(plural="") + entry("Hi {name}", "Привіт")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "x").mkdir()
            (Path(tmp) / "x/uk.po").write_text(text, encoding="utf-8")
            (Path(tmp) / "x/pl.po").write_text(text, encoding="utf-8")
            with mock.patch.object(cp, "LEGACY", legacy):
                found = cp.check_files(Path(tmp), ["x/uk.po", "x/pl.po"])
        by_file = {(p.path, p.rule): p.warning for p in found}
        self.assertEqual(
            by_file,
            {
                ("x/pl.po", "header-language"): False,
                ("x/pl.po", "header-plural"): False,
                ("x/pl.po", "placeholders"): False,
                ("x/uk.po", "header-plural"): True,
                ("x/uk.po", "placeholders"): False,
            },
        )


class RunTests(unittest.TestCase):
    def run_main(self, files: dict[str, str], args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in files.items():
                path = Path(tmp) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            with (
                mock.patch.object(cp, "ROOT", Path(tmp)),
                mock.patch.object(cp, "LEGACY", frozenset()),
                mock.patch.object(cp, "tracked_catalogs", return_value=sorted(files)),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = cp.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_good_catalogues_exit_0(self) -> None:
        files = {"a/uk.po": header() + entry("Hi {n}", "Привіт {n}"), "a/demo.pot": header_pot()}
        code, out, err = self.run_main(files, [])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), "2 catalogues checked: 0 errors, 0 warnings")

    def test_warnings_alone_exit_0_and_are_marked(self) -> None:
        code, out, err = self.run_main(
            {"a/uk.po": header() + entry("Hi", "Привіт", flags="fuzzy")}, []
        )
        self.assertEqual(code, 0)
        self.assertIn("a/uk.po: warning: line 8", err)
        self.assertIn("0 errors, 1 warnings", out)

    def test_an_error_exits_1_in_the_form_path_problem(self) -> None:
        code, out, err = self.run_main({"a/uk.po": header() + entry("Hi {n}", "Привіт")}, [])
        self.assertEqual(code, 1)
        self.assertTrue(
            err.startswith('a/uk.po: line 7 (msgid "Hi {n}"): placeholders differ'), err
        )
        self.assertIn("1 errors, 0 warnings", out)

    def test_named_files_only(self) -> None:
        files = {"a/uk.po": header() + entry("Hi {n}", "Привіт"), "a/lt.po": header("lt")}
        code, out, err = self.run_main(files, ["a\\lt.po"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("1 catalogues checked", out)

    def test_wrong_calls_exit_2(self) -> None:
        files = {"a/uk.po": header()}
        for args in (["--bad"], ["a/missing.po"], ["a/uk.po", "b/nope.po"]):
            with self.subTest(args=args):
                self.assertEqual(self.run_main(files, args)[0], 2)
        self.assertEqual(self.run_main({}, [])[0], 2)

    def test_help_exits_0(self) -> None:
        code, _, err = self.run_main({"a/uk.po": header()}, ["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", err)

    def test_git_failure_exits_2(self) -> None:
        err = io.StringIO()
        boom = subprocess.CalledProcessError(128, "git")
        with (
            mock.patch.object(cp, "tracked_catalogs", side_effect=boom),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(cp.main([]), 2)
        self.assertIn("cannot list files with git ls-files", err.getvalue())

    def test_an_unreadable_file_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            found = cp.check_files(Path(tmp), ["gone/uk.po"])
        self.assertEqual(rules(found, warning=False), ["read"])
        self.assertTrue(str(found[0]).startswith("gone/uk.po: cannot read the file"))


@unittest.skipUnless(shutil.which("git"), "git is needed")
class GitTests(unittest.TestCase):
    def test_tracked_catalogs_lists_po_and_pot_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("a/uk.po", "a/b/ca@valencia.po", "a/x.pot", "a/readme.md", "untracked.po"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("x", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "a"], cwd=root, check=True)
            self.assertEqual(
                cp.tracked_catalogs(root), ["a/b/ca@valencia.po", "a/uk.po", "a/x.pot"]
            )


@unittest.skipUnless(shutil.which("git"), "git is needed")
class RepoTests(unittest.TestCase):
    def test_tracked_catalogues_have_no_errors(self) -> None:
        tracked = cp.tracked_catalogs(ROOT)
        self.assertTrue(tracked, "the repo has tracked catalogues")
        found = [str(p) for p in cp.check_files(ROOT, tracked) if not p.warning]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
