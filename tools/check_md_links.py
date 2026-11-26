#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the local links and anchors of the tracked Markdown files of the repo.

For every tracked ``*.md`` file the checker finds the links ``[text](target)``,
the images ``![alt](target)`` and the reference definitions ``[label]: target``.
Fenced code blocks (``` or ~~~), inline code and HTML comments are skipped.

* ``http:``, ``https:`` and ``mailto:`` targets are only counted: they are never
  opened and nothing is read from the network. Other schemes (``tel:``,
  ``ftp:`` ...) and ``//host/path`` are counted as external too.
* A local target is a path relative to the file (``%20`` and other percent
  escapes are decoded; ``?query`` is ignored). It must be a tracked file
  (``git ls-files``) or a folder that contains at least one tracked file.
* An anchor (``#section`` or ``file.md#section``) must match a heading of the
  target file as GitHub makes the anchor: lower case, spaces become ``-``,
  punctuation is dropped, letters of any alphabet (Cyrillic too) stay, repeated
  headings get ``-1``, ``-2``. Explicit ``<a name="x">`` / ``id="x"`` anchors and
  GitHub line anchors (``#L10``, ``#L10-L20``) are accepted as well.
* An absolute path (``/home/me/x``, ``C:\\Users\\x``, ``\\\\server\\share``,
  ``file:`` URLs), an empty target, a path that leaves the repo, or a file
  written with a trailing ``/`` as if it were a folder is an error.

Errors make the exit code 1. Warnings (``path: line N: warning: ...``) are
listed but do not fail the check; they mark broken links the repo already has
today (see ``LEGACY``). Only ``git ls-files`` is run; nothing else is executed.

Not checked: raw HTML (``<a href>``, ``<img src>``), reference links
``[text][label]`` whose label is not defined, bare URLs, ``<autolinks>``.

Run: python3 tools/check_md_links.py [FILE.md ...]
Without arguments every tracked ``*.md`` file is checked.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import bisect
import html
import posixpath
import re
import subprocess
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

# Broken links the repo already has today: (file, target as written). They are
# reported as warnings so that the check can be switched on in CI. Fix the link,
# then delete the line.
LEGACY: frozenset[tuple[str, str]] = frozenset(
    {
        # ADR 0020 is not in main yet (it is in an open pull request).
        ("docs/adr/0022-call-recording-and-dictation.md", "0020-gloxi-cloud-models.md"),
    }
)

SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*):")
COUNTED_SCHEMES = ("http", "https", "mailto")
WINDOWS_DRIVE = re.compile(r"[A-Za-z]:[\\/]")
LINE_ANCHOR = re.compile(r"L\d+(C\d+)?(-L\d+(C\d+)?)?")
FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$|^ {0,3}(#{1,6})[ \t]*$")
SETEXT_UNDERLINE = re.compile(r"^ {0,3}(=+|-{2,})[ \t]*$")
DEFINITION = re.compile(
    r"^ {0,3}\[([^\]\n]+)\]:[ \t]*(<[^>\n]*>|\S+)(?:[ \t]+(?:\"[^\"]*\"|'[^']*'))?[ \t]*$"
)
HTML_ANCHOR = re.compile(r"""<[A-Za-z][^<>]*?\b(?:name|id)\s*=\s*["']([^"']+)["']""")
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n).)+?)(?<!`)\1(?!`)", re.S)
COMMENT = re.compile(r"<!--.*?-->", re.S)
BLANK_LINE = re.compile(r"\n[ \t]*\n")


@dataclass(frozen=True)
class Problem:
    path: str
    line: int
    message: str
    warning: bool = False

    def __str__(self) -> str:
        kind = "warning: " if self.warning else ""
        return f"{self.path}: line {self.line}: {kind}{self.message}"


@dataclass(frozen=True)
class Link:
    line: int
    target: str  # as written, without the <> around it
    kind: str  # "link", "image" or "definition"


@dataclass
class Result:
    problems: list[Problem] = field(default_factory=list)
    files: int = 0
    links: int = 0
    external: int = 0
    local: int = 0
    anchors: int = 0

    @property
    def errors(self) -> int:
        return sum(not p.warning for p in self.problems)

    @property
    def warnings(self) -> int:
        return sum(p.warning for p in self.problems)


# --- reading Markdown -------------------------------------------------------


def blank(text: str) -> str:
    """The text with every character but the line breaks replaced by a space."""
    return re.sub(r"[^\n]", " ", text)


def strip_fences(text: str) -> str:
    """Empty the lines of fenced code blocks (fences included); keep the line count."""
    out: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.split("\n"):
        match = FENCE.match(line)
        if fence is None:
            if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                fence = (match.group(1)[0], len(match.group(1)))
                out.append("")
            else:
                out.append(line)
        else:
            out.append("")
            closes = match and match.group(1)[0] == fence[0] and len(match.group(1)) >= fence[1]
            if closes and not match.group(2).strip():
                fence = None
    return "\n".join(out)


def prose(text: str) -> str:
    """Text where links are looked for: no fenced blocks, comments or inline code."""
    no_fences = strip_fences(text.replace("\r\n", "\n").replace("\r", "\n"))
    no_comments = COMMENT.sub(lambda m: blank(m.group()), no_fences)
    return CODE_SPAN.sub(lambda m: blank(m.group()), no_comments)


def parse_destination(text: str, start: int) -> tuple[str, int] | None:
    """Read ``(target "title")`` from the ``(`` at ``start``; return (target, end)."""
    i = start + 1
    n = len(text)
    while i < n and text[i] in " \t\n":
        i += 1
    if i < n and text[i] == "<":
        end = text.find(">", i)
        if end == -1 or "\n" in text[i:end]:
            return None
        target, i = text[i + 1 : end], end + 1
    else:
        begin, depth = i, 0
        while i < n and text[i] not in " \t\n":
            if text[i] == "\\" and i + 1 < n:
                i += 2
                continue
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                if depth == 0:
                    break
                depth -= 1
            i += 1
        target = text[begin:i]
    while i < n and text[i] in " \t\n":
        i += 1
    if i < n and text[i] in "\"'(":
        closer = ")" if text[i] == "(" else text[i]
        end = text.find(closer, i + 1)
        if end == -1:
            return None
        i = end + 1
        while i < n and text[i] in " \t\n":
            i += 1
    if i < n and text[i] == ")":
        return target, i + 1
    return None


def opening_bracket(text: str, close: int, floor: int) -> int | None:
    """Index of the ``[`` that matches the ``]`` at ``close`` (not before ``floor``)."""
    depth = 0
    for i in range(close, floor - 1, -1):
        if i > 0 and text[i - 1] == "\\" and (i < 2 or text[i - 2] != "\\"):
            continue
        if text[i] == "]":
            depth += 1
        elif text[i] == "[":
            depth -= 1
            if depth == 0:
                return i
    return None


def extract_links(text: str) -> list[Link]:
    """All inline links, images and reference definitions of a Markdown text."""
    clean = prose(text)
    starts = [m.end() for m in BLANK_LINE.finditer(clean)]
    newlines = [i for i, ch in enumerate(clean) if ch == "\n"]

    def line_of(pos: int) -> int:
        return bisect.bisect_left(newlines, pos) + 1

    links: list[Link] = []
    pos = clean.find("](")
    while pos != -1:
        escaped = pos > 0 and clean[pos - 1] == "\\"
        floor = starts[bisect.bisect_right(starts, pos) - 1] if starts else 0
        floor = floor if floor <= pos else 0
        opener = None if escaped else opening_bracket(clean, pos, floor)
        parsed = parse_destination(clean, pos + 1) if opener is not None else None
        if parsed is not None and opener is not None:
            kind = "image" if opener > 0 and clean[opener - 1] == "!" else "link"
            links.append(Link(line_of(pos), parsed[0], kind))
        pos = clean.find("](", pos + 1)
    for number, line in enumerate(clean.split("\n"), start=1):
        match = DEFINITION.match(line)
        if match and not match.group(1).startswith("^"):
            target = match.group(2)
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            links.append(Link(number, target, "definition"))
    return sorted(links, key=lambda link: link.line)


# --- anchors -----------------------------------------------------------------


def heading_text(raw: str) -> str:
    """The visible text of a heading: Markdown and HTML marks removed."""
    spans: list[str] = []

    def keep(match: re.Match[str]) -> str:
        spans.append(match.group(2).strip())
        return f"\0{len(spans) - 1}\0"

    text = re.sub(r"(`+)(.+?)\1", keep, raw)  # code is literal: <id> in it stays
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"<[^<>]*>", "", text)
    text = re.sub(r"(?<!\w)_+(.+?)_+(?!\w)", r"\1", text)
    text = re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", text)
    text = html.unescape(text)
    return re.sub(r"\0(\d+)\0", lambda m: spans[int(m.group(1))], text).strip()


def slugify(text: str) -> str:
    """GitHub's heading anchor, before the -1, -2 suffix of repeated headings."""
    kept = []
    for ch in text.lower():
        if ch in " -_" or unicodedata.category(ch)[0] in "LMN" or unicodedata.category(ch) == "Pc":
            kept.append("-" if ch == " " else ch)
    return "".join(kept)


def heading_slugs(text: str) -> list[str]:
    """The anchors GitHub gives to the headings of the text, in document order."""
    lines = strip_fences(text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = COMMENT.sub(lambda m: blank(m.group()), lines).split("\n")
    raws: list[str] = []
    for i, line in enumerate(lines):
        atx = ATX_HEADING.match(line)
        if atx:
            raws.append(atx.group(2) or "")
        elif i > 0 and SETEXT_UNDERLINE.match(line) and is_paragraph_line(lines[i - 1]):
            raws.append(lines[i - 1].strip())
    seen: dict[str, int] = {}
    slugs = []
    for raw in raws:
        base = slug = slugify(heading_text(raw))
        while slug in seen:
            seen[base] += 1
            slug = f"{base}-{seen[base]}"
        seen[slug] = 0
        slugs.append(slug)
    return slugs


def is_paragraph_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not re.match(r"[#>|]|[-*+][ \t]|\d+[.)][ \t]|(=+|-+)$", stripped)


def anchors_of(text: str) -> set[str]:
    """Every anchor a Markdown file offers: headings and explicit name/id anchors."""
    explicit = HTML_ANCHOR.findall(prose(text))
    return set(heading_slugs(text)) | {a.lower() for a in explicit}


# --- checking ----------------------------------------------------------------


def split_target(target: str) -> tuple[str, str | None]:
    """(decoded path, decoded anchor or None); a ?query is dropped."""
    path, sep, fragment = target.partition("#")
    path = unquote(path.partition("?")[0])
    return path, unquote(fragment) if sep else None


def absolute_problem(target: str) -> str | None:
    if target.startswith("//"):
        return None  # //host/path: an external link without a scheme
    if target.startswith(("/", "\\")) or WINDOWS_DRIVE.match(target):
        return f"absolute path '{target}' (links must be relative to the file)"
    if target.lower().startswith("file:"):
        return f"absolute path '{target}' (links must be relative to the file)"
    return None


def is_external(target: str) -> bool:
    if target.startswith("//"):
        return True
    match = SCHEME.match(target)
    return match is not None and len(match.group(1)) > 1


class Checker:
    def __init__(self, root: Path, tracked: Sequence[str]) -> None:
        self.root = root
        self.files = set(tracked)
        self.dirs = {d for f in tracked for d in parents(f)}
        self._anchors: dict[str, set[str]] = {}

    def anchors(self, rel: str) -> set[str]:
        if rel not in self._anchors:
            try:
                text = (self.root / rel).read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                text = ""
            self._anchors[rel] = anchors_of(text)
        return self._anchors[rel]

    def check_file(self, rel: str, result: Result) -> None:
        try:
            text = (self.root / rel).read_bytes().decode("utf-8-sig")
        except OSError as err:
            result.problems.append(Problem(rel, 1, f"cannot read the file: {err.strerror or err}"))
            return
        except UnicodeDecodeError:
            result.problems.append(Problem(rel, 1, "the file is not valid UTF-8"))
            return
        result.files += 1
        for link in extract_links(text):
            result.links += 1
            message = self.check_link(rel, link, result)
            if message:
                legacy = (rel, link.target) in LEGACY
                result.problems.append(Problem(rel, link.line, message, warning=legacy))

    def check_link(self, rel: str, link: Link, result: Result) -> str | None:
        target = link.target.strip()
        if not target:
            return f"empty link target in {link.kind}"
        problem = absolute_problem(target)
        if problem:
            return problem
        if is_external(target):
            result.external += 1
            return None
        path, fragment = split_target(target)
        here = posixpath.dirname(rel)
        joined = posixpath.normpath(posixpath.join(here, path)) if path else rel
        if joined == ".." or joined.startswith("../"):
            return f"'{target}' points outside the repository"
        result.local += 1
        if path:
            problem = self.path_problem(path, joined, target)
            if problem:
                return problem
        if fragment is not None and fragment:
            result.anchors += 1
            return self.anchor_problem(joined, fragment, target)
        return None

    def path_problem(self, path: str, joined: str, target: str) -> str | None:
        shown = f"'{target}'"
        if joined in self.files:
            if path.endswith("/"):
                return f"{shown} is a file, not a folder"
            return None
        if joined == "." or joined in self.dirs:
            return None
        return f"{shown} does not exist (no tracked file or folder {joined})"

    def anchor_problem(self, joined: str, fragment: str, target: str) -> str | None:
        if joined not in self.files or not joined.lower().endswith(".md"):
            return None  # a folder, or a file whose anchors are not headings
        if LINE_ANCHOR.fullmatch(fragment) or fragment.lower() in self.anchors(joined):
            return None
        return f"anchor '#{fragment}' of '{target}' not found in {joined}"


def parents(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def check_repo(root: Path, tracked: Sequence[str], only: Sequence[str] = ()) -> Result:
    """Check the Markdown files among ``tracked`` (paths relative to ``root``).

    ``only`` limits the files whose links are checked; targets are always looked
    up among all ``tracked`` paths.
    """
    checker = Checker(root, tracked)
    result = Result()
    names = only or [p for p in tracked if p.lower().endswith(".md")]
    for rel in sorted(names):
        checker.check_file(rel, result)
    return result


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, text=True
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if any(a.startswith("-") for a in args):
        print("usage: python3 tools/check_md_links.py [FILE.md ...]", file=sys.stderr)
        return 0 if args in (["-h"], ["--help"]) else 2
    try:
        tracked = tracked_files(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    only = [a.replace("\\", "/") for a in args]
    unknown = [a for a in only if a not in set(tracked) or not a.lower().endswith(".md")]
    if unknown:
        print(f"not a tracked .md file: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if not any(p.lower().endswith(".md") for p in tracked):
        print("no tracked *.md files found", file=sys.stderr)
        return 2
    result = check_repo(ROOT, tracked, only)
    for problem in result.problems:
        print(problem, file=sys.stderr)
    print(
        f"{result.files} Markdown files checked: {result.links} links "
        f"({result.external} external, not fetched; {result.local} local, "
        f"{result.anchors} with an anchor): {result.errors} errors, {result.warnings} warnings"
    )
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
