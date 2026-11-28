#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_site_i18n.py (site translation catalogues).

Each test builds small catalogues in a temporary folder and checks them the
way ``main()`` does. The checks cover brand-name stems, leftover English,
numbers and links. Missing or extra keys, and ``{placeholders}`` in the
plain text, are left to the data-file check and are not reported here.

Run: python3 tools/test_check_site_i18n.py
"""

from __future__ import annotations

import contextlib
import io
import json
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

import check_site_i18n as ci  # noqa: E402  (after sys.path.insert above)

I18N = "site/src/i18n"


def dump(texts: dict[str, str]) -> str:
    return json.dumps(texts, ensure_ascii=False, indent=2) + "\n"


def catalogs(*pairs: tuple[str, dict[str, str]]) -> dict[str, str]:
    """Language code and strings → files under ``site/src/i18n/``."""
    return {f"{I18N}/{lang}.json": dump(strings) for lang, strings in pairs}


def rules(problems: list[ci.Problem], *, warning: bool | None = None) -> list[str]:
    return [p.rule for p in problems if warning is None or p.warning == warning]


def messages(problems: list[ci.Problem]) -> list[str]:
    return [str(p) for p in problems]


def check(
    files: dict[str, str | bytes],
    *,
    legacy: frozenset[tuple[str, str]] | None = None,
) -> list[ci.Problem]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, bytes):
                path.write_bytes(text)
            else:
                path.write_text(text, encoding="utf-8")
        with mock.patch.object(ci, "LEGACY", ci.LEGACY if legacy is None else legacy):
            return ci.check_files(root, sorted(files))


class GoodCatalogTests(unittest.TestCase):
    def test_translated_catalogues_pass(self) -> None:
        found = check(
            catalogs(
                (
                    "en",
                    {
                        "title": "Vilna OS and Gloxi 0.1, with Tinta and Blavet",
                        "lead": 'About 2.5 GB; see <a href="{{guide_url}}">the guide</a>.',
                        "note": "Art. 6(1)(a) and RFC 9116.",
                    },
                ),
                (
                    "uk",
                    {
                        "title": "Vilna OS і Gloxi 0.1, з Tinta і Blavet",
                        "lead": 'Близько 2,5 ГБ; див. <a href="{{guide_url}}">настанову</a>.',
                        "note": "ст. 6(1)(a) і RFC 9116.",
                    },
                ),
            )
        )
        self.assertEqual(found, [])

    def test_declined_stems_are_enough(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": "Welcome to Vilna, try Gloxi, Blavet and Tinta."}),
                ("uk", {"p": "Ласкаво просимо у Vilny, спробуйте Gloxiho, Blavetu і Tintu."}),
            )
        )
        self.assertEqual(found, [])

    def test_stem_match_is_case_insensitive(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": "Vilna, Gloxi, Blavet, Tinta"}),
                ("de", {"p": "vilna, gloxI, BLAVET, tinta"}),
            )
        )
        self.assertEqual(found, [])

    def test_missing_key_is_not_reported(self) -> None:
        found = check(
            catalogs(
                ("en", {"keep": "Vilna OS", "only_en": "A second line"}),
                ("uk", {"keep": "Vilna OS"}),
            )
        )
        self.assertEqual(found, [])

    def test_extra_key_is_not_reported_when_it_is_translated(self) -> None:
        found = check(
            catalogs(
                ("en", {"keep": "Vilna OS"}),
                ("uk", {"keep": "Vilna OS", "extra": "Додатковий рядок"}),
            )
        )
        self.assertEqual(found, [])


class NameTests(unittest.TestCase):
    def test_translated_name_without_stem_is_an_error(self) -> None:
        for name, stem, translated in (
            ("Vilna", "Viln", "Вільна система"),
            ("Gloxi", "Glox", "помічник"),
            ("Blavet", "Blavet", "установник"),
            ("Tinta", "Tint", "офісний набір"),
        ):
            with self.subTest(name=name):
                found = check(
                    catalogs(
                        ("en", {"p": f"This is {name}."}),
                        ("uk", {"p": f"Це {translated}."}),
                    )
                )
                self.assertEqual(rules(found, warning=False), ["names"])
                self.assertTrue(
                    str(found[0]).startswith(
                        f"{I18N}/uk.json: p: English has {name}, translation has no {stem}…"
                    ),
                    str(found[0]),
                )

    def test_name_only_checked_when_english_has_it(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": "A short page."}),
                ("uk", {"p": "Коротка сторінка."}),
            )
        )
        self.assertEqual(found, [])

    def test_each_missing_stem_is_its_own_error(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": "Vilna and Gloxi"}),
                ("uk", {"p": "система і помічник"}),
            )
        )
        self.assertEqual(rules(found), ["names", "names"])


class EnglishTests(unittest.TestCase):
    def test_five_english_words_are_a_warning(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": "This site collects nothing about you."}),
                ("uk", {"p": "This site collects nothing about you."}),
            )
        )
        self.assertEqual(rules(found, warning=True), ["english"])
        self.assertEqual(rules(found, warning=False), [])
        self.assertIn("English text longer than 4 words:", str(found[0]))
        self.assertIn("This site collects nothing about", str(found[0]))
        self.assertTrue(str(found[0]).startswith(f"{I18N}/uk.json: warning: "))

    def test_four_english_words_are_allowed(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": "Click here for more."}),
                ("uk", {"p": "Click here for more, тоді далі."}),
            )
        )
        self.assertEqual(found, [])

    def test_english_catalogue_is_not_flagged(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": "This site collects nothing about you on purpose."}),
            )
        )
        self.assertEqual(found, [])

    def test_english_inside_code_or_url_is_ignored(self) -> None:
        found = check(
            catalogs(
                (
                    "en",
                    {
                        "p": "See the guide.",
                        "q": "Open the page.",
                    },
                ),
                (
                    "uk",
                    {
                        "p": "Див. <code>this site collects nothing about you</code>.",
                        "q": "Відкрийте https://example.com/this-site-collects-nothing-about-you",
                    },
                ),
            )
        )
        self.assertEqual(rules(found, warning=True), [])

    def test_placeholder_words_are_ignored(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": "Go to the form."}),
                ("uk", {"p": "Далі {{this site collects nothing about you}}."}),
            )
        )
        self.assertEqual(found, [])


class NumberTests(unittest.TestCase):
    def test_decimal_comma_matches_decimal_point(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": "The model is about 2.5 GB (Gloxi 0.1)."}),
                ("uk", {"p": "Модель має близько 2,5 ГБ (Gloxi 0.1)."}),
            )
        )
        self.assertEqual(found, [])

    def test_legal_article_typography_matches(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": "Art. 6(1)(a) GDPR."}),
                ("es", {"p": "art. 6.1.a RGPD."}),
                ("fr", {"p": "art. 6, par. 1, a du RGPD."}),
            )
        )
        self.assertEqual(found, [])

    def test_reordered_date_digits_match(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": "Last updated: 12 November 2026."}),
                ("lt", {"p": "Paskutinį kartą atnaujinta 2026 m. lapkričio 12 d."}),
            )
        )
        self.assertEqual(found, [])

    def test_missing_number_is_an_error(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": "Kept for 90 days."}),
                ("uk", {"p": "Зберігається кілька днів."}),
            )
        )
        self.assertEqual(rules(found, warning=False), ["numbers"])
        self.assertIn("90 vs (none)", str(found[0]))
        self.assertTrue(str(found[0]).startswith(f"{I18N}/uk.json: p: numbers differ"))

    def test_changed_number_is_an_error(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": "Kept for 90 days."}),
                ("uk", {"p": "Зберігається 60 днів."}),
            )
        )
        self.assertEqual(rules(found), ["numbers"])
        self.assertIn("90 vs 60", str(found[0]))

    def test_number_count_matters(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": "90 days, then another 90."}),
                ("uk", {"p": "90 днів."}),
            )
        )
        self.assertEqual(rules(found), ["numbers"])
        self.assertIn("90, 90 vs 90", str(found[0]))


class LinkTests(unittest.TestCase):
    def test_same_href_and_bare_url_pass(self) -> None:
        found = check(
            catalogs(
                (
                    "en",
                    {
                        "p": '<a href="{{repo_url}}/issues">Issues</a> and https://www.aepd.es/',
                    },
                ),
                (
                    "uk",
                    {
                        "p": '<a href="{{repo_url}}/issues">Звернення</a> і https://www.aepd.es/',
                    },
                ),
            )
        )
        self.assertEqual(found, [])

    def test_href_is_not_counted_twice_as_a_bare_url(self) -> None:
        found = check(
            catalogs(
                (
                    "en",
                    {"p": '<a href="https://www.aepd.es/">the authority</a>'},
                ),
                (
                    "uk",
                    {"p": '<a href="https://www.aepd.es/">орган</a>'},
                ),
            )
        )
        self.assertEqual(found, [])

    def test_changed_href_is_an_error(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": '<a href="{{privacy_url}}">Privacy</a>'}),
                ("uk", {"p": '<a href="{{security_url}}">Приватність</a>'}),
            )
        )
        self.assertEqual(rules(found), ["links"])
        self.assertIn("{{privacy_url}} vs {{security_url}}", str(found[0]))

    def test_missing_link_is_an_error(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": 'See <a href="/.well-known/security.txt">security.txt</a>.'}),
                ("uk", {"p": "Див. security.txt."}),
            )
        )
        self.assertEqual(rules(found), ["links"])
        self.assertIn("/.well-known/security.txt vs (none)", str(found[0]))

    def test_entity_in_href_is_unescaped(self) -> None:
        found = check(
            catalogs(
                ("en", {"p": '<a href="https://example.com?a=1&amp;b=2">x</a>'}),
                ("uk", {"p": '<a href="https://example.com?a=1&b=2">x</a>'}),
            )
        )
        self.assertEqual(found, [])


class StructureTests(unittest.TestCase):
    def test_nested_keys_are_compared_by_path(self) -> None:
        files = {
            f"{I18N}/en.json": json.dumps({"home": {"lead": "Vilna 90"}}, ensure_ascii=False),
            f"{I18N}/uk.json": json.dumps({"home": {"lead": "система"}}, ensure_ascii=False),
        }
        found = check(files)
        self.assertIn("names", rules(found))
        self.assertIn("numbers", rules(found))
        self.assertTrue(any("home.lead:" in str(p) for p in found), found)

    def test_invalid_json_is_an_error(self) -> None:
        found = check(
            {
                f"{I18N}/en.json": dump({"p": "Vilna"}),
                f"{I18N}/uk.json": "{not json",
            }
        )
        self.assertEqual(rules(found, warning=False), ["json"])
        self.assertIn("not valid JSON", str(found[0]))

    def test_invalid_utf8_is_an_error(self) -> None:
        found = check(
            {
                f"{I18N}/en.json": dump({"p": "Vilna"}),
                f"{I18N}/uk.json": b"\xff\xfe{",
            }
        )
        self.assertEqual(rules(found), ["encoding"])
        self.assertIn("not valid UTF-8", str(found[0]))

    def test_missing_english_reference_is_an_error(self) -> None:
        found = check({f"{I18N}/uk.json": dump({"p": "Сторінка Vilna"})})
        self.assertEqual(rules(found), ["reference"])
        self.assertIn("no English reference (en.json)", str(found[0]))

    def test_legacy_pair_becomes_a_warning(self) -> None:
        path = f"{I18N}/uk.json"
        found = check(
            catalogs(
                ("en", {"p": "Kept for 90 days."}),
                ("uk", {"p": "Зберігається 60 днів."}),
            ),
            legacy=frozenset({(path, "numbers")}),
        )
        self.assertEqual(rules(found, warning=True), ["numbers"])
        self.assertEqual(rules(found, warning=False), [])
        self.assertTrue(str(found[0]).startswith(f"{path}: warning: "))

    def test_unreadable_file_is_an_error(self) -> None:
        found = ci.check_files(Path("/tmp"), ["gone/en.json"])
        self.assertEqual(rules(found, warning=False), ["read"])
        self.assertTrue(str(found[0]).startswith("gone/en.json: cannot read the file"))


class RunTests(unittest.TestCase):
    def run_main(self, files: dict[str, str], args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in files.items():
                path = Path(tmp) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            with (
                mock.patch.object(ci, "ROOT", Path(tmp)),
                mock.patch.object(ci, "LEGACY", frozenset()),
                mock.patch.object(ci, "tracked_catalogs", return_value=sorted(files)),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = ci.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_good_catalogues_exit_0(self) -> None:
        files = catalogs(("en", {"p": "Vilna 0.1"}), ("uk", {"p": "Vilna 0.1"}))
        code, out, err = self.run_main(files, [])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), "2 catalogues checked: 0 errors, 0 warnings")

    def test_warnings_alone_exit_0_and_are_marked(self) -> None:
        files = catalogs(
            ("en", {"p": "This site collects nothing about you."}),
            ("uk", {"p": "This site collects nothing about you."}),
        )
        code, out, err = self.run_main(files, [])
        self.assertEqual(code, 0)
        self.assertIn(f"{I18N}/uk.json: warning:", err)
        self.assertIn("0 errors, 1 warnings", out)

    def test_an_error_exits_1_in_the_form_path_problem(self) -> None:
        files = catalogs(("en", {"p": "Kept for 90 days."}), ("uk", {"p": "Кілька днів."}))
        code, out, err = self.run_main(files, [])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith(f"{I18N}/uk.json: p: numbers differ"), err)
        self.assertIn("1 errors, 0 warnings", out)

    def test_named_files_only(self) -> None:
        files = catalogs(
            ("en", {"p": "Kept for 90 days."}),
            ("uk", {"p": "Кілька днів."}),
            ("de", {"p": "90 Tage."}),
        )
        code, out, err = self.run_main(files, [f"{I18N}\\de.json", f"{I18N}\\en.json"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("2 catalogues checked", out)

    def test_wrong_calls_exit_2(self) -> None:
        files = catalogs(("en", {"p": "Vilna"}))
        for args in (["--bad"], ["site/src/i18n/missing.json"], [f"{I18N}/en.json", "nope.json"]):
            with self.subTest(args=args):
                self.assertEqual(self.run_main(files, args)[0], 2)
        self.assertEqual(self.run_main({}, [])[0], 2)

    def test_help_exits_0(self) -> None:
        code, _, err = self.run_main(catalogs(("en", {"p": "Vilna"})), ["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", err)

    def test_git_failure_exits_2(self) -> None:
        err = io.StringIO()
        boom = subprocess.CalledProcessError(128, "git")
        with (
            mock.patch.object(ci, "tracked_catalogs", side_effect=boom),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(ci.main([]), 2)
        self.assertIn("cannot list files with git ls-files", err.getvalue())


@unittest.skipUnless(shutil.which("git"), "git is needed")
class GitTests(unittest.TestCase):
    def test_tracked_catalogs_lists_site_i18n_json_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in (
                f"{I18N}/en.json",
                f"{I18N}/uk.json",
                "site/src/i18n/readme.md",
                "site/src/other.json",
                "untracked.json",
            ):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "site/src"], cwd=root, check=True)
            self.assertEqual(
                ci.tracked_catalogs(root),
                [f"{I18N}/en.json", f"{I18N}/uk.json"],
            )


@unittest.skipUnless(shutil.which("git"), "git is needed")
class RepoTests(unittest.TestCase):
    def test_tracked_catalogues_have_no_errors(self) -> None:
        tracked = ci.tracked_catalogs(ROOT)
        self.assertTrue(tracked, "the repo has tracked site i18n catalogues")
        found = [str(p) for p in ci.check_files(ROOT, tracked) if not p.warning]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
