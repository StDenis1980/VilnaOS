#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the SBOM of the Vilna OS images: the openSUSE packages they install, as SPDX 2.3 JSON.

    python3 tools/sbom_image.py                          # the document on stdout
    python3 tools/sbom_image.py --version 0.4 --out vilna-image.spdx.json
    python3 tools/sbom_image.py --check                  # build it and check its integrity (CI)
    python3 tools/sbom_image.py --created 2026-12-02T09:00:00Z --root /path/to/checkout
    python3 tools/sbom_image.py --versions versions.json # fill versions from a local file

It is the other half of ``tools/sbom.py``: that one lists our own code and its locked
dependencies, this one lists the packages that come from openSUSE.

Where the packages come from (only files that ``git ls-files`` lists are read):

* ``obs/images/*/*.kiwi``: every image (``<image name>`` and ``<version>``) and its
  ``<package name>`` entries of the types ``image``, ``bootstrap`` and the like (the
  ``delete`` and ``uninstall`` lists are not read). A ``<namedCollection>`` is resolved
  through the patterns of our specs;
* ``obs/packages/patterns-vilna`` (and any other spec in ``obs/packages`` that provides
  ``pattern() = <name>``): the ``Requires:`` of the pattern package are what an image
  that installs the pattern gets. ``Recommends:`` are not read: the image installs
  required packages alone (``onlyRequired``);
* ``image/packages.yaml``: the list ``vilna.packages`` with ``name``, ``license`` and
  ``status``. It tells our own packages (``obs``, ``planned``) from the openSUSE ones
  (``upstream``); the licence it notes for an openSUSE package goes into the package's
  ``comment``, not into a licence field, because we did not read it from the RPM.

A package is ours, and so not listed here (``tools/sbom.py`` has it), when its name is a
folder of ``obs/packages``, a ``Name`` or ``%package`` of one of our specs, an image, a
``status: obs|planned`` row of packages.yaml, ends in ``-branding-Vilna`` or starts with
``vilna-``/``Vilna-``. Everything else is an openSUSE package.

What goes into the document:

* one package per image (purpose OPERATING-SYSTEM), with the version of the KIWI
  description. The document DESCRIBES each image;
* one package per openSUSE package, with the package URL ``pkg:rpm/opensuse/<name>``,
  ``versionInfo`` ``NOASSERTION`` and ``NOASSERTION`` in every licence field. An image
  CONTAINS every package it installs (directly, in ``bootstrap`` or through a pattern;
  ``sourceInfo`` says which). An image that lists another of our images as a package
  (the live ISO carries ``tik-osimage-Vilna``) CONTAINS that image.

The output is deterministic: the same files give the same bytes. ``created`` comes from
SOURCE_DATE_EPOCH, else ``--created``, else the clock; the document namespace is
``https://vilnaos.com/spdx/vilna-image-<version>-<sha256 of the content without created>``.

``--check`` checks the built document: unique SPDXIDs and purls; every relationship points
to an element that exists; every image is DESCRIBED; every openSUSE package is contained in
an image; every package has a name, versionInfo and downloadLocation and a licence field;
a purl is ``pkg:rpm/opensuse/<name>[@<version>]`` for the package's name.

How the versions are filled in later (no network is used here)
--------------------------------------------------------------
The names are the ``Requires`` names of the sources, not what the solver picked, and
the solver also adds packages nobody names (the dependencies of the dependencies). The
real list, with versions, exists only after OBS has built the image: the list of installed
RPMs of the build result (KIWI writes it next to the image as ``<image>.packages``; check
the format on the first real build). Someone downloads that file outside this tool, turns
it into a JSON object ``{"<rpm name>": "<version>-<release>"}`` and gives it to
``--versions``. Every listed package then gets the version in ``versionInfo`` and in the
purl (``pkg:rpm/opensuse/<name>@<version>``). A name in the JSON that no image installs is
a warning (that is how the transitive packages show up), a package without a version is a
warning with the count. Reading the OBS build result itself, the RPM header digests
(SHA-256 per package) and the transitive closure are left to that later step.

Only ``git ls-files`` is run; nothing is read from the network. Only ASCII is written
(non-ASCII characters are ``\\u`` escapes). Nothing is written when a file cannot be read.

Exit code: 0 all good (warnings allowed), 1 problems found (nothing is written), 2 wrong call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

NOASSERTION = "NOASSERTION"
SPDX_VERSION = "SPDX-2.3"
NAMESPACE_BASE = "https://vilnaos.com/spdx/"
TOOL = "Tool: vilna-tools-sbom-image"
DEFAULT_VERSION = "0.0.0-dev"
PACKAGES_YAML = "image/packages.yaml"
KIWI_RE = re.compile(r"^obs/images/([^/]+)/([^/]+)\.kiwi$")
SPEC_RE = re.compile(r"^obs/packages/([^/]+)/([^/]+)\.spec$")
RPM_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+~-]*$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+~_-]*$")
RPM_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+~_:^-]*$")
CREATED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SPDX_ID_RE = re.compile(r"^SPDXRef-[A-Za-z0-9.\-]+$")
HEADER_RE = re.compile(r"SPDX-License-Identifier:\s*(.+?)\s*(?:\*/|-->)?\s*$", re.MULTILINE)
HEADER_LINES = 20
LICENSE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*\+?$")
SPDX_KEYWORDS = frozenset({"AND", "OR", "WITH"})
PURL_PREFIX = "pkg:rpm/opensuse/"
OWN_SUFFIX = "-branding-Vilna"
OWN_PREFIXES = ("vilna-", "Vilna-")
OWN_STATUSES = frozenset({"obs", "planned"})
SKIPPED_KIWI_TYPES = frozenset({"delete", "uninstall"})
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
        "%transfiletriggerin",
        "%transfiletriggerun",
        "%sourcelist",
        "%patchlist",
    ]
)
RELATIONSHIP_TYPES = frozenset(["DESCRIBES", "CONTAINS"])
VERSION_OPERATORS = frozenset({"<", ">", "=", "<=", ">=", "=="})
TAG_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)(?:\([^)]*\))?\s*:\s*(.*?)\s*$")
DEFINE_RE = re.compile(r"^%(?:define|global)\s+(\w+)\s+(.*?)\s*$")
MACRO_RE = re.compile(r"%\{\??(\w+)\}|%(\w+)")
PATTERN_RE = re.compile(r"pattern\(\)\s*=\s*(\S+)")
YAML_ENTRY_RE = re.compile(r"^\s+-\s*\{([^}]*)\}")
YAML_PAIR_RE = re.compile(r"([A-Za-z_]+)\s*:\s*([^,]*[^,\s])")


@dataclass(frozen=True)
class Problem:
    path: str
    text: str
    warning: bool = False

    def __str__(self) -> str:
        return f"{self.path}: {'warning: ' if self.warning else ''}{self.text}"


@dataclass
class Section:
    """A spec's preamble (the main package or one ``%package``): its name and dependencies."""

    name: str
    patterns: list[str] = field(default_factory=list)
    # (``Requires:`` value, where it stands): read only for a pattern, see patterns_of()
    requires: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Spec:
    path: str
    name: str
    sections: list[Section]


@dataclass
class Row:
    """One row of ``vilna.packages`` in packages.yaml."""

    name: str
    status: str
    license: str


@dataclass
class Image:
    name: str
    version: str
    path: str
    declared: str
    # package name -> where the image gets it ("kiwi image", "kiwi bootstrap", "pattern x")
    packages: dict[str, set[str]] = field(default_factory=dict)
    collections: list[str] = field(default_factory=list)
    spdx_id: str = ""


@dataclass
class Rpm:
    name: str
    installed_by: dict[str, set[str]] = field(default_factory=dict)  # image name -> origins
    version: str = ""
    comment: str = ""
    spdx_id: str = ""

    @property
    def purl(self) -> str:
        base = PURL_PREFIX + quote(self.name, safe="")
        return f"{base}@{quote(self.version, safe='')}" if self.version else base


def sanitize(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.\-]+", "-", text).strip("-") or "x"


# ---------------------------------------------------------------- licences


def license_expression(text: str | None) -> str | None:
    """``text`` when it is a simple SPDX expression (ids joined by AND, OR, WITH)."""
    if not text or len(text) > 200:
        return None
    tokens = text.replace("(", " ( ").replace(")", " ) ").split()
    depth = 0
    expect_id = True
    for token in tokens:
        if token == "(":
            if not expect_id:
                return None
            depth += 1
        elif token == ")":
            depth -= 1
            if depth < 0 or expect_id:
                return None
        elif token in SPDX_KEYWORDS:
            if expect_id:
                return None
            expect_id = True
        elif LICENSE_ID_RE.fullmatch(token) and expect_id:
            expect_id = False
        else:
            return None
    return text if depth == 0 and not expect_id else None


def header_license(text: str) -> str | None:
    head = "\n".join(text.splitlines()[:HEADER_LINES])
    found = HEADER_RE.search(head)
    return license_expression(found.group(1)) if found else None


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


def expand(value: str, macros: dict[str, str]) -> str:
    """Replace the macros we know (``%{name}``, ``%name``); an unknown one stays as it is."""

    def replace(found: re.Match[str]) -> str:
        key = found.group(1) or found.group(2)
        return macros.get(key, found.group(0))

    for _ in range(5):
        new = MACRO_RE.sub(replace, value)
        if new == value:
            break
        value = new
    return value


def requirement_names(value: str, where: str, problems: list[Problem]) -> list[str]:
    """The package names of one ``Requires:`` value; versions and operators are dropped.

    Files (``/usr/bin/x``), capabilities (``pkgconfig(x)``), rich dependencies and values
    with a macro that is still open are not package names: they are skipped, and the last
    two are warned about (a file or a capability is common and harmless).
    """
    if value.lstrip().startswith("("):
        problems.append(Problem(where, f"rich dependency not read: {value}", True))
        return []
    names: list[str] = []
    for part in value.split(","):
        tokens = part.split()
        skip_next = False
        for token in tokens:
            if skip_next:
                skip_next = False
                continue
            if token in VERSION_OPERATORS:
                skip_next = True
                continue
            if token.startswith("/") or "(" in token or ")" in token:
                continue
            if "%" in token:
                problems.append(Problem(where, f"macro not expanded in Requires: {token}", True))
                continue
            if not RPM_NAME_RE.fullmatch(token):
                problems.append(Problem(where, f"not a package name, skipped: {token!r}", True))
                continue
            names.append(token)
    return names


def parse_spec(path: str, text: str, problems: list[Problem]) -> Spec | None:
    """The sections of a spec that can carry ``Requires:`` and ``Provides: pattern()``.

    ``%if`` lines are not evaluated: the tags of every branch are read.
    """
    macros: dict[str, str] = {}
    main = Section("")
    sections = [main]
    current: Section | None = main
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        define = DEFINE_RE.match(line)
        if define:
            macros[define.group(1)] = expand(define.group(2), macros)
            continue
        head = line.split(None, 1)[0]
        if head in SECTIONS:
            current = None
            if head == "%package":
                name = _package_name(line.split()[1:], main.name)
                if name:
                    current = Section(expand(name, macros))
                    sections.append(current)
                else:
                    problems.append(Problem(path, f"line {number}: %package without a name"))
            continue
        tag = TAG_RE.match(line) if current is not None else None
        if current is None or not tag:
            continue
        key, value = tag.group(1).lower(), expand(tag.group(2), macros)
        if key == "name" and current is main and not main.name:
            main.name = value
            macros.setdefault("name", value)
        elif key == "version":
            macros.setdefault("version", value)
        elif key == "provides":
            current.patterns.extend(PATTERN_RE.findall(value))
        elif key == "requires":
            current.requires.append((value, f"{path}:{number}"))
    if not main.name:
        problems.append(Problem(path, "no Name tag"))
        return None
    return Spec(path, main.name, sections)


def _package_name(args: list[str], main: str) -> str:
    """The name a ``%package`` line gives: ``-n name`` or ``<main>-<suffix>``."""
    if "-n" in args:
        index = args.index("-n")
        return args[index + 1] if index + 1 < len(args) else ""
    plain = [a for a in args if not a.startswith("-")]
    return f"{main}-{plain[0]}" if plain and main else ""


def parse_yaml_rows(path: str, text: str, problems: list[Problem]) -> list[Row]:
    """The rows ``- {name: x, license: y, status: z}`` under ``vilna:`` / ``packages:``."""
    rows: list[Row] = []
    in_vilna = in_list = False
    for number, line in enumerate(text.splitlines(), 1):
        if line and not line[0].isspace() and not line.startswith("#"):
            in_vilna = line.split("#")[0].strip() == "vilna:"
            in_list = False
            continue
        if not in_vilna:
            continue
        stripped = line.split("#")[0].strip()
        if stripped.endswith(":") and not stripped.startswith("-"):
            in_list = stripped == "packages:"
            continue
        entry = YAML_ENTRY_RE.match(line)
        if not (in_list and entry):
            continue
        pairs = dict(YAML_PAIR_RE.findall(entry.group(1)))
        name = pairs.get("name", "")
        if not name:
            problems.append(Problem(path, f"line {number}: a package row without a name"))
            continue
        rows.append(Row(name, pairs.get("status", ""), pairs.get("license", "")))
    if not rows:
        problems.append(Problem(path, "no rows found under vilna: packages:", True))
    return rows


def parse_kiwi(path: str, text: str, problems: list[Problem]) -> Image | None:
    try:
        tree = ET.fromstring(text)
    except ET.ParseError as err:
        problems.append(Problem(path, f"not well-formed XML: {err}"))
        return None
    if tree.tag != "image":
        problems.append(Problem(path, f"the root is <{tree.tag}>, not <image>"))
        return None
    name = tree.get("name", "")
    folder = KIWI_RE.fullmatch(path)
    if not RPM_NAME_RE.fullmatch(name):
        problems.append(Problem(path, f"image name {name!r} is missing or not usable"))
        return None
    if folder and folder.group(1) != name:
        problems.append(Problem(path, f"image name {name!r} differs from its folder", True))
    version = (tree.findtext("preferences/version") or "").strip()
    if not VERSION_RE.fullmatch(version):
        problems.append(Problem(path, f"<version> {version!r} is missing or not usable"))
        return None
    declared = header_license(text)
    if declared is None:
        problems.append(Problem(path, "no usable SPDX-License-Identifier header", True))
    image = Image(name, version, path, declared or NOASSERTION)
    for block in tree.findall("packages"):
        kind = block.get("type", "")
        if kind in SKIPPED_KIWI_TYPES:
            continue
        origin = f"kiwi {kind or 'image'}"
        for item in block:
            wanted = (item.get("name") or "").strip()
            if item.tag == "package":
                if not RPM_NAME_RE.fullmatch(wanted):
                    problems.append(Problem(path, f"package name {wanted!r} not usable", True))
                    continue
                if item.get("profiles"):
                    problems.append(Problem(path, f"{wanted}: only in a profile, listed", True))
                image.packages.setdefault(wanted, set()).add(origin)
            elif item.tag in ("namedCollection", "collection") and wanted:
                image.collections.append(wanted)
    return image


# ---------------------------------------------------------------- the model


@dataclass
class Sources:
    specs: list[Spec]
    rows: list[Row]
    images: list[Image]
    folders: set[str]


def collect_sources(root: Path, tracked: Sequence[str], problems: list[Problem]) -> Sources:
    specs: list[Spec] = []
    folders: set[str] = set()
    for path in tracked:
        found = SPEC_RE.fullmatch(path)
        if found:
            folders.add(found.group(1))
            text = read_text(root, path, problems)
            spec = parse_spec(path, text, problems) if text is not None else None
            if spec:
                specs.append(spec)
    rows: list[Row] = []
    if PACKAGES_YAML in tracked:
        text = read_text(root, PACKAGES_YAML, problems)
        rows = parse_yaml_rows(PACKAGES_YAML, text, problems) if text is not None else []
    else:
        problems.append(Problem(PACKAGES_YAML, "is not tracked", True))
    images: list[Image] = []
    for path in tracked:
        if KIWI_RE.fullmatch(path):
            text = read_text(root, path, problems)
            image = parse_kiwi(path, text, problems) if text is not None else None
            if image:
                images.append(image)
    names = [i.name for i in images]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        problems.append(Problem("obs/images", f"image {dup} is described twice"))
    return Sources(specs, rows, sorted(images, key=lambda i: i.name), folders)


def own_names(src: Sources) -> set[str]:
    """Names of our own packages and images (see the module docstring)."""
    names = set(src.folders)
    for spec in src.specs:
        names.add(spec.name)
        names.update(s.name for s in spec.sections if s.name)
    names.update(r.name for r in src.rows if r.status in OWN_STATUSES)
    names.update(i.name for i in src.images)
    return names


def is_own(name: str, own: set[str]) -> bool:
    return name in own or name.endswith(OWN_SUFFIX) or name.startswith(OWN_PREFIXES)


def patterns_of(src: Sources, problems: list[Problem]) -> dict[str, list[str]]:
    """Pattern name -> the ``Requires`` of the packages that provide it."""
    found: dict[str, list[str]] = {}
    for spec in src.specs:
        for section in spec.sections:
            for pattern in section.patterns:
                names = found.setdefault(pattern, [])
                for value, where in section.requires:
                    names.extend(requirement_names(value, where, problems))
    return found


def expand_collections(src: Sources, problems: list[Problem]) -> None:
    """Give every image the ``Requires`` of the patterns it installs."""
    patterns = patterns_of(src, problems)
    for image in src.images:
        for collection in image.collections:
            if collection not in patterns:
                problems.append(
                    Problem(
                        image.path,
                        f"pattern {collection} is not provided by a spec of this repo: "
                        "its packages are not listed",
                        True,
                    )
                )
                continue
            for name in patterns[collection]:
                image.packages.setdefault(name, set()).add(f"pattern {collection}")


@dataclass
class Model:
    rpms: list[Rpm]
    nested: set[tuple[str, str]]  # (outer image, inner image): the outer lists the inner one
    own_skipped: int  # our own packages the images install; tools/sbom.py has them


def build_model(src: Sources, versions: dict[str, str], problems: list[Problem]) -> Model:
    """The openSUSE packages and the images that are packages of other images."""
    own = own_names(src)
    expand_collections(src, problems)
    image_names = {i.name for i in src.images}
    rpms: dict[str, Rpm] = {}
    nested: set[tuple[str, str]] = set()
    own_seen: set[str] = set()
    for image in src.images:
        for name, origins in image.packages.items():
            if name in image_names and name != image.name:
                nested.add((image.name, name))
                continue
            if is_own(name, own):
                own_seen.add(name)
                if name not in own:
                    problems.append(
                        Problem(
                            image.path,
                            f"{name}: looks like ours but no spec or row of packages.yaml "
                            "names it; not listed",
                            True,
                        )
                    )
                continue
            rpm = rpms.setdefault(name, Rpm(name))
            rpm.installed_by.setdefault(image.name, set()).update(origins)
    by_row = {r.name: r for r in src.rows}
    for rpm in rpms.values():
        row = by_row.get(rpm.name)
        if row and row.license:
            rpm.comment = (
                f"image/packages.yaml notes licence {row.license} (status {row.status}); "
                "not read from the RPM"
            )
        if rpm.name in versions:
            rpm.version = versions[rpm.name]
    for row in src.rows:
        if row.status == "upstream" and row.name not in rpms:
            problems.append(
                Problem(
                    PACKAGES_YAML,
                    f"{row.name}: status upstream, but no image or pattern installs it",
                    True,
                )
            )
    for name in sorted(set(versions) - set(rpms)):
        problems.append(Problem("versions", f"{name}: no image installs it (a dependency?)", True))
    missing = sum(1 for r in rpms.values() if not r.version)
    if missing:
        problems.append(
            Problem("versions", f"{missing} of {len(rpms)} packages have no version", True)
        )
    return Model(sorted(rpms.values(), key=lambda r: r.name), nested, len(own_seen))


# ---------------------------------------------------------------- the document


class IdPool:
    """Unique SPDXIDs: ``SPDXRef-<prefix>-<name>``, a number added on a clash."""

    def __init__(self) -> None:
        self.used = {"SPDXRef-DOCUMENT"}

    def make(self, prefix: str, name: str) -> str:
        base = f"SPDXRef-{prefix}-{sanitize(name)}"
        candidate, number = base, 2
        while candidate in self.used:
            candidate, number = f"{base}-{number}", number + 1
        self.used.add(candidate)
        return candidate


def image_package(image: Image) -> dict:
    return {
        "SPDXID": image.spdx_id,
        "name": image.name,
        "versionInfo": image.version,
        "supplier": "Organization: Vilna OS project",
        "downloadLocation": NOASSERTION,
        "filesAnalyzed": False,
        "licenseConcluded": image.declared,
        "licenseDeclared": image.declared,
        "copyrightText": NOASSERTION,
        "primaryPackagePurpose": "OPERATING-SYSTEM",
        "sourceInfo": f"image description {image.path}",
    }


def rpm_package(rpm: Rpm) -> dict:
    where = "; ".join(
        f"{image}: {', '.join(sorted(origins))}"
        for image, origins in sorted(rpm.installed_by.items())
    )
    pkg: dict = {
        "SPDXID": rpm.spdx_id,
        "name": rpm.name,
        "versionInfo": rpm.version or NOASSERTION,
        "supplier": NOASSERTION,
        "downloadLocation": NOASSERTION,
        "filesAnalyzed": False,
        "licenseConcluded": NOASSERTION,
        "licenseDeclared": NOASSERTION,
        "copyrightText": NOASSERTION,
        "externalRefs": [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": rpm.purl,
            }
        ],
        "sourceInfo": f"installed by {where}",
    }
    if rpm.comment:
        pkg["comment"] = rpm.comment
    return pkg


def relationship(source: str, kind: str, target: str) -> dict:
    return {"spdxElementId": source, "relationshipType": kind, "relatedSpdxElement": target}


def content_hash(doc: dict) -> str:
    """SHA-256 of the document without ``created`` and the namespace (they depend on time)."""
    body = json.loads(json.dumps(doc))
    body["creationInfo"].pop("created", None)
    body.pop("documentNamespace", None)
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Built:
    doc: dict
    problems: list[Problem]
    own_skipped: int


def build(
    root: Path,
    files: Iterable[str],
    version: str,
    created: str,
    versions: dict[str, str] | None = None,
) -> tuple[dict, list[Problem]]:
    """The SPDX document for the tracked ``files`` under ``root``, and what went wrong."""
    built = build_all(root, files, version, created, versions)
    return built.doc, built.problems


def build_all(
    root: Path,
    files: Iterable[str],
    version: str,
    created: str,
    versions: dict[str, str] | None = None,
) -> Built:
    problems: list[Problem] = []
    tracked = sorted(set(files))
    src = collect_sources(root, tracked, problems)
    model = build_model(src, versions or {}, problems)
    rpms = model.rpms
    ids = IdPool()
    for image in src.images:
        image.spdx_id = ids.make("Image", image.name)
    for rpm in rpms:
        rpm.spdx_id = ids.make("Rpm", rpm.name)
    image_id = {i.name: i.spdx_id for i in src.images}
    rpm_id = {r.name: r.spdx_id for r in rpms}
    relationships = [relationship("SPDXRef-DOCUMENT", "DESCRIBES", i.spdx_id) for i in src.images]
    contains = {(image_id[image], rpm_id[rpm.name]) for rpm in rpms for image in rpm.installed_by}
    contains.update((image_id[outer], image_id[inner]) for outer, inner in model.nested)
    relationships += [relationship(a, "CONTAINS", b) for a, b in sorted(contains)]
    doc: dict = {
        "spdxVersion": SPDX_VERSION,
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"Vilna OS images {version}",
        "creationInfo": {
            "created": created,
            "creators": [TOOL],
            "comment": "The openSUSE packages that the image descriptions, the pattern and "
            "image/packages.yaml name, without the packages the solver adds; our own "
            "packages are in the SBOM of tools/sbom.py. Versions are NOASSERTION until "
            "they are filled from the OBS build result.",
        },
        "packages": [image_package(i) for i in src.images] + [rpm_package(r) for r in rpms],
        "relationships": relationships,
    }
    doc["documentNamespace"] = (
        f"{NAMESPACE_BASE}vilna-image-{quote(version, safe='')}-{content_hash(doc)}"
    )
    return Built(doc, problems, model.own_skipped)


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
    name, version = pkg.get("name"), pkg.get("versionInfo")
    if purls and isinstance(name, str):
        expected = PURL_PREFIX + quote(name, safe="")
        if version not in (NOASSERTION, None) and isinstance(version, str):
            expected += "@" + quote(version, safe="")
        if purls != [expected]:
            problems.append(Problem(label, f"purl must be {expected}, found {purls}"))
    return problems


def check_relationships(doc: dict, ids: set[str], rpm_ids: set[str]) -> list[Problem]:
    problems: list[Problem] = []
    rels = doc.get("relationships")
    if not isinstance(rels, list):
        return [Problem("relationships", "must be a list")]
    seen: set[tuple[str, str, str]] = set()
    contained: set[str] = set()
    touched: set[str] = set()
    described: set[str] = set()
    for n, rel in enumerate(rels):
        where = f"relationships[{n}]"
        if not isinstance(rel, dict):
            problems.append(Problem(where, "a relationship must be an object"))
            continue
        source = rel.get("spdxElementId")
        kind = rel.get("relationshipType")
        target = rel.get("relatedSpdxElement")
        if not isinstance(kind, str) or kind not in RELATIONSHIP_TYPES:
            problems.append(Problem(where, f"unknown relationshipType {kind!r}"))
        for role, element in (("spdxElementId", source), ("relatedSpdxElement", target)):
            if not isinstance(element, str) or element not in ids:
                problems.append(
                    Problem(where, f"{role} {element!r} is not an element of the document")
                )
        key = (str(source), str(kind), str(target))
        if key in seen:
            problems.append(Problem(where, f"{source} {kind} {target} is listed twice"))
        seen.add(key)
        touched.update(str(x) for x in (source, target))
        if kind == "DESCRIBES" and source == "SPDXRef-DOCUMENT":
            described.add(str(target))
        if kind == "CONTAINS":
            contained.add(str(target))
    for ident in sorted(ids - touched):
        problems.append(Problem(ident, "is in no relationship"))
    for ident in sorted(rpm_ids - contained):
        problems.append(Problem(ident, "is not contained in any image"))
    if not described:
        problems.append(Problem("relationships", "the document describes no image"))
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
    if not isinstance(created, str) or not CREATED_RE.fullmatch(created):
        problems.append(Problem("creationInfo.created", "must look like 2026-12-02T09:00:00Z"))
    if not isinstance(info, dict) or not info.get("creators"):
        problems.append(Problem("creationInfo.creators", "is missing"))
    packages = doc.get("packages")
    if not isinstance(packages, list) or not packages:
        return problems + [Problem("packages", "must be a non-empty list")]
    ids: set[str] = {"SPDXRef-DOCUMENT"}
    rpm_ids: set[str] = set()
    purls: dict[str, str] = {}
    for n, pkg in enumerate(packages):
        where = f"packages[{n}]"
        problems.extend(check_package(pkg, where))
        if not isinstance(pkg, dict):
            continue
        ident = pkg.get("SPDXID")
        if not isinstance(ident, str) or not SPDX_ID_RE.fullmatch(ident):
            problems.append(
                Problem(where, f"SPDXID {ident!r} must look like SPDXRef-<letters, digits, . or ->")
            )
            continue
        if ident in ids:
            problems.append(Problem(where, f"SPDXID {ident} is used twice"))
        ids.add(ident)
        refs = pkg.get("externalRefs", [])
        for ref in refs if isinstance(refs, list) else []:
            if isinstance(ref, dict) and ref.get("referenceType") == "purl":
                locator = str(ref.get("referenceLocator"))
                rpm_ids.add(ident)
                if locator in purls:
                    problems.append(Problem(where, f"purl {locator} is already {purls[locator]}"))
                purls[locator] = ident
    return problems + check_relationships(doc, ids, rpm_ids)


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
        if not CREATED_RE.fullmatch(created_arg):
            raise ValueError(f"--created {created_arg!r} must look like 2026-12-02T09:00:00Z")
        try:
            datetime.strptime(created_arg, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as err:
            raise ValueError(
                f"--created {created_arg!r} must look like 2026-12-02T09:00:00Z"
            ) from err
        return created_arg
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_versions(path: str) -> dict[str, str]:
    """``--versions`` file: a JSON object ``{"<rpm name>": "<version>-<release>"}``."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError(f"--versions {path}: cannot read: {err}") from err
    if not isinstance(data, dict):
        raise ValueError(f"--versions {path}: must be a JSON object name -> version")
    for name, value in data.items():
        if not RPM_NAME_RE.fullmatch(name):
            raise ValueError(f"--versions {path}: {name!r} is not a package name")
        if not isinstance(value, str) or not RPM_VERSION_RE.fullmatch(value):
            raise ValueError(f"--versions {path}: {name}: {value!r} is not a usable version")
    return data


def render(doc: dict) -> str:
    return json.dumps(doc, indent=2) + "\n"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sbom_image.py",
        description="Build the SPDX 2.3 JSON SBOM of the openSUSE packages in the Vilna OS images.",
    )
    p.add_argument("--version", default=DEFAULT_VERSION, help="version for the document name")
    p.add_argument("--out", metavar="FILE", help="write the document here, not to stdout")
    p.add_argument("--created", metavar="TIME", help="created time, like 2026-12-02T09:00:00Z")
    p.add_argument("--versions", metavar="FILE", help="JSON {rpm name: version-release}")
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
    if not VERSION_RE.fullmatch(args.version):
        print(
            f"--version {args.version!r}: use letters, digits and . + ~ _ - only", file=sys.stderr
        )
        return 2
    try:
        created = created_time(args.created, dict(os.environ), datetime.now(UTC))
        versions = load_versions(args.versions) if args.versions else {}
    except ValueError as err:
        print(err, file=sys.stderr)
        return 2
    root = Path(args.root)
    try:
        files = tracked_files(root)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files in {root}: {err}", file=sys.stderr)
        return 2
    built = build_all(root, files, args.version, created, versions)
    doc = built.doc
    problems = built.problems + check_document(doc)
    for problem in problems:
        print(problem, file=sys.stderr)
    errors = sum(not p.warning for p in problems)
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
        images = sum(1 for p in doc["packages"] if p["SPDXID"].startswith("SPDXRef-Image-"))
        print(
            f"SBOM ok: {images} images, {len(doc['packages']) - images} openSUSE packages, "
            f"{len(doc['relationships'])} relationships, {built.own_skipped} own packages "
            f"left to tools/sbom.py, {len(problems)} warnings",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
