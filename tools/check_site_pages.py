#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the language versions of the vilnaos.com pages (site/src and site/public).

    python3 tools/check_site_pages.py [SITE_DIR]

``SITE_DIR`` is the ``site`` folder (it holds ``src`` and ``public``); the default
is the ``site`` folder of this repo. This checker looks at what
``tools/check_site.py`` does not: how the translations of one page relate to each
other and what search engines and screen readers read from every page.
``check_site.py`` keeps checking links, inline styles, security.txt and that
``site/public`` is what ``tools/build_site.py`` generates; the few rules that
overlap with it (the page set, ``<html lang>`` and "no external script") are
repeated here on purpose, so this tool also works on a folder that is not the
repo's own.

The languages are the files ``site/src/i18n/<lang>.json``. English is the
default and lives at the root of ``site/public``; every other language lives
under ``/<lang>/``.

site/public (the rendered pages):

* ``page-missing``, ``page-extra``: every language has the same pages as the
  others, and every two-letter folder with pages is a language of i18n (English
  is the root, so an ``en/`` folder is stray too);
* ``html-lang``: ``<html lang>`` is present and equals the language of the folder;
* ``title-empty``, ``title-same``: ``<title>`` is present, once, not empty, and is
  not the same in two languages of one page (case and spacing do not count);
* ``description-empty``, ``description-same``: the same for
  ``<meta name="description">``;
* ``hreflang-*``: a page (except 404.html) lists ``<link rel="alternate"
  hreflang>`` for every language and for ``x-default``, each one a full URL on
  one host that points at a real page of that language; the page lists itself;
  every linked page links back; ``x-default`` is the same in the whole group;
* ``img-alt``, ``img-alt-blank``: every ``<img>`` has an ``alt`` attribute (empty
  ``alt=""`` is allowed: it marks a decorative image), never only spaces;
* ``external-script``: no ``<script src>`` or ``modulepreload`` from another host
  (a scheme, ``//host``, ``\\host``, ``data:``), and no ``<base href>`` to
  another host, which would turn relative script paths into external ones;
* ``unreadable``: a page that is a symlink, is not UTF-8, or is over 2 MiB.

site/src (the sources):

* ``i18n-json``, ``i18n-keys``, ``i18n-empty``, ``i18n-placeholders``: every
  language file is a JSON object of strings with the same keys as ``en.json``,
  no value is empty, and a translation keeps the ``{{name}}`` fields of the
  English text;
* ``template-img-alt``, ``template-alt-empty``: every ``<img>`` in the layout and
  pages has ``alt``; an ``alt="{{name}}"`` is not empty in any language;
* ``template-script``: no external ``<script src>`` in a template;
* ``template-lang``: the layout has ``<html lang="{{lang}}">``;
* ``template-description``: a page declares ``<!-- description: ... -->``.

A problem that the site already has today is a warning, listed in ``LEGACY`` with
the reason (fix the cause, then delete the line). Anything new is an error.

Stdlib only; no network; nothing is written. Pages are read from disk (symlinks
are never followed); no subprocess is run, so it also works on a folder outside git.

Exit code: 0 no errors (warnings allowed), 1 errors found, 2 wrong call.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

DEFAULT = "en"
MAX_PAGE_BYTES = 2 * 1024 * 1024
LANG_DIR = re.compile(r"[a-z]{2}")
LANG_CODE = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
FIELD = re.compile(r"\{\{(\w+)\}\}")
WHOLE_FIELD = re.compile(r"\{\{(\w+)\}\}")
HEADER = re.compile(r"<!--\s*(\w+):\s*(.*?)\s*-->", re.DOTALL)
URL_NOISE = re.compile(r"[\t\r\n]")
X_DEFAULT = "x-default"

# Problems the site has today, as (path, rule) -> why. They are warnings.
# Fix the cause, then delete the line.
LEGACY: dict[tuple[str, str], str] = {
    (
        "site/public/index.html",
        "title-same",
    ): "home page title is the bare brand name in every language",
    (
        "site/public/blavet/index.html",
        "title-same",
    ): "the title is the release name, written the same in every language",
    (
        "site/public/feedback/index.html",
        "title-same",
    ): "the word is the same in English and German",
    (
        "site/public/privacy/index.html",
        "title-same",
    ): "the word is the same in English and Italian",
}


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False
    rule: str = ""

    def __str__(self) -> str:
        return f"{self.path}: {'warning: ' if self.warning else ''}{self.message}"


@dataclass
class Report:
    problems: list[Problem] = field(default_factory=list)

    def add(self, path: str, message: str, rule: str) -> None:
        why = LEGACY.get((path, rule))
        if why is not None:
            self.problems.append(Problem(path, f"{message} (legacy: {why})", True, rule))
        else:
            self.problems.append(Problem(path, message, False, rule))

    def sorted_problems(self) -> list[Problem]:
        return sorted(self.problems, key=lambda p: (p.path, p.rule, p.message))

    @property
    def errors(self) -> int:
        return sum(not p.warning for p in self.problems)

    @property
    def warnings(self) -> int:
        return sum(p.warning for p in self.problems)


# --- small helpers ------------------------------------------------------------


def norm(text: str) -> str:
    """Text for comparing: Unicode NFC, case folded, one space between words."""
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def clean_url(url: str) -> str:
    """A URL as a browser reads it: no tabs or line breaks, no edge spaces, \\ is /."""
    url = URL_NOISE.sub("", url).strip("".join(chr(c) for c in range(0x21)))
    return url.replace("\\", "/")


def is_external(url: str) -> bool:
    parts = urlsplit(clean_url(url))
    return bool(parts.scheme or parts.netloc)


def page_url(rel: str) -> str:
    """Public path of a page file: index.html is served as its folder."""
    path = "/" + rel
    return path.removesuffix("index.html")


def shown(base: Path) -> str:
    """How a folder is named in messages: relative to the repo when it is inside."""
    try:
        return base.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return base.as_posix()


# --- one rendered page --------------------------------------------------------


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_lang: str | None = None
        self.seen_html = False
        self.titles: list[str] = []
        self.descriptions: list[str] = []
        self.alternates: list[tuple[str, str]] = []
        self.images: list[dict[str, str | None]] = []
        self.external: list[str] = []
        self._svg = 0
        self._title: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v for k, v in attrs}
        if tag == "svg":
            self._svg += 1
        elif tag == "html" and not self.seen_html:
            self.seen_html = True
            self.html_lang = a.get("lang")
        elif tag == "title" and not self._svg:
            self._title = []
        elif tag == "meta" and (a.get("name") or "").strip().casefold() == "description":
            self.descriptions.append(a.get("content") or "")
        elif tag == "img":
            self.images.append(a)
        elif tag == "script" and (a.get("src") or "").strip() and is_external(a["src"] or ""):
            self.external.append(f"<script> loads {_short(a['src'] or '')}")
        elif tag == "link":
            rel = (a.get("rel") or "").casefold().split()
            href = a.get("href") or ""
            if "alternate" in rel and a.get("hreflang") is not None:
                self.alternates.append(((a["hreflang"] or "").strip().casefold(), href.strip()))
            if "modulepreload" in rel and is_external(href):
                self.external.append(f"modulepreload of {_short(href)}")
        elif tag == "base" and (a.get("href") or "").strip() and is_external(a["href"] or ""):
            self.external.append(f"<base href> points to {_short(a['href'] or '')}")

    def handle_endtag(self, tag: str) -> None:
        if tag == "svg" and self._svg:
            self._svg -= 1
        elif tag == "title" and self._title is not None:
            self.titles.append("".join(self._title))
            self._title = None

    def handle_data(self, data: str) -> None:
        if self._title is not None:
            self._title.append(data)

    def close(self) -> None:
        super().close()
        if self._title is not None:  # <title> never closed: what it read is the title
            self.titles.append("".join(self._title))
            self._title = None


def _short(url: str) -> str:
    url = " ".join(url.split())
    return url if len(url) <= 60 else url[:57] + "..."


@dataclass
class Page:
    rel: str  # path under public, with /
    lang: str
    base: str  # path under the language root
    parsed: PageParser
    stray: bool = False  # in a folder that is not the root or a site language

    @property
    def url(self) -> str:
        return page_url(self.rel)


def list_pages(public: Path, label: str, report: Report) -> list[tuple[str, Path]]:
    """Every *.html under public (sorted), never following a symlink."""
    found: list[tuple[str, Path]] = []
    for folder, dirs, files in os.walk(public, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            if not name.lower().endswith(".html"):
                continue
            path = Path(folder) / name
            rel = path.relative_to(public).as_posix()
            if path.is_symlink():
                report.add(f"{label}/{rel}", "page is a symlink, not read", "unreadable")
            else:
                found.append((rel, path))
        # os.walk lists a symlink to a folder in dirs but does not enter it.
        for name in dirs:
            if (Path(folder) / name).is_symlink():
                rel = (Path(folder) / name).relative_to(public).as_posix()
                report.add(f"{label}/{rel}", "folder is a symlink, not read", "unreadable")
    return found


def read_page(path: Path) -> tuple[PageParser | None, str]:
    """The parsed page, or None and the reason."""
    try:
        if path.stat().st_size > MAX_PAGE_BYTES:
            return (
                None,
                f"page is larger than {MAX_PAGE_BYTES // 1024 // 1024} MiB, not checked",
            )
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return None, "page is not valid UTF-8"
    except OSError as err:
        return None, f"cannot read the page: {err.strerror or err}"
    parser = PageParser()
    try:
        parser.feed(text)
        parser.close()
    except (AssertionError, ValueError, RecursionError) as err:
        return None, f"cannot parse the page: {type(err).__name__}"
    return parser, ""


def split_language(rel: str, languages: list[str]) -> tuple[str, str, bool]:
    """(language, path under its root, stray) of a page path under public."""
    parts = rel.split("/")
    if len(parts) > 1 and LANG_DIR.fullmatch(parts[0]):
        first = parts[0]
        return first, "/".join(parts[1:]), first == DEFAULT or first not in languages
    return DEFAULT, rel, False


# --- checks of site/public ----------------------------------------------------


def check_public(public: Path, languages: list[str], report: Report | None = None) -> Report:
    report = report if report is not None else Report()
    label = shown(public)
    files = list_pages(public, label, report)
    if not files and not report.problems:
        report.add(label, "no pages", "page-missing")
        return report
    pages: dict[str, Page] = {}
    for rel, path in files:
        lang, base, stray = split_language(rel, languages)
        parsed, why = read_page(path)
        if parsed is None:
            report.add(f"{label}/{rel}", why, "unreadable")
            continue
        pages[rel] = Page(rel, lang, base, parsed, stray)
    check_page_sets(label, pages, languages, report)
    for page in pages.values():
        check_html_lang(label, page, report)
        check_images(label, page, report)
        check_external(label, page, report)
    check_titles(label, pages, languages, report)
    check_hreflang(label, pages, languages, report)
    return report


def check_page_sets(
    label: str, pages: dict[str, Page], languages: list[str], report: Report
) -> None:
    for folder in sorted({p.rel.split("/")[0] for p in pages.values() if p.stray}):
        why = (
            "English pages live at the root, not here"
            if folder == DEFAULT
            else f"folder looks like a language but there is no i18n/{folder}.json"
        )
        report.add(f"{label}/{folder}/", why, "page-extra")
    by_lang: dict[str, dict[str, str]] = {lang: {} for lang in languages}
    for page in pages.values():
        if not page.stray and page.lang in by_lang:
            by_lang[page.lang][page.base] = page.rel
    all_bases = sorted({base for pages_of in by_lang.values() for base in pages_of})
    for lang in languages:
        root = "" if lang == DEFAULT else f"{lang}/"
        if not by_lang[lang]:
            report.add(f"{label}/{root}", f"no pages for language {lang}", "page-missing")
            continue
        for base in all_bases:
            if base not in by_lang[lang]:
                owners = [other for other in languages if base in by_lang[other]]
                report.add(
                    f"{label}/{root}{base}",
                    f"page is missing (it exists in {', '.join(owners)})",
                    "page-missing",
                )


def check_html_lang(label: str, page: Page, report: Report) -> None:
    path = f"{label}/{page.rel}"
    have = page.parsed.html_lang
    if have is None or not have.strip():
        report.add(path, "<html> has no lang", "html-lang")
    elif have.strip().casefold() != page.lang.casefold():
        report.add(path, f"<html lang> is {have!r}, expected {page.lang!r}", "html-lang")


def check_images(label: str, page: Page, report: Report) -> None:
    for img in page.parsed.images:
        src = _short(img.get("src") or "") or "(no src)"
        if "alt" not in img:
            report.add(f"{label}/{page.rel}", f"<img {src}> has no alt", "img-alt")
        elif (img["alt"] or "") and not (img["alt"] or "").strip():
            report.add(
                f"{label}/{page.rel}",
                f'<img {src}> has an alt of only spaces; use alt="" for a decorative image',
                "img-alt-blank",
            )


def check_external(label: str, page: Page, report: Report) -> None:
    for what in page.parsed.external:
        report.add(
            f"{label}/{page.rel}",
            f"{what}: pages must not load code from another host",
            "external-script",
        )


def group_by_base(pages: dict[str, Page], languages: list[str]) -> dict[str, dict[str, Page]]:
    groups: dict[str, dict[str, Page]] = {}
    for page in pages.values():
        if page.lang in languages and not page.stray:
            groups.setdefault(page.base, {})[page.lang] = page
    return groups


def check_titles(label: str, pages: dict[str, Page], languages: list[str], report: Report) -> None:
    for _base, group in sorted(group_by_base(pages, languages).items()):
        for kind, rule, get in (
            ("title", "title", lambda p: p.parsed.titles),
            ("meta description", "description", lambda p: p.parsed.descriptions),
        ):
            seen: dict[str, list[str]] = {}
            for lang in languages:
                page = group.get(lang)
                if page is None:
                    continue
                values = get(page)
                path = f"{label}/{page.rel}"
                if not values:
                    report.add(path, f"no {kind}", f"{rule}-empty")
                    continue
                if len(values) > 1:
                    report.add(
                        path,
                        f"{len(values)} {kind} elements, expected one",
                        f"{rule}-empty",
                    )
                if not norm(values[0]):
                    report.add(path, f"{kind} is empty", f"{rule}-empty")
                    continue
                seen.setdefault(norm(values[0]), []).append(lang)
            for langs in seen.values():
                if len(langs) > 1:
                    first = group[langs[0]]
                    report.add(
                        f"{label}/{first.rel}",
                        f"{kind} is the same in {', '.join(langs)}",
                        f"{rule}-same",
                    )


def hreflang_target(
    href: str, by_url: dict[str, Page]
) -> tuple[Page | None, str | None, str | None]:
    """(page, origin, problem) of an hreflang URL."""
    parts = urlsplit(clean_url(href))
    problem = None
    origin = None
    if parts.scheme in ("http", "https") and parts.netloc:
        origin = f"{parts.scheme}://{parts.netloc}".casefold()
    else:
        problem = "is not a full URL (scheme and host)"
    if parts.query or parts.fragment:
        problem = problem or "has a query or a fragment"
    return by_url.get(unquote(parts.path)), origin, problem


def check_hreflang(
    label: str, pages: dict[str, Page], languages: list[str], report: Report
) -> None:
    by_url: dict[str, Page] = {}
    for page in pages.values():
        by_url[page.url] = page
        by_url.setdefault("/" + page.rel, page)
    links: dict[str, dict[str, Page | None]] = {}
    origins: Counter[str] = Counter()
    for page in pages.values():
        for _, href in page.parsed.alternates:
            parts = urlsplit(clean_url(href))
            if parts.scheme in ("http", "https") and parts.netloc:
                origins[f"{parts.scheme}://{parts.netloc}".casefold()] += 1
    main_origin = min(origins, key=lambda o: (-origins[o], o)) if origins else None
    for rel, page in sorted(pages.items()):
        path = f"{label}/{rel}"
        alternates = page.parsed.alternates
        if not alternates:
            if rel.rsplit("/", 1)[-1] != "404.html":
                report.add(path, "no hreflang links", "hreflang-missing")
            continue
        found: dict[str, Page | None] = {}
        for code, href in alternates:
            if code in found:
                report.add(path, f"hreflang {code} is listed twice", "hreflang-duplicate")
                continue
            target, origin, problem = hreflang_target(href, by_url)
            if problem:
                report.add(path, f"hreflang {code}: {_short(href)} {problem}", "hreflang-url")
            if origin and main_origin and origin != main_origin:
                report.add(
                    path,
                    f"hreflang {code}: {_short(href)} is on another host than the rest of the site",
                    "hreflang-url",
                )
            if target is None:
                report.add(
                    path,
                    f"hreflang {code}: {_short(href)} is not a page of this site",
                    "hreflang-target",
                )
            found[code] = target
        links[rel] = found
        wanted = set(languages) | {X_DEFAULT}
        for code in sorted(wanted - found.keys()):
            report.add(path, f"hreflang {code} is missing", "hreflang-set")
        for code in sorted(found.keys() - wanted):
            report.add(path, f"hreflang {code} is not a site language", "hreflang-set")
        for code, target in found.items():
            if target is not None and code in languages and target.lang != code:
                report.add(
                    path,
                    f"hreflang {code} points at {target.rel}, a page in {target.lang}",
                    "hreflang-target",
                )
        own = found.get(page.lang)
        if page.lang in found and own is not None and own is not page:
            report.add(
                path,
                f"hreflang {page.lang} does not point at the page itself",
                "hreflang-self",
            )
    for rel, found in sorted(links.items()):
        page = pages[rel]
        path = f"{label}/{rel}"
        for code, target in sorted(found.items(), key=lambda kv: kv[0]):
            if target is None or target is page or code == X_DEFAULT:
                continue
            back = links.get(target.rel)
            if back is None:
                continue
            if back.get(page.lang) is not page:
                report.add(
                    path,
                    f"hreflang {code} links to {target.rel}, "
                    f"which does not link back with hreflang {page.lang}",
                    "hreflang-reciprocal",
                )
        mine = found.get(X_DEFAULT)
        for code, target in sorted(found.items(), key=lambda kv: kv[0]):
            if target is None or code == X_DEFAULT or target is page:
                continue
            theirs = links.get(target.rel, {}).get(X_DEFAULT)
            if mine is not None and theirs is not None and theirs is not mine:
                report.add(
                    path,
                    f"x-default differs from the x-default of {target.rel}",
                    "hreflang-reciprocal",
                )


# --- checks of site/src -------------------------------------------------------


class TemplateParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[dict[str, str | None]] = []
        self.scripts: list[str] = []
        self.html_langs: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v for k, v in attrs}
        if tag == "img":
            self.images.append(a)
        elif tag == "script" and (a.get("src") or "").strip():
            self.scripts.append(a["src"] or "")
        elif tag == "html":
            self.html_langs.append(a.get("lang"))


def language_files(i18n: Path) -> list[str]:
    names = sorted(p.stem for p in i18n.glob("*.json") if p.is_file() or p.is_symlink())
    return ([DEFAULT] if DEFAULT in names else []) + [n for n in names if n != DEFAULT]


def load_strings(
    i18n: Path, languages: list[str], label: str, report: Report
) -> dict[str, dict[str, str]]:
    strings: dict[str, dict[str, str]] = {}
    for lang in languages:
        path = i18n / f"{lang}.json"
        shown_path = f"{label}/i18n/{lang}.json"
        if not LANG_CODE.fullmatch(lang):
            report.add(shown_path, "file name is not a language code", "i18n-json")
        if path.is_symlink():
            report.add(shown_path, "file is a symlink, not read", "i18n-json")
            continue
        try:
            data = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, ValueError, RecursionError) as err:
            report.add(shown_path, f"cannot read the file: {type(err).__name__}", "i18n-json")
            continue
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            report.add(shown_path, "must be a JSON object of names and strings", "i18n-json")
            continue
        strings[lang] = data
    return strings


def check_src(src: Path, report: Report | None = None) -> tuple[Report, list[str]]:
    """Check site/src; return the report and the language list."""
    report = report if report is not None else Report()
    label = shown(src)
    i18n = src / "i18n"
    languages = language_files(i18n) if i18n.is_dir() else []
    if DEFAULT not in languages:
        report.add(
            f"{label}/i18n/{DEFAULT}.json",
            "the default language file is missing",
            "i18n-json",
        )
    strings = load_strings(i18n, languages, label, report)
    check_i18n(label, strings, report)
    check_templates(src, label, strings, report)
    return report, languages


def check_i18n(label: str, strings: dict[str, dict[str, str]], report: Report) -> None:
    reference = strings.get(DEFAULT)
    for lang, data in strings.items():
        path = f"{label}/i18n/{lang}.json"
        if reference is not None and lang != DEFAULT:
            missing = sorted(reference.keys() - data.keys())
            extra = sorted(data.keys() - reference.keys())
            if missing or extra:
                report.add(path, f"missing keys {missing}, unknown keys {extra}", "i18n-keys")
        for key in sorted(data):
            value = data[key]
            if not value.strip():
                report.add(path, f"{key} is empty", "i18n-empty")
            elif reference is not None and key in reference and lang != DEFAULT:
                want = Counter(FIELD.findall(reference[key]))
                if Counter(FIELD.findall(value)) != want:
                    report.add(
                        path,
                        f"{key} does not keep the {{{{name}}}} fields of the English text",
                        "i18n-placeholders",
                    )


def check_templates(
    src: Path, label: str, strings: dict[str, dict[str, str]], report: Report
) -> None:
    files: list[tuple[str, Path]] = []
    layout = src / "layout.html"
    if layout.is_file() and not layout.is_symlink():
        files.append(("layout.html", layout))
    pages = src / "pages"
    if pages.is_dir():
        files += [
            (f"pages/{p.name}", p)
            for p in sorted(pages.glob("*.html"))
            if p.is_file() and not p.is_symlink()
        ]
    for rel, path in files:
        shown_path = f"{label}/{rel}"
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            report.add(shown_path, "cannot read the template as UTF-8", "template-img-alt")
            continue
        parser = TemplateParser()
        try:
            parser.feed(text)
            parser.close()
        except (AssertionError, ValueError, RecursionError):
            report.add(shown_path, "cannot parse the template", "template-img-alt")
            continue
        for img in parser.images:
            name = _short(img.get("src") or "") or "(no src)"
            if "alt" not in img:
                report.add(shown_path, f"<img {name}> has no alt", "template-img-alt")
                continue
            whole = WHOLE_FIELD.fullmatch((img["alt"] or "").strip())
            if whole:
                empty = [
                    lang
                    for lang, data in strings.items()
                    if whole.group(1) in data and not data[whole.group(1)].strip()
                ]
                if empty:
                    report.add(
                        shown_path,
                        f"alt of <img {name}> is empty in {', '.join(empty)}",
                        "template-alt-empty",
                    )
        for url in parser.scripts:
            if is_external(url):
                report.add(shown_path, f"<script> loads {_short(url)}", "template-script")
        if rel == "layout.html":
            if parser.html_langs[:1] != ["{{lang}}"]:
                report.add(shown_path, '<html> must have lang="{{lang}}"', "template-lang")
        else:
            headers = dict(HEADER.findall(text))
            if not headers.get("description", "").strip():
                report.add(
                    shown_path,
                    "no <!-- description: ... --> header",
                    "template-description",
                )


# --- command line -------------------------------------------------------------

USAGE = "usage: python3 tools/check_site_pages.py [SITE_DIR]"


def check_site(site: Path) -> tuple[Report, int]:
    """Check src and public of a site folder; return the report and the page count."""
    report, languages = check_src(site / "src")
    check_public(site / "public", languages, report)
    pages = sum(1 for _ in (site / "public").rglob("*.html")) if (site / "public").is_dir() else 0
    return report, pages


def main(argv: list[str] | None = None) -> int:
    try:
        args = sys.argv[1:] if argv is None else argv
        if args in (["-h"], ["--help"]):
            print(USAGE)
            return 0
        if len(args) > 1 or any(a.startswith("-") for a in args):
            print(USAGE, file=sys.stderr)
            return 2
        site = Path(args[0]) if args else ROOT / "site"
        if not (site / "src" / "i18n").is_dir() or not (site / "public").is_dir():
            print(f"{site}: no src/i18n and public folders\n{USAGE}", file=sys.stderr)
            return 2
        report, pages = check_site(site)
        for problem in report.sorted_problems():
            print(problem, file=sys.stderr)
        print(f"{pages} pages checked: {report.errors} errors, {report.warnings} warnings")
        return 1 if report.errors else 0
    except (OSError, ValueError) as err:  # no traceback for a bad name, argument or bytes
        print(f"{os.path.basename(__file__)}: cannot check: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # A file name that is not UTF-8 is printed escaped, not as a traceback.
    sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    sys.exit(main())
