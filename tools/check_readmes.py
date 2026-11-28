#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check that README.md, README.uk.md and README.es.md say the same thing.

``README.md`` (English) is the reference; the Ukrainian and the Spanish file are
compared with it. The check looks at the structure, not at the words:

* headings: the same number of headings and the same level in the same order
  (ATX ``## x`` and setext ``x`` + ``---``; fenced code and HTML comments are
  skipped);
* links to repo files: the same set of local targets in every file, from
  ``[text](target)``, images, ``[label]: target`` definitions and the ``src``,
  ``href`` and ``srcset`` attributes of HTML tags. Anchors (``#section``) and
  external links (``https:``, ``mailto:`` ...) are left out. A language suffix
  is ignored when comparing (``docs/getting-started.uk.md`` equals
  ``docs/getting-started.md``);
* a link must lead to the page in the language of the README when that page
  exists (README.uk.md must link ``getting-started.uk.md``, not the English one);
* code blocks: the same number of blocks, the same language tag and the same
  commands. In shell blocks (sh, bash, console ...) blank lines and full-line
  ``#`` comments are ignored, because comments are translated;
* language links: every README links both other READMEs, so the links are
  mutual. These links are not part of the link comparison above;
* each file is readable: a regular UTF-8 text file inside the repo (no UTF-16,
  no binary data, no symlink that leaves the repo, at most 2 MiB).

Every problem is an error (exit code 1) unless it is listed in ``LEGACY``;
those are shown as warnings. A UTF-8 byte order mark is only a warning. A
message looks like ``README.uk.md: line 18: problem``.

Nothing is executed, nothing is read from the network and nothing is written:
the three files are read directly, so not even ``git ls-files`` is run.

Not checked: the words of the text, external links, anchors, tables, raw HTML
headings (``<h2>``), reference links whose label is not defined.

Run: python3 tools/check_readmes.py [--root DIR]
Without arguments the repo that holds this script is checked.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import bisect
import posixpath
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

BASE = "README.md"
TRANSLATIONS = {"README.uk.md": "uk", "README.es.md": "es"}
FILES = (BASE, *TRANSLATIONS)
LANG_OF = {BASE: "en", **TRANSLATIONS}
MAX_BYTES = 2 * 1024 * 1024
SHELL_LANGS = frozenset({"sh", "bash", "zsh", "shell", "console", "shell-session", "shellsession"})
TEXT_SHOWN = 70  # characters of a command or heading shown in a message

# Differences the repo already has today: (file, rule). They are shown as warnings
# so that the check can be switched on in CI. Fix the text, then delete the line.
# The three README files match today, so the list is empty.
LEGACY: frozenset[tuple[str, str]] = frozenset()

FENCE = re.compile(r"^([ \t]*)(`{3,}|~{3,})(.*)$")
ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
SETEXT = re.compile(r"^ {0,3}(=+|-{2,})[ \t]*$")
LANG_SUFFIX = re.compile(r"\.(uk|es)\.md$")
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
INLINE_LINK = re.compile(r"\]\(\s*(<[^<>\n]*>|(?:[^\s()\\]|\\.|\([^\s()]*\))*)")
DEFINITION = re.compile(r"^ {0,3}\[[^\]\n]+\]:[ \t]*(<[^<>\n]*>|\S+)", re.M)
HTML_TAG = re.compile(r"<[A-Za-z][^<>]*>")
HTML_ATTR = re.compile(r"""\b(src|href|srcset)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    line: int = 0
    warning: bool = False
    rule: str = ""

    def __str__(self) -> str:
        where = f"line {self.line}: " if self.line else ""
        kind = "warning: " if self.warning else ""
        return f"{self.path}: {where}{kind}{self.message}"


@dataclass(frozen=True)
class Heading:
    line: int
    level: int
    text: str


@dataclass(frozen=True)
class Block:
    line: int
    lang: str
    lines: tuple[tuple[int, str], ...]  # (line number, text without trailing blanks)
    closed: bool = True


@dataclass(frozen=True)
class Link:
    line: int
    target: str  # normalised path from the repo root


@dataclass
class Doc:
    rel: str
    headings: list[Heading] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    switch: dict[str, int] = field(default_factory=dict)  # README name -> line


@dataclass
class Result:
    problems: list[Problem] = field(default_factory=list)
    files: int = 0
    headings: int = 0
    blocks: int = 0
    links: int = 0

    @property
    def errors(self) -> int:
        return sum(not p.warning for p in self.problems)

    @property
    def warnings(self) -> int:
        return sum(p.warning for p in self.problems)


def problem(path: str, message: str, rule: str, line: int = 0) -> Problem:
    """An error, or a warning when (path, rule) is a known old difference (LEGACY)."""
    return Problem(path, message, line, (path, rule) in LEGACY, rule)


def short(text: str) -> str:
    text = text if len(text) <= TEXT_SHOWN else text[: TEXT_SHOWN - 3] + "..."
    return repr(text)


# --- reading the file ---------------------------------------------------------


def read_readme(root: Path, rel: str, out: list[Problem]) -> str | None:
    """The text of a README with ``\\n`` line ends, or None after an error is added."""
    path = root / rel
    try:
        resolved = path.resolve(strict=True)
        inside = resolved.is_relative_to(root.resolve())
        is_file = resolved.is_file()
        size = resolved.stat().st_size if is_file else 0
    except FileNotFoundError:
        out.append(problem(rel, "file is missing", "missing-file"))
        return None
    except (OSError, RuntimeError) as err:  # a symlink loop, no permission ...
        out.append(problem(rel, f"cannot be read: {type(err).__name__}", "unreadable"))
        return None
    if not inside:
        out.append(problem(rel, "is a symlink that leaves the repo", "unreadable"))
        return None
    if not is_file:
        out.append(problem(rel, "is not a regular file", "unreadable"))
        return None
    if size > MAX_BYTES:
        out.append(problem(rel, f"is {size} bytes, more than {MAX_BYTES}", "unreadable"))
        return None
    try:
        data = resolved.read_bytes()
    except OSError as err:
        out.append(problem(rel, f"cannot be read: {type(err).__name__}", "unreadable"))
        return None
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        out.append(problem(rel, "is UTF-16; save it as UTF-8", "encoding"))
        return None
    if b"\0" in data:
        out.append(problem(rel, "contains NUL bytes: it is not a text file", "encoding"))
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as err:
        out.append(problem(rel, f"is not valid UTF-8 (byte {err.start})", "encoding"))
        return None
    if text.startswith("\ufeff"):
        out.append(Problem(rel, "starts with a byte order mark", 1, True, "bom"))
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --- reading Markdown ---------------------------------------------------------


def blank(text: str) -> str:
    """The text with everything but the line breaks replaced by a space."""
    return re.sub(r"[^\n]", " ", text)


def split_fences(lines: list[str]) -> tuple[list[str], list[Block]]:
    """Return the lines without fenced blocks (same count) and the blocks.

    Lines inside a multi-line HTML comment are not looked at for fences.
    """
    prose: list[str] = []
    blocks: list[Block] = []
    fence: tuple[str, int, str, int, str] | None = None  # char, length, indent, line, lang
    body: list[tuple[int, str]] = []
    in_comment = False
    for number, line in enumerate(lines, 1):
        if fence is None:
            if in_comment:
                prose.append(line)
                in_comment = "-->" not in line
                continue
            stripped = line.lstrip()
            if stripped.startswith("<!--") and "-->" not in stripped[4:]:
                in_comment = True
                prose.append(line)
                continue
            match = FENCE.match(line)
            if match and not (match.group(2)[0] == "`" and "`" in match.group(3)):
                info = match.group(3).strip()
                lang = info.split()[0].lower() if info else ""
                fence = (match.group(2)[0], len(match.group(2)), match.group(1), number, lang)
                body = []
            prose.append("" if fence else line)
            continue
        prose.append("")
        match = FENCE.match(line)
        closes = match and match.group(2)[0] == fence[0] and len(match.group(2)) >= fence[1]
        if match and closes and not match.group(3).strip():
            blocks.append(Block(fence[3], fence[4], tuple(body)))
            fence = None
            continue
        indent = fence[2]
        cut = line[len(indent) :] if not line[: len(indent)].strip() else line.lstrip()
        body.append((number, cut.rstrip()))
    if fence is not None:
        blocks.append(Block(fence[3], fence[4], tuple(body), closed=False))
    return prose, blocks


def drop_comments(text: str) -> str:
    """Blank the HTML comments; an unclosed one runs to the end of the text."""
    out: list[str] = []
    pos = 0
    while True:
        start = text.find("<!--", pos)
        if start < 0:
            out.append(text[pos:])
            break
        end = text.find("-->", start + 4)
        stop = len(text) if end < 0 else end + 3
        out.append(text[pos:start])
        out.append(blank(text[start:stop]))
        pos = stop
    return "".join(out)


def drop_code_spans(text: str) -> str:
    """Blank the inline code (``code``); a span never crosses a blank line."""
    runs = [(m.start(), m.end()) for m in re.finditer(r"`+", text)]
    next_same: list[int] = [-1] * len(runs)
    last: dict[int, int] = {}
    for index in range(len(runs) - 1, -1, -1):
        length = runs[index][1] - runs[index][0]
        next_same[index] = last.get(length, -1)
        last[length] = index
    out: list[str] = []
    pos = 0
    index = 0
    while index < len(runs):
        closer = next_same[index]
        if closer >= 0:
            between = text[runs[index][1] : runs[closer][0]]
            if not re.search(r"\n[ \t]*\n", between):
                out.append(text[pos : runs[index][0]])
                out.append(blank(text[runs[index][0] : runs[closer][1]]))
                pos = runs[closer][1]
                while index < len(runs) and runs[index][0] < pos:
                    index += 1
                continue
        index += 1
    out.append(text[pos:])
    return "".join(out)


def find_headings(shown: list[str], matched: list[str]) -> list[Heading]:
    """Headings; ``shown`` has the text to print, ``matched`` is without code and comments."""
    found: list[Heading] = []
    for index, line in enumerate(matched):
        atx = ATX.match(line)
        if atx:
            text = re.sub(r"[ \t]+#+$", "", (shown[index].strip().lstrip("#")).strip())
            found.append(Heading(index + 1, len(atx.group(1)), text))
            continue
        under = SETEXT.match(line)
        if not under or index == 0:
            continue
        above = matched[index - 1]
        if not above.strip() or ATX.match(above) or above.lstrip()[:1] in ("|", ">", "-", "*", "+"):
            continue
        if re.match(r"^\s*\d+[.)]\s", above):
            continue
        found.append(Heading(index, 1 if under.group(1)[0] == "=" else 2, shown[index - 1].strip()))
    return found


def normalise_target(raw: str) -> str | None:
    """A link target as a path from the repo root, or None if it is not a local file."""
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    if not target or target.startswith(("#", "//")) or SCHEME.match(target):
        return None
    target = unquote(re.split(r"[#?]", target, maxsplit=1)[0])
    if not target:
        return None
    path = posixpath.normpath(target.lstrip("/"))
    return None if path == "." else path


def find_links(text: str) -> list[Link]:
    """The local file links of a text without fenced code, comments and inline code."""
    starts = [0]
    for match in re.finditer(r"\n", text):
        starts.append(match.end())

    def line_of(pos: int) -> int:
        return bisect.bisect_right(starts, pos)

    found: list[Link] = []
    raw: list[tuple[int, str]] = []
    raw += [(m.start(1), m.group(1)) for m in INLINE_LINK.finditer(text)]
    raw += [(m.start(1), m.group(1)) for m in DEFINITION.finditer(text)]
    for tag in HTML_TAG.finditer(text):
        for attr in HTML_ATTR.finditer(tag.group()):
            value = attr.group(2) if attr.group(2) is not None else attr.group(3)
            parts = value.split(",") if attr.group(1).lower() == "srcset" else [value]
            for part in parts:
                words = part.split()
                if words:
                    raw.append((tag.start() + attr.start(), words[0]))
    for pos, target in sorted(raw):
        path = normalise_target(target)
        if path is not None:
            found.append(Link(line_of(pos), path))
    return found


def parse(rel: str, text: str) -> Doc:
    prose, blocks = split_fences(text.split("\n"))
    no_comments = drop_comments("\n".join(prose))
    matched = drop_code_spans(no_comments)
    doc = Doc(rel, blocks=blocks)
    doc.headings = find_headings(no_comments.split("\n"), matched.split("\n"))
    for link in find_links(matched):
        if link.target in FILES:
            doc.switch.setdefault(link.target, link.line)
        else:
            doc.links.append(link)
    return doc


# --- comparing ----------------------------------------------------------------


def commands(block: Block) -> list[tuple[int, str]]:
    """The lines to compare: shell blocks lose blank lines and full-line comments."""
    if block.lang not in SHELL_LANGS:
        return list(block.lines)
    return [(n, t) for n, t in block.lines if t.strip() and not t.lstrip().startswith("#")]


def compare_headings(base: Doc, other: Doc) -> list[Problem]:
    out: list[Problem] = []
    a, b = base.headings, other.headings
    if len(a) != len(b):
        out.append(problem(other.rel, f"{len(b)} headings, {base.rel} has {len(a)}", "headings"))
    for index in range(min(len(a), len(b))):
        if a[index].level != b[index].level:
            out.append(
                problem(
                    other.rel,
                    f"heading {short(b[index].text)} is level {b[index].level}, "
                    f"{base.rel} line {a[index].line} has level {a[index].level}",
                    "headings",
                    b[index].line,
                )
            )
            if len(a) != len(b):
                break  # after a shifted heading the rest is noise
    if len(a) > len(b):
        extra = a[len(b)]
        out.append(
            problem(
                other.rel,
                f"missing heading for {base.rel} line {extra.line}: {short(extra.text)}",
                "headings",
            )
        )
    elif len(b) > len(a):
        extra = b[len(a)]
        out.append(
            problem(
                other.rel,
                f"heading {short(extra.text)} is not in {base.rel}",
                "headings",
                extra.line,
            )
        )
    return out


def compare_links(base: Doc, other: Doc) -> list[Problem]:
    def keyed(doc: Doc) -> dict[str, int]:
        table: dict[str, int] = {}
        for link in doc.links:
            table.setdefault(LANG_SUFFIX.sub(".md", link.target), link.line)
        return table

    a, b = keyed(base), keyed(other)
    out = [
        problem(
            other.rel,
            f"no link to {target} ({base.rel} line {a[target]} has it)",
            "links",
        )
        for target in a
        if target not in b
    ]
    out += [
        problem(other.rel, f"link to {target} is not in {base.rel}", "links", b[target])
        for target in b
        if target not in a
    ]
    return out


def compare_blocks(base: Doc, other: Doc) -> list[Problem]:
    out: list[Problem] = []
    a, b = base.blocks, other.blocks
    if len(a) != len(b):
        out.append(problem(other.rel, f"{len(b)} code blocks, {base.rel} has {len(a)}", "code"))
    for left, right in zip(a, b, strict=False):
        if left.lang != right.lang:
            out.append(
                problem(
                    other.rel,
                    f"code block is {right.lang or 'untagged'}, "
                    f"{base.rel} line {left.line} is {left.lang or 'untagged'}",
                    "code",
                    right.line,
                )
            )
            continue
        want, got = commands(left), commands(right)
        for (_, x), (n, y) in zip(want, got, strict=False):
            if x != y:
                out.append(
                    problem(
                        other.rel,
                        f"command is {short(y)}, {base.rel} has {short(x)}",
                        "code",
                        n,
                    )
                )
                break
        else:
            if len(want) > len(got):
                missing = want[len(got)]
                out.append(
                    problem(
                        other.rel,
                        f"code block is missing {short(missing[1])} "
                        f"(from {base.rel} line {missing[0]})",
                        "code",
                        right.line,
                    )
                )
            elif len(got) > len(want):
                n, y = got[len(want)]
                out.append(
                    problem(other.rel, f"extra command {short(y)} is not in {base.rel}", "code", n)
                )
    return out


def language_of(path: str) -> str:
    match = LANG_SUFFIX.search(path)
    return match.group(1) if match else "en"


def exists(root: Path, rel: str) -> bool:
    if rel.startswith(".."):
        return False
    try:
        return (root / rel).exists()
    except (OSError, ValueError):
        return False


def check_language_links(root: Path, doc: Doc) -> list[Problem]:
    """A link to a page in another language, although the page in this one exists."""
    own = LANG_OF[doc.rel]
    out: list[Problem] = []
    for link in doc.links:
        if not link.target.lower().endswith(".md") or language_of(link.target) == own:
            continue
        stem = LANG_SUFFIX.sub(".md", link.target)
        expected = stem if own == "en" else stem[: -len(".md")] + f".{own}.md"
        if exists(root, expected):
            out.append(
                problem(
                    doc.rel,
                    f"link to {link.target}, but this file is in {own}: link {expected}",
                    "wrong-language",
                    link.line,
                )
            )
    return out


def check_switcher(doc: Doc) -> list[Problem]:
    return [
        problem(doc.rel, f"no link to {name} (the language links must be mutual)", "switcher")
        for name in FILES
        if name != doc.rel and name not in doc.switch
    ]


def check_repo(root: Path) -> Result:
    result = Result()
    docs: dict[str, Doc] = {}
    for rel in FILES:
        text = read_readme(root, rel, result.problems)
        if text is None:
            continue
        docs[rel] = parse(rel, text)
        result.files += 1
        result.headings += len(docs[rel].headings)
        result.blocks += len(docs[rel].blocks)
        result.links += len(docs[rel].links)
    for doc in docs.values():
        result.problems += [
            problem(
                doc.rel, f"code block is not closed (opened at line {b.line})", "markup", b.line
            )
            for b in doc.blocks
            if not b.closed
        ]
        result.problems += check_switcher(doc)
        result.problems += check_language_links(root, doc)
    base = docs.get(BASE)
    if base is not None:
        for rel in TRANSLATIONS:
            other = docs.get(rel)
            if other is not None:
                result.problems += compare_headings(base, other)
                result.problems += compare_links(base, other)
                result.problems += compare_blocks(base, other)
    return result


USAGE = "usage: python3 tools/check_readmes.py [--root DIR]"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["-h"], ["--help"]):
        print(USAGE, file=sys.stderr)
        return 0
    root = ROOT
    if len(args) == 2 and args[0] == "--root":
        root = Path(args[1])
    elif args:
        print(USAGE, file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"not a folder: {root}", file=sys.stderr)
        return 2
    if not any(exists(root, name) for name in FILES):
        print(f"no README.md, README.uk.md or README.es.md in {root}", file=sys.stderr)
        return 2
    result = check_repo(root)
    for item in result.problems:
        print(item, file=sys.stderr)
    print(
        f"{result.files} README files checked: {result.headings} headings, "
        f"{result.blocks} code blocks, {result.links} file links: "
        f"{result.errors} errors, {result.warnings} warnings"
    )
    return 1 if result.errors else 0


if __name__ == "__main__":
    # A file name that is not UTF-8 is printed escaped, not as a traceback.
    sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    sys.exit(main())
