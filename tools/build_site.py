#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the vilnaos.com pages in every site language.

    python3 tools/build_site.py           # write site/public
    python3 tools/build_site.py --check   # fail if site/public is out of date

Sources live in site/src: pages/*.html are the page bodies, layout.html wraps
them, and i18n/<lang>.json holds the text for each language (ADR 0009).
English is the default and is served from the root; every other language
lives under /<lang>/. The output is plain HTML committed to site/public, so
the host publishes it as it is, with no build step there.

`{{name}}` in a page, the layout or a string is replaced by a string from the
language's JSON or by a value the builder computes (URLs, the language menu).
An unknown name is an error, so a typo cannot reach the site.

Stdlib only, so it runs anywhere CI does.
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "site" / "src"
PUBLIC = ROOT / "site" / "public"
BASE_URL = "https://vilnaos.com"
DEFAULT = "en"

# ADR 0009, in the order the language menu shows them.
LANGUAGES = {
    "en": "English",
    "uk": "Українська",
    "es": "Español",
    "pl": "Polski",
    "de": "Deutsch",
    "fr": "Français",
    "it": "Italiano",
    "ro": "Română",
    "cs": "Čeština",
    "lt": "Lietuvių",
}

# Guides that exist in docs/ in more than one language; the rest get English.
GUIDE_LANGS = {"uk", "es"}
REPO = "https://github.com/StDenis1980/VilnaOS"

# page id -> (source body, path under the language root, extra <head> lines)
NOINDEX = '<meta name="robots" content="noindex">\n'
PAGES = {
    "home": ("home.html", "index.html", ""),
    "gloxi": ("gloxi.html", "gloxi/index.html", NOINDEX),
    "privacy": ("privacy.html", "privacy/index.html", NOINDEX),
    "404": ("404.html", "404.html", NOINDEX),
}

FIELD = re.compile(r"\{\{(\w+)\}\}")
MAX_PASSES = 4  # strings may use computed values, which never nest deeper


class BuildError(Exception):
    pass


def lang_root(lang: str) -> str:
    return "/" if lang == DEFAULT else f"/{lang}/"


def page_url(lang: str, out: str) -> str:
    """Public path of a page: index.html files are served as their folder."""
    path = lang_root(lang) + out
    return path.removesuffix("index.html")


def load_strings() -> dict[str, dict[str, str]]:
    strings: dict[str, dict[str, str]] = {}
    for lang in LANGUAGES:
        path = SRC / "i18n" / f"{lang}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            raise BuildError(f"{path.relative_to(ROOT)}: {err}") from err
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            raise BuildError(f"{path.relative_to(ROOT)}: must map names to strings")
        strings[lang] = data
    want = strings[DEFAULT].keys()
    for lang, data in strings.items():
        missing = sorted(want - data.keys())
        extra = sorted(data.keys() - want)
        if missing or extra:
            raise BuildError(f"i18n/{lang}.json: missing {missing}, unknown {extra}")
    return strings


def language_menu(current: str, page: str, out: str, label: str) -> str:
    items = []
    for lang, name in LANGUAGES.items():
        mark = ' aria-current="true"' if lang == current else ""
        href = lang_root(lang) if page == "404" else page_url(lang, out)
        items.append(
            f'      <li><a href="{href}" hreflang="{lang}" lang="{lang}"{mark}>{name}</a></li>'
        )
    return (
        f'<details class="langs">\n'
        f'    <summary aria-label="{html.escape(label)}: {LANGUAGES[current]}">'
        f"{LANGUAGES[current]}</summary>\n"
        f'    <ul>\n' + "\n".join(items) + "\n    </ul>\n  </details>"
    )


def head_links(page: str, out: str, lang: str) -> str:
    if page == "404":
        return ""
    lines = [f'<link rel="canonical" href="{BASE_URL}{page_url(lang, out)}">']
    for other in LANGUAGES:
        lines.append(
            f'<link rel="alternate" hreflang="{other}" href="{BASE_URL}{page_url(other, out)}">'
        )
    lines.append(
        f'<link rel="alternate" hreflang="x-default" href="{BASE_URL}{page_url(DEFAULT, out)}">'
    )
    return "\n".join(lines) + "\n"


def fill(text: str, values: dict[str, str], where: str) -> str:
    for _ in range(MAX_PASSES):
        if not FIELD.search(text):
            return text

        def one(m: re.Match[str]) -> str:
            if m.group(1) not in values:
                raise BuildError(f"{where}: unknown {{{{{m.group(1)}}}}}")
            return values[m.group(1)]

        text = FIELD.sub(one, text)
    raise BuildError(f"{where}: {{{{…}}}} nested too deep")


def render(lang: str, strings: dict[str, str]) -> dict[str, str]:
    layout = (SRC / "layout.html").read_text(encoding="utf-8")
    out_files: dict[str, str] = {}
    for page, (source, out, head) in PAGES.items():
        body = (SRC / "pages" / source).read_text(encoding="utf-8")
        values = dict(strings)
        values.update(
            lang=lang,
            home_url=lang_root(lang),
            gloxi_url=page_url(lang, PAGES["gloxi"][1]),
            privacy_url=page_url(lang, PAGES["privacy"][1]),
            guide_url=f"{REPO}/blob/main/docs/getting-started"
            + (f".{lang}.md" if lang in GUIDE_LANGS else ".md"),
            repo_url=REPO,
            head_extra=head + head_links(page, out, lang),
            languages=language_menu(lang, page, out, strings["language"]),
            icon="/assets/gloxi.svg" if page == "gloxi" else "/assets/vilna-icon.svg",
            privacy_current=' aria-current="page"' if page == "privacy" else "",
            gloxi_current=' aria-current="page"' if page == "gloxi" else "",
        )
        # The page body may set the title, footer and navigation of the layout.
        parts = dict(re.findall(r"<!--\s*(\w+):\s*(.*?)\s*-->", body))
        values["title"] = parts.get("title", "Vilna OS")
        values["description"] = parts.get("description", "")
        values["nav"] = parts.get("nav", "")
        values["footer"] = parts.get("footer", "{{footer_base}}")
        values["main"] = re.sub(r"<!--\s*\w+:.*?-->\n?", "", body).rstrip("\n")
        where = f"{lang}/{source}"
        text = fill(layout, values, where)
        # Drop the indented blank line an empty {{nav}} leaves behind.
        text = re.sub(r"\n[ \t]+(?=\n)", "", text)
        out_files[lang_root(lang).lstrip("/") + out] = text
    return out_files


def build() -> dict[str, str]:
    strings = load_strings()
    files: dict[str, str] = {}
    for lang in LANGUAGES:
        files.update(render(lang, strings[lang]))
    files["_redirects"] = redirects()
    return files


def redirects() -> str:
    lines = [
        "# Generated by tools/build_site.py; edit that, not this file.",
        "# English moved from /en/ to the root on 2026-11-03.",
        "/en/*  /:splat  301",
    ]
    for lang in LANGUAGES:
        if lang != DEFAULT:
            lines.append(f"/{lang}/*  /{lang}/404.html  404")
    lines.append("/*  /404.html  404")
    return "\n".join(lines) + "\n"


def generated_paths(public: Path) -> set[str]:
    """Files under public/ the builder owns: every page and _redirects."""
    owned = {"_redirects"}
    for path in public.rglob("*.html"):
        owned.add(path.relative_to(public).as_posix())
    return owned


def main(argv: list[str]) -> int:
    check = "--check" in argv[1:]
    try:
        files = build()
    except BuildError as err:
        print(f"build_site: {err}", file=sys.stderr)
        return 1
    stale = []
    for rel in sorted(generated_paths(PUBLIC) | files.keys()):
        path = PUBLIC / rel
        want = files.get(rel)
        have = path.read_text(encoding="utf-8") if path.is_file() else None
        if want == have:
            continue
        stale.append(rel)
        if check:
            continue
        if want is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(want, encoding="utf-8")
    if not check:
        for folder in sorted(PUBLIC.rglob("*"), reverse=True):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
    if check and stale:
        for rel in stale:
            print(f"site/public/{rel} is out of date", file=sys.stderr)
        print("run: python3 tools/build_site.py", file=sys.stderr)
        return 1
    print(f"site built: {len(files) - 1} pages in {len(LANGUAGES)} languages"
          + (f", {len(stale)} changed" if stale and not check else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
