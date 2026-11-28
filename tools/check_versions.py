#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check that the versions of one program agree everywhere the repo writes them.

A program has a version in up to five places; this checker reads them all and
compares them, with the standard library only (``tomllib``, ``ast``,
``xml.etree``; RPM, cargo and pip are not needed and not run):

* the Python project: ``project.version`` of a ``pyproject.toml`` (PEP 440);
* the Rust project: ``workspace.package.version`` of a workspace
  ``Cargo.toml`` (or ``package.version`` of a crate that is not a member);
* the RPM spec of ``obs/packages/<name>/``: ``Version`` (simple macros are
  expanded). PEP 440 and RPM write a pre-release differently, so the checker
  maps them (``pep440_to_rpm``): ``0.1.0.dev0`` is ``0.1.0~dev0``, ``0.2.0a1``
  is ``0.2.0~a1``, ``1.0.0rc1`` is ``1.0.0~rc1``, ``1.0.0`` stays ``1.0.0``;
* ``__version__`` in the ``__init__.py`` of the Python package, if it has one;
* the newest ``<release version>`` of a ``*.metainfo.xml`` of the project;
* the "Версія на <date>" columns of the tables in ``docs/alpha-freeze.md``.

Which spec builds which project is worked out from, in this order: the
``TARBALLS`` of ``obs/tools/obs_sync.py`` (the folder it makes the source
tarball from, and its ``cargo=`` manifest); the spec ``Name`` equal to the
project name (a ``python-`` prefix is dropped); a repo folder written in the
spec's ``URL``/``Source`` or in its ``%prep``/``%build``/``%install`` lines.
The Cargo manifest a spec builds is the ``Cargo.toml`` path of its
``%build``/``%install`` (relative to the tarball folder) or the ``cargo=`` of
``TARBALLS``.

Errors (exit code 1):

* a pyproject version that is not a canonical PEP 440 version (the only form
  ``pep440_to_rpm`` and obs_sync.py map), or a missing one;
* a project and the spec that builds it have different versions;
* the Cargo workspace version and the spec that builds it differ;
* ``__version__`` of the package differs from the pyproject version.

Warnings (``path: warning: ...``; they do not fail the check):

* specs that no pyproject.toml or Cargo.toml project can be matched to (one
  line with the list); a spec that matches two projects or whose name and
  tarball point at different projects;
* a spec version with a macro that cannot be expanded;
* the newest metainfo release differs from the version of the program;
* a component of ``docs/alpha-freeze.md`` that shows another version than its
  spec (the table is a snapshot of a date, so this is not an error), or a row
  whose versions cannot be paired with packages;
* old problems of the repo (see ``LEGACY``).

The output is the errors and warnings (stderr) and a table (stdout):
``component | pyproject/Cargo | spec | metainfo | alpha``.

Only ``git ls-files`` is run (to find the files). Nothing else is executed,
nothing is read from the network and nothing is written.

Run: python3 tools/check_versions.py
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

OBS_SYNC = "obs/tools/obs_sync.py"
ALPHA_FREEZE = "docs/alpha-freeze.md"
# PEP 440 in the one spelling the repo uses (and obs_sync.py maps):
# release, optional pre-release, optional post-release, optional dev release.
PEP440 = re.compile(
    r"(?P<release>\d+(?:\.\d+)*)(?:(?P<pre>a|b|rc)(?P<pre_n>\d+))?"
    r"(?:\.post(?P<post_n>\d+))?(?:\.dev(?P<dev_n>\d+))?"
)
# The same version as RPM writes it: a pre-release or dev release after "~".
RPM_VERSION = re.compile(
    r"(?P<release>\d+(?:\.\d+)*)(?:~(?P<pre>a|b|rc)(?P<pre_n>\d+))?"
    r"(?:\.post(?P<post_n>\d+))?(?:~dev(?P<dev_n>\d+))?"
)
# Cargo versions (SemVer): the pre-release after "-" becomes "~" in RPM.
SEMVER = re.compile(r"(?P<release>\d+\.\d+\.\d+)(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")
MACRO = re.compile(r"%\{\??(\w+)\}|%(\w+)")
DEFINE = re.compile(r"%(?:define|global)\s+(\w+)\s+(.*\S)")
TAG = re.compile(r"([A-Za-z][A-Za-z0-9]*):\s*(.*\S)")
SPEC_SECTIONS = frozenset(
    {
        "%package",
        "%description",
        "%prep",
        "%generate_buildrequires",
        "%conf",
        "%build",
        "%install",
        "%check",
        "%files",
        "%changelog",
    }
)
BUILD_SECTIONS = ("%prep", "%conf", "%build", "%install", "%check")
CARGO_PATH = re.compile(r"[\w./-]*Cargo\.toml")
# What the alpha-freeze table calls a component, when its words are not the name of a spec:
# (a keyword of the component cell, the specs its versions belong to in the order of the
# versions in the row; ``None`` is a version that no spec of this repo has).
ALPHA_ALIASES: tuple[tuple[str, tuple[str | None, ...]], ...] = (
    (
        "брендування",
        (
            "Vilna-release",
            "distribution-logos-Vilna",
            "vilna-branding",
            "vilna-presets",
            "plymouth-branding-Vilna",
            "vilna-backgrounds",
        ),
    ),
    ("шлюз приватності", ("python-vilna-privacy-gateway",)),
    (
        "mcp:",
        (
            "vilna-mcp-files",
            "vilna-mcp-backup",
            "vilna-mcp-settings",
            "vilna-mcp-calendar",
            "vilna-mcp-mail",
            "vilna-mcp-desktop",
            "vilna-mcp-apps",
        ),
    ),
    ("копії (restic)", ("vilna-backup",)),
    ("живий iso", ("vilna-installer",)),
    ("vpn:", (None, "mullvad-vpn")),
    (
        "розширення gnome shell",
        ("gnome-shell-extension-vilna", "gnome-shell-extension-dash-to-dock"),
    ),
)
# Old problems of the repo: (path, rule) pairs reported as warnings.
LEGACY: frozenset[tuple[str, str]] = frozenset(
    {
        # The Cargo workspace of Herkos is 0.1.0 while the Python package and the
        # spec are 0.1.0~dev0 (0.1.0.dev0): the crates are not marked as pre-release.
        ("herkos/rs/Cargo.toml", "cargo-spec"),
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


@dataclass(frozen=True)
class Project:
    """A pyproject.toml project or a Cargo workspace (or crate) with a version."""

    folder: str  # repo-relative folder of the manifest ("" for the repo root)
    kind: str  # "python" or "cargo"
    name: str
    version: str
    path: str  # repo-relative manifest path
    data: dict = field(default_factory=dict, compare=False, hash=False)


@dataclass
class Spec:
    path: str
    name: str = ""
    version: str = ""
    url: str = ""
    sources: list[str] = field(default_factory=list)
    build_lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Tarball:
    folder: str
    cargo: str | None = None


@dataclass
class Row:
    """One line of the output table."""

    component: str
    project: str = "-"
    spec: str = "-"
    metainfo: str = "-"
    alpha: list[str] = field(default_factory=list)


# --- versions ---------------------------------------------------------------------------


def pep440_to_rpm(version: str) -> str:
    """The RPM spelling of a canonical PEP 440 version.

    ``0.1.0.dev0`` -> ``0.1.0~dev0``, ``0.2.0a1`` -> ``0.2.0~a1``,
    ``1.0.0rc1`` -> ``1.0.0~rc1``, ``1.0.0`` -> ``1.0.0``. A pre-release and a dev
    release get ``~`` so that they sort before the release; a post-release stays
    ``.postN`` (it sorts after). Raises ValueError for anything else (epoch, local
    version, ``1.0-rc1``, ``1.0.dev``): obs_sync.py would not map it either.
    """
    match = PEP440.fullmatch(version)
    if match is None:
        raise ValueError(f"not a canonical PEP 440 version: {version!r}")
    out = match["release"]
    if match["pre"]:
        out += f"~{match['pre']}{match['pre_n']}"
    if match["post_n"] is not None:
        out += f".post{match['post_n']}"
    if match["dev_n"] is not None:
        out += f"~dev{match['dev_n']}"
    return out


def rpm_to_pep440(version: str) -> str:
    """The PEP 440 spelling of an RPM version (the reverse of ``pep440_to_rpm``)."""
    match = RPM_VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"not an RPM version this checker maps: {version!r}")
    out = match["release"]
    if match["pre"]:
        out += f"{match['pre']}{match['pre_n']}"
    if match["post_n"] is not None:
        out += f".post{match['post_n']}"
    if match["dev_n"] is not None:
        out += f".dev{match['dev_n']}"
    return out


def cargo_to_rpm(version: str) -> str:
    """The RPM spelling of a SemVer (Cargo) version: ``1.2.3-dev0`` -> ``1.2.3~dev0``.

    The build metadata (``+...``) does not count in SemVer and is dropped.
    Raises ValueError if the version is not ``X.Y.Z[-pre][+build]``.
    """
    match = SEMVER.fullmatch(version)
    if match is None:
        raise ValueError(f"not a SemVer version: {version!r}")
    return match["release"] + (f"~{match['pre']}" if match["pre"] else "")


def to_pep440(version: str) -> str | None:
    """A version written either way as PEP 440, or None if it is neither."""
    if PEP440.fullmatch(version):
        return version
    try:
        return rpm_to_pep440(version)
    except ValueError:
        return None


def same_version(first: str, second: str) -> bool:
    """Equal as written, or equal after both are put in the PEP 440 spelling."""
    if first == second:
        return True
    one, two = to_pep440(first), to_pep440(second)
    return one is not None and one == two


# --- reading files ----------------------------------------------------------------------


def read_text(root: Path, rel: str) -> tuple[str | None, list[Problem]]:
    """The text of a file; one clear problem if it is missing, not UTF-8 or has a BOM."""
    try:
        text = (root / rel).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, [Problem(rel, "the file does not exist", rule="layout")]
    except OSError as err:
        return None, [Problem(rel, f"cannot read the file: {err.strerror or err}", rule="layout")]
    except UnicodeDecodeError:
        return None, [Problem(rel, "the file is not valid UTF-8", rule="encoding")]
    if text.startswith("\ufeff"):
        return text[1:], [
            Problem(rel, "the file starts with a byte order mark (BOM)", rule="encoding")
        ]
    return text, []


def read_toml(root: Path, rel: str) -> tuple[dict | None, list[Problem]]:
    text, problems = read_text(root, rel)
    if text is None:
        return None, problems
    try:
        return tomllib.loads(text), problems
    except tomllib.TOMLDecodeError as err:
        return None, [*problems, Problem(rel, f"not valid TOML: {err}", rule="toml")]


def folder_of(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def normalize_name(name: str) -> str:
    """PEP 503: ``Vilna_Backup`` and ``vilna.backup`` are ``vilna-backup``."""
    return re.sub(r"[-_.]+", "-", name).lower()


# --- projects ---------------------------------------------------------------------------


def python_project(path: str, data: dict) -> tuple[Project | None, list[Problem]]:
    section = data.get("project")
    if not isinstance(section, dict):
        return None, []  # a pyproject.toml with tool settings only
    name = str(section.get("name", ""))
    version = section.get("version")
    if version is None:
        if "version" in section.get("dynamic", []):
            message = "the version is dynamic, so it cannot be compared with the spec"
            return None, [Problem(path, message, warning=True, rule="version-dynamic")]
        return None, [Problem(path, "[project] has no version", rule="version-format")]
    if not isinstance(version, str):
        return None, [Problem(path, "project.version is not a string", rule="version-format")]
    try:
        pep440_to_rpm(version)
    except ValueError:
        message = f"project.version {version!r} is not a canonical PEP 440 version"
        return None, [Problem(path, message, rule="version-format")]
    return Project(folder_of(path), "python", name, version, path, data), []


def cargo_project(path: str, data: dict) -> tuple[Project | None, list[Problem]]:
    package = data.get("package") if isinstance(data.get("package"), dict) else {}
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    shared = workspace.get("package") if isinstance(workspace.get("package"), dict) else {}
    version = shared.get("version") if "version" in shared else package.get("version")
    name = str(package.get("name", "")) or folder_of(path)
    if isinstance(version, dict):  # version.workspace = true: the workspace root has it
        return None, []
    if version is None:
        return None, []
    if not isinstance(version, str):
        return None, [Problem(path, "the version is not a string", rule="version-format")]
    try:
        cargo_to_rpm(version)
    except ValueError:
        message = f"version {version!r} is not X.Y.Z[-pre]"
        return None, [Problem(path, message, rule="version-format")]
    return Project(folder_of(path), "cargo", name, version, path, data), []


def load_projects(root: Path, tracked: Iterable[str]) -> tuple[list[Project], list[Problem]]:
    projects: list[Project] = []
    problems: list[Problem] = []
    for path in sorted(tracked):
        base = PurePosixPath(path).name
        if base not in ("pyproject.toml", "Cargo.toml"):
            continue
        data, found = read_toml(root, path)
        problems += found
        if data is None:
            continue
        make = python_project if base == "pyproject.toml" else cargo_project
        project, found = make(path, data)
        problems += found
        if project is not None:
            projects.append(project)
    return projects, problems


# --- specs ------------------------------------------------------------------------------


def expand(value: str, macros: dict[str, str]) -> str:
    """Expand the known macros of ``value``; unknown ones stay as they are."""
    for _ in range(5):
        new = MACRO.sub(lambda m: macros.get(m.group(1) or m.group(2), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


def parse_spec(path: str, text: str) -> Spec:
    """Name, Version, URL, Sources and the build lines of a spec (the main preamble only)."""
    spec = Spec(path)
    macros: dict[str, str] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        words = line.split()
        if words and words[0] in SPEC_SECTIONS:
            section = words[0]
            continue
        if section in BUILD_SECTIONS:
            if line and not line.startswith("#"):
                spec.build_lines.append(line)
            continue
        if section:
            continue
        define = DEFINE.match(line)
        if define:
            macros[define.group(1)] = define.group(2)
            continue
        tag = TAG.fullmatch(line)
        if tag is None or raw.startswith((" ", "\t")):
            continue
        key, value = tag.group(1).lower(), expand(tag.group(2), macros)
        if key == "name" and not spec.name:
            spec.name = macros["name"] = value
        elif key == "version" and not spec.version:
            spec.version = macros["version"] = value
        elif key == "url" and not spec.url:
            spec.url = value
        elif key.startswith("source"):
            spec.sources.append(value)
    return spec


def parse_tarballs(text: str) -> dict[str, Tarball]:
    """``TARBALLS`` of obs_sync.py: spec name -> the repo folder and Cargo manifest it uses.

    The file is parsed (``ast``), not run.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return {}
    tarballs: dict[str, Tarball] = {}
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if not (isinstance(target, ast.Name) and target.id == "TARBALLS"):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if not (isinstance(value, ast.Call) and value.args):
                continue
            first = value.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            cargo = None
            for keyword in value.keywords:
                if keyword.arg == "cargo" and isinstance(keyword.value, ast.Constant):
                    cargo = keyword.value.value if isinstance(keyword.value.value, str) else None
            tarballs[key.value] = Tarball(first.value, cargo)
    return tarballs


def norm_path(path: str) -> str:
    return str(PurePosixPath(path))


def cargo_owner(path: str, projects: list[Project]) -> Project | None:
    """The Cargo project (workspace root) whose folder holds the manifest ``path``."""
    folder = folder_of(path)
    best: Project | None = None
    for project in projects:
        if project.kind != "cargo":
            continue
        inside = project.folder == "" or folder == project.folder
        inside = inside or folder.startswith(project.folder + "/")
        if inside and (best is None or len(project.folder) > len(best.folder)):
            best = project
    return best


def folder_mentions(spec: Spec, folders: Iterable[str]) -> set[str]:
    """Repo folders the URL, a Source or a build line of the spec names as a path."""
    found: set[str] = set()
    for folder in folders:
        if not folder:
            continue
        in_url = any(
            re.search(rf"/(?:tree|blob)/[^/\s]+/{re.escape(folder)}(?:/|$)", text)
            for text in (spec.url, *spec.sources)
        )
        word_start = rf"(?:^|[\s\"'=(]){re.escape(folder)}/"
        lines = [*spec.sources, *spec.build_lines]
        if in_url or any(re.search(word_start, line) for line in lines):
            found.add(folder)
    return found


@dataclass
class Match:
    python: Project | None = None
    cargo: list[Project] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)


def match_spec(spec: Spec, projects: list[Project], tarballs: dict[str, Tarball]) -> Match:
    """Which Python project and Cargo workspaces the spec builds (see the module docstring)."""
    result = Match()
    python = [p for p in projects if p.kind == "python"]
    tarball = tarballs.get(spec.name)
    by_tarball = [p for p in python if tarball and p.folder == tarball.folder]
    wanted = {normalize_name(spec.name)}
    wanted.add(normalize_name(re.sub(r"^python3?-", "", spec.name)))
    by_name = [p for p in python if normalize_name(p.name) in wanted]
    mentioned = folder_mentions(spec, [p.folder for p in python])
    by_path = [p for p in python if p.folder in mentioned]
    if by_tarball and by_name and by_tarball[0] != by_name[0]:
        result.problems.append(
            Problem(
                spec.path,
                f"its name points at {by_name[0].path} but obs_sync.py builds it from "
                f"{by_tarball[0].path}; the version is compared with the latter",
                warning=True,
                rule="spec-conflict",
            )
        )
    for found in (by_tarball, by_name, by_path):
        if len(found) > 1:
            names = ", ".join(p.path for p in found)
            message = f"it matches more than one project ({names}), so none is compared"
            result.problems.append(Problem(spec.path, message, warning=True, rule="spec-ambiguous"))
            break
        if found:
            result.python = found[0]
            break
    manifests: list[str] = []
    if tarball and tarball.cargo:
        manifests.append(norm_path(tarball.cargo))
    base = tarball.folder if tarball else ""
    for line in spec.build_lines:
        for token in CARGO_PATH.findall(line):
            manifests.append(norm_path(str(PurePosixPath(base) / token) if base else token))
    for manifest in manifests:
        owner = cargo_owner(manifest, projects)
        if owner is not None and owner not in result.cargo:
            result.cargo.append(owner)
    return result


# --- the checks -------------------------------------------------------------------------


def legacy(problem: Problem) -> Problem:
    """Turn a known old violation (see LEGACY) into a warning."""
    if (problem.path, problem.rule) in LEGACY:
        return Problem(problem.path, problem.message, warning=True, rule=problem.rule)
    return problem


def check_spec_versions(spec: Spec, match: Match) -> list[Problem]:
    """The version of the spec against its Python project and its Cargo workspaces."""
    if match.python is None and not match.cargo:
        return []
    if not spec.version:
        return []
    if "%" in spec.version:
        message = f"Version {spec.version} has a macro that cannot be expanded"
        return [Problem(spec.path, message, warning=True, rule="spec-macro")]
    problems: list[Problem] = []
    if match.python is not None:
        project = match.python
        want = pep440_to_rpm(project.version)
        if spec.version != want:
            problems.append(
                Problem(
                    spec.path,
                    f"Version {spec.version} does not match {project.path} version "
                    f"{project.version} (the spec needs {want})",
                    rule="spec-version",
                )
            )
    for project in match.cargo:
        want = cargo_to_rpm(project.version)
        if spec.version != want:
            problems.append(
                Problem(
                    project.path,
                    f"version {project.version} does not match {spec.path} Version "
                    f"{spec.version} (Cargo needs {_cargo_hint(spec.version)})",
                    rule="cargo-spec",
                )
            )
    return problems


def _cargo_hint(rpm_version: str) -> str:
    """The Cargo spelling of an RPM version: ``0.1.0~dev0`` -> ``0.1.0-dev0``."""
    return rpm_version.replace("~", "-")


def init_files(project: Project, tracked: set[str]) -> list[str]:
    """The ``__init__.py`` files of the packages a pyproject builds (hatch wheel packages)."""
    packages = (
        project.data.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("packages", [])
    )
    prefix = f"{project.folder}/" if project.folder else ""
    if isinstance(packages, list) and packages:
        names = [f"{prefix}{p}/__init__.py" for p in packages if isinstance(p, str)]
    else:
        pattern = re.compile(rf"{re.escape(prefix)}src/[^/]+/__init__\.py")
        names = sorted(p for p in tracked if pattern.fullmatch(p))
    return [n for n in names if n in tracked]


def module_version(text: str) -> str | None:
    """The string ``__version__`` is set to at the top level of a module, or None."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        named = any(isinstance(t, ast.Name) and t.id == "__version__" for t in targets)
        if named and isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def check_init_versions(root: Path, project: Project, tracked: set[str]) -> list[Problem]:
    problems: list[Problem] = []
    for path in init_files(project, tracked):
        text, found = read_text(root, path)
        problems += found
        if text is None:
            continue
        version = module_version(text)
        if version is not None and version != project.version:
            problems.append(
                Problem(
                    path,
                    f"__version__ {version!r} does not match {project.path} version "
                    f"{project.version!r}",
                    rule="init-version",
                )
            )
    return problems


def newest_release(text: str) -> str | None:
    """The version of the newest ``<release>`` (by date; the first one when dates tie)."""
    try:
        tree = ET.fromstring(text)
    except ET.ParseError:
        raise ValueError("not well-formed XML") from None
    best: tuple[str, int] | None = None
    version = None
    for index, release in enumerate(tree.iter("release")):
        found = release.get("version")
        if not found:
            continue
        key = (release.get("date") or "", -index)
        if best is None or key > best:
            best, version = key, found
    return version


def project_for(path: str, projects: list[Project]) -> Project | None:
    """The project whose folder holds the file (the deepest; Python before Cargo)."""
    folder = folder_of(path)
    inside = [
        p
        for p in projects
        if p.folder and (folder == p.folder or folder.startswith(p.folder + "/"))
    ]
    if not inside:
        return None
    return max(inside, key=lambda p: (len(p.folder), p.kind == "python"))


def check_metainfo(
    root: Path, tracked: Iterable[str], projects: list[Project]
) -> tuple[list[Problem], dict[str, str]]:
    """The newest release of every metainfo against the version of its program."""
    problems: list[Problem] = []
    latest: dict[str, str] = {}
    for path in sorted(t for t in tracked if t.endswith(".metainfo.xml")):
        project = project_for(path, projects)
        if project is None:
            continue
        text, found = read_text(root, path)
        problems += found
        if text is None:
            continue
        try:
            version = newest_release(text)
        except ValueError as err:
            problems.append(Problem(path, f"{err}, so its releases are not checked", True, "xml"))
            continue
        if version is None:
            continue
        latest[project.path] = version
        as_rpm = pep440_to_rpm if project.kind == "python" else cargo_to_rpm
        if version not in (project.version, as_rpm(project.version)):
            problems.append(
                Problem(
                    path,
                    f"the newest release is {version} but {project.path} is {project.version}",
                    warning=True,
                    rule="metainfo-version",
                )
            )
    return problems, latest


# --- docs/alpha-freeze.md ---------------------------------------------------------------


@dataclass(frozen=True)
class AlphaRow:
    line: int
    component: str
    header: str  # the version column title, "Версія на 23.11.2026"
    tokens: tuple[str, ...]  # the cell split at "/": each a version, "name version" or text


def table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return []
    return [c.strip() for c in re.split(r"(?<!\\)\|", stripped[1:-1])]


def parse_alpha(text: str) -> list[AlphaRow]:
    """The rows of every table whose second column is "Версія на <date>"."""
    rows: list[AlphaRow] = []
    header = ""
    for number, line in enumerate(text.splitlines(), start=1):
        cells = table_cells(line)
        if len(cells) < 2:
            header = ""
            continue
        if cells[1].startswith("Версія"):
            header = cells[1]
        elif header and not set(cells[0]) <= set("-: ") and not set(cells[1]) <= set("-: "):
            tokens = tuple(t.strip() for t in cells[1].split("/"))
            rows.append(AlphaRow(number, cells[0], header, tokens))
    return rows


VERSION_TOKEN = re.compile(r"(?:(?P<name>[A-Za-z][\w.+-]*)\s+)?(?P<version>\d[\w.~^+]*)")


def spec_for_word(word: str, names: list[str]) -> str | None:
    """The spec a word of a component names: its Name, or ``<Name>-<subpackage>``."""
    folded = word.casefold()
    best = None
    for name in names:
        low = name.casefold()
        named = folded == low or folded.startswith(low + "-")
        if named and (best is None or len(name) > len(best)):
            best = name
    return best


def row_packages(row: AlphaRow, names: list[str]) -> list[str | None]:
    """The specs a row of the table is about, in the order of its versions."""
    low = row.component.casefold()
    for keyword, aliased in ALPHA_ALIASES:
        if keyword in low:
            return [a if a is None or a in names else None for a in aliased]
    found: list[str | None] = []
    for word in re.findall(r"[A-Za-z][\w-]*", row.component):
        spec = spec_for_word(word, names)
        if spec is not None and spec not in found:
            found.append(spec)
    return found


def pair_alpha(row: AlphaRow, names: list[str]) -> tuple[list[tuple[str, str]], bool]:
    """(spec name, version) pairs of a row; the flag is True if the row could not be paired."""
    parsed = [VERSION_TOKEN.fullmatch(t) for t in row.tokens]
    if not any(parsed):
        return [], False
    named = [(m["name"], m["version"]) for m in parsed if m and m["name"]]
    if named:
        return [(n, v) for n, v in named if n in names], False
    candidates = row_packages(row, names)
    if not candidates:
        return [], False
    pairs: list[tuple[str, str]] = []
    if len(parsed) == 1 and parsed[0]:
        pairs = [(c, parsed[0]["version"]) for c in candidates if c]
    elif len(parsed) == len(candidates):
        pairs = [(c, m["version"]) for c, m in zip(candidates, parsed, strict=True) if c and m]
    else:
        return [], True
    return pairs, False


def check_alpha(
    path: str, text: str, specs: dict[str, Spec]
) -> tuple[list[Problem], dict[str, list[str]]]:
    """The versions of the table against the specs; the versions found for each spec."""
    problems: list[Problem] = []
    seen: dict[str, list[str]] = {}
    names = sorted(specs)
    for row in parse_alpha(text):
        pairs, unpaired = pair_alpha(row, names)
        if unpaired:
            message = (
                f"line {row.line}: the versions of {row.component!r} cannot be paired with "
                "its packages, so they are not compared"
            )
            problems.append(Problem(path, message, warning=True, rule="alpha-unpaired"))
        for name, version in pairs:
            if version not in seen.setdefault(name, []):
                seen[name].append(version)
            have = specs[name].version
            if have and "%" not in have and not same_version(version, have):
                message = (
                    f"line {row.line}: {row.component!r} shows {name} {version} ({row.header}) "
                    f"but its spec has {have}"
                )
                problems.append(Problem(path, message, warning=True, rule="alpha-version"))
    return problems, seen


# --- the whole repo ---------------------------------------------------------------------


def label(project: Project) -> str:
    return f"{project.version}" if project.kind == "python" else f"cargo {project.version}"


def check_repo(root: Path, tracked: Iterable[str]) -> tuple[list[Problem], list[Row]]:
    """All checks on a repository tree; also the rows of the table for the output."""
    files = sorted(set(tracked))
    tracked_set = set(files)
    projects, problems = load_projects(root, files)
    tarballs: dict[str, Tarball] = {}
    if OBS_SYNC in tracked_set:
        text, found = read_text(root, OBS_SYNC)
        problems += found
        tarballs = parse_tarballs(text or "")
    specs: dict[str, Spec] = {}
    for path in (f for f in files if f.endswith(".spec")):
        text, found = read_text(root, path)
        problems += found
        if text is not None:
            spec = parse_spec(path, text)
            specs[spec.name or path] = spec
    metainfo_problems, newest = check_metainfo(root, files, projects)
    problems += metainfo_problems
    alpha_seen: dict[str, list[str]] = {}
    if ALPHA_FREEZE in tracked_set:
        text, found = read_text(root, ALPHA_FREEZE)
        problems += found
        if text is not None:
            found, alpha_seen = check_alpha(ALPHA_FREEZE, text, specs)
            problems += found
    else:
        message = "the file does not exist, so the versions of the alpha are not checked"
        problems.append(Problem(ALPHA_FREEZE, message, warning=True, rule="alpha-missing"))
    rows: list[Row] = []
    unmatched: list[str] = []
    matched_projects: set[str] = set()
    for name, spec in sorted(specs.items()):
        match = match_spec(spec, projects, tarballs)
        problems += match.problems
        problems += check_spec_versions(spec, match)
        parts = ([label(match.python)] if match.python else []) + [label(p) for p in match.cargo]
        if not parts and not any(p.rule == "spec-ambiguous" for p in match.problems):
            unmatched.append(name)
        row = Row(name, " + ".join(parts) or "-", spec.version or "-")
        row.alpha = alpha_seen.get(name, [])
        for project in ([match.python] if match.python else []) + match.cargo:
            matched_projects.add(project.path)
            row.metainfo = newest.get(project.path, row.metainfo)
        rows.append(row)
    for project in projects:
        if project.path not in matched_projects:
            rows.append(Row(project.name, label(project), "-", newest.get(project.path, "-")))
    for project in projects:
        if project.kind == "python":
            problems += check_init_versions(root, project, tracked_set)
    if unmatched:
        message = (
            f"{len(unmatched)} spec files are not matched to a pyproject.toml or Cargo.toml "
            f"project, so their versions are not compared: {', '.join(unmatched)}"
        )
        problems.append(Problem("obs/packages", message, warning=True, rule="spec-unmatched"))
    return [legacy(p) for p in problems], rows


def format_table(rows: list[Row]) -> list[str]:
    """The table ``component | pyproject/Cargo | spec | metainfo | alpha`` (Markdown)."""
    header = ["component", "pyproject/Cargo", "spec", "metainfo", "alpha"]
    body = [
        [r.component, r.project, r.spec, r.metainfo, ", ".join(r.alpha) or "-"]
        for r in sorted(rows, key=lambda r: r.component.casefold())
    ]
    widths = [max(len(line[i]) for line in [header, *body]) for i in range(len(header))]

    def render(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)) + " |"

    return [render(header), "| " + " | ".join("-" * w for w in widths) + " |"] + [
        render(line) for line in body
    ]


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        errors="surrogateescape",
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        print("usage: python3 tools/check_versions.py", file=sys.stderr)
        return 0 if args in (["-h"], ["--help"]) else 2
    try:
        tracked = tracked_files(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    problems, rows = check_repo(ROOT, tracked)
    for problem in problems:
        print(problem, file=sys.stderr)
    for line in format_table(rows):
        print(line)
    errors = sum(not p.warning for p in problems)
    specs = sum(1 for t in tracked if t.endswith(".spec"))
    projects = sum(1 for t in tracked if PurePosixPath(t).name in ("pyproject.toml", "Cargo.toml"))
    print(
        f"{specs} spec files and {projects} project files checked: "
        f"{errors} errors, {len(problems) - errors} warnings"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    # A file name that is not UTF-8 is printed escaped, not as a traceback.
    sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    sys.exit(main())
