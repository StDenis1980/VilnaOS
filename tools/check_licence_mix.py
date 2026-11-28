#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check that the licences of the repo can be mixed (CRA, supply chain).

LICENSING.md gives most of the repo GPL-3.0-or-later and the shared components
(``privacy-gateway/``, ``mcp/``, ``sdk/``) Apache-2.0. Those must stay usable outside
Vilna, so they must not need GPL code. The tool reads the repo the way ``tools/sbom.py``
does (it imports ``collect_components``, ``link_locks``, ``parse_licensing`` and the
licence helpers of that file; nothing is copied) and checks:

* ``apache-gpl-dep``: an Apache-2.0 component reaches another component of the repo that
  is GPL-only (through ``uv.lock``/``Cargo.lock``, through ``[project].dependencies``,
  ``[dependency-groups]``, ``[tool.uv.sources]`` or a Cargo ``path`` dependency, also
  through other components). An error, with the chain.
* ``apache-gpl-third``: an Apache-2.0 component reaches a third-party package whose
  licence is known and is GPL/AGPL-only. A licence of the lock files comes from the
  ``license`` or ``license-expression`` key of the package, when the lock has one (today
  ``uv.lock`` and ``Cargo.lock`` have none). An error.
* ``apache-gpl-import``: a Python file of an Apache-2.0 component imports a package that a
  GPL-only component of the repo ships. An error in the code, a warning in ``tests/``.
* ``apache-gpl-header``: a file in a folder that LICENSING.md gives to Apache-2.0 (or a
  component declared as GPL there) carries a GPL-only ``SPDX-License-Identifier``. An error.
* ``apache-licence-file``: a folder that LICENSING.md gives to Apache-2.0 has no tracked
  ``LICENSE`` with the Apache text. An error (LICENSING.md says every such folder has one).
* ``gpl2-only``: a GPL-3.0 component of the repo reaches a third-party package that is
  GPL-2.0-only (not "or later"): the two cannot be combined. An error.
* ``rpm-requires-gpl`` (a warning): the ``Requires:`` of an Apache-2.0 RPM names a GPL-only
  RPM of the repo. ``Recommends:`` and ``Suggests:`` are not looked at.
* ``licence-review`` (a warning): a licence that needs a human look (a GPL licence with an
  exception, EPL/CDDL next to GPL, non-commercial or source-available licences, CC-BY-SA in
  code).
* ``unknown-licence`` (a warning): the licence of an own component is not an SPDX
  expression or not known to this check; for third-party packages the lock files carry no
  licence, so they are listed as "unknown" (one warning per lock file, the list on stdout).

Only a sure violation is an error: an expression such as ``MIT OR GPL-3.0-or-later`` is fine
for an Apache-2.0 component, a licence this tool does not know is "unknown", not an error.
Known old violations are warnings (see ``LEGACY``; none today). The problems that
``tools/sbom.py`` finds while reading files are repeated here.

Only ``git ls-files`` is run. Nothing is read from the network, nothing is written.

Run: python3 tools/check_licence_mix.py [DIR]
Without an argument the repo that holds this file is checked. With DIR the tracked files of
the git repo in DIR are checked.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import ast
import posixpath
import re
import subprocess
import os
import sys
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

import sbom  # noqa: E402  (a neighbour in tools/, as check_obs imports check_spec)

APACHE = "Apache-2.0"
BINARY_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".gz", ".whl", ".ttf", ".woff2"}
)
USAGE = "usage: python3 tools/check_licence_mix.py [DIR]"

# Known old violations: (path, rule) -> why it is still there. None today.
LEGACY: dict[tuple[str, str], str] = {}

# ---------------------------------------------------------------- the licence tables

OK, REVIEW, UNKNOWN, BAD = 0, 1, 2, 3  # the order matters: AND takes the worst, OR the best


def ids(text: str) -> frozenset[str]:
    return frozenset(word.lower() for word in text.split())


PERMISSIVE = ids(
    "0BSD Apache-2.0 Apache-1.1 BlueOak-1.0.0 BSD-1-Clause BSD-2-Clause BSD-3-Clause "
    "BSD-3-Clause-Clear BSL-1.0 CC0-1.0 CC-BY-3.0 CC-BY-4.0 HPND ISC MIT MIT-0 NCSA "
    "Python-2.0 PSF-2.0 Unicode-3.0 Unicode-DFS-2016 Unlicense WTFPL X11 Zlib"
)
WEAK = ids(
    "LGPL-2.0-only LGPL-2.0-or-later LGPL-2.1-only LGPL-2.1-or-later LGPL-3.0-only "
    "LGPL-3.0-or-later MPL-1.1 MPL-2.0 EPL-1.0 EPL-2.0 CDDL-1.0 CDDL-1.1"
)
STRONG = ids(
    "GPL-1.0-only GPL-1.0-or-later GPL-2.0-only GPL-2.0-or-later GPL-3.0-only "
    "GPL-3.0-or-later AGPL-1.0-only AGPL-1.0-or-later AGPL-3.0-only AGPL-3.0-or-later"
)
# A human has to look: not usable under Apache-2.0 as it is, but not plainly GPL either.
RESTRICTED = ids(
    "BUSL-1.1 Elastic-2.0 SSPL-1.0 CC-BY-SA-3.0 CC-BY-SA-4.0 CC-BY-NC-3.0 CC-BY-NC-4.0 "
    "CC-BY-NC-SA-4.0 CC-BY-ND-4.0 OFL-1.1 Commons-Clause"
)
GPL2_ONLY = frozenset({"gpl-2.0-only"})
NOT_WITH_GPL3 = frozenset({"epl-1.0", "cddl-1.0", "cddl-1.1"})
FAMILY = re.compile(r"^(A?L?GPL)-(\d\.\d)$", re.IGNORECASE)


def normalise_id(lic: str) -> str:
    """The lower-case SPDX id; ``GPL-2.0+`` is ``gpl-2.0-or-later``, ``GPL-2.0`` is ``-only``."""
    plus = lic.endswith("+")
    base = lic[:-1] if plus else lic
    family = FAMILY.match(base)
    if family:
        base = f"{base}-{'or-later' if plus else 'only'}"
    return base.lower()


def category(lic: str, exception: str | None = None) -> str:
    """permissive | weak | strong | review | unknown (a GPL with an exception is review)."""
    ident = normalise_id(lic)
    if ident.startswith("licenseref-") or ":licenseref-" in ident:
        return "unknown"
    if ident in STRONG:
        return "review" if exception else "strong"
    if exception:
        return "review" if ident in WEAK or ident in PERMISSIVE else "unknown"
    if ident in PERMISSIVE:
        return "permissive"
    if ident in WEAK:
        return "weak"
    return "review" if ident in RESTRICTED else "unknown"


Tree = tuple  # ("id", licence, exception | None) | ("and" | "or", [Tree, ...])


def parse_expression(expr: str) -> Tree | None:
    """The tree of an SPDX expression, or None when ``expr`` is not one."""
    tokens = sbom.split_license(re.sub(r"\s+", " ", expr.strip())) if expr.strip() else None
    if tokens is None:
        return None
    pos = 0

    def atom() -> Tree:
        nonlocal pos
        if tokens[pos] == "(":
            pos += 1
            inner = either()
            pos += 1  # the closing bracket: split_license checked the shape
            return inner
        lic = tokens[pos]
        pos += 1
        if pos < len(tokens) and tokens[pos] == "WITH":
            pos += 2
            return ("id", lic, tokens[pos - 1])
        return ("id", lic, None)

    def both() -> Tree:
        nonlocal pos
        parts = [atom()]
        while pos < len(tokens) and tokens[pos] == "AND":
            pos += 1
            parts.append(atom())
        return parts[0] if len(parts) == 1 else ("and", parts)

    def either() -> Tree:
        nonlocal pos
        parts = [both()]
        while pos < len(tokens) and tokens[pos] == "OR":
            pos += 1
            parts.append(both())
        return parts[0] if len(parts) == 1 else ("or", parts)

    return either()


def atom_rank(lic: str, exception: str | None, target: str) -> int:
    kind = category(lic, exception)
    if kind == "unknown":
        return UNKNOWN
    if kind == "review":
        return REVIEW
    if target == "apache":
        return BAD if kind == "strong" else OK
    ident = normalise_id(lic)  # target "gpl3": only the plain clashes count
    if ident in GPL2_ONLY:
        return BAD
    return REVIEW if ident in NOT_WITH_GPL3 else OK


def rank(tree: Tree, target: str) -> int:
    if tree[0] == "id":
        return atom_rank(tree[1], tree[2], target)
    ranks = [rank(part, target) for part in tree[1]]
    return max(ranks) if tree[0] == "and" else min(ranks)


def fit(expr: str, target: str) -> int:
    """Can code under ``expr`` be used by a ``target`` ("apache" or "gpl3") component?"""
    tree = parse_expression(expr)
    return UNKNOWN if tree is None else rank(tree, target)


def licence_ids(expr: str) -> set[str]:
    tree = parse_expression(expr)
    found: set[str] = set()

    def walk(node: Tree) -> None:
        if node[0] == "id":
            found.add(normalise_id(node[1]))
        else:
            for part in node[1]:
                walk(part)

    if tree is not None:
        walk(tree)
    return found


def gpl_only(expr: str) -> bool:
    """A sure GPL/AGPL-only licence: Apache-2.0 code cannot use it."""
    return fit(expr, "apache") == BAD


# ---------------------------------------------------------------- problems


@dataclass(frozen=True)
class Problem:
    path: str
    rule: str
    text: str
    warning: bool = False

    def __str__(self) -> str:
        return f"{self.path}: {'warning: ' if self.warning else ''}{self.text}"


def problem(path: str, rule: str, text: str, warning: bool = False) -> Problem:
    """A problem; a known old one (see LEGACY) is only a warning."""
    if (path, rule) in LEGACY:
        return Problem(path, rule, f"LEGACY: {text}", True)
    return Problem(path, rule, text, warning)


@dataclass
class Report:
    problems: list[Problem] = field(default_factory=list)
    unknown: dict[tuple[str, str, str], set[str]] = field(default_factory=dict)
    components: int = 0
    third_party: int = 0

    @property
    def errors(self) -> int:
        return sum(not p.warning for p in self.problems)


# ---------------------------------------------------------------- reading the repo


def lock_licence(entry: dict) -> str | None:
    """The licence expression a lock package carries (``license`` or ``license-expression``)."""
    for key in ("license-expression", "license"):
        value = entry.get(key)
        if isinstance(value, dict):
            value = value.get("text")
        if isinstance(value, str):
            expr = sbom.license_expression(value)
            if expr:
                return expr
    return None


def lock_licences(
    root: Path, files: Sequence[str], problems: list[sbom.Problem]
) -> dict[tuple[str, str, str], str]:
    """(ecosystem, name, version) -> licence, for the lock packages that say one."""
    found: dict[tuple[str, str, str], str] = {}
    for path in files:
        name = PurePosixPath(path).name
        if name not in ("uv.lock", "Cargo.lock"):
            continue
        loaded = sbom.read_toml(root, path, problems)
        if not loaded:
            continue
        eco = "pypi" if name == "uv.lock" else "cargo"
        for pkg in sbom.table_list(loaded[0], "package"):
            pkg_name, version = pkg.get("name"), pkg.get("version")
            lic = lock_licence(pkg)
            if isinstance(pkg_name, str) and isinstance(version, str) and lic:
                key_name = sbom.normalize_pypi(pkg_name) if eco == "pypi" else pkg_name
                found[(eco, key_name, version)] = lic
    return found


REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def requirement_names(values: object) -> list[str]:
    """The project names of a list of PEP 508 requirements (anything else is skipped)."""
    names: list[str] = []
    for value in values if isinstance(values, list) else []:
        found = REQ_NAME.match(value) if isinstance(value, str) else None
        if found:
            names.append(sbom.normalize_pypi(found.group(1)))
    return names


def declared_python_names(data: dict) -> list[str]:
    project = data.get("project")
    project = project if isinstance(project, dict) else {}
    names = requirement_names(project.get("dependencies"))
    optional = project.get("optional-dependencies")
    for group in optional.values() if isinstance(optional, dict) else []:
        names += requirement_names(group)
    groups = data.get("dependency-groups")
    for group in groups.values() if isinstance(groups, dict) else []:
        names += requirement_names(group)
    return names


def declared_paths(data: dict, folder: str) -> list[str]:
    """The folders of the ``path`` dependencies of a manifest (uv sources and Cargo)."""
    found: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, dict) and isinstance(value.get("path"), str):
            found.append(posixpath.normpath(posixpath.join(folder, value["path"])))

    tool = data.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    sources = uv.get("sources") if isinstance(uv, dict) else None
    for value in sources.values() if isinstance(sources, dict) else []:
        add(value)
    tables: list[object] = []
    for key in ("dependencies", "dev-dependencies", "build-dependencies"):
        tables.append(data.get(key))
    workspace = data.get("workspace")
    tables.append(workspace.get("dependencies") if isinstance(workspace, dict) else None)
    targets = data.get("target")
    for target in targets.values() if isinstance(targets, dict) else []:
        for key in ("dependencies", "dev-dependencies", "build-dependencies"):
            tables.append(target.get(key) if isinstance(target, dict) else None)
    for table in tables:
        for value in table.values() if isinstance(table, dict) else []:
            add(value)
    return found


def spec_requires(text: str) -> list[str]:
    """The package names of the ``Requires:`` lines of the main preamble of a spec."""
    names: list[str] = []
    for line in text.splitlines():
        words = line.split()
        if words and words[0] in sbom.SECTIONS:
            break
        found = re.match(r"^Requires(?:\([^)]*\))?:\s*(.+?)\s*$", line)
        if not found:
            continue
        value = re.sub(r"%\{[^}]*\}|%\w+", " ", found.group(1))
        tokens = re.findall(r"[^\s,]+", value)
        index = 0
        while index < len(tokens):
            name = tokens[index]
            index += 1
            if index + 1 < len(tokens) and tokens[index] in ("<", ">", "=", "<=", ">=", "=="):
                index += 2
            if name not in names:
                names.append(name)
    return names


@dataclass
class Graph:
    """The edges between our own components, and from them to third-party packages."""

    own: dict[sbom.Ref, sbom.Component]
    deps: dict[sbom.Ref, sbom.Dependency]
    edges: dict[sbom.Ref, set[sbom.Ref]] = field(default_factory=dict)
    via: dict[tuple[sbom.Ref, sbom.Ref], str] = field(default_factory=dict)

    def add(self, source: sbom.Ref, target: sbom.Ref, how: str) -> None:
        if source != target:
            self.edges.setdefault(source, set()).add(target)
            self.via.setdefault((source, target), how)


def label(comp: sbom.Component) -> str:
    return f"{comp.name} ({comp.source})"


def effective(comp: sbom.Component) -> str:
    """The licence to judge by: the concluded one, else the declared one."""
    return comp.concluded if comp.concluded != sbom.NOASSERTION else comp.declared


def build_graph(
    root: Path,
    files: Sequence[str],
    components: Sequence[sbom.Component],
    deps: dict[sbom.Ref, sbom.Dependency],
    lock_edges: set[tuple[sbom.Ref, sbom.Ref]],
    problems: list[sbom.Problem],
) -> Graph:
    graph = Graph({c.ref: c for c in components}, deps)
    for source, target in sorted(lock_edges):
        graph.add(source, target, "lock file")
    by_python = {sbom.normalize_pypi(c.name): c for c in components if c.kind == "pyproject"}
    by_folder = {(c.kind, c.folder): c for c in components if c.kind in ("pyproject", "cargo")}
    by_rpm = {c.name: c for c in components if c.kind == "rpm"}
    for comp in components:
        if comp.kind == "rpm":
            text = sbom.read_text(root, comp.source, problems)
            for name in spec_requires(text or ""):
                if name in by_rpm:
                    graph.add(comp.ref, by_rpm[name].ref, "Requires")
            continue
        loaded = sbom.read_toml(root, comp.source, problems)
        if not loaded:
            continue
        data = loaded[0]
        if comp.kind == "pyproject":
            for name in declared_python_names(data):
                if name in by_python:
                    graph.add(comp.ref, by_python[name].ref, "dependencies")
        for folder in declared_paths(data, comp.folder):
            other = by_folder.get((comp.kind, folder))
            if other is not None:
                graph.add(comp.ref, other.ref, "path dependency")
    return graph


# ---------------------------------------------------------------- the checks


def is_apache_part(comp: sbom.Component, rules: Sequence[tuple[str, str]]) -> bool:
    """A component that has to stay usable under Apache-2.0."""
    if sbom.rule_license(rules, comp.source) == APACHE:
        return True
    expr = effective(comp)
    return APACHE.lower() in licence_ids(expr) and fit(expr, "apache") == OK


def check_components(report: Report, components: Sequence[sbom.Component]) -> None:
    for comp in components:
        if comp.declared == sbom.NOASSERTION:
            report.problems.append(
                problem(comp.source, "unknown-licence", f"{comp.name} declares no licence", True)
            )
        elif fit(comp.declared, "apache") == UNKNOWN or parse_expression(comp.declared) is None:
            report.problems.append(
                problem(
                    comp.source,
                    "unknown-licence",
                    f"{comp.name}: licence {comp.declared!r} is not known to this check",
                    True,
                )
            )
        elif fit(comp.declared, "apache") == REVIEW:
            report.problems.append(
                problem(
                    comp.source,
                    "licence-review",
                    f"{comp.name}: licence {comp.declared!r} needs a look",
                    True,
                )
            )


def reach(graph: Graph, start: sbom.Component) -> list[tuple[sbom.Ref, list[sbom.Ref]]]:
    """Everything ``start`` reaches, with the chain to it. A GPL-only component of the repo is
    a stop: what lies behind it is its own business (it is reported once, as a problem)."""
    seen = {start.ref}
    todo: deque[tuple[sbom.Ref, list[sbom.Ref]]] = deque([(start.ref, [start.ref])])
    found: list[tuple[sbom.Ref, list[sbom.Ref]]] = []
    while todo:
        ref, chain = todo.popleft()
        for nxt in sorted(graph.edges.get(ref, ())):
            if nxt in seen:
                continue
            seen.add(nxt)
            found.append((nxt, chain + [nxt]))
            comp = graph.own.get(nxt)
            if comp is not None and gpl_only(effective(comp)):
                continue
            todo.append((nxt, chain + [nxt]))
    return found


def chain_text(graph: Graph, chain: Sequence[sbom.Ref]) -> str:
    names = []
    for ref in chain:
        comp = graph.own.get(ref)
        names.append(comp.name if comp else f"{ref[2]} {ref[3]}")
    how = graph.via.get((chain[-2], chain[-1]), "") if len(chain) > 1 else ""
    return " -> ".join(names) + (f" [{how}]" if how else "")


def check_dependencies(
    report: Report,
    graph: Graph,
    rules: Sequence[tuple[str, str]],
    licences: dict[tuple[str, str, str], str],
) -> None:
    for comp in graph.own.values():
        if is_apache_part(comp, rules):
            check_apache_part(report, graph, comp, licences)
        if "gpl-3.0-only" in licence_ids(effective(comp)) or "gpl-3.0-or-later" in licence_ids(
            effective(comp)
        ):
            check_gpl3_part(report, graph, comp, licences)


def check_apache_part(
    report: Report,
    graph: Graph,
    comp: sbom.Component,
    licences: dict[tuple[str, str, str], str],
) -> None:
    for ref, chain in reach(graph, comp):
        other = graph.own.get(ref)
        if other is not None:
            expr = effective(other)
            kind = fit(expr, "apache")
            if kind == BAD:
                report.problems.append(
                    problem(
                        comp.source,
                        "rpm-requires-gpl" if comp.kind == "rpm" else "apache-gpl-dep",
                        f"{comp.declared} component {comp.name} reaches {expr} code of the "
                        f"repo: {chain_text(graph, chain)}",
                        comp.kind == "rpm",
                    )
                )
            elif kind == REVIEW:
                report.problems.append(
                    problem(
                        comp.source,
                        "licence-review",
                        f"{comp.name} reaches {other.name} under {expr}: "
                        f"{chain_text(graph, chain)}",
                        True,
                    )
                )
            continue
        dep = graph.deps[ref]
        expr = licences.get((dep.ecosystem, dep.name, dep.version))
        if expr is None:
            continue
        kind = fit(expr, "apache")
        if kind == BAD:
            report.problems.append(
                problem(
                    comp.source,
                    "apache-gpl-third",
                    f"{comp.declared} component {comp.name} reaches {dep.ecosystem} package "
                    f"{dep.name} {dep.version} under {expr}: {chain_text(graph, chain)}",
                )
            )
        elif kind == REVIEW:
            report.problems.append(
                problem(
                    comp.source,
                    "licence-review",
                    f"{comp.name} reaches {dep.name} {dep.version} under {expr}",
                    True,
                )
            )


def check_gpl3_part(
    report: Report,
    graph: Graph,
    comp: sbom.Component,
    licences: dict[tuple[str, str, str], str],
) -> None:
    for ref in sorted(graph.edges.get(comp.ref, ())):
        dep = graph.deps.get(ref)
        expr = licences.get((dep.ecosystem, dep.name, dep.version)) if dep else None
        if dep is not None and expr is not None and fit(expr, "gpl3") == BAD:
            report.problems.append(
                problem(
                    comp.source,
                    "gpl2-only",
                    f"{effective(comp)} component {comp.name} uses {dep.ecosystem} package "
                    f"{dep.name} {dep.version} under {expr}, which cannot be combined with "
                    "GPL-3.0",
                )
            )


def list_unknown(
    report: Report,
    graph: Graph,
    licences: dict[tuple[str, str, str], str],
) -> None:
    """The third-party packages without a licence in the lock; one warning per lock file."""
    per_lock: dict[str, int] = {}
    for dep in graph.deps.values():
        key = (dep.ecosystem, dep.name, dep.version)
        expr = licences.get(key)
        if expr is not None and parse_expression(expr) is not None:
            if fit(expr, "apache") == UNKNOWN:
                report.problems.append(
                    problem(
                        sorted(dep.locks)[0],
                        "unknown-licence",
                        f"{dep.ecosystem} package {dep.name} {dep.version}: licence {expr!r} "
                        "is not known to this check",
                        True,
                    )
                )
            continue
        report.unknown[key] = set(dep.locks)
        for lock in dep.locks:
            per_lock[lock] = per_lock.get(lock, 0) + 1
    for lock, count in sorted(per_lock.items()):
        report.problems.append(
            problem(
                lock,
                "unknown-licence",
                f"{count} third-party packages carry no licence in the lock: unknown",
                True,
            )
        )


def package_roots(comp: sbom.Component, files: Sequence[str]) -> set[str]:
    """The import names of the Python packages a pyproject component ships."""
    names: set[str] = set()
    prefix = re.escape(comp.folder + "/" if comp.folder else "")
    pattern = re.compile(rf"^{prefix}(?:src/)?([A-Za-z_]\w*)/__init__\.py$")
    for path in files:
        found = pattern.match(path)
        if found and found.group(1) not in ("tests", "test", "tools", "docs", "bench"):
            names.add(found.group(1))
    return names


# A Python source this large is not code a person wrote; parsing it would take minutes.
MAX_PYTHON_BYTES = 4 * 2**20


def imported_roots(source: str) -> list[tuple[int, str]]:
    """(line, top-level module) of every absolute import of a Python source."""
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name.split(".")[0]) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module.split(".")[0]))
    return found


def owner_of(path: str, comps: Sequence[sbom.Component]) -> sbom.Component | None:
    best: sbom.Component | None = None
    for comp in comps:
        if sbom.inside(path, comp.folder) and (best is None or len(comp.folder) > len(best.folder)):
            best = comp
    return best


def check_imports(
    root: Path,
    files: Sequence[str],
    components: Sequence[sbom.Component],
    rules: Sequence[tuple[str, str]],
    report: Report,
) -> None:
    pyprojects = [c for c in components if c.kind == "pyproject"]
    gpl_names: dict[str, sbom.Component] = {}
    for comp in pyprojects:
        if not is_apache_part(comp, rules) and gpl_only(effective(comp)):
            for name in package_roots(comp, files):
                gpl_names.setdefault(name, comp)
    own_names = {n for c in pyprojects if is_apache_part(c, rules) for n in package_roots(c, files)}
    for path in files:
        if not path.endswith(".py"):
            continue
        comp = owner_of(path, pyprojects)
        if comp is None or not is_apache_part(comp, rules):
            continue
        try:
            if (root / path).stat().st_size > MAX_PYTHON_BYTES:
                report.problems.append(
                    problem(path, "read", f"larger than {MAX_PYTHON_BYTES // 2**20} MiB, not read", True)
                )
                continue
            text = (root / path).read_text(encoding="utf-8")
            imports = imported_roots(text)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as err:
            report.problems.append(problem(path, "read", f"cannot read as Python: {err}", True))
            continue
        in_tests = any(part in ("tests", "test") for part in PurePosixPath(path).parts)
        for line, name in imports:
            if name in gpl_names and name not in own_names:
                gpl = gpl_names[name]
                report.problems.append(
                    problem(
                        f"{path}:{line}",
                        "apache-gpl-import",
                        f"imports {name}, which {gpl.name} ({effective(gpl)}) ships; "
                        f"{path} is part of an {APACHE} component",
                        in_tests,
                    )
                )


def check_folders(
    root: Path,
    files: Sequence[str],
    components: Sequence[sbom.Component],
    rules: Sequence[tuple[str, str]],
    report: Report,
) -> None:
    folders = sorted({folder for folder, lic in rules if lic == APACHE})
    for folder in folders:
        inside = [p for p in files if p.startswith(folder)]
        if not inside:
            continue
        licence = f"{folder}LICENSE"
        text = sbom.read_text(root, licence, []) if licence in files else None
        if text is None or "Apache License" not in text:
            reason = "is not tracked" if licence not in files else "is not the Apache-2.0 text"
            report.problems.append(
                problem(
                    licence,
                    "apache-licence-file",
                    f"{licence} {reason}, but {sbom.LICENSING} gives {folder} to {APACHE}",
                )
            )
        reported: set[str] = set()
        for comp in components:
            if comp.source.startswith(folder) and gpl_only(comp.declared):
                reported.add(comp.source)
                report.problems.append(
                    problem(
                        comp.source,
                        "apache-gpl-header",
                        f"{comp.name} declares {comp.declared} inside {folder}, "
                        f"which {sbom.LICENSING} gives to {APACHE}",
                    )
                )
        for path in inside:
            if path in reported or path.endswith("LICENSE"):
                continue
            if PurePosixPath(path).suffix in BINARY_SUFFIXES:
                continue
            try:
                head = (root / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # not text: it has no header to read
            found = sbom.header_license(head)
            if found and gpl_only(found):
                report.problems.append(
                    problem(
                        path,
                        "apache-gpl-header",
                        f"header says {found}, but {sbom.LICENSING} gives {folder} to {APACHE}",
                    )
                )


def check(root: Path, files: Sequence[str]) -> Report:
    """Check the tracked ``files`` under ``root``."""
    report = Report()
    reading: list[sbom.Problem] = []
    tracked = sorted(set(files))
    rules: list[tuple[str, str]] = []
    if sbom.LICENSING in tracked:
        text = sbom.read_text(root, sbom.LICENSING, reading)
        rules = sbom.parse_licensing(text) if text is not None else []
    components = sbom.collect_components(root, tracked, rules, reading)
    deps, lock_edges = sbom.link_locks(root, tracked, components, reading)
    licences = lock_licences(root, tracked, reading)
    graph = build_graph(root, tracked, components, deps, lock_edges, reading)
    report.components, report.third_party = len(components), len(deps)

    check_components(report, components)
    check_dependencies(report, graph, rules, licences)
    list_unknown(report, graph, licences)
    check_imports(root, tracked, components, rules, report)
    check_folders(root, tracked, components, rules, report)

    reported = {p.path for p in report.problems if p.rule == "apache-gpl-header"}
    for item in reading:
        if item.warning and "gives this folder" in item.text and item.path in reported:
            continue  # the same finding is an error above
        report.problems.append(problem(item.path, "sbom", item.text, item.warning))
    report.problems.sort(key=lambda p: (p.path, p.rule, p.text))
    return report


# ---------------------------------------------------------------- command line


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, text=True
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def main(argv: list[str] | None = None) -> int:
    try:
        args = sys.argv[1:] if argv is None else argv
        if args in (["-h"], ["--help"]):
            print(USAGE)
            return 0
        if len(args) > 1 or any(a.startswith("-") for a in args):
            print(USAGE, file=sys.stderr)
            return 2
        root = Path(args[0]) if args else ROOT
        if not root.is_dir():
            print(f"{root}: not a folder\n{USAGE}", file=sys.stderr)
            return 2
        try:
            files = tracked_files(root)
        except (OSError, subprocess.CalledProcessError) as err:
            print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
            return 2
        if not files:
            print(f"{root}: no tracked files", file=sys.stderr)
            return 2
        report = check(root, files)
        for item in report.problems:
            print(item, file=sys.stderr)
        if report.unknown:
            print(f"third-party packages with an unknown licence: {len(report.unknown)}")
            for (eco, name, version), locks in sorted(report.unknown.items()):
                print(f"  {eco} {name} {version}  ({', '.join(sorted(locks))})")
        warnings = len(report.problems) - report.errors
        print(
            f"{report.components} own components, {report.third_party} third-party packages: "
            f"{report.errors} errors, {warnings} warnings"
        )
        return 1 if report.errors else 0
    except (OSError, ValueError) as err:  # no traceback for a bad name, argument or bytes
        print(f"{os.path.basename(__file__)}: cannot check: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
