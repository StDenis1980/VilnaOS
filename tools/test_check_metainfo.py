#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_metainfo.py (the AppStream metainfo checker).

Every test writes small files into a temporary folder and checks them the way
``main()`` does; there is a test for every rule and one for the good file.

Run: python3 tools/test_check_metainfo.py
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_metainfo as cm  # noqa: E402  (after sys.path.insert above)

APP_ID = "io.github.stdenis1980.Demo"
FILE = f"{APP_ID}.metainfo.xml"
TODAY = date(2026, 11, 29)
RELEASES = (
    '<release version="1.10.0" date="2026-11-20"/>\n'
    '    <release version="1.9.1~rc1" date="2026-11-10"/>\n'
    '    <release version="1.9.0" date="2026-10-01"/>'
)


def metainfo(
    *,
    component: str = "component",
    id_: str = APP_ID,
    drop: tuple[str, ...] = (),
    releases: str = RELEASES,
    urls: str = '<url type="homepage">https://example.org/demo</url>',
    screenshots: str = "https://example.org/demo.png",
    extra: str = "",
    summary_langs: str = "uk de",
    name_langs: str = "uk de",
) -> str:
    """A good file; the keyword arguments break it in one place."""
    parts = {
        "id": f"<id>{id_}</id>",
        "metadata_license": "<metadata_license>CC0-1.0</metadata_license>",
        "project_license": "<project_license>GPL-3.0-or-later</project_license>",
        "name": "<name>Demo</name>"
        + "".join(f'<name xml:lang="{lang}">Демо {lang}</name>' for lang in name_langs.split()),
        "summary": "<summary>A demo</summary>"
        + "".join(
            f'<summary xml:lang="{lang}">Демо {lang}</summary>' for lang in summary_langs.split()
        ),
        "description": '<description><p>Text.</p><p xml:lang="uk">Текст.</p></description>',
        "content_rating": '<content_rating type="oars-1.1"/>',
    }
    body = "\n  ".join(v for k, v in parts.items() if k not in drop)
    shots = (
        f"<screenshots><screenshot><image>{screenshots}</image></screenshot></screenshots>"
        if screenshots
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- SPDX-License-Identifier: CC0-1.0 -->\n"
        f'<{component} type="desktop-application">\n  {body}\n  {urls}\n  {shots}\n  {extra}\n'
        f"  <releases>\n    {releases}\n  </releases>\n</{component}>\n"
    )


def rels(*items: tuple[str, str]) -> str:
    """The <release> elements for (version, date) pairs, newest first."""
    return "".join(f'<release version="{v}" date="{d}"/>' for v, d in items)


def problems_of(text: str, name: str = FILE) -> list[cm.Problem]:
    """Write the text to a temporary file and check it the way main() does."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / name).write_text(text, encoding="utf-8")
        return cm.check_files(Path(tmp), [name], TODAY)


def errors_of(text: str, name: str = FILE) -> list[str]:
    return [str(p) for p in problems_of(text, name) if not p.warning]


def warnings_of(text: str, name: str = FILE) -> list[str]:
    return [str(p) for p in problems_of(text, name) if p.warning]


class GoodFile(unittest.TestCase):
    def test_good_file_has_no_problems(self) -> None:
        self.assertEqual(problems_of(metainfo()), [])

    def test_file_without_releases_or_screenshots_passes(self) -> None:
        text = metainfo(releases="", screenshots="")
        self.assertEqual(problems_of(text), [])

    def test_messages_start_with_the_path(self) -> None:
        text = metainfo(drop=("summary",))
        self.assertEqual(errors_of(text), [f"{FILE}: missing <summary>"])


class RootAndId(unittest.TestCase):
    def test_not_well_formed_xml(self) -> None:
        errors = errors_of("<component><id>x</component>")
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith(f"{FILE}: not well-formed XML"))

    def test_root_must_be_component(self) -> None:
        self.assertEqual(
            errors_of(metainfo(component="application")),
            [f"{FILE}: the root element is <application>, not <component>"],
        )

    def test_id_must_match_file_name(self) -> None:
        errors = errors_of(metainfo(id_="io.github.stdenis1980.Other"))
        self.assertEqual(
            errors,
            [f"{FILE}: <id> is 'io.github.stdenis1980.Other' but the file name says '{APP_ID}'"],
        )

    def test_id_missing(self) -> None:
        self.assertEqual(errors_of(metainfo(drop=("id",))), [f"{FILE}: missing <id>"])

    def test_id_is_compared_with_the_name_of_the_file_not_its_folder(self) -> None:
        self.assertEqual(
            errors_of(metainfo(), name=f"other.{APP_ID}.metainfo.xml"),
            [
                f"other.{APP_ID}.metainfo.xml: <id> is '{APP_ID}' but the file name says "
                f"'other.{APP_ID}'"
            ],
        )


class RequiredElements(unittest.TestCase):
    def test_each_required_element_is_reported(self) -> None:
        for tag in (
            "metadata_license",
            "project_license",
            "name",
            "summary",
            "description",
            "content_rating",
        ):
            with self.subTest(tag=tag):
                errors = errors_of(metainfo(drop=(tag,)))
                self.assertIn(f"{FILE}: missing <{tag}>", errors)

    def test_empty_license_is_reported(self) -> None:
        text = metainfo().replace(">CC0-1.0<", "><")
        self.assertEqual(errors_of(text), [f"{FILE}: <metadata_license> is empty"])

    def test_empty_name_is_reported(self) -> None:
        text = metainfo().replace("<name>Demo</name>", "<name> </name>")
        self.assertEqual(errors_of(text), [f"{FILE}: <name> is empty"])

    def test_empty_description_is_reported(self) -> None:
        text = metainfo().replace(
            '<description><p>Text.</p><p xml:lang="uk">Текст.</p></description>',
            "<description/>",
        )
        self.assertEqual(errors_of(text), [f"{FILE}: <description> is empty"])

    def test_name_only_in_translation_is_reported(self) -> None:
        text = metainfo().replace("<name>Demo</name>", "")
        self.assertEqual(
            errors_of(text), [f"{FILE}: <name> has no untranslated text (no xml:lang-free one)"]
        )


class Releases(unittest.TestCase):
    def test_missing_version(self) -> None:
        errors = errors_of(metainfo(releases='<release date="2026-11-20"/>'))
        self.assertEqual(errors, [f"{FILE}: release #1: missing version"])

    def test_version_without_a_number(self) -> None:
        errors = errors_of(metainfo(releases='<release version="git" date="2026-11-20"/>'))
        self.assertEqual(errors, [f"{FILE}: release git: version has no number in it"])

    def test_missing_date(self) -> None:
        errors = errors_of(metainfo(releases='<release version="1.0"/>'))
        self.assertEqual(errors, [f"{FILE}: release 1.0: missing date"])

    def test_wrong_date_formats(self) -> None:
        for bad in ("20261120", "2026-W47-5", "2026-11-20T10:00:00", "20-11-2026", "2026-1-5"):
            with self.subTest(date=bad):
                text = metainfo(releases=f'<release version="1.0" date="{bad}"/>')
                self.assertEqual(
                    errors_of(text),
                    [f"{FILE}: release 1.0: date '{bad}' is not a valid YYYY-MM-DD date"],
                )

    def test_impossible_date(self) -> None:
        text = metainfo(releases='<release version="1.0" date="2026-02-30"/>')
        self.assertEqual(
            errors_of(text),
            [f"{FILE}: release 1.0: date '2026-02-30' is not a valid YYYY-MM-DD date"],
        )

    def test_date_up_to_30_days_ahead_is_fine(self) -> None:
        text = metainfo(releases='<release version="1.0" date="2026-12-29"/>')
        self.assertEqual(problems_of(text), [])

    def test_date_more_than_30_days_ahead_is_an_error(self) -> None:
        text = metainfo(releases='<release version="1.0" date="2026-12-30"/>')
        self.assertEqual(
            errors_of(text),
            [f"{FILE}: release 1.0: date 2026-12-30 is more than 30 days in the future"],
        )

    def test_dates_must_not_go_up(self) -> None:
        releases = rels(("2.0", "2026-10-01"), ("1.0", "2026-11-01"))
        errors = errors_of(metainfo(releases=releases))
        self.assertEqual(len(errors), 1)
        self.assertIn("release 1.0: date 2026-11-01 is newer than 2026-10-01", errors[0])

    def test_two_releases_on_the_same_day_are_fine(self) -> None:
        releases = rels(("2.0", "2026-10-01"), ("1.0", "2026-10-01"))
        self.assertEqual(problems_of(metainfo(releases=releases)), [])

    def test_versions_must_not_go_up(self) -> None:
        releases = rels(("1.0", "2026-11-01"), ("2.0", "2026-10-01"))
        errors = errors_of(metainfo(releases=releases))
        self.assertEqual(len(errors), 1)
        self.assertIn("release 2.0: not older than 1.0 above it", errors[0])

    def test_versions_are_compared_by_numbers_not_text(self) -> None:
        good = rels(("0.10.0", "2026-11-01"), ("0.9.0", "2026-10-01"))
        self.assertEqual(problems_of(metainfo(releases=good)), [])
        bad = rels(("0.9.0", "2026-11-01"), ("0.10.0", "2026-10-01"))
        self.assertEqual(len(errors_of(metainfo(releases=bad))), 1)

    def test_prerelease_is_older_than_the_release(self) -> None:
        good = rels(("1.0", "2026-11-01"), ("1.0~a1", "2026-10-01"))
        self.assertEqual(problems_of(metainfo(releases=good)), [])
        bad = rels(("1.0~a1", "2026-11-01"), ("1.0", "2026-10-01"))
        self.assertEqual(len(errors_of(metainfo(releases=bad))), 1)

    def test_numeric_prerelease_suffix(self) -> None:
        good = rels(("1.0~rc10", "2026-11-01"), ("1.0~rc9", "2026-10-01"))
        self.assertEqual(problems_of(metainfo(releases=good)), [])

    def test_duplicate_version(self) -> None:
        releases = rels(("2.0", "2026-11-01"), ("1.0", "2026-10-01"), ("2.0", "2026-09-01"))
        self.assertEqual(
            errors_of(metainfo(releases=releases)),
            [f"{FILE}: release 2.0: version listed more than once"],
        )

    def test_same_version_written_differently_is_not_older(self) -> None:
        releases = rels(("1.0", "2026-11-01"), ("1.0.0", "2026-10-01"))
        errors = errors_of(metainfo(releases=releases))
        self.assertEqual(len(errors), 1)
        self.assertIn("release 1.0.0: not older than 1.0 above it", errors[0])

    def test_unusual_version_text_is_only_a_warning(self) -> None:
        text = metainfo(releases='<release version="1.0rc1" date="2026-11-01"/>')
        self.assertEqual(errors_of(text), [])
        self.assertEqual(len(warnings_of(text)), 1)
        self.assertIn("release 1.0rc1: version is not dot-separated numbers", warnings_of(text)[0])


class Urls(unittest.TestCase):
    def test_http_url_is_an_error(self) -> None:
        text = metainfo(urls='<url type="bugtracker">http://example.org/bugs</url>')
        self.assertEqual(
            errors_of(text),
            [
                f"{FILE}: <url type=\"bugtracker\"> is not an https:// URL: 'http://example.org/bugs'"
            ],
        )

    def test_url_without_scheme_or_empty(self) -> None:
        for bad in ("example.org", "", "https://"):
            with self.subTest(url=bad):
                text = metainfo(urls=f'<url type="homepage">{bad}</url>')
                self.assertEqual(len(errors_of(text)), 1)

    def test_every_url_is_checked(self) -> None:
        urls = (
            '<url type="homepage">https://example.org</url>'
            '<url type="faq">ftp://example.org/faq</url>'
            '<url type="help">http://example.org/help</url>'
        )
        self.assertEqual(len(errors_of(metainfo(urls=urls))), 2)

    def test_https_url_is_good(self) -> None:
        self.assertEqual(problems_of(metainfo(urls='<url type="homepage">https://a.b/c</url>')), [])

    def test_http_screenshot_is_an_error(self) -> None:
        text = metainfo(screenshots="http://example.org/shot.png")
        self.assertEqual(
            errors_of(text),
            [f"{FILE}: screenshot <image> is not an https:// URL: 'http://example.org/shot.png'"],
        )

    def test_all_images_of_a_screenshot_are_checked(self) -> None:
        extra = (
            "<screenshots><screenshot>"
            '<image type="source">https://example.org/a.png</image>'
            '<image type="thumbnail">http://example.org/a-small.png</image>'
            "</screenshot></screenshots>"
        )
        self.assertEqual(len(errors_of(metainfo(screenshots="", extra=extra))), 1)


class Languages(unittest.TestCase):
    def test_all_known_languages_are_accepted(self) -> None:
        langs = " ".join(cm.LANGS)
        text = metainfo(summary_langs=langs, name_langs=langs)
        self.assertEqual(problems_of(text), [])

    def test_unknown_language_in_summary(self) -> None:
        text = metainfo(summary_langs="uk de ru", name_langs="uk de ru")
        self.assertEqual(
            errors_of(text),
            [
                f"{FILE}: <name> has an unknown language xml:lang='ru'",
                f"{FILE}: <summary> has an unknown language xml:lang='ru'",
            ],
        )

    def test_unknown_language_is_found_in_nested_elements(self) -> None:
        text = metainfo().replace('<p xml:lang="uk">', '<p xml:lang="pt_BR">')
        self.assertEqual(errors_of(text), [f"{FILE}: <p> has an unknown language xml:lang='pt_BR'"])

    def test_english_code_is_not_a_known_language(self) -> None:
        text = metainfo(extra='<developer_name xml:lang="en">Me</developer_name>')
        self.assertEqual(
            errors_of(text), [f"{FILE}: <developer_name> has an unknown language xml:lang='en'"]
        )

    def test_name_and_summary_with_different_languages_is_a_warning(self) -> None:
        text = metainfo(summary_langs="uk de fr", name_langs="uk de pl")
        self.assertEqual(errors_of(text), [])
        self.assertEqual(
            warnings_of(text),
            [
                f"{FILE}: warning: <name> and <summary> are translated into different languages "
                "(only <name>: pl; only <summary>: fr)"
            ],
        )

    def test_translated_summary_but_untranslated_name_is_a_warning(self) -> None:
        text = metainfo(name_langs="")
        self.assertEqual(errors_of(text), [])
        self.assertEqual(len(warnings_of(text)), 1)

    def test_same_languages_in_another_order_are_fine(self) -> None:
        self.assertEqual(problems_of(metainfo(summary_langs="de uk", name_langs="uk de")), [])


class FilesAndMain(unittest.TestCase):
    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            problems = cm.check_files(Path(tmp), ["nope.metainfo.xml"], TODAY)
        self.assertEqual(len(problems), 1)
        self.assertTrue(str(problems[0]).startswith("nope.metainfo.xml: cannot read the file"))

    def test_file_that_is_not_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / FILE).write_bytes(b"<component>\xff</component>")
            problems = cm.check_files(Path(tmp), [FILE], TODAY)
        self.assertEqual([str(p) for p in problems], [f"{FILE}: the file is not valid UTF-8"])

    def test_each_file_is_checked_on_its_own(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / FILE).write_text(metainfo(), encoding="utf-8")
            other = "io.github.stdenis1980.Bad.metainfo.xml"
            (Path(tmp) / other).write_text(metainfo(drop=("name",)), encoding="utf-8")
            problems = cm.check_files(Path(tmp), [FILE, other], TODAY)
        self.assertEqual({p.path for p in problems if not p.warning}, {other})

    def run_main(self, args: list[str], cwd: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        previous = Path.cwd()
        os.chdir(cwd)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cm.main(args)
        finally:
            os.chdir(previous)
        return code, out.getvalue(), err.getvalue()

    def test_main_exit_codes_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / FILE
            # A real date in the past, so that the test does not depend on today's date.
            good.write_text(
                metainfo(releases='<release version="1.0" date="2020-01-01"/>'), "utf-8"
            )
            code, out, err = self.run_main([FILE], Path(tmp))
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(out, "1 metainfo files checked: 0 errors, 0 warnings\n")

            good.write_text(
                metainfo(
                    drop=("summary",),
                    name_langs="",
                    releases='<release version="1.0" date="2020-01-01"/>',
                ),
                "utf-8",
            )
            code, out, err = self.run_main([FILE], Path(tmp))
            self.assertEqual(code, 1)
            self.assertEqual(err, f"{FILE}: missing <summary>\n")
            self.assertIn("1 errors", out)

    def test_main_warnings_do_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            text = metainfo(name_langs="", releases='<release version="1.0" date="2020-01-01"/>')
            (Path(tmp) / FILE).write_text(text, encoding="utf-8")
            code, out, err = self.run_main([FILE], Path(tmp))
        self.assertEqual(code, 0)
        self.assertIn(f"{FILE}: warning: <name> and <summary>", err)
        self.assertIn("0 errors, 1 warnings", out)

    def test_main_wrong_call_is_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_main(["--bogus"], Path(tmp))
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)

    def test_main_help_is_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_main(["--help"], Path(tmp))[0], 0)


class TrackedFiles(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_only_tracked_metainfo_files_are_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "sub").mkdir()
            tracked = [f"sub/{FILE}", "other.metainfo.xml", "readme.txt"]
            for rel in [*tracked, "untracked.metainfo.xml"]:
                (root / rel).write_text("<component/>", encoding="utf-8")
            subprocess.run(["git", "add", *tracked], cwd=root, check=True)
            self.assertEqual(cm.tracked_metainfo_files(root), ["other.metainfo.xml", f"sub/{FILE}"])


if __name__ == "__main__":
    unittest.main()
