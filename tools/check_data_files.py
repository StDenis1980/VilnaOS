#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the tracked data files of the repo: ``*.json`` and ``*.toml``.

Every tracked ``.json`` file:

* is valid JSON (the standard only: ``NaN`` and ``Infinity`` are errors);
* has no duplicate key in any object, at any depth (a later key would silently win);
* contains no lone surrogate in a string (it cannot be written as UTF-8);
* is UTF-8 without a byte order mark;
* ends with a newline.

Every tracked ``.toml`` file is valid TOML (read with ``tomllib``, which already refuses
duplicate keys) and is UTF-8 without a byte order mark.

A file larger than 16 MiB is an error and is not read further, so a huge or endless file
cannot eat the memory of the machine.

The site strings ``site/src/i18n/<lang>.json`` are compared with ``en.json`` of the same
folder (the build reads English as the default language):

* every file maps names to strings;
* a key that ``en.json`` has and the language lacks is a warning (``build_site.py``
  refuses such a file, so do not leave it);
* a key the language has and ``en.json`` lacks is an error;
* the placeholders ``{{name}}`` (and single ``{name}``) of a string are the same as in
  English, the same number of times; an unmatched ``{`` or ``}`` is an error.

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but do not
fail the check; they mark things the repo already does today (see ``LEGACY``).

Only ``git ls-files`` is run (to find the files). Nothing else is executed, nothing is
read from the network and nothing is written.

Run: python3 tools/check_data_files.py [FILE.json | FILE.toml ...]
Without arguments every tracked ``*.json`` and ``*.toml`` file is checked. With arguments,
the files are read from the current folder.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

MAX_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 512  # the deepest data file of the repo nests about ten levels
SUFFIXES = (".json", ".toml")
BOM = "\ufeff"
I18N_DIR = ("site", "src", "i18n")
REFERENCE = "en.json"
# {{name}} first, so that a double brace is one placeholder; then a single {name}.
PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}|\{[^{}]*\}")

# Violations the repo already has: (path, rule). They are warnings, not errors.
# Fix the file, then delete the line.
LEGACY: frozenset[tuple[str, str]] = frozenset(
    {
        # The design tokens file has no newline at the end.
        ("docs/design/motion/tokens.json", "final-newline"),
    }
)


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False
    rule: str = ""

    def __str__(self) -> str:
        return f"{self.path}: {'warning: ' if self.warning else ''}{self.message}"


def problem(path: str, rule: str, message: str, warning: bool = False) -> Problem:
    """A problem; a known old one (see LEGACY) is only a warning."""
    return Problem(path, message, warning or (path, rule) in LEGACY, rule)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def parse_json(text: str) -> tuple[Any, list[str]]:
    """Parse JSON; return the value and the list of duplicate keys (raises ValueError)."""
    dups: list[str] = []

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        for key, _ in items:
            if key in seen and key not in dups:
                dups.append(key)
            seen.add(key)
        return dict(items)

    value = json.loads(text, object_pairs_hook=pairs, parse_constant=_reject_constant)
    # Whether json runs out of recursion depends on the Python build and the
    # stack size, so the depth is also checked here, without recursion.
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list)):
            if depth > MAX_JSON_DEPTH:
                raise ValueError("nested too deeply")
            items = item.values() if isinstance(item, dict) else item
            stack.extend((child, depth + 1) for child in items)
    return value, dups


def has_lone_surrogate(value: Any) -> bool:
    """True if any string or key inside the value has a lone surrogate (no recursion)."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if any("\ud800" <= ch <= "\udfff" for ch in item):
                return True
        elif isinstance(item, dict):
            for key, val in item.items():
                stack.append(key)
                stack.append(val)
        elif isinstance(item, list):
            stack.extend(item)
    return False


def read_text(root: Path, rel: str) -> tuple[str | None, list[Problem]]:
    """Read one file as UTF-8, with its encoding problems. None means do not go on."""
    try:
        with open(root / rel, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError as err:
        return None, [problem(rel, "read", f"cannot read the file: {err.strerror or err}")]
    if len(data) > MAX_BYTES:
        return None, [problem(rel, "size", f"larger than {MAX_BYTES // 1024 // 1024} MiB")]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as err:
        return None, [problem(rel, "utf8", f"not valid UTF-8 (byte {err.start})")]
    problems: list[Problem] = []
    if text.startswith(BOM):
        problems.append(problem(rel, "bom", "starts with a byte order mark (BOM)"))
        text = text[len(BOM) :]
    if rel.lower().endswith(".json") and not data.endswith(b"\n"):
        problems.append(problem(rel, "final-newline", "no newline at the end of the file"))
    return text, problems


def check_json(rel: str, text: str) -> tuple[bool, Any, list[Problem]]:
    """Return (parsed, value, problems); a file that did not parse has no value."""
    try:
        value, dups = parse_json(text)
    except (ValueError, RecursionError) as err:
        reason = "nested too deeply" if isinstance(err, RecursionError) else str(err)
        return False, None, [problem(rel, "invalid", f"invalid JSON: {reason}")]
    problems = [problem(rel, "duplicate-key", f"duplicate key {key!r}") for key in dups]
    if has_lone_surrogate(value):
        problems.append(problem(rel, "surrogate", "a string has a lone surrogate"))
    return True, value, problems


def check_toml(rel: str, text: str) -> list[Problem]:
    try:
        tomllib.loads(text)
    except (ValueError, RecursionError) as err:
        reason = "nested too deeply" if isinstance(err, RecursionError) else str(err)
        return [problem(rel, "invalid", f"invalid TOML: {reason}")]
    return []


def placeholders(text: str) -> Counter[str]:
    return Counter("".join(found.split()) for found in PLACEHOLDER.findall(text))


def stray_brace(text: str) -> bool:
    return any(ch in "{}" for ch in PLACEHOLDER.sub("", text))


def shown(names: Counter[str]) -> str:
    return " ".join(sorted(names.elements())) or "none"


def is_i18n(rel: str) -> bool:
    parts = PurePosixPath(rel.replace("\\", "/")).parts
    return len(parts) >= 4 and parts[-4:-1] == I18N_DIR and parts[-1].endswith(".json")


def check_i18n(rel: str, strings: Any, reference: dict[str, str], ref_rel: str) -> list[Problem]:
    """Compare one language file (already a valid JSON value) with the English one."""
    if not isinstance(strings, dict) or not all(isinstance(v, str) for v in strings.values()):
        return [problem(rel, "i18n-shape", "must map names to strings")]
    problems: list[Problem] = []
    if rel != ref_rel:
        for key in sorted(reference.keys() - strings.keys()):
            problems.append(
                problem(rel, "i18n-missing", f"key {key!r} is missing (see {REFERENCE})", True)
            )
        for key in sorted(strings.keys() - reference.keys()):
            problems.append(problem(rel, "i18n-extra", f"extra key {key!r} (not in {REFERENCE})"))
    for key in sorted(strings.keys() & reference.keys()):
        if rel != ref_rel:
            want, got = placeholders(reference[key]), placeholders(strings[key])
            if want != got:
                problems.append(
                    problem(
                        rel,
                        "i18n-placeholder",
                        f"key {key!r}: placeholders [{shown(got)}] differ from "
                        f"{REFERENCE} [{shown(want)}]",
                    )
                )
        if stray_brace(strings[key]):
            problems.append(problem(rel, "i18n-brace", f"key {key!r}: unmatched '{{' or '}}'"))
    return problems


def check_files(root: Path, paths: list[str]) -> list[Problem]:
    problems: list[Problem] = []
    loaded: dict[str, Any] = {}
    for rel in paths:
        text, found = read_text(root, rel)
        problems += found
        if text is None:
            continue
        if rel.lower().endswith(".toml"):
            problems += check_toml(rel, text)
        else:
            parsed, value, found = check_json(rel, text)
            problems += found
            if parsed:
                loaded[rel] = value
    problems += compare_languages(root, loaded)
    return sorted(problems, key=lambda p: (p.path, p.message))


def compare_languages(root: Path, loaded: dict[str, Any]) -> list[Problem]:
    groups: dict[str, list[str]] = {}
    for rel in loaded:
        if is_i18n(rel):
            groups.setdefault(str(PurePosixPath(rel.replace("\\", "/")).parent), []).append(rel)
    problems: list[Problem] = []
    for folder, members in sorted(groups.items()):
        ref_rel = f"{folder}/{REFERENCE}"
        names = {rel.replace("\\", "/"): rel for rel in members}
        if ref_rel in names:
            reference = loaded[names[ref_rel]]
        else:
            text, _ = read_text(root, ref_rel)
            if text is None:
                problems.append(
                    problem(members[0], "i18n-reference", f"{REFERENCE} cannot be read")
                )
                continue
            try:
                reference, _ = parse_json(text)
            except (ValueError, RecursionError):
                continue  # the English file is invalid: reported when it is checked itself
        if not isinstance(reference, dict) or not all(
            isinstance(v, str) for v in reference.values()
        ):
            problems += check_i18n(names.get(ref_rel, ref_rel), reference, {}, ref_rel)
            continue
        for rel in sorted(members):
            problems += check_i18n(rel, loaded[rel], reference, names.get(ref_rel, ref_rel))
    return problems


def tracked_data_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p.lower().endswith(SUFFIXES))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    usage = "usage: python3 tools/check_data_files.py [FILE.json | FILE.toml ...]"
    if any(a.startswith("-") for a in args):
        print(usage, file=sys.stderr)
        return 0 if args in (["-h"], ["--help"]) else 2
    if any(not a.lower().endswith(SUFFIXES) for a in args):
        print(f"every file must be a .json or a .toml file\n{usage}", file=sys.stderr)
        return 2
    if args:
        paths = args
    else:
        try:
            paths = tracked_data_files(ROOT)
        except (OSError, subprocess.CalledProcessError) as err:
            print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
            return 2
    if not paths:
        print("no tracked *.json or *.toml files found", file=sys.stderr)
        return 2
    problems = check_files(Path.cwd() if args else ROOT, paths)
    for found in problems:
        print(found, file=sys.stderr)
    errors = sum(not p.warning for p in problems)
    print(f"{len(paths)} data files checked: {errors} errors, {len(problems) - errors} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
