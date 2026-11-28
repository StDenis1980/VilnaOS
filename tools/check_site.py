#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the static site in site/public before it is published.

    python3 tools/check_site.py [site/public]

- No page loads anything from another host: no <script> at all, and every
  src, stylesheet, icon or preload is a local path. Links (<a href>) to
  other sites are fine: following one is the visitor's choice.
- No inline style or event-handler attributes, so a strict
  Content-Security-Policy (site/README.md) holds.
- Every local link and resource exists.
- Every page exists in all site languages (English at the root, the others
  under /<lang>/), each with the right lang, and site/public matches what
  tools/build_site.py generates from site/src.
- security.txt has a Contact and an Expires date in the future.

Stdlib only, so it runs anywhere CI does.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import build_site

ROOT = Path(__file__).resolve().parent.parent
RESOURCE_ATTRS = {"src", "srcset", "poster", "data"}


# rel values of a <link> the browser does not load: it only names another address.
NAVIGATION_RELS = {"alternate", "canonical"}
# rel values that make the browser fetch the address (even next to "alternate").
LOADING_RELS = {
    "stylesheet",
    "icon",
    "preload",
    "prefetch",
    "modulepreload",
    "manifest",
    "apple-touch-icon",
    "mask-icon",
    "preconnect",
    "dns-prefetch",
}
# The longest page read; a bigger file is a problem, not something to parse.
MAX_PAGE_BYTES = 8 * 1024 * 1024


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []  # <a href>: may point anywhere
        self.resources: list[str] = []  # loaded by the browser: must be local
        self.problems: list[str] = []
        self.lang: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "script":
            self.problems.append("<script> is not allowed")
        if tag == "iframe":
            self.problems.append("<iframe> is not allowed")
        if tag == "base":
            # Every relative link of the page would then go to that address.
            self.problems.append("<base> is not allowed")
        if tag == "style":
            # Inline CSS, @import included: the Content-Security-Policy refuses it.
            self.problems.append("<style> element is not allowed")
        for name in a:
            if name.startswith("on"):
                self.problems.append(f"event handler attribute {name}=")
        if "style" in a:
            self.problems.append("inline style= attribute")
        for name in RESOURCE_ATTRS & a.keys():
            for part in a[name].split(","):
                url = part.strip().split(" ")[0]
                if url:
                    self.resources.append(url)
        for name in ("href", "xlink:href"):
            if name not in a:
                continue
            rels = set(a.get("rel", "").lower().split())
            if tag == "a" or (
                tag == "link" and rels & NAVIGATION_RELS and not rels & LOADING_RELS
            ):
                self.links.append(a[name])
            else:
                # <link rel=stylesheet>, <use href>, <image href>: the browser loads it.
                self.resources.append(a[name])


def read_page(page: Path) -> tuple[str | None, str]:
    """The text of a page, or ``None`` and why it cannot be read."""
    try:
        with page.open("rb") as f:
            data = f.read(MAX_PAGE_BYTES + 1)
    except (OSError, ValueError) as err:
        return None, f"cannot be read ({err.__class__.__name__})"
    if len(data) > MAX_PAGE_BYTES:
        return None, f"is larger than {MAX_PAGE_BYTES // (1024 * 1024)} MiB"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "is not valid UTF-8"
    if "\0" in text:
        return None, "holds a NUL byte"
    return text, ""


def is_external(url: str) -> bool:
    parts = urlsplit(url)
    return bool(parts.scheme or parts.netloc)


def target(public: Path, page: Path, url: str) -> Path | None:
    """The file a local URL points at, or None for anchors and mailto."""
    parts = urlsplit(url)
    if parts.scheme in ("mailto",) or not parts.path:
        return None
    path = unquote(parts.path)
    base = public if path.startswith("/") else page.parent
    if "\0" in path:
        return None
    try:
        resolved = (base / path.lstrip("/")).resolve()
        if path.endswith("/") or resolved.is_dir():
            resolved = resolved / "index.html"
    except (OSError, RuntimeError):  # a link loop, a name too long
        return base / "\uFFFD-unresolvable"
    return resolved


def check(public: Path) -> list[str]:
    problems: list[str] = []
    try:
        pages = sorted(public.rglob("*.html")) if public.is_dir() else []
        site = public.resolve()
    except (OSError, ValueError, RuntimeError) as err:
        return [f"{public}: cannot be read ({err.__class__.__name__})"]
    if not pages:
        return [f"{public}: no pages"]
    for page in pages:
        rel = page.relative_to(public)
        text, why = read_page(page)
        if text is None:
            problems.append(f"{rel}: {why}")
            continue
        parser = Page()
        parser.feed(text)
        problems += [f"{rel}: {p}" for p in parser.problems]
        if not parser.lang:
            problems.append(f"{rel}: <html> has no lang")
        for url in parser.resources:
            if is_external(url):
                problems.append(f"{rel}: loads {url} from another host")
        for url in [u for u in parser.resources + parser.links if not is_external(u)]:
            dest = target(public, page, url)
            if dest is None:
                continue
            if not dest.is_relative_to(site):
                problems.append(f"{rel}: {url} points outside the site")
            elif not is_file(dest):
                problems.append(f"{rel}: {url} does not exist")
        # Language twins: /x <-> /<lang>/x for every site language.
        first = rel.parts[0] if len(rel.parts) > 1 else ""
        lang = first if first in build_site.LANGUAGES else build_site.DEFAULT
        if parser.lang and parser.lang != lang:
            problems.append(f"{rel}: lang is {parser.lang}, expected {lang}")
        base = rel if lang == build_site.DEFAULT else Path(*rel.parts[1:])
        for other in build_site.LANGUAGES:
            twin = base if other == build_site.DEFAULT else Path(other) / base
            if not is_file(public / twin):
                problems.append(f"{rel}: no {other} translation at {twin}")
    problems += check_security_txt(public)
    return problems


def is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except (OSError, ValueError):
        return False


def check_security_txt(public: Path) -> list[str]:
    path = public / ".well-known" / "security.txt"
    if not is_file(path):
        return [".well-known/security.txt is missing"]
    text, why = read_page(path)
    if text is None:
        return [f".well-known/security.txt {why}"]
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields.setdefault(key.strip().lower(), value.strip())
    problems = []
    if "contact" not in fields:
        problems.append("security.txt: no Contact")
    expires = fields.get("expires", "")
    try:
        when = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError:
        problems.append(f"security.txt: Expires {expires!r} is not a date")
    else:
        if when.tzinfo is None:
            problems.append(f"security.txt: Expires {expires} has no time zone (RFC 9116)")
        elif when <= datetime.now(UTC):
            problems.append(f"security.txt: Expires {expires} has passed, renew it")
    return problems


def main(argv: list[str]) -> int:
    public = Path(argv[1]) if len(argv) > 1 else ROOT / "site" / "public"
    if "\0" in str(public):
        print("a folder name cannot hold a NUL byte", file=sys.stderr)
        return 2
    problems = check(public)
    try:
        same = public.resolve() == build_site.PUBLIC.resolve()
    except (OSError, RuntimeError):
        same = False
    if same and build_site.main(["", "--check"]):
        problems.append("site/public is out of date: run python3 tools/build_site.py")
    for p in problems:
        print(p, file=sys.stderr)
    if not problems:
        print(f"site ok: {len(list(public.rglob('*.html')))} pages")
    return 1 if problems else 0


if __name__ == "__main__":
    # A file name that is not UTF-8 is printed escaped, not as a traceback.
    sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    sys.exit(main(sys.argv))
