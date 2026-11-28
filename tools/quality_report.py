#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build one Markdown quality report from every ``tools/check_*.py`` of the repo.

    python3 tools/quality_report.py                      # the report on stdout
    python3 tools/quality_report.py --output report.md   # ... into a file
    python3 tools/quality_report.py --previous old.md    # add a trend section
    python3 tools/quality_report.py --top 10 --timeout 300

The checks are found at run time (``check_*.py`` next to this file, or in
``--tools DIR``), so a new check shows up in the report with no change here.

How a check is run (imported, never as a subprocess):

* the source is parsed first (``ast``, not executed). A check is run only when it has a
  top-level ``def main`` that can be called with one argument or none, and when its
  top level only holds imports, definitions, assignments and an
  ``if __name__ == "__main__"`` guard. Anything else (a call or a loop at import time)
  could have side effects, so that file is listed under "Skipped" with the reason;
* the module is imported under a private name, ``main([])`` (or ``main()``) is called with
  stdout, stderr and stdin replaced by in-memory streams, ``sys.argv`` and the working
  folder fixed, and ``SystemExit`` caught. ``sys.path``, ``sys.modules``, the streams,
  ``sys.argv`` and the folder are restored afterwards, so the checks cannot leak into each
  other or into this tool;
* the counts come from the check's own summary line ("... N errors, M warnings"). A check
  with no such line is counted from stderr: lines with ``warning:`` are warnings, every
  other non-empty line is an error. A non-zero exit code is never "0 errors": at least one
  error is counted. An exception or a timeout is a failed check, not a traceback.

The report holds no dates, durations or paths of this machine, so the same repo gives the
same bytes. Only the checks are run: this tool starts no subprocess itself (the checks
run ``git ls-files`` on their own) and uses no network.

Exit code: 0 when no check has errors, 1 when at least one has (or crashed), 2 for a
usage or file problem.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import inspect
import io
import os
import re
import signal
import sys
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_TOP = 5
TOP_KINDS = 3  # most frequent kinds of warning shown per check
MAX_FAILURE_LINES = 10
KIND_WIDTH = 80

USAGE = (
    "usage: python3 tools/quality_report.py [--previous REPORT.md] [--output FILE] "
    "[--top N] [--timeout SECONDS] [--tools DIR]"
)

SUMMARY = re.compile(r"(\d+) errors?, (\d+) warnings?")
WARNING_MARK = re.compile(r"\bwarning:\s*")
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`")
NUMBER = re.compile(r"\d+")
LINE_PREFIX = re.compile(r"^line \d+:\s*")
TABLE_ROW = re.compile(r"^\| (check_[A-Za-z0-9_]+) \| ([A-Z]+) \| (\d+) \| (\d+) \|\s*$")

OK, WARN, FAIL, CRASH, TIMEOUT = "OK", "WARN", "FAIL", "CRASH", "TIMEOUT"
BAD_STATUSES = frozenset({FAIL, CRASH, TIMEOUT})

# Top-level statements that cannot do anything when the file is imported.
SAFE_NODES = (
    ast.Import,
    ast.ImportFrom,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Assign,
    ast.AnnAssign,
)


class CheckTimeout(BaseException):
    """Raised by the alarm; a BaseException, so a check cannot swallow it."""


@dataclass
class Result:
    name: str
    status: str
    errors: int = 0
    warnings: int = 0
    exit_code: int | None = None
    warning_lines: list[str] = field(default_factory=list)
    error_lines: list[str] = field(default_factory=list)
    note: str = ""


@dataclass(frozen=True)
class Skipped:
    name: str
    reason: str


# --- finding the checks -------------------------------------------------------------


def find_checks(tools: Path) -> list[Path]:
    """The ``check_*.py`` files of the folder, sorted by name. Symlinks are not followed."""
    try:
        entries = sorted(tools.glob("check_*.py"), key=lambda p: p.name)
    except OSError:
        return []
    return [p for p in entries if p.is_file() or p.is_symlink()]


def is_main_guard(node: ast.stmt) -> bool:
    """``if __name__ == "__main__":`` (or ``if TYPE_CHECKING:``): no effect on import."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
    )


def is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def runnable(path: Path) -> str | None:
    """None when the file may be imported and run; otherwise the reason it is skipped."""
    if path.is_symlink():
        return "is a symbolic link"
    try:
        source = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as err:
        return f"cannot read: {type(err).__name__}"
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError) as err:
        return f"cannot parse: {type(err).__name__}"
    main_def: ast.FunctionDef | None = None
    for node in tree.body:
        if is_docstring(node) or isinstance(node, SAFE_NODES) or is_main_guard(node):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                main_def = node
            continue
        return f"top level does something at import time (line {node.lineno})"
    if main_def is None:
        return "has no top-level function main"
    args = main_def.args
    required = len(args.posonlyargs) + len(args.args) - len(args.defaults)
    required += sum(1 for d in args.kw_defaults if d is None)
    if required > 1 or (args.kwonlyargs and any(d is None for d in args.kw_defaults)):
        return "main needs more than one argument"
    return None


# --- running one check --------------------------------------------------------------


def exit_code_of(code: object) -> tuple[int, str]:
    """The exit code of a SystemExit argument and the text it would print."""
    if code is None:
        return 0, ""
    if isinstance(code, int):
        return code, ""
    return 1, str(code)


def parse_counts(stdout: str, stderr: str, code: int) -> tuple[int, int, list[str], list[str]]:
    """Errors, warnings, the warning lines and the error lines of one finished check."""
    warning_lines: list[str] = []
    error_lines: list[str] = []
    for line in stderr.splitlines():
        if not line.strip():
            continue
        (warning_lines if WARNING_MARK.search(line) else error_lines).append(line.rstrip())
    found = SUMMARY.findall(stdout)
    if found:
        errors, warnings = int(found[-1][0]), int(found[-1][1])
    else:
        errors, warnings = len(error_lines), len(warning_lines)
    if code != 0 and errors == 0:
        errors = 1
    return errors, warnings, warning_lines, error_lines


def status_of(errors: int, warnings: int, code: int) -> str:
    if errors or code != 0:
        return FAIL
    return WARN if warnings else OK


def call_main(module: object, root: Path) -> int:
    main = module.main  # type: ignore[attr-defined]
    params = inspect.signature(main).parameters
    value = main([]) if params else main()
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"main returned {type(value).__name__}, not an int")
    return value


def run_check(path: Path, root: Path, timeout: float = 0.0) -> Result:
    """Import one check and run it with every side channel captured and restored."""
    name = path.stem
    module_name = f"_quality_report_check_{name}"
    out, err = io.StringIO(), io.StringIO()
    saved = {
        "path": list(sys.path),
        "argv": sys.argv,
        "cwd": os.getcwd(),
        "modules": set(sys.modules),
        "stdin": sys.stdin,
    }
    can_alarm = (
        timeout > 0
        and hasattr(signal, "setitimer")
        and threading.current_thread() is threading.main_thread()
    )
    old_handler = None
    code = 0
    crash = ""
    timed_out = False
    try:
        sys.path.insert(0, str(path.parent))
        sys.argv = [str(path)]
        sys.stdin = io.StringIO("")
        os.chdir(root)
        if can_alarm:

            def on_alarm(signum: int, frame: object) -> None:
                raise CheckTimeout

            old_handler = signal.signal(signal.SIGALRM, on_alarm)
            signal.setitimer(signal.ITIMER_REAL, timeout)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise ImportError("no import spec")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                code = call_main(module, root)
            except SystemExit as exit_request:
                code, text = exit_code_of(exit_request.code)
                if text:
                    print(text, file=sys.stderr)
            except CheckTimeout:
                timed_out = True
            except Exception as problem:
                crash = f"{type(problem).__name__}: {problem}".splitlines()[0][:200]
    except CheckTimeout:
        timed_out = True
    finally:
        if can_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if old_handler is not None:
                signal.signal(signal.SIGALRM, old_handler)
        sys.path[:] = saved["path"]
        sys.argv = saved["argv"]
        sys.stdin = saved["stdin"]
        os.chdir(saved["cwd"])
        for added in set(sys.modules) - saved["modules"]:
            del sys.modules[added]
    if timed_out:
        return Result(name, TIMEOUT, errors=1, note=f"stopped after {timeout:g} seconds")
    if crash:
        return Result(name, CRASH, errors=1, note=crash)
    errors, warnings, warning_lines, error_lines = parse_counts(
        out.getvalue(), err.getvalue(), code
    )
    return Result(
        name,
        status_of(errors, warnings, code),
        errors,
        warnings,
        code,
        warning_lines,
        error_lines,
        f"exit code {code}" if code != 0 else "",
    )


def run_all(tools: Path, root: Path, timeout: float = 0.0) -> tuple[list[Result], list[Skipped]]:
    results: list[Result] = []
    skipped: list[Skipped] = []
    for path in find_checks(tools):
        reason = runnable(path)
        if reason:
            skipped.append(Skipped(path.stem, reason))
        else:
            results.append(run_check(path, root, timeout))
    return results, skipped


# --- the report ---------------------------------------------------------------------


def kind_of(line: str) -> str:
    """A warning with the file, the line number and the particulars taken out."""
    match = WARNING_MARK.search(line)
    text = line[match.end() :] if match else line
    text = LINE_PREFIX.sub("", text.strip())
    text = QUOTED.sub("'…'", text)
    text = NUMBER.sub("N", text)
    return text if len(text) <= KIND_WIDTH else text[: KIND_WIDTH - 1] + "…"


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def frequent_kinds(result: Result) -> str:
    counts = Counter(kind_of(line) for line in result.warning_lines)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:TOP_KINDS]
    return "; ".join(f"{n}× {cell(text)}" for text, n in ranked) or "-"


def parse_previous(text: str) -> dict[str, tuple[str, int, int]]:
    """The rows of the "Checks" table of an earlier report: name -> (status, errors, warnings)."""
    rows: dict[str, tuple[str, int, int]] = {}
    for line in text.splitlines():
        match = TABLE_ROW.match(line)
        if match:
            rows.setdefault(match[1], (match[2], int(match[3]), int(match[4])))
    return rows


def delta(before: int, after: int) -> str:
    return f"{before} → {after} ({after - before:+d})" if before != after else str(after)


def trend_lines(results: list[Result], previous: dict[str, tuple[str, int, int]]) -> list[str]:
    current = {r.name: r for r in results}
    rows: list[str] = []
    for name in sorted(current.keys() | previous.keys()):
        now, before = current.get(name), previous.get(name)
        if now and before:
            if (now.status, now.errors, now.warnings) == before:
                continue
            rows.append(
                f"| {name} | {before[0]} → {now.status} | {delta(before[1], now.errors)} "
                f"| {delta(before[2], now.warnings)} |"
            )
        elif now:
            rows.append(f"| {name} | new ({now.status}) | {now.errors} | {now.warnings} |")
        elif before:
            rows.append(f"| {name} | removed (was {before[0]}) | - | - |")
    total_before = (sum(v[1] for v in previous.values()), sum(v[2] for v in previous.values()))
    total_now = (sum(r.errors for r in results), sum(r.warnings for r in results))
    lines = ["## Trend", ""]
    lines.append(
        f"Errors: {delta(total_before[0], total_now[0])}. "
        f"Warnings: {delta(total_before[1], total_now[1])}."
    )
    lines.append("")
    if rows:
        lines += ["| Check | Status | Errors | Warnings |", "| --- | --- | --- | --- |", *rows]
    else:
        lines.append("No check changed since the previous report.")
    lines.append("")
    return lines


def render(
    results: list[Result],
    skipped: list[Skipped],
    previous: dict[str, tuple[str, int, int]] | None = None,
    top: int = DEFAULT_TOP,
) -> str:
    errors = sum(r.errors for r in results)
    warnings = sum(r.warnings for r in results)
    lines = ["# Quality report", ""]
    lines.append(
        f"{len(results)} checks run: {errors} errors, {warnings} warnings ({len(skipped)} skipped)."
    )
    lines += [
        "",
        "## Checks",
        "",
        "| Check | Status | Errors | Warnings |",
        "| --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(f"| {r.name} | {r.status} | {r.errors} | {r.warnings} |")
    lines.append(f"| Total | | {errors} | {warnings} |")
    lines.append("")
    failed = [r for r in results if r.status in BAD_STATUSES]
    if failed:
        lines += ["## Failures", ""]
        for r in failed:
            lines += [f"### {r.name}", ""]
            if r.note:
                lines.append(f"- {r.note}")
            shown = r.error_lines[:MAX_FAILURE_LINES]
            lines += [f"- `{line}`" for line in shown]
            if len(r.error_lines) > len(shown):
                lines.append(f"- … and {len(r.error_lines) - len(shown)} more")
            lines.append("")
    legacy = sorted((r for r in results if r.warnings), key=lambda r: (-r.warnings, r.name))
    lines += ["## Longest LEGACY tables", ""]
    if legacy and top > 0:
        lines += ["| Check | Warnings | Most frequent kinds |", "| --- | --- | --- |"]
        for r in legacy[:top]:
            lines.append(f"| {r.name} | {r.warnings} | {frequent_kinds(r)} |")
    else:
        lines.append("None.")
    lines.append("")
    if previous is not None:
        lines += trend_lines(results, previous)
    if skipped:
        lines += ["## Skipped", ""]
        lines += [f"- {s.name}: {s.reason}" for s in skipped]
        lines.append("")
    return "\n".join(lines)


# --- command line -------------------------------------------------------------------


def parse_args(argv: list[str]) -> dict[str, str] | str:
    """The options as a dict, or a message when the command line is wrong."""
    options = {"--previous": "", "--output": "", "--top": str(DEFAULT_TOP), "--timeout": "0"}
    options["--tools"] = str(HERE)
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            return "help"
        if arg not in options:
            return f"unknown argument: {arg}"
        if i + 1 >= len(argv):
            return f"{arg} needs a value"
        options[arg] = argv[i + 1]
        i += 2
    if not options["--top"].isdecimal():
        return "--top must be a whole number"
    try:
        timeout = float(options["--timeout"])
    except ValueError:
        return "--timeout must be a number"
    if not 0 <= timeout < float("inf"):
        return "--timeout must be a finite number, 0 or more"
    return options


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    options = parse_args(args)
    if isinstance(options, str):
        stream = sys.stdout if options == "help" else sys.stderr
        if options != "help":
            print(f"quality_report.py: {options}", file=sys.stderr)
        print(USAGE, file=stream)
        return 0 if options == "help" else 2
    tools = Path(options["--tools"])
    if not tools.is_dir():
        print(f"{tools}: not a folder", file=sys.stderr)
        return 2
    previous = None
    if options["--previous"]:
        path = Path(options["--previous"])
        try:
            previous = parse_previous(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError) as err:
            print(f"{path}: cannot read the previous report: {type(err).__name__}", file=sys.stderr)
            return 2
        if not previous:
            print(f"{path}: no quality report table found", file=sys.stderr)
            return 2
    results, skipped = run_all(tools, tools.parent, float(options["--timeout"]))
    report = render(results, skipped, previous, int(options["--top"]))
    if options["--output"]:
        target = Path(options["--output"])
        try:
            target.write_text(report, encoding="utf-8", newline="\n")
        except OSError as err:
            print(f"{target}: cannot write the report: {type(err).__name__}", file=sys.stderr)
            return 2
        errors = sum(r.errors for r in results)
        warnings = sum(r.warnings for r in results)
        print(f"{len(results)} checks run: {errors} errors, {warnings} warnings: {target}")
    else:
        sys.stdout.write(report)
    failed = [r for r in results if r.status in BAD_STATUSES]
    for r in failed:
        what = r.note or f"{r.errors} errors"
        print(f"tools/{r.name}.py: {r.status.lower()}: {what}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
