#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the tracked shell scripts (``*.sh``): header, strict mode, dangerous patterns.

Only the text of the script is read. No shell, ``shellcheck`` or ``bash -n`` is run: a
small reader splits the script into commands (it knows quotes, ``$(...)``, backticks,
here-documents, comments and ``case`` patterns), and the rules look at those commands.

For every script the checker verifies:

* the first line is exactly ``#!/bin/sh``, ``#!/bin/bash`` or ``#!/usr/bin/env bash``, with
  no byte order mark; the first 5 lines (as in check_spdx.py) have a comment with
  ``SPDX-License-Identifier:``; the file has no CRLF line endings;
* strict mode in the first 20 code lines (comments, empty lines and here-document bodies
  do not count): ``set -eu`` for ``#!/bin/sh``, ``set -euo pipefail`` for bash (the
  letters may be in any order or split: ``set -e -u``, ``set -o errexit -o nounset``).
  For ``config.sh`` and ``disk.sh`` of the KIWI images (``obs/images/``), which have
  their own habits, a missing strict mode is only a warning;
* errors: ``curl`` or ``wget`` piped into a shell (``| sh``, ``| sudo bash``) or fed to one
  through ``<(...)`` or ``sh -c "$(curl ...)"``; ``eval`` with a variable or a command
  substitution; a recursive ``rm`` with a variable that is not quoted, or that is quoted
  but starts the path (so it may become ``/``) without a ``${VAR:?}`` guard; a script
  with an unterminated quote or here-document;
* in ``#!/bin/sh`` scripts, errors for plain bash constructs: ``[[ ]]``, ``function``,
  arrays (``a=(...)``, ``${a[@]}``, ``declare -a``, ``mapfile``), ``source``, ``==`` in
  ``[ ]``, ``$'...'`` and ``&>``;
* warnings: backticks (use ``$(...)``); a write into ``/etc`` or ``/usr`` (redirect, ``tee``,
  ``cp``, ``mv``, ``install``, ``ln``, ``sed -i``, ``dd of=``, ``touch``, ``mkdir``) with no
  comment on the line above it (a run of such lines may share one comment); KIWI scripts
  are not checked for this, they configure the image root.

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but do not fail
the check; some mark things the repo already does today (see ``LEGACY``). Only
``git ls-files`` is run. Nothing else is executed, nothing is written.

Run: python3 tools/check_shell.py [SCRIPT ...]
SCRIPT is a path from the repo root; without it every tracked ``*.sh`` is checked.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import posixpath
import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

SHEBANGS = ("#!/bin/sh", "#!/bin/bash", "#!/usr/bin/env bash")
SPDX_LINES = 5  # the same window as tools/check_spdx.py
STRICT_LINES = 20
KIWI_NAMES = frozenset({"config.sh", "disk.sh"})
KIWI_DIR = "obs/images/"

# Problems the repo already has today: (path from the repo root, rule). They are
# warnings, so that the check can be switched on in CI; fix the script, then delete
# the line.
LEGACY: frozenset[tuple[str, str]] = frozenset(
    {
        # The official rustup installer (https, TLS 1.2), the way rustup.rs documents it.
        ("reports/deploy/install.sh", "curl-pipe"),
        # No strict mode on purpose: the report is made of commands that may fail
        # ("a failing command is recorded, not fatal"); only unset variables are errors.
        ("image/tests/update-rollback.sh", "strict-mode"),
        # No -e on purpose: failed installs end in exit 75 (retry later), see the header.
        ("obs/packages/vilna-flatpak-defaults/vilna-flatpak-defaults.sh", "strict-mode"),
        # Twelve lines, no command that could fail on the way to exec.
        ("thirdparty/claude-desktop/claude-desktop.sh", "strict-mode"),
        # $new is "$www/$rev", both set a few lines above.
        ("site/deploy/update.sh", "rm-rf"),
        # $work comes from mktemp -d on a line above.
        ("shell-extension/tests/shell/run.sh", "rm-rf"),
    }
)

LEADING_WORDS = frozenset({"if", "then", "elif", "else", "while", "until", "do", "!", "{", "time"})
WRAPPERS = frozenset(
    {"sudo", "doas", "env", "exec", "command", "builtin", "nohup", "nice", "timeout"}
)
# The short options of a wrapper that take a value (sudo -u root, timeout -k 5 ...).
WRAPPER_ARG_OPTIONS = {
    "sudo": "ughpCDRTU",
    "doas": "uC",
    "env": "uCS",
    "nice": "n",
    "timeout": "ks",
}
SHELLS = frozenset({"sh", "bash", "dash", "ash", "zsh", "ksh", "mksh", "busybox"})
DOWNLOADERS = frozenset({"curl", "wget"})
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*(\[[^\]]*\])?\+?=")
ARRAY_ELEMENT = re.compile(r"[A-Za-z_]\w*\[[^\]]*\]\+?=")
ARRAY_EXPANSION = re.compile(r"\$\{[#!]?[A-Za-z_]\w*\[")
GUARD = re.compile(r"\$\{[A-Za-z_]\w*:?\?")
SYSTEM_PATH = re.compile(r"/(etc|usr)(/|$)")
ARRAY_DECLARE = frozenset({"declare", "typeset", "local"})
VARIABLE_NAME = re.compile(r"[A-Za-z_]\w*|\d|[@*#?$!-]")


# --- results -----------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False
    rule: str = ""
    line: int = 0

    def __str__(self) -> str:
        kind = "warning: " if self.warning else ""
        where = f"line {self.line}: " if self.line else ""
        return f"{self.path}: {kind}{where}{self.message}"


class ShellSyntaxError(Exception):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(message)
        self.line = line
        self.message = message


# --- reading a script --------------------------------------------------------


@dataclass
class Piece:
    kind: str  # "lit", "var" (a variable or arithmetic expansion) or "sub" (a command)
    text: str
    quoted: bool


@dataclass
class Word:
    line: int
    pieces: list[Piece] = field(default_factory=list)
    subs: list[list[Command]] = field(default_factory=list)
    array: bool = False  # name=( ... ) or name+=( ... )
    ansi: bool = False  # an unquoted $'...'

    @property
    def value(self) -> str | None:
        """The text of a word with no expansion in it, else None."""
        if any(p.kind != "lit" for p in self.pieces):
            return None
        return "".join(p.text for p in self.pieces)

    @property
    def keyword(self) -> str | None:
        """The text of a word that is written plainly (nothing quoted, nothing expanded)."""
        if any(p.kind != "lit" or p.quoted for p in self.pieces):
            return None
        return "".join(p.text for p in self.pieces)

    @property
    def prefix(self) -> str:
        """The literal text before the first expansion."""
        out = []
        for piece in self.pieces:
            if piece.kind != "lit":
                break
            out.append(piece.text)
        return "".join(out)

    @property
    def expansions(self) -> list[Piece]:
        return [p for p in self.pieces if p.kind != "lit"]


@dataclass
class Redirect:
    op: str
    target: Word | None
    line: int


@dataclass
class Command:
    words: list[Word]
    redirects: list[Redirect]
    line: int
    end_line: int
    level: int  # 0 for the script itself, more inside ( ), $( ), backticks and <( )
    pipe_next: bool = False


@dataclass
class Program:
    commands: list[Command] = field(default_factory=list)
    pipelines: list[list[Command]] = field(default_factory=list)
    code_lines: set[int] = field(default_factory=set)
    backticks: list[int] = field(default_factory=list)


@dataclass
class Frame:
    """What one run of ``parse_commands`` has seen so far."""

    cmds: list[Command] = field(default_factory=list)
    words: list[Word] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)
    word: Word | None = None
    cmd_line: int = 0
    pending: Redirect | None = None
    heredocs: list[tuple[str, bool, int]] = field(default_factory=list)
    paren: int = 0
    case_depth: int = 0
    in_cond: bool = False


class Parser:
    def __init__(self, text: str, line: int = 1, level: int = 0, program: Program | None = None):
        self.s = text
        self.i = 0
        self.line = line
        self.level = level
        self.program = program if program is not None else Program()

    # -- the main loop --

    def parse_commands(self, nested: bool = False) -> list[Command]:
        """Read commands up to the end of the text, or (nested) up to the closing ``)``."""
        f = Frame()
        start_line = self.line
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if c in " \t\r":
                self.end_word(f)
                self.i += 1
            elif c == "\\":
                self.backslash(f)
            elif c == "\n":
                self.end_word(f)
                self.end_command(f, "\n")
                self.i += 1
                self.line += 1
                self.read_heredocs(f)
            elif c == "#" and f.word is None:
                while self.i < len(s) and s[self.i] != "\n":
                    self.i += 1
            elif c == ")" and not f.in_cond:
                if self.close_paren(f, nested):
                    return self.finish(f)
            else:
                self.program.code_lines.add(self.line)
                self.token(f, c)
        if nested:
            raise ShellSyntaxError(start_line, "unterminated $( ) or <( )")
        self.end_word(f)
        self.end_command(f, "\n")
        if f.heredocs:
            raise ShellSyntaxError(f.heredocs[0][2], "unterminated here-document")
        return self.finish(f)

    def finish(self, f: Frame) -> list[Command]:
        group: list[Command] = []
        for cmd in f.cmds:
            group.append(cmd)
            if not cmd.pipe_next:
                if len(group) > 1:
                    self.program.pipelines.append(group)
                group = []
        return f.cmds

    def token(self, f: Frame, c: str) -> None:
        s = self.s
        if c == "'":
            self.read_single(f)
        elif c == '"':
            self.read_double(f)
        elif c == "`":
            self.read_backtick(f, quoted=False)
        elif c == "$":
            self.read_dollar(f, quoted=False)
        elif f.in_cond and c != ";":
            self.add_lit(f, c, quoted=False)
            self.i += 1
        elif c == "(" and f.word is not None and self.assignment_prefix(f):
            self.read_array(f)
        elif c == "(" and f.word is None and not f.words and s[self.i + 1 : self.i + 2] == "(":
            self.read_arithmetic_command(f)
        elif c in ";&|<>()":
            self.operator(f, c)
        else:
            self.add_lit(f, c, quoted=False)
            self.i += 1

    def backslash(self, f: Frame) -> None:
        nxt = self.s[self.i + 1 : self.i + 2]
        if nxt == "\n":  # a continuation: the line goes on, the word too
            self.i += 2
            self.line += 1
            return
        self.program.code_lines.add(self.line)
        self.add_lit(f, nxt, quoted=True)
        self.i += 2

    def close_paren(self, f: Frame, nested: bool) -> bool:
        """Handle ``)``; True when it closes the ``$(`` this run belongs to."""
        self.end_word(f)
        if f.paren > 0:
            self.end_command(f, ")")
            f.paren -= 1
        elif f.case_depth > 0:  # the end of a case pattern
            self.end_command(f, ")")
        elif nested:
            self.end_command(f, "\n")
            self.i += 1
            return True
        else:  # a stray ), left for the shell to complain about
            self.end_command(f, ")")
        self.program.code_lines.add(self.line)
        self.i += 1
        return False

    # -- words and commands --

    def add_lit(self, f: Frame, text: str, quoted: bool) -> None:
        word = self.begin_word(f)
        if word.pieces and word.pieces[-1].kind == "lit" and word.pieces[-1].quoted == quoted:
            word.pieces[-1].text += text
        else:
            word.pieces.append(Piece("lit", text, quoted))

    def begin_word(self, f: Frame) -> Word:
        if f.word is None:
            f.word = Word(self.line)
        return f.word

    def end_word(self, f: Frame) -> None:
        word = f.word
        if word is None:
            return
        f.word = None
        if f.pending is not None:
            redirect = f.pending
            f.pending = None
            redirect.target = word
            if redirect.op in ("<<", "<<-"):
                delimiter = "".join(p.text for p in word.pieces if p.kind == "lit")
                f.heredocs.append((delimiter, redirect.op == "<<-", redirect.line))
            return
        if not f.words:
            f.cmd_line = word.line
        f.words.append(word)
        index = command_index(f.words)
        if index == len(f.words) - 1:
            keyword = word.keyword
            if keyword == "case":
                f.case_depth += 1
            elif keyword == "esac":
                f.case_depth = max(0, f.case_depth - 1)
            elif keyword == "[[":
                f.in_cond = True
        if f.in_cond and word.keyword == "]]":
            f.in_cond = False

    def end_command(self, f: Frame, separator: str) -> None:
        self.end_word(f)
        if f.pending is not None:
            f.pending = None
        if f.words or f.redirects:
            cmd = Command(
                f.words,
                f.redirects,
                f.cmd_line or self.line,
                self.line,
                self.level + (1 if f.paren > 0 else 0),
                pipe_next=separator == "|",
            )
            f.cmds.append(cmd)
            self.program.commands.append(cmd)
        elif separator == "|" and f.cmds:
            f.cmds[-1].pipe_next = True
        f.words = []
        f.redirects = []
        f.cmd_line = 0

    def operator(self, f: Frame, c: str) -> None:
        s = self.s
        two = s[self.i : self.i + 2]
        three = s[self.i : self.i + 3]
        if c == "(":
            self.end_word(f)
            self.end_command(f, "(")
            f.paren += 1
            self.i += 1
        elif c == ";":
            self.end_word(f)
            self.end_command(f, ";")
            self.i += 3 if three == ";;&" else 2 if two in (";;", ";&") else 1
        elif c == "&" and two == "&&":
            self.end_word(f)
            self.end_command(f, "&&")
            self.i += 2
        elif c == "&" and two == "&>":
            self.end_word(f)
            op = "&>>" if three == "&>>" else "&>"
            self.start_redirect(f, op)
            self.i += len(op)
        elif c == "&":
            self.end_word(f)
            self.end_command(f, "&")
            self.i += 1
        elif c == "|":
            self.end_word(f)
            self.end_command(f, "||" if two == "||" else "|")
            self.i += 2 if two in ("||", "|&") else 1
        else:
            self.redirection(f, c, two, three)

    def redirection(self, f: Frame, c: str, two: str, three: str) -> None:
        if two in ("<(", ">("):
            self.read_process_substitution(f)
            return
        operators = ("<<<", "<<-", "<<", ">>", ">|", ">&", "<&", "<>")
        op = next((o for o in operators if three.startswith(o)), c)
        if f.word is not None and all(
            p.kind == "lit" and not p.quoted and p.text.isdigit() for p in f.word.pieces
        ):
            f.word = None  # the file descriptor in front: 2>file
        else:
            self.end_word(f)
        self.start_redirect(f, op)
        self.i += len(op)

    def start_redirect(self, f: Frame, op: str) -> None:
        redirect = Redirect(op, None, self.line)
        f.redirects.append(redirect)
        f.pending = redirect

    def assignment_prefix(self, f: Frame) -> bool:
        word = f.word
        if word is None or len(word.pieces) != 1:
            return False
        piece = word.pieces[0]
        return (
            piece.kind == "lit"
            and not piece.quoted
            and bool(re.fullmatch(r"[A-Za-z_]\w*\+?=", piece.text))
        )

    # -- quotes and expansions --

    def read_single(self, f: Frame) -> None:
        start = self.line
        end = self.s.find("'", self.i + 1)
        if end == -1:
            raise ShellSyntaxError(start, "unterminated single quote")
        text = self.s[self.i + 1 : end]
        self.add_lit(f, "", quoted=True)
        self.add_lit(f, text, quoted=True)
        self.span(text)
        self.i = end + 1

    def span(self, text: str) -> None:
        """A string that may cover several lines: they are all code lines."""
        for _ in range(text.count("\n")):
            self.program.code_lines.add(self.line)
            self.line += 1
        self.program.code_lines.add(self.line)

    def read_double(self, f: Frame) -> None:
        start = self.line
        s = self.s
        self.add_lit(f, "", quoted=True)
        self.i += 1
        while True:
            if self.i >= len(s):
                raise ShellSyntaxError(start, "unterminated double quote")
            ch = s[self.i]
            if ch == '"':
                self.i += 1
                return
            if ch == "\\":
                nxt = s[self.i + 1 : self.i + 2]
                if nxt == "\n":
                    self.line += 1
                elif nxt in ('"', "\\", "$", "`"):
                    self.add_lit(f, nxt, quoted=True)
                else:
                    self.add_lit(f, "\\" + nxt, quoted=True)
                self.i += 2
            elif ch == "$":
                self.read_dollar(f, quoted=True)
            elif ch == "`":
                self.read_backtick(f, quoted=True)
            else:
                self.add_lit(f, ch, quoted=True)
                if ch == "\n":
                    self.program.code_lines.add(self.line)
                    self.line += 1
                self.i += 1

    def read_dollar(self, f: Frame, quoted: bool) -> None:
        s = self.s
        nxt = s[self.i + 1 : self.i + 2]
        word = self.begin_word(f)
        if s.startswith("$((", self.i):
            end = self.balanced(self.i + 2, "(", ")")
            word.pieces.append(Piece("var", s[self.i : end], quoted))
            self.span(s[self.i : end])
            self.i = end
        elif nxt == "(":
            start = self.i
            self.i += 2
            sub = Parser(s, self.line, self.level + 1, self.program)
            sub.i = self.i
            cmds = sub.parse_commands(nested=True)
            self.i, self.line = sub.i, sub.line
            word.subs.append(cmds)
            word.pieces.append(Piece("sub", s[start : self.i], quoted))
        elif nxt == "{":
            end = self.braces(self.i + 2)
            word.pieces.append(Piece("var", s[self.i : end], quoted))
            self.span(s[self.i : end])
            self.i = end
        elif nxt == "'" and not quoted:
            end = self.ansi_end(self.i + 2)
            word.ansi = True
            self.add_lit(f, "", quoted=True)
            self.add_lit(f, s[self.i + 2 : end - 1], quoted=True)
            self.span(s[self.i : end])
            self.i = end
        else:
            match = VARIABLE_NAME.match(s, self.i + 1)
            if match:
                word.pieces.append(Piece("var", "$" + match.group(0), quoted))
                self.i = match.end()
            else:
                self.add_lit(f, "$", quoted=quoted)
                self.i += 1

    def balanced(self, start: int, open_: str, close: str) -> int:
        """The index after the bracket that closes the one before ``start``."""
        s = self.s
        depth = 1
        j = start
        while j < len(s):
            ch = s[j]
            if ch == "\\":
                j += 1
            elif ch in "'\"":
                end = j + 1
                while end < len(s) and s[end] != ch:
                    end += 2 if s[end] == "\\" and ch == '"' else 1
                j = end
            elif ch == open_:
                depth += 1
            elif ch == close:
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        raise ShellSyntaxError(self.line, f"unterminated {open_}")

    def braces(self, start: int) -> int:
        return self.balanced(start, "{", "}")

    def ansi_end(self, start: int) -> int:
        s = self.s
        j = start
        while j < len(s):
            if s[j] == "\\":
                j += 2
                continue
            if s[j] == "'":
                return j + 1
            j += 1
        raise ShellSyntaxError(self.line, "unterminated $'...'")

    def read_backtick(self, f: Frame, quoted: bool) -> None:
        s = self.s
        start = self.i + 1
        j = start
        raw: list[str] = []
        while True:
            if j >= len(s):
                raise ShellSyntaxError(self.line, "unterminated backtick")
            ch = s[j]
            if ch == "\\" and s[j + 1 : j + 2] in ("`", "\\", "$"):
                raw.append(s[j + 1])
                j += 2
            elif ch == "`":
                break
            else:
                raw.append(ch)
                j += 1
        text = "".join(raw)
        self.program.backticks.append(self.line)
        word = self.begin_word(f)
        sub = Parser(text, self.line, self.level + 1, self.program)
        word.subs.append(sub.parse_commands())
        word.pieces.append(Piece("sub", s[self.i : j + 1], quoted))
        self.line += s[self.i : j + 1].count("\n")
        self.i = j + 1

    def read_process_substitution(self, f: Frame) -> None:
        word = self.begin_word(f)
        start = self.i
        sub = Parser(self.s, self.line, self.level + 1, self.program)
        sub.i = self.i + 2
        word.subs.append(sub.parse_commands(nested=True))
        self.i, self.line = sub.i, sub.line
        word.pieces.append(Piece("sub", self.s[start : self.i], quoted=False))

    def read_array(self, f: Frame) -> None:
        """``name=( ... )``: the elements are skipped, the word is marked as an array."""
        end = self.balanced(self.i + 1, "(", ")")
        word = self.begin_word(f)
        word.array = True
        self.span(self.s[self.i : end])
        self.i = end
        self.end_word(f)

    def read_arithmetic_command(self, f: Frame) -> None:
        """``(( ... ))`` at the start of a command."""
        end = self.balanced(self.i + 1, "(", ")")
        self.add_lit(f, "((", quoted=False)
        self.span(self.s[self.i : end])
        self.i = end
        self.end_word(f)

    def read_heredocs(self, f: Frame) -> None:
        """Skip the bodies of the here-documents the last line asked for."""
        s = self.s
        for delimiter, strip, start in f.heredocs:
            while True:
                if self.i >= len(s):
                    raise ShellSyntaxError(start, "unterminated here-document")
                end = s.find("\n", self.i)
                text = s[self.i : len(s) if end == -1 else end]
                self.i = len(s) if end == -1 else end + 1
                self.line += 1
                text = text.rstrip("\r")
                if (text.lstrip("\t") if strip else text) == delimiter:
                    break
        f.heredocs = []


def parse(text: str) -> Program:
    parser = Parser(text)
    parser.parse_commands()
    return parser.program


# --- looking at commands -----------------------------------------------------


def command_index(words: Sequence[Word]) -> int | None:
    """The index of the word that names the command: after ``if``, ``VAR=x``, ``sudo`` ..."""
    i = 0
    while i < len(words):
        word = words[i]
        keyword = word.keyword
        if keyword in LEADING_WORDS or is_assignment(word):
            i += 1
        elif keyword in WRAPPERS:
            i = skip_wrapper(words, i, keyword)
        else:
            return i
    return None


def is_assignment(word: Word) -> bool:
    first = word.pieces[0] if word.pieces else None
    return bool(first and first.kind == "lit" and not first.quoted and ASSIGNMENT.match(first.text))


def skip_wrapper(words: Sequence[Word], i: int, name: str) -> int:
    """The index of the word after ``sudo -u root``, ``env A=1``, ``timeout 5`` ..."""
    taking = WRAPPER_ARG_OPTIONS.get(name, "")
    i += 1
    while i < len(words):
        text = words[i].keyword or ""
        if text == "--":
            return i + 1
        if not text.startswith("-") or len(text) < 2:
            break
        i += 1
        if not text.startswith("--") and text[-1] in taking:
            i += 1
    if name == "timeout":
        i += 1  # the duration
    return i


def name_and_args(cmd: Command) -> tuple[str, list[Word]]:
    """The command name (``""`` if there is none or it is built from a variable) and its words."""
    index = command_index(cmd.words)
    if index is None:
        return "", []
    word = cmd.words[index]
    name = word.value if word.value is not None else ""
    return posixpath.basename(name), cmd.words[index + 1 :]


def split_args(
    args: Sequence[Word], taking: str = ""
) -> tuple[list[str], list[tuple[str, str]], list[Word]]:
    """Split the words of a command into flags, options with a value, and operands.

    ``taking`` lists the short options that take a value (``-m 0755``, ``-t DIR``).
    Flags are ``"r"`` for ``-r`` and ``"--recursive"``; values are (flag, text) pairs.
    """
    flags: list[str] = []
    values: list[tuple[str, str]] = []
    operands: list[Word] = []
    i = 0
    options_end = False
    while i < len(args):
        word = args[i]
        text = word.keyword
        i += 1
        if options_end or text is None or not text.startswith("-") or len(text) < 2:
            operands.append(word)
        elif text == "--":
            options_end = True
        elif text.startswith("--"):
            name, eq, value = text.partition("=")
            flags.append(name)
            if eq:
                values.append((name, value))
            elif name == "--target-directory" and i < len(args):
                values.append((name, args[i].prefix))
                i += 1
        else:
            for pos, letter in enumerate(text[1:], start=1):
                flags.append(letter)
                if letter in taking:
                    rest = text[pos + 1 :]
                    if rest:
                        values.append((letter, rest))
                    elif i < len(args):
                        values.append((letter, args[i].prefix))
                        i += 1
                    break
    return flags, values, operands


def iter_commands(cmds: Sequence[Command]) -> Iterator[Command]:
    """The commands and, recursively, the ones inside their ``$( )`` and ``<( )``."""
    for cmd in cmds:
        yield cmd
        words = [w for w in cmd.words] + [r.target for r in cmd.redirects if r.target]
        for word in words:
            for sub in word.subs:
                yield from iter_commands(sub)


# --- the rules ---------------------------------------------------------------


def is_kiwi(path: str) -> bool:
    return path.startswith(KIWI_DIR) and posixpath.basename(path) in KIWI_NAMES


def check_header(path: str, lines: list[str]) -> tuple[list[Problem], str]:
    """The shebang and SPDX rules; also tells the kind of shell: ``sh``, ``bash`` or ``""``."""
    problems: list[Problem] = []
    first = lines[0] if lines else ""
    kind = ""
    if first.startswith("\ufeff"):
        problems.append(Problem(path, "byte order mark before the shebang", rule="shebang", line=1))
    elif first in SHEBANGS:
        kind = "sh" if first == "#!/bin/sh" else "bash"
    elif first.startswith("#!"):
        problems.append(
            Problem(
                path,
                f"shebang {first!r} is not one of {', '.join(SHEBANGS)}",
                rule="shebang",
                line=1,
            )
        )
    else:
        problems.append(Problem(path, "the first line is not a shebang", rule="shebang", line=1))
    head = lines[:SPDX_LINES]
    spdx = [ln for ln in head if ln.lstrip().startswith("#") and "SPDX-License-Identifier:" in ln]
    if not any(ln.split("SPDX-License-Identifier:", 1)[1].strip() for ln in spdx):
        problems.append(
            Problem(
                path,
                f"no '# SPDX-License-Identifier: ...' comment in the first {SPDX_LINES} lines",
                rule="spdx",
            )
        )
    return problems, kind


def set_options(cmd: Command) -> set[str]:
    """What a ``set`` command switches on: letters such as ``e``, ``u`` and ``pipefail``."""
    name, args = name_and_args(cmd)
    if name != "set":
        return set()
    names = {"errexit": "e", "nounset": "u", "pipefail": "pipefail"}
    found: set[str] = set()
    i = 0
    while i < len(args):
        text = args[i].keyword or ""
        i += 1
        if text == "-o" and i < len(args):
            option = args[i].keyword or ""
            found.add(names.get(option, option))
            i += 1
        elif text.startswith("-") and not text.startswith("--") and len(text) > 1:
            found.update(letter for letter in text[1:] if letter != "o")
            if text.endswith("o") and i < len(args):
                option = args[i].keyword or ""
                found.add(names.get(option, option))
                i += 1
    return found


def check_strict(path: str, program: Program, kind: str) -> list[Problem]:
    if not kind:
        return []
    code_lines = sorted(program.code_lines)
    cutoff = code_lines[min(STRICT_LINES, len(code_lines)) - 1] if code_lines else 0
    found: set[str] = set()
    for cmd in program.commands:
        if cmd.level == 0 and cmd.line <= cutoff:
            found |= set_options(cmd)
    need = ["e", "u"] + (["pipefail"] if kind == "bash" else [])
    missing = [item for item in need if item not in found]
    if not missing:
        return []
    wanted = "set -euo pipefail" if kind == "bash" else "set -eu"
    names = {"e": "-e", "u": "-u", "pipefail": "-o pipefail"}
    lack = ", ".join(names[m] for m in missing)
    kiwi = is_kiwi(path)
    extra = " (KIWI scripts have their own habits)" if kiwi else ""
    return [
        Problem(
            path,
            f"no strict mode in the first {STRICT_LINES} code lines: expected '{wanted}', "
            f"missing {lack}{extra}",
            warning=kiwi,
            rule="strict-mode",
        )
    ]


def check_curl_pipe(path: str, program: Program) -> list[Problem]:
    problems: list[Problem] = []
    for group in program.pipelines:
        names = [name_and_args(cmd)[0] for cmd in group]
        for i, name in enumerate(names):
            if name not in DOWNLOADERS:
                continue
            later = [c for c, n in zip(group[i + 1 :], names[i + 1 :], strict=True) if n in SHELLS]
            later = [c for c in later if not has_flag(name_and_args(c)[1], "c")]
            if later:
                problems.append(
                    Problem(
                        path,
                        f"{name} is piped into a shell: download, check, then run",
                        rule="curl-pipe",
                        line=group[i].line,
                    )
                )
                break
    for cmd in iter_commands(program.commands):
        name, args = name_and_args(cmd)
        if name in SHELLS or name in (".", "source", "eval"):
            for word in args:
                inner = [c for sub in word.subs for c in iter_commands(sub)]
                if any(name_and_args(c)[0] in DOWNLOADERS for c in inner):
                    problems.append(
                        Problem(
                            path,
                            f"{name} runs the output of a download: download, check, then run",
                            rule="curl-pipe",
                            line=cmd.line,
                        )
                    )
                    break
    return problems


def has_flag(args: Sequence[Word], letter: str) -> bool:
    flags, _, _ = split_args(args)
    return letter in flags


def check_eval(path: str, program: Program) -> list[Problem]:
    problems = []
    for cmd in program.commands:
        name, args = name_and_args(cmd)
        if name == "eval" and any(word.expansions for word in args):
            problems.append(
                Problem(
                    path,
                    "eval with a variable or a command substitution runs whatever it holds",
                    rule="eval",
                    line=cmd.line,
                )
            )
    return problems


def check_rm(path: str, program: Program) -> list[Problem]:
    problems = []
    for cmd in program.commands:
        name, args = name_and_args(cmd)
        if name != "rm":
            continue
        flags, _, operands = split_args(args)
        if not {"r", "R", "--recursive"} & set(flags):
            continue
        for operand in operands:
            message = rm_problem(operand)
            if message:
                problems.append(Problem(path, message, rule="rm-rf", line=operand.line))
    return problems


def rm_problem(operand: Word) -> str:
    """What is wrong with a variable in the operand of ``rm -r``, or ``""``."""
    if not operand.expansions:
        return ""
    if any(not piece.quoted for piece in operand.expansions):
        return "rm -r with a variable that is not in quotes: it may split or expand"
    first = next(p for p in operand.pieces if p.kind != "lit")
    anchor = operand.prefix.strip("/.~")
    if anchor or GUARD.match(first.text):
        return ""
    return f"rm -r {first.text}: if it is empty this deletes the wrong place; use ${{VAR:?}}"


def check_bashisms(path: str, program: Program) -> list[Problem]:
    found: list[tuple[int, str]] = []
    for cmd in program.commands:
        name, args = name_and_args(cmd)
        index = command_index(cmd.words)
        first = cmd.words[index].keyword if index is not None else None
        if first in ("[[", "function", "source"):
            found.append((cmd.line, {"[[": "[[ ]]", "function": "function"}.get(first, first)))
        if name in ("[", "test") and any(word.keyword == "==" for word in args):
            found.append((cmd.line, "== in [ ] (use =)"))
        if name == "mapfile" or name == "readarray":
            found.append((cmd.line, "an array (" + name + ")"))
        if name in ARRAY_DECLARE:
            flags, _, _ = split_args(args)
            if {"a", "A"} & set(flags):
                found.append((cmd.line, f"an array ({name} -a)"))
        for word in cmd.words:
            found += word_bashisms(word)
        for redirect in cmd.redirects:
            if redirect.op in ("&>", "&>>"):
                found.append((redirect.line, "&> (use >file 2>&1)"))
    return [
        Problem(
            path,
            f"{what} is a bash construct, but the script runs under sh",
            rule="bashism",
            line=n,
        )
        for n, what in sorted(set(found))
    ]


def word_bashisms(word: Word) -> list[tuple[int, str]]:
    found = []
    if word.ansi:
        found.append((word.line, "$'...'"))
    if word.array:
        found.append((word.line, "an array (name=(...))"))
    text = word.pieces[0].text if word.pieces and word.pieces[0].kind == "lit" else ""
    if word.pieces and not word.pieces[0].quoted and ARRAY_ELEMENT.match(text):
        found.append((word.line, "an array element (name[i]=...)"))
    if any(ARRAY_EXPANSION.match(p.text) for p in word.pieces if p.kind == "var"):
        found.append((word.line, "an array (${name[...]})"))
    return found


def check_backticks(path: str, program: Program) -> list[Problem]:
    return [
        Problem(path, "backticks: use $(...)", warning=True, rule="backtick", line=line)
        for line in sorted(set(program.backticks))
    ]


def written_paths(cmd: Command) -> list[str]:
    """The literal beginnings of the paths a command writes to (best effort)."""
    paths = [
        r.target.prefix
        for r in cmd.redirects
        if r.target is not None and r.op in (">", ">>", ">|", "&>", "&>>", "<>")
    ]
    name, args = name_and_args(cmd)
    if name == "tee":
        paths += [w.prefix for w in split_args(args)[2]]
    elif name in ("cp", "install", "ln", "mv"):
        flags, values, operands = split_args(args, "mtogSTZ" if name == "install" else "tS")
        targets = [v for f, v in values if f in ("t", "--target-directory")]
        if targets:
            paths += targets
        elif name == "mv" or (name == "install" and ("d" in flags or "-d" in flags)):
            paths += [w.prefix for w in operands]
        elif operands:
            paths.append(operands[-1].prefix)
    elif name == "sed":
        flags, values, operands = split_args(args, "efl")
        if "i" in flags or "--in-place" in flags or any(f.startswith("--in-place") for f in flags):
            has_script = any(f in ("e", "f") for f, _ in values) or "--expression" in flags
            paths += [w.prefix for w in (operands if has_script else operands[1:])]
    elif name == "dd":
        paths += [w.prefix[3:] for w in args if w.prefix.startswith("of=")]
    elif name in ("touch", "mkdir"):
        paths += [w.prefix for w in split_args(args, "mdtr")[2]]
    return paths


def check_system_writes(path: str, program: Program, lines: list[str]) -> list[Problem]:
    problems = []
    chain_end = 0
    chain_explained = False
    for cmd in sorted(program.commands, key=lambda c: c.line):
        if not any(SYSTEM_PATH.match(p) for p in written_paths(cmd)):
            continue
        above = lines[cmd.line - 2].lstrip() if cmd.line >= 2 else ""
        explained = above.startswith("#") or (chain_explained and chain_end >= cmd.line - 1)
        chain_end, chain_explained = cmd.end_line, explained
        if not explained:
            problems.append(
                Problem(
                    path,
                    "writes into /etc or /usr with no comment above it that says why",
                    warning=True,
                    rule="system-write",
                    line=cmd.line,
                )
            )
    return problems


def check_script(path: str, text: str) -> list[Problem]:
    """All problems of one script, given its text."""
    problems: list[Problem] = []
    if "\r" in text:
        problems.append(Problem(path, "CRLF line endings: the shebang would fail", rule="crlf"))
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    problems_head, kind = check_header(path, lines)
    problems += problems_head
    try:
        program = parse(text)
    except ShellSyntaxError as err:
        problems.append(Problem(path, err.message, rule="syntax", line=err.line))
        return [downgrade(p) for p in problems]
    problems += check_strict(path, program, kind)
    problems += check_curl_pipe(path, program)
    problems += check_eval(path, program)
    problems += check_rm(path, program)
    if kind == "sh":
        problems += check_bashisms(path, program)
    problems += check_backticks(path, program)
    if not is_kiwi(path):
        problems += check_system_writes(path, program, lines)
    return [downgrade(p) for p in problems]


def downgrade(problem: Problem) -> Problem:
    """Turn a known old violation (see LEGACY) into a warning."""
    if (problem.path, problem.rule) in LEGACY:
        return Problem(
            problem.path, problem.message, warning=True, rule=problem.rule, line=problem.line
        )
    return problem


def check_files(root: Path, paths: Sequence[str]) -> list[Problem]:
    """Check the scripts at ``paths`` (from ``root``)."""
    problems: list[Problem] = []
    for rel in paths:
        try:
            text = (root / rel).read_bytes().decode("utf-8")
        except OSError as err:
            problems.append(Problem(rel, f"cannot read the file: {err.strerror or err}"))
            continue
        except UnicodeDecodeError:
            problems.append(Problem(rel, "the file is not valid UTF-8", rule="encoding"))
            continue
        problems += check_script(rel, text)
    return sorted(problems, key=lambda p: (p.path, p.line, p.message))


def tracked_scripts(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.sh"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p.endswith(".sh"))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    usage = "usage: python3 tools/check_shell.py [SCRIPT ...]"
    options = [a for a in args if a.startswith("-")]
    if any(o not in ("-h", "--help") for o in options):
        print(usage, file=sys.stderr)
        return 2
    if options:
        print(usage, file=sys.stderr)
        return 0
    try:
        tracked = tracked_scripts(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    only = [a.replace("\\", "/") for a in args]
    unknown = [a for a in only if a not in tracked]
    if unknown:
        print(f"not a tracked shell script: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if not tracked:
        print("no tracked shell scripts found", file=sys.stderr)
        return 2
    checked = only or tracked
    problems = check_files(ROOT, checked)
    for problem in problems:
        print(problem, file=sys.stderr)
    kiwi = [p for p in checked if is_kiwi(p)]
    if kiwi:
        print("KIWI scripts (a missing strict mode is only a warning): " + ", ".join(kiwi))
    errors = sum(not p.warning for p in problems)
    warnings = len(problems) - errors
    print(f"{len(checked)} shell scripts checked: {errors} errors, {warnings} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
