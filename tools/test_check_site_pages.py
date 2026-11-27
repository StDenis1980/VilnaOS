#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_site_pages.py (the check of the site language versions).

Each test builds a small site (src and public, three languages) in a temporary
folder, changes one thing and checks the rule that must catch it. The last
tests run the checker on the real site folder of the repo.

Run: python3 tools/test_check_site_pages.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_site_pages as csp  # noqa: E402  (after sys.path.insert above)

LANGS = ("en", "uk", "de")
BASES = ("index.html", "about/index.html", "404.html")
HOST = "https://example.org"
TITLES = {"en": "Home", "uk": "Головна", "de": "Startseite"}


def url_of(lang: str, base: str) -> str:
    root = "/" if lang == "en" else f"/{lang}/"
    return (root + base).removesuffix("index.html")


UNSET = "\0unset"


def page(
    lang: str,
    base: str,
    *,
    html_lang: str | None = UNSET,
    title: str | None = UNSET,
    description: str | None = UNSET,
    head: str | None = None,
    body: str = '<img src="/a.svg" alt="Logo">\n',
    links: dict[str, str] | None = None,
) -> str:
    """One good page; keyword arguments break it in one place."""
    if html_lang == UNSET:
        html_lang = lang
    if title == UNSET:
        title = f"{TITLES[lang]} {base}"
    if description == UNSET:
        description = f"About {base} in {lang}"
    if head is None:
        wanted = (
            links
            if links is not None
            else {code: f"{HOST}{url_of(code, base)}" for code in (*LANGS, "x-default")}
        )
        if "x-default" in wanted and links is None:
            wanted["x-default"] = f"{HOST}{url_of('en', base)}"
        head = "".join(
            f'<link rel="alternate" hreflang="{c}" href="{u}">\n' for c, u in wanted.items()
        )
        if base == "404.html":
            head = ""
    opening = "<html>" if html_lang is None else f'<html lang="{html_lang}">'
    parts = [f'<!doctype html>\n{opening}\n<head>\n<meta charset="utf-8">\n']
    if title is not None:
        parts.append(f"<title>{title}</title>\n")
    if description is not None:
        parts.append(f'<meta name="description" content="{description}">\n')
    parts.append(head)
    parts.append(f"</head>\n<body>\n{body}</body>\n</html>\n")
    return "".join(parts)


LAYOUT = (
    '<!doctype html>\n<html lang="{{lang}}">\n<head>\n<title>{{title}}</title>\n</head>\n'
    '<body><img src="/icon.svg" alt="">\n{{main}}\n</body>\n</html>\n'
)
SRC_PAGE = (
    "<!-- title: {{page_title}} -->\n<!-- description: {{page_desc}} -->\n"
    '<img src="/a.svg" alt="{{logo_alt}}">\n<p>{{text}}</p>\n'
)
EN_STRINGS = {
    "page_title": "Home",
    "page_desc": "About the home",
    "logo_alt": "Logo",
    "text": "Hello {{name}}",
    "name": "world",
}


class Site:
    """A fake site folder: edit ``public`` and ``src`` files, then ``write``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.public: dict[str, str] = {}
        for lang in LANGS:
            for base in BASES:
                rel = base if lang == "en" else f"{lang}/{base}"
                self.public[rel] = page(lang, base)
        self.i18n: dict[str, object] = {lang: dict(EN_STRINGS) for lang in LANGS}
        for lang in LANGS:
            if lang != "en":
                self.i18n[lang]["page_title"] = f"Title {lang}"  # type: ignore[index]
        self.layout = LAYOUT
        self.pages = {"home.html": SRC_PAGE}

    def write(self) -> Path:
        for rel, text in self.public.items():
            target = self.root / "public" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        i18n = self.root / "src" / "i18n"
        i18n.mkdir(parents=True, exist_ok=True)
        for lang, data in self.i18n.items():
            text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            (i18n / f"{lang}.json").write_text(text, encoding="utf-8")
        (self.root / "src" / "layout.html").write_text(self.layout, encoding="utf-8")
        pages = self.root / "src" / "pages"
        pages.mkdir(parents=True, exist_ok=True)
        for name, text in self.pages.items():
            (pages / name).write_text(text, encoding="utf-8")
        return self.root


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.site = Site(self.tmp / "site")

    def run_check(self) -> csp.Report:
        report, _ = csp.check_site(self.site.write())
        return report

    def messages(self, report: csp.Report, rule: str | None = None) -> list[str]:
        return [str(p) for p in report.sorted_problems() if rule is None or p.rule == rule]

    def assertRule(self, report: csp.Report, rule: str, *needles: str) -> None:
        found = self.messages(report, rule)
        self.assertTrue(found, f"no {rule} problem; got {self.messages(report)}")
        text = "\n".join(found)
        for needle in needles:
            self.assertIn(needle, text)
        self.assertTrue(all(not p.warning for p in report.problems if p.rule == rule))

    def assertClean(self, report: csp.Report) -> None:
        self.assertEqual(self.messages(report), [])

    def public_edit(self, rel: str, fn: Callable[[str], str]) -> None:
        self.site.public[rel] = fn(self.site.public[rel])


class GoodSite(Base):
    def test_good_site_has_no_problems(self) -> None:
        self.assertClean(self.run_check())

    def test_main_ok(self) -> None:
        self.site.write()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = csp.main([str(self.site.root)])
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertIn("9 pages checked: 0 errors, 0 warnings", out.getvalue())


class PageSets(Base):
    def test_missing_page_in_a_language(self) -> None:
        del self.site.public["uk/about/index.html"]
        report = self.run_check()
        self.assertRule(report, "page-missing", "uk/about/index.html", "exists in en, de")

    def test_page_only_in_a_translation(self) -> None:
        self.site.public["de/extra/index.html"] = page("de", "about/index.html")
        report = self.run_check()
        self.assertRule(report, "page-missing", "extra/index.html: page is missing")
        self.assertIn("exists in de", "\n".join(self.messages(report, "page-missing")))

    def test_missing_default_page(self) -> None:
        del self.site.public["index.html"]
        report = self.run_check()
        self.assertRule(report, "page-missing", "site/public/index.html")

    def test_language_without_any_page(self) -> None:
        for base in BASES:
            del self.site.public[f"de/{base}"]
        report = self.run_check()
        self.assertRule(report, "page-missing", "no pages for language de")

    def test_unknown_language_folder(self) -> None:
        for base in BASES:
            self.site.public[f"fr/{base}"] = page("uk", base).replace('lang="uk"', 'lang="fr"')
        report = self.run_check()
        self.assertRule(report, "page-extra", "public/fr/", "no i18n/fr.json")
        self.assertEqual(self.messages(report, "page-missing"), [])

    def test_english_folder_is_stray(self) -> None:
        self.site.public["en/index.html"] = page("en", "index.html")
        report = self.run_check()
        self.assertRule(report, "page-extra", "English pages live at the root")

    def test_english_folder_page_is_not_counted_as_a_page(self) -> None:
        self.site.public["en/only-here/index.html"] = page("en", "index.html")
        report = self.run_check()
        self.assertRule(report, "page-extra", "public/en/")
        self.assertEqual(self.messages(report, "page-missing"), [])

    def test_non_language_folder_is_not_a_language(self) -> None:
        # A three-letter or longer folder is a page, not a language.
        for lang in LANGS:
            root = "" if lang == "en" else f"{lang}/"
            self.site.public[f"{root}faq/index.html"] = page(lang, "about/index.html")
        report = self.run_check()
        self.assertEqual(self.messages(report, "page-missing"), [])
        self.assertEqual(self.messages(report, "page-extra"), [])

    def test_language_file_without_folder(self) -> None:
        self.site.i18n["fr"] = dict(EN_STRINGS)
        report = self.run_check()
        self.assertRule(report, "page-missing", "no pages for language fr")

    def test_empty_public_is_reported(self) -> None:
        self.site.public.clear()
        report = self.run_check()
        self.assertRule(report, "page-missing", "public: no pages")


class HtmlLang(Base):
    def test_missing_lang(self) -> None:
        self.site.public["uk/index.html"] = page("uk", "index.html", html_lang=None)
        self.assertRule(self.run_check(), "html-lang", "uk/index.html: <html> has no lang")

    def test_empty_lang(self) -> None:
        self.site.public["uk/index.html"] = page("uk", "index.html", html_lang="  ")
        self.assertRule(self.run_check(), "html-lang", "no lang")

    def test_wrong_lang(self) -> None:
        self.site.public["uk/index.html"] = page("uk", "index.html", html_lang="en")
        self.assertRule(self.run_check(), "html-lang", "'en', expected 'uk'")

    def test_regional_lang_is_not_the_language(self) -> None:
        self.site.public["uk/index.html"] = page("uk", "index.html", html_lang="uk-UA")
        self.assertRule(self.run_check(), "html-lang", "'uk-UA', expected 'uk'")

    def test_lang_case_does_not_matter(self) -> None:
        self.site.public["uk/index.html"] = page("uk", "index.html", html_lang="UK")
        self.assertClean(self.run_check())

    def test_default_language_at_root(self) -> None:
        self.site.public["index.html"] = page("en", "index.html", html_lang="uk")
        self.assertRule(self.run_check(), "html-lang", "'uk', expected 'en'")

    def test_first_html_tag_counts(self) -> None:
        self.public_edit("uk/index.html", lambda t: t.replace("</body>", '<html lang="en"></body>'))
        self.assertClean(self.run_check())


class Titles(Base):
    def test_missing_title(self) -> None:
        self.site.public["uk/index.html"] = page("uk", "index.html", title=None)
        self.assertRule(self.run_check(), "title-empty", "uk/index.html: no title")

    def test_empty_and_blank_titles(self) -> None:
        for blank in ("", "   ", "\n\t "):
            with self.subTest(blank=repr(blank)):
                self.site.public["uk/index.html"] = page("uk", "index.html", title=blank)
                self.assertRule(self.run_check(), "title-empty", "title is empty")

    def test_non_breaking_space_title_is_empty(self) -> None:
        self.site.public["uk/index.html"] = page("uk", "index.html", title="&nbsp;&#x2003;")
        self.assertRule(self.run_check(), "title-empty", "title is empty")

    def test_two_titles(self) -> None:
        self.public_edit(
            "uk/index.html", lambda t: t.replace("</head>", "<title>Other</title>\n</head>")
        )
        self.assertRule(self.run_check(), "title-empty", "2 title elements")

    def test_svg_title_is_not_the_page_title(self) -> None:
        self.site.public["uk/index.html"] = page(
            "uk", "index.html", title=None, body="<svg><title>Icon</title></svg>\n"
        )
        self.assertRule(self.run_check(), "title-empty", "no title")

    def test_svg_title_does_not_count_as_a_second_title(self) -> None:
        self.site.public["uk/index.html"] = page(
            "uk", "index.html", body="<svg><title>Icon</title></svg>\n"
        )
        self.assertClean(self.run_check())

    def test_title_in_comment_does_not_count(self) -> None:
        self.site.public["uk/index.html"] = page(
            "uk", "index.html", title=None, head="<!-- <title>Hidden</title> -->\n"
        )
        report = self.run_check()
        self.assertRule(report, "title-empty", "no title")

    def test_title_with_entities_and_tags_is_read_as_text(self) -> None:
        self.site.public["uk/index.html"] = page("uk", "index.html", title="A &amp; B")
        self.assertClean(self.run_check())

    def test_same_title_in_two_languages(self) -> None:
        for lang in ("uk", "de"):
            self.site.public[f"{lang}/index.html"] = page(lang, "index.html", title="Same")
        report = self.run_check()
        self.assertRule(report, "title-same", "public/de/index.html: title is the same in de, uk")

    def test_same_title_ignores_case_and_spacing(self) -> None:
        self.site.public["index.html"] = page("en", "index.html", title="Vilna  OS")
        self.site.public["de/index.html"] = page("de", "index.html", title=" vilna os\n")
        self.assertRule(self.run_check(), "title-same", "title is the same in en, de")

    def test_same_title_across_unicode_forms(self) -> None:
        self.site.public["index.html"] = page("en", "index.html", title="Caf\u00e9")
        self.site.public["de/index.html"] = page("de", "index.html", title="Cafe\u0301")
        self.assertRule(self.run_check(), "title-same", "title is the same in en, de")

    def test_same_title_on_different_pages_is_fine(self) -> None:
        for lang in LANGS:
            root = "" if lang == "en" else f"{lang}/"
            self.site.public[f"{root}about/index.html"] = page(
                lang, "about/index.html", title=TITLES[lang]
            )
        self.assertClean(self.run_check())

    def test_missing_description(self) -> None:
        self.site.public["de/index.html"] = page("de", "index.html", description=None)
        self.assertRule(self.run_check(), "description-empty", "de/index.html: no meta description")

    def test_empty_description(self) -> None:
        for blank in ("", "  "):
            with self.subTest(blank=repr(blank)):
                self.site.public["de/index.html"] = page("de", "index.html", description=blank)
                self.assertRule(self.run_check(), "description-empty", "meta description is empty")

    def test_description_without_content(self) -> None:
        self.site.public["de/index.html"] = page(
            "de", "index.html", description=None, head='<meta name="description">\n'
        )
        self.assertRule(self.run_check(), "description-empty", "meta description is empty")

    def test_description_name_is_not_case_sensitive(self) -> None:
        text = page("de", "index.html", description=None)
        self.site.public["de/index.html"] = text.replace(
            "</head>", '<META NAME="Description" CONTENT="Ja">\n</head>'
        )
        self.assertClean(self.run_check())

    def test_other_meta_is_not_a_description(self) -> None:
        self.site.public["de/index.html"] = page(
            "de", "index.html", description=None, head='<meta name="keywords" content="x">\n'
        )
        self.assertRule(self.run_check(), "description-empty", "no meta description")

    def test_same_description_in_two_languages(self) -> None:
        for lang in ("en", "uk"):
            rel = "index.html" if lang == "en" else f"{lang}/index.html"
            self.site.public[rel] = page(lang, "index.html", description="One text")
        self.assertRule(
            self.run_check(), "description-same", "meta description is the same in en, uk"
        )

    def test_404_pages_are_compared_too(self) -> None:
        for lang in ("en", "uk"):
            rel = "404.html" if lang == "en" else f"{lang}/404.html"
            self.site.public[rel] = page(lang, "404.html", title="Gone")
        self.assertRule(self.run_check(), "title-same", "404.html: title is the same")


class Images(Base):
    def body(self, tag: str) -> None:
        self.site.public["uk/index.html"] = page("uk", "index.html", body=tag + "\n")

    def test_missing_alt(self) -> None:
        self.body('<img src="/x.png">')
        self.assertRule(self.run_check(), "img-alt", "uk/index.html: <img /x.png> has no alt")

    def test_empty_alt_marks_a_decorative_image(self) -> None:
        self.body('<img src="/x.png" alt="">')
        self.assertClean(self.run_check())

    def test_bare_alt_attribute_is_empty_alt(self) -> None:
        self.body('<img src="/x.png" alt>')
        self.assertClean(self.run_check())

    def test_blank_alt(self) -> None:
        self.body('<img src="/x.png" alt="   ">')
        self.assertRule(self.run_check(), "img-alt-blank", "only spaces")

    def test_upper_case_tag_and_attribute(self) -> None:
        self.body('<IMG SRC="/x.png">')
        self.assertRule(self.run_check(), "img-alt", "has no alt")
        self.body('<IMG SRC="/x.png" ALT="Fine">')
        self.assertClean(self.run_check())

    def test_every_image_is_checked(self) -> None:
        self.body('<img src="/1.png" alt="A"><img src="/2.png"><img src="/3.png">')
        found = self.messages(self.run_check(), "img-alt")
        self.assertEqual(len(found), 2)

    def test_self_closing_image(self) -> None:
        self.body('<img src="/x.png" />')
        self.assertRule(self.run_check(), "img-alt", "has no alt")

    def test_image_in_comment_or_script_is_not_an_image(self) -> None:
        self.body('<!-- <img src="/x.png"> -->\n<script>var s = \'<img src="/y.png">\';</script>')
        report = self.run_check()
        self.assertEqual(self.messages(report, "img-alt"), [])

    def test_long_src_is_shortened_in_the_message(self) -> None:
        self.body('<img src="/' + "a" * 200 + '.png">')
        (line,) = self.messages(self.run_check(), "img-alt")
        self.assertLess(len(line), 200)


class ExternalScripts(Base):
    def head(self, text: str) -> None:
        self.site.public["uk/index.html"] = page("uk", "index.html").replace(
            "</head>", text + "\n</head>"
        )

    def test_external_script_urls(self) -> None:
        urls = [
            "https://cdn.example.net/x.js",
            "http://cdn.example.net/x.js",
            "//cdn.example.net/x.js",
            "\\\\cdn.example.net/x.js",
            "/\\cdn.example.net/x.js",
            "HTTPS://CDN.EXAMPLE.NET/X.JS",
            "ht\ntps://cdn.example.net/x.js",
            "  https://cdn.example.net/x.js",
            "data:text/javascript,alert(1)",
            "javascript:alert(1)",
            "blob:https://example.org/x",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.head(f'<script src="{url}"></script>')
                self.assertRule(self.run_check(), "external-script", "uk/index.html: <script>")

    def test_local_script_and_inline_script_are_not_external(self) -> None:
        self.head('<script src="/app.js"></script><script>var a = 1;</script>')
        self.head('<script src="app.js"></script><script src="../x.js"></script>')
        self.assertClean(self.run_check())

    def test_script_in_comment_is_ignored(self) -> None:
        self.head('<!-- <script src="https://cdn.example.net/x.js"></script> -->')
        self.assertClean(self.run_check())

    def test_empty_src_is_not_external(self) -> None:
        self.head('<script src=""></script>')
        self.assertClean(self.run_check())

    def test_external_modulepreload(self) -> None:
        self.head('<link rel="modulepreload" href="https://cdn.example.net/m.js">')
        self.assertRule(self.run_check(), "external-script", "modulepreload")

    def test_local_modulepreload(self) -> None:
        self.head('<link rel="modulepreload" href="/m.js">')
        self.assertClean(self.run_check())

    def test_external_base(self) -> None:
        self.head('<base href="https://cdn.example.net/">')
        self.assertRule(self.run_check(), "external-script", "<base href>")

    def test_local_base(self) -> None:
        self.head('<base href="/uk/">')
        self.assertClean(self.run_check())

    def test_external_link_to_other_site_is_fine(self) -> None:
        self.site.public["uk/index.html"] = page(
            "uk", "index.html", body='<a href="https://example.net/">x</a>\n'
        )
        self.assertClean(self.run_check())


class Hreflang(Base):
    def set_links(self, rel: str, lang: str, base: str, links: dict[str, str]) -> None:
        self.site.public[rel] = page(lang, base, links=links)

    def good_links(self, base: str = "about/index.html") -> dict[str, str]:
        links = {c: f"{HOST}{url_of(c, base)}" for c in LANGS}
        links["x-default"] = f"{HOST}{url_of('en', base)}"
        return links

    def test_no_hreflang(self) -> None:
        self.site.public["uk/about/index.html"] = page("uk", "about/index.html", head="")
        self.assertRule(self.run_check(), "hreflang-missing", "uk/about/index.html")

    def test_404_needs_no_hreflang(self) -> None:
        self.assertEqual(self.messages(self.run_check(), "hreflang-missing"), [])

    def test_language_missing_from_set(self) -> None:
        links = self.good_links()
        del links["de"]
        self.set_links("uk/about/index.html", "uk", "about/index.html", links)
        self.assertRule(self.run_check(), "hreflang-set", "hreflang de is missing")

    def test_x_default_missing(self) -> None:
        links = self.good_links()
        del links["x-default"]
        self.set_links("about/index.html", "en", "about/index.html", links)
        self.assertRule(self.run_check(), "hreflang-set", "hreflang x-default is missing")

    def test_unknown_language_code(self) -> None:
        links = self.good_links()
        links["fr"] = f"{HOST}/fr/about/"
        self.set_links("about/index.html", "en", "about/index.html", links)
        report = self.run_check()
        self.assertRule(report, "hreflang-set", "hreflang fr is not a site language")
        self.assertRule(report, "hreflang-target", "hreflang fr")

    def test_code_case_does_not_matter(self) -> None:
        links = {c.upper(): u for c, u in self.good_links().items()}
        self.set_links("about/index.html", "en", "about/index.html", links)
        self.assertClean(self.run_check())

    def test_relative_url(self) -> None:
        links = self.good_links()
        links["uk"] = "/uk/about/"
        self.set_links("about/index.html", "en", "about/index.html", links)
        self.assertRule(self.run_check(), "hreflang-url", "is not a full URL")

    def test_query_in_url(self) -> None:
        links = self.good_links()
        links["uk"] = f"{HOST}/uk/about/?x=1"
        self.set_links("about/index.html", "en", "about/index.html", links)
        self.assertRule(self.run_check(), "hreflang-url", "query or a fragment")

    def test_fragment_in_url(self) -> None:
        links = self.good_links()
        links["uk"] = f"{HOST}/uk/about/#top"
        self.set_links("about/index.html", "en", "about/index.html", links)
        self.assertRule(self.run_check(), "hreflang-url", "query or a fragment")

    def test_other_host_for_one_link(self) -> None:
        links = self.good_links()
        links["uk"] = "https://evil.example.net/uk/about/"
        self.set_links("about/index.html", "en", "about/index.html", links)
        self.assertRule(self.run_check(), "hreflang-url", "another host")

    def test_other_scheme_for_one_link(self) -> None:
        links = self.good_links()
        links["uk"] = "ftp://example.org/uk/about/"
        self.set_links("about/index.html", "en", "about/index.html", links)
        self.assertRule(self.run_check(), "hreflang-url", "is not a full URL")

    def test_target_is_not_a_page(self) -> None:
        links = self.good_links()
        links["uk"] = f"{HOST}/uk/nowhere/"
        self.set_links("about/index.html", "en", "about/index.html", links)
        self.assertRule(self.run_check(), "hreflang-target", "/uk/nowhere/ is not a page")

    def test_url_without_trailing_slash_is_not_the_page_url(self) -> None:
        links = self.good_links()
        links["uk"] = f"{HOST}/uk/about"
        self.set_links("about/index.html", "en", "about/index.html", links)
        self.assertRule(self.run_check(), "hreflang-target", "/uk/about is not a page")

    def test_index_html_form_of_the_url_is_accepted(self) -> None:
        links = self.good_links()
        links["uk"] = f"{HOST}/uk/about/index.html"
        self.set_links("about/index.html", "en", "about/index.html", links)
        report = self.run_check()
        self.assertEqual(self.messages(report, "hreflang-target"), [])

    def test_target_in_another_language(self) -> None:
        links = self.good_links()
        links["uk"] = f"{HOST}/de/about/"
        self.set_links("about/index.html", "en", "about/index.html", links)
        self.assertRule(self.run_check(), "hreflang-target", "hreflang uk points at de/about")

    def test_target_is_another_page(self) -> None:
        links = self.good_links()
        links["uk"] = f"{HOST}/uk/"
        self.set_links("about/index.html", "en", "about/index.html", links)
        report = self.run_check()
        self.assertRule(report, "hreflang-reciprocal", "does not link back")

    def test_page_does_not_list_itself(self) -> None:
        links = self.good_links()
        links["uk"] = f"{HOST}/uk/"
        self.set_links("uk/about/index.html", "uk", "about/index.html", links)
        report = self.run_check()
        self.assertRule(report, "hreflang-self", "hreflang uk does not point at the page itself")

    def test_page_does_not_list_itself_at_all(self) -> None:
        links = self.good_links()
        del links["uk"]
        self.set_links("uk/about/index.html", "uk", "about/index.html", links)
        report = self.run_check()
        self.assertRule(report, "hreflang-set", "hreflang uk is missing")

    def test_not_reciprocal(self) -> None:
        links = self.good_links()
        links["en"] = f"{HOST}/de/about/"  # the uk page sends English readers to German
        self.set_links("uk/about/index.html", "uk", "about/index.html", links)
        report = self.run_check()
        self.assertRule(report, "hreflang-reciprocal", "hreflang uk links to uk/about/index.html")

    def test_duplicate_code(self) -> None:
        head = "".join(
            f'<link rel="alternate" hreflang="{c}" href="{u}">\n'
            for c, u in [*self.good_links().items(), ("uk", f"{HOST}/uk/about/")]
        )
        self.site.public["about/index.html"] = page("en", "about/index.html", head=head)
        self.assertRule(self.run_check(), "hreflang-duplicate", "hreflang uk is listed twice")

    def test_x_default_differs_in_the_group(self) -> None:
        links = self.good_links()
        links["x-default"] = f"{HOST}/de/about/"
        self.set_links("uk/about/index.html", "uk", "about/index.html", links)
        self.assertRule(self.run_check(), "hreflang-reciprocal", "x-default differs")

    def test_x_default_to_a_missing_page(self) -> None:
        links = self.good_links()
        links["x-default"] = f"{HOST}/none/"
        self.set_links("about/index.html", "en", "about/index.html", links)
        self.assertRule(self.run_check(), "hreflang-target", "hreflang x-default")

    def test_hreflang_in_comment_does_not_count(self) -> None:
        head = (
            "<!--\n"
            + "".join(
                f'<link rel="alternate" hreflang="{c}" href="{u}">\n'
                for c, u in self.good_links().items()
            )
            + "-->\n"
        )
        self.site.public["about/index.html"] = page("en", "about/index.html", head=head)
        self.assertRule(self.run_check(), "hreflang-missing", "about/index.html")

    def test_link_without_alternate_rel_does_not_count(self) -> None:
        head = "".join(
            f'<link rel="canonical" hreflang="{c}" href="{u}">\n'
            for c, u in self.good_links().items()
        )
        self.site.public["about/index.html"] = page("en", "about/index.html", head=head)
        self.assertRule(self.run_check(), "hreflang-missing", "about/index.html")

    def test_a_tag_with_hreflang_is_not_a_head_link(self) -> None:
        # The language menu in the body uses <a hreflang>; it must not count.
        self.site.public["about/index.html"] = page(
            "en", "about/index.html", head="", body='<a href="/uk/about/" hreflang="uk">uk</a>\n'
        )
        self.assertRule(self.run_check(), "hreflang-missing", "about/index.html")

    def test_rel_may_have_several_words(self) -> None:
        head = "".join(
            f'<link rel="alternate nofollow" hreflang="{c}" href="{u}">\n'
            for c, u in self.good_links().items()
        )
        self.site.public["about/index.html"] = page("en", "about/index.html", head=head)
        report = self.run_check()
        self.assertEqual(self.messages(report, "hreflang-missing"), [])
        self.assertEqual(self.messages(report, "hreflang-set"), [])

    def test_hreflang_on_404_is_checked_when_present(self) -> None:
        links = self.good_links("404.html")
        del links["de"]
        self.site.public["404.html"] = page("en", "404.html", links=links)
        self.site.public["404.html"] = self.site.public["404.html"].replace(
            "</head>",
            "".join(f'<link rel="alternate" hreflang="{c}" href="{u}">\n' for c, u in links.items())
            + "</head>",
        )
        self.assertRule(self.run_check(), "hreflang-set", "hreflang de is missing")

    def test_stray_pages_do_not_break_the_check(self) -> None:
        self.site.public["fr/index.html"] = page("uk", "index.html").replace(
            'lang="uk"', 'lang="fr"'
        )
        report = self.run_check()
        self.assertTrue(report.errors)


class Files(Base):
    def test_not_utf8(self) -> None:
        self.site.write()
        (self.site.root / "public" / "uk" / "index.html").write_bytes(b"<html lang='uk'>\xff\xfe")
        report, _ = csp.check_site(self.site.root)
        self.assertRule(report, "unreadable", "uk/index.html: page is not valid UTF-8")

    def test_empty_file(self) -> None:
        self.site.public["uk/index.html"] = ""
        report = self.run_check()
        self.assertRule(report, "html-lang", "uk/index.html")
        self.assertRule(report, "title-empty", "no title")

    def test_bom_is_accepted(self) -> None:
        text = page("uk", "index.html")
        self.site.write()
        (self.site.root / "public" / "uk" / "index.html").write_bytes(
            b"\xef\xbb\xbf" + text.encode("utf-8")
        )
        report, _ = csp.check_site(self.site.root)
        self.assertEqual([str(p) for p in report.problems], [])

    def test_huge_page_is_not_parsed(self) -> None:
        self.site.public["uk/index.html"] = page("uk", "index.html", body="x" * 100)
        self.site.write()
        with mock.patch.object(csp, "MAX_PAGE_BYTES", 50):
            report, _ = csp.check_site(self.site.root)
        self.assertRule(report, "unreadable", "larger than")

    def test_symlinked_page_is_not_followed(self) -> None:
        self.site.write()
        outside = self.tmp / "outside.html"
        outside.write_text(page("uk", "index.html"), encoding="utf-8")
        link = self.site.root / "public" / "uk" / "index.html"
        link.unlink()
        link.symlink_to(outside)
        report, _ = csp.check_site(self.site.root)
        self.assertRule(report, "unreadable", "symlink")

    def test_symlinked_folder_is_not_followed(self) -> None:
        self.site.write()
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "index.html").write_text(page("de", "index.html"), encoding="utf-8")
        (self.site.root / "public" / "de2").symlink_to(outside, target_is_directory=True)
        report, _ = csp.check_site(self.site.root)
        self.assertRule(report, "unreadable", "de2: folder is a symlink")

    def test_unclosed_title_does_not_crash(self) -> None:
        self.site.public["uk/index.html"] = page("uk", "index.html", title=None).replace(
            "</head>", "<title>Never closed\n</head>"
        )
        report = self.run_check()
        self.assertEqual(self.messages(report, "unreadable"), [])
        self.assertEqual(self.messages(report, "title-empty"), [])

    def test_broken_markup_does_not_crash(self) -> None:
        junk = "<!--" + "<<" * 50 + "<![CDATA[ <img <script src=x " + "<a" * 500
        for text in (junk, "<![if x]>", "<?php ?>", "<!doctype", "<" * 1000):
            with self.subTest(text=text[:20]):
                self.site.public["uk/index.html"] = page("uk", "index.html", body=text)
                report = self.run_check()
                self.assertIsInstance(report.errors, int)

    def test_non_html_files_are_ignored(self) -> None:
        self.site.public["uk/notes.txt"] = "not a page"
        self.site.public["assets/site.css"] = "body{}"
        self.assertClean(self.run_check())


class Source(Base):
    def test_invalid_json(self) -> None:
        self.site.i18n["uk"] = "{not json"
        self.assertRule(self.run_check(), "i18n-json", "i18n/uk.json: cannot read the file")

    def test_json_not_an_object(self) -> None:
        self.site.i18n["uk"] = "[1, 2]"
        self.assertRule(self.run_check(), "i18n-json", "JSON object of names and strings")

    def test_non_string_value(self) -> None:
        self.site.i18n["uk"] = json.dumps({**EN_STRINGS, "text": 5})
        self.assertRule(self.run_check(), "i18n-json", "names and strings")

    def test_missing_default_language_file(self) -> None:
        del self.site.i18n["en"]
        self.assertRule(self.run_check(), "i18n-json", "i18n/en.json: the default language file")

    def test_missing_and_unknown_keys(self) -> None:
        data = dict(EN_STRINGS)
        del data["text"]
        data["extra"] = "x"
        self.site.i18n["uk"] = data
        self.assertRule(
            self.run_check(), "i18n-keys", "missing keys ['text'], unknown keys ['extra']"
        )

    def test_unknown_keys_only(self) -> None:
        self.site.i18n["uk"] = {**EN_STRINGS, "extra": "x"}
        self.assertRule(self.run_check(), "i18n-keys", "missing keys [], unknown keys ['extra']")

    def test_missing_keys_only(self) -> None:
        data = dict(EN_STRINGS)
        del data["name"]
        self.site.i18n["uk"] = data
        self.assertRule(self.run_check(), "i18n-keys", "missing keys ['name'], unknown keys []")

    def test_empty_value(self) -> None:
        self.site.i18n["uk"] = {**EN_STRINGS, "page_desc": "  "}
        self.assertRule(self.run_check(), "i18n-empty", "uk.json: page_desc is empty")

    def test_empty_english_value(self) -> None:
        self.site.i18n["en"] = {**EN_STRINGS, "page_desc": ""}
        self.assertRule(self.run_check(), "i18n-empty", "en.json: page_desc is empty")

    def test_lost_placeholder(self) -> None:
        self.site.i18n["uk"] = {**EN_STRINGS, "text": "Hello"}
        self.assertRule(self.run_check(), "i18n-placeholders", "text does not keep")

    def test_extra_placeholder(self) -> None:
        self.site.i18n["uk"] = {**EN_STRINGS, "text": "Hello {{name}} {{name}}"}
        self.assertRule(self.run_check(), "i18n-placeholders", "text does not keep")

    def test_placeholder_order_may_change(self) -> None:
        self.site.i18n["en"] = {**EN_STRINGS, "text": "{{name}} then {{logo_alt}}"}
        self.site.i18n["uk"] = {**EN_STRINGS, "text": "{{logo_alt}} then {{name}}"}
        self.site.i18n["de"] = {**EN_STRINGS, "text": "{{logo_alt}} then {{name}}"}
        self.assertClean(self.run_check())

    def test_template_image_without_alt(self) -> None:
        self.site.pages["home.html"] = SRC_PAGE + '<img src="/b.svg">\n'
        self.assertRule(self.run_check(), "template-img-alt", "pages/home.html: <img /b.svg>")

    def test_layout_image_without_alt(self) -> None:
        self.site.layout = LAYOUT.replace('alt=""', "")
        self.assertRule(self.run_check(), "template-img-alt", "layout.html")

    def test_template_alt_empty_in_one_language(self) -> None:
        self.site.i18n["de"] = {**EN_STRINGS, "logo_alt": ""}
        report = self.run_check()
        self.assertRule(report, "template-alt-empty", "alt of <img /a.svg> is empty in de")

    def test_template_alt_with_literal_text(self) -> None:
        self.site.pages["home.html"] = SRC_PAGE.replace("{{logo_alt}}", "Logo")
        self.assertClean(self.run_check())

    def test_template_alt_with_computed_name_is_not_guessed(self) -> None:
        self.site.pages["home.html"] = SRC_PAGE.replace("{{logo_alt}}", "{{built_by_the_builder}}")
        self.assertClean(self.run_check())

    def test_template_external_script(self) -> None:
        self.site.pages["home.html"] = (
            SRC_PAGE + '<script src="https://cdn.example.net/x.js"></script>\n'
        )
        self.assertRule(self.run_check(), "template-script", "pages/home.html: <script>")

    def test_template_local_script(self) -> None:
        self.site.pages["home.html"] = SRC_PAGE + '<script src="/x.js"></script>\n'
        self.assertClean(self.run_check())

    def test_layout_without_lang_field(self) -> None:
        self.site.layout = LAYOUT.replace('lang="{{lang}}"', 'lang="en"')
        self.assertRule(self.run_check(), "template-lang", 'lang="{{lang}}"')

    def test_layout_without_lang_at_all(self) -> None:
        self.site.layout = LAYOUT.replace(' lang="{{lang}}"', "")
        self.assertRule(self.run_check(), "template-lang", "layout.html")

    def test_page_without_description_header(self) -> None:
        self.site.pages["home.html"] = SRC_PAGE.replace("<!-- description: {{page_desc}} -->\n", "")
        self.assertRule(self.run_check(), "template-description", "pages/home.html")

    def test_page_with_empty_description_header(self) -> None:
        self.site.pages["home.html"] = SRC_PAGE.replace("{{page_desc}}", "")
        self.assertRule(self.run_check(), "template-description", "pages/home.html")

    def test_title_header_is_optional(self) -> None:
        self.site.pages["home.html"] = SRC_PAGE.replace("<!-- title: {{page_title}} -->\n", "")
        self.assertClean(self.run_check())


class Legacy(Base):
    def test_legacy_problem_is_a_warning(self) -> None:
        for lang in ("en", "uk"):
            rel = "index.html" if lang == "en" else f"{lang}/index.html"
            self.site.public[rel] = page(lang, "index.html", title="Same")
        site = self.site.write()
        label = csp.shown(site / "public")
        with mock.patch.object(csp, "LEGACY", {(f"{label}/index.html", "title-same"): "brand"}):
            report, _ = csp.check_site(site)
        (problem,) = report.problems
        self.assertTrue(problem.warning)
        self.assertEqual(report.errors, 0)
        self.assertIn("warning: title is the same in en, uk (legacy: brand)", str(problem))

    def test_legacy_is_per_path_and_rule(self) -> None:
        for lang in ("en", "uk"):
            rel = "index.html" if lang == "en" else f"{lang}/index.html"
            self.site.public[rel] = page(lang, "index.html", title="Same")
        site = self.site.write()
        label = csp.shown(site / "public")
        other = {
            (f"{label}/index.html", "description-same"): "x",
            (f"{label}/x.html", "title-same"): "x",
        }
        with mock.patch.object(csp, "LEGACY", other):
            report, _ = csp.check_site(site)
        self.assertEqual(report.errors, 1)

    def test_repo_legacy_entries_are_all_title_same(self) -> None:
        self.assertTrue(csp.LEGACY)
        for (path, rule), why in csp.LEGACY.items():
            self.assertTrue(path.startswith("site/public/"), path)
            self.assertEqual(rule, "title-same")
            self.assertTrue(why)


class MainFunction(Base):
    def call(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = csp.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_help(self) -> None:
        code, out, _ = self.call("--help")
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)

    def test_wrong_calls_exit_2(self) -> None:
        for args in (("a", "b"), ("--nope",), ("-x", "site")):
            with self.subTest(args=args):
                code, _, err = self.call(*args)
                self.assertEqual(code, 2)
                self.assertIn("usage:", err)

    def test_not_a_site_folder_exit_2(self) -> None:
        code, _, err = self.call(str(self.tmp / "nothing"))
        self.assertEqual(code, 2)
        self.assertIn("no src/i18n and public folders", err)

    def test_public_only_folder_exit_2(self) -> None:
        (self.tmp / "half" / "public").mkdir(parents=True)
        self.assertEqual(self.call(str(self.tmp / "half"))[0], 2)

    def test_error_exit_1_and_message_shape(self) -> None:
        self.site.public["uk/index.html"] = page("uk", "index.html", html_lang="en")
        self.site.write()
        code, out, err = self.call(str(self.site.root))
        self.assertEqual(code, 1)
        self.assertIn("1 errors, 0 warnings", out)
        line = err.strip()
        self.assertTrue(line.endswith("<html lang> is 'en', expected 'uk'"), line)
        self.assertIn("/public/uk/index.html: ", line)
        self.assertNotIn("warning", line)

    def test_warnings_alone_exit_0(self) -> None:
        for lang in ("en", "uk"):
            rel = "index.html" if lang == "en" else f"{lang}/index.html"
            self.site.public[rel] = page(lang, "index.html", title="Same")
        site = self.site.write()
        label = csp.shown(site / "public")
        with mock.patch.object(csp, "LEGACY", {(f"{label}/index.html", "title-same"): "brand"}):
            code, out, err = self.call(str(site))
        self.assertEqual(code, 0)
        self.assertIn("0 errors, 1 warnings", out)
        self.assertIn(": warning: ", err)

    def test_output_is_deterministic(self) -> None:
        self.site.public["uk/index.html"] = page("uk", "index.html", html_lang="en", title="")
        del self.site.public["de/about/index.html"]
        self.site.write()
        runs = {self.call(str(self.site.root)) for _ in range(3)}
        self.assertEqual(len(runs), 1)

    def test_nothing_is_written(self) -> None:
        site = self.site.write()
        before = sorted(
            (p.relative_to(site).as_posix(), p.stat().st_mtime_ns) for p in site.rglob("*")
        )
        self.call(str(site))
        after = sorted(
            (p.relative_to(site).as_posix(), p.stat().st_mtime_ns) for p in site.rglob("*")
        )
        self.assertEqual(before, after)

    def test_module_runs_no_subprocess(self) -> None:
        source = Path(csp.__file__).read_text(encoding="utf-8")
        for banned in ("import subprocess", "from subprocess", "import socket", "urllib.request"):
            self.assertNotIn(banned, source)
        self.assertNotIn("os.system", source)
        self.assertNotIn(chr(72) + "erkos", source)


class RealSite(unittest.TestCase):
    def test_repo_site_has_no_errors(self) -> None:
        report, pages = csp.check_site(ROOT / "site")
        self.assertGreater(pages, 0)
        self.assertEqual([str(p) for p in report.problems if not p.warning], [])

    def test_repo_languages_come_from_i18n(self) -> None:
        languages = csp.language_files(ROOT / "site" / "src" / "i18n")
        self.assertEqual(languages[0], "en")
        folders = [p.name for p in (ROOT / "site" / "public").iterdir() if p.is_dir()]
        self.assertEqual(sorted(languages), sorted(["en", *(f for f in folders if len(f) == 2)]))

    def test_repo_main_exit_code(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(csp.main([]), 0)

    def test_cwd_does_not_matter(self) -> None:
        old = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(csp.main([]), 0)
            finally:
                os.chdir(old)


if __name__ == "__main__":
    unittest.main()
