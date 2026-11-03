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
- Every Ukrainian page has its English twin under /en/ and back.
- security.txt has a Contact and an Expires date in the future.

Stdlib only, so it runs anywhere CI does.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
RESOURCE_ATTRS = {"src", "srcset", "poster", "data"}


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
        if "href" in a:
            if tag == "a":
                self.links.append(a["href"])
            elif tag == "link" and a.get("rel") not in ("alternate", "canonical"):
                self.resources.append(a["href"])


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
    resolved = (base / path.lstrip("/")).resolve()
    if path.endswith("/") or resolved.is_dir():
        resolved = resolved / "index.html"
    return resolved


def check(public: Path) -> list[str]:
    problems: list[str] = []
    pages = sorted(public.rglob("*.html"))
    if not pages:
        return [f"{public}: no pages"]
    for page in pages:
        rel = page.relative_to(public)
        parser = Page()
        parser.feed(page.read_text(encoding="utf-8"))
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
            if not dest.is_relative_to(public.resolve()):
                problems.append(f"{rel}: {url} points outside the site")
            elif not dest.is_file():
                problems.append(f"{rel}: {url} does not exist")
        # Language twins: /x/index.html <-> /en/x/index.html (404 is shared).
        if rel.name == "404.html":
            continue
        if rel.parts[0] == "en":
            twin = public.joinpath(*rel.parts[1:])
            want = "en"
        else:
            twin = public / "en" / rel
            want = "uk"
        if parser.lang and parser.lang != want:
            problems.append(f"{rel}: lang is {parser.lang}, expected {want}")
        if not twin.is_file():
            problems.append(f"{rel}: no translation at {twin.relative_to(public)}")
    problems += check_security_txt(public)
    return problems


def check_security_txt(public: Path) -> list[str]:
    path = public / ".well-known" / "security.txt"
    if not path.is_file():
        return [".well-known/security.txt is missing"]
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
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
        if when <= datetime.now(UTC):
            problems.append(f"security.txt: Expires {expires} has passed, renew it")
    return problems


def main(argv: list[str]) -> int:
    public = Path(argv[1]) if len(argv) > 1 else ROOT / "site" / "public"
    problems = check(public)
    for p in problems:
        print(p, file=sys.stderr)
    if not problems:
        print(f"site ok: {len(list(public.rglob('*.html')))} pages")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
