#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the tracked SELinux policy sources of the repo, as text (no ``checkmodule``).

Files: every tracked ``*.te``, ``*.fc``, ``*.if`` and ``*.cil`` file under ``herkos/selinux/``
or ``docs/``, and the fenced code blocks of ``docs/**/*.md`` that hold policy (language
``te``, ``selinux``, ``sepolicy``, ``refpolicy``, ``if`` or ``cil``, or no language when the
block has an ``allow`` rule). A module is the set of files with the same folder and name
(``foo.te``, ``foo.fc``, ``foo.if``, ``foo.cil``).

Rules:

* **types of a .fc**: every type of a file context (``object_r:TYPE``; the context
  ``<<none>>`` has none) is defined in the ``.te`` (or ``.cil``) of the same module: by
  ``type``, ``typealias`` or ``(type ...)``, or named in a ``require`` / ``gen_require``
  block (a type that the module takes from the base policy must be said so). A ``.fc``
  with no ``.te`` or ``.cil`` next to it is an error. Types that an interface creates when
  it is called (a template that makes ``$1_t``) are not seen: name them in ``require``.
  The same holds for the type of a CIL ``filecon``.
* **sys_admin**: no ``allow`` (``.te``, ``.if``, CIL ``allow`` and ``classpermissionset``)
  grants ``sys_admin`` of the class ``capability`` or ``cap_userns``, to ``self`` or to any
  other type. A comment does not excuse it. ``neverallow``, ``dontaudit`` and ``auditallow``
  are not grants and are not read.
* **permissive**: no ``permissive TYPE;`` or CIL ``(typepermissive TYPE)``: such a domain
  is not confined, SELinux only logs. A macro (``define``) that holds ``sys_admin`` is a
  sys_admin grant too, and an ``allowxperm`` over every ioctl number (``0x0000-0xffff``,
  ``*`` or ``~``) is a wildcard.
* **wildcards**: no ``*`` or ``~`` (complement), and in CIL no ``all``, ``*`` or ``not``,
  in an ``allow`` (types, classes or permissions) unless a comment explains it: a comment
  on a line of the rule, or comment lines right above it (no blank line between). The text
  must say something (10 characters, one word of 3 letters); an SPDX line is not an
  explanation.
* **interfaces**: every call of an interface or macro exists. In ``.te`` and ``.if``:
  ``interface(`x',`` and ``template(`x',`` of any tracked ``.if``, or ``define(`x',``. A
  name that is not defined but has a layer of the reference policy as prefix
  (``files_``, ``logging_``, ``systemd_``, ...) or ends in ``_pattern`` comes from the
  installed ``selinux-policy``: it cannot be checked offline and is a warning. Any other
  unknown name is an error. In CIL: ``(call NAME ...)`` names a ``(macro NAME ...)`` of a
  tracked ``.cil``; inside ``(optional ...)`` an unknown macro is a warning, elsewhere an
  error.
* **syntax**: CIL parentheses and strings are balanced; files are UTF-8 without a byte
  order mark.

Errors make the exit code 1. Warnings (``path: warning: ...``) do not. A violation that the
repo already has goes to ``LEGACY`` and is a warning (``LEGACY:``); fix it and delete the
line.

Only ``git ls-files`` is run. Nothing is read from the network, nothing is written, and no
policy is compiled or loaded.

Run: python3 tools/check_selinux.py [FILE.te | FILE.fc | FILE.if | FILE.cil | FILE.md ...]
Without arguments every tracked file under ``herkos/selinux/`` and ``docs/`` is checked.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import bisect
import re
import subprocess
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

SCAN_ROOTS = ("herkos/selinux/", "docs/")
POLICY_SUFFIXES = (".te", ".fc", ".if", ".cil")
SUFFIXES = (*POLICY_SUFFIXES, ".md")
# Violations that the repo already has: (path, rule). Each one is a warning, not an error.
# Fix the file, then delete the line. (None today.)
LEGACY: frozenset[tuple[str, str]] = frozenset()

# Fenced-block languages of docs that are policy sources.
TE_LANGS = frozenset({"te", "selinux", "sepolicy", "refpolicy", "if"})
CAPABILITY_CLASSES = frozenset({"capability", "cap_userns"})
CIL_WILDCARDS = frozenset({"all", "*", "not", "~"})
# Layers of the reference policy: an interface "<layer>_..." is provided by selinux-policy.
POLICY_LAYERS = frozenset(
    {
        "abrt",
        "accountsd",
        "apache",
        "application",
        "auth",
        "authlogin",
        "bootloader",
        "cgroup",
        "clock",
        "consoletype",
        "corecmd",
        "corenet",
        "cron",
        "dbus",
        "devices",
        "dmesg",
        "domain",
        "files",
        "filesystem",
        "fs",
        "fstools",
        "getty",
        "gnome",
        "hostname",
        "init",
        "ipsec",
        "iptables",
        "kernel",
        "libraries",
        "locallogin",
        "logging",
        "lvm",
        "mcs",
        "miscfiles",
        "mls",
        "modutils",
        "mount",
        "netutils",
        "nscd",
        "polkit",
        "rpm",
        "selinux",
        "seutil",
        "setrans",
        "ssh",
        "storage",
        "sudo",
        "sysnet",
        "systemd",
        "terminal",
        "ubac",
        "udev",
        "unconfined",
        "userdom",
        "usermanage",
        "xdg",
        "xserver",
    }
)
# Support macros of the reference policy (not interfaces, but called the same way).
SUPPORT_MACROS = frozenset({"can_exec", "domain_trans", "domain_auto_trans", "allow_map"})
# m4 and policy keywords that look like a call. They are not interfaces.
KEYWORDS = frozenset(
    {
        "gen_require",
        "require",
        "optional_policy",
        "tunable_policy",
        "gen_tunable",
        "gen_bool",
        "gen_context",
        "gen_user",
        "gen_cats",
        "gen_sens",
        "gen_levels",
        "gen_reqs",
        "policy_module",
        "interface",
        "template",
        "define",
        "undefine",
        "ifdef",
        "ifndef",
        "ifelse",
        "if",
        "else",
        "include",
        "sinclude",
        "divert",
        "dnl",
        "shift",
        "eval",
        "substr",
        "index",
        "len",
        "format",
        "refpolicywarn",
        "refpolicyerr",
        "ifsecret",
        "typeattribute",
        "type",
        "typealias",
        "allow",
        "neverallow",
        "dontaudit",
        "auditallow",
        "role",
        "user",
        "range_transition",
        "type_transition",
    }
)

IDENT = r"[A-Za-z_]\w*"
ALLOW = re.compile(r"(?<![\w$])allow(?=\s)([^;]*)(?:;|$)")
ALLOWXPERM = re.compile(r"(?<![\w$])allowxperm(?=\s)([^;]*)(?:;|$)")
XPERM_RANGE = re.compile(r"0x([0-9A-Fa-f]+)\s*-\s*0x([0-9A-Fa-f]+)")
PERMISSIVE = re.compile(r"(?<![\w$])permissive\s+[\w$]+\s*;")
# An m4 macro the rules use by name: define(`name', `text').
DEFINE_BODY = re.compile(r"(?<![\w$])define\(\s*`[\w$]+'\s*,\s*`([^']*)'")
CALL = re.compile(rf"(?<![\w$.])({IDENT})\s*\(")
IFACE_DEF = re.compile(rf"(?<![\w$])(?:interface|template|define)\(\s*[`']?({IDENT})")
REQUIRE_OPEN = re.compile(r"(?<![\w$])(gen_require\(\s*`|require\s*\{)")
M4_CLOSE = re.compile(r"'\s*\)")
TYPE_WORD = re.compile(r"(?<![\w$])type\s+")
TYPE_HEAD = re.compile(rf"\s*({IDENT})\s*(?:alias\s+(\{{[^}}]*\}}|{IDENT}))?")
TYPEALIAS = re.compile(rf"(?<![\w$])typealias\s+{IDENT}\s+alias\s+(\{{[^}}]*\}}|{IDENT})\s*;")
FC_TYPE = re.compile(rf"object_r:({IDENT})")
DNL = re.compile(r"dnl(?!\w)")
TE_LIKE = re.compile(r"^\s*allow\s+\S+\s+\S+\s*:\s*\S+", re.M)
CIL_LIKE = re.compile(r"\(\s*allow\s")
FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})\s*([^\s`]*)")


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    line: int = 0
    warning: bool = False
    rule: str = ""
    legacy: bool = False

    def __str__(self) -> str:
        where = f"line {self.line}: " if self.line else ""
        kind = "warning: " if self.warning else ""
        tag = "LEGACY: " if self.legacy else ""
        return f"{self.path}: {kind}{tag}{where}{self.message}"


# --- comments ---------------------------------------------------------------


@dataclass
class Source:
    """A policy text with its comments taken out. Lines are numbered from 1."""

    clean: str
    comments: dict[int, str]
    code_lines: set[int]
    only: frozenset[int] = field(init=False)
    starts: list[int] = field(init=False)

    def __post_init__(self) -> None:
        # Offsets where a line starts: starts[n - 1] is the offset of line n, so bisect gives n.
        self.starts = [0] + [i + 1 for i, ch in enumerate(self.clean) if ch == "\n"]
        # Lines that hold a comment and nothing else.
        self.only = frozenset(set(self.comments) - self.code_lines)


def strip_te(text: str) -> Source:
    """Remove ``#`` and ``dnl`` comments (not inside double quotes) and blank the text of strings.

    The line numbers stay.
    """
    out: list[str] = []
    comments: dict[int, str] = {}
    code: set[int] = set()
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        kept: list[str] = []
        in_string = False
        i = 0
        while i < len(line):
            ch = line[i]
            if in_string:
                # The text of a string is not code: keep the quotes, blank the rest.
                if ch == "\\" and i + 1 < len(line):
                    kept.append("  ")
                    i += 1
                elif ch == '"':
                    in_string = False
                    kept.append(ch)
                else:
                    kept.append(" ")
            elif ch == '"':
                in_string = True
                kept.append(ch)
            elif ch == "#":
                comments[number] = line[i + 1 :]
                break
            elif ch == "d" and DNL.match(line, i) and (i == 0 or not _is_word(line[i - 1])):
                comments[number] = line[i + 3 :]
                break
            else:
                kept.append(ch)
            i += 1
        clean = "".join(kept).rstrip()
        if clean.strip():
            code.add(number)
        out.append(clean)
    return Source("\n".join(out), comments, code)


def _is_word(ch: str) -> bool:
    return ch.isalnum() or ch in "_$"


def explanatory(texts: Sequence[str]) -> bool:
    """True when the comment text says something (an SPDX line or a row of ``#`` does not)."""
    kept = [t for t in texts if "SPDX-License-Identifier" not in t]
    joined = re.sub(r"\s+", " ", " ".join(kept)).strip()
    return len(joined) >= 10 and re.search(r"[^\W\d_]{3,}", joined) is not None


def is_explained(src: Source, first: int, last: int) -> bool:
    """A comment on a line of ``first..last``, or comment lines right above ``first``."""
    texts = [src.comments[n] for n in range(first, last + 1) if n in src.comments]
    only = src.only
    above: list[str] = []
    n = first - 1
    while n in only:
        above.append(src.comments[n])
        n -= 1
    return explanatory([*reversed(above), *texts])


def line_of(src: Source, pos: int) -> int:
    """The line (from 1) of the character at ``pos`` of ``src.clean``."""
    return bisect.bisect_right(src.starts, pos)


# --- .te and .if ------------------------------------------------------------


def require_blocks(clean: str) -> list[tuple[int, int, str]]:
    """(start, end, text) of every ``gen_require(`...')`` and ``require { ... }`` block."""
    found: list[tuple[int, int, str]] = []
    last_end = 0
    for m in REQUIRE_OPEN.finditer(clean):
        if m.start() < last_end:
            continue
        if m.group(1).startswith("gen_require"):
            close = M4_CLOSE.search(clean, m.end())
            end = close.start() if close else -1
            stop = close.end() if close else -1
        else:
            end = stop = clean.find("}", m.end())
            stop = stop + 1 if stop >= 0 else -1
        if end < 0:
            break  # no block closes after this one either
        found.append((m.start(), stop, clean[m.end() : end]))
        last_end = stop
    return found


def type_statement(statement: str) -> str:
    """The text after the last ``type`` word of a statement (``""`` when it has none)."""
    words = list(TYPE_WORD.finditer(statement))
    return statement[words[-1].end() :] if words else ""


def blank_out(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """Replace the given spans by spaces, keeping the newlines (and so the line numbers)."""
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def check_te_rules(path: str, src: Source, offset: int = 0) -> list[Problem]:
    """The sys_admin and wildcard rules for the ``allow`` statements of a .te text."""
    problems: list[Problem] = []
    for m in ALLOW.finditer(src.clean):
        body = m.group(1)
        first = line_of(src, m.start())
        last = line_of(src, m.end(1))
        words = set(re.findall(r"\w+", body))
        if words & CAPABILITY_CLASSES and "sys_admin" in words:
            problems.append(
                Problem(
                    path,
                    "allow gives sys_admin (CAP_SYS_ADMIN is nearly root)",
                    first + offset,
                    rule="sys-admin",
                )
            )
        flat = body.replace("$*", "").replace("$@", "")
        if ("*" in flat or "~" in flat) and not is_explained(src, first, last):
            problems.append(
                Problem(
                    path,
                    "allow uses * or ~ with no comment that explains it",
                    first + offset,
                    rule="wildcard",
                )
            )
    for m in PERMISSIVE.finditer(src.clean):
        # A permissive domain is not confined at all: SELinux only logs what it does.
        line = line_of(src, m.start()) + offset
        problems.append(Problem(path, "permissive domain", line, rule="permissive"))
    for m in DEFINE_BODY.finditer(src.clean):
        if "sys_admin" in re.findall(r"\w+", m.group(1)):
            line = line_of(src, m.start()) + offset
            message = "a macro holds sys_admin (an allow can use it by name)"
            problems.append(Problem(path, message, line, rule="sys-admin"))
    for m in ALLOWXPERM.finditer(src.clean):
        whole = any(
            int(lo, 16) == 0 and int(hi, 16) >= 0xFFFF for lo, hi in XPERM_RANGE.findall(m.group(1))
        )
        if whole or "*" in m.group(1) or "~" in m.group(1):
            line = line_of(src, m.start()) + offset
            problems.append(
                Problem(path, "allowxperm allows every ioctl number", line, rule="wildcard")
            )
    return problems


@dataclass
class Call:
    path: str
    line: int
    name: str
    optional: bool = False


@dataclass
class Facts:
    """What the files define and use, resolved after all of them are read."""

    types: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    required: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    interfaces: set[str] = field(default_factory=set)
    macros: set[str] = field(default_factory=set)
    contexts: list[tuple[str, int, str]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    macro_calls: list[Call] = field(default_factory=list)


def module_key(path: str) -> tuple[str, str]:
    p = PurePosixPath(path)
    return (str(p.parent), p.stem)


def names_of(group: str) -> list[str]:
    return re.findall(IDENT, group)


def scan_te(path: str, text: str, facts: Facts) -> list[Problem]:
    src = strip_te(text)
    problems = check_te_rules(path, src)
    key = module_key(path)
    clean = src.clean
    required = facts.required.setdefault(key, set())
    spans = []
    for start, end, block in require_blocks(clean):
        spans.append((start, end))
        for statement in block.split(";"):
            required.update(names_of(type_statement(statement)))
    body = blank_out(clean, spans)
    defined = facts.types.setdefault(key, set())
    for statement in body.split(";"):
        head = TYPE_HEAD.match(type_statement(statement))
        if head:
            defined.add(head.group(1))
            if head.group(2):
                defined.update(names_of(head.group(2)))
    for t in TYPEALIAS.finditer(body):
        defined.update(names_of(t.group(1)))
    for m in IFACE_DEF.finditer(clean):
        facts.interfaces.add(m.group(1))
    for m in CALL.finditer(clean):
        name = m.group(1)
        if name in KEYWORDS or name.startswith("m4_"):
            continue
        facts.calls.append(Call(path, line_of(src, m.start()), name))
    return problems


def scan_fc(path: str, text: str, facts: Facts) -> None:
    src = strip_te(text)
    for number, line in enumerate(src.clean.split("\n"), 1):
        for m in FC_TYPE.finditer(line):
            facts.contexts.append((path, number, m.group(1)))


# --- CIL --------------------------------------------------------------------


@dataclass
class Node:
    kind: str  # list, atom, str
    value: str = ""
    line: int = 0
    end: int = 0
    items: list[Node] = field(default_factory=list)

    @property
    def head(self) -> str:
        if self.kind == "list" and self.items and self.items[0].kind == "atom":
            return self.items[0].value
        return ""


def parse_cil(text: str) -> tuple[list[Node], Source, str | None]:
    """The top-level forms, the comments and the first syntax error (or None)."""
    nodes: list[Node] = []
    stack: list[Node] = []
    comments: dict[int, str] = {}
    code: set[int] = set()
    error: str | None = None
    i, line, n = 0, 1, len(text)
    while i < n and error is None:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
        elif ch in " \t\r\f\v":
            i += 1
        elif ch == ";":
            j = text.find("\n", i)
            j = n if j < 0 else j
            comments[line] = text[i + 1 : j].rstrip("\r")
            i = j
        elif ch == "(":
            stack.append(Node("list", line=line))
            code.add(line)
            i += 1
        elif ch == ")":
            code.add(line)
            if not stack:
                error = f"line {line}: ')' with no '(' before it"
                break
            done = stack.pop()
            done.end = line
            (stack[-1].items if stack else nodes).append(done)
            i += 1
        elif ch == '"':
            start_line, j = line, i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                if text[j] == "\n":
                    line += 1
                j += 1
            if j >= n:
                error = f"line {start_line}: a string is not closed"
                break
            code.update(range(start_line, line + 1))
            node = Node("str", text[i + 1 : j], start_line, line)
            if stack:
                stack[-1].items.append(node)
            else:
                nodes.append(node)
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\f\v\n();"':
                j += 1
            code.add(line)
            node = Node("atom", text[i:j], line, line)
            (stack[-1].items if stack else nodes).append(node)
            i = j
    if error is None and stack:
        error = f"line {stack[-1].line}: '(' is not closed"
    return nodes, Source("", comments, code), error


def atoms_of(node: Node) -> set[str]:
    """Every atom under ``node`` (no recursion, so deep nesting is safe)."""
    found: set[str] = set()
    todo = [node]
    while todo:
        cur = todo.pop()
        if cur.kind == "atom":
            found.add(cur.value)
        todo.extend(cur.items)
    return found


def walk_cil(nodes: Sequence[Node]):
    """Yield (list node, inside optional, block path) for every list, parents first."""
    todo = [(n, False, ()) for n in reversed(nodes)]
    while todo:
        node, optional, blocks = todo.pop()
        if node.kind != "list":
            continue
        yield node, optional, blocks
        inner_optional = optional or node.head == "optional"
        inner_blocks = blocks
        if node.head in ("block", "macro") and len(node.items) > 1:
            inner_blocks = (*blocks, node.items[1].value)
        children = node.items
        if node.head == "macro" and len(children) > 2:
            children = [*children[:2], *children[3:]]  # the parameter list is not a body
        todo.extend((child, inner_optional, inner_blocks) for child in reversed(children))


def check_cil_rules(
    path: str, nodes: Sequence[Node], src: Source, offset: int = 0
) -> list[Problem]:
    problems: list[Problem] = []
    for node, _optional, _blocks in walk_cil(nodes):
        kind = node.head
        if kind == "typepermissive":
            problems.append(
                Problem(path, "permissive domain", node.line + offset, rule="permissive")
            )
            continue
        if kind not in ("allow", "classpermissionset"):
            continue
        parts = node.items[1:4] if kind == "allow" else node.items[2:]
        atoms: set[str] = set()
        for part in parts:
            atoms |= atoms_of(part)
        line = node.line + offset
        if atoms & CAPABILITY_CLASSES and "sys_admin" in atoms:
            problems.append(
                Problem(
                    path,
                    f"{kind} gives sys_admin (CAP_SYS_ADMIN is nearly root)",
                    line,
                    rule="sys-admin",
                )
            )
        if atoms & CIL_WILDCARDS and not is_explained(src, node.line, node.end):
            problems.append(
                Problem(
                    path,
                    f"{kind} uses all, * or not with no comment that explains it",
                    line,
                    rule="wildcard",
                )
            )
    return problems


def scan_cil(path: str, text: str, facts: Facts) -> list[Problem]:
    nodes, src, error = parse_cil(text)
    if error:
        return [Problem(path, f"CIL syntax: {error}", rule="syntax")]
    problems = check_cil_rules(path, nodes, src)
    key = module_key(path)
    defined = facts.types.setdefault(key, set())
    named_contexts: dict[str, str] = {}
    fileconds: list[Node] = []
    for node, optional, blocks in walk_cil(nodes):
        head = node.head
        if len(node.items) > 1 and node.items[1].kind == "atom":
            name = node.items[1].value
            if head in ("type", "typealias"):
                defined.add(name)
                defined.add(".".join((*blocks, name)))
            elif head == "macro":
                facts.macros.add(name)
            elif head == "context" and len(node.items) > 2:
                ctx_type = context_type(node.items[2])
                if ctx_type:
                    named_contexts[name] = ctx_type
            elif head == "call":
                facts.macro_calls.append(Call(path, node.line, name, optional))
        if head == "filecon":
            fileconds.append(node)
    for node in fileconds:
        ctx = node.items[3] if len(node.items) > 3 else None
        if ctx is None:
            continue
        ctx_type = named_contexts.get(ctx.value) if ctx.kind == "atom" else context_type(ctx)
        if ctx_type:
            facts.contexts.append((path, node.line, ctx_type))
    return problems


def context_type(node: Node) -> str:
    """The type of ``(user role type range)``, or an empty string."""
    if node.kind == "list" and len(node.items) >= 3 and node.items[2].kind == "atom":
        return node.items[2].value
    return ""


# --- markdown ---------------------------------------------------------------


def scan_markdown(path: str, text: str) -> list[Problem]:
    """The sys_admin and wildcard rules for the policy blocks of a markdown file."""
    problems: list[Problem] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = FENCE_OPEN.match(lines[i])
        if not m:
            i += 1
            continue
        fence, lang = m.group(1), m.group(2).lower()
        start = i + 1
        j = start
        while j < len(lines):
            stripped = lines[j].strip()
            if stripped.startswith(fence[0] * len(fence)) and not stripped.strip(fence[0]):
                break
            j += 1
        block = "\n".join(lines[start:j])
        problems += check_block(path, lang, block, offset=start)
        i = j + 1
    return problems


def check_block(path: str, lang: str, block: str, offset: int) -> list[Problem]:
    if lang == "cil" or (lang == "" and CIL_LIKE.search(block)):
        nodes, src, error = parse_cil(block)
        if error:
            return [
                Problem(
                    path,
                    f"a CIL block cannot be read, so its rules are not checked: {error}",
                    offset + 1,
                    warning=True,
                    rule="syntax",
                )
            ]
        return check_cil_rules(path, nodes, src, offset)
    if lang in TE_LANGS or (lang == "" and TE_LIKE.search(strip_te(block).clean)):
        return check_te_rules(path, strip_te(block), offset)
    return []


# --- resolving --------------------------------------------------------------


def is_external(name: str) -> bool:
    layer = name.split("_", 1)[0]
    return (
        (layer in POLICY_LAYERS and "_" in name)
        or name.endswith("_pattern")
        or name in SUPPORT_MACROS
    )


def resolve(facts: Facts) -> list[Problem]:
    problems: list[Problem] = []
    reported_missing: set[str] = set()
    for path, line, type_name in facts.contexts:
        key = module_key(path)
        if key not in facts.types:
            if path not in reported_missing:
                reported_missing.add(path)
                problems.append(
                    Problem(
                        path,
                        "no .te or .cil with the same name next to it: its types cannot be checked",
                        rule="fc-type",
                    )
                )
            continue
        known = facts.types[key] | facts.required.get(key, set())
        if type_name not in known and type_name.rsplit(".", 1)[-1] not in known:
            problems.append(
                Problem(
                    path,
                    f"type {type_name} is not defined in the .te or .cil of the module",
                    line,
                    rule="fc-type",
                )
            )
    seen: set[tuple[str, str]] = set()
    for call in facts.calls:
        if call.name in facts.interfaces or (call.path, call.name) in seen:
            continue
        seen.add((call.path, call.name))
        if is_external(call.name):
            message = (
                f"interface {call.name} is not defined in a tracked .if file; it is taken "
                "from selinux-policy and cannot be checked offline"
            )
            problems.append(Problem(call.path, message, call.line, True, "interface"))
        else:
            message = f"interface {call.name} is called but defined nowhere in the repo"
            problems.append(Problem(call.path, message, call.line, rule="interface"))
    seen.clear()
    for call in facts.macro_calls:
        bare = call.name.rsplit(".", 1)[-1]
        if bare in facts.macros or (call.path, call.name) in seen:
            continue
        seen.add((call.path, call.name))
        message = f"macro {call.name} is called but no (macro {bare} ...) is defined"
        problems.append(Problem(call.path, message, call.line, call.optional, "macro"))
    return problems


# --- files ------------------------------------------------------------------


def read_text(root: Path, rel: str) -> tuple[str | None, list[Problem]]:
    """The text of a file; a byte order mark is one clear error, then ignored."""
    try:
        text = (root / rel).read_bytes().decode("utf-8")
    except OSError as err:
        return None, [Problem(rel, f"cannot read the file: {err.strerror or err}", rule="file")]
    except UnicodeDecodeError:
        return None, [Problem(rel, "the file is not valid UTF-8", rule="file")]
    if text.startswith("\ufeff"):
        return text[1:], [Problem(rel, "the file starts with a byte order mark (BOM)", rule="file")]
    return text, []


def mark_legacy(problems: Sequence[Problem]) -> list[Problem]:
    out = []
    for p in problems:
        if not p.warning and (p.path, p.rule) in LEGACY:
            p = Problem(p.path, p.message, p.line, True, p.rule, True)
        out.append(p)
    return out


def check_files(root: Path, paths: Sequence[str]) -> list[Problem]:
    """Check the given files (paths relative to ``root`` or absolute); sorted result."""
    problems: list[Problem] = []
    facts = Facts()
    for rel in sorted(set(paths)):
        if rel.endswith(".md"):
            try:
                text = (root / rel).read_bytes().decode("utf-8", errors="replace")
            except OSError as err:
                problems.append(Problem(rel, f"cannot read the file: {err.strerror or err}"))
                continue
            problems += scan_markdown(rel, text.removeprefix("\ufeff"))
            continue
        text, found = read_text(root, rel)
        problems += found
        if text is None:
            continue
        if rel.endswith((".te", ".if")):
            problems += scan_te(rel, text, facts)
        elif rel.endswith(".fc"):
            scan_fc(rel, text, facts)
        elif rel.endswith(".cil"):
            problems += scan_cil(rel, text, facts)
    problems += resolve(facts)
    unique = set(mark_legacy(problems))
    return sorted(unique, key=lambda p: (p.path, p.line, p.warning, p.message))


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *SCAN_ROOTS],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        errors="surrogateescape",
    ).stdout
    found = [p for p in out.split("\0") if p and p.endswith(SUFFIXES)]
    return sorted(p for p in found if p.endswith(POLICY_SUFFIXES) or p.startswith("docs/"))


def main(argv: list[str] | None = None) -> int:
    try:
        args = sys.argv[1:] if argv is None else argv
        usage = "usage: python3 tools/check_selinux.py [FILE.te | .fc | .if | .cil | .md ...]"
        if any(a.startswith("-") for a in args):
            print(usage, file=sys.stderr)
            return 0 if args in (["-h"], ["--help"]) else 2
        if any(not a.endswith(SUFFIXES) for a in args):
            print(f"every file must be a .te, .fc, .if, .cil or .md file\n{usage}", file=sys.stderr)
            return 2
        if args:
            paths = args
        else:
            try:
                paths = tracked_files(ROOT)
            except (OSError, subprocess.CalledProcessError) as err:
                print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
                return 2
        if not paths:
            print("no tracked SELinux policy files found", file=sys.stderr)
            return 2
        problems = check_files(Path.cwd() if args else ROOT, paths)
        for problem in problems:
            print(problem, file=sys.stderr)
        errors = sum(not p.warning for p in problems)
        print(f"{len(paths)} files checked: {errors} errors, {len(problems) - errors} warnings")
        return 1 if errors else 0
    except (OSError, ValueError) as err:  # no traceback for a bad name, argument or bytes
        print(f"{os.path.basename(__file__)}: cannot check: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # A file name that is not UTF-8 is printed escaped, not as a traceback.
    sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    sys.exit(main())
