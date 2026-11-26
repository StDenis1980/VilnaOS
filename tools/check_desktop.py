#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the tracked ``*.desktop`` files (freedesktop Desktop Entry) of the repo.

For every file the checker verifies:

* the first group is ``[Desktop Entry]``; every line is a comment, an empty
  line, a ``[Group]`` or a ``Key=Value``; no key is repeated inside a group;
* ``Type`` is set and is one of Application, Link, Directory; an Application
  also has ``Name`` and ``Exec``;
* in files named ``io.github.stdenis1980.<Name>.desktop`` the ``Icon``, if set,
  is that app id or a known icon theme name (no path, no file extension);
* list values (Categories, Keywords, MimeType, ...) end with ``;``;
* translations: a ``Name`` or ``Comment`` that has any ``[lang]`` variant has
  all 14 translated languages (uk es pl de fr it ro cs lt ca ca@valencia gl eu
  pt; English is the key without a suffix); unknown language suffixes are
  errors; an empty translation is an error and a translation equal to the
  English text is only a warning.

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but
do not fail the check; they mark things the repo already does today.

Only ``git ls-files`` is run (to find the files). Nothing else is executed and
nothing is written.

Run: python3 tools/check_desktop.py [FILE.desktop ...]
Without arguments every tracked ``*.desktop`` file is checked.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

ENTRY_GROUP = "Desktop Entry"
TYPES = ("Application", "Link", "Directory")
# The 14 translated languages; English is the key without a suffix.
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
TRANSLATED_KEYS = ("Name", "Comment")  # must be complete once any [lang] exists
# Keys whose value is a list of strings separated (and ended) by ";".
LIST_KEYS = (
    "Categories",
    "Keywords",
    "MimeType",
    "OnlyShowIn",
    "NotShowIn",
    "Actions",
    "Implements",
)
# Icon theme names (not the app id) the desktop files may use: the Vilna icons in
# docs/design/icons and one stock symbolic icon of the GNOME theme.
KNOWN_THEME_ICONS = frozenset(
    {
        "vilna-backup",
        "vilna-backup-symbolic",
        "vilna-installer",
        "vilna-installer-symbolic",
        "vilna-permissions",
        "vilna-permissions-symbolic",
        "mail-send-symbolic",
    }
)
ICON_FILE_SUFFIXES = (".png", ".svg", ".svgz", ".xpm", ".ico")

# Violations the repo already has today. They are reported as warnings, not errors,
# so that the check can be switched on in CI; fix the file, then delete the line.
# Each entry is (path from the repo root, rule).
LEGACY = frozenset(
    {
        # Only Comment[uk] is translated (the app is a third-party package).
        (
            "thirdparty/claude-desktop/io.github.stdenis1980.ClaudeDesktop.desktop",
            "incomplete-translation",
        ),
    }
)

APP_ID_FILE = re.compile(r"(io\.github\.stdenis1980\.[A-Za-z0-9_-]+)\.desktop")
GROUP_LINE = re.compile(r"\[([^\[\]\x00-\x1f\x7f]+)\]")
KEY_NAME = re.compile(r"([A-Za-z0-9-]+)(?:\[([^\[\]\s]+)\])?")


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False
    rule: str = ""

    def __str__(self) -> str:
        kind = "warning: " if self.warning else ""
        return f"{self.path}: {kind}{self.message}"


@dataclass
class Group:
    name: str
    line: int
    # (line number, key, language or None, value), in file order.
    entries: list[tuple[int, str, str | None, str]] = field(default_factory=list)

    def get(self, key: str, lang: str | None = None) -> str | None:
        for _, k, lg, value in self.entries:
            if k == key and lg == lang:
                return value
        return None

    def languages(self, key: str) -> set[str]:
        return {lg for _, k, lg, _ in self.entries if k == key and lg is not None}


def parse(path: str, text: str) -> tuple[list[Group], list[Problem]]:
    """Split a desktop file into groups; report lines that are not valid syntax."""
    groups: list[Group] = []
    problems: list[Problem] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            match = GROUP_LINE.fullmatch(line)
            if match is None:
                problems.append(Problem(path, f"line {number}: invalid group header '{line}'"))
            elif any(g.name == match.group(1) for g in groups):
                problems.append(Problem(path, f"line {number}: group [{match.group(1)}] repeated"))
            else:
                groups.append(Group(match.group(1), number))
            continue
        key_text, sep, value = line.partition("=")
        key_match = KEY_NAME.fullmatch(key_text.strip())
        if not sep or key_match is None:
            problems.append(
                Problem(path, f"line {number}: not a comment, empty line, [Group] or Key=Value")
            )
        elif not groups:
            problems.append(Problem(path, f"line {number}: key before the first group"))
        else:
            groups[-1].entries.append(
                (number, key_match.group(1), key_match.group(2), value.strip())
            )
    return groups, problems


def check_structure(path: str, groups: list[Group]) -> list[Problem]:
    problems = []
    if not groups or groups[0].name != ENTRY_GROUP:
        problems.append(Problem(path, f"the first group must be [{ENTRY_GROUP}]"))
    for group in groups:
        seen: set[tuple[str, str | None]] = set()
        for number, key, lang, _ in group.entries:
            if (key, lang) in seen:
                shown = key if lang is None else f"{key}[{lang}]"
                problems.append(
                    Problem(path, f"line {number}: duplicate key {shown} in [{group.name}]")
                )
            seen.add((key, lang))
    return problems


def check_type(path: str, entry: Group) -> list[Problem]:
    kind = entry.get("Type")
    if not kind:
        return [Problem(path, "missing Type")]
    if kind not in TYPES:
        return [Problem(path, f"Type={kind} is not one of {', '.join(TYPES)}")]
    if kind != "Application":
        return []
    return [
        Problem(path, f"Type=Application needs {key}")
        for key in ("Name", "Exec")
        if not entry.get(key)
    ]


def check_icon(path: str, entry: Group) -> list[Problem]:
    match = APP_ID_FILE.fullmatch(Path(path).name)
    icon = entry.get("Icon")
    if match is None or not icon:
        return []
    if "/" in icon or icon.lower().endswith(ICON_FILE_SUFFIXES):
        return [Problem(path, f"Icon={icon} must be an icon name, without a path or extension")]
    if icon != match.group(1) and icon not in KNOWN_THEME_ICONS:
        return [
            Problem(
                path, f"Icon={icon} is neither the app id {match.group(1)} nor a known icon name"
            )
        ]
    return []


def check_lists(path: str, group: Group) -> list[Problem]:
    problems = []
    for number, key, lang, value in group.entries:
        if key in LIST_KEYS and value and not ends_with_separator(value):
            shown = key if lang is None else f"{key}[{lang}]"
            problems.append(Problem(path, f"line {number}: {shown} must end with ';'"))
    return problems


def ends_with_separator(value: str) -> bool:
    """True if the value ends with an unescaped ";" (a backslash-semicolon is literal)."""
    if not value.endswith(";"):
        return False
    backslashes = len(value) - 1 - len(value[:-1].rstrip("\\"))
    return backslashes % 2 == 0


def check_translations(path: str, group: Group) -> list[Problem]:
    problems = []
    for number, key, lang, value in group.entries:
        if lang is None:
            continue
        shown = f"{key}[{lang}]"
        english = group.get(key)
        if lang not in LANGS:
            problems.append(Problem(path, f"line {number}: unknown language suffix in {shown}"))
        elif not value:
            problems.append(Problem(path, f"line {number}: {shown} is empty"))
        elif english is not None and value == english:
            problems.append(
                Problem(path, f"line {number}: {shown} equals the English text", warning=True)
            )
    for key in TRANSLATED_KEYS:
        have = group.languages(key)
        if not have:
            continue
        if group.get(key) is None:
            problems.append(Problem(path, f"[{group.name}] has {key}[..] but no English {key}"))
        missing = [lang for lang in LANGS if lang not in have]
        if missing:
            message = f"[{group.name}] {key} is not translated into: {' '.join(missing)}"
            problems.append(Problem(path, message, rule="incomplete-translation"))
    return problems


def check_text(path: str, text: str) -> list[Problem]:
    """All problems of one desktop file; ``path`` is only used in the messages."""
    groups, problems = parse(path, text)
    problems += check_structure(path, groups)
    entry = next((g for g in groups if g.name == ENTRY_GROUP), None)
    if entry is not None:
        problems += check_type(path, entry)
        problems += check_icon(path, entry)
    for group in groups:
        problems += check_lists(path, group)
        problems += check_translations(path, group)
    return [downgrade(p) for p in problems]


def downgrade(problem: Problem) -> Problem:
    """Turn a known old violation (see LEGACY) into a warning."""
    if (problem.path, problem.rule) in LEGACY:
        return Problem(problem.path, problem.message, warning=True, rule=problem.rule)
    return problem


def check_files(root: Path, paths: Sequence[str]) -> list[Problem]:
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
            problems += check_text(rel, text)
    return problems


def tracked_desktop_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.desktop"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if any(a.startswith("-") for a in args):
        print("usage: python3 tools/check_desktop.py [FILE.desktop ...]", file=sys.stderr)
        return 0 if args in (["-h"], ["--help"]) else 2
    if args:
        paths = args
    else:
        try:
            paths = tracked_desktop_files(ROOT)
        except (OSError, subprocess.CalledProcessError) as err:
            print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
            return 2
    if not paths:
        print("no tracked *.desktop files found", file=sys.stderr)
        return 2
    problems = check_files(Path.cwd() if args else ROOT, paths)
    for problem in problems:
        print(problem, file=sys.stderr)
    errors = sum(not p.warning for p in problems)
    print(f"{len(paths)} desktop files checked: {errors} errors, {len(problems) - errors} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
