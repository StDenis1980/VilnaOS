#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the site translation catalogues in ``site/src/i18n/*.json``.

The catalogues are compared with the English file in the same folder (``en.json``).
Identical keys, duplicate keys, encoding details and ``{placeholders}`` in the
plain text are not checked here (see the data-file check). Built HTML in
``site/public`` is not checked here either (see ``tools/check_site.py``).

Every string that exists in both English and another language is checked:

* If the English text contains Vilna, Gloxi, Blavet or Tinta, the translation
  keeps the stem (Viln, Glox, Blavet, Tint). Languages may decline the names.
* The same numbers appear in both texts. A number is a run of digits, so
  typography may differ: ``2.5`` and ``2,5``, or ``Art. 6(1)(a)`` and
  ``art. 6.1.a``, compare equal. The lists are compared as multisets.
* The same links appear in both texts: every ``href`` and every bare
  ``http:``, ``https:`` or ``mailto:`` URL (after ``&amp;`` is unescaped).

In a non-English file, a run of more than four consecutive words from a list
of frequent English words is a warning (a heuristic; code, URLs and
``{{placeholders}}`` are ignored).

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but
do not fail the check; they mark leftover English and anything in ``LEGACY``.
Only ``git ls-files`` is run. Nothing else is executed, nothing is written.

Run: python3 tools/check_site_i18n.py [FILE ...]
FILE is a path from the repo root; without it every tracked
``site/src/i18n/*.json`` is checked. Exit code: 0 all good (warnings allowed),
1 problems found, 2 wrong call.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from html import unescape
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

REFERENCE = "en"
I18N_GLOB = "site/src/i18n/*.json"

# Official names as they appear in English, and the stem a translation must
# keep (languages decline the names: Vilna/Vilny, Tinta/Tintu, Gloxi/Gloxiho).
NAME_STEMS = (
    ("Vilna", "Viln"),
    ("Gloxi", "Glox"),
    ("Blavet", "Blavet"),
    ("Tinta", "Tint"),
)

# Known old (path, rule) pairs become warnings, so the check can run in CI.
# Fix the file, then delete the pair from here. Today the catalogues on main
# have no name, number or link mismatches.
LEGACY: frozenset[tuple[str, str]] = frozenset()

# Frequent English words used to spot leftover English in a translation.
# One-letter words are omitted: they belong to many languages and would break
# or inflate a run without adding meaning. The list is the common function and
# content words (roughly the head of a general-service list), not technical
# proper nouns (Linux, GNOME, Flatpak, …). A run of more than four of these
# words is only a warning: the heuristic can be wrong.
ENGLISH_WORDS = frozenset(
    """
    about above after again against all almost already also always am among
    an and another any anyone anything around as asked at away back be became
    because become becomes been before begin behind being below between both
    but by came can cannot could did do does doing done down during each early
    end enough even ever every everyone everything few first for found from
    further get gets getting give given gives go goes going gone good got
    great had has have having he her here hers herself him himself his how
    however if into is it its itself just keep kept know known knows last
    later least less let like likely little long look looked looking looks
    made make makes making many may me might more most much must my myself
    need needed needs never new no nor not nothing now of off often on once
    only onto or other others our ours ourselves out over own part parts
    people place places put same see seemed seems seen several she should
    show showed shown since so some someone something still such take taken
    takes taking than that the their them themselves then there these they
    thing things this those though through time times to too under until up
    upon us use used uses using very want wanted wants was way ways we well
    went were what when where whether which while who whom whose why will
    with within without would yes yet you your yours yourself
    page site website click here read more learn please thanks thank contact
    privacy policy security feedback report reports message messages email
    form data personal collect collected information home download install
    installed installation remove uninstall start run running follow work
    works working planned development preview testers tester test testing
    version versions free software code public repository under license
    update updated last kept server days deleted delete short collects
    nothing choose send sent them stored store addresses address host
    interest legitimate running consent withdraw affect comes until build
    source latest release menu first asks downloads model checks checksum
    coming soon built based open assistant runs own computer acts only
    permission early ordinary weaker protection system service give folders
    would let person see processor graphics card optional memory better
    space cloud key compatible details masked leave record honestly
    """.split()  # noqa: SIM905
)

TAG = re.compile(r"<[^>]*>")
CODE = re.compile(r"<code\b[^>]*>.*?</code>", re.IGNORECASE | re.DOTALL)
PLACEHOLDER = re.compile(r"\{\{[^{}]+\}\}")
WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
DIGITS = re.compile(r"\d+")
HREF = re.compile(r"""\bhref\s*=\s*(?P<q>['"])(?P<url>.*?)(?P=q)""", re.IGNORECASE)
BARE_LINK = re.compile(r"""(?:https?://|mailto:)[^\s<>\"']+""", re.IGNORECASE)

# Longer than four words: a run of five or more.
ENGLISH_RUN = 4


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False
    rule: str = ""

    def __str__(self) -> str:
        kind = "warning: " if self.warning else ""
        return f"{self.path}: {kind}{self.message}"


def error(path: str, message: str, rule: str) -> Problem:
    return Problem(path, message, False, rule)


def warn(path: str, message: str, rule: str) -> Problem:
    return Problem(path, message, True, rule)


def language_of(path: str) -> str:
    name = Path(path).name
    return name[:-5] if name.endswith(".json") else name


def walk_strings(obj: object, prefix: str = "") -> list[tuple[str, str]]:
    """``(key path, text)`` for every string in a JSON value."""
    if isinstance(obj, str):
        return [(prefix or "(root)", obj)]
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            out.extend(walk_strings(value, child))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            child = f"{prefix}[{index}]" if prefix else f"[{index}]"
            out.extend(walk_strings(value, child))
    return out


def numbers(text: str) -> list[str]:
    """Digit runs in ``text``, in the order they appear."""
    return DIGITS.findall(text)


def format_numbers(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "(none)"


def normalize_link(url: str) -> str:
    return unescape(url).strip()


def links(text: str) -> list[str]:
    """``href`` values and bare URLs, in a stable order, as a multiset source."""
    found: list[str] = []
    seen_spans: list[tuple[int, int]] = []
    for match in HREF.finditer(text):
        found.append(normalize_link(match.group("url")))
        seen_spans.append(match.span("url"))
    for match in BARE_LINK.finditer(text):
        start, end = match.span()
        if any(start >= left and end <= right for left, right in seen_spans):
            continue
        found.append(normalize_link(match.group(0)))
    return found


def format_links(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "(none)"


def visible_words(text: str) -> list[str]:
    """Words left after code, URLs, placeholders and markup are taken out."""
    cleaned = CODE.sub(" ", text)
    cleaned = PLACEHOLDER.sub(" ", cleaned)
    cleaned = BARE_LINK.sub(" ", cleaned)
    cleaned = TAG.sub(" ", cleaned)
    return WORD.findall(cleaned)


def english_runs(text: str) -> list[list[str]]:
    """Runs of more than ``ENGLISH_RUN`` consecutive frequent English words."""
    words = visible_words(text)
    runs: list[list[str]] = []
    index = 0
    while index < len(words):
        if words[index].casefold() in ENGLISH_WORDS:
            end = index + 1
            while end < len(words) and words[end].casefold() in ENGLISH_WORDS:
                end += 1
            if end - index > ENGLISH_RUN:
                runs.append(words[index:end])
            index = end
        else:
            index += 1
    return runs


def check_names(path: str, key: str, source: str, translation: str) -> list[Problem]:
    folded = translation.casefold()
    return [
        error(path, f"{key}: English has {name}, translation has no {stem}…", "names")
        for name, stem in NAME_STEMS
        if name in source and stem.casefold() not in folded
    ]


def check_numbers(path: str, key: str, source: str, translation: str) -> list[Problem]:
    left = numbers(source)
    right = numbers(translation)
    if Counter(left) == Counter(right):
        return []
    return [
        error(
            path,
            f"{key}: numbers differ from English ({format_numbers(left)} vs "
            f"{format_numbers(right)})",
            "numbers",
        )
    ]


def check_links(path: str, key: str, source: str, translation: str) -> list[Problem]:
    left = links(source)
    right = links(translation)
    if Counter(left) == Counter(right):
        return []
    return [
        error(
            path,
            f"{key}: links differ from English ({format_links(left)} vs {format_links(right)})",
            "links",
        )
    ]


def check_english(path: str, key: str, text: str) -> list[Problem]:
    return [
        warn(
            path,
            f'{key}: English text longer than 4 words: "{" ".join(run)}"',
            "english",
        )
        for run in english_runs(text)
    ]


def load_catalog(path: str, data: bytes) -> tuple[object | None, list[Problem]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, [error(path, "the file is not valid UTF-8", "encoding")]
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        return json.loads(text), []
    except json.JSONDecodeError as err:
        return None, [error(path, f"the file is not valid JSON: {err.msg}", "json")]


def check_translation(
    path: str,
    catalog: object,
    reference: dict[str, str] | None,
    *,
    is_reference: bool,
) -> list[Problem]:
    problems: list[Problem] = []
    strings = walk_strings(catalog)
    if not is_reference:
        for key, text in strings:
            problems += check_english(path, key, text)
    if reference is None or is_reference:
        return problems
    for key, text in strings:
        source = reference.get(key)
        if source is None:
            continue
        problems += check_names(path, key, source, text)
        problems += check_numbers(path, key, source, text)
        problems += check_links(path, key, source, text)
    return problems


def downgrade(problem: Problem) -> Problem:
    """Turn a known old violation (see ``LEGACY``) into a warning."""
    if (problem.path, problem.rule) in LEGACY:
        return Problem(problem.path, problem.message, warning=True, rule=problem.rule)
    return problem


def check_files(root: Path, paths: Sequence[str]) -> list[Problem]:
    """Check the catalogues ``paths`` (from ``root``); old violations become warnings."""
    loaded: dict[str, object | None] = {}
    problems: list[Problem] = []
    for rel in paths:
        try:
            data = (root / rel).read_bytes()
        except OSError as err:
            problems.append(error(rel, f"cannot read the file: {err.strerror or err}", "read"))
            loaded[rel] = None
            continue
        catalog, load_problems = load_catalog(rel, data)
        problems += load_problems
        loaded[rel] = catalog

    by_dir: dict[str, list[str]] = {}
    for rel in paths:
        parent = str(Path(rel).parent).replace("\\", "/")
        by_dir.setdefault(parent, []).append(rel)

    for folder, group in by_dir.items():
        ref_path = next((p for p in group if language_of(p) == REFERENCE), None)
        if ref_path is None:
            if folder not in (".", ""):
                candidate = f"{folder}/{REFERENCE}.json"
            else:
                candidate = f"{REFERENCE}.json"
            candidate_file = root / candidate
            ref_strings: dict[str, str] | None = None
            if candidate_file.is_file():
                catalog, load_problems = load_catalog(candidate, candidate_file.read_bytes())
                problems += load_problems
                if catalog is not None:
                    ref_strings = dict(walk_strings(catalog))
            else:
                for rel in group:
                    if loaded.get(rel) is not None and language_of(rel) != REFERENCE:
                        problems.append(
                            error(rel, f"no English reference ({REFERENCE}.json)", "reference")
                        )
        else:
            catalog = loaded.get(ref_path)
            ref_strings = dict(walk_strings(catalog)) if catalog is not None else None

        for rel in group:
            catalog = loaded.get(rel)
            if catalog is None:
                continue
            problems += check_translation(
                rel,
                catalog,
                ref_strings,
                is_reference=language_of(rel) == REFERENCE,
            )

    return [downgrade(p) for p in problems]


def tracked_catalogs(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", I18N_GLOB],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    usage = "usage: python3 tools/check_site_i18n.py [FILE ...]"
    options = [a for a in args if a.startswith("-")]
    if any(o not in ("-h", "--help") for o in options):
        print(usage, file=sys.stderr)
        return 2
    if options:
        print(usage, file=sys.stderr)
        return 0
    try:
        tracked = tracked_catalogs(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    only = [a.replace("\\", "/") for a in args]
    unknown = [a for a in only if a not in tracked]
    if unknown:
        print(f"not a tracked site i18n file: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if not tracked:
        print("no tracked site i18n files found", file=sys.stderr)
        return 2
    problems = check_files(ROOT, only or tracked)
    for problem in problems:
        print(problem, file=sys.stderr)
    errors = sum(not p.warning for p in problems)
    print(
        f"{len(only or tracked)} catalogues checked: "
        f"{errors} errors, {len(problems) - errors} warnings"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
