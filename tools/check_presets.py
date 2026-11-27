#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the tracked systemd preset files (``*.preset``, systemd.preset(5)).

Presets decide which units ``systemctl preset-all`` switches on at the first boot, so a
wrong line means a missing network or a service that opens a port. For every tracked
``*.preset`` file the checker verifies:

* syntax: a line is a comment (``#`` or ``;``), an empty line, ``enable UNIT [INSTANCE ...]``
  or ``disable UNIT``; no byte order mark, no carriage return, no control characters, no
  trailing comment (systemd reads ``# text`` after the unit as instance names); a unit
  name has a known suffix (``.service``, ``.socket``, ``.timer``, ``.path``, ``.target``,
  ``.mount``, ``.automount``, ``.swap``, ``.slice``); instances are only for templates;
* no ``enable`` with a glob (``enable *`` would switch on every unit of the image);
* every ``enable`` and ``disable`` names a unit the image has: a unit file tracked in the
  repo (a template for an instance), or an external unit from a package of the image
  (the table ``EXTERNAL`` below). A unit that is neither is an error; every external one
  is listed as a warning, with its package and whether the package is named in the
  image lists (``patterns-vilna``, ``obs/images/*/*.kiwi``, ``image/packages.yaml``);
* a unit of the repo that a preset names is mentioned by a spec (the package that ships
  it), and a system preset does not name a unit that the specs install only as a user
  unit (``%{_userunitdir}``) or the other way round;
* ``enable`` of a unit of the repo that has no ``[Install]`` entry (``WantedBy=``,
  ``RequiredBy=``, ``UpheldBy=``, ``Alias=``, ``Also=``, ``DefaultInstance=``) does
  nothing and is an error;
* a template (``name@.service``) that is switched on with ``enable`` and no instance has
  ``DefaultInstance=`` in its ``[Install]``, or the same file documents it: a comment
  that names the template (``disable`` of a template needs nothing);
* the first matching line wins, in the order of the file names (``50-`` before ``87-``
  before ``90-``; system and user presets are two separate lists, a user preset file is
  one that ends in ``-user.preset``). A line that repeats an earlier one, contradicts
  it or is covered by an earlier glob never has an effect and is an error;
* every preset file is a ``Source`` of a spec in its own folder, so that a package ships it;
* ``disable_by_default`` and ``enable_services`` of ``image/packages.yaml`` agree with
  the effective system presets (a unit of the first list ends up disabled, of the second
  enabled).

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but do not
fail the check; they are the external units and the things the repo already does today
(see ``LEGACY``). Only ``git ls-files`` is run. Nothing else is executed, nothing is
read from the network and nothing is written.

``--table`` also prints a Markdown table: every preset line, where its unit comes from
and what the presets finally do with it.

Run: python3 tools/check_presets.py [--table] [PRESET ...]
PRESET is a path from the repo root; without it every tracked preset is checked (the
other files are always read to resolve the units and the order of the lines).
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

UNIT_SUFFIXES = (
    ".service",
    ".socket",
    ".timer",
    ".path",
    ".target",
    ".mount",
    ".automount",
    ".swap",
    ".slice",
)
# The unit files of the repo (the types the repo's own units use).
REPO_UNIT_SUFFIXES = (".service", ".socket", ".timer", ".path")
VERBS = ("enable", "disable")
GLOB_CHARS = "*?["
INSTALL_KEYS = ("WantedBy", "RequiredBy", "UpheldBy", "Alias", "Also", "DefaultInstance")
PATTERNS_SPEC = "obs/packages/patterns-vilna/patterns-vilna.spec"
PACKAGES_YAML = "image/packages.yaml"

# Units the presets name that this repo does not track: they come from packages of openSUSE
# (Tumbleweed, Aeon). unit -> the package that ships it, written down from the package
# names of Tumbleweed; the check cannot look into the repositories (no network). A new
# external unit in a preset is an error until it is added here.
EXTERNAL: dict[str, str] = {
    "NetworkManager-wait-online.service": "NetworkManager",
    "NetworkManager.service": "NetworkManager",
    "acpid.service": "acpid",
    "aeon-check.service": "aeon-check",
    "avahi-daemon.service": "avahi",
    "avahi-daemon.socket": "avahi",
    "chronyd.service": "chrony",
    "create-dirs-from-rpmdb.service": "transactional-update",
    "cups.path": "cups",
    "cups.service": "cups",
    "cups.socket": "cups",
    "firewalld.service": "firewalld",
    "getty@.service": "systemd",
    "health-checker.service": "health-checker",
    "lastlog2-import.service": "lastlog2",
    "nmb.service": "samba",
    "obex.service": "bluez",
    "purge-kernels.service": "purge-kernels-service",
    "restorecond.service": "policycoreutils",
    "smb.service": "samba",
    "sshd.service": "openssh-server",
    "sshd.socket": "openssh-server",
    "supergfxd.service": "supergfxctl",
    "switcheroo-control.service": "switcheroo-control",
    "systemd-resolved.service": "systemd-network",
    "transactional-update-notifier.service": "transactional-update-notifier",
    "transactional-update.timer": "transactional-update",
    "virtlxcd.service": "libvirt-daemon-driver-lxc",
    "virtnetworkd.socket": "libvirt-daemon-driver-network",
    "virtnodedevd.socket": "libvirt-daemon-driver-nodedev",
    "virtnwfilterd.socket": "libvirt-daemon-driver-nwfilter",
    "virtqemud.service": "libvirt-daemon-driver-qemu",
    "virtsecretd.socket": "libvirt-daemon-driver-secret",
    "virtxend.service": "libvirt-daemon-driver-libxl",
    "zramswap.service": "systemd-zram-service",
}

# Problems the repo already has today: (path from the repo root, rule, unit). They are
# warnings, so that the check can be switched on in CI; fix the preset, then delete the
# line.
LEGACY: frozenset[tuple[str, str, str]] = frozenset(
    {
        # The same unit is enabled by 87-default-Vilna.preset (as in Aeon) and again here.
        (
            "obs/packages/vilna-branding/90-default-Vilna.preset",
            "duplicate",
            "switcheroo-control.service",
        ),
    }
)

UNIT_NAME = re.compile(r"[A-Za-z0-9:_.\\@-]+")
INSTANCE_NAME = re.compile(r"[A-Za-z0-9:_.\\-]+")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
SOURCE_LINE = re.compile(r"^\s*Source\d*\s*:\s*(\S+)", re.M)
SPEC_NAME = re.compile(r"^\s*Name\s*:\s*(\S+)", re.M)
SPEC_PACKAGE_N = re.compile(r"^\s*%package\s+-n\s+(\S+)", re.M)
SPEC_REQUIRES = re.compile(r"^\s*Requires\s*:\s*(\S+)", re.M)
KIWI_PACKAGE = re.compile(r"<package\s+name\s*=\s*[\"']([^\"']+)[\"']")
XML_COMMENT = re.compile(r"<!--.*?-->", re.S)
YAML_NAME = re.compile(r"\bname\s*:\s*([^\s,}]+)")


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False
    rule: str = ""

    def __str__(self) -> str:
        kind = "warning: " if self.warning else ""
        return f"{self.path}: {kind}{self.message}"


@dataclass(frozen=True)
class Line:
    path: str
    number: int
    verb: str
    pattern: str
    instances: tuple[str, ...] = ()

    @property
    def is_glob(self) -> bool:
        return any(c in self.pattern for c in GLOB_CHARS)

    @property
    def is_template(self) -> bool:
        return "@." in self.pattern


@dataclass
class Preset:
    path: str
    lines: list[Line]
    comments: list[str]
    user: bool


def is_user_preset(path: str) -> bool:
    return posixpath.basename(path).endswith("-user.preset")


def order_key(path: str) -> tuple[str, str]:
    """The order of systemd: by file name, whatever the folder."""
    return (posixpath.basename(path), path)


# --- reading a preset --------------------------------------------------------


def valid_pattern(pattern: str) -> str:
    """The problem with a unit name or glob, or an empty string."""
    if "/" in pattern:
        return "a unit name has no slash"
    if any(c in pattern for c in GLOB_CHARS):
        if not re.fullmatch(r"[A-Za-z0-9:_.\\@*?\[\]!^-]+", pattern):
            return "invalid characters in the glob"
        return ""
    if not UNIT_NAME.fullmatch(pattern):
        return "invalid characters in the unit name"
    suffix = next((s for s in UNIT_SUFFIXES if pattern.endswith(s)), "")
    if not suffix:
        return "the unit name has no known suffix (" + ", ".join(UNIT_SUFFIXES) + ")"
    stem = pattern[: -len(suffix)]
    if not stem or stem == "@":
        return "the unit name is empty"
    if stem.count("@") > 1:
        return "more than one @ in the unit name"
    if stem.startswith("@"):
        return "the template name is empty"
    return ""


def parse_preset(path: str, raw: bytes) -> tuple[Preset, list[Problem]]:
    """Read one preset file; the problems are the syntax errors."""
    problems: list[Problem] = []
    preset = Preset(path, [], [], is_user_preset(path))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return preset, [Problem(path, "the file is not valid UTF-8", rule="syntax")]
    if text.startswith("\ufeff"):
        problems.append(
            Problem(path, "line 1: the file starts with a byte order mark", rule="syntax")
        )
        text = text[1:]
    for number, raw_line in enumerate(text.split("\n"), start=1):
        if raw_line.endswith("\r"):
            problems.append(
                Problem(path, f"line {number}: the line ends with a carriage return", rule="syntax")
            )
            raw_line = raw_line[:-1]
        line = raw_line.strip()
        if not line:
            continue
        if line[0] in "#;":
            preset.comments.append(line)
            continue
        if CONTROL.search(line):
            problems.append(
                Problem(path, f"line {number}: control character in the line", rule="syntax")
            )
            continue
        words = line.split()
        verb = words[0]
        if verb not in VERBS:
            problems.append(
                Problem(
                    path,
                    f"line {number}: unknown directive '{verb}' (enable or disable)",
                    rule="syntax",
                )
            )
            continue
        if len(words) < 2:
            problems.append(Problem(path, f"line {number}: '{verb}' without a unit", rule="syntax"))
            continue
        pattern, instances = words[1], tuple(words[2:])
        if any(w.startswith(("#", ";")) for w in words[1:]):
            problems.append(
                Problem(
                    path,
                    f"line {number}: a comment after the unit; systemd reads it as instance names",
                    rule="syntax",
                )
            )
            continue
        bad = valid_pattern(pattern)
        if bad:
            problems.append(Problem(path, f"line {number}: '{pattern}': {bad}", rule="syntax"))
            continue
        if verb == "disable" and instances:
            problems.append(
                Problem(path, f"line {number}: 'disable' takes only a unit", rule="syntax")
            )
            continue
        if instances and "@." not in pattern:
            problems.append(
                Problem(
                    path,
                    f"line {number}: instances for '{pattern}', which is not a template",
                    rule="syntax",
                )
            )
            continue
        if any(not INSTANCE_NAME.fullmatch(i) for i in instances):
            problems.append(Problem(path, f"line {number}: invalid instance name", rule="syntax"))
            continue
        preset.lines.append(Line(path, number, verb, pattern, instances))
    return preset, problems


# --- what the repo and the image have ----------------------------------------


@dataclass
class Install:
    keys: frozenset[str]
    default_instance: bool


def parse_install(text: str) -> Install:
    """The keys of the ``[Install]`` group of a unit file."""
    keys: set[str] = set()
    in_install = False
    for raw in text.removeprefix("\ufeff").splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            in_install = line == "[Install]"
            continue
        if in_install and "=" in line:
            key, _, value = line.partition("=")
            if value.strip():
                keys.add(key.strip())
    return Install(frozenset(k for k in keys if k in INSTALL_KEYS), "DefaultInstance" in keys)


def package_names(root: Path, tracked: Sequence[str]) -> set[str]:
    """The packages of the image: the pattern's ``Requires``, the KIWI packages, the list
    of ``image/packages.yaml`` and the packages our specs build."""
    names: set[str] = set()
    for rel in tracked:
        text = read_text(root, rel)
        if text is None:
            continue
        if rel.endswith(".spec"):
            names.update(SPEC_NAME.findall(text))
            names.update(SPEC_PACKAGE_N.findall(text))
            if rel == PATTERNS_SPEC:
                names.update(SPEC_REQUIRES.findall(text))
        elif rel.endswith(".kiwi"):
            names.update(KIWI_PACKAGE.findall(XML_COMMENT.sub("", text)))
        elif rel == PACKAGES_YAML:
            names.update(YAML_NAME.findall(text))
    return names


def spec_lines(root: Path, tracked: Sequence[str]) -> list[str]:
    """The lines of every tracked spec; a line that ends in a backslash is joined to the next."""
    lines: list[str] = []
    for rel in tracked:
        if not rel.endswith(".spec"):
            continue
        pending = ""
        for raw in (read_text(root, rel) or "").splitlines():
            if raw.endswith("\\"):
                pending += raw[:-1] + " "
                continue
            lines.append(pending + raw)
            pending = ""
        if pending:
            lines.append(pending)
    return lines


def unit_scopes(unit: str, lines: Sequence[str]) -> tuple[bool, set[str]]:
    """Whether a spec mentions the unit, and where the specs put it: ``user``, ``system``.

    A line that names the unit and ``_userunitdir`` (or ``_unitdir``) tells the place.
    """
    name = re.compile(r"(?<![A-Za-z0-9@_.-])" + re.escape(unit) + r"(?![A-Za-z0-9_@-]|\.d\b)")
    mentioned = False
    scopes: set[str] = set()
    for line in lines:
        if not name.search(line):
            continue
        mentioned = True
        if "_userunitdir" in line:
            scopes.add("user")
        elif "_unitdir" in line:
            scopes.add("system")
    return mentioned, scopes


def read_text(root: Path, rel: str) -> str | None:
    try:
        return (root / rel).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def yaml_list(text: str, key: str) -> list[str]:
    """The flow list ``key: [a, b]`` of a (very simple) YAML text; comments are cut off."""
    match = re.search(rf"^\s*{re.escape(key)}\s*:\s*\[(.*?)\]", text, re.M)
    if not match:
        return []
    return [i.strip().strip("'\"") for i in match.group(1).split(",") if i.strip()]


@dataclass
class Resolved:
    kind: str  # "repo" or "external"
    where: str  # the path of the unit file or the name of the package
    template: str  # the name of the template the unit is an instance of, or ""


def template_of(unit: str) -> str:
    """``a@b.service`` -> ``a@.service``; a unit that is no instance gives ``""``."""
    suffix = next((s for s in UNIT_SUFFIXES if unit.endswith(s)), "")
    stem = unit[: -len(suffix)] if suffix else unit
    if "@" in stem and not stem.endswith("@"):
        return stem.split("@", 1)[0] + "@" + suffix
    return ""


def resolve(unit: str, repo_units: dict[str, str]) -> Resolved | None:
    template = template_of(unit)
    for name, is_template in ((unit, False), (template, True)):
        if name and name in repo_units:
            return Resolved("repo", repo_units[name], name if is_template else "")
    for name, is_template in ((unit, False), (template, True)):
        if name and name in EXTERNAL:
            return Resolved("external", EXTERNAL[name], name if is_template else "")
    return None


# --- the checks --------------------------------------------------------------


def effective(presets: Sequence[Preset], unit: str) -> Line | None:
    """The line that decides about the unit: the first one that matches, in order."""
    for preset in presets:
        for line in preset.lines:
            if fnmatch.fnmatchcase(unit, line.pattern):
                return line
    return None


def ordered(presets: Sequence[Preset], user: bool) -> list[Preset]:
    return sorted((p for p in presets if p.user == user), key=lambda p: order_key(p.path))


def problem(path: str, message: str, rule: str, unit: str = "", warning: bool = False) -> Problem:
    """A problem; one that the repo already has (``LEGACY``) is a warning."""
    if (path, rule, unit) in LEGACY:
        return Problem(path, message, True, rule)
    return Problem(path, message, warning, rule)


def check_unit_line(
    line: Line,
    preset: Preset,
    repo_units: dict[str, str],
    installs: dict[str, Install],
    packages: set[str],
    specs: Sequence[str] = (),
) -> list[Problem]:
    out: list[Problem] = []
    if line.is_glob:
        if line.verb == "enable":
            out.append(
                problem(
                    line.path,
                    f"line {line.number}: 'enable {line.pattern}' switches on units nobody checked",
                    "enable-glob",
                    line.pattern,
                )
            )
        return out
    found = resolve(line.pattern, repo_units)
    if found is None:
        out.append(
            problem(
                line.path,
                f"line {line.number}: '{line.pattern}' is neither a unit of the repo nor a "
                "known external unit of the image",
                "unknown-unit",
                line.pattern,
            )
        )
        return out
    if found.kind == "external":
        listed = (
            "named in the image lists"
            if found.where in packages
            else ("not named in the image lists, it comes with another package")
        )
        out.append(
            Problem(
                line.path,
                f"line {line.number}: external unit '{line.pattern}' comes from package "
                f"'{found.where}' ({listed})",
                True,
                "external",
            )
        )
    if found.kind == "repo":
        out += check_spec_scope(line, preset, found, specs)
    if line.verb != "enable":
        return out
    default_instance = False
    if found.kind == "repo":
        install = installs.get(found.where)
        if install is not None:
            default_instance = install.default_instance
            if not install.keys:
                out.append(
                    problem(
                        line.path,
                        f"line {line.number}: '{line.pattern}' ({found.where}) has no [Install] "
                        "entry, so 'enable' does nothing",
                        "no-install",
                        line.pattern,
                    )
                )
    if line.is_template and not line.instances and not default_instance:
        documented = any(line.pattern in c for c in preset.comments)
        if not documented:
            out.append(
                problem(
                    line.path,
                    f"line {line.number}: template '{line.pattern}' is enabled with no instance, "
                    "no DefaultInstance= and no comment that explains it",
                    "template-instance",
                    line.pattern,
                )
            )
    return out


def check_spec_scope(
    line: Line, preset: Preset, found: Resolved, specs: Sequence[str]
) -> list[Problem]:
    """A unit of the repo is shipped by a spec, in the place the preset file is for."""
    unit = found.template or line.pattern
    mentioned, scopes = unit_scopes(unit, specs)
    if not mentioned:
        return [
            problem(
                line.path,
                f"line {line.number}: '{line.pattern}' ({found.where}) is in no spec, "
                "so no package ships it",
                "not-in-spec",
                line.pattern,
            )
        ]
    want = "user" if preset.user else "system"
    if scopes and want not in scopes:
        return [
            problem(
                line.path,
                f"line {line.number}: '{line.pattern}' is a {want} preset line, but the specs "
                f"install the unit only as a {'/'.join(sorted(scopes))} unit",
                "wrong-scope",
                line.pattern,
            )
        ]
    return []


def check_order(presets: Sequence[Preset]) -> list[Problem]:
    """Lines that repeat, contradict or are covered by an earlier line."""
    out: list[Problem] = []
    for user in (False, True):
        earlier: list[Line] = []
        for preset in ordered(presets, user):
            for line in preset.lines:
                if not line.instances:
                    out += shadow_problems(line, earlier)
                    earlier.append(line)
    return out


def shadow_problems(line: Line, earlier: Sequence[Line]) -> list[Problem]:
    for first in earlier:
        if first.pattern == line.pattern:
            same = first.verb == line.verb
            rule = "duplicate" if same else "conflict"
            what = "repeats" if same else "contradicts"
            note = "" if same else " (the first one wins)"
            return [
                problem(
                    line.path,
                    f"line {line.number}: '{line.verb} {line.pattern}' {what} "
                    f"{first.path}:{first.number}{note}; it has no effect",
                    rule,
                    line.pattern,
                )
            ]
        if first.is_glob and not line.is_glob and fnmatch.fnmatchcase(line.pattern, first.pattern):
            return [
                problem(
                    line.path,
                    f"line {line.number}: '{line.verb} {line.pattern}' is covered by "
                    f"'{first.verb} {first.pattern}' at {first.path}:{first.number}; "
                    "it has no effect",
                    "shadowed",
                    line.pattern,
                )
            ]
    return []


def check_packaging(presets: Sequence[Preset], tracked: Sequence[str], root: Path) -> list[Problem]:
    """Every preset is a Source of a spec in its own folder."""
    out: list[Problem] = []
    specs = [t for t in tracked if t.endswith(".spec")]
    for preset in presets:
        folder = posixpath.dirname(preset.path)
        name = posixpath.basename(preset.path)
        shipped = False
        for spec in specs:
            if posixpath.dirname(spec) != folder:
                continue
            text = read_text(root, spec) or ""
            if name in SOURCE_LINE.findall(text):
                shipped = True
        if not shipped:
            out.append(
                problem(
                    preset.path,
                    "no spec in the same folder lists this file as a Source, "
                    "so no package ships it",
                    "not-packaged",
                    name,
                )
            )
    return out


def check_image_lists(
    presets: Sequence[Preset], root: Path, tracked: Sequence[str]
) -> list[Problem]:
    """``disable_by_default`` and ``enable_services`` of image/packages.yaml against the presets."""
    if PACKAGES_YAML not in tracked:
        return []
    text = read_text(root, PACKAGES_YAML)
    if text is None:
        return []
    system = ordered(presets, False)
    out: list[Problem] = []
    for key, verb in (("disable_by_default", "disable"), ("enable_services", "enable")):
        for unit in yaml_list(text, key):
            decided = effective(system, unit)
            if decided is None or decided.verb != verb:
                how = (
                    "no preset line"
                    if decided is None
                    else f"'{decided.verb}' at {decided.path}:{decided.number}"
                )
                out.append(
                    problem(
                        PACKAGES_YAML,
                        f"{key} lists '{unit}', but the system presets end up with {how}, "
                        f"not '{verb}'",
                        "image-list",
                        unit,
                    )
                )
    return out


def check_files(root: Path, tracked: Sequence[str], only: Sequence[str] = ()) -> list[Problem]:
    """Check the presets among ``tracked`` (paths from ``root``); ``only`` limits which ones.

    The other presets, units, specs and image lists are always read to resolve the
    units and the order of the lines.
    """
    problems: list[Problem] = []
    presets: list[Preset] = []
    for rel in sorted(p for p in tracked if p.endswith(".preset")):
        try:
            raw = (root / rel).read_bytes()
        except OSError as err:
            problems.append(Problem(rel, f"cannot read the file: {err.strerror or err}"))
            continue
        preset, found = parse_preset(rel, raw)
        presets.append(preset)
        problems += found
    repo_units: dict[str, str] = {}
    installs: dict[str, Install] = {}
    for rel in sorted(p for p in tracked if p.endswith(REPO_UNIT_SUFFIXES)):
        repo_units.setdefault(posixpath.basename(rel), rel)
        text = read_text(root, rel)
        if text is not None:
            installs[rel] = parse_install(text)
    packages = package_names(root, tracked)
    specs = spec_lines(root, tracked)
    for preset in presets:
        for line in preset.lines:
            problems += check_unit_line(line, preset, repo_units, installs, packages, specs)
    problems += check_order(presets)
    problems += check_packaging(presets, tracked, root)
    problems += check_image_lists(presets, root, tracked)
    if only:
        wanted = set(only)
        problems = [p for p in problems if p.path in wanted]
    return sorted(problems, key=problem_order)


def problem_order(item: Problem) -> tuple[str, bool, int, str]:
    """By file, errors first, then by line number (not as text: 4 before 10)."""
    match = re.match(r"line (\d+)", item.message)
    return (item.path, item.warning, int(match.group(1)) if match else 0, item.message)


def presets_table(root: Path, tracked: Sequence[str]) -> str:
    """A Markdown table: every preset line, where its unit comes from, what finally happens."""
    presets = []
    for rel in sorted(p for p in tracked if p.endswith(".preset")):
        try:
            presets.append(parse_preset(rel, (root / rel).read_bytes())[0])
        except OSError:
            continue
    repo_units = {
        posixpath.basename(p): p for p in sorted(tracked) if p.endswith(REPO_UNIT_SUFFIXES)
    }
    header = ["Preset", "Line", "Unit", "From", "Final"]
    rows = [header, ["---"] * len(header)]
    for user in (False, True):
        group = ordered(presets, user)
        for preset in group:
            for line in preset.lines:
                found = None if line.is_glob else resolve(line.pattern, repo_units)
                source = "glob" if line.is_glob else "unknown"
                if found is not None:
                    source = (
                        f"`{found.where}`" if found.kind == "repo" else f"package {found.where}"
                    )
                decided = effective(group, line.pattern) if not line.is_glob else line
                final = decided.verb if decided is not None else "—"
                if decided is not None and decided is not line:
                    final += " (earlier line)"
                rows.append(
                    [f"`{preset.path}`", str(line.number), f"`{line.pattern}`", source, final]
                )
    return "\n".join("| " + " | ".join(row) + " |" for row in rows)


def tracked_files(root: Path) -> list[str]:
    patterns = ["*.preset", "*.service", "*.socket", "*.timer", "*.path", "*.spec", "*.kiwi"]
    patterns.append(PACKAGES_YAML)
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
    usage = "usage: python3 tools/check_presets.py [--table] [PRESET ...]"
    options = [a for a in args if a.startswith("-")]
    if any(o not in ("--table", "-h", "--help") for o in options):
        print(usage, file=sys.stderr)
        return 2
    if "-h" in options or "--help" in options:
        print(usage, file=sys.stderr)
        return 0
    try:
        tracked = tracked_files(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    all_presets = [p for p in tracked if p.endswith(".preset")]
    only = [a.replace("\\", "/") for a in args if not a.startswith("-")]
    unknown = [a for a in only if a not in all_presets]
    if unknown:
        print(f"not a tracked preset file: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if not all_presets:
        print("no tracked preset files found", file=sys.stderr)
        return 2
    problems = check_files(ROOT, tracked, only)
    for item in problems:
        print(item, file=sys.stderr)
    if "--table" in options:
        print(presets_table(ROOT, tracked))
    errors = sum(not p.warning for p in problems)
    print(
        f"{len(only or all_presets)} preset files checked: "
        f"{errors} errors, {len(problems) - errors} warnings"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
