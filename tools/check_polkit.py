#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the tracked polkit files of the repo: ``*.policy`` (XML) and ``*.rules``.

A ``.policy`` file is parsed with ``xml.etree.ElementTree``; for every ``<action>``:

* the id starts with ``org.vilna.`` and is unique in the whole repo;
* ``<description>`` and ``<message>`` exist in English (no ``xml:lang``);
* ``<defaults>`` has ``allow_any``, ``allow_inactive`` and ``allow_active``, each one of
  no, yes, auth_self, auth_admin, auth_self_keep, auth_admin_keep;
* translations (``xml:lang``): an unknown language code is an error; a missing language
  is a warning with the list (known: uk es pl de fr it ro cs lt ca ca@valencia gl eu pt;
  English is the element without ``xml:lang``);
* ``allow_any`` = yes is a warning: the action is open to anybody, with no session.

A ``.rules`` file is JavaScript, but it is never run: it is read as text by a small
tokenizer that knows comments, strings, template literals and regular expression
literals. For every file:

* every ``"org.vilna.…"`` string compared with ``action.id`` (``===``, ``==``, ``!==``,
  ``!=`` or a ``case`` of ``switch (action.id)``) names an action of some ``.policy``;
  an ``org.vilna.`` string used in another way is a warning (it cannot be checked);
* the brackets ``() {} []`` are balanced outside strings, comments and regex literals,
  and no string, comment, template or regex is left open;
* a ``return polkit.Result.YES`` without a look at ``subject.user``,
  ``subject.isInGroup`` or ``subject.groups`` (in an enclosing ``if``, or in an earlier
  ``if`` of the same function that returns something else) is a warning: a heuristic, not a proof.

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but do not
fail the check; they mark things the repo already does today.

Only ``git ls-files`` is run (to find the files). Nothing else is executed, nothing is
read from the network and nothing is written.

Run: python3 tools/check_polkit.py [FILE.policy | FILE.rules ...]
Without arguments every tracked ``*.policy`` and ``*.rules`` file is checked. With
arguments, the actions that the rules may name are those of the ``.policy`` files given.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import bisect
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

ACTION_PREFIX = "org.vilna."
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
# The 14 translated languages; English is the element without xml:lang.
LANGS = (
    "uk",
    "es",
    "pl",
    "de",
    "fr",
    "it",
    "ro",
    "cs",
    "lt",
    "ca",
    "ca@valencia",
    "gl",
    "eu",
    "pt",
)
TRANSLATED_TAGS = ("description", "message")
DEFAULTS_KEYS = ("allow_any", "allow_inactive", "allow_active")
AUTH_VALUES = ("no", "yes", "auth_self", "auth_admin", "auth_self_keep", "auth_admin_keep")
# Action ids the repo already has that do not start with org.vilna.: warnings, not errors.
# Fix the id (and whatever names it), then delete the line.
LEGACY_IDS = frozenset(
    {
        # The id of the app that owns the policy (pkexec /usr/bin/vilna-reportd).
        "io.github.stdenis1980.vilna-reportd.consent",
    }
)

# --- the .rules tokenizer ---------------------------------------------------

OPERATORS = ("===", "!==", "==", "!=", "<=", ">=", "=>", "&&", "||", "??", "++", "--", "...")
COMPARISONS = ("==", "===", "!=", "!==")
NAME = re.compile(r"[^\W\d][\w$]*|\$[\w$]*")
NUMBER = re.compile(r"\d[\w.]*")
# After one of these words a "/" starts a regular expression, not a division.
BEFORE_REGEX = frozenset(
    {"return", "typeof", "case", "in", "of", "void", "delete", "throw", "new", "else", "do"}
)
OPENERS = {"(": ")", "[": "]", "{": "}"}
GUARD_FIELDS = ("user", "isInGroup", "groups")


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False

    def __str__(self) -> str:
        return f"{self.path}: {'warning: ' if self.warning else ''}{self.message}"


@dataclass(frozen=True)
class Token:
    kind: str  # name, num, str (text without quotes), template, regex, punct
    text: str
    line: int


class Scanner:
    """Cut a JavaScript text into tokens. Nothing is evaluated."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.tokens: list[Token] = []
        self.problems: list[tuple[int, str]] = []
        self.newlines = [i for i, ch in enumerate(text) if ch == "\n"]

    def line_at(self, pos: int) -> int:
        return bisect.bisect_left(self.newlines, pos) + 1

    def add(self, kind: str, text: str, start: int) -> None:
        self.tokens.append(Token(kind, text, self.line_at(start)))

    def regex_allowed(self) -> bool:
        if not self.tokens:
            return True
        last = self.tokens[-1]
        if last.kind == "name":
            return last.text in BEFORE_REGEX
        if last.kind == "punct":
            return last.text not in (")", "]", "}")
        return False

    def run(self, in_template: bool = False) -> bool:
        """Read to the end, or inside ``${ ... }`` to its closing brace (True if found)."""
        depth = 0
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch.isspace():
                self.pos += 1
            elif self.text.startswith("//", self.pos):
                end = self.text.find("\n", self.pos)
                self.pos = len(self.text) if end < 0 else end
            elif self.text.startswith("/*", self.pos):
                self.skip_block_comment()
            elif ch in "\"'":
                self.read_string(ch)
            elif ch == "`":
                self.read_template()
            elif ch == "/" and self.regex_allowed():
                self.read_regex()
            elif ch == "}" and in_template and depth == 0:
                self.pos += 1
                return True
            else:
                depth += {"{": 1, "}": -1}.get(self.read_other(), 0)
        return False

    def skip_block_comment(self) -> None:
        end = self.text.find("*/", self.pos + 2)
        if end < 0:
            self.problems.append((self.line_at(self.pos), "comment /* is never closed"))
            self.pos = len(self.text)
        else:
            self.pos = end + 2

    def read_string(self, quote: str) -> None:
        start = self.pos
        self.pos += 1
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch == quote:
                self.add("str", self.text[start + 1 : self.pos], start)
                self.pos += 1
                return
            if ch == "\n":
                break
            self.pos += 2 if ch == "\\" else 1
        self.problems.append((self.line_at(start), f"string {quote}... is not closed"))
        self.add("str", self.text[start + 1 : min(self.pos, len(self.text))], start)

    def read_template(self) -> None:
        start = self.pos
        self.pos += 1
        while self.pos < len(self.text):
            if self.text.startswith("${", self.pos):
                self.pos += 2
                if not self.run(in_template=True):
                    break
            elif self.text[self.pos] == "`":
                self.pos += 1
                self.add("template", "", start)
                return
            else:
                self.pos += 2 if self.text[self.pos] == "\\" else 1
        self.problems.append((self.line_at(start), "template literal ` is never closed"))

    def read_regex(self) -> None:
        start = self.pos
        self.pos += 1
        in_class = False
        while self.pos < len(self.text) and self.text[self.pos] != "\n":
            ch = self.text[self.pos]
            if ch == "/" and not in_class:
                self.pos += 1
                while self.pos < len(self.text) and self.text[self.pos].isalpha():
                    self.pos += 1
                self.add("regex", self.text[start : self.pos], start)
                return
            in_class = (ch == "[") or (in_class and ch != "]")
            self.pos += 2 if ch == "\\" else 1
        self.pos = min(self.pos, len(self.text))
        self.problems.append((self.line_at(start), "regular expression /... is not closed"))

    def read_other(self) -> str:
        """A name, a number, an operator or one punctuation character."""
        start = self.pos
        for pattern, kind in ((NAME, "name"), (NUMBER, "num")):
            match = pattern.match(self.text, start)
            if match:
                self.pos = match.end()
                self.add(kind, match.group(), start)
                return match.group()
        operator = next((op for op in OPERATORS if self.text.startswith(op, start)), None)
        text = operator or self.text[start]
        self.pos += len(text)
        self.add("punct", text, start)
        return text


def tokenize(text: str) -> tuple[list[Token], list[tuple[int, str]]]:
    scanner = Scanner(text)
    scanner.run()
    return scanner.tokens, scanner.problems


def check_brackets(tokens: Sequence[Token]) -> list[tuple[int, str]]:
    problems: list[tuple[int, str]] = []
    stack: list[Token] = []
    closers = set(OPENERS.values())
    for token in tokens:
        if token.kind != "punct":
            continue
        if token.text in OPENERS:
            stack.append(token)
        elif token.text in closers:
            if not stack:
                problems.append((token.line, f"unexpected '{token.text}' with nothing open"))
            elif OPENERS[stack[-1].text] != token.text:
                problems.append(
                    (
                        token.line,
                        f"'{token.text}' does not match '{stack[-1].text}' opened on line "
                        f"{stack[-1].line}",
                    )
                )
                stack.pop()
            else:
                stack.pop()
    problems += [(t.line, f"'{t.text}' is never closed") for t in stack]
    return problems


# --- the .rules checks ------------------------------------------------------


def texts(tokens: Sequence[Token]) -> list[str]:
    return [t.text for t in tokens]


def is_action_id_comparison(tokens: Sequence[Token], i: int) -> bool:
    before = texts(tokens[max(0, i - 4) : i])
    after = texts(tokens[i + 1 : i + 5])
    return (
        len(before) == 4 and before[:3] == ["action", ".", "id"] and before[3] in COMPARISONS
    ) or (len(after) == 4 and after[0] in COMPARISONS and after[1:] == ["action", ".", "id"])


def is_case_of_action_switch(tokens: Sequence[Token], i: int) -> bool:
    if i == 0 or tokens[i - 1].text != "case":
        return False
    for j in range(i - 1, -1, -1):
        if tokens[j].text == "switch":
            return texts(tokens[j + 1 : j + 6]) == ["(", "action", ".", "id", ")"]
    return False


def check_action_names(path: str, tokens: Sequence[Token], known: set[str]) -> list[Problem]:
    problems = []
    for i, token in enumerate(tokens):
        if token.kind != "str" or not token.text.startswith(ACTION_PREFIX):
            continue
        if is_action_id_comparison(tokens, i) or is_case_of_action_switch(tokens, i):
            if token.text not in known:
                problems.append(
                    Problem(
                        path,
                        f"line {token.line}: the rule names the action '{token.text}', "
                        "but no .policy defines it",
                    )
                )
        else:
            problems.append(
                Problem(
                    path,
                    f"line {token.line}: '{token.text}' is not compared with action.id, "
                    "so it is not checked against the .policy files",
                    warning=True,
                )
            )
    return problems


def match_pairs(tokens: Sequence[Token]) -> dict[int, int]:
    """Index of each opening bracket -> index of its closing one (balanced input only)."""
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for i, token in enumerate(tokens):
        if token.kind != "punct":
            continue
        if token.text in OPENERS:
            stack.append(i)
        elif token.text in OPENERS.values() and stack:
            pairs[stack.pop()] = i
    return pairs


def mentions_guard(tokens: Sequence[Token]) -> bool:
    """True if the tokens read ``subject.user``, ``subject.isInGroup`` or ``subject.groups``."""
    names = texts(tokens)
    return any(
        names[i : i + 2] == ["subject", "."] and names[i + 2] in GUARD_FIELDS
        for i in range(len(names) - 2)
    )


def paren_before(
    tokens: Sequence[Token], openers: dict[int, int], brace: int
) -> int | None:
    """Index of the ``(`` of ``if (...) {`` for the brace at ``brace``; None for other blocks.
    ``openers`` maps each closing bracket to its opening one."""
    if brace == 0 or tokens[brace - 1].text != ")":
        return None
    opening = openers.get(brace - 1)
    if opening is not None and opening > 0 and tokens[opening - 1].text == "if":
        return opening
    return None


def guarded_by_header(
    tokens: Sequence[Token], pairs: dict[int, int], openers: dict[int, int], brace: int
) -> bool:
    opening = paren_before(tokens, openers, brace)
    return opening is not None and mentions_guard(tokens[opening : pairs[opening]])


def statement_end(tokens: Sequence[Token], pairs: dict[int, int], start: int) -> int:
    """Index of the last token of the statement at ``start``: its block or up to ``;``."""
    if start >= len(tokens):
        return len(tokens) - 1
    if tokens[start].text == "{" and start in pairs:
        return pairs[start]
    end = start
    while end < len(tokens) - 1 and tokens[end].text != ";":
        end += 1
    return end


def returns_something_but_yes(tokens: Sequence[Token]) -> bool:
    """True if the statement has a ``return`` that is not ``return polkit.Result.YES``."""
    names = texts(tokens)
    return any(
        name == "return" and names[i + 1 : i + 6] != ["polkit", ".", "Result", ".", "YES"]
        for i, name in enumerate(names)
    )


def guarded_by_earlier_if(
    tokens: Sequence[Token], pairs: dict[int, int], start: int, end: int
) -> bool:
    """An ``if`` between ``start`` and ``end``, on the block's own level, that returns."""
    i = start + 1
    while i < end:
        if tokens[i].text in OPENERS and i in pairs:
            if tokens[i].text == "(" and tokens[i - 1].text == "if":
                body = pairs[i] + 1
                inside = tokens[body : statement_end(tokens, pairs, body) + 1]
                if mentions_guard(tokens[i : pairs[i]]) and returns_something_but_yes(inside):
                    return True
            i = pairs[i]
        i += 1
    return False


def check_yes_returns(path: str, tokens: Sequence[Token]) -> list[Problem]:
    """Heuristic: ``return polkit.Result.YES`` should follow a check of user or group."""
    pairs = match_pairs(tokens)
    openers = {c: o for o, c in pairs.items()}
    problems = []
    wanted = ["return", "polkit", ".", "Result", ".", "YES"]
    # The braces open at each token, kept while walking once (asking every pair for every
    # return took quadratic time).
    open_braces: list[int] = []
    for i in range(len(tokens)):
        if tokens[i].text == "{" and i in pairs:
            open_braces.append(i)
        elif open_braces and i == pairs[open_braces[-1]]:
            open_braces.pop()
        if texts(tokens[i : i + len(wanted)]) != wanted:
            continue
        blocks = open_braces[::-1]  # innermost first
        guarded = any(
            guarded_by_header(tokens, pairs, openers, o)
            or guarded_by_earlier_if(tokens, pairs, o, i)
            for o in blocks
        )
        if not guarded:
            problems.append(
                Problem(
                    path,
                    f"line {tokens[i].line}: return polkit.Result.YES without a check of "
                    "subject.user, subject.isInGroup or subject.groups before it",
                    warning=True,
                )
            )
    return problems


def check_rules_text(path: str, text: str, known: set[str]) -> list[Problem]:
    """All problems of one .rules file; ``known`` are the action ids of the .policy files."""
    tokens, syntax = tokenize(text)
    syntax += check_brackets(tokens)
    problems = [
        Problem(path, f"line {line}: {message}")
        for line, message in sorted(syntax, key=lambda p: p[0])
    ]
    problems += check_action_names(path, tokens, known)
    if not syntax:
        problems += check_yes_returns(path, tokens)
    return problems


# --- the .policy checks -----------------------------------------------------


def check_action_id(path: str, action: ET.Element) -> list[Problem]:
    action_id = action.get("id", "").strip()
    if not action_id:
        return [Problem(path, "an <action> has no id")]
    if action_id.startswith(ACTION_PREFIX):
        return []
    legacy = action_id in LEGACY_IDS
    return [Problem(path, f"action {action_id}: the id must start with {ACTION_PREFIX}", legacy)]


def check_english(path: str, label: str, action: ET.Element) -> list[Problem]:
    problems = []
    for tag in TRANSLATED_TAGS:
        plain = [e for e in action.findall(tag) if XML_LANG not in e.attrib]
        if not plain:
            problems.append(Problem(path, f"{label}: missing <{tag}> in English"))
        elif not any((e.text or "").strip() for e in plain):
            problems.append(Problem(path, f"{label}: the English <{tag}> is empty"))
    return problems


def check_defaults(path: str, label: str, action: ET.Element) -> list[Problem]:
    defaults = action.find("defaults")
    if defaults is None:
        return [Problem(path, f"{label}: missing <defaults>")]
    problems = []
    for key in DEFAULTS_KEYS:
        element = defaults.find(key)
        value = "" if element is None else (element.text or "").strip()
        if element is None:
            problems.append(Problem(path, f"{label}: <defaults> has no <{key}>"))
        elif value not in AUTH_VALUES:
            problems.append(
                Problem(path, f"{label}: <{key}> is '{value}', not one of {' '.join(AUTH_VALUES)}")
            )
        elif key == "allow_any" and value == "yes":
            problems.append(
                Problem(
                    path,
                    f"{label}: allow_any is yes: anybody may do it, with no session at all",
                    warning=True,
                )
            )
    return problems


def check_translations(path: str, label: str, action: ET.Element) -> list[Problem]:
    problems = []
    reported: set[tuple[str, str]] = set()
    for element in action.iter():
        lang = element.get(XML_LANG)
        if lang is not None and lang not in LANGS and (element.tag, lang) not in reported:
            reported.add((element.tag, lang))
            problems.append(
                Problem(path, f"{label}: <{element.tag}> has unknown xml:lang='{lang}'")
            )
    for tag in TRANSLATED_TAGS:
        have = {e.get(XML_LANG) for e in action.findall(tag)}
        missing = [lang for lang in LANGS if lang not in have]
        if missing:
            message = f"{label}: <{tag}> is not translated into: {' '.join(missing)}"
            problems.append(Problem(path, message, warning=True))
    return problems


def check_policy_text(path: str, text: str) -> tuple[list[str], list[Problem]]:
    """The action ids of one .policy file and all its problems."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as err:
        return [], [Problem(path, f"not well-formed XML: {err}")]
    if root.tag != "policyconfig":
        return [], [Problem(path, f"the root element is <{root.tag}>, not <policyconfig>")]
    ids: list[str] = []
    problems: list[Problem] = []
    for action in root.findall("action"):
        problems += check_action_id(path, action)
        label = f"action {action.get('id', '?')}"
        ids.append(action.get("id", "").strip())
        problems += check_english(path, label, action)
        problems += check_defaults(path, label, action)
        problems += check_translations(path, label, action)
    return [i for i in ids if i], problems


# --- files ------------------------------------------------------------------


def read_text(root: Path, rel: str) -> tuple[str | None, list[Problem]]:
    """The text of a file; a byte order mark is one clear error, then ignored."""
    try:
        text = (root / rel).read_bytes().decode("utf-8")
    except OSError as err:
        return None, [Problem(rel, f"cannot read the file: {err.strerror or err}")]
    except UnicodeDecodeError:
        return None, [Problem(rel, "the file is not valid UTF-8")]
    if text.startswith("\ufeff"):
        return text[1:], [Problem(rel, "the file starts with a byte order mark (BOM)")]
    return text, []


def check_files(root: Path, paths: Sequence[str]) -> list[Problem]:
    """Check the given .policy and .rules files (paths relative to ``root`` or absolute)."""
    problems: list[Problem] = []
    owners: dict[str, str] = {}
    rules: list[tuple[str, str]] = []
    for rel in sorted(paths):
        text, found = read_text(root, rel)
        problems += found
        if text is None:
            continue
        if rel.endswith(".rules"):
            rules.append((rel, text))
            continue
        ids, found = check_policy_text(rel, text)
        problems += found
        for action_id in ids:
            if action_id in owners:
                problems.append(
                    Problem(rel, f"action {action_id}: id already used in {owners[action_id]}")
                )
            else:
                owners[action_id] = rel
    for rel, text in rules:
        problems += check_rules_text(rel, text, set(owners))
    return problems


def tracked_polkit_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.policy", "*.rules"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        errors="surrogateescape",
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    usage = "usage: python3 tools/check_polkit.py [FILE.policy | FILE.rules ...]"
    if any(a.startswith("-") for a in args):
        print(usage, file=sys.stderr)
        return 0 if args in (["-h"], ["--help"]) else 2
    if any(not a.endswith((".policy", ".rules")) for a in args):
        print(f"every file must be a .policy or a .rules file\n{usage}", file=sys.stderr)
        return 2
    if args:
        paths = args
    else:
        try:
            paths = tracked_polkit_files(ROOT)
        except (OSError, subprocess.CalledProcessError) as err:
            print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
            return 2
    if not paths:
        print("no tracked *.policy or *.rules files found", file=sys.stderr)
        return 2
    problems = check_files(Path.cwd() if args else ROOT, paths)
    for problem in problems:
        print(problem, file=sys.stderr)
    errors = sum(not p.warning for p in problems)
    print(f"{len(paths)} polkit files checked: {errors} errors, {len(problems) - errors} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    # A file name that is not UTF-8 is printed escaped, not as a traceback.
    sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    sys.exit(main())
