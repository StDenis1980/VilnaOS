#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Look for secrets in the tracked files of the repo: keys, tokens, passwords.

Every tracked text file is read line by line and checked for:

* ``github-token``: a GitHub token with a known prefix (``ghp_``, ``gho_``, ``ghu_``,
  ``ghs_``, ``ghr_``, ``github_pat_``);
* ``api-key``: an ``sk-`` key (OpenAI style, also ``sk-proj-`` and ``sk-ant-``);
* ``aws-key``: an AWS access key id (``AKIA`` or ``ASIA`` and 16 more characters);
* ``slack-token``: a Slack token (``xoxb-``, ``xoxp-`` and the other ``xox?-`` kinds);
* ``private-key``: the first line of a PEM private key (``-----BEGIN ... PRIVATE KEY-----``);
* ``secret-assignment``: an assignment to a name that contains ``token``, ``password``,
  ``passwd``, ``secret`` or ``api_key`` (``name = "value"``, ``name: value``, ``NAME=value``,
  ``"name": "value"``) where the value looks random: at least 12 characters, no spaces,
  not a path, a variable or a placeholder, and a Shannon entropy of at least 3.5 bits per
  character (3.0 with three kinds of characters when the value is shorter than 20). In
  source code (``.py .rs .js .ts .c .h .go .java``) only a quoted value counts; elsewhere an
  unquoted value followed by ``(`` or ``[`` is a call or an index, not a value. Names that end
  in ``file``, ``path``, ``url``, ``name``, ``env``, ``id`` and the like are not looked at.

A value is allowed when it is a test value with an explicit marker: ``FAKE``,
``EXAMPLE`` or ``TEST`` (any case) inside the value, or as a word that starts on the same
line (for a PEM key: in the first line or in the lines up to ``-----END``).

The output never repeats a value. A message names the rule, the known prefix at most, the
length of the value and a short id of its hash (the first 8 hex digits of SHA-256), for
example ``line 12: github-token: ghp_... (40 characters, id 1a2b3c4d)``.

Known old findings are listed in ``LEGACY`` by file, rule and that id. They are warnings
(``path: warning: ...``); anything else is an error and makes the exit code 1. A ``LEGACY``
entry that nothing matches any more is a warning too: delete it.

Files that are not scanned: symbolic links and anything that is not a regular file,
binary files (a NUL byte in the first 8 KiB; UTF-16 text with a byte order mark is read)
and files above 4 MiB (a warning names them). Only ``git ls-files`` is run; nothing else
is executed, nothing is read from the network and nothing is written.

Run: python3 tools/check_secrets.py [FILE ...]
Without arguments every tracked file is checked; with arguments, those files (any path).
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

MAX_BYTES = 4 * 1024 * 1024
BINARY_PROBE = 8192
MAX_VALUE = 512
PEM_BODY_LINES = 80
CODE_SUFFIXES = frozenset({".py", ".rs", ".js", ".ts", ".c", ".h", ".go", ".java"})

# --- known old findings -------------------------------------------------------
# (path from the repo root, rule, id from the message) -> why it is not a real secret.
# They are warnings, so that the check can run in CI; fix the file, then delete the line.
LEGACY: dict[tuple[str, str, str], str] = {
    # A cut-off PGP header and "..." in a test of the Flatpak repo signing: no key body.
    ("gloxi/tests/test_flatpak_repo.py", "private-key", "8a1ddaa7"): "header only, no body",
    # A made-up key shape that the secret-detector benchmark must find; it opens nothing.
    (
        "privacy-gateway/bench/pg_bench/data/placeholder_tasks.toml",
        "api-key",
        "54975052",
    ): "a placeholder task of the benchmark, not a live key",
    # Made-up keys in the detector tests (the same value on two lines).
    ("privacy-gateway/tests/test_detectors.py", "api-key", "8856c5dc"): "made-up test key",
    # The header repeated 8000 times to test the detector on long input.
    ("privacy-gateway/tests/test_detectors.py", "private-key", "3021d90e"): "header only",
}

# --- the rules ----------------------------------------------------------------

BOUNDARY = r"(?<![A-Za-z0-9_])"
GITHUB_TOKEN = re.compile(
    BOUNDARY + r"(?P<prefix>gh[pousr]_|github_pat_)(?P<body>[A-Za-z0-9_]{30,255})"
)
API_KEY = re.compile(r"(?<![A-Za-z0-9_-])(?P<prefix>sk-)(?P<body>[A-Za-z0-9_-]{20,255})")
AWS_KEY = re.compile(
    BOUNDARY.replace("_", "") + r"(?P<prefix>AKIA|ASIA)(?P<body>[A-Z2-7]{16})(?![A-Z0-9])"
)
SLACK_TOKEN = re.compile(BOUNDARY + r"(?P<prefix>xox[a-z]-)(?P<body>[A-Za-z0-9-]{10,255})")
PEM_HEADER = re.compile(r"-----BEGIN (?:[A-Z0-9]+ ){0,3}PRIVATE KEY(?: BLOCK)?-----")
PEM_END = "-----END"
# A line of a PEM body: base64, or a header line such as "Proc-Type: 4,ENCRYPTED".
PEM_BODY = re.compile(r"[A-Za-z0-9+/=]{16,}|[A-Za-z][A-Za-z0-9-]*: .*")

_VALUE_LIMIT = str(MAX_VALUE)
ASSIGNMENT = re.compile(
    r"(?<![\w.-])(?P<name>[\w.-]+)[\"']?"
    r"(?:\s*:\s*[A-Za-z_][\w.|\[\], ]{0,40})?"
    r"\s*(?::=|=(?!=)|:)\s*"
    r"(?:\"(?P<dq>[^\"\n]{1,N})\"|'(?P<sq>[^'\n]{1,N})'"
    r"|(?P<raw>[^\s\"'#;,()\[\]}\\]{8,N})(?P<after>.?))".replace("N", _VALUE_LIMIT)
)
NAME_WORDS = re.compile(r"token|password|passwd|secret|api[_-]?key", re.IGNORECASE)
# A name that ends in one of these holds a path, a label or a setting, not a secret.
HARMLESS_ENDINGS = frozenset(
    {
        *("file", "path", "dir", "url", "uri", "name", "env", "var", "command", "cmd", "label"),
        *("prompt", "title", "text", "hint", "help", "length", "len", "type", "kind", "count"),
        *("max", "min", "field", "header", "placeholder", "desc", "description", "id"),
    }
)
MARKER = re.compile(r"(?<![A-Za-z0-9])(?:fake|example|test)", re.IGNORECASE)
MARKER_IN_VALUE = re.compile(r"fake|example|test", re.IGNORECASE)
PLACEHOLDER = re.compile(r"\$|\{\{|\$\{|<[^>]*>|%\(|%s|^\{|^\[|^@|^\*+$|^x+$|^\.+$", re.IGNORECASE)

RULES = (
    "github-token",
    "api-key",
    "aws-key",
    "slack-token",
    "private-key",
    "secret-assignment",
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    shown: str  # never the whole value
    length: int
    ident: str
    warning: bool = False
    note: str = ""

    def __str__(self) -> str:
        kind = "warning: " if self.warning else ""
        where = f"line {self.line}: " if self.line else ""
        if self.note:
            return f"{self.path}: {kind}{where}{self.note}"
        return (
            f"{self.path}: {kind}{where}{self.rule}: {self.shown} "
            f"({self.length} characters, id {self.ident})"
        )


def entropy(value: str) -> float:
    """Shannon entropy in bits per character."""
    if not value:
        return 0.0
    total = len(value)
    return -sum(n / total * math.log2(n / total) for n in Counter(value).values())


def ident_of(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]


def char_kinds(value: str) -> int:
    return sum(
        (
            any(c.islower() for c in value),
            any(c.isupper() for c in value),
            any(c.isdigit() for c in value),
            any(not c.isalnum() for c in value),
        )
    )


def has_marker(line: str, value: str = "") -> bool:
    return bool(MARKER_IN_VALUE.search(value) or MARKER.search(line))


def looks_random(value: str) -> bool:
    """True when a value read after ``token =`` and the like looks like a real secret."""
    if len(value) < 12 or len(value) > MAX_VALUE or any(c.isspace() for c in value):
        return False
    if PLACEHOLDER.search(value) or "://" in value or value[0] in "/~":
        return False
    if value.startswith("./") or value.startswith("../"):
        return False
    kinds = char_kinds(value)
    if not any(c.isdigit() for c in value) and kinds < 3:
        return False  # words and snake_case names
    bits = entropy(value)
    if len(value) >= 20:
        return bits >= 3.5 and (kinds >= 3 or any(c.isdigit() for c in value))
    return kinds >= 3 and bits >= 3.0


def name_matches(name: str) -> bool:
    if not NAME_WORDS.search(name):
        return False
    last = re.split(r"[_.\-]", name.lower())[-1]
    return last not in HARMLESS_ENDINGS


def scan_prefixed(rule: str, pattern: re.Pattern[str], line: str) -> Iterable[tuple[str, str]]:
    """Yield (known prefix, whole token) for the matches of a prefix rule."""
    for found in pattern.finditer(line):
        whole = found.group(0)
        body = found.group("body")
        if rule == "api-key" and not (any(c.isdigit() for c in body) and entropy(body) >= 3.0):
            continue  # a hyphenated word such as a CSS class
        yield found.group("prefix"), whole


def check_text(path: str, text: str) -> list[Finding]:
    """Findings of one file (no LEGACY applied). Pure: no files are read."""
    lines = text.split("\n")
    suffix = Path(path).suffix.lower()
    out: list[Finding] = []
    seen: set[tuple[int, str, str]] = set()
    prefixed: list[str] = []  # the tokens of the prefix rules on the current line

    def add(number: int, rule: str, shown: str, value: str) -> None:
        key = (number, rule, value)
        if key not in seen:
            seen.add(key)
            out.append(Finding(path, number, rule, shown, len(value), ident_of(value)))

    for index, raw in enumerate(lines):
        number = index + 1
        line = raw.rstrip("\r")
        prefixed.clear()
        for rule, pattern in (
            ("github-token", GITHUB_TOKEN),
            ("api-key", API_KEY),
            ("aws-key", AWS_KEY),
            ("slack-token", SLACK_TOKEN),
        ):
            for prefix, whole in scan_prefixed(rule, pattern, line):
                prefixed.append(whole)
                if not has_marker(line, whole):
                    add(number, rule, prefix + "...", whole)
        header = PEM_HEADER.search(line)
        if header:
            block = [line]
            for follow in lines[index + 1 : index + 1 + PEM_BODY_LINES]:
                follow = follow.rstrip("\r")
                if PEM_END in follow:
                    block.append(follow)
                    break
                if follow.strip() and not PEM_BODY.fullmatch(follow.strip()):
                    break
                block.append(follow)
            if not any(MARKER.search(item) for item in block):
                first = block[1].strip() if len(block) > 1 else ""
                body = first if PEM_BODY.fullmatch(first) else ""
                add(
                    number, "private-key", "-----BEGIN ... PRIVATE KEY-----", header.group(0) + body
                )
        for found in ASSIGNMENT.finditer(line):
            name = found.group("name")
            quoted = found.group("dq") is not None or found.group("sq") is not None
            value = found.group("dq") or found.group("sq") or found.group("raw") or ""
            if not quoted and (suffix in CODE_SUFFIXES or found.group("after") in ("(", "[")):
                continue  # a variable, or a call such as secrets.token_urlsafe(32)
            if not name_matches(name) or not looks_random(value):
                continue
            if any(token in value or value in token for token in prefixed):
                continue  # already reported by a prefix rule
            if has_marker(line, value):
                continue
            add(number, "secret-assignment", f"{name} = ...", value)
    return out


def apply_legacy(findings: list[Finding], whole_repo: bool) -> list[Finding]:
    """Turn the findings listed in LEGACY into warnings; report entries nothing matches."""
    result: list[Finding] = []
    used: set[tuple[str, str, str]] = set()
    for item in findings:
        key = (item.path, item.rule, item.ident)
        if key in LEGACY:
            used.add(key)
            item = Finding(
                item.path, item.line, item.rule, item.shown, item.length, item.ident, True
            )
        result.append(item)
    if whole_repo:
        for path, rule, ident in sorted(set(LEGACY) - used):
            note = f"LEGACY entry {rule} id {ident} matches nothing: delete it"
            result.append(Finding(path, 0, rule, "", 0, ident, True, note))
    return result


# --- reading files --------------------------------------------------------------


def read_text(path: Path) -> tuple[str | None, str]:
    """(text, reason): the text of a file, or None and why it is not scanned."""
    try:
        info = os.lstat(path)
    except OSError as err:
        return None, f"cannot read: {err.strerror or err}"
    if stat.S_ISLNK(info.st_mode):
        return None, "symlink"
    if not stat.S_ISREG(info.st_mode):
        return None, "not a regular file"
    if info.st_size > MAX_BYTES:
        return None, "too large"
    try:
        data = path.read_bytes()
    except OSError as err:
        return None, f"cannot read: {err.strerror or err}"
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace"), ""
    if b"\0" in data[:BINARY_PROBE]:
        return None, "binary"
    return data.decode("utf-8", errors="replace"), ""


def check_files(root: Path, paths: Sequence[str]) -> tuple[list[Finding], dict[str, int]]:
    findings: list[Finding] = []
    stats = {"files": 0, "scanned": 0, "binary": 0, "links": 0, "large": 0}
    for rel in paths:
        stats["files"] += 1
        text, reason = read_text(root / rel)
        if text is not None:
            stats["scanned"] += 1
            findings += check_text(rel, text)
        elif reason == "binary":
            stats["binary"] += 1
        elif reason in ("symlink", "not a regular file"):
            stats["links"] += 1
        elif reason == "too large":
            stats["large"] += 1
            note = f"larger than {MAX_BYTES // (1024 * 1024)} MiB: not scanned for secrets"
            findings.append(Finding(rel, 0, "too-large", "", 0, "", True, note))
        else:
            findings.append(Finding(rel, 0, "unreadable", "", 0, "", False, reason))
    return findings, stats


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, text=True
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    usage = "usage: python3 tools/check_secrets.py [FILE ...]"
    if any(a.startswith("-") for a in args):
        print(usage, file=sys.stderr)
        return 0 if args in (["-h"], ["--help"]) else 2
    if args:
        base = Path.cwd()
        paths = args
        missing = [a for a in args if not os.path.lexists(base / a)]
        if missing:
            print(f"no such file: {', '.join(missing)}", file=sys.stderr)
            return 2
    else:
        base = ROOT
        try:
            paths = tracked_files(ROOT)
        except (OSError, subprocess.CalledProcessError) as err:
            print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
            return 2
    findings, stats = check_files(base, paths)
    findings = apply_legacy(findings, whole_repo=not args)
    for item in findings:
        print(item, file=sys.stderr)
    errors = sum(not f.warning for f in findings)
    print(
        f"{stats['files']} files ({stats['scanned']} scanned, {stats['binary']} binary, "
        f"{stats['links']} links or special, {stats['large']} too large): "
        f"{errors} errors, {len(findings) - errors} warnings"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
