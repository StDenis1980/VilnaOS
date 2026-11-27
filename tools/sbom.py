#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the SBOM of Vilna OS: one SPDX 2.3 JSON document from the files of the repo.

    python3 tools/sbom.py                          # the document on stdout
    python3 tools/sbom.py --version 0.4 --out vilna.spdx.json
    python3 tools/sbom.py --check                  # build it and check its integrity (CI)
    python3 tools/sbom.py --created 2026-11-27T09:00:00Z --root /path/to/checkout

What goes in (only files that ``git ls-files`` lists are read):

* the root package "Vilna OS" (``--version``, default ``0.0.0-dev``): the document
  DESCRIBES it and it CONTAINS every component below;
* our own components: every ``pyproject.toml`` project, every Cargo package
  (``Cargo.toml`` with ``[package]``, version and licence inherited from the
  ``[workspace.package]`` where the package says ``workspace = true``) and every
  ``obs/packages/<name>/<name>.spec`` (Name, Version, License; the simple macros
  ``%define``/``%global``/``%{name}``/``%{version}`` are expanded);
* third-party packages: every package of every ``uv.lock`` (purl ``pkg:pypi/..``,
  SHA-256 of the sdist, or of the first wheel when there is no sdist) and every
  ``Cargo.lock`` (purl ``pkg:cargo/..``, the checksum of the lock). The same
  package at the same version is one element, with one DEPENDS_ON from each of our
  components whose lock reaches it (the whole closure of the lock, dev groups and
  extras included: the lock files do not tell what ships). The lock files carry no
  licences, so licenseConcluded and licenseDeclared are NOASSERTION;
* the Tinta Office bundles of ``image/tinta-bundles.toml`` (version, SHA-256, URL).

The licence of our own code: ``licenseDeclared`` is what the file says (``license``
of pyproject/Cargo, ``License:`` of a spec; otherwise the ``SPDX-License-Identifier``
header of that file); ``licenseConcluded`` is the licence LICENSING.md gives to the
folder, or the declared one when no folder rule fits. When the two disagree the
concluded licence is NOASSERTION and a warning says so. A licence that is not an SPDX
expression is NOASSERTION with a warning.

The output is deterministic: the same files give the same bytes. ``created`` comes
from SOURCE_DATE_EPOCH, else ``--created``, else the clock; the document namespace is
``https://vilnaos.com/spdx/vilna-<version>-<sha256 of the content without created>``.

``--check`` checks the built document: unique SPDXIDs; every relationship points to an
element that exists; one DESCRIBES; every package has a name, versionInfo and
downloadLocation and a licence field; every package that has a purl has a SHA-256 (a
git or unknown source is a warning); no package is left without a relationship; no
purl twice. Our own components have no checksum: they are built from this repo, the
file hash of what ships has to come from the build.

Not included: the openSUSE packages of the image (image/packages.yaml); the SBOM of
the image comes from OBS/KIWI. Only ``git ls-files`` is run; nothing is read from the
network.

Only ASCII is written (non-ASCII characters are ``\\u`` escapes), so the file is the same on any
terminal. Nothing is written when a lock file, spec or manifest cannot be read.

Exit code: 0 all good (warnings allowed), 1 problems found (nothing is written), 2 wrong call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

NOASSERTION = "NOASSERTION"
SPDX_VERSION = "SPDX-2.3"
NAMESPACE_BASE = "https://vilnaos.com/spdx/"
REPO_URL = "https://github.com/StDenis1980/VilnaOS"
SUPPLIER = "Organization: Vilna OS project"
TOOL = "Tool: vilna-tools-sbom"
ROOT_ID = "SPDXRef-Vilna-OS"
DEFAULT_VERSION = "0.0.0-dev"
LICENSING = "LICENSING.md"
BUNDLES = "image/tinta-bundles.toml"
SPEC_RE = re.compile(r"^obs/packages/([^/]+)/([^/]+)\.spec$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+~_-]*$")
CREATED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SPDX_ID_RE = re.compile(r"^SPDXRef-[A-Za-z0-9.\-]+$")
HEADER_RE = re.compile(r"SPDX-License-Identifier:\s*(.+?)\s*(?:\*/|-->)?\s*$", re.MULTILINE)
HEADER_LINES = 20
LICENSE_ID_RE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9.\-]*\+?|(?:DocumentRef-[A-Za-z0-9.\-]+:)?"
    r"LicenseRef-[A-Za-z0-9.\-]+)$"
)
SPDX_KEYWORDS = frozenset({"AND", "OR", "WITH"})
LICENSING_ROW = re.compile(r"^\|\s*\*\*([A-Za-z0-9.+\-]+)\*\*[^|]*\|(.*)\|\s*$")
FOLDER_IN_ROW = re.compile(r"`([A-Za-z0-9_.\-/]+/)`")
SECTIONS = frozenset(
    [
        "%package",
        "%description",
        "%prep",
        "%generate_buildrequires",
        "%conf",
        "%build",
        "%install",
        "%check",
        "%clean",
        "%files",
        "%changelog",
        "%pre",
        "%post",
        "%preun",
        "%postun",
        "%pretrans",
        "%posttrans",
        "%verifyscript",
        "%triggerin",
        "%triggerun",
        "%triggerpostun",
        "%trigger",
        "%filetriggerin",
        "%filetriggerun",
    ]
)
MACRO = re.compile(r"%\{(\w+)\}|%(\w+)")
RELATIONSHIP_TYPES = frozenset(
    [
        "AMENDS",
        "ANCESTOR_OF",
        "BUILD_DEPENDENCY_OF",
        "BUILD_TOOL_OF",
        "CONTAINED_BY",
        "CONTAINS",
        "COPY_OF",
        "DATA_FILE_OF",
        "DEPENDENCY_MANIFEST_OF",
        "DEPENDENCY_OF",
        "DEPENDS_ON",
        "DESCENDANT_OF",
        "DESCRIBED_BY",
        "DESCRIBES",
        "DEV_DEPENDENCY_OF",
        "DEV_TOOL_OF",
        "DISTRIBUTION_ARTIFACT",
        "DOCUMENTATION_OF",
        "DYNAMIC_LINK",
        "EXAMPLE_OF",
        "EXPANDED_FROM_ARCHIVE",
        "FILE_ADDED",
        "FILE_DELETED",
        "FILE_MODIFIED",
        "GENERATED_FROM",
        "GENERATES",
        "HAS_PREREQUISITE",
        "METAFILE_OF",
        "OPTIONAL_COMPONENT_OF",
        "OPTIONAL_DEPENDENCY_OF",
        "OTHER",
        "PACKAGE_OF",
        "PATCH_APPLIED",
        "PATCH_FOR",
        "PREREQUISITE_FOR",
        "PROVIDED_DEPENDENCY_OF",
        "REQUIREMENT_DESCRIPTION_FOR",
        "RUNTIME_DEPENDENCY_OF",
        "SPECIFICATION_FOR",
        "STATIC_LINK",
        "TEST_CASE_OF",
        "TEST_DEPENDENCY_OF",
        "TEST_OF",
        "TEST_TOOL_OF",
        "VARIANT_OF",
    ]
)

# Our component or a third-party package, as a hashable reference used by the edges.
Ref = tuple[str, ...]


@dataclass(frozen=True)
class Problem:
    path: str
    text: str
    warning: bool = False

    def __str__(self) -> str:
        return f"{self.path}: {'warning: ' if self.warning else ''}{self.text}"


@dataclass
class Component:
    """One of our own components: a Python project, a Cargo package or an RPM package."""

    kind: str  # "pyproject" | "cargo" | "rpm"
    name: str
    version: str
    declared: str
    concluded: str
    source: str  # the tracked manifest or spec
    spdx_id: str = ""

    @property
    def ref(self) -> Ref:
        return ("component", self.source)

    @property
    def folder(self) -> str:
        return folder_of(self.source)


@dataclass
class Dependency:
    """A third-party package, one per ecosystem, name and version."""

    ecosystem: str  # "pypi" | "cargo"
    name: str
    version: str
    download: str = NOASSERTION
    sha256: str | None = None
    hash_kind: str = ""  # "sdist" | "wheel" | "crate": where the hash comes from
    locks: set[str] = field(default_factory=set)
    spdx_id: str = ""

    @property
    def ref(self) -> Ref:
        return ("dependency", self.ecosystem, self.name, self.version)

    @property
    def purl(self) -> str:
        return purl(self.ecosystem, self.name, self.version)


@dataclass
class Node:
    """A package of a lock file; ``edges`` are indexes of the packages it depends on."""

    name: str
    version: str
    folder: str | None = None  # a local package: its folder (uv) or None (Cargo)
    local: bool = False
    dependency: Dependency | None = None
    edges: list[int] = field(default_factory=list)


@dataclass
class Bundle:
    ident: str
    name: str
    version: str
    url: str
    sha256: str | None
    repo: str
    tag: str
    spdx_id: str = ""


def folder_of(path: str) -> str:
    """The folder of a tracked path; the repo root is the empty string."""
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def inside(folder: str, parent: str) -> bool:
    return parent == "" or folder == parent or folder.startswith(parent + "/")


def normalize_pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def purl(ecosystem: str, name: str, version: str) -> str:
    """The package URL of a third-party package (pkg:pypi/name@version)."""
    clean = normalize_pypi(name) if ecosystem == "pypi" else name
    return f"pkg:{ecosystem}/{quote(clean, safe='')}@{quote(version, safe='')}"


def sanitize(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.\-]+", "-", text).strip("-") or "x"


# ---------------------------------------------------------------- licences


def split_license(expr: str) -> list[str] | None:
    """The tokens of an SPDX licence expression, or None when it is not one."""
    tokens = re.findall(r"\(|\)|[^\s()]+", expr)
    if not tokens:
        return None
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def ident() -> bool:
        nonlocal pos
        tok = peek()
        if tok is None or tok in SPDX_KEYWORDS or not LICENSE_ID_RE.match(tok):
            return False
        pos += 1
        return True

    def atom() -> bool:
        nonlocal pos
        if peek() == "(":
            pos += 1
            if not or_expr() or peek() != ")":
                return False
            pos += 1
            return True
        return ident()

    def with_expr() -> bool:
        nonlocal pos
        if not atom():
            return False
        if peek() == "WITH":
            pos += 1
            return ident()
        return True

    def and_expr() -> bool:
        nonlocal pos
        if not with_expr():
            return False
        while peek() == "AND":
            pos += 1
            if not with_expr():
                return False
        return True

    def or_expr() -> bool:
        nonlocal pos
        if not and_expr():
            return False
        while peek() == "OR":
            pos += 1
            if not and_expr():
                return False
        return True

    return tokens if or_expr() and pos == len(tokens) else None


def license_expression(text: str | None) -> str | None:
    """The licence expression with single spaces, or None if ``text`` is not one."""
    if text is None:
        return None
    expr = re.sub(r"\s+", " ", text.strip())
    return expr if split_license(expr) else None


def header_license(text: str) -> str | None:
    """The ``SPDX-License-Identifier`` of the first lines of a file."""
    head = "\n".join(text.splitlines()[:HEADER_LINES])
    found = HEADER_RE.search(head)
    return license_expression(found.group(1)) if found else None


def parse_licensing(text: str) -> list[tuple[str, str]]:
    """The folder rules of LICENSING.md: (folder with a slash, licence), first rule wins."""
    rules: list[tuple[str, str]] = []
    for line in text.splitlines():
        row = LICENSING_ROW.match(line)
        if not row:
            continue
        for folder in FOLDER_IN_ROW.findall(row.group(2)):
            if all(folder != known for known, _ in rules):
                rules.append((folder, row.group(1)))
    return rules


def rule_license(rules: Sequence[tuple[str, str]], path: str) -> str | None:
    """The licence LICENSING.md gives the folder of ``path`` (the longest folder wins)."""
    best: tuple[str, str] | None = None
    for folder, lic in rules:
        if path.startswith(folder) and (best is None or len(folder) > len(best[0])):
            best = (folder, lic)
    return best[1] if best else None


def conclude(declared: str, rule: str | None, path: str, problems: list[Problem]) -> str:
    """licenseConcluded: the folder's licence from LICENSING.md, or the declared one."""
    if rule is None:
        return declared
    if declared in (NOASSERTION, rule):
        return rule
    problems.append(
        Problem(
            path,
            f"the file declares {declared} but {LICENSING} gives this folder {rule}: "
            "licenseConcluded is NOASSERTION",
            True,
        )
    )
    return NOASSERTION


# ---------------------------------------------------------------- reading files


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, text=True
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def read_text(root: Path, rel: str, problems: list[Problem]) -> str | None:
    try:
        return (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        problems.append(Problem(rel, f"cannot read: {err}"))
        return None


def read_toml(root: Path, rel: str, problems: list[Problem]) -> tuple[dict, str] | None:
    text = read_text(root, rel, problems)
    if text is None:
        return None
    try:
        return tomllib.loads(text), text
    except tomllib.TOMLDecodeError as err:
        problems.append(Problem(rel, f"not valid TOML: {err}"))
        return None


# ---------------------------------------------------------------- our components


def declared_license(value: object, text: str, path: str, problems: list[Problem]) -> str:
    """The declared licence: the metadata value, else the file's SPDX header, else NOASSERTION."""
    raw = value
    if isinstance(raw, dict):
        raw = raw.get("text")
    if isinstance(raw, str) and raw.strip():
        expr = license_expression(raw.replace(" / ", " OR ").replace("/", " OR "))
        if expr:
            return expr
        problems.append(Problem(path, f"licence {raw!r} is not an SPDX expression", True))
    header = header_license(text)
    return header or NOASSERTION


def collect_pyproject(
    path: str, data: dict, text: str, rules: Sequence[tuple[str, str]], problems: list[Problem]
) -> Component | None:
    project = data.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("name"), str):
        problems.append(Problem(path, "no [project] with a name: skipped", True))
        return None
    version = project.get("version")
    if not isinstance(version, str) or not version:
        problems.append(Problem(path, "the version is not written in the file: NOASSERTION", True))
        version = NOASSERTION
    declared = declared_license(project.get("license"), text, path, problems)
    concluded = conclude(declared, rule_license(rules, path), path, problems)
    return Component("pyproject", project["name"], version, declared, concluded, path)


def workspace_package(path: str, manifests: dict[str, dict]) -> dict:
    """``[workspace.package]`` of the nearest Cargo.toml above (or at) ``path`` that has one."""
    folder = folder_of(path)
    while True:
        candidate = posixpath.join(folder, "Cargo.toml")
        data = manifests.get(candidate)
        if data is not None and isinstance(data.get("workspace"), dict):
            package = data["workspace"].get("package")
            return package if isinstance(package, dict) else {}
        if folder == "":
            return {}
        folder = folder_of(folder)


def inherited(package: dict, key: str, workspace: dict) -> object:
    value = package.get(key)
    if isinstance(value, dict) and value.get("workspace") is True:
        return workspace.get(key)
    return value


def collect_cargo(
    path: str,
    data: dict,
    text: str,
    manifests: dict[str, dict],
    rules: Sequence[tuple[str, str]],
    problems: list[Problem],
) -> Component | None:
    package = data.get("package")
    if not isinstance(package, dict):
        return None
    if not isinstance(package.get("name"), str):
        problems.append(Problem(path, "[package] has no name: skipped", True))
        return None
    workspace = workspace_package(path, manifests)
    version = inherited(package, "version", workspace)
    if not isinstance(version, str) or not version:
        problems.append(Problem(path, "no version (not even in the workspace): NOASSERTION", True))
        version = NOASSERTION
    declared = declared_license(inherited(package, "license", workspace), text, path, problems)
    concluded = conclude(declared, rule_license(rules, path), path, problems)
    return Component("cargo", package["name"], version, declared, concluded, path)


def expand(value: str, macros: dict[str, str]) -> str:
    """Expand the known macros of ``value``; unknown ones stay as they are."""
    for _ in range(5):
        new = MACRO.sub(lambda m: macros.get(m.group(1) or m.group(2), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


def spec_tags(text: str) -> dict[str, str]:
    """Name, Version and License of the main preamble of a spec, simple macros expanded."""
    macros: dict[str, str] = {}
    tags: dict[str, str] = {}
    for line in text.splitlines():
        words = line.split()
        if words and words[0] in SECTIONS:
            break
        define = re.match(r"^%(?:define|global)\s+([A-Za-z_]\w*)\s+(.*?)\s*$", line)
        if define:
            macros[define.group(1)] = expand(define.group(2), macros)
            continue
        tag = re.match(r"^(Name|Version|License):\s*(.*?)\s*$", line)
        if tag and tag.group(1) not in tags:
            value = expand(tag.group(2), macros)
            tags[tag.group(1)] = value
            macros[tag.group(1).lower()] = value
    return tags


def collect_spec(path: str, text: str, problems: list[Problem]) -> Component | None:
    tags = spec_tags(text)
    folder = SPEC_RE.match(path)
    name = tags.get("Name", "")
    if not name or "%" in name:
        problems.append(Problem(path, "no usable Name: skipped", True))
        return None
    if folder and name != folder.group(1):
        problems.append(
            Problem(path, f"Name {name} is not the folder name {folder.group(1)}", True)
        )
    version = tags.get("Version", "")
    if not version or "%" in version:
        problems.append(Problem(path, f"Version {version!r} is not known: NOASSERTION", True))
        version = NOASSERTION
    declared = NOASSERTION
    raw = tags.get("License", "")
    expr = license_expression(raw)
    if expr:
        declared = expr
    else:
        problems.append(Problem(path, f"License {raw!r} is not an SPDX expression", True))
        declared = header_license(text) or NOASSERTION
    return Component("rpm", name, version, declared, declared, path)


def collect_components(
    root: Path, files: Sequence[str], rules: Sequence[tuple[str, str]], problems: list[Problem]
) -> list[Component]:
    components: list[Component] = []
    cargo: dict[str, tuple[dict, str]] = {}
    for path in files:
        name = PurePosixPath(path).name
        if name == "pyproject.toml":
            loaded = read_toml(root, path, problems)
            if loaded:
                comp = collect_pyproject(path, loaded[0], loaded[1], rules, problems)
                components.extend([comp] if comp else [])
        elif name == "Cargo.toml":
            loaded = read_toml(root, path, problems)
            if loaded:
                cargo[path] = loaded
        elif SPEC_RE.match(path):
            text = read_text(root, path, problems)
            comp = collect_spec(path, text, problems) if text is not None else None
            components.extend([comp] if comp else [])
    manifests = {path: data for path, (data, _) in cargo.items()}
    for path, (data, text) in sorted(cargo.items()):
        comp = collect_cargo(path, data, text, manifests, rules, problems)
        components.extend([comp] if comp else [])
    return sorted(components, key=lambda c: (c.kind, c.name, c.source))


# ---------------------------------------------------------------- lock files


def lock_dependency(
    ecosystem: str, name: str, version: str, download: str, sha: str | None, kind: str, lock: str
) -> Dependency:
    return Dependency(
        ecosystem,
        normalize_pypi(name) if ecosystem == "pypi" else name,
        version,
        download,
        sha,
        kind if sha else "",
        {lock},
    )


def uv_hash(entry: object) -> tuple[str, str] | None:
    """(url, sha256) of a ``sdist``/``wheels`` entry of uv.lock."""
    if not isinstance(entry, dict):
        return None
    url, digest = entry.get("url"), entry.get("hash")
    if isinstance(url, str) and isinstance(digest, str) and digest.startswith("sha256:"):
        sha = digest.removeprefix("sha256:")
        if SHA256_RE.match(sha):
            return url, sha
    return None


def uv_source_url(source: dict) -> str:
    if isinstance(source.get("git"), str):
        return "git+" + source["git"]
    if isinstance(source.get("url"), str):
        return source["url"]
    return NOASSERTION


def table_list(data: dict, key: str) -> list[dict]:
    """The tables of an array of tables (``[[package]]``); anything else is no table."""
    value = data.get(key)
    return [t for t in value if isinstance(t, dict)] if isinstance(value, list) else []


def parse_uv_lock(path: str, data: dict, problems: list[Problem]) -> list[Node]:
    """The packages of a uv.lock as nodes; the dependencies are resolved to node indexes."""
    packages = table_list(data, "package")
    lock_folder = folder_of(path)
    nodes: list[Node] = []
    by_name: dict[str, list[int]] = {}
    for pkg in packages:
        name, source = pkg.get("name"), pkg.get("source")
        if not isinstance(name, str) or not isinstance(source, dict):
            problems.append(Problem(path, "a [[package]] without a name or a source", False))
            nodes.append(Node("?", ""))
            continue
        by_name.setdefault(normalize_pypi(name), []).append(len(nodes))
        version = pkg.get("version") if isinstance(pkg.get("version"), str) else ""
        local = next((k for k in ("editable", "virtual", "directory") if k in source), None)
        if local:
            target = posixpath.normpath(posixpath.join(lock_folder, str(source[local])))
            nodes.append(Node(name, version, "" if target == "." else target, True))
            continue
        if not version:
            problems.append(Problem(path, f"package {name} has no version: skipped", True))
            nodes.append(Node(name, ""))
            continue
        found = uv_hash(pkg.get("sdist")), "sdist"
        if found[0] is None:
            wheels = pkg.get("wheels") if isinstance(pkg.get("wheels"), list) else []
            found = (uv_hash(wheels[0]) if wheels else None), "wheel"
        url, sha = found[0] if found[0] else (uv_source_url(source), None)
        dep = lock_dependency("pypi", name, version, url, sha, found[1], path)
        nodes.append(Node(name, version, None, False, dep))
    for index, pkg in enumerate(packages):
        for entry in uv_dependency_entries(pkg):
            nodes[index].edges.extend(resolve_uv(entry, by_name, nodes, packages, path, problems))
    return nodes


def uv_dependency_entries(pkg: dict) -> list[dict]:
    """Runtime, extra and dev dependencies of a package of uv.lock."""
    groups: list[object] = [pkg.get("dependencies", [])]
    for key in ("optional-dependencies", "dev-dependencies"):
        table = pkg.get(key)
        if isinstance(table, dict):
            groups.extend(table.values())
    return [e for g in groups if isinstance(g, list) for e in g if isinstance(e, dict)]


def resolve_uv(
    entry: dict,
    by_name: dict[str, list[int]],
    nodes: list[Node],
    packages: list[dict],
    path: str,
    problems: list[Problem],
) -> list[int]:
    name = entry.get("name")
    candidates = by_name.get(normalize_pypi(name), []) if isinstance(name, str) else []
    if len(candidates) > 1 and isinstance(entry.get("version"), str):
        candidates = [i for i in candidates if nodes[i].version == entry["version"]]
    if len(candidates) > 1 and isinstance(entry.get("source"), dict):
        candidates = [i for i in candidates if packages[i].get("source") == entry["source"]]
    if not candidates:
        problems.append(Problem(path, f"dependency {name!r} is not a package of the lock"))
    return candidates


def cargo_url(name: str, version: str, source: str) -> str:
    if source.startswith("git+"):
        return source
    if source in (
        "registry+https://github.com/rust-lang/crates.io-index",
        "sparse+https://index.crates.io/",
    ):
        crate, number = quote(name, safe=""), quote(version, safe="")
        return f"https://crates.io/api/v1/crates/{crate}/{number}/download"
    return NOASSERTION


def parse_cargo_lock(path: str, data: dict, problems: list[Problem]) -> list[Node]:
    packages = table_list(data, "package")
    nodes: list[Node] = []
    by_name: dict[str, list[int]] = {}
    for pkg in packages:
        name, version = pkg.get("name"), pkg.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            problems.append(Problem(path, "a [[package]] without a name or a version"))
            nodes.append(Node("?", ""))
            continue
        by_name.setdefault(name, []).append(len(nodes))
        source = pkg.get("source")
        if not isinstance(source, str):
            nodes.append(Node(name, version, None, True))
            continue
        checksum = pkg.get("checksum")
        sha = checksum if isinstance(checksum, str) and SHA256_RE.match(checksum) else None
        dep = lock_dependency(
            "cargo", name, version, cargo_url(name, version, source), sha, "crate", path
        )
        nodes.append(Node(name, version, None, False, dep))
    for index, pkg in enumerate(packages):
        deps = pkg.get("dependencies")
        for text in deps if isinstance(deps, list) else []:
            nodes[index].edges.extend(
                resolve_cargo(str(text), by_name, nodes, packages, path, problems)
            )
    return nodes


def resolve_cargo(
    text: str,
    by_name: dict[str, list[int]],
    nodes: list[Node],
    packages: list[dict],
    path: str,
    problems: list[Problem],
) -> list[int]:
    """A Cargo.lock dependency is ``name``, ``name version`` or ``name version (source)``."""
    parts = text.split(" ", 2)
    candidates = by_name.get(parts[0], [])
    if len(candidates) > 1 and len(parts) > 1:
        candidates = [i for i in candidates if nodes[i].version == parts[1]]
    if len(candidates) > 1 and len(parts) > 2:
        wanted = parts[2].strip("()")
        candidates = [i for i in candidates if packages[i].get("source") == wanted]
    if not candidates:
        problems.append(Problem(path, f"dependency {text!r} is not a package of the lock"))
    return candidates


def closure(nodes: list[Node], start: int) -> list[int]:
    """Every node reachable from ``start`` (not ``start`` itself), in index order."""
    seen = {start}
    todo = [start]
    while todo:
        for nxt in nodes[todo.pop()].edges:
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    seen.discard(start)
    return sorted(seen)


def merge_dependency(
    known: dict[Ref, Dependency], new: Dependency, problems: list[Problem]
) -> Dependency:
    """Keep one element per package; two locks with different hashes are a problem."""
    old = known.setdefault(new.ref, new)
    if old is new:
        return old
    lock = next(iter(new.locks))
    old.locks |= new.locks
    if not new.sha256:
        return old
    if not old.sha256 or (old.hash_kind == "wheel" and new.hash_kind == "sdist"):
        old.sha256, old.hash_kind, old.download = new.sha256, new.hash_kind, new.download
    elif old.sha256 != new.sha256 and old.hash_kind == new.hash_kind:
        problems.append(
            Problem(
                lock,
                f"{new.ecosystem} package {new.name} {new.version} has another {new.hash_kind} "
                "hash in a lock read before",
                new.hash_kind == "wheel",  # the first wheel may differ between locks: a warning
            )
        )
    return old


def link_locks(
    root: Path,
    files: Sequence[str],
    components: Sequence[Component],
    problems: list[Problem],
) -> tuple[dict[Ref, Dependency], set[tuple[Ref, Ref]]]:
    """Read the lock files; return the third-party packages and our DEPENDS_ON edges."""
    known: dict[Ref, Dependency] = {}
    edges: set[tuple[Ref, Ref]] = set()
    for path in files:
        name = PurePosixPath(path).name
        if name not in ("uv.lock", "Cargo.lock"):
            continue
        loaded = read_toml(root, path, problems)
        if not loaded:
            continue
        nodes = (parse_uv_lock if name == "uv.lock" else parse_cargo_lock)(
            path, loaded[0], problems
        )
        lock_folder = folder_of(path)
        owners: dict[int, Component] = {}
        for index, node in enumerate(nodes):
            if node.dependency is not None:
                nodes[index].dependency = merge_dependency(known, node.dependency, problems)
            elif node.local:
                owner = find_owner(node, name, lock_folder, components)
                if owner is None:
                    problems.append(
                        Problem(path, f"local package {node.name} has no tracked manifest", True)
                    )
                else:
                    owners[index] = owner
        for index, owner in owners.items():
            if not inside(owner.folder, lock_folder):
                continue  # a path dependency of another project: not a member of this lock
            for reached in closure(nodes, index):
                target = nodes[reached].dependency or owners.get(reached)
                if target is not None and target.ref != owner.ref:
                    edges.add((owner.ref, target.ref))
    return known, edges


def find_owner(
    node: Node, lock_name: str, lock_folder: str, comps: Sequence[Component]
) -> Component | None:
    """The component of a local package of a lock file."""
    for comp in comps:
        if lock_name == "uv.lock" and comp.kind == "pyproject" and comp.folder == node.folder:
            return comp
        if (
            lock_name == "Cargo.lock"
            and comp.kind == "cargo"
            and comp.name == node.name
            and inside(comp.folder, lock_folder)
        ):
            return comp
    return None


# ---------------------------------------------------------------- bundles


def collect_bundles(root: Path, files: Sequence[str], problems: list[Problem]) -> list[Bundle]:
    if BUNDLES not in files:
        return []
    loaded = read_toml(root, BUNDLES, problems)
    if not loaded:
        return []
    bundles: list[Bundle] = []
    raw = loaded[0].get("bundle")
    if not isinstance(raw, list) or not raw:
        problems.append(Problem(BUNDLES, "no [[bundle]] entries"))
        return []
    for n, entry in enumerate(raw, 1):
        keys = ("id", "name", "repo", "tag", "file", "sha256")
        fields = {k: entry.get(k) for k in keys} if isinstance(entry, dict) else {}
        if len(fields) != len(keys) or not all(isinstance(v, str) and v for v in fields.values()):
            problems.append(
                Problem(
                    BUNDLES,
                    f"bundle {n}: id, name, repo, tag, file, sha256 "
                    "must be non-empty strings: skipped",
                )
            )
            continue
        sha = fields["sha256"] if SHA256_RE.match(fields["sha256"]) else None
        if sha is None:
            problems.append(Problem(BUNDLES, f"bundle {fields['id']}: sha256 is not 64 hex digits"))
        url = f"https://github.com/{fields['repo']}/releases/download/{fields['tag']}/{fields['file']}"
        bundles.append(
            Bundle(
                fields["id"],
                fields["name"],
                fields["tag"].removeprefix("v"),
                url,
                sha,
                fields["repo"],
                fields["tag"],
            )
        )
    return sorted(bundles, key=lambda b: b.ident)


# ---------------------------------------------------------------- the document


class IdPool:
    """Unique SPDXIDs: ``SPDXRef-<prefix>-<name>-<version>``, a number added on a clash."""

    def __init__(self) -> None:
        self.used = {"SPDXRef-DOCUMENT", ROOT_ID}

    def make(self, prefix: str, name: str, version: str) -> str:
        base = f"SPDXRef-{prefix}-{sanitize(name)}-{sanitize(version)}"
        candidate, number = base, 2
        while candidate in self.used:
            candidate, number = f"{base}-{number}", number + 1
        self.used.add(candidate)
        return candidate


def checksum_list(sha: str | None) -> list[dict]:
    return [{"algorithm": "SHA256", "checksumValue": sha}] if sha else []


def own_package(comp: Component) -> dict:
    pkg: dict = {
        "SPDXID": comp.spdx_id,
        "name": comp.name,
        "versionInfo": comp.version,
        "supplier": SUPPLIER,
        "downloadLocation": NOASSERTION,
        "filesAnalyzed": False,
        "licenseConcluded": comp.concluded,
        "licenseDeclared": comp.declared,
        "copyrightText": NOASSERTION,
        "sourceInfo": f"{comp.kind} component, from {comp.source}",
    }
    return pkg


def third_party_package(dep: Dependency) -> dict:
    pkg: dict = {
        "SPDXID": dep.spdx_id,
        "name": dep.name,
        "versionInfo": dep.version,
        "supplier": NOASSERTION,
        "downloadLocation": dep.download,
        "filesAnalyzed": False,
        "licenseConcluded": NOASSERTION,
        "licenseDeclared": NOASSERTION,
        "copyrightText": NOASSERTION,
        "externalRefs": [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": dep.purl,
            }
        ],
        "sourceInfo": "locked in " + ", ".join(sorted(dep.locks)),
    }
    if dep.sha256:
        pkg["checksums"] = checksum_list(dep.sha256)
    return pkg


def bundle_package(bundle: Bundle) -> dict:
    owner_repo = bundle.repo.lower()
    pkg: dict = {
        "SPDXID": bundle.spdx_id,
        "name": bundle.name,
        "versionInfo": bundle.version,
        "supplier": NOASSERTION,
        "downloadLocation": bundle.url,
        "filesAnalyzed": False,
        "licenseConcluded": NOASSERTION,
        "licenseDeclared": NOASSERTION,
        "copyrightText": NOASSERTION,
        "externalRefs": [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": f"pkg:github/{owner_repo}@{quote(bundle.tag, safe='')}",
            }
        ],
        "primaryPackagePurpose": "APPLICATION",
        "sourceInfo": f"Flatpak bundle {bundle.ident}, pinned in {BUNDLES}",
    }
    if bundle.sha256:
        pkg["checksums"] = checksum_list(bundle.sha256)
    return pkg


def root_package(version: str) -> dict:
    return {
        "SPDXID": ROOT_ID,
        "name": "Vilna OS",
        "versionInfo": version,
        "supplier": SUPPLIER,
        "downloadLocation": REPO_URL,
        "filesAnalyzed": False,
        "licenseConcluded": NOASSERTION,
        "licenseDeclared": NOASSERTION,
        "copyrightText": NOASSERTION,
        "primaryPackagePurpose": "OPERATING-SYSTEM",
        "sourceInfo": "the components of this repository; the openSUSE packages of the image "
        "are not listed",
    }


def relationship(source: str, kind: str, target: str) -> dict:
    return {"spdxElementId": source, "relationshipType": kind, "relatedSpdxElement": target}


def content_hash(doc: dict) -> str:
    """SHA-256 of the document without ``created`` and the namespace (they depend on time)."""
    body = json.loads(json.dumps(doc))
    body["creationInfo"].pop("created", None)
    body.pop("documentNamespace", None)
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build(
    root: Path, files: Iterable[str], version: str, created: str
) -> tuple[dict, list[Problem]]:
    """The SPDX document for the tracked ``files`` under ``root``, and what went wrong."""
    problems: list[Problem] = []
    tracked = sorted(set(files))
    rules: list[tuple[str, str]] = []
    if LICENSING in tracked:
        text = read_text(root, LICENSING, problems)
        rules = parse_licensing(text) if text is not None else []
    components = collect_components(root, tracked, rules, problems)
    dependencies, edges = link_locks(root, tracked, components, problems)
    bundles = collect_bundles(root, tracked, problems)

    ids = IdPool()
    for comp in components:
        comp.spdx_id = ids.make(comp.kind.capitalize(), comp.name, comp.version)
    for dep in sorted(dependencies.values(), key=lambda d: (d.ecosystem, d.name, d.version)):
        dep.spdx_id = ids.make(dep.ecosystem.capitalize(), dep.name, dep.version)
    for bundle in bundles:
        bundle.spdx_id = ids.make("Bundle", bundle.ident, bundle.version)

    spdx_of: dict[Ref, str] = {c.ref: c.spdx_id for c in components}
    spdx_of.update({d.ref: d.spdx_id for d in dependencies.values()})
    packages = [root_package(version)]
    packages += [own_package(c) for c in components]
    packages += [
        third_party_package(d) for d in sorted(dependencies.values(), key=lambda d: d.spdx_id)
    ]
    packages += [bundle_package(b) for b in bundles]
    relationships = [relationship("SPDXRef-DOCUMENT", "DESCRIBES", ROOT_ID)]
    relationships += [relationship(ROOT_ID, "CONTAINS", c.spdx_id) for c in components]
    relationships += [relationship(ROOT_ID, "CONTAINS", b.spdx_id) for b in bundles]
    relationships += [
        relationship(spdx_of[a], "DEPENDS_ON", spdx_of[b])
        for a, b in sorted(edges, key=lambda e: (spdx_of[e[0]], spdx_of[e[1]]))
    ]
    doc: dict = {
        "spdxVersion": SPDX_VERSION,
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"Vilna OS {version}",
        "creationInfo": {
            "created": created,
            "creators": [TOOL],
            "comment": "Built from the lock files, spec files, manifests and bundle pins "
            "tracked in the repository; the packages of the image are not included.",
        },
        "packages": packages,
        "relationships": relationships,
    }
    doc["documentNamespace"] = (
        f"{NAMESPACE_BASE}vilna-{quote(version, safe='')}-{content_hash(doc)}"
    )
    return doc, problems


# ---------------------------------------------------------------- the check


def check_package(pkg: object, where: str) -> list[Problem]:
    if not isinstance(pkg, dict):
        return [Problem(where, "a package must be an object")]
    problems: list[Problem] = []
    label = f"{where} ({pkg.get('SPDXID', '?')})"
    for key in ("name", "versionInfo", "downloadLocation"):
        value = pkg.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(Problem(label, f"{key} is missing or empty"))
    for key in ("licenseConcluded", "licenseDeclared"):
        value = pkg.get(key)
        if not isinstance(value, str) or not (
            value in (NOASSERTION, "NONE") or license_expression(value)
        ):
            problems.append(
                Problem(label, f"{key} must be NOASSERTION, NONE or an SPDX expression")
            )
    sums = pkg.get("checksums", [])
    for item in sums if isinstance(sums, list) else []:
        if not (
            isinstance(item, dict)
            and item.get("algorithm") == "SHA256"
            and isinstance(item.get("checksumValue"), str)
            and SHA256_RE.match(item["checksumValue"])
        ):
            problems.append(
                Problem(label, f"checksum is not a SHA256 of 64 lowercase hex: {item!r}")
            )
    refs = pkg.get("externalRefs", [])
    purls = (
        [
            r.get("referenceLocator")
            for r in refs
            if isinstance(r, dict) and r.get("referenceType") == "purl"
        ]
        if isinstance(refs, list)
        else []
    )
    if purls and not sums:
        known = str(pkg.get("downloadLocation", "")).startswith(("http://", "https://"))
        problems.append(Problem(label, "no checksum for a package with a purl", not known))
    return problems


def check_relationships(doc: dict, ids: set[str]) -> list[Problem]:
    problems: list[Problem] = []
    rels = doc.get("relationships")
    if not isinstance(rels, list):
        return [Problem("relationships", "must be a list")]
    seen: set[tuple[str, str, str]] = set()
    touched: set[str] = set()
    describes = 0
    for n, rel in enumerate(rels):
        where = f"relationships[{n}]"
        if not isinstance(rel, dict):
            problems.append(Problem(where, "a relationship must be an object"))
            continue
        source, kind, target = (
            rel.get("spdxElementId"),
            rel.get("relationshipType"),
            rel.get("relatedSpdxElement"),
        )
        if not isinstance(kind, str) or kind not in RELATIONSHIP_TYPES:
            problems.append(Problem(where, f"unknown relationshipType {kind!r}"))
        for role, element in (("spdxElementId", source), ("relatedSpdxElement", target)):
            if role == "relatedSpdxElement" and element in ("NONE", "NOASSERTION"):
                continue
            if not isinstance(element, str) or element not in ids:
                problems.append(
                    Problem(where, f"{role} {element!r} is not an element of the document")
                )
        key = (str(source), str(kind), str(target))
        if key in seen:
            problems.append(Problem(where, f"{source} {kind} {target} is listed twice"))
        seen.add(key)
        touched.update(str(x) for x in (source, target))
        describes += kind == "DESCRIBES" and source == "SPDXRef-DOCUMENT"
    if describes != 1:
        problems.append(Problem("relationships", f"need exactly one DESCRIBES, found {describes}"))
    for ident in sorted(ids - touched):
        problems.append(Problem(ident, "is in no relationship"))
    return problems


def check_document(doc: object) -> list[Problem]:
    """The integrity of an SPDX document (see the module docstring)."""
    if not isinstance(doc, dict):
        return [Problem("document", "must be a JSON object")]
    problems: list[Problem] = []
    if doc.get("spdxVersion") != SPDX_VERSION:
        problems.append(Problem("spdxVersion", f"must be {SPDX_VERSION}"))
    if doc.get("dataLicense") != "CC0-1.0":
        problems.append(Problem("dataLicense", "must be CC0-1.0"))
    if doc.get("SPDXID") != "SPDXRef-DOCUMENT":
        problems.append(Problem("SPDXID", "the document's SPDXID must be SPDXRef-DOCUMENT"))
    if not isinstance(doc.get("name"), str) or not doc["name"]:
        problems.append(Problem("name", "is missing"))
    namespace = doc.get("documentNamespace")
    if not isinstance(namespace, str) or not namespace.startswith("https://") or "#" in namespace:
        problems.append(Problem("documentNamespace", "must be an https URL without a fragment"))
    info = doc.get("creationInfo")
    created = info.get("created") if isinstance(info, dict) else None
    if not isinstance(created, str) or not CREATED_RE.match(created):
        problems.append(Problem("creationInfo.created", "must look like 2026-11-27T09:00:00Z"))
    if not isinstance(info, dict) or not info.get("creators"):
        problems.append(Problem("creationInfo.creators", "is missing"))
    packages = doc.get("packages")
    if not isinstance(packages, list) or not packages:
        return problems + [Problem("packages", "must be a non-empty list")]
    ids: set[str] = {"SPDXRef-DOCUMENT"}
    purls: dict[str, str] = {}
    for n, pkg in enumerate(packages):
        where = f"packages[{n}]"
        problems.extend(check_package(pkg, where))
        if not isinstance(pkg, dict):
            continue
        ident = pkg.get("SPDXID")
        if not isinstance(ident, str) or not SPDX_ID_RE.match(ident):
            problems.append(
                Problem(where, f"SPDXID {ident!r} must look like SPDXRef-<letters, digits, . or ->")
            )
        elif ident in ids:
            problems.append(Problem(where, f"SPDXID {ident} is used twice"))
        else:
            ids.add(ident)
        for ref in pkg.get("externalRefs", []) if isinstance(pkg.get("externalRefs"), list) else []:
            if isinstance(ref, dict) and ref.get("referenceType") == "purl":
                locator = str(ref.get("referenceLocator"))
                if locator in purls and purls[locator] != ident:
                    problems.append(Problem(where, f"purl {locator} is already {purls[locator]}"))
                purls[locator] = str(ident)
    return problems + check_relationships(doc, ids)


# ---------------------------------------------------------------- command line


def created_time(created_arg: str | None, environ: dict[str, str], now: datetime) -> str:
    """SOURCE_DATE_EPOCH, else ``--created``, else the clock. Raises ValueError."""
    epoch = environ.get("SOURCE_DATE_EPOCH", "").strip()
    if epoch:
        try:
            moment = datetime.fromtimestamp(int(epoch), tz=UTC)
        except (ValueError, OverflowError, OSError) as err:
            raise ValueError(f"SOURCE_DATE_EPOCH={epoch!r} is not a Unix time") from err
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    if created_arg is not None:
        try:
            datetime.strptime(created_arg, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as err:
            raise ValueError(
                f"--created {created_arg!r} must look like 2026-11-27T09:00:00Z"
            ) from err
        return created_arg
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def render(doc: dict) -> str:
    return json.dumps(doc, indent=2) + "\n"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sbom.py", description="Build the SPDX 2.3 JSON SBOM of Vilna OS from the repo."
    )
    p.add_argument("--version", default=DEFAULT_VERSION, help="version of Vilna OS")
    p.add_argument("--out", metavar="FILE", help="write the document here, not to stdout")
    p.add_argument("--created", metavar="TIME", help="created time, like 2026-11-27T09:00:00Z")
    p.add_argument("--check", action="store_true", help="check the document's integrity (CI)")
    p.add_argument(
        "--root", metavar="DIR", default=str(ROOT), help="the repository (default: this one)"
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
    except SystemExit as stop:
        return 0 if stop.code == 0 else 2
    if not VERSION_RE.match(args.version):
        print(
            f"--version {args.version!r}: use letters, digits and . + ~ _ - only", file=sys.stderr
        )
        return 2
    try:
        created = created_time(args.created, dict(os.environ), datetime.now(UTC))
    except ValueError as err:
        print(err, file=sys.stderr)
        return 2
    root = Path(args.root)
    try:
        files = tracked_files(root)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files in {root}: {err}", file=sys.stderr)
        return 2
    doc, problems = build(root, files, args.version, created)
    problems += check_document(doc)
    for problem in problems:
        print(problem, file=sys.stderr)
    errors = sum(not p.warning for p in problems)
    packages = len(doc["packages"])
    if errors:
        print(
            f"SBOM not written: {errors} errors, {len(problems) - errors} warnings", file=sys.stderr
        )
        return 1
    if args.out:
        try:
            Path(args.out).write_text(render(doc), encoding="utf-8", newline="\n")
        except OSError as err:
            print(f"cannot write {args.out}: {err}", file=sys.stderr)
            return 2
    elif not args.check:
        sys.stdout.write(render(doc))
    if args.check or args.out:
        print(
            f"SBOM ok: {packages} packages, {len(doc['relationships'])} relationships, "
            f"{len(problems)} warnings",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
