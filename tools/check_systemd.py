#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the tracked systemd units (``*.service``, ``*.socket``, ``*.timer``, ``*.path``).

For every unit the checker verifies:

* syntax: every line is a comment (``#`` or ``;``), an empty line, a ``[Group]`` or
  ``Key=Value``; a line ending in ``\\`` continues on the next line; the file has no
  byte order mark; groups are ``[Unit]``, ``[Install]`` and the group of the unit type
  (``[Service]``, ``[Socket]``, ``[Timer]``, ``[Path]``; ``X-...`` groups are ignored by
  systemd and allowed); a group that does not belong to this unit type is an error and
  so is a missing group of the type;
* ``[Unit]`` has a non-empty ``Description``;
* ``ExecStart``, ``ExecStartPre``, ``ExecStartPost``, ``ExecStop``, ``ExecStopPost``,
  ``ExecReload`` and ``ExecCondition`` start with an absolute path; the prefixes
  ``-``, ``@``, ``+``, ``!``, ``!!`` and ``:`` of systemd.exec(5) may come before it (with
  no space after them: systemd ignores ``- /bin/x``), and a path specifier (``%h``, ``%t``,
  ``%S``, ``%C``, ``%L``, ``%E``) may stand for it; an empty value (it resets the list) is
  fine;
* a template (``name@.service``) uses ``%i`` or ``%I`` at least once; a unit that is not
  an instance or a template does not use them (``%%i`` is a literal text);
* a ``.socket``, ``.timer`` or ``.path`` without ``Service=`` / ``Unit=`` has the
  service of the same name in the same folder (``name@.socket`` -> ``name@.service``); a
  given ``Service=`` / ``Unit=`` names a unit that is tracked in the repo;
* ``WantedBy=`` and ``RequiredBy=`` in ``[Install]`` name a ``.target`` or a unit of the
  repo;
* a ``.service`` that has none of ``NoNewPrivileges=yes``, ``ProtectSystem=``,
  ``ProtectHome=``, ``PrivateTmp=yes`` gets a warning (some services cannot use them).

``--table`` also prints, for every service, which of these four keys it has.

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but do not
fail the check; they mark things the repo already does today (see ``LEGACY``).
Only ``git ls-files`` is run. Nothing else is executed, nothing is written.

Run: python3 tools/check_systemd.py [--table] [UNIT ...]
UNIT is a path from the repo root; without it every tracked unit is checked (the
partner units are always looked up among all tracked units).
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import posixpath
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

TYPE_GROUP = {".service": "Service", ".socket": "Socket", ".timer": "Timer", ".path": "Path"}
COMMON_GROUPS = ("Unit", "Install")
EXEC_KEYS = frozenset(
    {
        "ExecStart",
        "ExecStartPre",
        "ExecStartPost",
        "ExecStop",
        "ExecStopPost",
        "ExecReload",
        "ExecCondition",
    }
)
EXEC_PREFIXES = "-@+!:"
PATH_SPECIFIERS = ("%h", "%t", "%S", "%C", "%L", "%E")  # each expands to an absolute path
# The key that names the unit to start, per unit type (systemd.socket(5): Service=).
PARTNER_KEY = {".socket": "Service", ".timer": "Unit", ".path": "Unit"}
INSTALL_KEYS = ("WantedBy", "RequiredBy")
# The hardening keys of the warning and the table; the first three need a true value.
HARDENING_KEYS = ("NoNewPrivileges", "ProtectSystem", "ProtectHome", "PrivateTmp")
BOOLEAN_KEYS = ("NoNewPrivileges", "PrivateTmp")
TRUE_VALUES = ("yes", "true", "1", "on")
OFF_VALUES = ("", "no", "false", "0", "off")

# Problems the repo already has today: (path from the repo root, rule). They are
# warnings, so that the check can be switched on in CI; fix the unit, then delete
# the line.
LEGACY: frozenset[tuple[str, str]] = frozenset(
    {
        # Installed into transactional-update.service, a unit of openSUSE, not of this repo.
        ("obs/packages/vilna-branding/x86_64_v3-transactional-update.service", "install-target"),
    }
)

GROUP_LINE = re.compile(r"\[([^\[\]\x00-\x1f]+)\]")
KEY_LINE = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)\s*=(.*)", re.S)
SPECIFIER = re.compile(r"%(.)", re.S)


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
class Entry:
    line: int
    key: str
    value: str


@dataclass
class Group:
    name: str
    line: int
    entries: list[Entry] = field(default_factory=list)

    def values(self, key: str) -> list[str]:
        return [e.value for e in self.entries if e.key == key]


# --- reading a unit ----------------------------------------------------------


def logical_lines(text: str) -> tuple[list[tuple[int, str]], int | None]:
    """The lines of the text with every ``\\`` continuation joined (by one space).

    Returns the (first line number, text) pairs and the number of the line of an
    unfinished continuation at the end of the file, if there is one.
    """
    lines: list[tuple[int, str]] = []
    pending: tuple[int, str] | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        if pending is None:
            if not raw.strip() or raw.lstrip()[0] in "#;":
                continue
            start, current = number, raw
        else:
            start, current = pending
            if raw.lstrip().startswith(("#", ";")):
                continue  # a comment in the middle of a continuation is dropped
            if not raw.strip():
                lines.append((start, current))
                pending = None
                continue
            current = current[:-1].rstrip() + " " + raw.strip()
            raw = current
        if raw.endswith("\\"):
            pending = (start, current)
        else:
            lines.append((start, current.strip()))
            pending = None
    if pending is not None:
        return lines, pending[0]
    return lines, None


def parse(path: str, text: str) -> tuple[list[Group], list[Problem]]:
    """Split a unit into groups; report lines that are not valid syntax."""
    groups: list[Group] = []
    problems: list[Problem] = []
    lines, unfinished = logical_lines(text)
    for number, line in lines:
        header = GROUP_LINE.fullmatch(line)
        pair = KEY_LINE.fullmatch(line)
        if line.startswith("["):
            if header is None:
                problems.append(Problem(path, f"line {number}: invalid group header '{line}'"))
            else:
                groups.append(Group(header.group(1), number))
        elif pair is None:
            problems.append(
                Problem(path, f"line {number}: not a comment, empty line, [Group] or Key=Value")
            )
        elif not groups:
            problems.append(Problem(path, f"line {number}: key before the first group"))
        else:
            groups[-1].entries.append(Entry(number, pair.group(1), pair.group(2).strip()))
    if unfinished is not None:
        problems.append(
            Problem(path, f"line {unfinished}: the file ends after a '\\' continuation")
        )
    return groups, problems


def suffix_of(path: str) -> str:
    return posixpath.splitext(path)[1]


def is_template(name: str) -> bool:
    return re.search(r"@\.[a-z]+$", name) is not None


def is_instance(name: str) -> bool:
    return not is_template(name) and re.search(r"@[^.]+\.[a-z]+$", name) is not None


def unit_name_variants(name: str) -> list[str]:
    """The name itself and, for an instance ``a@b.service``, its template ``a@.service``."""
    match = re.fullmatch(r"(.+@)[^.@]*(\.[a-z]+)", name)
    return [name, match.group(1) + match.group(2)] if match else [name]


# --- rules of one unit -------------------------------------------------------


def check_groups(path: str, groups: list[Group]) -> list[Problem]:
    suffix = suffix_of(path)
    wanted = TYPE_GROUP[suffix]
    problems = []
    for group in groups:
        if group.name in (*COMMON_GROUPS, wanted) or group.name.startswith("X-"):
            continue
        problems.append(
            Problem(
                path, f"line {group.line}: group [{group.name}] is not valid in a {suffix} unit"
            )
        )
    if not any(g.name == wanted for g in groups):
        problems.append(Problem(path, f"missing the [{wanted}] group"))
    return problems


def check_description(path: str, groups: list[Group]) -> list[Problem]:
    for group in groups:
        if group.name == "Unit" and any(group.values("Description")):
            return []
    return [Problem(path, "[Unit] needs a Description")]


def command_path(value: str) -> str:
    """The command of an Exec value without the prefixes ``-@+!:`` and a leading quote."""
    rest = value.lstrip()
    while rest and rest[0] in EXEC_PREFIXES:
        rest = rest[1:]
    return rest[1:] if rest[:1] in ('"', "'") else rest


def check_exec(path: str, groups: list[Group]) -> list[Problem]:
    problems = []
    for group in groups:
        for entry in group.entries:
            if entry.key not in EXEC_KEYS or not entry.value:
                continue  # an empty value resets the list
            command = command_path(entry.value)
            if command[:1].isspace():
                problems.append(
                    Problem(path, f"line {entry.line}: {entry.key} has a space after its prefix")
                )
            elif not command.startswith(("/", *PATH_SPECIFIERS)):
                shown = command.split()[0] if command.split() else entry.value
                problems.append(
                    Problem(
                        path,
                        f"line {entry.line}: {entry.key} must start with an absolute path "
                        f"(after the prefixes {' '.join(EXEC_PREFIXES)}), found '{shown}'",
                    )
                )
    return problems


def instance_specifiers(groups: list[Group]) -> list[int]:
    """The line numbers of the values that use ``%i`` or ``%I`` (``%%`` is skipped)."""
    lines = []
    for group in groups:
        for entry in group.entries:
            if any(m.group(1) in "iI" for m in SPECIFIER.finditer(entry.value)):
                lines.append(entry.line)
    return lines


def check_specifiers(path: str, groups: list[Group]) -> list[Problem]:
    name = posixpath.basename(path)
    used = instance_specifiers(groups)
    if is_template(name) and not used:
        return [Problem(path, "a template unit must use %i or %I at least once")]
    if not is_template(name) and not is_instance(name):
        return [
            Problem(path, f"line {line}: %i or %I in a unit that is not a template")
            for line in used
        ]
    return []


def check_install(path: str, groups: list[Group], names: set[str]) -> list[Problem]:
    problems = []
    for group in groups:
        if group.name != "Install":
            continue
        for entry in group.entries:
            if entry.key not in INSTALL_KEYS:
                continue
            for target in entry.value.split():
                if target.endswith(".target") or exists_in_repo(target, names):
                    continue
                problems.append(
                    Problem(
                        path,
                        f"line {entry.line}: {entry.key}={target} is neither a .target nor a unit "
                        "of the repo",
                        rule="install-target",
                    )
                )
    return problems


def exists_in_repo(name: str, names: set[str]) -> bool:
    """Is the unit (or the template of an instance, ``%i`` counting as an instance) tracked?"""
    name = re.sub(r"@[^.@]*\.", "@.", name) if "@" in name else name
    return any(variant in names for variant in unit_name_variants(name))


def check_partner(
    path: str, groups: list[Group], names: set[str], folders: dict[str, set[str]]
) -> list[Problem]:
    suffix = suffix_of(path)
    key = PARTNER_KEY.get(suffix)
    if key is None:
        return []
    given = [(e.line, e.value) for g in groups for e in g.entries if e.key == key and e.value]
    problems = []
    for line, value in given:
        if not exists_in_repo(value, names):
            problems.append(Problem(path, f"line {line}: {key}={value} is not a unit of the repo"))
    if given:
        return problems
    name = posixpath.basename(path)
    beside = folders.get(posixpath.dirname(path), set())
    own = [v.removesuffix(suffix) + ".service" for v in unit_name_variants(name)]
    if not any(candidate in beside for candidate in own):
        return [Problem(path, f"no {own[0]} next to it (and no {key}= names another unit)")]
    return []


def true_value(key: str, values: list[str]) -> str | None:
    """The effective value of a hardening key (the last one wins), or None if it is off."""
    value = values[-1].lower() if values else ""
    if key in BOOLEAN_KEYS:
        return values[-1] if value in TRUE_VALUES else None
    return values[-1] if value not in OFF_VALUES else None


def hardening(groups: list[Group]) -> dict[str, str | None]:
    """For each hardening key the value that switches it on, or None."""
    result: dict[str, str | None] = {}
    for key in HARDENING_KEYS:
        values = [v for g in groups if g.name == "Service" for v in g.values(key)]
        result[key] = true_value(key, values)
    return result


def check_hardening(path: str, groups: list[Group]) -> list[Problem]:
    if suffix_of(path) != ".service" or any(hardening(groups).values()):
        return []
    keys = "NoNewPrivileges=yes, ProtectSystem=, ProtectHome=, PrivateTmp=yes"
    return [Problem(path, f"none of {keys}", warning=True, rule="hardening")]


# --- checking the repo -------------------------------------------------------


def check_unit(
    path: str, text: str, names: set[str], folders: dict[str, set[str]]
) -> list[Problem]:
    problems: list[Problem] = []
    if text.startswith("\ufeff"):
        problems.append(Problem(path, "the file starts with a byte order mark (BOM)"))
        text = text[1:]
    groups, syntax = parse(path, text)
    problems += syntax
    problems += check_groups(path, groups)
    problems += check_description(path, groups)
    problems += check_exec(path, groups)
    problems += check_specifiers(path, groups)
    problems += check_partner(path, groups, names, folders)
    problems += check_install(path, groups, names)
    problems += check_hardening(path, groups)
    return [downgrade(p) for p in problems]


def downgrade(problem: Problem) -> Problem:
    """Turn a known old violation (see LEGACY) into a warning."""
    if (problem.path, problem.rule) in LEGACY:
        return Problem(problem.path, problem.message, warning=True, rule=problem.rule)
    return problem


def index(paths: Sequence[str]) -> tuple[set[str], dict[str, set[str]]]:
    """The names of all units, and the names per folder."""
    names = {posixpath.basename(p) for p in paths}
    folders: dict[str, set[str]] = {}
    for p in paths:
        folders.setdefault(posixpath.dirname(p), set()).add(posixpath.basename(p))
    return names, folders


def read_units(root: Path, paths: Sequence[str]) -> tuple[dict[str, str], list[Problem]]:
    texts: dict[str, str] = {}
    problems: list[Problem] = []
    for rel in paths:
        try:
            texts[rel] = (root / rel).read_bytes().decode("utf-8")
        except OSError as err:
            problems.append(Problem(rel, f"cannot read the file: {err.strerror or err}"))
        except UnicodeDecodeError:
            problems.append(Problem(rel, "the file is not valid UTF-8"))
    return texts, problems


def check_files(root: Path, tracked: Sequence[str], only: Sequence[str] = ()) -> list[Problem]:
    """Check the units among ``tracked`` (paths from ``root``); ``only`` limits which ones.

    The partner units and targets are always looked up among all ``tracked`` units.
    """
    units = [p for p in tracked if suffix_of(p) in TYPE_GROUP]
    names, folders = index(units)
    texts, problems = read_units(root, only or units)
    for rel, text in sorted(texts.items()):
        problems += check_unit(rel, text, names, folders)
    return problems


def hardening_table(root: Path, tracked: Sequence[str]) -> str:
    """A Markdown table: which hardening keys each tracked service has."""
    header = ["Unit", *HARDENING_KEYS]
    rows = [header, ["---"] * len(header)]
    services = sorted(p for p in tracked if suffix_of(p) == ".service")
    texts, _ = read_units(root, services)
    for rel in services:
        groups, _ = parse(rel, texts.get(rel, "").removeprefix("\ufeff"))
        found = hardening(groups)
        rows.append([f"`{rel}`", *[f"`{found[k]}`" if found[k] else "—" for k in HARDENING_KEYS]])
    return "\n".join("| " + " | ".join(row) + " |" for row in rows)


def tracked_units(root: Path) -> list[str]:
    patterns = [f"*{suffix}" for suffix in TYPE_GROUP]
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *patterns],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    usage = "usage: python3 tools/check_systemd.py [--table] [UNIT ...]"
    options = [a for a in args if a.startswith("-")]
    if any(o not in ("--table", "-h", "--help") for o in options):
        print(usage, file=sys.stderr)
        return 2
    if "-h" in options or "--help" in options:
        print(usage, file=sys.stderr)
        return 0
    try:
        tracked = tracked_units(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    only = [a.replace("\\", "/") for a in args if not a.startswith("-")]
    unknown = [a for a in only if a not in tracked]
    if unknown:
        print(f"not a tracked systemd unit: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if not tracked:
        print("no tracked systemd units found", file=sys.stderr)
        return 2
    problems = check_files(ROOT, tracked, only)
    for problem in problems:
        print(problem, file=sys.stderr)
    if "--table" in options:
        print(hardening_table(ROOT, tracked))
    errors = sum(not p.warning for p in problems)
    print(
        f"{len(only or tracked)} systemd units checked: "
        f"{errors} errors, {len(problems) - errors} warnings"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
