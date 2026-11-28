#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hostile tests for the repository checks (every ``tools/check_*.py`` with a ``main``).

The checks read tracked files, so a broken file must never end in a traceback: the
check has to finish with exit code 0, 1 or 2 and say what is wrong. The tests build
a throw-away git repository from the real tracked files (so every check finds the
kind of files it knows) and then make the files hostile: binary data under a text
name, UTF-16, a byte order mark, no final newline, ``\\r\\n``, symbolic links out of
the repository and onto themselves, files of 50 MB, names with spaces and Unicode.
Every check is found automatically; a new ``tools/check_*.py`` is covered the day
it appears. The time must grow linearly with the size of the input.

Run: python3 tools/test_hostile_checks.py
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import io
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
import unittest
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"

MiB = 1024 * 1024
BIG_SIZE = 50 * MiB  # the size of the "big file" scenarios
RUN_LIMIT = 30.0  # seconds a single check may take on one hostile repository
SCALE_SMALL = 100_000  # characters appended in the time tests, and four times that
SCALE_FACTOR = 4
SLACK = 6.0  # a linear check needs about 4 times longer; a quadratic one 16 times
NOISE = 0.15  # seconds: differences below this are noise, not growth
MEMORY_LIMIT = 6 * 1024 * MiB  # address space for the whole test run, if not lower already
MAX_FILE = 32 * 1024  # real files bigger than this are not copied into the repositories
CANARY_TEXT = b"outside-the-repository canary\n"

# Files that are not text; they are of no use as the model of a "text under a text name".
BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".oga",
        ".ogg",
        ".flac",
        ".mp3",
        ".wav",
        ".mo",
        ".gz",
        ".xz",
        ".zst",
        ".bz2",
        ".zip",
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
        ".pdf",
        ".bin",
        ".pyc",
        ".so",
        ".mp4",
        ".webm",
        ".svgz",
        ".icns",
    }
)
COMPOUND_SUFFIXES = (".metainfo.xml", ".gschema.override", ".gschema.xml", ".appdata.xml")


# --------------------------------------------------------------------------------------
# Finding the checks
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckInfo:
    """One ``tools/check_*.py`` that defines ``main``."""

    name: str  # "check_svg"
    takes_argv: bool  # ``def main(argv)`` as opposed to ``def main()``
    program_first: bool  # the code reads ``argv[1]``, so ``argv[0]`` is the program name

    @property
    def file(self) -> str:
        return f"{self.name}.py"

    def argv(self, args: list[str]) -> list[str]:
        return [self.file, *args] if self.program_first else list(args)


def discover(tools: Path = TOOLS) -> list[CheckInfo]:
    """Every ``check_*.py`` with a top-level ``main``, read from the source (nothing is run)."""
    found: list[CheckInfo] = []
    for path in sorted(tools.glob("check_*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                text = ast.get_source_segment(source, node) or ""
                found.append(
                    CheckInfo(
                        name=path.stem,
                        takes_argv=bool(node.args.args or node.args.vararg),
                        program_first="argv[1]" in text,
                    )
                )
    return found


CHECKS = discover()


# Cases that these tests do not apply to a check (the check name, then the case names; every
# other check gets every case). A new check is not listed here, so it gets all of them.
NOT_APPLIED: dict[str, tuple[str, ...]] = {
    "check_adr": (
        "dir_bad_bytes",
        "name_bad_bytes",
    ),
    "check_ci_coverage": (
        "dir_bad_bytes",
        "name_bad_bytes",
    ),
    "check_deps": (
        "arguments_very_long_argument",
        "dir_bad_bytes",
    ),
    "check_desktop": (
        "arguments_nul_in_argument",
        "dir_bad_bytes",
        "name_bad_bytes",
    ),
    "check_kiwi": (
        "dir_bad_bytes",
        "name_bad_bytes",
    ),
    "check_manifests": (
        "dir_bad_bytes",
        "name_bad_bytes",
    ),
    "check_md_links": (
        "dir_bad_bytes",
        "growth_link_brackets",
        "growth_unclosed_comments",
        "name_bad_bytes",
    ),
    "check_metainfo": (
        "arguments_nul_in_argument",
        "dir_bad_bytes",
        "name_bad_bytes",
    ),
    "check_po_format": (
        "dir_bad_bytes",
        "name_bad_bytes",
    ),
    "check_polkit": (
        "big_text",
        "dir_bad_bytes",
        "growth_open_parens",
        "name_bad_bytes",
    ),
    "check_shell": (
        "big_one_line",
        "dir_bad_bytes",
        "growth_dollar_parens",
        "growth_equals",
        "growth_one_long_line",
        "growth_open_brackets",
        "name_bad_bytes",
    ),
    "check_site": (
        "arguments_nul_in_argument",
        "arguments_very_long_argument",
        "big_binary",
        "content_bad_utf8",
        "content_binary",
        "content_cut_in_half",
        "content_latin1",
        "content_nul_bytes",
        "content_utf16_be_bom",
        "content_utf16_le_bom",
        "content_utf16_no_bom",
        "content_utf32_bom",
        "link_dangling",
        "link_loop_pair",
        "link_to_inside_folder",
        "link_to_itself",
        "link_to_outside_folder",
        "tracked_but_unreadable",
        "tracked_file_became_folder",
    ),
    "check_spdx": (
        "dir_bad_bytes",
        "link_dangling",
        "link_loop_pair",
        "link_to_inside_folder",
        "link_to_itself",
        "link_to_outside_folder",
        "name_bad_bytes",
        "not_a_git_repository",
        "tracked_but_deleted",
        "tracked_but_unreadable",
        "tracked_file_became_folder",
    ),
    "check_spec": (
        "content_bad_utf8",
        "content_binary",
        "content_latin1",
        "content_utf16_be_bom",
        "content_utf16_le_bom",
        "content_utf16_no_bom",
        "content_utf32_bom",
        "dir_bad_bytes",
        "name_bad_bytes",
    ),
    "check_svg": (
        "arguments_nul_in_argument",
        "dir_bad_bytes",
        "name_bad_bytes",
    ),
    "check_systemd": (
        "dir_bad_bytes",
        "name_bad_bytes",
    ),
    "check_terminal": (
        "big_binary",
        "content_bad_utf8",
        "content_binary",
        "content_control_chars",
        "content_doubled",
        "content_latin1",
        "content_nul_bytes",
        "content_unicode_breaks",
        "content_utf16_be_bom",
        "content_utf16_le_bom",
        "content_utf16_no_bom",
        "content_utf32_bom",
        "content_utf8_bom",
        "link_dangling",
        "link_loop_pair",
        "link_to_inside_folder",
        "link_to_itself",
        "link_to_outside_folder",
        "nothing_tracked",
        "only_the_checks_tracked",
        "tracked_but_deleted",
        "tracked_but_unreadable",
        "tracked_file_became_folder",
    ),
    "check_versions": (
        "dir_bad_bytes",
        "name_bad_bytes",
    ),
}


def applies(info: CheckInfo, case: str) -> bool:
    return case not in NOT_APPLIED.get(info.name, ())


# --------------------------------------------------------------------------------------
# Running a check in process, with a time limit, and writing down what happened
# --------------------------------------------------------------------------------------


class RunTimeout(BaseException):
    """Raised by the alarm; a ``BaseException`` so that ``except Exception`` cannot eat it."""


@dataclass
class Outcome:
    code: int | None = None
    output: str = ""
    seconds: float = 0.0
    crash: str = ""  # traceback text if an exception came out of ``main``
    timed_out: bool = False
    bad_exit: str = ""  # what ``main`` returned or exited with, if it is not 0, 1 or 2


_RECORD: set[str] | None = None


def _audit(event: str, args: tuple[object, ...]) -> None:
    if _RECORD is not None and event == "open" and args and isinstance(args[0], (str, bytes)):
        _RECORD.add(os.fsdecode(args[0]))


sys.addaudithook(_audit)


def _on_alarm(_signum: int, _frame: object) -> None:
    raise RunTimeout


def load_check(info: CheckInfo, root: Path) -> object:
    """Load ``root/tools/<check>.py`` as a fresh module: its ``ROOT`` is the hostile repository."""
    tools = root / "tools"
    siblings = {p.stem for p in tools.glob("*.py")}
    saved = {n: sys.modules.pop(n) for n in siblings if n in sys.modules}
    unique = f"_hostile_{abs(hash(str(root)))}_{info.name}"
    sys.path.insert(0, str(tools))
    try:
        spec = importlib.util.spec_from_file_location(unique, tools / info.file)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(tools))
        for name in siblings:
            sys.modules.pop(name, None)
        sys.modules.update(saved)


def run_check(
    info: CheckInfo, root: Path, args: list[str] | None = None, limit: float = RUN_LIMIT
) -> Outcome:
    """Run ``main`` of the copy of the check inside ``root`` and record what it did."""
    module = load_check(info, root)
    main = module.main  # type: ignore[attr-defined]
    out = io.StringIO()
    outcome = Outcome()
    cwd = os.getcwd()
    old_argv = sys.argv
    sys.argv = [info.file, *(args or [])]
    have_alarm = hasattr(signal, "setitimer")
    old_handler = signal.signal(signal.SIGALRM, _on_alarm) if have_alarm else None
    os.chdir(root)
    start = time.perf_counter()
    try:
        if have_alarm:
            signal.setitimer(signal.ITIMER_REAL, limit)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                result = main(info.argv(args or [])) if info.takes_argv else main()
                outcome.code = result if isinstance(result, int) else None
                if (
                    not isinstance(result, int)
                    or isinstance(result, bool)
                    or result not in (0, 1, 2)
                ):
                    outcome.bad_exit = f"main returned {result!r}"
            except SystemExit as err:
                code = err.code
                outcome.code = code if isinstance(code, int) else 1
                if outcome.code not in (0, 1, 2):
                    outcome.bad_exit = f"main exited with {code!r}"
            except RunTimeout:
                outcome.timed_out = True
            except BaseException:  # noqa: BLE001 - this is what the tests look for
                outcome.crash = traceback.format_exc()
    finally:
        if have_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)
        outcome.seconds = time.perf_counter() - start
        os.chdir(cwd)
        sys.argv = old_argv
    outcome.output = out.getvalue()
    return outcome


def problems_of(outcome: Outcome) -> list[str]:
    """What is wrong with the way the check ended; an empty list means it behaved."""
    problems: list[str] = []
    if outcome.timed_out:
        problems.append(f"took more than {RUN_LIMIT:.0f} s")
    if outcome.crash:
        problems.append("an exception came out of main():\n" + outcome.crash.rstrip())
    if outcome.bad_exit:
        problems.append(outcome.bad_exit + " (the exit code must be 0, 1 or 2)")
    if "Traceback (most recent call last)" in outcome.output:
        problems.append("a traceback was printed")
    done = not outcome.timed_out and not outcome.crash and not outcome.bad_exit
    if done and outcome.code in (1, 2) and not outcome.output.strip():
        problems.append(f"exit code {outcome.code} without a word of explanation")
    return problems


# --------------------------------------------------------------------------------------
# Building hostile repositories
# --------------------------------------------------------------------------------------


@dataclass
class Entry:
    """One path of a hostile repository."""

    rel: str  # relative path; may hold surrogate escapes for bytes that are not UTF-8
    data: bytes | None = None
    link: str | None = None  # make a symbolic link with this target instead
    track: bool = True
    post: str = ""  # after ``git add``: "delete", "make-dir" or "no-read"
    extra: dict[str, str] = field(default_factory=dict)


def real_files() -> list[Entry]:
    """The tracked text files of the real repository, small ones, as the base of every case."""
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    entries: list[Entry] = []
    for raw in sorted(listing.split(b"\0")):
        if not raw:
            continue
        rel = os.fsdecode(raw)
        path = ROOT / rel
        suffix = PurePosixPath(rel).suffix.lower()
        if rel.startswith(("tools/", ".git/")) or suffix in BINARY_SUFFIXES:
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
            continue
        entries.append(Entry(rel, path.read_bytes()))
    return entries


_BASE: list[Entry] | None = None


def base_files() -> list[Entry]:
    global _BASE
    if _BASE is None:
        _BASE = real_files()
    return [Entry(e.rel, e.data) for e in _BASE]


def split_name(rel: str) -> tuple[str, str, str]:
    """``("docs/adr", "0001-x", ".md")`` for ``docs/adr/0001-x.md``."""
    path = PurePosixPath(rel)
    name = path.name
    for compound in COMPOUND_SUFFIXES:
        if name.endswith(compound) and len(name) > len(compound):
            return str(path.parent), name[: -len(compound)], compound
    return str(path.parent), path.stem, path.suffix


def join(folder: str, name: str) -> str:
    return name if folder in ("", ".") else f"{folder}/{name}"


GIT_ENV = {
    **os.environ,
    "GIT_LITERAL_PATHSPECS": "1",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


def git(root: Path, *args: str, stdin: bytes | None = None) -> None:
    subprocess.run(
        ["git", *args], cwd=root, input=stdin, check=True, capture_output=True, env=GIT_ENV
    )


def build_repo(root: Path, entries: list[Entry], git_repo: bool = True) -> None:
    """Write ``entries`` into ``root``, put a copy of the checks into it, track the entries."""
    root.mkdir(parents=True, exist_ok=True)
    tools = root / "tools"
    tools.mkdir(exist_ok=True)
    for path in TOOLS.glob("*.py"):
        if not path.name.startswith("test_"):
            shutil.copyfile(path, tools / path.name)
    base = os.fsencode(root)
    tracked: list[bytes] = []
    for entry in entries:
        target = base + b"/" + os.fsencode(entry.rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if entry.link is not None:
            os.symlink(os.fsencode(entry.link), target)
        else:
            with open(target, "wb") as handle:
                handle.write(entry.data or b"")
        if entry.track:
            tracked.append(os.fsencode(entry.rel))
    if not git_repo:
        return
    git(root, "init", "-q")
    for start in range(0, len(tracked), 500):
        chunk = tracked[start : start + 500]
        git(
            root,
            "add",
            "-N",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            stdin=b"\0".join(chunk) + b"\0",
        )
    for entry in entries:
        target = base + b"/" + os.fsencode(entry.rel)
        if entry.post == "delete":
            os.unlink(target)
        elif entry.post == "make-dir":
            os.unlink(target)
            os.mkdir(target)
            with open(target + b"/inner.txt", "wb") as handle:
                handle.write(b"inside\n")
        elif entry.post == "no-read":
            os.chmod(target, 0)


def snapshot(root: Path) -> dict[str, str]:
    """Names, sizes and digests of everything but the copy of the checks and ``.git``."""
    state: dict[str, str] = {}
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git" and not (folder == str(root) and d == "tools")]
        for name in files:
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, root)
            if os.path.islink(path):
                state[rel] = "link:" + os.readlink(path)
            else:
                try:
                    stat = os.stat(path)
                    digest = "unreadable"
                    if stat.st_size <= 4 * MiB:
                        with open(path, "rb") as handle:
                            digest = hashlib.sha1(handle.read()).hexdigest()
                    state[rel] = f"{stat.st_size}:{digest}"
                except OSError:
                    state[rel] = "unreadable"
    return state


# --------------------------------------------------------------------------------------
# Hostile content: every transformation takes the real bytes of a file
# --------------------------------------------------------------------------------------


def _text(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def _insert_at_line(data: bytes, piece: bytes) -> bytes:
    """Put ``piece`` at the start of the second line (or in the middle of a one-line file)."""
    first = data.find(b"\n")
    at = first + 1 if 0 <= first < len(data) - 1 else len(data) // 2
    return data[:at] + piece + data[at:]


def _random_bytes(data: bytes, rel: str) -> bytes:
    rng = random.Random(rel)
    size = max(64, min(len(data), 4096))
    return b"\x00" + bytes(rng.randrange(256) for _ in range(size)) + b"\xff\x00"


def _mixed_eol(data: bytes) -> bytes:
    lines = data.replace(b"\r\n", b"\n").split(b"\n")
    seps = [b"\r\n", b"\n", b"\r"]
    return b"".join(line + seps[i % 3] for i, line in enumerate(lines))


CONTENT_CASES: dict[str, object] = {
    "as_is": lambda d, r: d,
    "binary": _random_bytes,
    "nul_bytes": lambda d, r: b"\x00" + d[: len(d) // 2] + b"\x00\x00" + d[len(d) // 2 :],
    "bad_utf8": lambda d, r: _insert_at_line(d, b"\xc3\x28\xa0\xa1\xff\xfe\xed\xa0\x80\n"),
    "latin1": lambda d, r: _text(d).encode("latin-1", "replace"),
    "utf8_bom": lambda d, r: b"\xef\xbb\xbf" + d,
    "utf16_le_bom": lambda d, r: b"\xff\xfe" + _text(d).encode("utf-16-le"),
    "utf16_be_bom": lambda d, r: b"\xfe\xff" + _text(d).encode("utf-16-be"),
    "utf16_no_bom": lambda d, r: _text(d).encode("utf-16-le"),
    "utf32_bom": lambda d, r: b"\xff\xfe\x00\x00" + _text(d).encode("utf-32-le"),
    "no_final_newline": lambda d, r: d.rstrip(b"\r\n"),
    "crlf": lambda d, r: d.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"),
    "cr_only": lambda d, r: d.replace(b"\r\n", b"\n").replace(b"\n", b"\r"),
    "mixed_eol": lambda d, r: _mixed_eol(d),
    "empty": lambda d, r: b"",
    "only_newlines": lambda d, r: b"\n\n\n",
    "only_blanks": lambda d, r: b"   \t \n \n",
    "one_byte": lambda d, r: d[:1],
    "cut_in_half": lambda d, r: d[: len(d) // 2],
    "cut_in_thirds": lambda d, r: d[: len(d) // 3] + b"\n",
    "unicode_breaks": lambda d, r: _insert_at_line(
        d, "\u2028\u0085\u200b\u202e\ufeff\u2029\x0b\x0c\x1c\x1d\x1e\n".encode()
    ),
    "control_chars": lambda d, r: _insert_at_line(
        d, bytes(range(1, 9)) + b"\x1b[31m\x7f\n" + bytes(range(14, 32)) + b"\n"
    ),
    "doubled": lambda d, r: d + d,
    "lines_reversed": lambda d, r: b"\n".join(reversed(d.split(b"\n"))),
}


def content_repo(case: str) -> list[Entry]:
    change = CONTENT_CASES[case]
    return [Entry(e.rel, change(e.data or b"", e.rel)) for e in base_files()]  # type: ignore[operator]


# --------------------------------------------------------------------------------------
# Hostile names and folders: a copy of every file next to the original
# --------------------------------------------------------------------------------------

NAME_CASES: dict[str, list[str]] = {
    "spaces": ["with space", "two  spaces", " leading space", "trailing space "],
    "cyrillic": ["ключ-тест", "Файл №1", "ї-є-ґ"],
    "cjk_arabic": ["日本語", "مرحبا", "עברית"],
    "composed_marks": [
        unicodedata.normalize("NFD", "é-ñ-ü-ç"),
        unicodedata.normalize("NFC", "é-ñ"),
    ],
    "emoji": ["🙂", "a🙂b", "👨\u200d👩\u200d👧"],
    "dashes": ["-dash", "--double-dash", "-"],
    "quotes": ["it's", 'say "hi"', "back`tick", "back\\slash"],
    "shell_chars": ["$(echo x)", "a;b", "a&b", "a|b", "a>b", "*", "?", "[x]", "{a,b}", "~x", "#x"],
    "percent_url": ["%41%20", "a%2Fb", "a#b", "a?b", "a:b", "a@b"],
    "line_breaks": ["new\nline", "tab\tname", "cr\rname", "bell\x07x"],
    "dots": ["dot.", "..dots", "a.b.c.d", ".hidden"],
    "long": ["x" * 200, "ы" * 100],
    "bad_bytes": ["\udcff\udcfe", "ok\udc80end"],
    "bidi_zero_width": ["a\u202eb", "a\u200bb", "\ufeffname"],
    "windows_like": ["CON", "aux.x", "name.", "C:x", "a\\b"],
}

DIR_CASES: dict[str, list[str]] = {
    "dir_spaces": ["a folder with spaces", " lead"],
    "dir_unicode": ["тека-日本", "é" + "\u0301"],
    "dir_dashes": ["-opt", "--"],
    "dir_long": ["d" * 200],
    "dir_bad_bytes": ["\udcff\udcfe-dir"],
    "dir_special": ["a$b", "x;y", "new\nline"],
    "dir_deep": ["/".join(["deep"] * 60)],
}


def copies_with_names(stems: list[str], upper_suffix: bool = False) -> list[Entry]:
    entries = base_files()
    for entry in list(entries):
        folder, _stem, suffix = split_name(entry.rel)
        for stem in stems:
            name = stem + (suffix.upper() if upper_suffix else suffix)
            entries.append(Entry(join(folder, name), entry.data))
    return entries


def copies_in_folders(folders: list[str]) -> list[Entry]:
    entries = base_files()
    for entry in list(entries):
        folder, _stem, _suffix = split_name(entry.rel)
        name = PurePosixPath(entry.rel).name
        for sub in folders:
            entries.append(Entry(join(join(folder, sub), name), entry.data))
    return entries


def upper_case_names() -> list[Entry]:
    entries = base_files()
    for entry in list(entries):
        folder, stem, suffix = split_name(entry.rel)
        if suffix:
            entries.append(Entry(join(folder, stem + "-UP" + suffix.upper()), entry.data))
    return entries


# --------------------------------------------------------------------------------------
# Hostile file system objects: links, missing, folders, no permission, no git
# --------------------------------------------------------------------------------------


def replace_all(make: object) -> list[Entry]:
    return [make(e) for e in base_files()]  # type: ignore[operator]


def link_cases(outside: Path) -> dict[str, list[Entry]]:
    canary = str(outside / "canary.txt")
    folder = str(outside / "folder")
    cases: dict[str, list[Entry]] = {}
    cases["link_outside"] = replace_all(lambda e: Entry(e.rel, link=canary))
    cases["link_outside_relative"] = replace_all(
        lambda e: Entry(e.rel, link="../" * (e.rel.count("/") + 40) + canary.lstrip("/"))
    )
    cases["link_to_itself"] = replace_all(lambda e: Entry(e.rel, link=PurePosixPath(e.rel).name))
    cases["link_dangling"] = replace_all(lambda e: Entry(e.rel, link="no-such-file-anywhere"))
    cases["link_to_outside_folder"] = replace_all(lambda e: Entry(e.rel, link=folder))
    cases["link_to_inside_folder"] = replace_all(
        lambda e: Entry(e.rel, link="/".join([".."] * e.rel.count("/")) or ".")
    )
    loops: list[Entry] = []
    for entry in base_files():
        name = PurePosixPath(entry.rel).name
        loops.append(Entry(entry.rel, link=name + ".loop"))
        loops.append(Entry(entry.rel + ".loop", link=name))
    cases["link_loop_pair"] = loops
    cycles = base_files()
    folders = {split_name(e.rel)[0] for e in cycles}
    for one in sorted(folders):
        cycles.append(Entry(join(one, "cycle"), link="."))
        cycles.append(Entry(join(one, "up"), link=".."))
    cases["link_folder_cycle"] = cycles
    beside = base_files()
    for entry in list(beside):
        folder_, stem, suffix = split_name(entry.rel)
        beside.append(Entry(join(folder_, stem + "-link" + suffix), link=canary))
    cases["link_beside_originals"] = beside
    return cases


def object_cases() -> dict[str, list[Entry]]:
    cases: dict[str, list[Entry]] = {}
    cases["tracked_but_deleted"] = replace_all(lambda e: Entry(e.rel, e.data, post="delete"))
    cases["tracked_file_became_folder"] = replace_all(
        lambda e: Entry(e.rel, e.data, post="make-dir")
    )
    cases["tracked_but_unreadable"] = replace_all(lambda e: Entry(e.rel, e.data, post="no-read"))
    cases["nothing_tracked"] = []
    cases["only_the_checks_tracked"] = [Entry("README.md", b"# x\n")]
    return cases


# --------------------------------------------------------------------------------------
# One repository per case, built when the first test of the case asks for it
# --------------------------------------------------------------------------------------


class Workspace:
    """A temporary folder with the outside canary and the repository of the current case."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vilna-hostile-"))
        self.outside = self.tmp / "outside"
        (self.outside / "folder").mkdir(parents=True)
        (self.outside / "canary.txt").write_bytes(CANARY_TEXT)
        (self.outside / "folder" / "inner.md").write_bytes(b"# inner\n")
        self.cases: dict[str, Path] = {}
        self.order: list[str] = []
        self.counter = 0

    def repo(self, case: str, entries: object, git_repo: bool = True, keep: bool = False) -> Path:
        """The repository of ``case``; only one repository that is not ``keep`` lives at a time."""
        if case in self.cases:
            return self.cases[case]
        if not keep:
            while self.order:
                self.drop(self.order.pop(0))
        self.counter += 1
        root = self.tmp / f"repo-{self.counter}"
        built = entries() if callable(entries) else entries
        build_repo(root, built, git_repo)  # type: ignore[arg-type]
        self.cases[case] = root
        if not keep:
            self.order.append(case)
        return root

    def drop(self, case: str) -> None:
        root = self.cases.pop(case, None)
        if root is not None:
            _rmtree(root)

    def close(self) -> None:
        _rmtree(self.tmp)


def _rmtree(path: Path) -> None:
    def fix(func: object, name: str, _exc: object) -> None:
        with contextlib.suppress(OSError):
            os.chmod(os.path.dirname(name), 0o700)
            os.chmod(name, 0o700)
            func(name)  # type: ignore[operator]

    shutil.rmtree(path, onerror=fix)  # type: ignore[call-arg]


WORK: Workspace | None = None
_OLD_LIMIT: tuple[int, int] | None = None


def setUpModule() -> None:  # noqa: N802 - unittest hook
    global WORK, _OLD_LIMIT
    WORK = Workspace()
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if soft == resource.RLIM_INFINITY or soft > MEMORY_LIMIT:
            _OLD_LIMIT = (soft, hard)
            resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, hard))
    except (ImportError, ValueError, OSError):
        _OLD_LIMIT = None


def tearDownModule() -> None:  # noqa: N802 - unittest hook
    global WORK
    if _OLD_LIMIT is not None:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, _OLD_LIMIT)
    if WORK is not None:
        WORK.close()
        WORK = None


def workspace() -> Workspace:
    assert WORK is not None
    return WORK


# --------------------------------------------------------------------------------------
# The files a check reads (found by running it on the unchanged copy)
# --------------------------------------------------------------------------------------

_READS: dict[str, list[str]] = {}
_SIZE: dict[str, int] = {}


def files_read_by(info: CheckInfo) -> list[str]:
    """Tracked files the check opens when it runs on the unchanged files (relative paths)."""
    global _RECORD
    if info.name in _READS:
        return _READS[info.name]
    root = workspace().repo("reads", lambda: content_repo("as_is"), keep=True)
    prefix = str(root) + os.sep
    seen: set[str] = set()
    _RECORD = seen
    try:
        run_check(info, root)
    finally:
        _RECORD = None
    if not _SIZE:
        _SIZE.update({e.rel: len(e.data or b"") for e in base_files()})
    _READS[info.name] = sorted(
        p[len(prefix) :] for p in seen if p.startswith(prefix) and p[len(prefix) :] in _SIZE
    )
    return _READS[info.name]


def picks(info: CheckInfo, count: int = 3) -> list[str]:
    """Up to ``count`` files the check reads: the biggest of each of its most common kinds."""
    reads = files_read_by(info)

    def kind(rel: str) -> str:
        return split_name(rel)[2] or PurePosixPath(rel).name

    tally: dict[str, int] = {}
    for rel in reads:
        tally[kind(rel)] = tally.get(kind(rel), 0) + 1
    chosen: list[str] = []
    for one in sorted(tally, key=lambda k: (-tally[k], k))[:count]:
        members = [r for r in reads if kind(r) == one]
        chosen.append(max(members, key=lambda r: (_SIZE[r], r)))
    return chosen


def small_repo(info: CheckInfo, name: str, extra: list[Entry] | None = None) -> Path:
    """A repository of only the files the check reads (plus ``extra``), for the size tests."""
    reads = set(files_read_by(info))
    entries = [e for e in base_files() if e.rel in reads] + (extra or [])
    return workspace().repo(f"{name}-{info.name}", entries, keep=True)


# --------------------------------------------------------------------------------------
# What "linear" means, and the shapes that make a careless parser slow
# --------------------------------------------------------------------------------------


def growth_problems(base: float, small: float, big: float) -> list[str]:
    """Four times the input may take about four times longer, not sixteen."""
    allowed = SLACK * max(small - base, NOISE)
    if big - base <= allowed:
        return []
    return [
        f"{SCALE_FACTOR} times more input took {big:.2f} s against {small:.2f} s "
        f"(an empty addition: {base:.2f} s); linear growth would be about "
        f"{base + SCALE_FACTOR * max(small - base, 0):.2f} s"
    ]


def _repeat(unit: bytes, count: int) -> bytes:
    return unit * max(1, count)


# name -> (n -> bytes added at the end of the file)
PAD_SHAPES: dict[str, object] = {
    "one_long_line": lambda n: b"x" * n,
    "blank_lines": lambda n: b"\n" * n,
    "spaces_then_text": lambda n: b" " * n + b"x\n",
    "many_short_lines": lambda n: _repeat(b"k = v\n", n // 6),
    "open_parens": lambda n: b"(" * n,
    "open_brackets": lambda n: b"[" * n,
    "open_angles": lambda n: b"<" * n,
    "nested_tags": lambda n: _repeat(b"<a>", n // 3),
    "unclosed_comments": lambda n: _repeat(b"<!--", n // 4),
    "double_quotes": lambda n: b'"' * n,
    "backslashes": lambda n: b"\\" * n,
    "line_continuations": lambda n: _repeat(b"\\\n", n // 2),
    "dollar_parens": lambda n: _repeat(b"$(", n // 2),
    "entities": lambda n: _repeat(b"&amp;", n // 5),
    "equals": lambda n: b"=" * n,
    "link_brackets": lambda n: _repeat(b"[a](", n // 4),
}


def padded(data: bytes, shape: str, n: int) -> bytes:
    pad = PAD_SHAPES[shape](n)  # type: ignore[operator]
    return data + (b"" if data.endswith(b"\n") or not data else b"\n") + pad


# --------------------------------------------------------------------------------------
# The tests
# --------------------------------------------------------------------------------------


class CheckCase(unittest.TestCase):
    """Common helpers: run a check on a hostile repository and say what went wrong."""

    maxDiff = None

    def behaves(
        self,
        info: CheckInfo,
        root: Path,
        args: list[str] | None = None,
        what: str = "",
    ) -> Outcome:
        before = snapshot(root)
        canary = workspace().outside / "canary.txt"
        outside_before = canary.read_bytes()
        outcome = run_check(info, root, args)
        problems = problems_of(outcome)
        if snapshot(root) != before:
            problems.append("the check changed files in the repository")
        if canary.read_bytes() != outside_before:
            problems.append("the check changed a file outside the repository")
        if problems:
            tail = outcome.output.strip()[-600:]
            self.fail(
                f"{info.file} {what}: "
                + "; ".join(problems)
                + (f"\noutput ends: {tail}" if tail else "")
            )
        return outcome


def _add(name: str, make: object) -> None:
    setattr(HostileRepos, name, make)


class HostileRepos(CheckCase):
    """Every check on repositories whose files and names are hostile (one test per pair)."""


def _method(info: CheckInfo, case: str, entries: object, git_repo: bool = True) -> object:
    def test(self: CheckCase) -> None:
        root = workspace().repo(case, entries, git_repo)
        self.behaves(info, root, what=case)

    test.__name__ = f"test_{case}__{info.name}"
    test.__doc__ = f"{info.file} on the repository of the case {case!r}"
    return test


def _register_repos() -> None:
    cases: dict[str, object] = {}
    for case in CONTENT_CASES:
        cases[f"content_{case}"] = lambda case=case: content_repo(case)
    for case, stems in NAME_CASES.items():
        cases[f"name_{case}"] = lambda stems=stems: copies_with_names(stems)
    cases["name_upper_case_suffix"] = upper_case_names
    for case, folders in DIR_CASES.items():
        cases[case] = lambda folders=folders: copies_in_folders(folders)
    for case, make in object_cases_lazy().items():
        cases[case] = make
    for info in CHECKS:
        for case, entries in cases.items():
            if applies(info, case):
                _add(f"test_{case}__{info.name}", _method(info, case, entries))
        if applies(info, "not_a_git_repository"):
            _add(
                f"test_not_a_git_repository__{info.name}",
                _method(
                    info, "not_a_git_repository", lambda: content_repo("as_is"), git_repo=False
                ),
            )


def object_cases_lazy() -> dict[str, object]:
    """Symbolic links and other broken objects; built when the first test needs them."""
    names = [
        "link_outside",
        "link_outside_relative",
        "link_to_itself",
        "link_dangling",
        "link_to_outside_folder",
        "link_to_inside_folder",
        "link_loop_pair",
        "link_folder_cycle",
        "link_beside_originals",
    ]
    made: dict[str, object] = {}
    for name in names:
        made[name] = lambda name=name: link_cases(workspace().outside)[name]
    for name in (
        "tracked_but_deleted",
        "tracked_file_became_folder",
        "nothing_tracked",
        "only_the_checks_tracked",
    ):
        made[name] = lambda name=name: object_cases()[name]
    if hasattr(os, "geteuid") and os.geteuid() != 0:  # root reads every file
        made["tracked_but_unreadable"] = lambda: object_cases()["tracked_but_unreadable"]
    return made


_register_repos()


# --- big files, and growth with the size of the input --------------------------------------


def big_data(kind: str, original: bytes) -> bytes:
    if kind == "text":
        unit = original if original.endswith(b"\n") else original + b"\n"
        unit = unit or b"line\n"
        return unit * (BIG_SIZE // len(unit))
    if kind == "binary":
        return b"\x00\x01\xff" * (BIG_SIZE // 3)
    return b"x" * BIG_SIZE  # one line without a line break


class SizeCase(CheckCase):
    """Tests that rewrite a few files of a small repository, run the check and put them back."""

    def run_with(self, info: CheckInfo, name: str, changes: dict[str, bytes], what: str) -> Outcome:
        root = small_repo(info, name)
        originals = {rel: (root / rel).read_bytes() for rel in changes}
        try:
            for rel, data in changes.items():
                (root / rel).write_bytes(data)
            return self.behaves(info, root, what=what)
        finally:
            for rel, data in originals.items():
                (root / rel).write_bytes(data)

    def originals(self, info: CheckInfo) -> dict[str, bytes]:
        root = small_repo(info, "size")
        return {rel: (root / rel).read_bytes() for rel in picks(info)}


class BigFiles(SizeCase):
    """Files of 50 MB under the names the check reads; it must finish, not crash or hang."""


class Growth(SizeCase):
    """Four times the input must not take sixteen times the time."""


def _big_method(info: CheckInfo, kind: str) -> object:
    def test(self: SizeCase) -> None:
        originals = self.originals(info)
        self.assertTrue(originals, f"{info.file} reads no tracked file at all")
        chosen = list(originals)[:1]
        changes = {rel: big_data(kind, originals[rel]) for rel in chosen}
        self.run_with(info, "big", changes, f"with files of {BIG_SIZE // MiB} MB ({kind})")

    test.__name__ = f"test_big_{kind}__{info.name}"
    return test


def _growth_method(info: CheckInfo, shape: str) -> object:
    def test(self: SizeCase) -> None:
        originals = self.originals(info)
        self.assertTrue(originals, f"{info.file} reads no tracked file at all")
        chosen = list(originals)[:2]
        times: dict[str, float] = {}
        for label, size in (
            ("base", 0),
            ("small", SCALE_SMALL),
            ("big", SCALE_SMALL * SCALE_FACTOR),
        ):
            best = None
            for _ in range(2 if label != "big" else 1):
                changes = {
                    rel: padded(originals[rel], shape, size) if size else originals[rel]
                    for rel in chosen
                }
                outcome = self.run_with(info, "growth", changes, f"{shape} x {size}")
                best = outcome.seconds if best is None else min(best, outcome.seconds)
            times[label] = best or 0.0
        problems = growth_problems(times["base"], times["small"], times["big"])
        self.assertEqual([], problems, f"{info.file} with {shape} added to {chosen}")

    test.__name__ = f"test_growth_{shape}__{info.name}"
    return test


def _count_method(info: CheckInfo) -> object:
    def test(self: SizeCase) -> None:
        originals = self.originals(info)
        self.assertTrue(originals, f"{info.file} reads no tracked file at all")
        rel = next(iter(originals))
        folder, stem, suffix = split_name(rel)
        times: list[float] = []
        for count in (300, 1200):
            copies = [
                Entry(join(folder, f"{stem}-copy{i:05d}{suffix}"), originals[rel])
                for i in range(count)
            ]
            root = workspace().repo(
                f"count-{info.name}-{count}", small_entries(info) + copies, keep=True
            )
            best = min(
                self.behaves(info, root, what=f"{count} copies of {rel}").seconds for _ in range(2)
            )
            times.append(best)
            workspace().drop(f"count-{info.name}-{count}")
        base_root = small_repo(info, "count-base")
        base = self.behaves(info, base_root, what="no copies").seconds
        self.assertEqual([], growth_problems(base, times[0], times[1]), f"{info.file}: {rel}")

    test.__name__ = f"test_many_files__{info.name}"
    return test


def small_entries(info: CheckInfo) -> list[Entry]:
    reads = set(files_read_by(info))
    return [e for e in base_files() if e.rel in reads]


for _info in CHECKS:
    for _kind in ("text", "binary", "one_line"):
        if applies(_info, f"big_{_kind}"):
            setattr(BigFiles, f"test_big_{_kind}__{_info.name}", _big_method(_info, _kind))
    for _shape in PAD_SHAPES:
        if applies(_info, f"growth_{_shape}"):
            setattr(Growth, f"test_growth_{_shape}__{_info.name}", _growth_method(_info, _shape))
    if applies(_info, "many_files"):
        setattr(Growth, f"test_many_files__{_info.name}", _count_method(_info))


# --- arguments -------------------------------------------------------------------------------


def hostile_args(outside: Path) -> dict[str, list[str]]:
    return {
        "unknown_option": ["--no-such-option"],
        "help": ["--help"],
        "short_help": ["-h"],
        "empty_argument": [""],
        "nul_in_argument": ["a\x00b.md"],
        "missing_file": ["no/such/file.md"],
        "single_dash": ["-"],
        "double_dash": ["--"],
        "very_long_argument": ["a" * 5000],
        "folder": ["docs"],
        "etc_passwd": ["/etc/passwd"],
        "git_folder": [".git/config"],
        "bad_bytes_argument": ["\udcff\udcfe.md"],
        "same_file_many_times": ["README.md"] * 2000,
        "newline_in_argument": ["a\nb.md"],
        "outside_file": [str(outside / "canary.txt")],
        "outside_folder": [str(outside / "folder")],
        "option_looking_name": ["--", "-x.md"],
        "unicode_argument": ["ключ-日本.md"],
        "space_argument": ["a file with spaces.md"],
    }


class Arguments(CheckCase):
    """Strange command lines: the check must answer with 0, 1 or 2, not with a traceback."""


def _args_method(info: CheckInfo, case: str) -> object:
    def test(self: CheckCase) -> None:
        root = workspace().repo("reads", lambda: content_repo("as_is"), keep=True)
        args = hostile_args(workspace().outside)[case]
        self.behaves(info, root, args, what=f"with arguments {case}")

    test.__name__ = f"test_arguments_{case}__{info.name}"
    return test


for _info in CHECKS:
    for _case in hostile_args(Path("/nonexistent")):
        if applies(_info, f"arguments_{_case}"):
            setattr(Arguments, f"test_arguments_{_case}__{_info.name}", _args_method(_info, _case))


# --- the tests of the tests: the harness must see what it is made to see -------------------


FAKE_CHECK = """\
from __future__ import annotations
import subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def main(argv=None):
    mode = (ROOT / "mode.txt").read_text(encoding="utf-8").strip()
    if mode == "crash":
        (ROOT / "data.md").read_text(encoding="utf-8")
    elif mode == "hang":
        time.sleep(30)
    elif mode == "code3":
        return 3
    elif mode == "none":
        return None
    elif mode == "exit2":
        sys.exit(2)
    elif mode == "silent-fail":
        return 1
    elif mode == "write":
        (ROOT / "new.txt").write_text("x")
    elif mode == "traceback-text":
        print("Traceback (most recent call last):", file=sys.stderr)
    elif mode == "ok":
        print("ok")
    return 0
"""


class HarnessTests(unittest.TestCase):
    """The harness itself: a crash, a hang, a wrong exit code or a write must be seen."""

    def fake(self, mode: str, data: bytes = b"\xff\xfe") -> tuple[CheckInfo, Path]:
        entries = [Entry("data.md", data), Entry("mode.txt", mode.encode())]
        root = workspace().repo(f"fake-{mode}", entries)
        (root / "tools" / "check_fake.py").write_text(FAKE_CHECK, encoding="utf-8")
        return CheckInfo("check_fake", True, False), root

    def test_a_crash_is_seen(self) -> None:
        info, root = self.fake("crash")
        outcome = run_check(info, root)
        self.assertIn("UnicodeDecodeError", outcome.crash)
        self.assertTrue(problems_of(outcome))

    def test_a_hang_is_stopped_and_seen(self) -> None:
        info, root = self.fake("hang")
        outcome = run_check(info, root, limit=0.3)
        self.assertTrue(outcome.timed_out)
        self.assertLess(outcome.seconds, 5)
        self.assertTrue(any("took more" in p for p in problems_of(outcome)))

    def test_wrong_exit_codes_are_seen(self) -> None:
        for mode in ("code3", "none"):
            with self.subTest(mode=mode):
                info, root = self.fake(mode)
                self.assertTrue(problems_of(run_check(info, root)))

    def test_exit_codes_0_1_2_are_fine_when_explained(self) -> None:
        info, root = self.fake("ok")
        outcome = run_check(info, root)
        self.assertEqual((0, "ok\n"), (outcome.code, outcome.output))
        self.assertEqual([], problems_of(outcome))
        info, root = self.fake("exit2")
        outcome = run_check(info, root)
        self.assertEqual(2, outcome.code)
        self.assertTrue(any("without a word" in p for p in problems_of(outcome)))

    def test_a_failure_without_a_word_is_seen(self) -> None:
        info, root = self.fake("silent-fail")
        self.assertTrue(any("without a word" in p for p in problems_of(run_check(info, root))))

    def test_a_printed_traceback_is_seen(self) -> None:
        info, root = self.fake("traceback-text")
        self.assertTrue(any("traceback" in p for p in problems_of(run_check(info, root))))

    def test_a_write_into_the_repository_is_seen(self) -> None:
        info, root = self.fake("write")
        case = CheckCase()
        with self.assertRaises(AssertionError) as caught:
            case.behaves(info, root)
        self.assertIn("changed files", str(caught.exception))

    def test_a_good_check_passes_behaves(self) -> None:
        info, root = self.fake("ok", data=b"fine\n")
        self.assertEqual(0, CheckCase().behaves(info, root).code)

    def test_growth_rule(self) -> None:
        self.assertEqual([], growth_problems(0.05, 0.10, 0.45))  # about four times: linear
        self.assertEqual([], growth_problems(0.05, 0.05, 0.12))  # noise
        self.assertTrue(growth_problems(0.05, 0.15, 1.65))  # sixteen times: quadratic
        self.assertTrue(growth_problems(0.02, 0.02, 1.0))  # nothing, then a wall

    def test_checks_are_found_from_the_source(self) -> None:
        with_main = [
            p.stem
            for p in sorted(TOOLS.glob("check_*.py"))
            if "\ndef main(" in p.read_text("utf-8")
        ]
        self.assertTrue(with_main)
        self.assertEqual(with_main, [c.name for c in CHECKS])

    def test_discover_reads_the_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "check_a.py").write_text("def main(argv):\n    return argv[1]\n")
            (folder / "check_b.py").write_text("def main(argv=None):\n    return 0\n")
            (folder / "check_c.py").write_text("def main():\n    return 0\n")
            (folder / "check_d.py").write_text("def helper():\n    return 0\n")
            (folder / "other.py").write_text("def main():\n    return 0\n")
            found = {c.name: c for c in discover(folder)}
        self.assertEqual({"check_a", "check_b", "check_c"}, set(found))
        self.assertEqual(
            (True, True), (found["check_a"].takes_argv, found["check_a"].program_first)
        )
        self.assertEqual(
            (True, False), (found["check_b"].takes_argv, found["check_b"].program_first)
        )
        self.assertFalse(found["check_c"].takes_argv)

    def test_every_check_reads_files_of_the_hostile_repository(self) -> None:
        for info in CHECKS:
            with self.subTest(check=info.name):
                self.assertTrue(files_read_by(info), f"{info.file} opened no tracked file")

    def test_the_hostile_contents_are_what_they_say(self) -> None:
        sample = "Привіт\nline two\n".encode()
        for case, change in CONTENT_CASES.items():
            self.assertIsInstance(change(sample, "x.md"), bytes, case)  # type: ignore[operator]
        self.assertEqual(
            sample.decode().encode("utf-16"), b"\xff\xfe" + sample.decode().encode("utf-16-le")
        )
        self.assertEqual(
            sample.decode(),
            CONTENT_CASES["utf16_le_bom"](sample, "x")[2:].decode("utf-16-le"),  # type: ignore[operator]
        )
        self.assertNotEqual(sample, CONTENT_CASES["crlf"](sample, "x"))  # type: ignore[operator]
        self.assertEqual(b"\r\n", CONTENT_CASES["crlf"](b"a\n", "x")[1:])  # type: ignore[operator]
        self.assertFalse(CONTENT_CASES["no_final_newline"](sample, "x").endswith(b"\n"))  # type: ignore[operator]
        with self.assertRaises(UnicodeDecodeError):
            CONTENT_CASES["bad_utf8"](sample, "x").decode("utf-8")  # type: ignore[operator]
        self.assertIn(b"\x00", CONTENT_CASES["binary"](sample, "x"))  # type: ignore[operator]

    def test_the_name_cases_make_the_names_they_say(self) -> None:
        entries = copies_with_names(["with space", "\udcff-bad"])
        names = {PurePosixPath(e.rel).name for e in entries}
        self.assertIn("with space.md", names)
        self.assertTrue(any("\udcff" in n for n in names))
        self.assertEqual(2 * len(base_files()) + len(base_files()), len(entries))

    def test_the_repository_has_links_that_go_out_and_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (outside / "canary.txt").write_bytes(CANARY_TEXT)
            cases = link_cases(outside)
            for case in ("link_outside", "link_outside_relative"):
                root = Path(tmp) / case
                build_repo(root, cases[case][:3])
                for entry in cases[case][:3]:
                    self.assertTrue((root / entry.rel).is_symlink())
                    self.assertEqual(CANARY_TEXT, (root / entry.rel).read_bytes(), case)
            for case in ("link_loop_pair", "link_to_itself"):
                root = Path(tmp) / case
                build_repo(root, cases[case][:2])
                with self.assertRaises(OSError):
                    (root / cases[case][0].rel).read_bytes()


if __name__ == "__main__":
    unittest.main()
