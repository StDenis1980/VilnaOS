#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the RPM spec files of the OBS packages (``obs/packages/<name>/``).

The spec files are read as text; ``rpm`` and ``rpmspec`` are not needed and
not run. Simple macros (``%define``, ``%global``, ``%{name}``,
``%{version}``, ``%{release}``) are expanded; anything else (conditionals,
macros from rpm) is left as it is.

For every package folder the checker verifies:

* layout: the folder has exactly one spec, ``<folder>/<folder>.spec``, and
  ``Name`` (after the simple macros) equals the folder name; a missing
  ``<folder>.changes`` is a warning;
* the main preamble has Name, Version, Release, Summary and License (no URL is
  a warning), and BuildArch or at least one ``%build``/``%install`` section;
  every ``%package`` has a Summary; a tag that takes one value is not
  repeated in a preamble; a ``SourceN``/``PatchN`` number is not used twice;
* ``Version`` has no ``-`` and only the characters rpm allows
  (``0-9 A-Z a-z . _ + ~ ^``); ``Release`` is ``0`` (OBS counts the builds);
* sections: the main package and every ``%package`` have a ``%description``;
  every ``%package`` has a ``%files`` (a main package without ``%files`` is
  not built, as rpm does); a ``%description`` or ``%files`` does not name a
  package that is not declared, and no package has two of them;
* sources: every ``SourceN``/``PatchN`` that is not a URL is a tracked file of
  the folder, a file ``_service`` downloads (``download_url``, with a
  ``verify_file`` that checks it) or ``vendor.tar.xz`` of a tarball with
  ``cargo`` in ``obs/tools/obs_sync.py``; ``%{name}-%{version}.tar.*`` is
  made at upload time (a warning if neither obs_sync.py ``TARBALLS`` nor
  ``_service`` makes it); every other tracked file of the folder (apart from
  the spec, ``.changes``, ``_service`` and ``*rpmlintrc``) is used as a
  Source or Patch, else a warning: it does not get into the package;
* ``%changelog``: one, the last section and empty: openSUSE keeps the entries
  in ``<folder>.changes``, whose entries start with a line of 67 ``-`` and a
  ``Www Mmm DD HH:MM:SS UTC YYYY - author`` line with the right weekday, and
  go from the newest to the oldest;
* licences: ``License`` is an SPDX expression (AND, OR, WITH, brackets) of
  known identifiers; a package built from this repository carries the licence
  of its code folder (LICENSING.md: Apache-2.0 for ``privacy-gateway/``,
  ``mcp/``, ``sdk/``, GPL-3.0-or-later elsewhere), CC-BY-SA-4.0 only when the
  CC-BY-SA-4.0 part of LICENSING.md lists its folder or the package, other
  licences only with vendored crates (``vendor.tar.xz``); a third-party
  program (a folder with ``_service``) has its own licence: it is only listed
  as a warning;
* the first lines of the spec have ``# SPDX-License-Identifier: <licence>``
  with the licence of the code folder (as ``tools/check_spdx.py`` wants for
  code);
* across specs: no RPM name is built by two spec files.

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but
do not fail the check; they mark things the repo already does today (see
``LEGACY``).

Only ``git ls-files`` is run (to find the files). Nothing else is executed,
nothing is read from the network and nothing is written.

Run: python3 tools/check_spec.py [obs/packages/NAME ...]
Without arguments every folder with tracked files in ``obs/packages`` is checked.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

PACKAGES = "obs/packages"
OBS_SYNC = "obs/tools/obs_sync.py"
LICENSING = "LICENSING.md"
SPDX_HEADER = "# SPDX-License-Identifier: "
HEADER_LINES = 10
REQUIRED_MAIN = ("Name", "Version", "Release", "Summary", "License")
SINGLE_TAGS = frozenset(
    {"name", "version", "release", "summary", "license", "url", "buildarch", "epoch", "group"}
)
# Lines that start a new section of a spec (everything else is in the section).
SECTIONS = frozenset(
    {
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
        "%preuninstall",
        "%verifyscript",
        "%triggerin",
        "%triggerun",
        "%triggerpostun",
        "%triggerprein",
        "%filetriggerin",
        "%filetriggerun",
        "%filetriggerpostun",
        "%transfiletriggerin",
        "%transfiletriggerun",
        "%transfiletriggerpostun",
    }
)
TAG = re.compile(r"([A-Za-z][A-Za-z0-9]*)(\([^)]*\))?:\s*(.*)")
DEFINE = re.compile(r"%(?:define|global)\s+(\w+)\s+(.*\S)")
MACRO = re.compile(r"%\{\??(\w+)\}|%(\w+)")
VERSION_CHARS = re.compile(r"[0-9A-Za-z._+~^]+")
APACHE_DIRS = ("privacy-gateway/", "mcp/", "sdk/")
DEFAULT_LICENCE = "GPL-3.0-or-later"
CC_LICENCE = "CC-BY-SA-4.0"
# SPDX identifiers the checker knows (https://spdx.org/licenses/).
KNOWN_LICENCES = frozenset(
    {
        "0BSD",
        "AGPL-3.0-only",
        "AGPL-3.0-or-later",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "BSL-1.0",
        "CC-BY-4.0",
        "CC-BY-SA-4.0",
        "CC0-1.0",
        "GPL-2.0-only",
        "GPL-2.0-or-later",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "ISC",
        "LGPL-2.1-only",
        "LGPL-2.1-or-later",
        "LGPL-3.0-only",
        "LGPL-3.0-or-later",
        "MIT",
        "MPL-2.0",
        "OFL-1.1",
        "Python-2.0",
        "Unicode-3.0",
        "Unicode-DFS-2016",
        "Unlicense",
        "Zlib",
    }
)
KNOWN_EXCEPTIONS = frozenset({"LLVM-exception", "GCC-exception-3.1", "Classpath-exception-2.0"})
# Files of a package folder that are not sources.
NOT_SOURCES = ("_service",)
CHANGES_SEPARATOR = "-" * 67
CHANGES_HEADER = re.compile(
    r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r" ([ \d]\d) (\d\d):(\d\d):(\d\d) UTC (\d{4}) - \S.*"
)
# Old problems of the repo: (path, rule) pairs reported as warnings.
LEGACY: frozenset[tuple[str, str]] = frozenset(
    {
        # Two entries of 2026-11 are out of order (fixing it rewrites history).
        ("obs/packages/gloxi/gloxi.changes", "changes-order"),
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


@dataclass
class Package:
    """One binary package of a spec: the main one or a ``%package``."""

    name: str
    line: int
    tags: list[tuple[str, str, int]] = field(default_factory=list)
    descriptions: int = 0
    files: int = 0

    def values(self, tag: str) -> list[str]:
        return [value for name, value, _ in self.tags if name.lower() == tag.lower()]


@dataclass
class Spec:
    """What the checker reads from a spec file."""

    packages: list[Package]
    macros: dict[str, str]
    # (keyword, package name or "", line) of every %description and %files
    sections: list[tuple[str, str, int]]
    # (line, section keyword) of every section start, in order
    order: list[tuple[int, str]]
    changelog_lines: list[str]
    lang_package: bool = False
    python_subpackages: bool = False

    @property
    def main(self) -> Package:
        return self.packages[0]


def expand(value: str, macros: dict[str, str]) -> str:
    """Expand the known macros of ``value``; unknown ones stay as they are."""
    for _ in range(5):
        new = MACRO.sub(lambda m: macros.get(m.group(1) or m.group(2), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


def section_name(words: list[str], main: str, macros: dict[str, str]) -> str:
    """The package a ``%package``/``%description``/``%files`` line is about."""
    args = words[1:]
    full = False
    names: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg == "-n":
            full = True
        elif arg == "-f":
            skip = True
        elif not arg.startswith("-"):
            names.append(arg)
    if not names:
        return main
    name = expand(names[0], macros)
    return name if full else f"{main}-{name}"


def parse_spec(text: str) -> Spec:
    """Read the preambles and sections of a spec (no rpm, simple macros only)."""
    main = Package("", 1)
    spec = Spec([main], {}, [], [], [])
    current: Package | None = main
    in_changelog = False
    for number, line in enumerate(text.splitlines(), start=1):
        words = line.split()
        keyword = words[0] if words else ""
        if keyword in SECTIONS:
            spec.order.append((number, keyword))
            in_changelog = keyword == "%changelog"
            current = None
            if keyword == "%package":
                name = section_name(words, main.name, spec.macros)
                current = Package(name, number)
                spec.packages.append(current)
            elif keyword in ("%description", "%files"):
                name = section_name(words, main.name, spec.macros)
                if keyword == "%files" and "%{python_files}" in words:
                    name = main.name
                spec.sections.append((keyword, name, number))
            continue
        if in_changelog:
            spec.changelog_lines.append(line)
            continue
        define = DEFINE.match(line.strip())
        if define:
            spec.macros[define.group(1)] = define.group(2)
            continue
        if keyword == "%lang_package":
            spec.lang_package = True
        elif keyword == "%python_subpackages":
            spec.python_subpackages = True
        if current is None:
            continue
        tag = TAG.fullmatch(line.strip())
        if tag and not line.startswith((" ", "\t", "#", "%")):
            value = tag.group(3).strip()
            current.tags.append((tag.group(1), value, number))
            if current is main and tag.group(1).lower() in ("name", "version", "release"):
                key = tag.group(1).lower()
                spec.macros.setdefault(key, expand(value, spec.macros))
                if key == "name":
                    main.name = spec.macros["name"]
    for package in spec.packages:
        package.descriptions = sum(
            1 for kw, name, _ in spec.sections if kw == "%description" and name == package.name
        )
        package.files = sum(
            1 for kw, name, _ in spec.sections if kw == "%files" and name == package.name
        )
    return spec


def built_packages(spec: Spec) -> list[str]:
    """The RPM names rpmbuild makes from the spec (packages with ``%files``)."""
    names = [p.name for p in spec.packages if p.files]
    if spec.lang_package and any(n == f"{spec.main.name}-lang" for _, n, _ in spec.sections):
        names.append(f"{spec.main.name}-lang")
    if spec.python_subpackages and spec.main.name.startswith("python-"):
        flavors = spec.macros.get("pythons", "python3").split()
        rest = spec.main.name[len("python") :]
        names = [n for n in names if n != spec.main.name]
        names[:0] = [f"{flavor}{rest}" for flavor in flavors]
    return sorted(set(names))


def check_preambles(path: str, spec: Spec) -> list[Problem]:
    problems: list[Problem] = []
    main = spec.main
    present = {name.lower() for name, _, _ in main.tags}
    for tag in REQUIRED_MAIN:
        if tag.lower() not in present:
            problems.append(Problem(path, f"the main package has no {tag}:", rule="required-tag"))
    if "url" not in present:
        problems.append(
            Problem(path, "the main package has no URL:", warning=True, rule="required-tag")
        )
    if "buildarch" not in present and not any(
        keyword in ("%build", "%install") for _, keyword in spec.order
    ):
        problems.append(
            Problem(path, "no BuildArch: and no %build or %install section", rule="required-tag")
        )
    for package in spec.packages[1:]:
        if not package.values("Summary"):
            problems.append(
                Problem(
                    path,
                    f"line {package.line}: %package {package.name} has no Summary:",
                    rule="required-tag",
                )
            )
    numbers: dict[str, int] = {}
    for package in spec.packages:
        seen: set[str] = set()
        for name, _, line in package.tags:
            key = name.lower()
            if key in SINGLE_TAGS and key in seen:
                problems.append(
                    Problem(
                        path,
                        f"line {line}: {name}: is set twice for {package.name}",
                        rule="duplicate-tag",
                    )
                )
            seen.add(key)
            numbered = re.fullmatch(r"(source|patch)(\d*)", key)
            if numbered:
                label = f"{numbered.group(1).capitalize()}{numbered.group(2) or '0'}"
                if label in numbers:
                    problems.append(
                        Problem(path, f"line {line}: {label} is used twice", rule="duplicate-tag")
                    )
                numbers[label] = line
    return problems


def check_version(path: str, spec: Spec) -> list[Problem]:
    problems: list[Problem] = []
    version = spec.macros.get("version", "")
    if version and "%" not in version and not VERSION_CHARS.fullmatch(version):
        problems.append(
            Problem(
                path, f"Version {version!r} may only have 0-9 A-Z a-z . _ + ~ ^", rule="version"
            )
        )
    release = spec.macros.get("release")
    if release is not None and release != "0":
        problems.append(
            Problem(path, f"Release is {release!r}, not 0 (OBS counts the builds)", rule="release")
        )
    return problems


def check_sections(path: str, spec: Spec) -> list[Problem]:
    problems: list[Problem] = []
    declared = {p.name for p in spec.packages}
    if spec.lang_package:
        declared.add(f"{spec.main.name}-lang")
    for keyword, name, line in spec.sections:
        if name not in declared:
            problems.append(
                Problem(
                    path,
                    f"line {line}: {keyword} for {name}, which is not declared",
                    rule="sections",
                )
            )
    for package in spec.packages:
        if package.descriptions == 0:
            problems.append(Problem(path, f"{package.name} has no %description", rule="sections"))
        if package is not spec.main and package.files == 0:
            problems.append(Problem(path, f"{package.name} has no %files", rule="sections"))
        for keyword, count in (("%description", package.descriptions), ("%files", package.files)):
            if count > 1:
                problems.append(
                    Problem(path, f"{package.name} has {count} {keyword} sections", rule="sections")
                )
    return problems


def check_changelog(path: str, spec: Spec) -> list[Problem]:
    count = sum(keyword == "%changelog" for _, keyword in spec.order)
    if count > 1:
        return [Problem(path, f"{count} %changelog sections", rule="changelog")]
    if not spec.order or spec.order[-1][1] != "%changelog":
        has = any(keyword == "%changelog" for _, keyword in spec.order)
        message = "%changelog is not the last section" if has else "there is no %changelog"
        return [Problem(path, message, rule="changelog")]
    if any(line.strip() for line in spec.changelog_lines):
        return [
            Problem(
                path,
                "%changelog is not empty: the entries belong in the .changes file",
                rule="changelog",
            )
        ]
    return []


def check_changes(path: str, text: str) -> list[Problem]:
    """The ``.changes`` file of ``osc vc``: format and order of the entries."""
    problems: list[Problem] = []
    lines = text.splitlines()
    if not lines or lines[0] != CHANGES_SEPARATOR:
        problems.append(
            Problem(path, "the file does not start with a line of 67 '-'", rule="changes-format")
        )
    previous: datetime | None = None
    for index, line in enumerate(lines):
        if line != CHANGES_SEPARATOR:
            if re.fullmatch(r"-{20,}", line):
                problems.append(
                    Problem(
                        path,
                        f"line {index + 1}: a separator of {len(line)} '-', not 67",
                        rule="changes-format",
                    )
                )
            continue
        header = lines[index + 1] if index + 1 < len(lines) else ""
        match = CHANGES_HEADER.fullmatch(header)
        if not match:
            problems.append(
                Problem(
                    path,
                    f"line {index + 2}: not 'Www Mmm DD HH:MM:SS UTC YYYY - author'",
                    rule="changes-format",
                )
            )
            continue
        stamp = " ".join(header.split(" - ", 1)[0].split()).replace(" UTC ", " ")
        try:
            when = datetime.strptime(stamp, "%a %b %d %H:%M:%S %Y")
        except ValueError:
            problems.append(
                Problem(
                    path,
                    f"line {index + 2}: the date does not exist",
                    rule="changes-format",
                )
            )
            continue
        if when.strftime("%a") != match.group(1):
            problems.append(
                Problem(
                    path,
                    f"line {index + 2}: {when:%Y-%m-%d} is a {when:%a}, not {match.group(1)}",
                    rule="changes-format",
                )
            )
        if previous is not None and when > previous:
            problems.append(
                Problem(
                    path,
                    f"line {index + 2}: the entry of {when:%Y-%m-%d %H:%M} is newer "
                    f"than the one above it ({previous:%Y-%m-%d %H:%M})",
                    rule="changes-order",
                )
            )
        previous = when
    return problems


def split_licence(expression: str) -> list[str] | None:
    """The tokens of an SPDX expression, or None when brackets do not match."""
    tokens = re.findall(r"\(|\)|[^\s()]+", expression)
    depth = 0
    for token in tokens:
        depth += {"(": 1, ")": -1}.get(token, 0)
        if depth < 0:
            return None
    return tokens if depth == 0 else None


def licence_ids(path: str, line: int, expression: str) -> tuple[set[str], list[Problem]]:
    """The licence identifiers of a ``License:`` value, with syntax problems."""
    where = f"line {line}: License {expression!r}"
    tokens = split_licence(expression)
    if not tokens:
        return set(), [
            Problem(
                path, f"{where}: the brackets do not match or it is empty", rule="license-syntax"
            )
        ]
    ids: set[str] = set()
    problems: list[Problem] = []
    expect_id = True
    after_with = False
    for token in tokens:
        if token in ("(", ")"):
            continue
        if token in ("AND", "OR", "WITH"):
            if expect_id:
                problems.append(
                    Problem(
                        path, f"{where}: {token} without a licence before it", rule="license-syntax"
                    )
                )
            expect_id = True
            after_with = token == "WITH"
            continue
        if not expect_id:
            problems.append(
                Problem(
                    path,
                    f"{where}: {token!r} follows a licence without AND/OR",
                    rule="license-syntax",
                )
            )
        known = KNOWN_EXCEPTIONS if after_with else KNOWN_LICENCES
        if token.removesuffix("+") not in known or token.endswith("+"):
            kind = "licence exception" if after_with else "SPDX licence identifier"
            problems.append(
                Problem(path, f"{where}: {token!r} is not a known {kind}", rule="license-syntax")
            )
        elif not after_with:
            ids.add(token)
        expect_id = False
        after_with = False
    if expect_id:
        problems.append(Problem(path, f"{where}: it ends with an operator", rule="license-syntax"))
    return ids, problems


@dataclass(frozen=True)
class Context:
    """What the checks need from outside the package folder."""

    tracked: frozenset[str]  # tracked files under obs/packages, repo-relative
    tarballs: dict[str, tuple[str, bool]]  # package -> (code folder, has cargo vendor)
    cc_places: frozenset[str]  # paths and names LICENSING.md lists under CC-BY-SA-4.0


def code_folder(folder: str, context: Context) -> str:
    """The repo folder with the code of the package (obs_sync TARBALLS, else the package)."""
    name = PurePosixPath(folder).name
    return context.tarballs.get(name, (folder, False))[0]


def cc_allowed(folder: str, context: Context) -> bool:
    name = PurePosixPath(folder).name
    code = code_folder(folder, context).rstrip("/") + "/"
    return any(
        place == name or place.startswith(code) or code.startswith(place.rstrip("/") + "/")
        for place in context.cc_places
    )


def check_licences(path: str, folder: str, spec: Spec, context: Context) -> list[Problem]:
    problems: list[Problem] = []
    upstream = f"{folder}/_service" in context.tracked
    vendored = any(
        expand(v, spec.macros) == "vendor.tar.xz"
        for p in spec.packages
        for n, v, _ in p.tags
        if n.lower().startswith("source")
    )
    code = code_folder(folder, context).rstrip("/") + "/"
    own = own_licence(folder, context)
    for package in spec.packages:
        for name, value, line in package.tags:
            if name.lower() != "license":
                continue
            ids, found = licence_ids(path, line, expand(value, spec.macros))
            problems += found
            if found:
                continue
            if upstream:
                problems.append(
                    Problem(
                        path,
                        f"line {line}: License of {package.name} is {value}: a third-party "
                        "program (_service) with its own licence",
                        warning=True,
                        rule="license-upstream",
                    )
                )
                continue
            where = f"line {line}: License of {package.name}"
            if own not in ids:
                problems.append(
                    Problem(
                        path, f"{where} lacks {own}, the licence of {code}", rule="license-folder"
                    )
                )
            if CC_LICENCE in ids and not cc_allowed(folder, context):
                problems.append(
                    Problem(
                        path,
                        f"{where} has {CC_LICENCE}, but LICENSING.md does not list "
                        f"{code} or {PurePosixPath(folder).name} under it",
                        rule="license-folder",
                    )
                )
            other = sorted(ids - {own, CC_LICENCE})
            if other and not vendored:
                problems.append(
                    Problem(
                        path,
                        f"{where} has {', '.join(other)}: only vendored crates "
                        "(vendor.tar.xz) or upstream code (_service) bring other licences",
                        rule="license-folder",
                    )
                )
    return problems


def service_files(text: str) -> tuple[set[str], set[str]]:
    """(files download_url fetches, files verify_file checks) of a ``_service``."""
    downloads: set[str] = set()
    verified: set[str] = set()
    root = ET.fromstring(text)
    for service in root.iter("service"):
        params = {p.get("name"): (p.text or "").strip() for p in service.iter("param")}
        if service.get("name") == "download_url":
            filename = params.get("filename") or PurePosixPath(params.get("path", "")).name
            if filename:
                downloads.add(filename)
        elif service.get("name") == "verify_file" and params.get("checksum"):
            verified.add(params.get("file", "").rsplit(":", 1)[-1])
    return downloads, verified


def check_sources(
    path: str, folder: str, spec: Spec, context: Context, service: str | None
) -> list[Problem]:
    problems: list[Problem] = []
    name = PurePosixPath(folder).name
    downloads: set[str] = set()
    verified: set[str] = set()
    if service is not None:
        try:
            downloads, verified = service_files(service)
        except ET.ParseError as err:
            problems.append(
                Problem(f"{folder}/_service", f"not well-formed XML: {err}", rule="source")
            )
    tarball = context.tarballs.get(name)
    made: set[str] = set()
    if tarball is not None and tarball[1]:
        made.add("vendor.tar.xz")
    own_tarball = re.compile(re.escape(expand("%{name}-%{version}", spec.macros)) + r"\.tar\.\w+")
    used: set[str] = set()
    for package in spec.packages:
        for tag, value, line in package.tags:
            if not re.fullmatch(r"(source|patch)\d*", tag.lower()):
                continue
            source = expand(value, spec.macros)
            if "://" in source:
                continue
            base = PurePosixPath(source).name
            used.add(base)
            if "%" in base:
                problems.append(
                    Problem(
                        path,
                        f"line {line}: {tag} {source!r} has a macro the checker cannot expand",
                        warning=True,
                        rule="source",
                    )
                )
            elif f"{folder}/{base}" in context.tracked or base in made:
                continue
            elif own_tarball.fullmatch(base):
                if tarball is None and base not in downloads:
                    problems.append(
                        Problem(
                            path,
                            f"line {line}: {tag} {base}: neither obs/tools/obs_sync.py (TARBALLS) "
                            "nor _service makes this tarball",
                            warning=True,
                            rule="source",
                        )
                    )
            elif base in downloads:
                if base not in verified:
                    problems.append(
                        Problem(
                            f"{folder}/_service",
                            f"{base} is downloaded but no verify_file checks its checksum",
                            rule="source",
                        )
                    )
            else:
                problems.append(
                    Problem(
                        path,
                        f"line {line}: {tag} {base} is not in {folder}, "
                        "not downloaded by _service and not made by obs_sync.py",
                        rule="source",
                    )
                )
    for file in sorted(context.tracked):
        parent, _, base = file.rpartition("/")
        if parent != folder or base in used:
            continue
        if base in (f"{name}.spec", f"{name}.changes", *NOT_SOURCES) or base.endswith("rpmlintrc"):
            continue
        problems.append(
            Problem(
                file,
                "the file is in the package folder but no Source or Patch uses it "
                "(it does not get into the package)",
                warning=True,
                rule="unused-file",
            )
        )
    return problems


def own_licence(folder: str, context: Context) -> str:
    """The licence of the package's code folder (LICENSING.md, tools/check_spdx.py)."""
    code = code_folder(folder, context).rstrip("/") + "/"
    return "Apache-2.0" if code.startswith(APACHE_DIRS) else DEFAULT_LICENCE


def check_header(path: str, text: str, licence: str) -> list[Problem]:
    want = SPDX_HEADER + licence
    if want in text.splitlines()[:HEADER_LINES]:
        return []
    return [Problem(path, f"no '{want}' in the first {HEADER_LINES} lines", rule="spdx-header")]


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


def check_folder(root: Path, folder: str, context: Context) -> tuple[list[Problem], list[str]]:
    """Check one package folder; also return the RPM names its spec builds."""
    name = PurePosixPath(folder).name
    specs = sorted(f for f in context.tracked if f.startswith(f"{folder}/") and f.endswith(".spec"))
    path = f"{folder}/{name}.spec"
    problems = [
        Problem(other, f"a second spec in the folder (OBS expects only {name}.spec)", rule="layout")
        for other in specs
        if other != path
    ]
    text, found = read_text(root, path)
    problems += found
    if text is None:
        return problems, []
    changes: str | None = None
    if f"{folder}/{name}.changes" in context.tracked:
        changes, found = read_text(root, f"{folder}/{name}.changes")
        problems += found
    else:
        problems.append(
            Problem(
                f"{folder}/{name}.changes",
                "missing: OBS keeps the changelog of the package there",
                warning=True,
                rule="layout",
            )
        )
    if changes is not None:
        problems += check_changes(f"{folder}/{name}.changes", changes)
    service = None
    if f"{folder}/_service" in context.tracked:
        service, found = read_text(root, f"{folder}/_service")
        problems += found
    spec = parse_spec(text)
    if spec.main.name != name:
        problems.append(
            Problem(path, f"Name is {spec.main.name!r}, not the folder name {name!r}", rule="name")
        )
    problems += check_header(path, text, own_licence(folder, context))
    problems += check_preambles(path, spec)
    problems += check_version(path, spec)
    problems += check_sections(path, spec)
    problems += check_changelog(path, spec)
    problems += check_licences(path, folder, spec, context)
    problems += check_sources(path, folder, spec, context, service)
    return problems, built_packages(spec)


def obs_sync_tarballs(text: str) -> dict[str, tuple[str, bool]]:
    """``TARBALLS`` of obs_sync.py, read as text: package -> (code folder, cargo vendor)."""
    starts = list(re.finditer(r'^\s+"([\w.+-]+)": Tarball\(\s*"([^"]+)"', text, re.M))
    tarballs: dict[str, tuple[str, bool]] = {}
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        tarballs[match.group(1)] = (match.group(2), "cargo=" in text[match.end() : end])
    return tarballs


def cc_places(text: str) -> frozenset[str]:
    """The `paths` and `names` in the CC-BY-SA-4.0 part of LICENSING.md."""
    places: set[str] = set()
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = CC_LICENCE in line
        elif inside:
            for code in re.findall(r"`([^`]+)`", line):
                places.add(code.split("*")[0])
    return frozenset(places)


def load_context(root: Path, tracked: Iterable[str]) -> Context:
    def text(rel: str) -> str:
        try:
            return (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # read_text reports it for the file itself
            return ""

    return Context(
        frozenset(tracked), obs_sync_tarballs(text(OBS_SYNC)), cc_places(text(LICENSING))
    )


def legacy(problem: Problem) -> Problem:
    """Turn a known old violation (see LEGACY) into a warning."""
    if (problem.path, problem.rule) in LEGACY:
        return Problem(problem.path, problem.message, warning=True, rule=problem.rule)
    return problem


def check_folders(root: Path, folders: Sequence[str], tracked: Iterable[str]) -> list[Problem]:
    """Check the package folders (repo-relative) and the RPM names across them."""
    context = load_context(root, tracked)
    problems: list[Problem] = []
    builders: dict[str, list[str]] = {}
    for folder in folders:
        found, names = check_folder(root, folder, context)
        problems += found
        for name in names:
            builders.setdefault(name, []).append(f"{folder}/{PurePosixPath(folder).name}.spec")
    for name, specs in sorted(builders.items()):
        if len(specs) > 1:
            problems += [
                Problem(
                    spec,
                    f"{name} is also built by {', '.join(s for s in specs if s != spec)}",
                    rule="duplicate-package",
                )
                for spec in specs
            ]
    return [legacy(p) for p in problems]


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", f"{PACKAGES}/"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        errors="surrogateescape",
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def package_folders(tracked: Iterable[str]) -> list[str]:
    folders = set()
    for file in tracked:
        parts = PurePosixPath(file).parts
        if len(parts) > 3 and "/".join(parts[:2]) == PACKAGES:
            folders.add("/".join(parts[:3]))
    return sorted(folders)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if any(a.startswith("-") for a in args):
        print("usage: python3 tools/check_spec.py [obs/packages/NAME ...]", file=sys.stderr)
        return 0 if args in (["-h"], ["--help"]) else 2
    try:
        tracked = tracked_files(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    folders = [a.rstrip("/") for a in args] or package_folders(tracked)
    unknown = [f for f in folders if f not in package_folders(tracked)]
    if unknown:
        print(f"not a package folder with tracked files: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if not folders:
        print(f"no package folders found in {PACKAGES}", file=sys.stderr)
        return 2
    problems = check_folders(ROOT, folders, tracked)
    for problem in problems:
        print(problem, file=sys.stderr)
    errors = sum(not p.warning for p in problems)
    print(f"{len(folders)} spec files checked: {errors} errors, {len(problems) - errors} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    # A file name that is not UTF-8 is printed escaped, not as a traceback.
    sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    sys.exit(main())
