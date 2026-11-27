#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Match the third-party packages of the Vilna OS SBOM against a local OSV file, offline.

    python3 tools/vuln_match.py --osv osv-dump.json
    python3 tools/vuln_match.py --osv pypi.json --osv crates.json --json
    python3 tools/vuln_match.py --osv osv-dump.json --sbom vilna.spdx.json
    python3 tools/vuln_match.py --osv osv-dump.json --report-only      # exit 0 on findings

This is the groundwork for the vulnerability handling the Cyber Resilience Act asks for.
Nothing is read from the network: somebody downloads the OSV records separately
(https://osv.dev, the ``PyPI`` and ``crates.io`` dumps) and gives the file to the tool.

Inputs:

* the SBOM: by default built from the tracked files with ``tools/sbom.py`` (``--root``);
  or an SPDX 2.3 JSON file of that tool (``--sbom``). Only packages with a ``pkg:pypi/..``
  or ``pkg:cargo/..`` purl are matched; our own components and the Flatpak bundles have
  no purl of that kind and are not looked up;
* the OSV file (``--osv``, can be given several times): a JSON array of OSV records
  (a single record object is accepted too). A record counts when it has an ``id`` and
  ``affected`` entries whose ``package.ecosystem`` is ``PyPI`` or ``crates.io``;
  other ecosystems are ignored. Records with ``withdrawn`` are skipped.

How a version is matched (the evaluation algorithm of the OSV schema):

* ``affected[].versions``: the version is listed (compared as versions, so ``1.0``
  equals ``1.0.0``);
* ``affected[].ranges`` of type ``ECOSYSTEM`` or ``SEMVER``: the events (``introduced``,
  ``fixed``, ``last_affected``) are sorted by version and walked; ``introduced: "0"``
  is the lowest version. ``GIT`` ranges are skipped (there are no commits here);
* PyPI versions are compared by PEP 440 (epoch, pre/post/dev releases, local
  versions); crates.io versions by SemVer 2.0.0 (build metadata does not count).

Output: a table ``package | version | identifier | fixed in`` (the identifier is the OSV
id, then its aliases such as the CVE number; ``fixed in`` is the lowest ``fixed`` event
above the version in a range that matches, or ``-`` when no fix is known), sorted by
package, version and identifier; ``--json`` prints a list of objects with the ecosystem
and the summary as well. Texts that come from the files are shown without control
characters.

A version that cannot be parsed (in the SBOM or in an event) is a warning, and what
cannot be compared is not matched. A record that is broken (no ``id``, ``affected`` not
a list, ...) is an error and is skipped, the other records are still used.

Only ``git ls-files`` is run (by tools/sbom.py), nothing is written.

Exit code: 0 no vulnerability found (or ``--report-only``), 1 a vulnerability or a broken
record found, 2 wrong call or an input that cannot be used (a file that cannot be read
or parsed, an SBOM that cannot be built).
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import sbom  # noqa: E402  (after sys.path.insert above)

MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_VERSION_CHARS = 128
ECOSYSTEMS = {"pypi": "pypi", "crates.io": "cargo"}
EVENT_ORDER = {"introduced": 0, "fixed": 1, "last_affected": 2}
HEADER = ("package", "version", "identifier", "fixed in")
MAX_ALIASES = 5

# A version key: tuples that compare the way the ecosystem orders its versions.
Key = tuple


# ---------------------------------------------------------------- versions

PEP440 = re.compile(
    r"""
    v?
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?P<pre>[-_.]?(?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)[-_.]?(?P<pre_n>[0-9]+)?)?
    (?P<post>-(?P<post_n1>[0-9]+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?)?
    (?P<dev>[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    """,
    re.VERBOSE | re.IGNORECASE,
)
PRE_RANK = {
    "a": 0,
    "alpha": 0,
    "b": 1,
    "beta": 1,
    "c": 2,
    "rc": 2,
    "pre": 2,
    "preview": 2,
}

SEMVER_ID = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    rf"(?:-({SEMVER_ID}(?:\.{SEMVER_ID})*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def pep440_key(text: str) -> Key | None:
    """The sort key of a PEP 440 version, or None when it is not one."""
    if len(text) > MAX_VERSION_CHARS:
        return None
    m = PEP440.fullmatch(text.strip())
    if m is None:
        return None
    release = [int(x) for x in m["release"].split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    has_pre = m["pre"] is not None
    has_post = m["post"] is not None
    has_dev = m["dev"] is not None
    if has_pre:
        pre = (PRE_RANK[m["pre_l"].lower()], int(m["pre_n"] or 0))
    elif has_dev and not has_post:
        pre = (-1, 0)  # 1.0.dev1 comes before 1.0a1
    else:
        pre = (3, 0)  # a final release comes after its pre-releases
    post = (1, int(m["post_n1"] or m["post_n2"] or 0)) if has_post else (0, 0)
    dev = (0, int(m["dev_n"] or 0)) if has_dev else (1, 0)
    local: tuple = ()
    if m["local"]:
        parts = re.split(r"[-_.]", m["local"].lower())
        local = tuple((1, int(p), "") if p.isdigit() else (0, 0, p) for p in parts)
    return (int(m["epoch"] or 0), tuple(release), pre, post, dev, local)


def semver_key(text: str) -> Key | None:
    """The sort key of a SemVer 2.0.0 version, or None when it is not one."""
    if len(text) > MAX_VERSION_CHARS:
        return None
    m = SEMVER.fullmatch(text)
    if m is None:
        return None
    if m.group(4) is None:
        pre: tuple = ((1,),)  # a release comes after its pre-releases
    else:
        ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in m.group(4).split("."))
        pre = ((0,), *ids)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), pre)


@functools.lru_cache(maxsize=65536)
def version_key(ecosystem: str, text: str) -> Key | None:
    return pep440_key(text) if ecosystem == "pypi" else semver_key(text)


# ---------------------------------------------------------------- the SBOM


@dataclass(frozen=True)
class Package:
    ecosystem: str  # "pypi" | "cargo"
    name: str
    version: str


def parse_purl(locator: str) -> Package | None:
    """The package of a ``pkg:pypi/..@..`` or ``pkg:cargo/..@..`` purl; None for others."""
    m = re.fullmatch(r"pkg:(pypi|cargo)/([^@?#]+)@([^?#]+)(?:[?#].*)?", locator)
    if m is None:
        return None
    return Package(m.group(1), unquote(m.group(2)), unquote(m.group(3)))


def packages_of(doc: object) -> list[Package]:
    """The third-party packages (with a pypi or cargo purl) of an SPDX document, unique."""
    found: dict[Package, None] = {}
    packages = doc.get("packages") if isinstance(doc, dict) else None
    for pkg in packages if isinstance(packages, list) else []:
        refs = pkg.get("externalRefs") if isinstance(pkg, dict) else None
        for ref in refs if isinstance(refs, list) else []:
            if not isinstance(ref, dict) or ref.get("referenceType") != "purl":
                continue
            locator = ref.get("referenceLocator")
            parsed = parse_purl(locator) if isinstance(locator, str) else None
            if parsed is not None:
                found[parsed] = None
    return sorted(found, key=lambda p: (p.ecosystem, p.name, p.version))


# ---------------------------------------------------------------- the OSV records


@dataclass
class Affected:
    """One ``affected`` entry of a record, for one package of PyPI or crates.io."""

    ecosystem: str
    name: str
    versions: list[str]
    ranges: list[list[dict]]  # the events of every SEMVER/ECOSYSTEM range


@dataclass
class Record:
    ident: str
    aliases: list[str]
    summary: str
    source: str
    affected: list[Affected] = field(default_factory=list)


@dataclass
class Finding:
    ecosystem: str
    name: str
    version: str
    ident: str
    aliases: list[str]
    fixed: list[str]
    summary: str

    def row(self) -> tuple[str, str, str, str]:
        ident = self.ident
        if self.aliases:
            ident += " (" + ", ".join(self.aliases[:MAX_ALIASES]) + ")"
        return (self.name, self.version, ident, ", ".join(self.fixed) or "-")


def clean(text: str) -> str:
    """Text from a file, without control characters (they could drive the terminal)."""
    return "".join("?" if ch.isspace() and ch != " " or not ch.isprintable() else ch for ch in text)


def string_list(value: object) -> list[str]:
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def parse_affected(
    entry: object, label: str, problems: list[sbom.Problem], source: str
) -> Affected | None:
    """One ``affected`` entry; None when it is for another ecosystem or unusable."""
    if not isinstance(entry, dict):
        problems.append(sbom.Problem(source, f"{label}: an affected entry is not an object"))
        return None
    package = entry.get("package")
    if not isinstance(package, dict):
        return None  # no package: nothing to match (commit-only records)
    ecosystem = ECOSYSTEMS.get(str(package.get("ecosystem", "")).lower())
    name = package.get("name")
    if ecosystem is None:
        return None
    if not isinstance(name, str) or not name:
        problems.append(sbom.Problem(source, f"{label}: an affected package has no name"))
        return None
    versions = entry.get("versions", [])
    ranges = entry.get("ranges", [])
    if not isinstance(versions, list) or not isinstance(ranges, list):
        problems.append(
            sbom.Problem(source, f"{label}: affected versions or ranges are not a list")
        )
        return None
    events: list[list[dict]] = []
    for item in ranges:
        if not isinstance(item, dict) or not isinstance(item.get("events"), list):
            problems.append(sbom.Problem(source, f"{label}: a range has no list of events"))
            continue
        if str(item.get("type", "")).upper() in ("SEMVER", "ECOSYSTEM"):
            events.append(item["events"])
    if not versions and not ranges:
        problems.append(
            sbom.Problem(
                source,
                f"{label}: {name} has neither versions nor ranges, not matched",
                True,
            )
        )
    return Affected(ecosystem, name, string_list(versions), events)


def parse_records(data: object, source: str, problems: list[sbom.Problem]) -> list[Record]:
    """The usable records of one OSV file (an array, or one record object)."""
    items = data if isinstance(data, list) else [data]
    records: list[Record] = []
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict):
            problems.append(sbom.Problem(source, f"record {number}: not an object"))
            continue
        ident = item.get("id")
        if not isinstance(ident, str) or not ident.strip():
            problems.append(sbom.Problem(source, f"record {number}: no id"))
            continue
        label = clean(ident)
        if item.get("withdrawn"):
            continue
        affected = item.get("affected", [])
        if not isinstance(affected, list):
            problems.append(sbom.Problem(source, f"{label}: affected is not a list"))
            continue
        summary = item.get("summary")
        record = Record(
            clean(ident),
            [clean(a) for a in string_list(item.get("aliases"))],
            clean(summary) if isinstance(summary, str) else "",
            source,
        )
        for entry in affected:
            parsed = parse_affected(entry, label, problems, source)
            if parsed is not None:
                record.affected.append(parsed)
        records.append(record)
    return records


def load_osv(path: str, problems: list[sbom.Problem]) -> list[Record] | None:
    """The records of an OSV file, or None (a problem is added) when it cannot be used."""
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            problems.append(sbom.Problem(path, f"larger than {MAX_FILE_BYTES // 2**20} MiB"))
            return None
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, RecursionError) as err:  # JSON/Unicode errors are ValueErrors
        problems.append(sbom.Problem(path, f"cannot read: {err}"))
        return None
    if not isinstance(data, (list, dict)):
        problems.append(sbom.Problem(path, "an OSV file is a JSON array of records"))
        return None
    return parse_records(data, path, problems)


# ---------------------------------------------------------------- matching


def range_result(
    ecosystem: str,
    events: list[dict],
    target: Key,
    where: str,
    problems: list[sbom.Problem],
) -> tuple[bool, str | None]:
    """Is ``target`` inside the range; and the lowest ``fixed`` version above it."""
    walk: list[tuple[tuple, str, str, Key | None]] = []
    for event in events:
        kinds = (
            [k for k in event if k in (*EVENT_ORDER, "limit")] if isinstance(event, dict) else []
        )
        if len(kinds) != 1 or not isinstance(event[kinds[0]], str):
            problems.append(sbom.Problem(where, "an event is not understood", True))
            continue
        kind, text = kinds[0], event[kinds[0]]
        if kind == "limit":
            continue
        if kind == "introduced" and text == "0":
            walk.append(((0, (), 0), kind, text, None))
            continue
        key = version_key(ecosystem, text)
        if key is None:
            shown = clean(text)[:60]
            problems.append(
                sbom.Problem(where, f"{kind} {shown!r} is not a version, event skipped", True)
            )
            continue
        walk.append(((1, key, EVENT_ORDER[kind]), kind, text, key))
    walk.sort(key=lambda w: w[0])
    affected = False
    for _, kind, _, key in walk:
        if kind == "introduced":
            affected = affected or key is None or target >= key
        elif key is not None and (target >= key if kind == "fixed" else target > key):
            affected = False
    if not affected:
        return False, None
    above = [(key, text) for _, kind, text, key in walk if kind == "fixed" and key > target]
    return True, min(above, key=lambda x: x[0])[1] if above else None


def entry_matches(
    entry: Affected,
    pkg: Package,
    target: Key | None,
    where: str,
    problems: list[sbom.Problem],
) -> tuple[bool, list[str]]:
    """Does ``pkg`` fall under ``entry``; and the fixed versions of the ranges that match."""
    hit = False
    fixed: list[str] = []
    for text in entry.versions:
        key = version_key(pkg.ecosystem, text)
        if text == pkg.version or (key is not None and key == target):
            hit = True
    if target is not None:
        for events in entry.ranges:
            inside, fix = range_result(pkg.ecosystem, events, target, where, problems)
            if inside:
                hit = True
                if fix is not None:
                    fixed.append(clean(fix))
    return hit, fixed


def index_records(
    records: Sequence[Record],
) -> dict[tuple[str, str], list[tuple[Record, Affected]]]:
    index: dict[tuple[str, str], list[tuple[Record, Affected]]] = {}
    for record in records:
        for entry in record.affected:
            name = sbom.normalize_pypi(entry.name) if entry.ecosystem == "pypi" else entry.name
            index.setdefault((entry.ecosystem, name), []).append((record, entry))
    return index


def match(
    packages: Sequence[Package], records: Sequence[Record], problems: list[sbom.Problem]
) -> list[Finding]:
    """The findings: one per package version and record id, sorted."""
    index = index_records(records)
    found: dict[tuple[str, str, str, str], Finding] = {}
    for pkg in packages:
        name = sbom.normalize_pypi(pkg.name) if pkg.ecosystem == "pypi" else pkg.name
        candidates = index.get((pkg.ecosystem, name), [])
        if not candidates:
            continue
        target = version_key(pkg.ecosystem, pkg.version)
        if target is None:
            shown = clean(pkg.version)[:60]
            problems.append(
                sbom.Problem(
                    f"{pkg.name}",
                    f"version {shown!r} is not a {pkg.ecosystem} version; "
                    "only a listed version can match",
                    True,
                )
            )
        for record, entry in candidates:
            hit, fixed = entry_matches(entry, pkg, target, record.ident, problems)
            if not hit:
                continue
            key = (pkg.ecosystem, pkg.name, pkg.version, record.ident)
            finding = found.get(key)
            if finding is None:
                finding = Finding(
                    pkg.ecosystem,
                    clean(pkg.name),
                    clean(pkg.version),
                    record.ident,
                    record.aliases,
                    [],
                    record.summary,
                )
                found[key] = finding
            for fix in fixed:
                if fix not in finding.fixed:
                    finding.fixed.append(fix)
    return [found[k] for k in sorted(found, key=lambda k: (k[1], k[2], k[3], k[0]))]


# ---------------------------------------------------------------- output


def render_table(findings: Sequence[Finding]) -> str:
    rows = [HEADER] + [f.row() for f in findings]
    widths = [max(len(row[i]) for row in rows) for i in range(len(HEADER))]
    lines = [
        " | ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]
    lines.insert(1, "-+-".join("-" * w for w in widths))
    return "\n".join(lines) + "\n"


def render_json(findings: Sequence[Finding]) -> str:
    return (
        json.dumps(
            [
                {
                    "ecosystem": f.ecosystem,
                    "package": f.name,
                    "version": f.version,
                    "id": f.ident,
                    "aliases": f.aliases,
                    "fixed_in": f.fixed,
                    "summary": f.summary,
                }
                for f in findings
            ],
            indent=2,
            ensure_ascii=True,
        )
        + "\n"
    )


# ---------------------------------------------------------------- command line


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vuln_match.py",
        description="Match the SBOM packages of Vilna OS against a local OSV file (offline).",
    )
    p.add_argument(
        "--osv",
        metavar="FILE",
        action="append",
        default=[],
        help="OSV file: a JSON array of records (can be repeated; required)",
    )
    source = p.add_mutually_exclusive_group()
    source.add_argument("--sbom", metavar="FILE", help="read this SPDX JSON, do not build one")
    source.add_argument(
        "--root",
        metavar="DIR",
        default=str(ROOT),
        help="the repository to build the SBOM from (default: this one)",
    )
    p.add_argument("--json", action="store_true", help="print JSON, not a table")
    p.add_argument(
        "--report-only",
        action="store_true",
        help="exit 0 even when a vulnerability is found",
    )
    return p


def load_sbom(args: argparse.Namespace, problems: list[sbom.Problem]) -> object | None:
    """The SPDX document: from ``--sbom``, or built from the tracked files of ``--root``."""
    if args.sbom:
        try:
            if os.path.getsize(args.sbom) > MAX_FILE_BYTES:
                problems.append(sbom.Problem(args.sbom, "too large"))
                return None
            with open(args.sbom, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError, RecursionError) as err:
            problems.append(sbom.Problem(args.sbom, f"cannot read: {err}"))
            return None
    root = Path(args.root)
    try:
        files = sbom.tracked_files(root)
    except (OSError, subprocess.CalledProcessError) as err:
        problems.append(sbom.Problem(str(root), f"cannot list files with git ls-files: {err}"))
        return None
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc, built = sbom.build(root, files, sbom.DEFAULT_VERSION, created)
    errors = [p for p in built if not p.warning]
    problems.extend(errors)
    return None if errors else doc


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
    except SystemExit as stop:
        return 0 if stop.code == 0 else 2
    if not args.osv:
        print(
            "vuln_match.py: --osv FILE is required (an OSV file downloaded separately)",
            file=sys.stderr,
        )
        return 2
    input_problems: list[sbom.Problem] = []
    records: list[Record] = []
    usable = True
    for path in args.osv:
        loaded = load_osv(path, input_problems)
        if loaded is None:
            usable = False
        else:
            records += loaded
    doc = load_sbom(args, input_problems)
    if doc is None or not usable:
        for problem in input_problems:
            print(problem, file=sys.stderr)
        return 2
    packages = packages_of(doc)
    problems = list(input_problems)
    findings = match(packages, records, problems)
    for problem in problems:
        print(problem, file=sys.stderr)
    if args.json:
        sys.stdout.write(render_json(findings))
    elif findings:
        sys.stdout.write(render_table(findings))
    errors = sum(not p.warning for p in problems)
    print(
        f"{len(packages)} packages checked against {len(records)} records: "
        f"{len(findings)} vulnerabilities, {errors} errors, {len(problems) - errors} warnings",
        file=sys.stderr,
    )
    if (findings or errors) and not args.report_only:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
