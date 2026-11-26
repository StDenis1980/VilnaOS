#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the tracked ``*.metainfo.xml`` files (AppStream) of the repo.

For every file the checker verifies:

* the root element is ``<component>`` and its ``<id>`` equals the file name
  without ``.metainfo.xml``;
* ``<metadata_license>``, ``<project_license>``, ``<name>``, ``<summary>``,
  ``<description>`` and ``<content_rating>`` are present (the first five with
  an untranslated text, i.e. an element without ``xml:lang``);
* ``<releases>``: every ``<release>`` has a ``version`` and a ``date`` written
  YYYY-MM-DD and parsed with ``datetime.date.fromisoformat``; releases go from
  the newest to the oldest by date and by version (versions are compared by
  their numeric parts: ``0.10.0`` is newer than ``0.9.0``; ``1.0~a1`` is older
  than ``1.0``); no version is listed twice; no date is more than 30 days in
  the future;
* every ``<url>`` and every screenshot ``<image>`` starts with ``https://``;
* translated elements (``xml:lang``): an unknown language code is an error
  (known: uk es pl de fr it ro cs lt ca ca@valencia gl eu pt; English is the
  element without ``xml:lang``); ``<name>`` and ``<summary>`` should be
  translated into the same set of languages (a warning if not).

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but
do not fail the check; they mark things the repo already does today.

Only ``git ls-files`` is run (to find the files). Nothing else is executed,
nothing is read from the network and nothing is written.

Run: python3 tools/check_metainfo.py [FILE.metainfo.xml ...]
Without arguments every tracked ``*.metainfo.xml`` file is checked.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

SUFFIX = ".metainfo.xml"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
# The 14 translated languages; English is the element without xml:lang.
LANGS = (
    "uk",
    "es",
    "pl",
    "de",
    "fr",
    "it",
    "ro",
    "cs",
    "lt",
    "ca",
    "ca@valencia",
    "gl",
    "eu",
    "pt",
)
# Children of <component> that must exist; the flag says whether the element
# itself needs a text (a description is made of <p> and <ul> children).
REQUIRED = (
    ("metadata_license", True),
    ("project_license", True),
    ("name", True),
    ("summary", True),
    ("description", False),
    ("content_rating", False),
)
FUTURE_DAYS = 30
DATE_FORMAT = re.compile(r"\d{4}-\d{2}-\d{2}")
# A version we can order exactly: dot-separated numbers, then an optional ~suffix.
PLAIN_VERSION = re.compile(r"\d+(\.\d+)*(~[0-9A-Za-z.]+)?")


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False

    def __str__(self) -> str:
        return f"{self.path}: {'warning: ' if self.warning else ''}{self.message}"


def parse_xml(path: str, text: str) -> tuple[ET.Element | None, list[Problem]]:
    try:
        return ET.fromstring(text), []
    except ET.ParseError as err:
        return None, [Problem(path, f"not well-formed XML: {err}")]


def check_root(path: str, root: ET.Element) -> list[Problem]:
    """The ``<id>`` must equal the file name without ``.metainfo.xml``."""
    expected = Path(path).name.removesuffix(SUFFIX)
    ids = root.findall("id")
    if not ids:
        return [Problem(path, "missing <id>")]
    found = (ids[0].text or "").strip()
    if found != expected:
        return [Problem(path, f"<id> is '{found}' but the file name says '{expected}'")]
    return []


def is_filled(element: ET.Element, needs_text: bool) -> bool:
    """Has a text; elements like <description> may instead be made of child elements."""
    return bool((element.text or "").strip() or (len(element) and not needs_text))


def check_required(path: str, root: ET.Element) -> list[Problem]:
    problems = []
    for tag, needs_text in REQUIRED:
        found = root.findall(tag)
        plain = [e for e in found if XML_LANG not in e.attrib]
        if not found:
            problems.append(Problem(path, f"missing <{tag}>"))
        elif tag == "content_rating":
            continue  # <content_rating type="oars-1.1"/> is complete when empty
        elif not plain:
            problems.append(
                Problem(path, f"<{tag}> has no untranslated text (no xml:lang-free one)")
            )
        elif not any(is_filled(e, needs_text) for e in plain):
            problems.append(Problem(path, f"<{tag}> is empty"))
    return problems


def version_key(
    version: str,
) -> tuple[tuple[int, ...], int, tuple[tuple[int, int, str], ...]] | None:
    """Sort key of a version by its numeric parts; None if it has no digits.

    ``1.0`` equals ``1.0.0``; ``1.0~a1`` (a ~ suffix means pre-release) sorts
    before ``1.0``; suffix parts are compared as numbers or, if text, as text.
    """
    base, tilde, suffix = version.partition("~")
    numbers = [int(n) for n in re.findall(r"\d+", base)]
    if not numbers:
        return None
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    tokens = tuple(
        (0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.findall(r"\d+|[A-Za-z]+", suffix)
    )
    return tuple(numbers), 0 if tilde else 1, tokens


def parse_date(text: str) -> date | None:
    """A YYYY-MM-DD date; ``fromisoformat`` alone would also accept 20261125 or 2026-W48-3."""
    if not DATE_FORMAT.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def check_release(path: str, label: str, release: ET.Element, today: date) -> list[Problem]:
    problems = []
    version = release.get("version", "").strip()
    if not version:
        problems.append(Problem(path, f"{label}: missing version"))
    elif version_key(version) is None:
        problems.append(Problem(path, f"{label}: version has no number in it"))
    elif not PLAIN_VERSION.fullmatch(version):
        problems.append(
            Problem(
                path,
                f"{label}: version is not dot-separated numbers with an optional ~suffix, "
                "so it is ordered by its numeric parts only",
                warning=True,
            )
        )
    text = release.get("date", "").strip()
    if not text:
        problems.append(Problem(path, f"{label}: missing date"))
    else:
        when = parse_date(text)
        if when is None:
            problems.append(Problem(path, f"{label}: date '{text}' is not a valid YYYY-MM-DD date"))
        elif when > today + timedelta(days=FUTURE_DAYS):
            problems.append(
                Problem(path, f"{label}: date {text} is more than {FUTURE_DAYS} days in the future")
            )
    return problems


def check_release_order(path: str, releases: Sequence[ET.Element]) -> list[Problem]:
    """Duplicates, and the order newest to oldest by date and by version."""
    problems = []
    seen: set[str] = set()
    previous_version = previous_date = None
    for release in releases:
        version = release.get("version", "").strip()
        when = parse_date(release.get("date", "").strip())
        if version in seen:
            problems.append(Problem(path, f"release {version}: version listed more than once"))
            continue
        if version:
            seen.add(version)
        key = version_key(version)
        if key is not None:
            if previous_version is not None and key >= previous_version[1]:
                problems.append(
                    Problem(
                        path,
                        f"release {version}: not older than {previous_version[0]} above it "
                        "(releases go from the newest to the oldest version)",
                    )
                )
            previous_version = (version, key)
        if when is not None:
            if previous_date is not None and when > previous_date[1]:
                problems.append(
                    Problem(
                        path,
                        f"release {version or '?'}: date {when} is newer than {previous_date[1]} "
                        "of the release above it (releases go from the newest to the oldest date)",
                    )
                )
            previous_date = (version, when)
    return problems


def check_releases(path: str, root: ET.Element, today: date) -> list[Problem]:
    releases = [r for block in root.findall("releases") for r in block.findall("release")]
    problems = []
    for number, release in enumerate(releases, start=1):
        label = f"release {release.get('version') or '#' + str(number)}"
        problems += check_release(path, label, release, today)
    return problems + check_release_order(path, releases)


def check_urls(path: str, root: ET.Element) -> list[Problem]:
    problems = []
    for url in root.iter("url"):
        text = (url.text or "").strip()
        if not text.lower().startswith("https://") or len(text) == len("https://"):
            problems.append(
                Problem(
                    path, f"<url type=\"{url.get('type', '')}\"> is not an https:// URL: '{text}'"
                )
            )
    for shot in root.iter("screenshot"):
        for image in shot.iter("image"):
            text = (image.text or "").strip()
            if not text.lower().startswith("https://") or len(text) == len("https://"):
                problems.append(
                    Problem(path, f"screenshot <image> is not an https:// URL: '{text}'")
                )
    return problems


def check_languages(path: str, root: ET.Element) -> list[Problem]:
    problems = []
    reported: set[tuple[str, str]] = set()
    for element in root.iter():
        lang = element.get(XML_LANG)
        if lang is not None and lang not in LANGS and (element.tag, lang) not in reported:
            reported.add((element.tag, lang))
            problems.append(
                Problem(path, f"<{element.tag}> has an unknown language xml:lang='{lang}'")
            )
    sets = {
        tag: {e.get(XML_LANG) for e in root.findall(tag) if e.get(XML_LANG) is not None}
        for tag in ("name", "summary")
    }
    if sets["name"] != sets["summary"]:
        only_name = " ".join(sorted(sets["name"] - sets["summary"])) or "-"
        only_summary = " ".join(sorted(sets["summary"] - sets["name"])) or "-"
        problems.append(
            Problem(
                path,
                "<name> and <summary> are translated into different languages "
                f"(only <name>: {only_name}; only <summary>: {only_summary})",
                warning=True,
            )
        )
    return problems


def check_text(path: str, text: str, today: date | None = None) -> list[Problem]:
    """All problems of one metainfo file; ``path`` gives the expected ``<id>`` in the messages."""
    root, problems = parse_xml(path, text)
    if root is None:
        return problems
    if root.tag != "component":
        return [Problem(path, f"the root element is <{root.tag}>, not <component>")]
    problems += check_root(path, root)
    problems += check_required(path, root)
    problems += check_releases(path, root, today or date.today())
    problems += check_urls(path, root)
    problems += check_languages(path, root)
    return problems


def check_files(root: Path, paths: Sequence[str], today: date | None = None) -> list[Problem]:
    """Check the given files (paths relative to ``root`` or absolute)."""
    problems: list[Problem] = []
    for rel in paths:
        try:
            text = (root / rel).read_bytes().decode("utf-8")
        except OSError as err:
            problems.append(Problem(rel, f"cannot read the file: {err.strerror or err}"))
        except UnicodeDecodeError:
            problems.append(Problem(rel, "the file is not valid UTF-8"))
        else:
            problems += check_text(rel, text, today)
    return problems


def tracked_metainfo_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.metainfo.xml"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if any(a.startswith("-") for a in args):
        print("usage: python3 tools/check_metainfo.py [FILE.metainfo.xml ...]", file=sys.stderr)
        return 0 if args in (["-h"], ["--help"]) else 2
    if args:
        paths = args
    else:
        try:
            paths = tracked_metainfo_files(ROOT)
        except (OSError, subprocess.CalledProcessError) as err:
            print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
            return 2
    if not paths:
        print("no tracked *.metainfo.xml files found", file=sys.stderr)
        return 2
    problems = check_files(Path.cwd() if args else ROOT, paths)
    for problem in problems:
        print(problem, file=sys.stderr)
    errors = sum(not p.warning for p in problems)
    print(
        f"{len(paths)} metainfo files checked: {errors} errors, {len(problems) - errors} warnings"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
