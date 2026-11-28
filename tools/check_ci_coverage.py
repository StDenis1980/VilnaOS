#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check that every check and every test of the repo is run by a CI workflow.

The workflows (``.github/workflows/*.yml``) are read as text with a small
YAML reader of our own (no PyYAML): jobs, ``defaults.run.working-directory``,
steps with ``working-directory`` and ``run:`` (plain, quoted, ``|`` and ``>``).
Every ``run`` is split into commands (``&&``, ``;``, ``|``, ``cd``, ``sudo``,
``uv run``, ``timeout`` ... are understood) and nothing is executed. The
checker verifies, for the tracked files of the repo:

* every ``tools/check_*.py`` and ``<dir>/tools/check_*.py`` is run by at least
  one step (``python3 tools/check_x.py``, ``uv run python ...``, ``./x.py``);
* every ``test_*.py`` / ``*_test.py`` is run: ``python3 tools/test_x.py``,
  ``python3 -m unittest ...`` or a ``pytest`` step that collects it (the
  working directory of the step, the paths on the command line, ``testpaths``
  of the project's ``pyproject.toml`` and ``--ignore`` are taken into account);
* every folder with a ``pyproject.toml`` that has tests has a step with that
  ``working-directory`` (or a ``cd`` into it) that runs ``pytest``; a folder
  with a ``pyproject.toml`` and no tests is a warning;
* every Cargo workspace (and every crate outside a workspace) has a step that
  runs ``cargo test`` for the whole workspace there (``-p``, ``--lib``,
  ``--no-run`` and the like do not count);
* a script in ``tools/`` that is not a check (a builder, a helper) and is not
  run by CI is listed in ``NOT_A_CHECK`` below with the reason; an entry
  whose file does not exist is an error, an entry for a script CI runs is a
  warning (delete it);
* a ``run`` that names a file that does not exist (a script, a test path, a
  glob that matches nothing, or an argument with a source-file suffix inside a
  tracked folder) is an error. Words with ``$`` or ``${{ }}`` are not checked.

A file run only by a workflow that has neither ``push`` nor ``pull_request``
(only ``schedule`` or ``workflow_dispatch``) is a warning: it is not run for a
change. Known old violations are warnings too (see ``LEGACY``). A tool that is
new and not in ``ci.yml`` yet is an error that names the file and says what to
add.

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but
do not fail the check. Only ``git ls-files`` is run; nothing else is executed,
nothing is read from the network and nothing is written.

Run: python3 tools/check_ci_coverage.py
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ".github/workflows"


def wordset(text: str) -> frozenset[str]:
    """The words of ``text`` as a set."""
    return frozenset(text.split())


UNKNOWN = "?"  # a working directory we cannot work out (a variable, a matrix value)

# Scripts of tools/ that are not checks and that CI does not run itself. A script
# is not a check when its name does not start with check_ or test_; if it is
# really a check, rename it and call it from ci.yml instead of listing it here.
NOT_A_CHECK: dict[str, str] = {
    "tools/a11y_audit.py": (
        "a helper: the live tests of herkos (herkos/tests/a11y_tour.py) start it as a process "
        "of their own to read the AT-SPI tree; it needs a desktop session, so it is not a check"
    ),
    "tools/quality_report.py": (
        "a report for people over the checks; ci.yml runs each check itself, and "
        "tools/test_quality_report.py tests the report"
    ),
    "tools/vuln_match.py": (
        "it needs an OSV file downloaded separately (CI has no feed by design); "
        "tools/test_vuln_match.py tests it on made-up data"
    ),
    "tools/build_site.py": (
        "the builder of site/public; tools/check_site.py (run by ci.yml) calls "
        "build_site.main(['', '--check']), so the build is checked there"
    ),
}

# Problems the repo already has today: (path from the repo root, rule). They are
# warnings, so that the check can be switched on in CI; fix them, then delete the line.
LEGACY: frozenset[tuple[str, str]] = frozenset()

TEST_FILE = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$")
CHECK_FILE = re.compile(r"(?:^|/)tools/check_[^/]+\.py$")
LOOSE_TOOL = re.compile(r"^tools/(?!check_|test_)[^/]+\.py$")
PYTHON = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1(?!<)")
OPERATOR_PARTS = re.compile(r"\(|\)|[;|&]+|[<>][<>&|]*")
SHELL_KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "time"})
SHELLS = frozenset({"sh", "bash", "dash", "zsh"})
# Commands that make or move files: their arguments need not exist yet.
WRITERS = wordset("""
    cp mv mkdir touch rm ln install tar curl wget unzip bunzip2 tee echo printf cat sha256sum
    chmod chown git gh rsync zip gzip bzip2 xz mktemp test [
    """)
# Folders that steps fill with build output: a path through one of them need not be tracked.
BUILD_FOLDERS = wordset("target build dist out node_modules .venv venv")
SOURCE_SUFFIXES = wordset("""
    .py .sh .js .toml .yaml .yml .spec .desktop .service .xml .md .policy .rules .cfg
    """)
PARTIAL_CARGO = wordset("""
    -p --package --lib --bin --bins --test --tests --doc --example --examples --bench --benches
    --no-run
    """)
PYTEST_VALUE_OPTIONS = wordset("""
    -k -m -p -c -o -W -n -r --rootdir --confcutdir --maxfail --tb --junitxml --junit-xml
    --durations --durations-min --dist --cov --cov-report --basetemp --import-mode --log-level
    --override-ini --deselect --capture --color --timeout --log-file
    """)
NODE_VALUE_OPTIONS = wordset("""
    -r --require --import -e --eval -p --print --loader --test-name-pattern
    """)
WRAPPER_VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "sudo": wordset("-u -g -D -C -h -p -r -t -U"),
    "env": wordset("-u -C -S"),
    "timeout": wordset("-s -k --signal --kill-after"),
    "nice": frozenset({"-n"}),
    "xvfb-run": wordset("-s --server-args -n --server-num -e -f -w"),
    "uv-run": wordset("""--python -p --with --extra --group --project --directory --env-file
        --with-requirements --package --index --index-url"""),
    "coverage": wordset("--rcfile --include --omit --source --data-file"),
}


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False
    rule: str = ""

    def __str__(self) -> str:
        kind = "warning: " if self.warning else ""
        return f"{self.path}: {kind}{self.message}"


def problem(path: str, rule: str, message: str, warning: bool = False) -> Problem:
    """A problem; a known old one (see LEGACY) is only a warning."""
    return Problem(path, message, warning or (path, rule) in LEGACY, rule)


# ---------------------------------------------------------------------------
# A small YAML reader: block maps and lists, plain and quoted scalars, flow
# lists, ``|`` and ``>`` blocks. Anchors, aliases and tags are refused.
# ---------------------------------------------------------------------------


class YamlError(ValueError):
    """The text is not YAML that this reader understands."""


class Scalar(str):
    """A string of the document."""


class Map(dict):  # type: ignore[type-arg]
    """A mapping of the document; ``line`` is the line of its first key."""

    line: int = 0


class Seq(list):  # type: ignore[type-arg]
    """A list of the document."""

    line: int = 0


KEY = re.compile(
    r"""^(?:"(?P<dq>(?:[^"\\]|\\.)*)"|'(?P<sq>(?:[^']|'')*)'"""
    r"""|(?P<plain>[^\s'"\[\]{}#&*!|>%@`,][^\n]*?))\s*:(?:\s+(?P<rest>.*))?$"""
)
BLOCK_HEADER = re.compile(r"^[|>][+-]?[1-9]?[+-]?(?:\s+#.*)?$")
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "/": "/", " ": " "}


def cut_comment(text: str) -> str:
    """Drop `` # comment`` from a plain value (quotes are not special in plain text)."""
    match = re.search(r"\s#", text)
    return text[: match.start()].rstrip() if match else text.rstrip()


def closing_quote(text: str, quote: str) -> int | None:
    """Index of the quote that ends a quoted scalar whose text starts at 0, or None."""
    i = 0
    while i < len(text):
        char = text[i]
        if quote == '"' and char == "\\":
            i += 2
            continue
        if char == quote:
            if quote == "'" and text[i + 1 : i + 2] == "'":
                i += 2
                continue
            return i
        i += 1
    return None


def unquote(text: str, quote: str) -> str:
    if quote == "'":
        return text.replace("''", "'")
    return re.sub(r"\\(.)", lambda m: ESCAPES.get(m.group(1), "\\" + m.group(1)), text)


def flow_clean(text: str) -> str:
    """Drop a trailing comment from a line of a flow collection (quote-aware)."""
    quote = ""
    for i, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#" and (i == 0 or text[i - 1].isspace()):
            return text[:i].rstrip()
    return text.rstrip()


def flow_depth(text: str) -> int:
    depth, quote = 0, ""
    for char in text:
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
    return depth


def flow_value(text: str, i: int) -> tuple[object, int]:
    """Read one value of a flow collection starting at ``i``; returns it and the next index."""
    while text[i].isspace():
        i += 1
    if text[i] == "[":
        seq = Seq()
        i += 1
        while True:
            while text[i].isspace() or text[i] == ",":
                i += 1
            if text[i] == "]":
                return seq, i + 1
            item, i = flow_value(text, i)
            seq.append(item)
    if text[i] == "{":
        mapping = Map()
        i += 1
        while True:
            while text[i].isspace() or text[i] == ",":
                i += 1
            if text[i] == "}":
                return mapping, i + 1
            key, i = flow_scalar(text, i, ":,}")
            while text[i].isspace():
                i += 1
            value: object = Scalar("")
            if text[i] == ":":
                value, i = flow_value(text, i + 1)
            mapping[str(key)] = value
    return flow_scalar(text, i, ",]}")


def flow_scalar(text: str, i: int, stops: str) -> tuple[Scalar, int]:
    if text[i] in "\"'":
        quote = text[i]
        end = closing_quote(text[i + 1 :], quote)
        if end is None:
            raise YamlError("unterminated quoted text in a flow collection")
        return Scalar(unquote(text[i + 1 : i + 1 + end], quote)), i + end + 2
    start = i
    while i < len(text) and text[i] not in stops:
        i += 1
    return Scalar(text[start:i].strip()), i


class Parser:
    """Reads the YAML subset into ``Map``, ``Seq`` and ``Scalar``."""

    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()
        self.pos = 0

    def significant(self) -> tuple[int, int, str] | None:
        """(line number, indent, text) of the next line that is not blank or a comment."""
        while self.pos < len(self.lines):
            raw = self.lines[self.pos]
            text = raw.strip()
            if not text or text.startswith("#") or (text in ("---", "...") and raw[0] != " "):
                self.pos += 1
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if raw[indent : indent + 1] == "\t":
                raise YamlError(f"line {self.pos + 1}: a tab is used for indentation")
            return self.pos + 1, indent, text
        return None

    def document(self) -> object:
        value = self.block(0)
        left = self.significant()
        if left is not None:
            raise YamlError(f"line {left[0]}: unexpected text {left[2]!r}")
        return value

    def block(self, min_indent: int) -> object:
        tok = self.significant()
        if tok is None or tok[1] < min_indent:
            return Scalar("")
        line, indent, text = tok
        if text == "-" or text.startswith("- "):
            return self.sequence(indent)
        if KEY.match(text):
            return self.mapping(indent)
        self.pos += 1
        return self.value(text, line, indent - 1)

    def sequence(self, indent: int) -> Seq:
        result = Seq()
        result.line = self.pos + 1
        while True:
            tok = self.significant()
            if tok is None or tok[1] != indent or not (tok[2] == "-" or tok[2].startswith("- ")):
                return result
            text = tok[2]
            after = text[1:].lstrip(" ")
            if not after or after.startswith("#"):
                self.pos += 1
                nxt = self.significant()
                result.append(self.block(indent + 1) if nxt and nxt[1] > indent else Scalar(""))
            else:
                column = indent + len(text) - len(after)
                self.lines[self.pos] = " " * column + after
                result.append(self.block(indent + 1))

    def mapping(self, indent: int) -> Map:
        result = Map()
        result.line = self.pos + 1
        while True:
            tok = self.significant()
            if tok is None or tok[1] < indent:
                return result
            line, here, text = tok
            if here > indent:
                raise YamlError(f"line {line}: wrong indentation")
            if text == "-" or text.startswith("- "):
                return result
            match = KEY.match(text)
            if not match:
                raise YamlError(f"line {line}: expected 'key: value', found {text!r}")
            if match.group("dq") is not None:
                key = unquote(match.group("dq"), '"')
            elif match.group("sq") is not None:
                key = unquote(match.group("sq"), "'")
            else:
                key = match.group("plain").strip()
            if key in result:
                raise YamlError(f"line {line}: the key {key!r} is repeated")
            self.pos += 1
            rest = (match.group("rest") or "").strip()
            if rest.startswith("#"):
                rest = ""
            if rest:
                result[key] = self.value(rest, line, indent)
                continue
            nxt = self.significant()
            if nxt and nxt[1] > indent:
                result[key] = self.block(indent + 1)
            elif nxt and nxt[1] == indent and (nxt[2] == "-" or nxt[2].startswith("- ")):
                result[key] = self.sequence(indent)
            else:
                result[key] = Scalar("")

    def value(self, text: str, line: int, parent: int) -> object:
        """A scalar or flow value that starts on the current line; ``parent`` is its indent."""
        if text[0] in "&*!":
            raise YamlError(f"line {line}: anchors, aliases and tags are not supported")
        if BLOCK_HEADER.match(text):
            return self.block_scalar(text.split()[0], parent)
        if text[0] in "[{":
            return self.flow(text, line)
        if text[0] in "\"'":
            return self.quoted(text, line)
        parts = [cut_comment(text)]
        while True:
            tok = self.significant()
            if tok is None or tok[1] <= parent:
                return Scalar(" ".join(parts))
            parts.append(cut_comment(tok[2]))
            self.pos += 1

    def quoted(self, text: str, line: int) -> Scalar:
        quote, buffer, pieces = text[0], text[1:], []
        while True:
            end = closing_quote(buffer, quote)
            if end is not None:
                pieces.append(buffer[:end])
                tail = buffer[end + 1 :].strip()
                break
            pieces.append(buffer)
            if self.pos >= len(self.lines):
                raise YamlError(f"line {line}: unterminated quoted text")
            buffer = self.lines[self.pos].strip()
            self.pos += 1
        if tail and not tail.startswith("#"):
            raise YamlError(f"line {line}: text after a quoted value")
        return Scalar(unquote(" ".join(pieces), quote))

    def flow(self, text: str, line: int) -> object:
        text = flow_clean(text)
        while flow_depth(text) > 0:
            if self.pos >= len(self.lines):
                raise YamlError(f"line {line}: unterminated [ or {{")
            text += " " + flow_clean(self.lines[self.pos].strip())
            self.pos += 1
        try:
            value, end = flow_value(text, 0)
        except IndexError:
            raise YamlError(f"line {line}: cannot read the list or mapping {text!r}") from None
        if text[end:].strip():
            raise YamlError(f"line {line}: text after a list or mapping")
        return value

    def block_scalar(self, header: str, parent: int) -> Scalar:
        """The lines of a ``|`` or ``>`` block that follow the current line."""
        digit = re.search(r"[1-9]", header)
        block_indent = parent + int(digit.group()) if digit else None
        collected: list[str] = []
        while self.pos < len(self.lines):
            raw = self.lines[self.pos]
            if not raw.strip():
                collected.append("")
                self.pos += 1
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if block_indent is None:
                if indent <= parent:
                    break
                block_indent = indent
            if indent < block_indent:
                break
            collected.append(raw[block_indent:])
            self.pos += 1
        while collected and collected[-1] == "":
            collected.pop()
        if header[0] == "|":
            body = "\n".join(collected)
        else:
            body, previous_text = "", False
            for item in collected:
                if item == "":
                    body += "\n"
                    previous_text = False
                else:
                    body += (" " if previous_text else "") + item
                    previous_text = True
        return Scalar(body + ("\n" if body and "-" not in header else ""))


def parse_yaml(text: str) -> object:
    return Parser(text).document()


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


@dataclass
class Step:
    label: str
    line: int
    run: str | None
    working_directory: str | None


@dataclass
class Job:
    id: str
    working_directory: str | None
    steps: list[Step] = field(default_factory=list)


@dataclass
class Workflow:
    path: str
    triggers: frozenset[str]
    jobs: list[Job]

    @property
    def for_changes(self) -> bool:
        """True when the workflow runs on a push or a pull request."""
        return bool({"push", "pull_request", "pull_request_target"} & self.triggers)


def as_map(value: object) -> Map:
    return value if isinstance(value, Map) else Map()


def text_of(value: object) -> str | None:
    return str(value) if isinstance(value, Scalar) else None


def trigger_names(value: object) -> frozenset[str]:
    if isinstance(value, Map):
        return frozenset(value)
    if isinstance(value, Seq):
        return frozenset(str(item) for item in value)
    if isinstance(value, Scalar) and value:
        return frozenset({str(value)})
    return frozenset()


def default_directory(node: Map) -> str | None:
    return text_of(as_map(as_map(node.get("defaults")).get("run")).get("working-directory"))


def step_label(step: Map, index: int) -> str:
    name = text_of(step.get("name"))
    if name:
        return name
    run = text_of(step.get("run")) or text_of(step.get("uses")) or ""
    first = run.strip().splitlines()[0] if run.strip() else ""
    return first[:50] if first else f"#{index}"


def read_workflow(path: str, text: str) -> Workflow:
    """The jobs and steps of one workflow file (raises YamlError)."""
    document = as_map(parse_yaml(text))
    workflow_dir = default_directory(document)
    jobs = []
    for job_id, node in as_map(document.get("jobs")).items():
        job_map = as_map(node)
        job = Job(str(job_id), default_directory(job_map) or workflow_dir)
        steps = job_map.get("steps")
        for index, item in enumerate(steps if isinstance(steps, Seq) else [], start=1):
            step = as_map(item)
            job.steps.append(
                Step(
                    step_label(step, index),
                    step.line,
                    text_of(step.get("run")),
                    text_of(step.get("working-directory")),
                )
            )
        jobs.append(job)
    return Workflow(path, trigger_names(document.get("on")), jobs)


# ---------------------------------------------------------------------------
# Shell commands of a ``run``
# ---------------------------------------------------------------------------


def logical_lines(script: str) -> list[str]:
    """The lines of a script: continuations joined, here-documents and comments dropped."""
    result: list[str] = []
    pending = ""
    delimiter = ""
    for raw in script.splitlines():
        if delimiter:
            if raw.strip() == delimiter:
                delimiter = ""
            continue
        line = pending + raw.strip()
        if line.endswith("\\"):
            pending = line[:-1] + " "
            continue
        pending = ""
        match = HEREDOC.search(line)
        if match:
            delimiter = match.group(2)
        result.append(line)
    if pending:
        result.append(pending)
    return result


def cut_shell_comment(line: str) -> str:
    quote = ""
    for i, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def tokenize(line: str) -> list[str]:
    line = cut_shell_comment(line)
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return line.split()


def shell_events(line: str) -> list[tuple[str, list[str]]]:
    """The line as ``("cmd", words)``, ``("push", [])`` and ``("pop", [])`` events."""
    events: list[tuple[str, list[str]]] = []
    words: list[str] = []
    skip_next = False

    def flush() -> None:
        nonlocal words
        if words:
            events.append(("cmd", words))
        words = []

    for token in tokenize(line):
        if set(token) <= set("();<>|&"):
            for part in OPERATOR_PARTS.findall(token):
                if part == "(":
                    flush()
                    events.append(("push", []))
                elif part == ")":
                    flush()
                    events.append(("pop", []))
                elif part[0] in "<>":
                    skip_next = True
                else:
                    flush()
            continue
        if skip_next:
            skip_next = False
            continue
        words.append(token)
    flush()
    return events


def skip_options(words: list[str], start: int, value_options: frozenset[str]) -> int:
    """Index of the first word at or after ``start`` that is not an option or its value."""
    i = start
    while i < len(words):
        word = words[i]
        if word == "--":
            return i + 1
        if not word.startswith("-") or word == "-":
            return i
        i += 2 if word in value_options else 1
    return i


def unwrap(words: list[str]) -> list[str]:
    """The command itself, without VAR=x, sudo, env, timeout, ``uv run`` and the like."""
    current = list(words)
    for _ in range(8):
        while current and (ENV_ASSIGN.match(current[0]) or current[0] in SHELL_KEYWORDS):
            current = current[1:]
        if not current:
            return current
        head = current[0]
        options = WRAPPER_VALUE_OPTIONS
        if head == "env":
            i = skip_options(current, 1, options["env"])
            while i < len(current) and ENV_ASSIGN.match(current[i]):
                i += 1
            current = current[i:]
        elif head in ("sudo", "nice", "xvfb-run"):
            current = current[skip_options(current, 1, options[head]) :]
        elif head == "timeout":
            current = current[skip_options(current, 1, options["timeout"]) + 1 :]
        elif head in ("nohup", "exec"):
            current = current[1:]
        elif head == "uv" and current[1:2] == ["run"]:
            current = current[skip_options(current, 2, options["uv-run"]) :]
        elif head == "coverage" and current[1:2] == ["run"]:
            current = current[skip_options(current, 2, options["coverage"]) :]
        else:
            return current
    return current


def join_path(base: str, relative: str) -> str | None:
    """``relative`` taken from the repo folder ``base``; None when it leaves the repo."""
    joined = posixpath.normpath(posixpath.join(base or ".", relative))
    if joined == ".":
        return ""
    return None if joined == ".." or joined.startswith("../") or joined.startswith("/") else joined


def is_script_path(word: str) -> bool:
    """A command word that names a script of the repo (a program of a build is not one)."""
    return (
        "/" in word
        and not word.startswith("/")
        and not has_variable(word)
        and posixpath.splitext(word)[1] in SOURCE_SUFFIXES
    )


def has_variable(word: str) -> bool:
    return any(mark in word for mark in ("$", "{{", "`", "~"))


def under(path: str, folder: str) -> bool:
    return folder == "" or path == folder or path.startswith(folder + "/")


def glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        elif pattern[i] == "[" and "]" in pattern[i:]:
            end = pattern.index("]", i)
            out += "[" + pattern[i + 1 : end].replace("\\", "\\\\") + "]"
            i = end + 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out)


def is_glob(word: str) -> bool:
    return any(char in word for char in "*?[")


def nearest_folder(path: str, folders: set[str]) -> str | None:
    """The innermost folder of ``folders`` that holds ``path`` (``""`` is the repo root)."""
    current = posixpath.dirname(path)
    while True:
        if current in folders:
            return current
        parent = posixpath.dirname(current)
        if parent == current:
            return None
        current = parent


# ---------------------------------------------------------------------------
# What the steps run
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    """What the run steps of the workflows do, as the checker understands them."""

    executed: dict[str, set[str]] = field(default_factory=dict)  # file -> workflow paths
    pytest_folders: dict[str, set[str]] = field(default_factory=dict)  # working dir -> workflows
    cargo_folders: dict[str, set[str]] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)
    steps: int = 0


class Analyzer:
    def __init__(self, root: Path, tracked: list[str]) -> None:
        self.root = root
        self.files = frozenset(tracked)
        self.folders = frozenset({"", *(p for f in tracked for p in parents_of(f))})
        self.top = frozenset(f.split("/")[0] for f in tracked if "/" in f)
        self.test_files = sorted(f for f in tracked if TEST_FILE.search(f))
        self.projects = {
            posixpath.dirname(f) for f in tracked if posixpath.basename(f) == "pyproject.toml"
        }
        self.usage = Usage()
        self.testpaths_cache: dict[str, list[str] | None] = {}
        self.workflow = ""
        self.where = ""

    # --- bookkeeping -------------------------------------------------------

    def error(self, rule: str, message: str) -> None:
        self.usage.problems.append(problem(self.workflow, rule, f"{self.where}: {message}"))

    def warn(self, rule: str, message: str) -> None:
        self.usage.problems.append(problem(self.workflow, rule, f"{self.where}: {message}", True))

    def mark_run(self, path: str) -> None:
        self.usage.executed.setdefault(path, set()).add(self.workflow)

    # --- paths ---------------------------------------------------------------

    def resolve(self, word: str, cwd: str, what: str) -> list[str]:
        """The tracked files or folders ``word`` names; reports a missing one as an error."""
        if not word or has_variable(word) or "://" in word or word.startswith("/"):
            return []
        target = join_path(cwd, word)
        if target is None:
            return []
        if is_glob(target):
            regex = glob_regex(target)
            found = sorted(p for p in self.files | self.folders if p and regex.fullmatch(p))
            if not found:
                self.error("missing-file", f"{what} '{word}' matches no tracked file")
            return found
        if target in self.files or target in self.folders:
            return [target]
        if BUILD_FOLDERS & set(target.split("/")):
            return []  # made by an earlier step (a build output), not a tracked file
        self.error("missing-file", f"{what} '{word}' does not exist (not a tracked file)")
        return []

    # --- commands --------------------------------------------------------------

    def run_script(self, run: str, start: str) -> None:
        cwd = start
        stack: list[str] = []
        for line in logical_lines(run):
            for kind, words in shell_events(line):
                if kind == "push":
                    stack.append(cwd)
                elif kind == "pop":
                    cwd = stack.pop() if stack else cwd
                else:
                    cwd = self.command(words, cwd)

    def command(self, words: list[str], cwd: str) -> str:
        """Look at one command; returns the working directory for the next one."""
        words = unwrap(words)
        if not words:
            return cwd
        head, args = words[0], words[1:]
        if head == "cd":
            return self.change_directory(args, cwd)
        if cwd == UNKNOWN:
            return cwd
        if PYTHON.match(head):
            self.python(args, cwd)
        elif head in ("pytest", "py.test"):
            self.pytest(args, cwd)
        elif head in SHELLS:
            self.shell_script(args, cwd)
        elif head == "node":
            self.node(args, cwd)
        elif head == "cargo":
            self.cargo(args, cwd)
        elif is_script_path(head):
            for path in self.resolve(head, cwd, "script"):
                self.mark_run(path)
            self.generic(args, cwd)
        elif head not in WRITERS:
            self.generic(args, cwd)
        return cwd

    def change_directory(self, args: list[str], cwd: str) -> str:
        args = [a for a in args if a != "--"]
        if cwd == UNKNOWN or not args or has_variable(args[0]) or args[0].startswith("/"):
            return UNKNOWN
        target = join_path(cwd, args[0])
        return UNKNOWN if target is None else target

    def generic(self, args: list[str], cwd: str) -> None:
        """Arguments that look like a source file of a tracked folder must exist."""
        for arg in args:
            if arg.startswith("-"):
                if "=" not in arg:
                    continue
                arg = arg.split("=", 1)[1]
            if "/" not in arg or has_variable(arg) or "://" in arg or arg.startswith("/"):
                continue
            if posixpath.splitext(arg)[1] not in SOURCE_SUFFIXES:
                continue
            target = join_path(cwd, arg)
            if target is None or target.split("/")[0] not in self.top or is_glob(target):
                continue
            if target not in self.files:
                self.error("missing-file", f"file '{arg}' does not exist (not a tracked file)")

    def python(self, args: list[str], cwd: str) -> None:
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "-m" and i + 1 < len(args):
                self.python_module(args[i + 1], args[i + 2 :], cwd)
                return
            if arg in ("-c", "-"):
                return
            if arg in ("-W", "-X", "-Q"):
                i += 2
            elif arg.startswith("-"):
                i += 1
            else:
                for path in self.resolve(arg, cwd, "script"):
                    if path in self.files:
                        self.mark_run(path)
                self.generic(args[i + 1 :], cwd)
                return

    def python_module(self, module: str, rest: list[str], cwd: str) -> None:
        if module == "pytest":
            self.pytest(rest, cwd)
        elif module == "unittest":
            self.unittest(rest, cwd)
        elif not has_variable(module):
            target = join_path(cwd, module.replace(".", "/") + ".py")
            if target in self.files:
                self.mark_run(target)

    def unittest(self, args: list[str], cwd: str) -> None:
        if args[:1] == ["discover"] or not args:
            start, pattern = ".", "test*.py"
            rest = args[1:]
            for i, arg in enumerate(rest):
                if arg in ("-s", "--start-directory") and i + 1 < len(rest):
                    start = rest[i + 1]
                elif arg in ("-p", "--pattern") and i + 1 < len(rest):
                    pattern = rest[i + 1]
            folder = join_path(cwd, start)
            if folder is not None and not has_variable(start) and folder in self.folders:
                for path in self.files:
                    in_folder = under(path, folder) and fnmatch.fnmatchcase(
                        posixpath.basename(path), pattern
                    )
                    if in_folder:
                        self.mark_run(path)
            return
        for arg in args:
            if arg.startswith("-") or has_variable(arg):
                continue
            name = arg if arg.endswith(".py") else arg.replace(".", "/") + ".py"
            target = join_path(cwd, name)
            if target is not None and target in self.files:
                self.mark_run(target)
            elif "/" in arg or arg.endswith(".py"):
                self.error("missing-file", f"test '{arg}' does not exist (not a tracked file)")

    def shell_script(self, args: list[str], cwd: str) -> None:
        for i, arg in enumerate(args):
            if arg == "-c":
                return
            if not arg.startswith("-"):
                self.resolve(arg, cwd, "script")
                self.generic(args[i + 1 :], cwd)
                return

    def node(self, args: list[str], cwd: str) -> None:
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in ("-e", "--eval", "-p", "--print"):
                return
            if arg in NODE_VALUE_OPTIONS:
                i += 2
                continue
            if not arg.startswith("-"):
                for path in self.resolve(arg, cwd, "script"):
                    self.mark_run(path)
            i += 1

    def cargo(self, args: list[str], cwd: str) -> None:
        rest = [a for a in args if not a.startswith("+")]
        if rest[:1] not in (["test"], ["t"]) and rest[:2] != ["nextest", "run"]:
            return
        words = rest[1:]
        if "--" in words:
            words = words[: words.index("--")]
        if any(
            w in PARTIAL_CARGO or w.startswith(("--package=", "--bin=", "--test=")) for w in words
        ):
            return
        folder = cwd
        for i, word in enumerate(words):
            manifest = ""
            if word == "--manifest-path" and i + 1 < len(words):
                manifest = words[i + 1]
            elif word.startswith("--manifest-path="):
                manifest = word.split("=", 1)[1]
            if manifest:
                if has_variable(manifest):
                    return
                target = join_path(cwd, manifest)
                if target is None:
                    return
                folder = posixpath.dirname(target)
        self.usage.cargo_folders.setdefault(folder, set()).add(self.workflow)

    # --- pytest ------------------------------------------------------------------

    def testpaths(self, folder: str) -> list[str]:
        """What ``pytest`` without paths collects when run in ``folder``."""
        if folder not in self.projects:
            return [folder]
        if folder not in self.testpaths_cache:
            self.testpaths_cache[folder] = self.read_testpaths(folder)
        paths = self.testpaths_cache[folder]
        return [join_path(folder, p) or "" for p in paths] if paths else [folder]

    def read_testpaths(self, folder: str) -> list[str] | None:
        path = self.root / folder / "pyproject.toml"
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        value = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("testpaths")
        if isinstance(value, str):
            return value.split()
        if isinstance(value, list):
            return [str(v) for v in value]
        return None

    def pytest(self, args: list[str], cwd: str) -> None:
        paths: list[str] = []
        ignores: list[str] = []
        collect_only = False
        after_options = False
        i = 0
        while i < len(args):
            arg = args[i]
            if after_options or not arg.startswith("-"):
                name = arg.split("::", 1)[0]
                paths.extend(self.resolve(name, cwd, "test path"))
            elif arg == "--":
                after_options = True
            elif arg in ("--collect-only", "--co"):
                collect_only = True
            elif arg.startswith(("--ignore=", "--ignore-glob=")):
                ignores.append(arg.split("=", 1)[1])
            elif arg in ("--ignore", "--ignore-glob") and i + 1 < len(args):
                ignores.append(args[i + 1])
                i += 1
            elif arg in PYTEST_VALUE_OPTIONS:
                i += 1
            i += 1
        if collect_only:
            return
        self.usage.pytest_folders.setdefault(cwd, set()).add(self.workflow)
        roots = paths or self.testpaths(cwd)
        skipped = [
            p
            for p in (join_path(cwd, ig) for ig in ignores if not has_variable(ig))
            if p is not None
        ]
        for path in self.test_files:
            if any(under(path, r) for r in roots) and not any(under(path, s) for s in skipped):
                self.mark_run(path)

    # --- steps -----------------------------------------------------------------------

    def step(self, workflow: Workflow, job: Job, step: Step) -> None:
        if step.run is None:
            return
        self.usage.steps += 1
        self.workflow = workflow.path
        self.where = f"job '{job.id}', step '{step.label}' (line {step.line})"
        directory = (
            step.working_directory if step.working_directory is not None else job.working_directory
        )
        if directory is None or directory in ("", "."):
            start: str | None = ""
        elif has_variable(directory):
            start = None
        else:
            start = join_path("", directory)
        if start is None:
            self.warn(
                "unknown-directory",
                f"cannot tell the working directory '{directory}'; the step is skipped",
            )
            return
        if start and start not in self.folders:
            self.error("missing-file", f"working-directory '{directory}' does not exist")
            return
        self.run_script(step.run, start)


def parents_of(path: str) -> list[str]:
    """The folders that contain ``path``: ``a/b/c.py`` gives ``a`` and ``a/b``."""
    parts = path.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def load_workflows(root: Path, tracked: list[str]) -> tuple[list[Workflow], list[Problem]]:
    workflows: list[Workflow] = []
    problems: list[Problem] = []
    paths = sorted(
        p for p in tracked if posixpath.dirname(p) == WORKFLOWS and p.endswith((".yml", ".yaml"))
    )
    for path in paths:
        try:
            text = (root / path).read_text(encoding="utf-8")
            workflows.append(read_workflow(path, text))
        except (OSError, UnicodeDecodeError, YamlError) as err:
            problems.append(problem(path, "yaml", f"cannot read the workflow: {err}"))
    return workflows, problems


def cargo_roots(root: Path, tracked: list[str]) -> tuple[list[str], list[Problem]]:
    """Folders of Cargo workspaces and of crates that are not in a workspace."""
    manifests = sorted(p for p in tracked if posixpath.basename(p) == "Cargo.toml")
    parsed: dict[str, dict[str, object]] = {}
    problems: list[Problem] = []
    for manifest in manifests:
        try:
            parsed[manifest] = tomllib.loads((root / manifest).read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            problems.append(problem(manifest, "toml", f"cannot read the manifest: {err}"))
    workspaces = {posixpath.dirname(m) for m, data in parsed.items() if "workspace" in data}
    roots = []
    for manifest, data in parsed.items():
        folder = posixpath.dirname(manifest)
        standalone = "package" in data and nearest_folder(manifest, workspaces) is None
        if folder in workspaces or standalone:
            roots.append(folder)
    return sorted(roots), problems


def label(folder: str) -> str:
    return folder or "."


def mentions(workflows: set[str]) -> str:
    return ", ".join(sorted(workflows))


def check_runs(analyzer: Analyzer, workflows: list[Workflow], tracked: list[str]) -> list[Problem]:
    """Every check, test and exempt script against what the steps run."""
    executed = analyzer.usage.executed
    for_changes = {w.path for w in workflows if w.for_changes}
    problems: list[Problem] = []
    for path in sorted(tracked):
        if not (CHECK_FILE.search(path) or TEST_FILE.search(path) or LOOSE_TOOL.match(path)):
            continue
        running = executed.get(path, set())
        exempt = path in NOT_A_CHECK
        if running and exempt:
            problems.append(
                problem(path, "stale-exception", STALE_EXCEPTION, warning=True),
            )
        elif running and not running & for_changes:
            only = (
                f"run only by {mentions(running)}, which does not run on a push or a pull request"
            )
            problems.append(problem(path, "only-on-schedule", only, warning=True))
        elif not running and not exempt:
            if TEST_FILE.search(path) and unrun_project(analyzer, path):
                continue  # the missing pytest job of its project is reported once, below
            problems.append(problem(path, "not-run", not_run_message(path)))
    for path in sorted(NOT_A_CHECK):
        if path not in analyzer.files:
            problems.append(problem(path, "exception-without-file", MISSING_EXCEPTION))
    return problems


STALE_EXCEPTION = "is listed in NOT_A_CHECK but CI runs it: remove it from NOT_A_CHECK"
MISSING_EXCEPTION = (
    "is listed in NOT_A_CHECK but the file does not exist: remove it from NOT_A_CHECK"
)


def unrun_project(analyzer: Analyzer, path: str) -> bool:
    """True when ``path`` is in a Python project that no pytest step runs in at all."""
    folder = nearest_folder(path, analyzer.projects)
    return folder is not None and folder not in analyzer.usage.pytest_folders


def not_run_message(path: str) -> str:
    if CHECK_FILE.search(path):
        return (
            "no step of .github/workflows runs this check: add a step to ci.yml "
            f"(python3 {path}), or list the file in NOT_A_CHECK with the reason"
        )
    if TEST_FILE.search(path):
        return (
            "no step of .github/workflows runs this test: add a step to ci.yml "
            f"(python3 {path}, or a pytest step that collects it)"
        )
    return (
        "is in tools/, is not run by CI and is not a check by name: if it checks something, "
        "call it from ci.yml; if it does not, list it in NOT_A_CHECK with the reason"
    )


def check_projects(analyzer: Analyzer, tracked: list[str]) -> list[Problem]:
    """Python projects need a pytest step in their folder, Cargo workspaces a cargo test."""
    problems: list[Problem] = []
    usage = analyzer.usage
    for folder in sorted(analyzer.projects):
        has_tests = any(nearest_folder(t, analyzer.projects) == folder for t in analyzer.test_files)
        where = f"{folder}/pyproject.toml" if folder else "pyproject.toml"
        if not has_tests:
            problems.append(
                problem(
                    where,
                    "no-tests",
                    "the project has no test_*.py files, so no pytest step is needed",
                    True,
                )
            )
        elif folder not in usage.pytest_folders:
            idle = sum(
                1
                for t in analyzer.test_files
                if nearest_folder(t, analyzer.projects) == folder and t not in usage.executed
            )
            problems.append(
                problem(
                    where,
                    "no-pytest-job",
                    f"no CI step runs pytest in {label(folder)} (a job with "
                    f"working-directory: {label(folder)} and a pytest step is missing); "
                    f"test files in it that nothing else runs: {idle}",
                )
            )
    return problems


def check_cargo(analyzer: Analyzer, folders: list[str]) -> list[Problem]:
    problems: list[Problem] = []
    for folder in folders:
        if folder not in analyzer.usage.cargo_folders:
            where = f"{folder}/Cargo.toml" if folder else "Cargo.toml"
            problems.append(
                problem(
                    where,
                    "no-cargo-test",
                    f"no CI step runs cargo test for the whole workspace in {label(folder)} "
                    f"(a job with working-directory: {label(folder)} and `cargo test` is missing)",
                )
            )
    return problems


def check(root: Path, tracked: list[str]) -> tuple[list[Problem], dict[str, int]]:
    """All problems of the repo at ``root`` whose tracked files are ``tracked``."""
    workflows, problems = load_workflows(root, tracked)
    if not workflows and not problems:
        problems.append(problem(WORKFLOWS, "no-workflows", "no workflow files found"))
    analyzer = Analyzer(root, tracked)
    for workflow in workflows:
        for job in workflow.jobs:
            for step in job.steps:
                analyzer.step(workflow, job, step)
    problems += analyzer.usage.problems
    roots, cargo_problems = cargo_roots(root, tracked)
    problems += cargo_problems
    problems += check_runs(analyzer, workflows, tracked)
    problems += check_projects(analyzer, tracked)
    problems += check_cargo(analyzer, roots)
    stats = {
        "workflows": len(workflows),
        "steps": analyzer.usage.steps,
        "checks": sum(1 for p in tracked if CHECK_FILE.search(p)),
        "tests": len(analyzer.test_files),
        "python": len(analyzer.projects),
        "cargo": len(roots),
    }
    problems.sort(key=lambda p: (p.warning, p.path, p.message))
    return problems, stats


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=root, check=True, capture_output=True, text=True, errors="surrogateescape"
    ).stdout
    return out.splitlines()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        print("usage: python3 tools/check_ci_coverage.py", file=sys.stderr)
        return 0 if args in (["-h"], ["--help"]) else 2
    try:
        tracked = tracked_files(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    problems, stats = check(ROOT, tracked)
    for item in problems:
        print(item, file=sys.stderr)
    errors = sum(not p.warning for p in problems)
    print(
        f"{stats['workflows']} workflows ({stats['steps']} run steps), {stats['checks']} checks, "
        f"{stats['tests']} test files, {stats['python']} Python projects, "
        f"{stats['cargo']} Cargo workspaces checked: {errors} errors, "
        f"{len(problems) - errors} warnings"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    # A file name that is not UTF-8 is printed escaped, not as a traceback.
    sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    sys.exit(main())
