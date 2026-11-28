#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the tracked D-Bus files: bus policies (``*.conf``) and activation files (``*.service``).

Templates (``*.conf.in``, ``*.service.in``) and upper-case suffixes count too. A file is
a *policy* when its path has a ``dbus-1`` folder or its text holds a
``<busconfig`` element or a ``DOCTYPE busconfig``. A ``*.service`` file is an *activation*
file when its path has a ``dbus-1`` folder or it has a ``[D-BUS Service]`` group. Systemd
units (a ``*.service`` with ``[Unit]`` and ``[Service]``) are not D-Bus files and are
left alone, unless a path is given on the command line.

For every policy:

* the file is not a symlink, is not larger than 1 MiB and is valid XML (read by expat, never
  run through the network); the root is ``busconfig``;
* there is no entity declaration (``<!ENTITY ...>``, general or parameter) in the DOCTYPE;
  the DOCTYPE may name only ``busconfig`` and the usual ``PUBLIC``/``SYSTEM`` id is fine;
* a ``<policy>`` has exactly one of ``context`` (``default`` or ``mandatory``), ``user``,
  ``group``, ``at_console``; inside it only ``<allow>`` and ``<deny>`` stand, and they use
  only the attributes dbus-daemon knows;
* ``<allow own="NAME">`` and ``<allow own_prefix="NAME">`` name something of ours:
  ``org.vilna.<element>[.<element>...]`` (ASCII letters, digits, ``_`` and ``-``; for the
  prefix also ``org.vilna`` itself) or a name of ``DOCUMENTED_NAMES``; no ``*``;
* no ``<allow send_destination="*">`` (whatever else the element says);
* no ``<allow>`` without a condition (no attribute, or only the attributes about the type of the
  message, reply or eavesdropping, which name no peer); an attribute with an empty value is
  an error too;
* an ``<allow>`` with ``send_*`` attributes names ``send_destination`` (or its prefix):
  without it the rule sends to every service; ``receive_sender="*"`` is an error too;
* ``<allow own>`` inside ``<policy context="default">`` is a warning: any user may own the name.

For every activation file:

* the file is a key file with a ``[D-BUS Service]`` group and no repeated group or key;
* ``Name`` is ours (the rule above for ``own``) and in no other activation file, and ``Exec``
  is one program by its absolute path (no shell, no ``|``, ``;``, ``&``, ``$`` or
  backtick); a file name that is not ``<Name>.service`` is a warning;
* ``SystemdService`` is set, is a plain unit name ending in ``.service`` and names a systemd
  unit that is tracked in the repo (by its file name, an instance of a template
  ``name@.service`` or an ``Alias=`` of its ``[Install]``). Another activation file never
  counts as that unit.

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but do not fail
the check; the old violations of ``main`` go to ``LEGACY`` and become warnings. Today the
repo tracks no such file; the check is there for the first one. Only ``git ls-files`` is
run. Nothing else is executed, nothing is read from the network and nothing is written.

Run: python3 tools/check_dbus.py [FILE ...]
FILE is a ``.conf`` or ``.service`` path from the repo root; without it every tracked D-Bus
file is checked (the units are always looked up among all tracked files).
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import posixpath
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from xml.parsers import expat

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

MAX_BYTES = 1024 * 1024
OWN_PREFIX = "org.vilna"
# Names outside org.vilna.* that the repo documents as its own. Add a name only together
# with the document that explains why we own it.
DOCUMENTED_NAMES: frozenset[str] = frozenset()
# Problems the repo already has today: (path from the repo root, rule). They are warnings.
LEGACY: frozenset[tuple[str, str]] = frozenset()

NAME_TAIL = r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*"
OUR_NAME = re.compile(rf"{re.escape(OWN_PREFIX)}\.{NAME_TAIL}")
UNIT_NAME = re.compile(r"[A-Za-z0-9:_.\\@-]+\.service")
UNIT_ALIAS_SPLIT = re.compile(r"\s+")
KEY_LINE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[^\]]*\])?)\s*=(.*)", re.S)

POLICY_SELECTORS = ("context", "user", "group", "at_console")
CONTEXTS = ("default", "mandatory")
RULE_ATTRIBUTES = frozenset(
    {
        "send_interface",
        "send_member",
        "send_error",
        "send_destination",
        "send_destination_prefix",
        "send_path",
        "send_type",
        "send_requested_reply",
        "receive_interface",
        "receive_member",
        "receive_error",
        "receive_sender",
        "receive_path",
        "receive_type",
        "receive_requested_reply",
        "eavesdrop",
        "own",
        "own_prefix",
        "user",
        "group",
    }
)
# These say what kind of message it is, not who sends or receives it.
WEAK_ATTRIBUTES = frozenset(
    {
        "send_type",
        "receive_type",
        "send_requested_reply",
        "receive_requested_reply",
        "eavesdrop",
    }
)
KNOWN_ROOT_TAGS = ("busconfig",)


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False
    rule: str = ""

    def __str__(self) -> str:
        kind = "warning: " if self.warning else ""
        return f"{self.path}: {kind}{self.message}"


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    line: int
    children: list[Element] = field(default_factory=list)


class EntityDeclared(Exception):
    """Raised from the expat handler at the first ``<!ENTITY``."""


# --- reading XML -----------------------------------------------------------------------


def parse_xml(data: bytes) -> tuple[Element, str | None]:
    """The element tree of ``data`` and the DOCTYPE name (None when there is none).

    Raises ``expat.ExpatError`` for bad XML and ``EntityDeclared`` for an entity.
    """
    parser = expat.ParserCreate()
    parser.buffer_text = True
    stack: list[Element] = []
    roots: list[Element] = []
    doctype: list[str] = []

    def start(tag: str, attrs: dict[str, str]) -> None:
        element = Element(tag, dict(attrs), parser.CurrentLineNumber)
        if stack:
            stack[-1].children.append(element)
        else:
            roots.append(element)
        stack.append(element)

    def end(_tag: str) -> None:
        stack.pop()

    def on_doctype(name: str, _system: str | None, _public: str | None, _subset: int) -> None:
        doctype.append(name)

    def on_entity(name: str, *_rest: object) -> None:
        raise EntityDeclared(name)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = on_doctype
    parser.EntityDeclHandler = on_entity
    parser.Parse(data, True)
    return roots[0], (doctype[0] if doctype else None)


def our_name(name: str, prefix: bool = False) -> bool:
    """True when ``name`` is ``org.vilna.<...>`` or documented (``org.vilna`` too for a prefix)."""
    if OUR_NAME.fullmatch(name) or name in DOCUMENTED_NAMES:
        return True
    return prefix and name == OWN_PREFIX


# --- policies --------------------------------------------------------------------------


def check_rule(path: str, element: Element, context: str | None) -> list[Problem]:
    tag, attrs, where = element.tag, element.attrs, f"line {element.line}"
    problems: list[Problem] = []

    def error(message: str, rule: str) -> None:
        problems.append(Problem(path, f"{message} ({where})", rule=rule))

    for key in sorted(attrs):
        if key not in RULE_ATTRIBUTES:
            error(f"<{tag}> has the unknown attribute {key!r}", "unknown-attribute")
        elif not attrs[key].strip():
            error(f"<{tag} {key}=...> is empty", "empty-attribute")
    if tag != "allow":
        return problems
    known = {k for k in attrs if k in RULE_ATTRIBUTES}
    if not attrs:
        error("<allow> has no condition: it allows everything", "unconditional-allow")
    elif known and known <= WEAK_ATTRIBUTES:
        names = ", ".join(sorted(known))
        error(f"<allow> names no peer, name, interface or path (only {names})", "weak-allow")
    if attrs.get("send_destination", "").strip() == "*":
        error('<allow send_destination="*"> lets the policy send to every service', "wildcard-send")
    sends = {k for k in known if k.startswith("send_")} - WEAK_ATTRIBUTES
    if sends and not sends & {"send_destination", "send_destination_prefix"}:
        # A missing attribute means "any": this lets messages go to every service.
        error("<allow> with send_* but no send_destination sends to every service", "no-destination")
    if attrs.get("receive_sender", "").strip() == "*":
        error('<allow receive_sender="*"> takes messages from every service', "wildcard-receive")
    for key, prefix in (("own", False), ("own_prefix", True)):
        value = attrs.get(key)
        if value is None or not value.strip():
            continue
        if not our_name(value, prefix):
            error(f"<allow {key}={value!r}> is not an org.vilna.* name", "foreign-name")
        elif context == "default":
            message = f"<allow {key}={value!r}> in context default: any user may own it ({where})"
            problems.append(Problem(path, message, warning=True, rule="default-own"))
    return problems


def check_policy_element(path: str, policy: Element) -> list[Problem]:
    where = f"line {policy.line}"
    problems: list[Problem] = []
    selectors = [k for k in POLICY_SELECTORS if k in policy.attrs]
    extra = sorted(set(policy.attrs) - set(POLICY_SELECTORS))
    if len(selectors) != 1 or extra:
        problems.append(
            Problem(
                path,
                f"<policy> needs exactly one of {', '.join(POLICY_SELECTORS)} ({where})",
                rule="policy-selector",
            )
        )
    context = policy.attrs.get("context")
    if context is not None and context not in CONTEXTS:
        problems.append(
            Problem(path, f"<policy context={context!r}> is not default or mandatory ({where})")
        )
    for child in policy.children:
        if child.tag not in ("allow", "deny"):
            problems.append(
                Problem(path, f"<policy> holds <{child.tag}>, only allow and deny ({where})")
            )
            continue
        problems += check_rule(path, child, context)
    return problems


def check_policy(path: str, data: bytes) -> list[Problem]:
    try:
        root, doctype = parse_xml(data)
    except EntityDeclared as err:
        return [Problem(path, f"the DOCTYPE declares the entity {err.args[0]!r}", rule="entity")]
    except expat.ExpatError as err:
        return [Problem(path, f"not valid XML: {expat.ErrorString(err.code)} (line {err.lineno})")]
    problems: list[Problem] = []
    if doctype is not None and doctype != "busconfig":
        problems.append(Problem(path, f"the DOCTYPE names {doctype!r}, not busconfig"))
    if root.tag not in KNOWN_ROOT_TAGS:
        problems.append(Problem(path, f"the root element is <{root.tag}>, not <busconfig>"))
        return problems
    stack = [root]
    while stack:
        element = stack.pop()
        if element.tag == "policy":
            problems += check_policy_element(path, element)
        else:
            stack.extend(element.children)
    return problems


# --- activation files ------------------------------------------------------------------


@dataclass
class KeyFile:
    groups: dict[str, dict[str, str]]
    problems: list[str]


def parse_keyfile(text: str) -> KeyFile:
    groups: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    current: dict[str, str] | None = None
    for number, raw in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
            if name in groups:
                problems.append(f"the group [{name}] is repeated (line {number})")
            current = groups.setdefault(name, {})
            continue
        match = KEY_LINE.fullmatch(line)
        if match is None:
            problems.append(f"line {number} is not a comment, a group or Key=Value")
        elif current is None:
            problems.append(f"line {number}: a key before any group")
        else:
            key, value = match.group(1), match.group(2).strip()
            if key in current:
                problems.append(f"the key {key} is repeated (line {number})")
            current[key] = value
    return KeyFile(groups, problems)


def unit_names(root: Path, tracked: Sequence[str]) -> set[str]:
    """File names and ``Alias=`` names of the tracked systemd units (D-Bus files excluded)."""
    names: set[str] = set()
    for rel in tracked:
        if not rel.endswith(".service") or is_dbus_dir(rel):
            continue
        base = posixpath.basename(rel)
        full = root / rel
        try:
            text = (
                full.read_text(encoding="utf-8", errors="replace") if not full.is_symlink() else ""
            )
        except OSError:
            text = ""
        if is_activation_text(text):
            continue
        names.add(base)
        group = ""
        for raw in text.replace("\r\n", "\n").split("\n"):
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                group = line[1:-1]
            elif group == "Install" and line.startswith("Alias="):
                names.update(n for n in UNIT_ALIAS_SPLIT.split(line[6:].strip()) if n)
    return names


def unit_exists(name: str, names: set[str]) -> bool:
    if name in names:
        return True
    match = re.fullmatch(r"([^@]+)@[^.]+(\.service)", name)
    return bool(match and f"{match.group(1)}@{match.group(2)}" in names)


def check_activation(
    path: str, text: str, root: Path, tracked: Sequence[str], cache: dict[str, set[str]]
) -> list[Problem]:
    keyfile = parse_keyfile(text)
    problems = [Problem(path, msg, rule="keyfile") for msg in keyfile.problems]
    group = keyfile.groups.get("D-BUS Service")
    if group is None:
        problems.append(Problem(path, "no [D-BUS Service] group", rule="no-group"))
        return problems
    name = group.get("Name", "")
    if not name:
        problems.append(Problem(path, "Name is missing or empty", rule="name"))
    elif not our_name(name):
        problems.append(
            Problem(path, f"Name={name} is not an org.vilna.* name", rule="foreign-name")
        )
    elif posixpath.basename(path) != f"{name}.service":
        problems.append(
            Problem(path, f"the file name is not {name}.service", warning=True, rule="file-name")
        )
    exec_line = group.get("Exec", "")
    if not exec_line:
        problems.append(Problem(path, "Exec is missing or empty", rule="exec"))
    elif exec_shell(exec_line):
        # dbus-daemon runs Exec itself when systemd is not there or the unit is gone.
        problems.append(
            Problem(path, "Exec runs a shell or a pipeline, not one program", rule="exec-shell")
        )
    if name:
        cache.setdefault(f"name:{name}", set()).add(path)
    unit = group.get("SystemdService", "")
    if not unit:
        problems.append(Problem(path, "SystemdService is missing", rule="systemd-service"))
    elif not UNIT_NAME.fullmatch(unit) or len(unit) > 255:
        problems.append(
            Problem(path, f"SystemdService={unit} is not a plain *.service name", rule="unit-name")
        )
    else:
        if "names" not in cache:
            cache["names"] = unit_names(root, tracked)
        if not unit_exists(unit, cache["names"]):
            problems.append(
                Problem(path, f"SystemdService={unit}: no such unit in the repo", rule="no-unit")
            )
    return problems


SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "busybox", "env"})


def exec_shell(line: str) -> bool:
    """True when ``Exec`` starts a shell or holds shell syntax instead of one program."""
    words = line.split()
    program = posixpath.basename(words[0]) if words else ""
    return program in SHELLS or not line.startswith("/") or any(c in line for c in "|;&`$<>")


def dbus_suffix(rel: str) -> str:
    """``.conf`` or ``.service`` for any case and for a template (``*.conf.in``)."""
    name = posixpath.basename(rel).lower()
    name = name[: -len(".in")] if name.endswith(".in") else name
    return posixpath.splitext(name)[1]


# --- finding and reading the files -----------------------------------------------------


def is_dbus_dir(path: str) -> bool:
    return "dbus-1" in posixpath.dirname(path).split("/")


def is_activation_text(text: str) -> bool:
    return any(line.strip() == "[D-BUS Service]" for line in text.split("\n"))


def is_policy_data(data: bytes) -> bool:
    return b"<busconfig" in data or b"DOCTYPE busconfig" in data


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    ).stdout
    return [p for p in out.decode("utf-8", errors="surrogateescape").split("\0") if p]


def read_file(root: Path, rel: str) -> tuple[bytes | None, Problem | None]:
    full = root / rel
    try:
        if full.is_symlink():
            return None, Problem(rel, "is a symlink; D-Bus files must be real files")
        size = full.stat().st_size
        if size > MAX_BYTES:
            return None, Problem(rel, f"is {size} bytes, more than {MAX_BYTES}")
        return full.read_bytes(), None
    except OSError as err:
        return None, Problem(rel, f"cannot read: {err.strerror or err}")


def check_file(
    root: Path,
    rel: str,
    tracked: Sequence[str],
    cache: dict[str, set[str]],
    explicit: bool = False,
) -> tuple[list[Problem], bool]:
    """The problems of one file and whether it was a D-Bus file at all."""
    suffix = dbus_suffix(rel)
    if suffix not in (".conf", ".service"):
        return [], False
    in_dir = is_dbus_dir(rel)
    full = root / rel
    if not in_dir and not explicit and full.is_symlink():
        return [], False  # cannot tell what it is without following it
    data, problem = read_file(root, rel)
    if problem is not None:
        return ([problem] if in_dir or explicit else []), in_dir or explicit
    assert data is not None
    if suffix == ".conf":
        if not (in_dir or explicit or is_policy_data(data)):
            return [], False
        return check_policy(rel, data), True
    text = data.decode("utf-8", errors="replace")
    if not (in_dir or is_activation_text(text)):
        if explicit:
            return [Problem(rel, "is not a D-Bus activation file ([D-BUS Service])")], True
        return [], False
    if "\x00" in text or data.startswith(b"\xef\xbb\xbf"):
        return [Problem(rel, "has a NUL byte or a byte order mark")], True
    return check_activation(rel, text, root, tracked, cache), True


def downgrade(problem: Problem) -> Problem:
    if (problem.path, problem.rule) in LEGACY:
        return Problem(problem.path, problem.message, warning=True, rule=problem.rule)
    return problem


def check_files(
    root: Path, paths: Sequence[str], tracked: Sequence[str], explicit: bool = False
) -> tuple[list[Problem], int]:
    """All problems (sorted) and the number of D-Bus files that were checked."""
    problems: list[Problem] = []
    cache: dict[str, set[str]] = {}
    count = 0
    for rel in paths:
        found, is_dbus = check_file(root, rel, tracked, cache, explicit)
        count += is_dbus
        problems += [downgrade(p) for p in found]
    for key, owners in cache.items():
        if key.startswith("name:") and len(owners) > 1:
            name = key[len("name:") :]
            for owner in sorted(owners):
                message = f"Name={name} is also in another activation file"
                problems.append(downgrade(Problem(owner, message, rule="duplicate-name")))
    problems.sort(key=lambda p: (p.path, p.warning, p.message))
    return problems, count


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    usage = "usage: python3 tools/check_dbus.py [FILE.conf | FILE.service ...]"
    if any(a.startswith("-") for a in args):
        print(usage, file=sys.stderr)
        return 0 if args in (["-h"], ["--help"]) else 2
    for arg in args:
        parts = arg.split("/")
        if arg.startswith("/") or ".." in parts or not arg.endswith((".conf", ".service")):
            print(
                f"{arg}: must be a .conf or .service path from the repo root\n{usage}",
                file=sys.stderr,
            )
            return 2
        if not (ROOT / arg).exists() and not (ROOT / arg).is_symlink():
            print(f"{arg}: no such file", file=sys.stderr)
            return 2
    try:
        tracked = tracked_files(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    problems, count = check_files(ROOT, args or tracked, tracked, explicit=bool(args))
    for problem in problems:
        print(problem, file=sys.stderr)
    errors = sum(not p.warning for p in problems)
    print(f"{count} D-Bus files checked: {errors} errors, {len(problems) - errors} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    # A file name that is not UTF-8 is printed escaped, not as a traceback.
    sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    sys.exit(main())
