#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Report the driver and firmware packages of the KIWI images (``obs/images/``).

For every tracked ``obs/images/<name>/<name>.kiwi`` the tool builds the set of
packages the image asks for:

* every ``<package name>`` of a ``<packages>`` section of any type but
  ``delete`` and ``uninstall`` (``image``, ``bootstrap``, a build type such
  as ``iso``, or none);
* for every ``<namedCollection>``/``<collection>``, the ``Requires:`` and
  ``Recommends:`` of the package in
  ``obs/packages/patterns-vilna/patterns-vilna.spec`` that has
  ``Provides: pattern() = <name>``, and so on through sub-patterns
  (``Requires: pattern() = <name>`` or the name of another pattern package);
* minus the packages of a ``<packages type="delete">``/``"uninstall"`` section
  and the ``<ignore name>`` entries.

Only what the files name is counted, not what rpm pulls in through the
dependencies of those packages (that needs the repositories). Files
(``/usr/bin/gzip``), ``name(...)`` capabilities and rich dependencies
(``(a if b)``) are not package names and are left out. ``%if`` blocks of the
spec are all read.

The packages are sorted into classes by name rules (``CLASS_RULES``): firmware,
CPU microcode, graphics, audio, Bluetooth, network, input, sensors, printing,
scanning, fingerprint, Thunderbolt, firmware updates and kernel. Packages no
rule matches are "other" and never an error.

Without options it prints a table "class | Vilna-Live | Vilna |
tik-osimage-Vilna" with the number of packages of each class, then every
driver or firmware package with the images that have it.

``--check`` compares instead: a person who installs from the live image needs
the same hardware support in the live session as after the install, so every
driver or firmware package of an installed image (``--installed``, by default
Vilna and tik-osimage-Vilna) must be in the live image (``--live``, by default
Vilna-Live). Each one that is not is an error, unless it is named with
``--allow-missing`` (a deliberate gap, printed as a note).

Only ``git ls-files`` is run (to find the files). Nothing else is executed,
nothing is read from the network and nothing is written.

Run: python3 tools/report_drivers.py [--check] [--live NAME] [--installed NAME]...
     [--allow-missing PACKAGE]...
Exit code: 0 all good, 1 problems found (or a --check gap), 2 wrong call.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

IMAGES = "obs/images"
PATTERNS_SPEC = "obs/packages/patterns-vilna/patterns-vilna.spec"
LIVE = "Vilna-Live"
INSTALLED = ("Vilna", "tik-osimage-Vilna")
COLUMN_ORDER = (LIVE, *INSTALLED)
OTHER = "other"

# First match wins, so the specific rules come before the general ones
# (sof-firmware is audio, bluez-firmware is Bluetooth, the rest of *-firmware
# is firmware).
CLASS_RULES: tuple[tuple[str, str], ...] = (
    ("firmware", r"kernel-firmware(-.+)?"),
    ("microcode", r"ucode-.+"),
    ("audio", r"sof-.+|alsa(-.+)?|pipewire(-.+)?|wireplumber(-.+)?"),
    ("bluetooth", r"bluez(-.+)?"),
    ("firmware", r".+-firmware(-.+)?"),
    (
        "graphics",
        r"Mesa(-.+)?|libvulkan.*|vulkan-.+|xf86-video-.+|nvidia-.+|libva-.+"
        r"|intel-media-driver|switcheroo-control",
    ),
    ("input", r"xf86-input-.+|libinput.*|libwacom.*"),
    ("sensors", r"iio-sensor-proxy"),
    ("scanning", r"sane-.+|hplip-sane"),
    (
        "printing",
        r"cups(-.+)?|printer-driver-.+|epson-.+|hplip(-.+)?|OpenPrintingPPDs(-.+)?"
        r"|gutenprint.*|ipp-usb|splix|udev-configure-printer|system-config-printer.*",
    ),
    ("network", r"NetworkManager(-.+)?|ModemManager(-.+)?|libmbim.*|libqmi.*|usb_modeswitch.*"),
    ("fingerprint", r"fprintd.*|libfprint.*"),
    ("thunderbolt", r"bolt(-.+)?"),
    ("firmware-update", r"fwupd.*"),
    ("kernel", r"kernel-(default|longterm)(-.+)?|.+-kmp-.+"),
)
COMPILED_RULES = tuple((name, re.compile(rule)) for name, rule in CLASS_RULES)
CLASSES = tuple(dict.fromkeys(name for name, _ in CLASS_RULES))
# Branding, translations and development files are not drivers whatever
# their prefix (NetworkManager-branding-Vilna, cups-devel).
NOT_DRIVERS = re.compile(r".+-(branding-.+|lang|devel|doc)")

REMOVING = frozenset({"delete", "uninstall"})
SECTIONS = frozenset(
    {
        "%package",
        "%description",
        "%prep",
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
    }
)
DEP_TAG = re.compile(r"(Requires|Recommends|Provides)(\([^)]*\))?:\s*(.*)", re.IGNORECASE)
NAME_TAG = re.compile(r"Name:\s*(\S+)", re.IGNORECASE)
DEFINE = re.compile(r"%(?:define|global)\s+(\w+)\s+(.*\S)")
MACRO = re.compile(r"%\{\??(\w+)\}")
PATTERN_DEP = re.compile(r"pattern\(\)\s*=\s*(\S+)")
COMPARATORS = frozenset({"<", ">", "=", "<=", ">=", "=="})


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    note: bool = False

    def __str__(self) -> str:
        return f"{self.path}: {'note: ' if self.note else ''}{self.message}"


@dataclass
class SpecPackage:
    """One binary package of the patterns spec: what it provides and pulls in."""

    name: str
    patterns: list[str] = field(default_factory=list)  # Provides: pattern() = <name>
    packages: list[str] = field(default_factory=list)  # Requires and Recommends
    subpatterns: list[str] = field(default_factory=list)  # Requires: pattern() = <name>


@dataclass
class Kiwi:
    """What the report reads from a .kiwi file."""

    path: str
    name: str
    packages: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def classify(name: str) -> str:
    """The class of a package name, or ``OTHER``."""
    if NOT_DRIVERS.fullmatch(name):
        return OTHER
    for klass, rule in COMPILED_RULES:
        if rule.fullmatch(name):
            return klass
    return OTHER


def expand(value: str, macros: dict[str, str]) -> str:
    for _ in range(5):
        new = MACRO.sub(lambda m: macros.get(m.group(1), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


def dependency_names(value: str) -> list[str]:
    """The package names of a Requires/Recommends value, without versions.

    ``a >= 1, b`` gives a and b. Files, ``name(...)`` capabilities, macros
    that stayed unexpanded and rich dependencies (``(a if b)``) give nothing.
    """
    if value.startswith("("):
        return []
    names: list[str] = []
    skip_version = False
    for word in value.replace(",", " ").split():
        if word in COMPARATORS:
            skip_version = True
        elif skip_version:
            skip_version = False
        elif not word.startswith("/") and "(" not in word and "%" not in word:
            names.append(word)
    return names


def subpackage_name(words: list[str], main: str, macros: dict[str, str]) -> str:
    """The package of a ``%package`` line: ``-n full`` or ``<Name>-<suffix>``."""
    args = [w for w in words[1:] if w != "-n"]
    if not args:
        return main
    name = expand(args[0], macros)
    return name if "-n" in words[1:] else f"{main}-{name}"


def add_dependency(package: SpecPackage, tag: str, value: str) -> None:
    pattern = PATTERN_DEP.search(value)
    if tag == "provides":
        if pattern:
            package.patterns.append(pattern.group(1))
    elif pattern:
        package.subpatterns.append(pattern.group(1))
    else:
        package.packages.extend(dependency_names(value))


def parse_spec(text: str) -> dict[str, SpecPackage]:
    """The binary packages of a spec by name, with their dependencies."""
    macros: dict[str, str] = {}
    main = SpecPackage("")
    packages: dict[str, SpecPackage] = {}
    current: SpecPackage | None = main
    for raw in text.splitlines():
        line = raw.strip()
        words = line.split()
        if words and words[0] in SECTIONS:
            current = None
            if words[0] == "%package":
                current = SpecPackage(subpackage_name(words, main.name, macros))
                packages[current.name] = current
            continue
        if define := DEFINE.match(line):
            macros[define.group(1)] = define.group(2)
        elif current is main and (name := NAME_TAG.fullmatch(line)):
            main.name = macros.setdefault("name", expand(name.group(1), macros))
        elif current is not None and (tag := DEP_TAG.fullmatch(line)):
            add_dependency(current, tag.group(1).lower(), expand(tag.group(3), macros))
    packages[main.name] = main
    return packages


def sections(root: ET.Element) -> Iterable[tuple[str, ET.Element]]:
    for section in root.iter("packages"):
        yield section.get("type", ""), section


def parse_kiwi(path: str, text: str) -> tuple[Kiwi | None, list[Problem]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as err:
        return None, [Problem(path, f"not well-formed XML: {err}")]
    if root.tag != "image":
        return None, [Problem(path, f"root element is <{root.tag}>, not <image>")]
    kiwi = Kiwi(path, PurePosixPath(path).parent.name)
    for kind, section in sections(root):
        for item in section:
            name = item.get("name", "").strip()
            if not isinstance(item.tag, str) or not name:
                continue
            if item.tag == "ignore" or kind in REMOVING:
                kiwi.removed.append(name)
            elif item.tag == "package":
                kiwi.packages.append(name)
            elif item.tag in ("namedCollection", "collection"):
                kiwi.collections.append(name)
    return kiwi, []


def pattern_index(specs: dict[str, SpecPackage]) -> dict[str, SpecPackage]:
    return {pattern: pkg for pkg in specs.values() for pattern in pkg.patterns}


def expand_patterns(kiwi: Kiwi, specs: dict[str, SpecPackage]) -> tuple[set[str], list[Problem]]:
    """The packages of an image's collections and pattern packages, through sub-patterns."""
    by_pattern = pattern_index(specs)
    problems: list[Problem] = []
    found: set[str] = set()
    todo = [("pattern", c) for c in kiwi.collections] + [("package", p) for p in kiwi.packages]
    seen: set[tuple[str, str]] = set()
    while todo:
        kind, name = todo.pop()
        if (kind, name) in seen:
            continue
        seen.add((kind, name))
        pkg = by_pattern.get(name) if kind == "pattern" else specs.get(name)
        if pkg is None:
            if kind == "pattern":
                message = f"collection {name} is not a pattern of {PATTERNS_SPEC}; not counted"
                problems.append(Problem(kiwi.path, message, True))
            continue
        found.add(pkg.name)
        todo += [("pattern", p) for p in pkg.subpatterns]
        todo += [("package", p) for p in pkg.packages]
        found.update(pkg.packages)
    return found, problems


def image_packages(kiwi: Kiwi, specs: dict[str, SpecPackage]) -> tuple[set[str], list[Problem]]:
    from_patterns, problems = expand_patterns(kiwi, specs)
    packages = (set(kiwi.packages) | from_patterns) - set(kiwi.removed)
    return packages, problems


def by_class(packages: Iterable[str]) -> dict[str, set[str]]:
    classes: dict[str, set[str]] = {}
    for name in packages:
        classes.setdefault(classify(name), set()).add(name)
    return classes


def column_order(names: Iterable[str]) -> list[str]:
    known = [n for n in COLUMN_ORDER if n in names]
    return known + sorted(set(names) - set(known))


def table(images: dict[str, set[str]]) -> list[str]:
    """The class table and the list of driver packages with their images."""
    columns = column_order(images)
    classes = {name: by_class(pkgs) for name, pkgs in images.items()}
    lines = [f"| class | {' | '.join(columns)} |", "|" + " --- |" * (len(columns) + 1)]
    for klass in (*CLASSES, OTHER):
        counts = [str(len(classes[c].get(klass, ()))) for c in columns]
        lines.append(f"| {klass} | {' | '.join(counts)} |")
    lines += ["", f"| class | package | {' | '.join(columns)} |"]
    lines.append("|" + " --- |" * (len(columns) + 2))
    drivers = {p for pkgs in images.values() for p in pkgs if classify(p) != OTHER}
    for name in sorted(drivers, key=lambda p: (CLASSES.index(classify(p)), p)):
        marks = ["x" if name in images[c] else "-" for c in columns]
        lines.append(f"| {classify(name)} | {name} | {' | '.join(marks)} |")
    return lines


def check_live(
    images: dict[str, set[str]],
    paths: dict[str, str],
    live: str,
    installed: Sequence[str],
    allowed: Iterable[str] = (),
) -> list[Problem]:
    """Every driver or firmware package of an installed image must be in the live one."""
    problems: list[Problem] = []
    allowed = set(allowed)
    for target in installed:
        for name in sorted(images[target] - images[live]):
            klass = classify(name)
            if klass == OTHER:
                continue
            message = f"{name} ({klass}) is in {target} but not in {live}"
            if name in allowed:
                problems.append(Problem(paths[live], f"{message} (allowed)", True))
            else:
                problems.append(Problem(paths[live], message))
    return problems


def kiwi_paths(files: Iterable[str]) -> list[str]:
    """The ``obs/images/<name>/<name>.kiwi`` files of a file list."""
    found = []
    for path in files:
        parts = PurePosixPath(path).parts
        if len(parts) == 4 and "/".join(parts[:2]) == IMAGES and parts[3].endswith(".kiwi"):
            found.append(path)
    return sorted(found)


def read_specs(root: Path, files: Sequence[str]) -> tuple[dict[str, SpecPackage], list[Problem]]:
    if PATTERNS_SPEC not in files:
        return {}, [Problem(PATTERNS_SPEC, "not tracked; collections are not expanded", True)]
    text = (root / PATTERNS_SPEC).read_text(encoding="utf-8", errors="replace")
    return parse_spec(text), []


def read_images(
    root: Path, files: Sequence[str]
) -> tuple[dict[str, set[str]], dict[str, str], list[Problem]]:
    """Every image's package set and path, and the problems found on the way."""
    specs, problems = read_specs(root, files)
    images: dict[str, set[str]] = {}
    paths: dict[str, str] = {}
    for path in kiwi_paths(files):
        text = (root / path).read_text(encoding="utf-8", errors="replace")
        kiwi, found = parse_kiwi(path, text)
        problems += found
        if kiwi is None:
            continue
        packages, found = image_packages(kiwi, specs)
        problems += found
        images[kiwi.name] = packages
        paths[kiwi.name] = path
    return images, paths, problems


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="report_drivers.py",
        description="Driver and firmware packages of the KIWI images in obs/images.",
    )
    parser.add_argument("--check", action="store_true", help="fail if the live image lacks one")
    parser.add_argument("--live", default=LIVE, help=f"the live image (default {LIVE})")
    parser.add_argument(
        "--installed",
        action="append",
        help=f"an installed image, repeatable (default {' and '.join(INSTALLED)})",
    )
    parser.add_argument(
        "--allow-missing",
        action="append",
        default=[],
        metavar="PACKAGE",
        help="a package the live image may lack on purpose, repeatable",
    )
    return parser.parse_args(argv)


def run(root: Path, files: Sequence[str], args: argparse.Namespace) -> int:
    images, paths, problems = read_images(root, files)
    if not images:
        print(f"no tracked *.kiwi files in {IMAGES}", file=sys.stderr)
        return 2
    installed = args.installed or list(INSTALLED)
    if args.check:
        unknown = [n for n in (args.live, *installed) if n not in images]
        if unknown:
            print(f"no such image in {IMAGES}: {', '.join(unknown)}", file=sys.stderr)
            return 2
        problems += check_live(images, paths, args.live, installed, args.allow_missing)
    else:
        for line in table(images):
            print(line)
    for problem in problems:
        print(problem, file=sys.stderr)
    errors = sum(not p.note for p in problems)
    if args.check:
        print(
            f"{args.live} against {', '.join(installed)}: {errors} driver or firmware "
            f"packages missing, {len(problems) - errors} notes"
        )
    return 1 if errors else 0


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", f"{IMAGES}/", PATTERNS_SPEC],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exit_:
        return 0 if exit_.code == 0 else 2
    try:
        files = tracked_files(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    return run(ROOT, files, args)


if __name__ == "__main__":
    sys.exit(main())
