#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_site.py (the static-site checker).

Each test builds a small site in a temporary folder and calls check_site
functions directly (``check``, ``check_security_txt``). A correct site has
a page in every language from ``build_site.LANGUAGES`` (English at the
root, the others under /<lang>/), a local stylesheet, and a valid
security.txt.

Run: python3 tools/test_check_site.py
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build_site  # noqa: E402  (after sys.path.insert above)
import check_site as cs  # noqa: E402

FUTURE = "2099-01-01T00:00:00Z"
PAST = "2000-01-01T00:00:00Z"
CONTACT = "mailto:security@example.org"


def security_txt(
    *,
    contact: str | None = CONTACT,
    expires: str | None = FUTURE,
) -> str:
    lines = ["# test site"]
    if contact is not None:
        lines.append(f"Contact: {contact}")
    if expires is not None:
        lines.append(f"Expires: {expires}")
    return "\n".join(lines) + "\n"


def html_page(
    lang: str | None,
    *,
    body: str = '<a href="/">Home</a>\n',
    head: str = "",
) -> str:
    opening = "<html>" if lang is None else f'<html lang="{lang}">'
    return (
        f"<!DOCTYPE html>\n{opening}\n"
        '<head>\n<meta charset="utf-8">\n<title>Test</title>\n'
        f'<link rel="stylesheet" href="/style.css">\n{head}'
        f"</head>\n<body>\n{body}</body>\n</html>\n"
    )


def write_good_site(public: Path) -> None:
    """A minimal site that satisfies every rule in check_site.check()."""
    (public / "style.css").write_text("body{margin:0}\n", encoding="utf-8")
    well = public / ".well-known"
    well.mkdir(parents=True)
    (well / "security.txt").write_text(security_txt(), encoding="utf-8")
    about_body = '<a href="/">Home</a>\n<a href="/about/">About</a>\n'
    for lang in build_site.LANGUAGES:
        folder = public if lang == build_site.DEFAULT else public / lang
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "index.html").write_text(html_page(lang), encoding="utf-8")
        about = folder / "about"
        about.mkdir(exist_ok=True)
        (about / "index.html").write_text(html_page(lang, body=about_body), encoding="utf-8")


def put(public: Path, rel: str, text: str) -> None:
    path = public / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_and_check(tweak: Callable[[Path], None] | None = None) -> list[str]:
    """Write a good site, optionally change it, and return check() problems."""
    with tempfile.TemporaryDirectory() as tmp:
        public = Path(tmp)
        write_good_site(public)
        if tweak is not None:
            tweak(public)
        return cs.check(public)


class GoodSite(unittest.TestCase):
    def test_good_site_passes(self) -> None:
        self.assertEqual(build_and_check(), [])

    def test_external_link_is_allowed(self) -> None:
        def tweak(public: Path) -> None:
            put(
                public,
                "index.html",
                html_page(
                    "en",
                    body='<a href="https://example.org/page">off-site</a>\n',
                    head=(
                        '<link rel="canonical" href="https://example.org/">\n'
                        '<link rel="alternate" hreflang="uk" href="https://example.org/uk/">\n'
                    ),
                ),
            )

        self.assertEqual(build_and_check(tweak), [])

    def test_mailto_and_fragment_are_allowed(self) -> None:
        def tweak(public: Path) -> None:
            put(
                public,
                "index.html",
                html_page(
                    "en",
                    body='<a href="mailto:hi@example.org">mail</a>\n<a href="#top">top</a>\n',
                ),
            )

        self.assertEqual(build_and_check(tweak), [])

    def test_main_accepts_a_good_site_and_rejects_a_bad_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            public = Path(tmp)
            write_good_site(public)
            code, out, err = run_main(public)
            self.assertEqual((code, err), (0, ""))
            self.assertIn("site ok:", out)
            put(public, "index.html", html_page("en", body="<script></script>\n"))
            code, out, err = run_main(public)
            self.assertEqual(code, 1)
            self.assertIn("index.html: <script> is not allowed", err)


def run_main(public: Path) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cs.main(["check_site.py", str(public)])
    return code, out.getvalue(), err.getvalue()


class ScriptAndAttributes(unittest.TestCase):
    def test_script_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(public, "index.html", html_page("en", body="<script></script>\n"))

        self.assertEqual(build_and_check(tweak), ["index.html: <script> is not allowed"])

    def test_iframe_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(public, "index.html", html_page("en", body='<iframe src="/style.css"></iframe>\n'))

        self.assertEqual(build_and_check(tweak), ["index.html: <iframe> is not allowed"])

    def test_inline_style_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(public, "index.html", html_page("en", body='<p style="color: red">x</p>\n'))

        self.assertEqual(build_and_check(tweak), ["index.html: inline style= attribute"])

    def test_onclick_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(
                public,
                "index.html",
                html_page("en", body='<button onclick="alert(1)">go</button>\n'),
            )

        self.assertEqual(build_and_check(tweak), ["index.html: event handler attribute onclick="])

    def test_other_event_handler_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(
                public,
                "index.html",
                html_page("en", body='<div onmouseover="go()">x</div>\n'),
            )

        self.assertEqual(
            build_and_check(tweak), ["index.html: event handler attribute onmouseover="]
        )


class ExternalResources(unittest.TestCase):
    def test_external_src_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(
                public,
                "index.html",
                html_page("en", body='<img src="https://cdn.example/pic.png" alt="">\n'),
            )

        self.assertEqual(
            build_and_check(tweak),
            ["index.html: loads https://cdn.example/pic.png from another host"],
        )

    def test_external_stylesheet_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(
                public,
                "index.html",
                html_page(
                    "en",
                    head='<link rel="stylesheet" href="https://cdn.example/site.css">\n',
                ),
            )

        self.assertEqual(
            build_and_check(tweak),
            ["index.html: loads https://cdn.example/site.css from another host"],
        )

    def test_protocol_relative_src_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(
                public,
                "index.html",
                html_page("en", body='<img src="//cdn.example/pic.png" alt="">\n'),
            )

        self.assertEqual(
            build_and_check(tweak),
            ["index.html: loads //cdn.example/pic.png from another host"],
        )


class WaysAroundTheRules(unittest.TestCase):
    def test_base_style_element_and_svg_use_are_reported(self) -> None:
        cases = {
            '<base href="https://evil.example/">': "<base> is not allowed",
            "<style>@import url(https://evil.example/x.css);</style>": "<style> element",
            '<svg><use href="https://evil.example/x.svg#a"/></svg>': "from another host",
        }
        for html, expected in cases.items():
            with self.subTest(html=html):

                def tweak(public: Path, html: str = html) -> None:
                    put(public, "index.html", html_page("en", head=html + "\n"))

                problems = build_and_check(tweak)
                self.assertTrue(any(expected in p for p in problems), problems)

    def test_rel_is_read_as_words_in_any_case(self) -> None:
        def tweak(public: Path) -> None:
            head = (
                '<link rel="Alternate" hreflang="uk" href="https://vilnaos.example/uk/">\n'
                '<link rel="alternate stylesheet" href="https://evil.example/x.css">\n'
            )
            put(public, "index.html", html_page("en", head=head))

        self.assertEqual(
            build_and_check(tweak),
            ["index.html: loads https://evil.example/x.css from another host"],
        )

    def test_a_page_that_cannot_be_read_is_a_problem_not_a_traceback(self) -> None:
        def tweak(public: Path) -> None:
            (public / "index.html").write_bytes(b"\xff\xfe<html>")

        self.assertEqual(build_and_check(tweak), ["index.html: is not valid UTF-8"])


class LocalLinks(unittest.TestCase):
    def test_link_to_a_missing_file_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(public, "index.html", html_page("en", body='<a href="/missing.html">x</a>\n'))

        self.assertEqual(build_and_check(tweak), ["index.html: /missing.html does not exist"])

    def test_relative_link_to_a_missing_file_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(public, "index.html", html_page("en", body='<a href="nowhere.html">x</a>\n'))

        self.assertEqual(build_and_check(tweak), ["index.html: nowhere.html does not exist"])

    def test_missing_local_resource_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(public, "index.html", html_page("en", body='<img src="/no.png" alt="">\n'))

        self.assertEqual(build_and_check(tweak), ["index.html: /no.png does not exist"])

    def test_percent_encoded_link_is_resolved(self) -> None:
        def tweak(public: Path) -> None:
            put(public, "my file.html", html_page("en"))
            for lang in build_site.LANGUAGES:
                if lang != build_site.DEFAULT:
                    put(public, f"{lang}/my file.html", html_page(lang))
            put(public, "index.html", html_page("en", body='<a href="/my%20file.html">x</a>\n'))

        self.assertEqual(build_and_check(tweak), [])

    def test_link_pointing_outside_the_site_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(public, "index.html", html_page("en", body='<a href="/../secret.html">x</a>\n'))

        problems = build_and_check(tweak)
        self.assertEqual(problems, ["index.html: /../secret.html points outside the site"])


class Languages(unittest.TestCase):
    def test_page_missing_in_one_language_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            (public / "uk" / "about" / "index.html").unlink()

        problems = build_and_check(tweak)
        self.assertIn("about/index.html: no uk translation at uk/about/index.html", problems)
        # Every remaining about page points at the same missing twin.
        about_problems = [p for p in problems if "no uk translation at uk/about/index.html" in p]
        self.assertEqual(len(about_problems), len(build_site.LANGUAGES) - 1)
        self.assertFalse(any("uk/index.html" in p and "translation" in p for p in problems))

    def test_wrong_lang_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(public, "uk/index.html", html_page("fr"))

        self.assertEqual(build_and_check(tweak), ["uk/index.html: lang is fr, expected uk"])

    def test_wrong_lang_on_the_english_page(self) -> None:
        def tweak(public: Path) -> None:
            put(public, "index.html", html_page("uk"))

        self.assertEqual(build_and_check(tweak), ["index.html: lang is uk, expected en"])

    def test_missing_lang_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(public, "index.html", html_page(None))

        self.assertEqual(build_and_check(tweak), ["index.html: <html> has no lang"])


class SecurityTxt(unittest.TestCase):
    def test_missing_contact_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(public, ".well-known/security.txt", security_txt(contact=None))

        self.assertEqual(build_and_check(tweak), ["security.txt: no Contact"])

    def test_expired_date_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(public, ".well-known/security.txt", security_txt(expires=PAST))

        self.assertEqual(
            build_and_check(tweak),
            [f"security.txt: Expires {PAST} has passed, renew it"],
        )

    def test_missing_contact_and_expired_date(self) -> None:
        def tweak(public: Path) -> None:
            put(public, ".well-known/security.txt", security_txt(contact=None, expires=PAST))

        self.assertEqual(
            build_and_check(tweak),
            [
                "security.txt: no Contact",
                f"security.txt: Expires {PAST} has passed, renew it",
            ],
        )

    def test_missing_file_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            (public / ".well-known" / "security.txt").unlink()

        self.assertEqual(build_and_check(tweak), [".well-known/security.txt is missing"])

    def test_invalid_expires_is_reported(self) -> None:
        def tweak(public: Path) -> None:
            put(public, ".well-known/security.txt", security_txt(expires="soon"))

        self.assertEqual(build_and_check(tweak), ["security.txt: Expires 'soon' is not a date"])

    def test_expires_without_a_time_zone_is_reported(self) -> None:
        for naive in ("2099-01-01T00:00:00", "2099-01-01"):
            with self.subTest(expires=naive):

                def tweak(public: Path, naive: str = naive) -> None:
                    put(public, ".well-known/security.txt", security_txt(expires=naive))

                self.assertEqual(
                    build_and_check(tweak),
                    [f"security.txt: Expires {naive} has no time zone (RFC 9116)"],
                )

    def test_check_security_txt_is_callable_on_its_own(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            public = Path(tmp)
            well = public / ".well-known"
            well.mkdir()
            (well / "security.txt").write_text(security_txt(), encoding="utf-8")
            self.assertEqual(cs.check_security_txt(public), [])
            (well / "security.txt").write_text(security_txt(contact=None), encoding="utf-8")
            self.assertEqual(cs.check_security_txt(public), ["security.txt: no Contact"])


class EmptySite(unittest.TestCase):
    def test_no_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            public = Path(tmp)
            self.assertEqual(cs.check(public), [f"{public}: no pages"])


class CommittedSite(unittest.TestCase):
    def test_committed_public_passes_check(self) -> None:
        public = ROOT / "site" / "public"
        if not public.is_dir():
            self.skipTest("site/public is not in this tree")
        self.assertEqual(cs.check(public), [])


if __name__ == "__main__":
    unittest.main()
