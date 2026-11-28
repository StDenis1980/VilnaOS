#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check what the OBS packages (``obs/packages/<name>/``) need to build and to be traced.

``tools/check_spec.py`` checks the spec itself (preamble, sections, licences, the
format and order of the ``.changes`` entries). This checker looks at what is
around the spec and what OBS does with it; it reads the files as text, runs
nothing and needs no network. For every package folder it verifies:

* sources: every ``SourceN``/``PatchN`` of the spec is a tracked file of the folder,
  a file ``_service`` downloads (``download_url``), the tarball or ``vendor.tar.xz``
  that ``obs/tools/obs_sync.py`` makes, or (a URL) a file ``download_files`` fetches;
  a source that only some other service could make is a warning; a source URL
  that is not https is an error;
* ``_service`` (if the folder has one): well-formed XML without a DOCTYPE, every
  ``download_url`` has https, a host and an absolute path (or ``url``) and a safe
  file name; every download has a ``verify_file`` with a ``sha256`` or ``sha512``
  checksum of the right length that names a file that is downloaded; a download
  that no source uses is a warning;
* the change log: the folder has ``<name>.changes`` with at least one entry, or the
  ``%changelog`` of the spec has entries; every entry has a date that exists
  (``Thu Nov 26 10:30:00 UTC 2026`` in ``.changes``, ``Thu Nov 26 2026`` in the
  spec) and an address ``<user@domain>`` on ``vilnaos.com`` (or a subdomain), on
  ``users.noreply.github.com`` or a ``noreply@`` one; a date more than a day
  ahead of the clock and an entry without text are warnings;
* ``BuildRequires``: the form of every dependency (a name, or a name, an operator
  ``= == < > <= >=`` and a version; ``%{python_module name >= 1}`` and
  ``(a or b)`` are read too; no empty item, no operator stuck to the name, no
  version without operator); the same dependency twice; a package that needs
  what it builds itself; a name of this project (the prefixes of ``OWN_PREFIXES``)
  that no spec of ``obs/packages`` builds, or one that ``image/packages.yaml``
  marks as ``planned``: neither can exist in openSUSE Tumbleweed; a cycle of
  packages that need each other, which OBS cannot build.

The list of Tumbleweed packages is not in the repository, so the checker cannot
know which names Tumbleweed has. When the file ``obs/tumbleweed-packages.txt``
is tracked (one name per line, ``#`` starts a comment; add the virtual
provides such as ``c_compiler`` too), or ``--tumbleweed-list FILE`` is given,
every plain name (not a macro, not ``name(...)``) must be in it or be built by a
spec of the repo. Without a list only the format is checked.

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but do not
fail the check; they mark things the repo already does today (see ``LEGACY``).

Only ``git ls-files`` is run. Nothing is written and no file larger than 5 MiB
is read.

Run: python3 tools/check_obs.py [--tumbleweed-list FILE] [obs/packages/NAME ...]
Without package arguments every folder with tracked files in ``obs/packages`` is checked.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import re
import subprocess
import os
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import check_spec as cs

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

PACKAGES = cs.PACKAGES
PACKAGES_YAML = "image/packages.yaml"
DEFAULT_LIST = "obs/tumbleweed-packages.txt"
MAX_BYTES = 5 * 1024 * 1024
# Package names that only this project can have (the security layer's package is the second).
OWN_PREFIXES = ("vilna-", "herkos", "gloxi")
OPERATORS = frozenset({"=", "==", "<", ">", "<=", ">="})
CHECKSUM_LENGTHS = {"sha256": 64, "sha512": 128}
# Services that do not make a file the spec could name as its Source.
PASSIVE_SERVICES = frozenset({"download_url", "verify_file", "download_files"})
DAY = timedelta(days=1)
NAME = re.compile(r"[A-Za-z0-9_+.\-/]+(\([^()\s]*\))?")
VERSION = re.compile(r"[0-9A-Za-z._+~^:%{}?()\-]+")
EMAIL = re.compile(r"<([^<>@\s]+)@([^<>@\s]+)>")
CHANGES_ENTRY = re.compile(
    r"(?P<date>[A-Z][a-z]{2} [A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d)"
    r" UTC (?P<year>\d{4}) - (?P<rest>.*)"
)
SPEC_ENTRY = re.compile(r"\*\s+(?P<date>[A-Z][a-z]{2} [A-Z][a-z]{2} [ \d]\d \d{4})\s+(?P<rest>.*)")
# Old problems of the repo: (path, rule) pairs reported as warnings.
LEGACY: frozenset[tuple[str, str]] = frozenset()

Problem = cs.Problem


@dataclass(frozen=True)
class Dependency:
    """One dependency of a ``BuildRequires`` line."""

    name: str  # package name; for ``%{python_module x}`` the name ``x``
    operator: str = ""
    version: str = ""
    module: bool = False  # written as %{python_module name}
    opaque: bool = False  # a macro or a rich dependency: not looked at

    @property
    def text(self) -> str:
        return f"{self.name} {self.operator} {self.version}".strip()


@dataclass
class Service:
    """One ``<service>`` of a ``_service`` file."""

    name: str
    params: dict[str, str] = field(default_factory=dict)


@dataclass
class Context:
    """What the checks need from outside one package folder."""

    spec_context: cs.Context
    own: dict[str, str]  # RPM name built by a spec of the repo -> its folder
    planned: frozenset[str]  # names image/packages.yaml marks as planned
    tumbleweed: frozenset[str] | None  # names of the Tumbleweed list, if there is one
    list_path: str = ""
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


# --- reading ---------------------------------------------------------------


def read_file(root: Path, rel: str) -> tuple[str | None, list[Problem]]:
    """The text of a file: a clear problem when it is a link, too big or not UTF-8."""
    path = root / rel
    try:
        if path.is_symlink():
            return None, [Problem(rel, "the file is a symbolic link", rule="layout")]
        if path.stat().st_size > MAX_BYTES:
            return None, [Problem(rel, "the file is larger than 5 MiB", rule="layout")]
    except OSError as err:
        if not path.exists():
            return None, [Problem(rel, "the file does not exist", rule="layout")]
        return None, [Problem(rel, f"cannot read the file: {err.strerror or err}", rule="layout")]
    return cs.read_text(root, rel)


def parse_service(text: str) -> tuple[list[Service], str | None]:
    """The services of a ``_service`` file, or the reason it cannot be read."""
    if re.search(r"<!(DOCTYPE|ENTITY)", text):
        return [], "has a DOCTYPE or ENTITY declaration"
    try:
        root = ET.fromstring(text)
    except ET.ParseError as err:
        return [], f"not well-formed XML: {err}"
    services = []
    for element in root.iter("service"):
        params = {p.get("name") or "": (p.text or "").strip() for p in element.iter("param")}
        services.append(Service(element.get("name") or "", params))
    return services, None


def download_name(service: Service) -> str:
    """The file name a ``download_url`` service saves its download under."""
    params = service.params
    if params.get("filename"):
        return params["filename"]
    path = params.get("path") or params.get("url", "").split("://", 1)[-1].partition("/")[2]
    return PurePosixPath(path.split("?", 1)[0]).name


# --- _service and sources --------------------------------------------------


def check_service(path: str, services: Sequence[Service]) -> list[Problem]:
    problems: list[Problem] = []

    def bad(message: str, *, warning: bool = False) -> None:
        problems.append(Problem(path, message, warning=warning, rule="service"))

    downloads: dict[str, Service] = {}
    for service in services:
        if service.name != "download_url":
            continue
        params = service.params
        name = download_name(service)
        label = name or "a download_url"
        if "url" in params:
            if not params["url"].startswith("https://"):
                bad(f"{label}: url is not https")
        else:
            protocol = params.get("protocol")
            if protocol is None:
                bad(f"{label}: no protocol (OBS then downloads over http)")
            elif protocol != "https":
                bad(f"{label}: protocol is {protocol!r}, not https")
            if not params.get("host"):
                bad(f"{label}: no host")
            if not params.get("path", "").startswith("/"):
                bad(f"{label}: path is empty or does not start with '/'")
        if not name:
            bad("a download_url has no file name (no filename, no file in the path)")
        elif "/" in name or "\\" in name or name.startswith(".") or "\0" in name:
            bad(f"{name!r}: not a safe file name")
        elif name in downloads:
            bad(f"{name}: two download_url services save the same file")
        else:
            downloads[name] = service
    verified: set[str] = set()
    for service in services:
        if service.name != "verify_file":
            continue
        params = service.params
        target = params.get("file", "").rsplit(":", 1)[-1]
        label = target or "a verify_file"
        verifier = params.get("verifier", "")
        checksum = params.get("checksum", "")
        if not target:
            bad("a verify_file has no file")
        elif target not in downloads:
            bad(f"{target}: verify_file names a file that no download_url saves")
        if verifier not in CHECKSUM_LENGTHS:
            bad(f"{label}: verifier {verifier!r} is not sha256 or sha512")
        elif not re.fullmatch(f"[0-9a-fA-F]{{{CHECKSUM_LENGTHS[verifier]}}}", checksum):
            bad(f"{label}: checksum is not {CHECKSUM_LENGTHS[verifier]} hex digits")
        elif target in downloads:
            verified.add(target)
    for name in sorted(downloads):
        if name not in verified:
            bad(f"{name} is downloaded but no verify_file checks its checksum")
    return problems


def check_sources(
    path: str,
    folder: str,
    spec: cs.Spec,
    context: Context,
    services: Sequence[Service] | None,
) -> list[Problem]:
    problems: list[Problem] = []
    package = PurePosixPath(folder).name
    services = services or []
    downloads = {download_name(s) for s in services if s.name == "download_url"}
    fetches = any(s.name == "download_files" for s in services)
    others = sorted({s.name for s in services if s.name not in PASSIVE_SERVICES})
    tarball = context.spec_context.tarballs.get(package)
    made = {"vendor.tar.xz"} if tarball and tarball[1] else set()
    own_tarball = re.compile(
        re.escape(cs.expand("%{name}-%{version}", spec.macros)) + r"\.tar\.\w+"
    )
    used: set[str] = set()
    for pkg in spec.packages:
        for tag, value, line in pkg.tags:
            if not re.fullmatch(r"(source|patch)\d*", tag.lower()):
                continue
            source = cs.expand(value, spec.macros)
            where = f"line {line}: {tag}"
            if "://" in source:
                base = PurePosixPath(source.rsplit("#/", 1)[-1].split("?", 1)[0]).name
                used.add(base)
                if not source.startswith("https://"):
                    problems.append(
                        Problem(path, f"{where} {source!r} is not an https URL", rule="source")
                    )
                if f"{folder}/{base}" in context.spec_context.tracked or base in downloads:
                    continue
                if not fetches:
                    problems.append(
                        Problem(
                            path,
                            f"{where} {base} is a URL, but the folder has no such file and "
                            "no _service with download_files fetches it",
                            rule="source",
                        )
                    )
                continue
            base = PurePosixPath(source).name
            used.add(base)
            if "%" in base:
                problems.append(
                    Problem(
                        path,
                        f"{where} {source!r} has a macro the checker cannot expand",
                        warning=True,
                        rule="source",
                    )
                )
            elif (
                f"{folder}/{base}" in context.spec_context.tracked
                or base in made
                or base in downloads
                or (own_tarball.fullmatch(base) and tarball is not None)
            ):
                continue
            elif others:
                problems.append(
                    Problem(
                        path,
                        f"{where} {base} is not in {folder}; only the service "
                        f"{', '.join(others)} could make it",
                        warning=True,
                        rule="source",
                    )
                )
            else:
                problems.append(
                    Problem(
                        path,
                        f"{where} {base} is not in {folder}, not downloaded by _service "
                        "and not made by obs_sync.py",
                        rule="source",
                    )
                )
    for name in sorted(downloads - used):
        if name:
            problems.append(
                Problem(
                    f"{folder}/_service",
                    f"{name} is downloaded but no Source or Patch of the spec uses it",
                    warning=True,
                    rule="service",
                )
            )
    return problems


# --- change log ------------------------------------------------------------


def address_problem(rest: str) -> str | None:
    """Why the author of an entry has no acceptable address (None: it has)."""
    found = EMAIL.findall(rest)
    if len(found) != 1:
        return "no address <user@domain> after the date" if not found else "more than one address"
    domain = found[0][1].lower()
    local = found[0][0].lower()
    if (
        domain == "vilnaos.com"
        or domain.endswith(".vilnaos.com")
        or domain == "users.noreply.github.com"
        or local in ("noreply", "no-reply")
    ):
        return None
    return f"the address {found[0][0]}@{found[0][1]} is not on vilnaos.com or a noreply one"


def stamp(text: str) -> datetime | None:
    try:
        return datetime.strptime(" ".join(text.split()), "%a %b %d %H:%M:%S %Y").replace(tzinfo=UTC)
    except ValueError:
        return None


def date_only(text: str) -> datetime | None:
    try:
        return datetime.strptime(" ".join(text.split()), "%a %b %d %Y").replace(tzinfo=UTC)
    except ValueError:
        return None


def check_entry(
    path: str, line: int, when: datetime | None, rest: str, now: datetime
) -> list[Problem]:
    problems: list[Problem] = []
    if when is None:
        problems.append(Problem(path, f"line {line}: the date does not exist", rule="changes"))
    elif when > now + DAY:
        problems.append(
            Problem(
                path,
                f"line {line}: the date {when:%Y-%m-%d} is ahead of the clock",
                warning=True,
                rule="changes",
            )
        )
    reason = address_problem(rest)
    if reason:
        problems.append(Problem(path, f"line {line}: {reason}", rule="changes"))
    return problems


def check_changes(path: str, text: str, now: datetime) -> tuple[list[Problem], int]:
    """Problems of a ``.changes`` file and the number of its entries."""
    problems: list[Problem] = []
    lines = text.splitlines()
    entries = 0
    for index, line in enumerate(lines):
        if line != cs.CHANGES_SEPARATOR:
            continue
        number = index + 2
        header = lines[index + 1] if index + 1 < len(lines) else ""
        match = CHANGES_ENTRY.fullmatch(header)
        if not match:
            problems.append(
                Problem(
                    path,
                    f"line {number}: no 'Www Mmm DD HH:MM:SS UTC YYYY - author <address>'",
                    rule="changes",
                )
            )
            continue
        entries += 1
        when = stamp(f"{match.group('date')} {match.group('year')}")
        problems += check_entry(path, number, when, match.group("rest"), now)
        body = []
        for follow in lines[index + 2 :]:
            if follow == cs.CHANGES_SEPARATOR:
                break
            body.append(follow)
        if not any(b.strip() for b in body):
            problems.append(
                Problem(path, f"line {number}: the entry has no text", warning=True, rule="changes")
            )
    return problems, entries


def check_spec_changelog(path: str, spec: cs.Spec, now: datetime) -> tuple[list[Problem], int]:
    problems: list[Problem] = []
    entries = 0
    for line in spec.changelog_lines:
        if not line.startswith("*"):
            continue
        match = SPEC_ENTRY.fullmatch(line.rstrip())
        if not match:
            problems.append(
                Problem(
                    path,
                    f"%changelog: no '* Www Mmm DD YYYY author <address>': {line!r}",
                    rule="changes",
                )
            )
            continue
        entries += 1
        when = date_only(match.group("date"))
        problems += [
            Problem(path, f"%changelog: {p.message}", warning=p.warning, rule=p.rule)
            for p in check_entry(path, 0, when, match.group("rest"), now)
        ]
    return problems, entries


# --- BuildRequires ---------------------------------------------------------


def split_top(value: str, separators: str) -> list[str]:
    """Split ``value`` at the separators that are not inside ``{}`` or ``()``."""
    parts: list[str] = []
    depth = 0
    current = ""
    for char in value:
        if char in "{(":
            depth += 1
        elif char in "})":
            depth = max(0, depth - 1)
        if depth == 0 and char in separators:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return parts


def parse_dependencies(value: str) -> tuple[list[Dependency], list[str]]:
    """The dependencies of a ``BuildRequires`` value and the problems of its form."""
    found: list[Dependency] = []
    errors: list[str] = []
    for group in split_top(value, ","):
        tokens = [t for t in split_top(group.strip(), " \t") if t]
        if not tokens:
            errors.append("an empty item (stray comma or empty value)")
            continue
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if token.startswith("(") or token.startswith("%"):
                prefix = "%{python_module "
                inner = token[len(prefix) : -1].strip() if token.startswith(prefix) else ""
                if inner:
                    sub, sub_errors = parse_module(inner)
                    errors += sub_errors
                    found += sub
                else:
                    found.append(Dependency(token, opaque=True))
                continue
            if token in OPERATORS or re.fullmatch(r"[<>=]+", token):
                errors.append(f"the operator {token!r} has no package before it")
                continue
            if not NAME.fullmatch(token):
                glued = re.search(r"[<>=]", token)
                errors.append(
                    f"{token!r}: the operator is stuck to the name, "
                    f"write 'name {token[glued.start() :]}'"
                    if glued
                    else f"{token!r} is not a valid package name"
                )
                continue
            operator = version = ""
            if index < len(tokens) and re.fullmatch(r"[<>=]+", tokens[index]):
                operator = tokens[index]
                index += 1
                if operator not in OPERATORS:
                    errors.append(f"{token}: unknown operator {operator!r}")
                if index < len(tokens) and VERSION.fullmatch(tokens[index]):
                    version = tokens[index]
                    index += 1
                else:
                    errors.append(f"{token} {operator}: no version after the operator")
            elif index < len(tokens) and re.fullmatch(r"\d+(\.\d+)+\S*", tokens[index]):
                errors.append(f"{token} {tokens[index]}: a version needs an operator")
                index += 1
            found.append(Dependency(token, operator, version))
    return found, errors


def parse_module(inner: str) -> tuple[list[Dependency], list[str]]:
    """``name [op version]`` inside ``%{python_module ...}``."""
    deps, errors = parse_dependencies(inner)
    for dep in deps:
        if dep.opaque or "(" in dep.name:
            return [Dependency(inner, opaque=True)], errors
    return [Dependency(d.name, d.operator, d.version, module=True) for d in deps], errors


def own_name(dep: Dependency, own: dict[str, str]) -> str | None:
    """The folder of this repo that builds the dependency (None: not one of ours)."""
    if dep.module:
        for name in (f"python3-{dep.name}", f"python-{dep.name}"):
            if name in own:
                return own[name]
        return None
    return own.get(dep.name)


def looks_ours(dep: Dependency) -> bool:
    base = re.sub(r"^python3?\d*-", "", dep.name)
    return base.startswith(OWN_PREFIXES)


def build_requires(spec: cs.Spec, macros: dict[str, str]) -> list[tuple[int, str]]:
    return [
        (line, cs.expand(value, macros))
        for tag, value, line in spec.main.tags
        if tag.lower() == "buildrequires"
    ]


def check_build_requires(
    path: str, folder: str, spec: cs.Spec, context: Context
) -> tuple[list[Problem], set[str]]:
    """Problems of the BuildRequires and the folders this package needs."""
    problems: list[Problem] = []
    needs: set[str] = set()
    seen: set[tuple[str, str, str]] = set()
    mine = {n for n, f in context.own.items() if f == folder}

    def bad(line: int, message: str) -> None:
        problems.append(
            Problem(path, f"line {line}: BuildRequires {message}", rule="buildrequires")
        )

    for line, value in build_requires(spec, spec.macros):
        dependencies, errors = parse_dependencies(value)
        for error in errors:
            bad(line, error)
        for dep in dependencies:
            if dep.opaque:
                continue
            key = (dep.name, dep.operator, dep.version)
            if key in seen:
                bad(line, f"{dep.text} is listed twice")
            seen.add(key)
            owner = own_name(dep, context.own)
            if owner is not None:
                if owner == folder or dep.name in mine:
                    bad(line, f"{dep.text} is a package this spec builds itself")
                else:
                    needs.add(owner)
                continue
            if looks_ours(dep):
                bad(
                    line,
                    f"{dep.text}: a name of this project that no spec in {PACKAGES} builds, "
                    "so Tumbleweed cannot have it either",
                )
            elif dep.name in context.planned and not dep.module:
                bad(
                    line,
                    f"{dep.text} is 'planned' in {PACKAGES_YAML}: no spec and not in Tumbleweed",
                )
            elif (
                context.tumbleweed is not None
                and not dep.module
                and "(" not in dep.name
                and dep.name not in context.tumbleweed
            ):
                bad(line, f"{dep.text} is not in the Tumbleweed list {context.list_path}")
    return problems, needs


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Every cycle of the graph once (each reported from its smallest folder)."""
    cycles: list[list[str]] = []
    seen: set[frozenset[str]] = set()

    def walk(start: str, node: str, trail: list[str]) -> None:
        for nxt in sorted(graph.get(node, ())):
            if nxt == start:
                key = frozenset(trail)
                if key not in seen:
                    seen.add(key)
                    cycles.append([*trail, start])
            elif nxt > start and nxt not in trail:
                walk(start, nxt, [*trail, nxt])

    for start in sorted(graph):
        walk(start, start, [start])
    return cycles


# --- one folder, all folders ------------------------------------------------


def check_folder(
    root: Path, folder: str, context: Context
) -> tuple[list[Problem], cs.Spec | None, set[str]]:
    name = PurePosixPath(folder).name
    path = f"{folder}/{name}.spec"
    text, problems = read_file(root, path)
    if text is None:
        return problems, None, set()
    if text.startswith("\ufeff"):
        text = text[1:]
    spec = cs.parse_spec(text)
    tracked = context.spec_context.tracked
    services: list[Service] | None = None
    if f"{folder}/_service" in tracked:
        service_text, found = read_file(root, f"{folder}/_service")
        problems += found
        if service_text is not None:
            parsed, reason = parse_service(service_text)
            if reason:
                problems.append(Problem(f"{folder}/_service", reason, rule="service"))
            else:
                services = parsed
                problems += check_service(f"{folder}/_service", parsed)
    problems += check_sources(path, folder, spec, context, services)
    entries = 0
    unreadable = False
    changes_path = f"{folder}/{name}.changes"
    if changes_path in tracked:
        changes, found = read_file(root, changes_path)
        problems += found
        unreadable = changes is None
        if changes is not None:
            found, entries = check_changes(
                changes_path, changes.removeprefix("\ufeff"), context.now
            )
            problems += found
    else:
        found, entries = check_spec_changelog(path, spec, context.now)
        problems += found
    if entries == 0 and not unreadable:
        problems.append(
            Problem(
                changes_path if changes_path in tracked else path,
                f"no change log: {name}.changes has no entry and the %changelog is empty"
                if changes_path in tracked
                else f"no change log: there is no {name}.changes and the %changelog has no entry",
                rule="changes",
            )
        )
    found, needs = check_build_requires(path, folder, spec, context)
    problems += found
    return problems, spec, needs


def legacy(problem: Problem) -> Problem:
    if (problem.path, problem.rule) in LEGACY:
        return Problem(problem.path, problem.message, warning=True, rule=problem.rule)
    return problem


def parse_list(text: str) -> frozenset[str]:
    names = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(line)
    return frozenset(names)


def planned_names(text: str) -> frozenset[str]:
    """Names with ``status: planned`` in a flow-style line of ``image/packages.yaml``."""
    names = set()
    for match in re.finditer(r"\{\s*name:\s*([^,}\s]+)[^}]*status:\s*planned\b", text):
        names.add(match.group(1))
    return frozenset(names)


def check_folders(
    root: Path,
    folders: Sequence[str],
    tracked: Iterable[str],
    *,
    tumbleweed_list: str | None = None,
    now: datetime | None = None,
) -> list[Problem]:
    """Check the package folders (repo-relative); the problems of ``folders`` only."""
    tracked = list(tracked)
    spec_context = cs.load_context(root, tracked)
    problems: list[Problem] = []
    names: frozenset[str] | None = None
    list_path = tumbleweed_list or (DEFAULT_LIST if DEFAULT_LIST in tracked else None)
    if list_path:
        text, found = read_file(root, list_path)
        problems += found
        if text is not None:
            names = parse_list(text.removeprefix("\ufeff"))
    yaml_text, _ = read_file(root, PACKAGES_YAML) if PACKAGES_YAML in tracked else (None, [])
    everything = cs.package_folders(tracked)
    # Pass 1: which RPM names the specs of the repo build.
    own: dict[str, str] = {}
    for folder in everything:
        text, _ = read_file(root, f"{folder}/{PurePosixPath(folder).name}.spec")
        if text is None:
            continue
        parsed = cs.parse_spec(text.removeprefix("\ufeff"))
        for built in cs.built_packages(parsed) + [PurePosixPath(folder).name]:
            own.setdefault(built, folder)
    context = Context(
        spec_context,
        own,
        planned_names(yaml_text or ""),
        names,
        list_path or "",
        now or datetime.now(UTC),
    )
    graph: dict[str, set[str]] = {}
    chosen = set(folders)
    for folder in everything:
        found, _, needs = check_folder(root, folder, context)
        graph[folder] = needs
        if folder in chosen:
            problems += found
    for cycle in find_cycles(graph):
        if cycle[0] in chosen:
            names_in = " -> ".join(PurePosixPath(f).name for f in cycle)
            spec_path = f"{cycle[0]}/{PurePosixPath(cycle[0]).name}.spec"
            problems.append(
                Problem(
                    spec_path, f"BuildRequires cycle, OBS cannot build it: {names_in}", rule="cycle"
                )
            )
    return [legacy(p) for p in problems]


def tracked_files(root: Path) -> list[str]:
    return cs.tracked_files(root) + _tracked_extra(root)


def _tracked_extra(root: Path) -> list[str]:
    """The tracked files outside obs/packages the checker reads (if they exist)."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", PACKAGES_YAML, DEFAULT_LIST],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


USAGE = "usage: python3 tools/check_obs.py [--tumbleweed-list FILE] [obs/packages/NAME ...]"


def main(argv: list[str] | None = None) -> int:
    try:
        args = list(sys.argv[1:] if argv is None else argv)
        if args in (["-h"], ["--help"]):
            print(USAGE)
            return 0
        list_file: str | None = None
        if "--tumbleweed-list" in args:
            at = args.index("--tumbleweed-list")
            if at + 1 >= len(args):
                print(USAGE, file=sys.stderr)
                return 2
            list_file = args[at + 1]
            del args[at : at + 2]
        if any(a.startswith("-") for a in args):
            print(USAGE, file=sys.stderr)
            return 2
        try:
            tracked = tracked_files(ROOT)
        except (OSError, subprocess.CalledProcessError) as err:
            print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
            return 2
        if list_file is not None:
            try:
                rel = Path(list_file).resolve().relative_to(ROOT).as_posix()
            except ValueError:
                print(f"{list_file}: the list must be a file of the repository", file=sys.stderr)
                return 2
            if not (ROOT / rel).is_file():
                print(f"{list_file}: no such file", file=sys.stderr)
                return 2
            list_file = rel
        available = cs.package_folders(tracked)
        folders = [a.rstrip("/") for a in args] or available
        unknown = [f for f in folders if f not in available]
        if unknown:
            print(f"not a package folder with tracked files: {', '.join(unknown)}", file=sys.stderr)
            return 2
        if not folders:
            print(f"no package folders found in {PACKAGES}", file=sys.stderr)
            return 2
        problems = check_folders(ROOT, folders, tracked, tumbleweed_list=list_file)
        for problem in problems:
            print(problem, file=sys.stderr)
        errors = sum(not p.warning for p in problems)
        print(f"{len(folders)} packages checked: {errors} errors, {len(problems) - errors} warnings")
        return 1 if errors else 0
    except (OSError, ValueError) as err:  # no traceback for a bad name, argument or bytes
        print(f"{os.path.basename(__file__)}: cannot check: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
