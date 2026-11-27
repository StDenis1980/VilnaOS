#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the dependencies and lock files of the repo (supply chain, CRA).

Python: for every tracked ``pyproject.toml`` with a ``[project]`` table:

* a tracked ``uv.lock`` is next to it, and it has an entry for the project itself with the
  same version as ``pyproject.toml`` (a ``dynamic`` version is not compared: warning);
* every requirement of ``[project].dependencies``, ``[project.optional-dependencies]``,
  ``[dependency-groups]`` (and the old ``[tool.uv].dev-dependencies``) is a package of
  ``uv.lock``; the name is read by PEP 503 rules, the version is ignored. A requirement the
  project's own lock entry does not list is a warning (the lock may be stale);
* the sources in ``uv.lock``: ``registry`` only ``https://pypi.org/simple``; ``editable``,
  ``virtual``, ``directory`` and ``path`` only inside the repo (relative, not leaving the
  root); ``git`` only with a full 40-hex commit after ``#``; any other kind is an error.
  A download URL that is not ``https://`` is an error; a malformed hash is a warning;
  a registry package without any hash is a warning;
* ``requires-python`` allows Python 3.11 (a missing one is a warning).

Rust: for every tracked ``Cargo.toml`` with a ``[workspace]`` table:

* a tracked ``Cargo.lock`` is next to it; every member (``members`` with ``*`` globs,
  minus ``exclude``; the root ``[package]``, if any) is in the lock with the version of its
  ``Cargo.toml`` (``version.workspace = true`` is followed);
* sources in the lock: only ``registry+https://github.com/rust-lang/crates.io-index`` or
  ``git+...#<40 hex>``; a registry package without a ``checksum`` is an error;
* ``path`` dependencies (``[dependencies]``, ``[dev-dependencies]``, ``[build-dependencies]``,
  ``target.*``, ``[workspace.dependencies]``, ``[patch]``) stay inside the repo.

Apart from that: a lock file without its manifest and a crate that is in no workspace are
warnings. At the end comes a summary: the number of third-party packages in every project, and
a warning for every package that has different versions in different projects.

Errors make the exit code 1. Warnings (``path: warning: ...``) do not. Some warnings mark
things the repo already does today (the LEGACY table below lists them).

Only ``git ls-files`` is run (to find the files). Nothing else is executed, nothing is
read from the network and nothing is written.

Run: python3 tools/check_deps.py [DIR]
Without an argument the repo that holds this file is checked. With DIR the tracked files
of the git repo in DIR are checked and DIR is the repo root for the "stays inside" rules.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import subprocess
import sys
import tomllib
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

PYPI_REGISTRY = "https://pypi.org/simple"
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"
PROJECT_KINDS = ("editable", "virtual", "directory")  # our own code, not third-party
SOURCE_KINDS = ("registry", "editable", "virtual", "directory", "path", "git", "url")
LOCAL_KINDS = ("editable", "virtual", "directory", "path")
DEP_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")
PYTHON_TO_ALLOW = "3.11"
# Candidate patch releases tried for "does the specifier allow some 3.11.x".
PATCH_CANDIDATES = range(100)

# Problems the repo has today, as (path, rule) -> why. They are reported as warnings.
# Fix the cause, then delete the line. (At 5855c7f the table is empty: the repo has none.)
LEGACY: dict[tuple[str, str], str] = {}

HEX40 = re.compile(r"[0-9a-fA-F]{40}")
PEP_HASH = re.compile(r"sha256:[0-9a-f]{64}")
CARGO_CHECKSUM = re.compile(r"[0-9a-f]{64}")
REQ_NAME = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")
SPECIFIER = re.compile(r"\s*(===|==|!=|~=|>=|<=|>|<)\s*(\S+)\s*")
VERSION = re.compile(
    r"""\s*v?(?:(?P<epoch>\d+)!)?(?P<release>\d+(?:\.\d+)*)
    (?:[-_.]?(?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)[-_.]?(?P<pre_n>\d*))?
    (?:-(?P<post_n1>\d+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>\d*))?
    (?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>\d*))?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?\s*""",
    re.IGNORECASE | re.VERBOSE,
)
PRE_NAMES = {"alpha": "a", "beta": "b", "c": "rc", "pre": "rc", "preview": "rc"}
NO_DEV = 10**18


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False
    rule: str = ""

    def __str__(self) -> str:
        return f"{self.path}: {'warning: ' if self.warning else ''}{self.message}"


@dataclass
class ProjectSummary:
    ecosystem: str  # pypi or cargo
    label: str  # the folder of the project
    lock: str
    # name -> versions of the third-party packages in the lock file
    packages: dict[str, set[str]] = field(default_factory=dict)
    third_party: int = 0


@dataclass
class Report:
    problems: list[Problem] = field(default_factory=list)
    summaries: list[ProjectSummary] = field(default_factory=list)

    def add(self, path: str, message: str, rule: str, warning: bool = False) -> None:
        legacy = LEGACY.get((path, rule))
        if legacy is not None:
            self.problems.append(Problem(path, f"{message} (legacy: {legacy})", True, rule))
        else:
            self.problems.append(Problem(path, message, warning, rule))


# --- small helpers ------------------------------------------------------------


def dir_of(rel: str) -> str:
    return posixpath.dirname(rel)


def join(base: str, name: str) -> str:
    return posixpath.join(base, name) if base else name


def resolve_inside(base: str, target: str) -> str | None:
    """Repo-relative path of ``target`` (relative to the folder ``base``), or None.

    None means: the path is absolute (also a Windows one) or leaves the repo root.
    The repo root itself is the empty string.
    """
    target = target.replace("\\", "/")
    if not target or target.startswith("/") or WINDOWS_DRIVE.match(target):
        return None
    full = posixpath.normpath(posixpath.join(base, target))
    if full == "..":
        return None
    if full.startswith("../"):
        return None
    return "" if full == "." else full


def as_table(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def load_toml(root: Path, rel: str, report: Report) -> dict[str, Any] | None:
    try:
        text = (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        report.add(rel, f"cannot read the file: {err}", "unreadable")
        return None
    if text.startswith("\ufeff"):
        report.add(rel, "starts with a UTF-8 byte order mark (BOM); remove it", "toml")
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        report.add(rel, f"invalid TOML: {err}", "toml")
        return None


# --- versions and specifiers (PEP 440, only what these checks need) -------------


def normalize_name(name: str) -> str:
    """PEP 503 normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(requirement: str) -> str | None:
    """The project name of a PEP 508 requirement string, or None if it has none."""
    match = REQ_NAME.match(requirement)
    if not match:
        return None
    rest = requirement[match.end() :].lstrip()
    if rest and rest[0] not in "[(;@<>=!~":
        return None
    return match.group(1)


def version_key(text: str) -> tuple[Any, ...] | None:
    """A sort key that follows PEP 440 order; equal versions have equal keys."""
    match = VERSION.fullmatch(text)
    if not match:
        return None
    release = [int(p) for p in match["release"].split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    pre = None
    if match["pre_l"]:
        letter = match["pre_l"].lower()
        pre = (PRE_NAMES.get(letter, letter), int(match["pre_n"] or 0))
    post = None
    if match["post_n1"] or match["post_l"]:
        post = int(match["post_n1"] or match["post_n2"] or 0)
    dev = int(match["dev_n"] or 0) if match["dev_l"] else None
    if pre is None and post is None and dev is not None:
        pre_key: tuple[Any, ...] = (0, "", 0)  # a dev release sorts before the pre-releases
    elif pre is None:
        pre_key = (2, "", 0)
    else:
        pre_key = (1, *pre)
    local = (match["local"] or "").lower()
    return (
        int(match["epoch"] or 0),
        tuple(release),
        pre_key,
        -1 if post is None else post,
        NO_DEV if dev is None else dev,
        local,
    )


def release_of(text: str) -> tuple[int, ...] | None:
    match = VERSION.fullmatch(text)
    return tuple(int(p) for p in match["release"].split(".")) if match else None


def same_version(left: str, right: str) -> bool:
    """Equal as text, or equal as PEP 440 versions (``1.0`` is ``1.0.0``)."""
    if left == right:
        return True
    left_key, right_key = version_key(left), version_key(right)
    return left_key is not None and left_key == right_key


def parse_specifiers(text: str) -> list[tuple[str, str]] | None:
    """``">=3.11,<4"`` -> [(">=", "3.11"), ("<", "4")]; None if it cannot be read."""
    result = []
    for part in text.split(","):
        match = SPECIFIER.fullmatch(part)
        if not match:
            return None
        op, version = match.groups()
        wildcard = version.endswith(".*")
        base = version[:-2] if wildcard else version
        if op != "===" and (version_key(base) is None or (wildcard and op not in ("==", "!="))):
            return None
        if op == "~=" and len(release_of(base) or ()) < 2:
            return None
        result.append((op, version))
    return result


def prefix_match(candidate: str, prefix_text: str) -> bool:
    cand = release_of(candidate) or ()
    prefix = release_of(prefix_text) or ()
    padded = cand + (0,) * max(0, len(prefix) - len(cand))
    return padded[: len(prefix)] == prefix


def satisfies(specifiers: Sequence[tuple[str, str]], candidate: str) -> bool:
    """Does the plain release ``candidate`` meet every specifier?"""
    cand_key = version_key(candidate) or ()
    for op, version in specifiers:
        if op == "===":
            ok = candidate == version
        elif op in ("==", "!="):
            if version.endswith(".*"):
                ok = prefix_match(candidate, version[:-2])
            else:
                ok = cand_key == (version_key(version) or ())
            ok = ok if op == "==" else not ok
        elif op == "~=":
            release = ".".join(str(n) for n in (release_of(version) or ())[:-1])
            ok = cand_key >= (version_key(version) or ()) and prefix_match(candidate, release)
        else:
            key = version_key(version) or ()
            ok = {">=": cand_key >= key, ">": cand_key > key, "<=": cand_key <= key}.get(
                op, cand_key < key
            )
        if not ok:
            return False
    return True


def allows_python(requires: str, python: str = PYTHON_TO_ALLOW) -> bool | None:
    """Does ``requires-python`` allow some ``python.x``? None if it cannot be read."""
    specifiers = parse_specifiers(requires)
    if specifiers is None:
        return None
    return any(satisfies(specifiers, f"{python}.{patch}") for patch in PATCH_CANDIDATES)


def squash(text: str) -> str:
    return ",".join(sorted(part.replace(" ", "") for part in text.split(",")))


# --- Python -------------------------------------------------------------------


def source_of(package: dict[str, Any]) -> tuple[str, str]:
    """(kind, value) of the ``source`` of a lock package; kind is ``?`` if unknown."""
    source = package.get("source")
    if isinstance(source, dict):
        for kind in SOURCE_KINDS:
            if kind in source:
                return kind, str(source[kind])
    return "?", repr(source)


def declared_requirements(doc: dict[str, Any], rel: str, report: Report) -> list[tuple[str, str]]:
    """Every requirement string of the pyproject as (where, requirement)."""
    found: list[tuple[str, str]] = []
    project = as_table(doc.get("project"))

    def add(where: str, items: Any) -> None:
        for item in as_list(items):
            if isinstance(item, str):
                found.append((where, item))
            elif not (isinstance(item, dict) and where.startswith("dependency-groups.")):
                report.add(rel, f"{where}: {item!r} is not a requirement string", "requirement")

    add("project.dependencies", project.get("dependencies"))
    for extra, items in as_table(project.get("optional-dependencies")).items():
        add(f"project.optional-dependencies.{extra}", items)
    groups = as_table(doc.get("dependency-groups"))
    for group, items in groups.items():
        add(f"dependency-groups.{group}", items)
        for item in as_list(items):
            if isinstance(item, dict):
                target = item.get("include-group")
                if not isinstance(target, str) or normalize_name(target) not in {
                    normalize_name(g) for g in groups
                }:
                    report.add(
                        rel,
                        f"dependency-groups.{group}: {item!r} does not name an existing group",
                        "requirement",
                    )
    add(
        "tool.uv.dev-dependencies",
        as_table(as_table(doc.get("tool")).get("uv")).get("dev-dependencies"),
    )
    return found


def lock_listed_names(package: dict[str, Any]) -> set[str]:
    """Names the lock entry of the project lists as its dependencies (all kinds)."""
    names: set[str] = set()

    def take(items: Any) -> None:
        for item in as_list(items):
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.add(normalize_name(item["name"]))

    take(package.get("dependencies"))
    for items in as_table(package.get("optional-dependencies")).values():
        take(items)
    for items in as_table(package.get("dev-dependencies")).values():
        take(items)
    metadata = as_table(package.get("metadata"))
    take(metadata.get("requires-dist"))
    for items in as_table(metadata.get("requires-dev")).values():
        take(items)
    return names


def check_uv_sources_paths(doc: dict[str, Any], rel: str, report: Report) -> None:
    """The ``path`` of ``[tool.uv.sources]`` stays inside the repo."""
    base = dir_of(rel)
    sources = as_table(as_table(as_table(doc.get("tool")).get("uv")).get("sources"))
    for name, spec in sources.items():
        for item in spec if isinstance(spec, list) else [spec]:
            path = as_table(item).get("path")
            if isinstance(path, str) and resolve_inside(base, path) is None:
                report.add(
                    rel,
                    f"tool.uv.sources.{name}: path '{path}' is absolute or leaves the repository",
                    "path-outside",
                )


def check_python_source(lock_rel: str, package: dict[str, Any], report: Report) -> None:
    name = package.get("name", "?")
    kind, value = source_of(package)
    label = f"package '{name}' {package.get('version', '')}".rstrip()
    if kind == "registry":
        if value != PYPI_REGISTRY:
            report.add(
                lock_rel,
                f"{label}: registry '{value}' is not {PYPI_REGISTRY}",
                "py-registry",
            )
    elif kind in LOCAL_KINDS:
        if resolve_inside(dir_of(lock_rel), value) is None:
            report.add(
                lock_rel,
                f"{label}: {kind} path '{value}' is absolute or leaves the repository",
                "path-outside",
            )
    elif kind == "git":
        _, _, commit = value.partition("#")
        if not HEX40.fullmatch(commit):
            report.add(
                lock_rel,
                f"{label}: git source '{value}' has no full 40-hex commit after '#'",
                "git-commit",
            )
    else:
        report.add(lock_rel, f"{label}: source {value} is not allowed", "py-source")


def check_python_artifacts(lock_rel: str, package: dict[str, Any], report: Report) -> None:
    """URLs are https; hashes look like sha256; a registry package has some hash."""
    label = f"package '{package.get('name', '?')}' {package.get('version', '')}".rstrip()
    artifacts = [as_table(package.get("sdist"))]
    artifacts += [as_table(w) for w in as_list(package.get("wheels"))]
    hashes = 0
    for artifact in artifacts:
        url = artifact.get("url")
        if isinstance(url, str) and not url.startswith("https://"):
            report.add(lock_rel, f"{label}: download URL '{url}' is not https", "py-url")
        digest = artifact.get("hash")
        if isinstance(digest, str) and digest:
            hashes += 1
            if not PEP_HASH.fullmatch(digest):
                report.add(
                    lock_rel,
                    f"{label}: hash '{digest}' is not sha256:<64 hex>",
                    "py-hash-format",
                    warning=True,
                )
    if source_of(package)[0] == "registry" and hashes == 0:
        report.add(lock_rel, f"{label}: no hash at all", "py-no-hash", warning=True)


def find_project_entry(
    lock_rel: str, packages: list[dict[str, Any]], name: str, report: Report
) -> dict[str, Any] | None:
    """The lock entry of the project itself (source = this folder), reporting if none."""
    wanted = normalize_name(name)
    same_name = [p for p in packages if normalize_name(str(p.get("name", ""))) == wanted]
    if not same_name:
        report.add(lock_rel, f"no entry for the project itself ('{name}')", "self-entry")
        return None
    for package in same_name:
        kind, value = source_of(package)
        if kind in PROJECT_KINDS and resolve_inside(dir_of(lock_rel), value) == dir_of(lock_rel):
            return package
    kind, value = source_of(same_name[0])
    report.add(
        lock_rel,
        f"the entry '{name}' is not the project itself (source {kind} = '{value}')",
        "self-entry",
    )
    return None


def check_python_project(
    root: Path, tracked: set[str], rel: str, report: Report
) -> ProjectSummary | None:
    doc = load_toml(root, rel, report)
    if doc is None:
        return None
    check_uv_sources_paths(doc, rel, report)
    project = doc.get("project")
    if not isinstance(project, dict):
        report.add(rel, "no [project] table: not checked", "no-project", warning=True)
        return None
    name = project.get("name")
    if not isinstance(name, str) or not name:
        report.add(rel, "[project] has no name", "no-name")
        return None
    requires = project.get("requires-python")
    check_requires_python(rel, requires, report)
    lock_rel = join(dir_of(rel), "uv.lock")
    if lock_rel not in tracked:
        report.add(rel, "no tracked uv.lock next to it", "no-lock")
        return None
    lock = load_toml(root, lock_rel, report)
    if lock is None:
        return None
    packages = [p for p in as_list(lock.get("package")) if isinstance(p, dict)]
    for package in packages:
        if not isinstance(package.get("name"), str) or not isinstance(
            package.get("version", ""), str
        ):
            report.add(
                lock_rel,
                f"a package entry without a text name or version: {package!r}",
                "lock-entry",
            )
    entry = find_project_entry(lock_rel, packages, name, report)
    if entry is not None:
        check_project_version(rel, lock_rel, project, entry, report)
    check_declared_in_lock(rel, lock_rel, doc, name, packages, entry, report)
    lock_requires = lock.get("requires-python")
    if (
        isinstance(requires, str)
        and isinstance(lock_requires, str)
        and squash(requires) != squash(lock_requires)
    ):
        report.add(
            lock_rel,
            f"requires-python '{lock_requires}' differs from the "
            f"pyproject's '{requires}' (lock may be stale)",
            "lock-requires-python",
            warning=True,
        )
    summary = ProjectSummary("pypi", dir_of(rel) or ".", lock_rel)
    for package in packages:
        check_python_source(lock_rel, package, report)
        check_python_artifacts(lock_rel, package, report)
        if source_of(package)[0] not in PROJECT_KINDS and isinstance(package.get("name"), str):
            summary.third_party += 1
            version = str(package.get("version", "?"))
            summary.packages.setdefault(normalize_name(package["name"]), set()).add(version)
    return summary


def check_requires_python(rel: str, requires: Any, report: Report) -> None:
    if requires is None:
        report.add(rel, "no requires-python", "requires-python-missing", warning=True)
        return
    allowed = allows_python(requires) if isinstance(requires, str) else None
    if allowed is None:
        report.add(rel, f"cannot read requires-python {requires!r}", "requires-python")
    elif not allowed:
        report.add(
            rel,
            f"requires-python '{requires}' does not allow Python {PYTHON_TO_ALLOW}",
            "requires-python",
        )


def check_project_version(
    rel: str, lock_rel: str, project: dict[str, Any], entry: dict[str, Any], report: Report
) -> None:
    dynamic = [str(d) for d in as_list(project.get("dynamic"))]
    version = project.get("version")
    if "version" in dynamic:
        report.add(
            rel,
            "the version is dynamic: not compared with uv.lock",
            "dynamic-version",
            warning=True,
        )
    elif not isinstance(version, str):
        report.add(rel, "[project] has no version (and it is not dynamic)", "no-version")
    elif not isinstance(entry.get("version"), str):
        report.add(lock_rel, f"the entry of '{project['name']}' has no version", "lock-entry")
    elif not same_version(version, entry["version"]):
        report.add(
            lock_rel,
            f"the project '{project['name']}' is {entry['version']} here but {version} in "
            f"{rel}; run `uv lock`",
            "version-mismatch",
        )


def check_declared_in_lock(
    rel: str,
    lock_rel: str,
    doc: dict[str, Any],
    name: str,
    packages: list[dict[str, Any]],
    entry: dict[str, Any] | None,
    report: Report,
) -> None:
    in_lock = {normalize_name(str(p.get("name", ""))) for p in packages}
    listed = lock_listed_names(entry) if entry is not None else None
    own = normalize_name(name)
    for where, requirement in declared_requirements(doc, rel, report):
        dep = requirement_name(requirement)
        if dep is None:
            report.add(rel, f"{where}: cannot read the name in '{requirement}'", "requirement")
            continue
        key = normalize_name(dep)
        if key not in in_lock:
            report.add(
                rel, f"{where}: '{dep}' is not in {lock_rel}; run `uv lock`", "dep-not-in-lock"
            )
        elif listed is not None and key != own and key not in listed:
            report.add(
                rel,
                f"{where}: '{dep}' is in {lock_rel}, but the project's own entry does not "
                "list it (lock may be stale)",
                "dep-not-listed",
                warning=True,
            )


# --- Rust ---------------------------------------------------------------------


def dependency_tables(doc: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for key in DEP_TABLES:
        if isinstance(doc.get(key), dict):
            yield key, doc[key]
    for cfg, sub in as_table(doc.get("target")).items():
        for key in DEP_TABLES:
            if isinstance(as_table(sub).get(key), dict):
                yield f"target.{cfg}.{key}", sub[key]
    workspace_deps = as_table(doc.get("workspace")).get("dependencies")
    if isinstance(workspace_deps, dict):
        yield "workspace.dependencies", workspace_deps
    for registry, table in as_table(doc.get("patch")).items():
        if isinstance(table, dict):
            yield f"patch.{registry}", table


def check_path_dependencies(rel: str, doc: dict[str, Any], report: Report) -> None:
    base = dir_of(rel)
    for table_name, table in dependency_tables(doc):
        for dep, spec in table.items():
            path = as_table(spec).get("path")
            if isinstance(path, str) and resolve_inside(base, path) is None:
                report.add(
                    rel,
                    f"[{table_name}] '{dep}': path '{path}' is absolute or leaves the repository",
                    "path-outside",
                )


def segments_match(path: str, pattern: str) -> bool:
    parts, pats = path.split("/") if path else [], pattern.split("/") if pattern else []
    return len(parts) == len(pats) and all(
        fnmatch.fnmatchcase(part, pat) for part, pat in zip(parts, pats, strict=True)
    )


def workspace_members(
    rel: str, doc: dict[str, Any], crate_dirs: set[str], report: Report
) -> list[str]:
    """Folders of the members of the workspace, as repo-relative paths."""
    base = dir_of(rel)
    workspace = as_table(doc.get("workspace"))
    members: list[str] = []
    if isinstance(doc.get("package"), dict):
        members.append(base)
    excluded = []
    for pattern in as_list(workspace.get("exclude")):
        full = resolve_inside(base, str(pattern))
        if full is not None:
            excluded.append(full)
    for pattern in as_list(workspace.get("members")):
        full = resolve_inside(base, str(pattern))
        if full is None:
            report.add(
                rel, f"member '{pattern}' is absolute or leaves the repository", "path-outside"
            )
            continue
        found = sorted(d for d in crate_dirs if segments_match(d, full))
        found = [d for d in found if not any(segments_match(d, ex) for ex in excluded)]
        if not found and not any(segments_match(full, ex) for ex in excluded):
            report.add(rel, f"member '{pattern}' has no tracked Cargo.toml", "member-missing")
        members.extend(d for d in found if d not in members)
    return members


def lock_source_problem(source: Any) -> str | None:
    if source == CRATES_IO:
        return None
    if isinstance(source, str) and source.startswith("git+"):
        _, _, commit = source.partition("#")
        if HEX40.fullmatch(commit):
            return None
        return f"git source '{source}' has no full 40-hex commit after '#'"
    return f"source '{source}' is not crates.io or git"


def check_cargo_lock_sources(lock_rel: str, packages: list[dict[str, Any]], report: Report) -> int:
    third_party = 0
    for package in packages:
        label = f"package '{package.get('name', '?')}' {package.get('version', '')}".rstrip()
        source = package.get("source")
        if source is None:
            continue  # a path crate of this repo
        third_party += 1
        problem = lock_source_problem(source)
        if problem:
            rule = "git-commit" if str(source).startswith("git+") else "cargo-source"
            report.add(lock_rel, f"{label}: {problem}", rule)
        elif source == CRATES_IO:
            checksum = package.get("checksum")
            if not isinstance(checksum, str) or not checksum:
                report.add(
                    lock_rel, f"{label}: crates.io package without a checksum", "cargo-no-checksum"
                )
            elif not CARGO_CHECKSUM.fullmatch(checksum):
                report.add(
                    lock_rel,
                    f"{label}: checksum '{checksum}' is not 64 hex digits",
                    "cargo-checksum-format",
                )
    return third_party


def member_identity(
    rel: str, doc: dict[str, Any], workspace_doc: dict[str, Any], report: Report
) -> tuple[str, str] | None:
    package = doc.get("package")
    if not isinstance(package, dict) or not isinstance(package.get("name"), str):
        report.add(rel, "a workspace member without [package] name", "member-package")
        return None
    version = package.get("version", "0.0.0")
    if isinstance(version, dict) and version.get("workspace") is True:
        version = as_table(as_table(workspace_doc.get("workspace")).get("package")).get("version")
        if not isinstance(version, str):
            report.add(
                rel,
                "version.workspace = true, but [workspace.package] has no version",
                "member-version",
            )
            return None
    if not isinstance(version, str):
        report.add(rel, f"cannot read the version {version!r}", "member-version")
        return None
    return package["name"], version


def check_rust_workspace(
    root: Path,
    tracked: set[str],
    rel: str,
    docs: dict[str, dict[str, Any]],
    report: Report,
) -> tuple[ProjectSummary | None, list[str]]:
    """Check one workspace; return its summary and the folders of its members."""
    doc = docs[rel]
    crate_dirs = {dir_of(r) for r in docs}
    members = workspace_members(rel, doc, crate_dirs, report)
    lock_rel = join(dir_of(rel), "Cargo.lock")
    if lock_rel not in tracked:
        report.add(rel, "no tracked Cargo.lock next to it", "no-lock")
        return None, members
    lock = load_toml(root, lock_rel, report)
    if lock is None:
        return None, members
    packages = [p for p in as_list(lock.get("package")) if isinstance(p, dict)]
    local: dict[str, set[str]] = defaultdict(set)
    for package in packages:
        if package.get("source") is None:
            local[str(package.get("name"))].add(str(package.get("version")))
    for member in members:
        member_rel = join(member, "Cargo.toml")
        member_doc = docs.get(member_rel)
        if member_doc is None:
            continue  # unreadable: already reported
        identity = member_identity(member_rel, member_doc, doc, report)
        if identity is None:
            continue
        name, version = identity
        if name not in local:
            report.add(
                lock_rel,
                f"no entry for the member '{name}' ({member_rel}); run `cargo update -w`",
                "member-not-in-lock",
            )
        elif version not in local[name]:
            report.add(
                lock_rel,
                f"the member '{name}' is {', '.join(sorted(local[name]))} here but {version} in "
                f"{member_rel}",
                "version-mismatch",
            )
    for manifest in [rel] + [
        join(m, "Cargo.toml") for m in members if join(m, "Cargo.toml") != rel
    ]:
        if manifest in docs:
            check_path_dependencies(manifest, docs[manifest], report)
    summary = ProjectSummary("cargo", dir_of(rel) or ".", lock_rel)
    summary.third_party = check_cargo_lock_sources(lock_rel, packages, report)
    for package in packages:
        if package.get("source") is not None and isinstance(package.get("name"), str):
            version = str(package.get("version", "?"))
            summary.packages.setdefault(package["name"], set()).add(version)
    return summary, members


# --- the whole repo --------------------------------------------------------------


def version_differences(summaries: Sequence[ProjectSummary]) -> list[Problem]:
    """A warning for every package that has different versions in different projects."""
    seen: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for summary in summaries:
        for name, versions in summary.packages.items():
            for version in versions:
                seen[(summary.ecosystem, name)][version].add(summary.label)
    problems = []
    for (ecosystem, name), by_version in sorted(seen.items()):
        projects = set().union(*by_version.values())
        if len(by_version) < 2 or len(projects) < 2:
            continue
        parts = [
            f"{version} ({', '.join(sorted(labels))})"
            for version, labels in sorted(by_version.items(), key=lambda kv: version_sort(kv[0]))
        ]
        problems.append(
            Problem(
                "summary",
                f"{ecosystem} package '{name}' has different versions in different projects: "
                + "; ".join(parts),
                True,
                "version-spread",
            )
        )
    return problems


def version_sort(version: str) -> tuple[Any, ...]:
    key = version_key(version)
    return (0, key) if key is not None else (1, version)


def check_repo(root: Path, tracked: Sequence[str]) -> Report:
    """Check the tracked files ``tracked`` (paths with ``/`` from ``root``)."""
    report = Report()
    files = set(tracked)
    pyprojects = sorted(f for f in files if posixpath.basename(f) == "pyproject.toml")
    cargo_tomls = sorted(f for f in files if posixpath.basename(f) == "Cargo.toml")
    for rel in pyprojects:
        summary = check_python_project(root, files, rel, report)
        if summary is not None:
            report.summaries.append(summary)
    docs: dict[str, dict[str, Any]] = {}
    for rel in cargo_tomls:
        loaded = load_toml(root, rel, report)
        if loaded is not None:
            docs[rel] = loaded
    roots = [r for r in cargo_tomls if r in docs and "workspace" in docs[r]]
    in_workspace: set[str] = set()
    for rel in roots:
        summary, members = check_rust_workspace(root, files, rel, docs, report)
        in_workspace.update(join(m, "Cargo.toml") for m in members)
        in_workspace.add(rel)
        if summary is not None:
            report.summaries.append(summary)
    for rel in cargo_tomls:
        if rel in docs and rel not in in_workspace:
            report.add(rel, "is in no Cargo workspace: not checked", "orphan-crate", warning=True)
    for lock_rel in sorted(f for f in files if posixpath.basename(f) in ("uv.lock", "Cargo.lock")):
        wanted = "pyproject.toml" if lock_rel.endswith("uv.lock") else "Cargo.toml"
        manifest = join(dir_of(lock_rel), wanted)
        if manifest not in files:
            report.add(lock_rel, f"no {wanted} next to this lock file", "orphan-lock", warning=True)
        elif wanted == "Cargo.toml" and manifest not in roots:
            report.add(lock_rel, "its Cargo.toml has no [workspace]", "orphan-lock", warning=True)
    report.problems.extend(version_differences(report.summaries))
    return report


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--",
            "*pyproject.toml",
            "*Cargo.toml",
            "*uv.lock",
            "*Cargo.lock",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def summary_lines(report: Report) -> list[str]:
    lines = []
    for s in sorted(report.summaries, key=lambda s: (s.ecosystem, s.label)):
        lines.append(f"{s.ecosystem:5} {s.label}: {s.third_party} third-party packages ({s.lock})")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    usage = "usage: python3 tools/check_deps.py [DIR]"
    if args in (["-h"], ["--help"]):
        print(usage)
        return 0
    if len(args) > 1 or any(a.startswith("-") for a in args):
        print(usage, file=sys.stderr)
        return 2
    root = Path(args[0]) if args else ROOT
    if not root.is_dir():
        print(f"{root}: not a folder\n{usage}", file=sys.stderr)
        return 2
    try:
        paths = tracked_files(root)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    if not paths:
        print("no tracked pyproject.toml or Cargo.toml files found", file=sys.stderr)
        return 2
    report = check_repo(root, paths)
    for problem in report.problems:
        print(problem, file=sys.stderr)
    for line in summary_lines(report):
        print(line)
    errors = sum(not p.warning for p in report.problems)
    warnings = len(report.problems) - errors
    print(f"{len(report.summaries)} projects checked: {errors} errors, {warnings} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
