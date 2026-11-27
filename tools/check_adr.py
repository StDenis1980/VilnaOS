#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the ADR registry: filenames, statuses and the table in docs/adr/README.md.

For every tracked file in ``docs/adr/`` the checker verifies:

* an ADR file is named ``NNNN-slug.md`` (four digits, a slug of lowercase Latin
  letters, digits and hyphens, no leading, trailing or doubled hyphen); the
  numbers are unique; any other file in the folder except ``README.md`` is an
  error;
* the first non-empty line is an H1 of the form ``# NNNN. Title``, and ``NNNN``
  matches the filename;
* a list item ``- Стан:`` gives the status; the statuses used in this repository
  and in the template are ``Прийнято``, ``Запропоновано``, ``Пропозиція`` and
  ``Замінено`` (the first two proposed wordings are both in use and are not the
  same word); commentary after a space, a semicolon or ``(`` is allowed;
* an ADR whose status is ``Замінено`` names an existing ADR in parentheses
  (``Замінено (NNNN)``, a filename, or a Markdown link);
* each ADR file appears in the table of ``README.md`` exactly once; the number
  in the first column matches the number of the file the link points to; a
  table row without a file is an error; the status in the file matches the
  «Стан» column (the status word only, ignoring case and punctuation);
* missing numbers between 0001 and the highest number are a warning (one
  number may be in an open pull request);
* a Markdown link from one ADR to another (``NNNN-slug.md``) points at a
  tracked ADR file; a link to a missing number that is already reported as a
  gap is a warning, because that file may be in an open pull request.

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but
do not fail the check; they mark things the repo already does today (see
``LEGACY``) and the missing numbers. Only ``git ls-files`` is run. Nothing else
is executed, nothing is written.

Run: python3 tools/check_adr.py [docs/adr/FILE.md ...]
Without arguments every tracked file in ``docs/adr/`` is checked (the registry
is always read as a whole). With arguments the same full check runs; each
argument must be one of those tracked files.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import posixpath
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

ADR_DIR = "docs/adr"
README = "docs/adr/README.md"

# An ADR filename: four digits, a slug of lowercase Latin letters, digits and
# hyphens (no leading, trailing or doubled hyphen).
ADR_NAME = re.compile(r"^(\d{4})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
# Any filename that looks like an attempt at an ADR, used to catch a bad slug.
ADR_NAME_LOOSE = re.compile(r"^(\d{4})-.+\.md$")
TITLE = re.compile(r"^# (\d{4})\. (.+\S)\s*$")
STATUS_LINE = re.compile(r"^- Стан:\s*(.*)$")
STATUS_TOKEN = re.compile(r"^[^\s;(]+")
# A Markdown link to an ADR file (optional ./, angle brackets and #anchor).
ADR_HREF = re.compile(
    r"\((?:<)?(?:\./)?(?P<name>\d{4}-[a-z0-9-]+\.md)(?P<anchor>#[^)>]*)?(?:>)?\)"
)
TABLE_ROW = re.compile(r"^\| \[(\d{4})\]\(([^)\s]+)\) \| (.*) \| (.*) \|\s*$")
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# The target of "Замінено (NNNN)", "Замінено (NNNN-slug.md)" or a Markdown link.
SUPERSEDED_REF = re.compile(
    r"\(\s*(?:\[(?P<linknum>\d{4})\]\((?P<linkhref>[^)]+)\)|"
    r"(?P<plain>(?P<num>\d{4})(?:-[a-z0-9-]+\.md)?))\s*\)"
)

# Statuses used in the ADRs of this repository and in the template in README.md.
# "Пропозиція" and "Запропоновано" are both in use and are not the same word.
STATUSES = ("Прийнято", "Запропоновано", "Пропозиція", "Замінено")
SUPERSEDED = "Замінено"

# Known violations already in the repo: reported as warnings so CI can start.
# Fix the file (or the table), then delete the line.
LEGACY: frozenset[tuple[str, str]] = frozenset(
    {
        # The file says "Пропозиція", the table says "Запропоновано".
        ("docs/adr/0032-site-hosts-dns.md", "status-mismatch"),
    }
)


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    line: int = 0
    warning: bool = False

    def __str__(self) -> str:
        line = f" line {self.line}:" if self.line else ""
        kind = " warning:" if self.warning else ""
        return f"{self.path}:{line}{kind} {self.message}"


@dataclass
class Adr:
    path: str
    number: str  # four digits, as in the filename
    slug: str
    title_number: str = ""
    title_line: int = 0
    status: str = ""  # canonical status word, or empty if missing or unknown
    status_raw: str = ""
    status_line: int = 0
    problems: list[Problem] = field(default_factory=list)
    links: list[tuple[int, str]] = field(default_factory=list)  # line, filename


@dataclass
class TableRow:
    line: int
    number: str
    href: str
    href_name: str
    status: str
    status_raw: str


def fold(text: str) -> str:
    """Lower-case text with punctuation and spaces removed."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


STATUS_FOLDED = {fold(name): name for name in STATUSES}


def status_word(text: str) -> str:
    """The canonical status word at the start of ``text``, or empty."""
    match = STATUS_TOKEN.match(text.strip())
    if not match:
        return ""
    token = match.group(0).rstrip(".,:;!?…")
    return STATUS_FOLDED.get(fold(token), "")


def iter_unfenced(text: str) -> Iterator[tuple[int, str]]:
    """Lines of ``text`` that are not inside a fenced code block."""
    closing: str | None = None
    for index, line in enumerate(text.splitlines(), 1):
        opened = FENCE_OPEN.match(line)
        if closing is not None:
            if (
                opened
                and opened.group(1)[0] == closing[0]
                and len(opened.group(1)) >= len(closing)
            ):
                closing = None
            continue
        if opened:
            closing = opened.group(1)
            continue
        yield index, line


def href_filename(href: str) -> str:
    """The basename of a relative Markdown target, without an anchor."""
    target = href.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    target = target.split("#", 1)[0]
    return posixpath.basename(posixpath.normpath(target))


def read_text(root: Path, rel: str) -> tuple[str | None, Problem | None]:
    path = root / rel
    try:
        data = path.read_bytes()
    except OSError as err:
        return None, Problem(rel, f"cannot read the file: {err}")
    if data.startswith(b"\xef\xbb\xbf"):
        return None, Problem(rel, "the file starts with a byte order mark (BOM)")
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, Problem(rel, "the file is not valid UTF-8")


def parse_adr(rel: str, number: str, slug: str, text: str) -> Adr:
    adr = Adr(path=rel, number=number, slug=slug)
    first: str | None = None
    first_line = 0
    for index, line in enumerate(text.splitlines(), 1):
        if first is None and line.strip():
            first, first_line = line, index
        if not adr.status_line:
            match = STATUS_LINE.match(line)
            if match:
                adr.status_line = index
                adr.status_raw = match.group(1).strip()
                adr.status = status_word(adr.status_raw)
    if first is None:
        adr.problems.append(Problem(rel, "the file is empty"))
        return adr
    title = TITLE.match(first)
    if title is None:
        adr.problems.append(
            Problem(
                rel,
                "the first non-empty line must be an H1 of the form '# NNNN. Title'",
                first_line,
            )
        )
    else:
        adr.title_number = title.group(1)
        adr.title_line = first_line
        if title.group(1) != number:
            adr.problems.append(
                Problem(
                    rel,
                    f"heading number {title.group(1)} does not match the filename",
                    first_line,
                )
            )
    if not adr.status_line:
        adr.problems.append(Problem(rel, "missing the '- Стан:' field"))
    elif not adr.status_raw:
        adr.problems.append(Problem(rel, "the status is empty", adr.status_line))
    elif not adr.status:
        adr.problems.append(
            Problem(
                rel,
                f"unknown status {adr.status_raw!r} "
                f"(expected {', '.join(STATUSES)})",
                adr.status_line,
            )
        )
    for index, line in iter_unfenced(text):
        for match in ADR_HREF.finditer(line):
            adr.links.append((index, match.group("name")))
    return adr


def parse_table(text: str) -> list[TableRow]:
    rows: list[TableRow] = []
    for index, line in iter_unfenced(text):
        match = TABLE_ROW.match(line)
        if not match:
            continue
        href = match.group(2)
        rows.append(
            TableRow(
                line=index,
                number=match.group(1),
                href=href,
                href_name=href_filename(href),
                status=status_word(match.group(4)),
                status_raw=match.group(4).strip(),
            )
        )
    return rows


def superseded_target(status_raw: str) -> tuple[str, str] | None:
    """Return ``(number, filename_or_empty)`` named after «Замінено», if any."""
    match = SUPERSEDED_REF.search(status_raw)
    if not match:
        return None
    if match.group("linknum"):
        return match.group("linknum"), href_filename(match.group("linkhref") or "")
    plain = match.group("plain") or ""
    filename = plain if "-" in plain else ""
    return match.group("num"), filename


def add_legacy(problems: list[Problem], path: str, key: str, message: str, line: int = 0) -> None:
    warning = (path, key) in LEGACY
    problems.append(Problem(path, message, line, warning=warning))


def check_files(root: Path, tracked: Sequence[str]) -> list[Problem]:
    """Check the ADR files listed in ``tracked`` (paths from the repo root)."""
    problems: list[Problem] = []
    adrs: list[Adr] = []
    by_number: dict[str, list[Adr]] = defaultdict(list)
    by_name: dict[str, Adr] = {}
    readme_text: str | None = None

    for rel in tracked:
        normal = posixpath.normpath(rel.replace("\\", "/"))
        folder, name = posixpath.split(normal)
        if folder != ADR_DIR:
            problems.append(Problem(rel, f"not a file in {ADR_DIR}/"))
            continue
        if name == "README.md":
            text, err = read_text(root, normal)
            if err:
                problems.append(err)
            else:
                readme_text = text
            continue
        match = ADR_NAME.match(name)
        if match is None:
            if ADR_NAME_LOOSE.match(name):
                problems.append(
                    Problem(
                        normal,
                        "filename slug must be lowercase Latin letters, digits "
                        "and hyphens, without a leading, trailing or doubled hyphen",
                    )
                )
            else:
                problems.append(
                    Problem(
                        normal,
                        "filename must be NNNN-slug.md (four digits, a slug of "
                        "lowercase Latin letters, digits and hyphens)",
                    )
                )
            continue
        text, err = read_text(root, normal)
        if err:
            problems.append(err)
            continue
        assert text is not None
        adr = parse_adr(normal, match.group(1), match.group(2), text)
        adrs.append(adr)
        by_number[adr.number].append(adr)
        by_name[name] = adr
        problems.extend(adr.problems)

    for number, group in by_number.items():
        if len(group) > 1:
            names = ", ".join(item.path for item in group)
            for item in group:
                problems.append(
                    Problem(item.path, f"number {number} is used by more than one file: {names}")
                )

    present = {int(number) for number, group in by_number.items() if group}
    gaps: set[int] = set()
    if present:
        gaps = set(range(1, max(present) + 1)) - present
        for gap in sorted(gaps):
            problems.append(
                Problem(
                    README,
                    f"missing ADR number {gap:04d} (a number may be in an open pull request)",
                    warning=True,
                )
            )

    tracked_norm = {posixpath.normpath(path.replace("\\", "/")) for path in tracked}
    if readme_text is not None:
        problems.extend(check_table(readme_text, adrs, by_name))
    elif README not in tracked_norm:
        problems.append(Problem(README, "the registry file is missing"))

    existing_names = set(by_name)
    for adr in adrs:
        if adr.status == SUPERSEDED:
            problems.extend(check_superseded(adr, present, existing_names))
        for line, target in adr.links:
            problems.extend(check_link(adr, line, target, existing_names, gaps))
    return problems


def check_table(text: str, adrs: Sequence[Adr], by_name: dict[str, Adr]) -> list[Problem]:
    problems: list[Problem] = []
    rows = parse_table(text)
    if not rows:
        if adrs:
            problems.append(Problem(README, "no registry table found"))
        return problems
    seen_files: dict[str, TableRow] = {}
    listed: set[str] = set()
    for row in rows:
        if not row.href_name:
            problems.append(
                Problem(
                    README,
                    f"table row {row.number} points to {row.href!r}, which is not an ADR file",
                    row.line,
                )
            )
            continue
        match = ADR_NAME.match(row.href_name)
        if match is None:
            problems.append(
                Problem(
                    README,
                    f"table row {row.number} points to {row.href_name}, "
                    f"which is not an ADR filename",
                    row.line,
                )
            )
            continue
        file_number = match.group(1)
        if file_number != row.number:
            problems.append(
                Problem(
                    README,
                    f"table number {row.number} does not match the linked file {row.href_name}",
                    row.line,
                )
            )
        if row.href_name in seen_files:
            problems.append(
                Problem(
                    README,
                    f"{row.href_name} appears more than once in the table",
                    row.line,
                )
            )
        seen_files[row.href_name] = row
        listed.add(row.href_name)
        adr = by_name.get(row.href_name)
        if adr is None:
            problems.append(
                Problem(
                    README,
                    f"table row {row.number} points to missing file {row.href_name}",
                    row.line,
                )
            )
            continue
        if adr.status and row.status and adr.status != row.status:
            add_legacy(
                problems,
                adr.path,
                "status-mismatch",
                f"status {adr.status!r} does not match the table column "
                f"{row.status!r} (line {row.line})",
                adr.status_line,
            )
        elif adr.status and not row.status:
            problems.append(
                Problem(
                    README,
                    f"table row {row.number} has unknown status {row.status_raw!r} "
                    f"(expected {', '.join(STATUSES)})",
                    row.line,
                )
            )
    for adr in adrs:
        if posixpath.basename(adr.path) not in listed:
            problems.append(Problem(adr.path, f"not listed in {README}"))
    return problems


def check_superseded(
    adr: Adr,
    present: set[int],
    existing_names: set[str],
) -> list[Problem]:
    target = superseded_target(adr.status_raw)
    if target is None:
        return [
            Problem(
                adr.path,
                "status 'Замінено' must name an existing ADR in parentheses, "
                "for example 'Замінено (0001)'",
                adr.status_line,
            )
        ]
    number, filename = target
    if number == adr.number:
        return [
            Problem(
                adr.path,
                "status 'Замінено' must not point at this same ADR",
                adr.status_line,
            )
        ]
    if filename:
        match = ADR_NAME.match(filename)
        if match is None or filename not in existing_names:
            return [
                Problem(
                    adr.path,
                    f"status 'Замінено' points to missing ADR {filename}",
                    adr.status_line,
                )
            ]
        if match.group(1) != number:
            return [
                Problem(
                    adr.path,
                    f"status 'Замінено' number {number} does not match {filename}",
                    adr.status_line,
                )
            ]
        return []
    if int(number) not in present:
        return [
            Problem(
                adr.path,
                f"status 'Замінено' points to missing ADR {number}",
                adr.status_line,
            )
        ]
    return []


def check_link(
    adr: Adr,
    line: int,
    target: str,
    existing_names: set[str],
    gaps: set[int],
) -> list[Problem]:
    if target in existing_names:
        return []
    match = ADR_NAME.match(target) or ADR_NAME_LOOSE.match(target)
    number = int(match.group(1)) if match else None
    if number in gaps:
        return [
            Problem(
                adr.path,
                f"link to {target} points at missing ADR {number:04d} "
                f"(a number may be in an open pull request)",
                line,
                warning=True,
            )
        ]
    return [Problem(adr.path, f"link to {target} points at a missing ADR file", line)]


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", f"{ADR_DIR}/"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(path for path in out.split("\0") if path)



def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    usage = "usage: python3 tools/check_adr.py [docs/adr/FILE.md ...]"
    if any(a.startswith("-") for a in args):
        print(usage, file=sys.stderr)
        return 0 if args in (["-h"], ["--help"]) else 2
    try:
        tracked = tracked_files(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    if not tracked:
        print(f"no tracked files in {ADR_DIR}/", file=sys.stderr)
        return 2
    if args:
        wanted = {posixpath.normpath(a.replace("\\", "/")) for a in args}
        known = {posixpath.normpath(p.replace("\\", "/")) for p in tracked}
        unknown = sorted(wanted - known)
        if unknown:
            print(f"not a tracked file in {ADR_DIR}/: {unknown[0]}\n{usage}", file=sys.stderr)
            return 2
    problems = check_files(ROOT, tracked)
    for problem in problems:
        print(problem, file=sys.stderr)
    errors = sum(not p.warning for p in problems)
    warnings = sum(p.warning for p in problems)
    adrs = sum(1 for p in tracked if ADR_NAME.match(posixpath.basename(p)))
    print(f"{adrs} ADR files checked: {errors} errors, {warnings} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
