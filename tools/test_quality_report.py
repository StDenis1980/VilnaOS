#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/quality_report.py (one Markdown report from all tools/check_*.py).

Run: python3 tools/test_quality_report.py
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import quality_report as qr  # noqa: E402  (after sys.path.insert above)

HEADER = "#!/usr/bin/env python3\n# SPDX-License-Identifier: GPL-3.0-or-later\n"


def check_source(body: str) -> str:
    return (
        HEADER
        + '"""A fake check."""\n\nfrom __future__ import annotations\n\nimport sys\n\n'
        + (textwrap.dedent(body))
    )


OK_CHECK = """
def main(argv: list[str] | None = None) -> int:
    print("3 files checked: 0 errors, 0 warnings")
    return 0
"""

WARN_CHECK = """
def main(argv: list[str] | None = None) -> int:
    print("a: warning: line 3: file 'x.txt' is odd", file=sys.stderr)
    print("b: warning: line 9: file 'y.txt' is odd", file=sys.stderr)
    print("2 files checked: 0 errors, 2 warnings")
    return 0
"""

FAIL_CHECK = """
def main(argv: list[str] | None = None) -> int:
    print("c.md: broken link", file=sys.stderr)
    print("1 files checked: 1 errors, 0 warnings")
    return 1
"""


class Sandbox:
    """A temporary repo: tools/ with fake checks; the report is made from it."""

    def __init__(self, checks: dict[str, str]) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name, body in checks.items():
            (self.tools / f"{name}.py").write_text(check_source(body), encoding="utf-8")

    def run(self, name: str, timeout: float = 0.0) -> qr.Result:
        return qr.run_check(self.tools / f"{name}.py", self.root, timeout)

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = qr.main(["--tools", str(self.tools), *args])
        return code, out.getvalue(), err.getvalue()

    def close(self) -> None:
        self._tmp.cleanup()


class SandboxCase(unittest.TestCase):
    def sandbox(self, checks: dict[str, str]) -> Sandbox:
        box = Sandbox(checks)
        self.addCleanup(box.close)
        return box


class RunOneCheck(SandboxCase):
    def test_clean_check(self) -> None:
        r = self.sandbox({"check_a": OK_CHECK}).run("check_a")
        self.assertEqual((r.status, r.errors, r.warnings), (qr.OK, 0, 0))

    def test_warnings_come_from_the_summary_line(self) -> None:
        r = self.sandbox({"check_a": WARN_CHECK}).run("check_a")
        self.assertEqual((r.status, r.errors, r.warnings), (qr.WARN, 0, 2))
        self.assertEqual(len(r.warning_lines), 2)

    def test_errors_and_exit_code(self) -> None:
        r = self.sandbox({"check_a": FAIL_CHECK}).run("check_a")
        self.assertEqual((r.status, r.errors, r.exit_code), (qr.FAIL, 1, 1))
        self.assertEqual(r.error_lines, ["c.md: broken link"])

    def test_no_summary_line_counts_stderr(self) -> None:
        body = """
        def main() -> int:
            print("x: warning: soft", file=sys.stderr)
            print("error: hard", file=sys.stderr)
            print("y: warning: soft too", file=sys.stderr)
            return 1
        """
        r = self.sandbox({"check_a": body}).run("check_a")
        self.assertEqual((r.errors, r.warnings, r.status), (1, 2, qr.FAIL))

    def test_nonzero_exit_is_never_zero_errors(self) -> None:
        body = """
        def main(argv: list[str] | None = None) -> int:
            print("0 errors, 0 warnings")
            return 2
        """
        r = self.sandbox({"check_a": body}).run("check_a")
        self.assertEqual((r.status, r.errors), (qr.FAIL, 1))

    def test_summary_line_beats_stderr_count(self) -> None:
        body = """
        def main(argv: list[str] | None = None) -> int:
            print("noise 7 errors, 7 warnings in a table")
            print("12 files: 0 errors, 4 warnings")
            return 0
        """
        r = self.sandbox({"check_a": body}).run("check_a")
        self.assertEqual((r.errors, r.warnings), (0, 4))

    def test_sys_exit_inside_main_is_caught(self) -> None:
        body = """
        def main(argv: list[str] | None = None) -> int:
            print("1 errors, 0 warnings")
            sys.exit(1)
        """
        r = self.sandbox({"check_a": body}).run("check_a")
        self.assertEqual((r.status, r.exit_code), (qr.FAIL, 1))

    def test_sys_exit_with_a_message(self) -> None:
        body = """
        def main(argv: list[str] | None = None) -> int:
            sys.exit("cannot go on")
        """
        r = self.sandbox({"check_a": body}).run("check_a")
        self.assertEqual((r.status, r.error_lines), (qr.FAIL, ["cannot go on"]))

    def test_sys_exit_zero_and_none(self) -> None:
        body = """
        def main(argv: list[str] | None = None) -> int:
            sys.exit(0)
        """
        self.assertEqual(self.sandbox({"check_a": body}).run("check_a").status, qr.OK)

    def test_exception_is_a_failed_check_not_a_traceback(self) -> None:
        body = """
        def main(argv: list[str] | None = None) -> int:
            raise ValueError("boom\\nsecond line")
        """
        r = self.sandbox({"check_a": body}).run("check_a")
        self.assertEqual((r.status, r.errors, r.note), (qr.CRASH, 1, "ValueError: boom"))

    def test_main_returning_not_an_int_is_a_crash(self) -> None:
        body = """
        def main(argv: list[str] | None = None):
            return "yes"
        """
        r = self.sandbox({"check_a": body}).run("check_a")
        self.assertEqual(r.status, qr.CRASH)
        self.assertIn("TypeError", r.note)

    def test_main_returning_none_is_success(self) -> None:
        body = """
        def main(argv: list[str] | None = None):
            print("0 errors, 0 warnings")
        """
        self.assertEqual(self.sandbox({"check_a": body}).run("check_a").status, qr.OK)

    def test_import_error_inside_the_module(self) -> None:
        body = """
        import no_such_module_anywhere

        def main() -> int:
            return 0
        """
        r = self.sandbox({"check_a": body}).run("check_a")
        self.assertEqual(r.status, qr.CRASH)
        self.assertIn("ModuleNotFoundError", r.note)

    def test_one_argument_main_gets_an_empty_list(self) -> None:
        body = """
        def main(argv: list[str]) -> int:
            print(f"{len(argv)} errors, 0 warnings")
            return 0 if argv == [] else 1
        """
        r = self.sandbox({"check_a": body}).run("check_a")
        self.assertEqual((r.status, r.errors), (qr.OK, 0))

    def test_checks_read_nothing_from_stdin(self) -> None:
        body = """
        def main() -> int:
            data = sys.stdin.read()
            print(f"{len(data)} errors, 0 warnings")
            return 0
        """
        self.assertEqual(self.sandbox({"check_a": body}).run("check_a").errors, 0)

    def test_check_sees_the_repo_root_as_working_folder(self) -> None:
        body = """
        import os

        def main() -> int:
            names = os.listdir(".")
            print("0 errors, 0 warnings" if names == ["tools"] else "9 errors, 0 warnings")
            return 0
        """
        box = self.sandbox({"check_a": body})
        self.assertEqual(box.run("check_a").errors, 0)

    def test_timeout(self) -> None:
        if not hasattr(qr.signal, "setitimer"):
            self.skipTest("no setitimer on this platform")
        body = """
        import time

        def main() -> int:
            try:
                time.sleep(30)
            except Exception:
                return 0
            return 0
        """
        r = self.sandbox({"check_a": body}).run("check_a", timeout=0.3)
        self.assertEqual((r.status, r.errors), (qr.TIMEOUT, 1))

    def test_timeout_does_not_leave_an_alarm(self) -> None:
        if not hasattr(qr.signal, "setitimer"):
            self.skipTest("no setitimer on this platform")
        self.sandbox({"check_a": OK_CHECK}).run("check_a", timeout=5)
        self.assertEqual(qr.signal.getitimer(qr.signal.ITIMER_REAL)[0], 0.0)


class NoLeaks(SandboxCase):
    def test_process_state_is_restored(self) -> None:
        body = """
        import os

        def main() -> int:
            sys.path.insert(0, "/nonexistent")
            sys.argv = ["changed"]
            os.chdir("/")
            sys.stdout = sys.stderr
            print("0 errors, 0 warnings")
            return 0
        """
        box = self.sandbox({"check_a": body, "check_b": OK_CHECK})
        before = (list(sys.path), list(sys.argv), os.getcwd(), set(sys.modules), sys.stdin)
        streams = (sys.stdout, sys.stderr)
        box.run("check_a")
        self.assertEqual(
            (list(sys.path), list(sys.argv), os.getcwd(), set(sys.modules), sys.stdin), before
        )
        self.assertEqual((sys.stdout, sys.stderr), streams)

    def test_a_check_cannot_print_on_the_real_streams(self) -> None:
        body = """
        def main() -> int:
            print("hello on stdout")
            print("hello on stderr", file=sys.stderr)
            sys.stdout.write("more")
            return 0
        """
        box = self.sandbox({"check_a": body})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            box.run("check_a")
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_state_is_restored_after_a_crash_and_a_sys_exit(self) -> None:
        crash = """
        def main() -> int:
            sys.path.insert(0, "/nonexistent")
            raise RuntimeError("x")
        """
        leave = """
        def main() -> int:
            sys.path.insert(0, "/nonexistent")
            sys.exit(3)
        """
        box = self.sandbox({"check_a": crash, "check_b": leave})
        path, modules = list(sys.path), set(sys.modules)
        box.run("check_a")
        box.run("check_b")
        self.assertEqual((list(sys.path), set(sys.modules)), (path, modules))

    def test_every_run_starts_from_a_fresh_module(self) -> None:
        body = """
        COUNT = [0]

        def main() -> int:
            COUNT[0] += 1
            print(f"{COUNT[0] - 1} errors, 0 warnings")
            return 0
        """
        box = self.sandbox({"check_a": body})
        self.assertEqual([box.run("check_a").errors for _ in range(3)], [0, 0, 0])

    def test_sibling_imports_work(self) -> None:
        box = self.sandbox(
            {"check_a": "import helper_mod\n\ndef main() -> int:\n    return helper_mod.CODE\n"}
        )
        (box.tools / "helper_mod.py").write_text("CODE = 0\n", encoding="utf-8")
        self.assertEqual(box.run("check_a").status, qr.OK)
        self.assertNotIn("helper_mod", sys.modules)


class SafeToImport(SandboxCase):
    def reason(self, body: str, raw: bool = False) -> str | None:
        box = self.sandbox({})
        path = box.tools / "check_x.py"
        path.write_text(body if raw else check_source(body), encoding="utf-8")
        return qr.runnable(path)

    def test_plain_check_is_runnable(self) -> None:
        self.assertIsNone(self.reason(OK_CHECK))

    def test_main_guard_and_constants_are_fine(self) -> None:
        body = (
            OK_CHECK
            + "\nLIMIT = 3\nNAMES: dict[str, int] = {}\n"
            + 'if __name__ == "__main__":\n    sys.exit(main())\n'
        )
        self.assertIsNone(self.reason(body))

    def test_call_at_top_level_is_skipped(self) -> None:
        reason = self.reason("print('hi')\n\ndef main() -> int:\n    return 0\n")
        self.assertIn("import time", reason or "")

    def test_loop_and_with_at_top_level_are_skipped(self) -> None:
        self.assertIsNotNone(
            self.reason("for i in range(3):\n    pass\n\ndef main() -> int:\n    return 0\n")
        )
        self.assertIsNotNone(
            self.reason("with open('x'):\n    pass\n\ndef main() -> int:\n    return 0\n")
        )

    def test_other_if_at_top_level_is_skipped(self) -> None:
        self.assertIsNotNone(
            self.reason("if sys.platform == 'x':\n    pass\n\ndef main() -> int:\n    return 0\n")
        )

    def test_no_main(self) -> None:
        self.assertEqual(
            self.reason("def check() -> int:\n    return 0\n"), "has no top-level function main"
        )

    def test_main_inside_a_class_does_not_count(self) -> None:
        body = "class Package:\n    def main(self) -> int:\n        return 0\n"
        self.assertEqual(self.reason(body), "has no top-level function main")

    def test_main_needing_two_arguments(self) -> None:
        self.assertEqual(
            self.reason("def main(a, b) -> int:\n    return 0\n"),
            "main needs more than one argument",
        )

    def test_main_with_defaults_is_fine(self) -> None:
        self.assertIsNone(self.reason("def main(a=None, b=1) -> int:\n    return 0\n"))

    def test_syntax_error(self) -> None:
        self.assertEqual(self.reason("def main(:\n", raw=True), "cannot parse: SyntaxError")

    def test_null_byte(self) -> None:
        self.assertIn("cannot parse", self.reason("x = 1\0\n", raw=True) or "")

    def test_not_utf8(self) -> None:
        box = self.sandbox({})
        path = box.tools / "check_x.py"
        path.write_bytes(b"\xff\xfe\x00bad")
        self.assertEqual(qr.runnable(path), "cannot read: UnicodeDecodeError")

    def test_empty_file(self) -> None:
        self.assertEqual(self.reason("", raw=True), "has no top-level function main")

    def test_symlink_is_skipped_and_never_run(self) -> None:
        box = self.sandbox({})
        target = box.root / "outside.py"
        target.write_text(check_source("def main() -> int:\n    raise SystemExit(7)\n"))
        link = box.tools / "check_link.py"
        link.symlink_to(target)
        results, skipped = qr.run_all(box.tools, box.root)
        self.assertEqual(results, [])
        self.assertEqual(skipped, [qr.Skipped("check_link", "is a symbolic link")])

    def test_import_time_side_effect_is_never_executed(self) -> None:
        box = self.sandbox({})
        marker = box.root / "marker.txt"
        body = f"open({str(marker)!r}, 'w').close()\n\ndef main() -> int:\n    return 0\n"
        (box.tools / "check_x.py").write_text(check_source(body), encoding="utf-8")
        _, skipped = qr.run_all(box.tools, box.root)
        self.assertEqual(len(skipped), 1)
        self.assertFalse(marker.exists())


class Discovery(SandboxCase):
    def test_only_check_files_in_sorted_order(self) -> None:
        box = self.sandbox({"check_b": OK_CHECK, "check_a": OK_CHECK, "other": OK_CHECK})
        (box.tools / "test_check_a.py").write_text("x = 1\n")
        (box.tools / "check_notes.txt").write_text("x")
        (box.tools / "check_dir.py").mkdir()
        self.assertEqual([p.name for p in qr.find_checks(box.tools)], ["check_a.py", "check_b.py"])

    def test_new_check_is_picked_up_without_a_change(self) -> None:
        box = self.sandbox({"check_a": OK_CHECK})
        before, _ = qr.run_all(box.tools, box.root)
        (box.tools / "check_new.py").write_text(check_source(WARN_CHECK), encoding="utf-8")
        after, _ = qr.run_all(box.tools, box.root)
        self.assertEqual([r.name for r in before], ["check_a"])
        self.assertEqual([r.name for r in after], ["check_a", "check_new"])

    def test_empty_folder(self) -> None:
        box = self.sandbox({})
        report = qr.render(*qr.run_all(box.tools, box.root))
        self.assertIn("0 checks run: 0 errors, 0 warnings (0 skipped).", report)
        self.assertIn("None.", report)

    def test_missing_folder(self) -> None:
        self.assertEqual(qr.find_checks(Path("/nonexistent-folder-for-test")), [])


class Rendering(SandboxCase):
    def report(self, checks: dict[str, str], **kwargs: object) -> str:
        box = self.sandbox(checks)
        results, skipped = qr.run_all(box.tools, box.root)
        return qr.render(results, skipped, **kwargs)  # type: ignore[arg-type]

    def test_table_and_totals(self) -> None:
        text = self.report({"check_a": OK_CHECK, "check_b": WARN_CHECK, "check_c": FAIL_CHECK})
        self.assertIn("3 checks run: 1 errors, 2 warnings (0 skipped).", text)
        self.assertIn("| check_a | OK | 0 | 0 |", text)
        self.assertIn("| check_b | WARN | 0 | 2 |", text)
        self.assertIn("| check_c | FAIL | 1 | 0 |", text)
        self.assertIn("| Total | | 1 | 2 |", text)

    def test_failures_section_lists_error_lines(self) -> None:
        text = self.report({"check_c": FAIL_CHECK})
        self.assertIn("## Failures", text)
        self.assertIn("- `c.md: broken link`", text)

    def test_no_failures_section_when_clean(self) -> None:
        self.assertNotIn("## Failures", self.report({"check_a": WARN_CHECK}))

    def test_failure_lines_are_capped(self) -> None:
        body = """
        def main() -> int:
            for i in range(25):
                print(f"f{i}: broken", file=sys.stderr)
            return 1
        """
        text = self.report({"check_a": body})
        self.assertIn("- … and 15 more", text)
        self.assertIn("f9: broken", text)
        self.assertNotIn("f10: broken", text)

    def test_legacy_tables_sorted_by_size_then_name(self) -> None:
        def many(n: int) -> str:
            return (
                "def main() -> int:\n"
                f"    for i in range({n}):\n"
                "        print(f'f{i}: warning: thing', file=sys.stderr)\n"
                "    return 0\n"
            )

        text = self.report(
            {"check_a": many(2), "check_b": many(5), "check_c": many(5), "check_d": many(1)},
            top=3,
        )
        section = text.split("## Longest LEGACY tables")[1]
        rows = [line.split(" | ")[0] for line in section.splitlines() if line.startswith("| check")]
        self.assertEqual(rows, ["| check_b", "| check_c", "| check_a"])

    def test_top_zero_and_no_warnings(self) -> None:
        self.assertIn("None.", self.report({"check_a": WARN_CHECK}, top=0))
        self.assertIn("None.", self.report({"check_a": OK_CHECK}))

    def test_warning_kinds_are_grouped(self) -> None:
        text = self.report({"check_a": WARN_CHECK})
        self.assertIn("2× file '…' is odd", text)

    def test_kind_of(self) -> None:
        self.assertEqual(
            qr.kind_of("p/q.md: line 12: warning: link to 'a.md' points at 3 files"),
            "link to '…' points at N files",
        )
        self.assertEqual(qr.kind_of("no marker at all"), "no marker at all")
        self.assertEqual(len(qr.kind_of("w: warning: " + "x" * 500)), qr.KIND_WIDTH)

    def test_pipes_in_messages_do_not_break_the_table(self) -> None:
        body = """
        def main() -> int:
            print("a: warning: left | right", file=sys.stderr)
            return 0
        """
        text = self.report({"check_a": body})
        self.assertIn("left \\| right", text)

    def test_skipped_section(self) -> None:
        box = self.sandbox({"check_a": OK_CHECK})
        (box.tools / "check_bad.py").write_text(check_source("print(1)\n"), encoding="utf-8")
        text = qr.render(*qr.run_all(box.tools, box.root))
        self.assertIn("## Skipped", text)
        self.assertIn("- check_bad: top level does something at import time (line ", text)
        self.assertIn("1 checks run: 0 errors, 0 warnings (1 skipped).", text)

    def test_deterministic(self) -> None:
        checks = {"check_a": OK_CHECK, "check_b": WARN_CHECK, "check_c": FAIL_CHECK}
        self.assertEqual(self.report(checks), self.report(checks))

    def test_report_has_no_time_or_machine_path(self) -> None:
        box = self.sandbox({"check_a": WARN_CHECK})
        text = qr.render(*qr.run_all(box.tools, box.root))
        self.assertNotIn(str(box.root), text)
        self.assertNotRegex(text, r"\d{4}-\d\d-\d\d")

    def test_ends_with_one_newline(self) -> None:
        self.assertTrue(self.report({"check_a": OK_CHECK}).endswith("\n"))


class Trend(SandboxCase):
    def results(self, checks: dict[str, str]) -> list[qr.Result]:
        box = self.sandbox(checks)
        return qr.run_all(box.tools, box.root)[0]

    def test_parse_previous_round_trip(self) -> None:
        results = self.results({"check_a": OK_CHECK, "check_b": WARN_CHECK, "check_c": FAIL_CHECK})
        parsed = qr.parse_previous(qr.render(results, []))
        self.assertEqual(
            parsed,
            {"check_a": ("OK", 0, 0), "check_b": ("WARN", 0, 2), "check_c": ("FAIL", 1, 0)},
        )

    def test_parse_previous_ignores_other_tables_and_garbage(self) -> None:
        text = (
            "| Total | | 1 | 2 |\n| x | y |\nrandom\n"
            "| check_a | OK | 0 | 0 |\n| check_a | FAIL | 9 | 9 |\n"
        )
        self.assertEqual(qr.parse_previous(text), {"check_a": ("OK", 0, 0)})

    def test_parse_previous_of_nothing(self) -> None:
        self.assertEqual(qr.parse_previous(""), {})

    def test_no_change(self) -> None:
        results = self.results({"check_a": WARN_CHECK})
        text = qr.render(results, [], qr.parse_previous(qr.render(results, [])))
        self.assertIn("## Trend", text)
        self.assertIn("No check changed since the previous report.", text)
        self.assertIn("Errors: 0. Warnings: 2.", text)

    def test_better_worse_new_and_removed(self) -> None:
        old = qr.parse_previous(
            "| check_a | WARN | 0 | 5 |\n| check_b | OK | 0 | 0 |\n| check_gone | OK | 0 | 1 |\n"
        )
        results = self.results(
            {"check_a": WARN_CHECK, "check_b": FAIL_CHECK, "check_new": OK_CHECK}
        )
        text = qr.render(results, [], old)
        self.assertIn("| check_a | WARN → WARN | 0 | 5 → 2 (-3) |", text)
        self.assertIn("| check_b | OK → FAIL | 0 → 1 (+1) | 0 |", text)
        self.assertIn("| check_gone | removed (was OK) | - | - |", text)
        self.assertIn("| check_new | new (OK) | 0 | 0 |", text)
        self.assertIn("Errors: 0 → 1 (+1). Warnings: 6 → 2 (-4).", text)

    def test_no_trend_section_without_previous(self) -> None:
        results = self.results({"check_a": OK_CHECK})
        self.assertNotIn("## Trend", qr.render(results, []))

    def test_report_with_trend_can_be_a_previous_report(self) -> None:
        results = self.results({"check_a": WARN_CHECK})
        old = qr.parse_previous("| check_a | OK | 0 | 0 |\n")
        again = qr.parse_previous(qr.render(results, [], old))
        self.assertEqual(again, {"check_a": ("WARN", 0, 2)})


class CommandLine(SandboxCase):
    def test_report_on_stdout_and_exit_zero(self) -> None:
        code, out, err = self.sandbox({"check_a": WARN_CHECK}).cli()
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("# Quality report\n"))

    def test_exit_one_when_a_check_fails(self) -> None:
        code, out, err = self.sandbox({"check_a": OK_CHECK, "check_c": FAIL_CHECK}).cli()
        self.assertEqual(code, 1)
        self.assertIn("tools/check_c.py: fail: exit code 1", err)
        self.assertIn("| check_c | FAIL | 1 | 0 |", out)

    def test_exit_one_for_a_crash(self) -> None:
        body = "def main() -> int:\n    raise KeyError('k')\n"
        code, _, err = self.sandbox({"check_a": body}).cli()
        self.assertEqual(code, 1)
        self.assertIn("tools/check_a.py: crash: KeyError: 'k'", err)

    def test_output_file(self) -> None:
        box = self.sandbox({"check_a": WARN_CHECK})
        target = box.root / "report.md"
        code, out, _ = box.cli("--output", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(out, f"1 checks run: 0 errors, 2 warnings: {target}\n")
        self.assertTrue(target.read_text(encoding="utf-8").startswith("# Quality report"))

    def test_output_cannot_be_written(self) -> None:
        box = self.sandbox({"check_a": OK_CHECK})
        code, _, err = box.cli("--output", str(box.root / "no" / "such" / "dir" / "r.md"))
        self.assertEqual(code, 2)
        self.assertIn("cannot write the report", err)

    def test_previous(self) -> None:
        box = self.sandbox({"check_a": WARN_CHECK})
        old = box.root / "old.md"
        old.write_text("| check_a | OK | 0 | 0 |\n", encoding="utf-8")
        code, out, _ = box.cli("--previous", str(old))
        self.assertEqual(code, 0)
        self.assertIn("| check_a | OK → WARN | 0 | 0 → 2 (+2) |", out)

    def test_previous_may_be_overwritten_by_output(self) -> None:
        box = self.sandbox({"check_a": WARN_CHECK})
        report = box.root / "r.md"
        report.write_text("| check_a | OK | 0 | 0 |\n", encoding="utf-8")
        self.assertEqual(box.cli("--previous", str(report), "--output", str(report))[0], 0)
        self.assertIn("## Trend", report.read_text(encoding="utf-8"))

    def test_previous_missing_empty_binary(self) -> None:
        box = self.sandbox({"check_a": OK_CHECK})
        code, _, err = box.cli("--previous", str(box.root / "nope.md"))
        self.assertEqual(code, 2)
        self.assertIn("cannot read the previous report", err)
        (box.root / "empty.md").write_text("", encoding="utf-8")
        code, _, err = box.cli("--previous", str(box.root / "empty.md"))
        self.assertEqual(code, 2)
        self.assertIn("empty.md: no quality report table found", err)
        (box.root / "bin.md").write_bytes(b"\xff\xfe\x00")
        self.assertEqual(box.cli("--previous", str(box.root / "bin.md"))[0], 2)

    def test_top(self) -> None:
        box = self.sandbox({"check_a": WARN_CHECK})
        self.assertIn("None.", box.cli("--top", "0")[1])

    def test_bad_arguments(self) -> None:
        box = self.sandbox({"check_a": OK_CHECK})
        for args in (
            ["--bogus"],
            ["--top"],
            ["--top", "-1"],
            ["--top", "x"],
            ["--timeout", "x"],
            ["--timeout", "-1"],
            ["--timeout", "inf"],
            ["--timeout", "nan"],
            ["positional"],
        ):
            code, out, err = box.cli(*args)
            self.assertEqual((code, out), (2, ""), args)
            self.assertIn("usage:", err)

    def test_missing_tools_folder(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = qr.main(["--tools", "/nonexistent-folder-for-test"])
        self.assertEqual(code, 2)
        self.assertIn("not a folder", err.getvalue())

    def test_help(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = qr.main(["--help"])
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertIn("usage:", out.getvalue())


class WholeRepo(unittest.TestCase):
    """The real checks of this repo, run by import."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.results, cls.skipped = qr.run_all(ROOT / "tools", ROOT)

    def test_every_check_file_is_run_or_explained(self) -> None:
        files = {p.stem for p in (ROOT / "tools").glob("check_*.py")}
        seen = {r.name for r in self.results} | {s.name for s in self.skipped}
        self.assertEqual(files, seen)
        self.assertGreaterEqual(len(self.results), 10)

    def test_no_check_is_skipped_crashes_or_times_out(self) -> None:
        # A check may FAIL (the repo can be broken); it must not crash or be left out.
        self.assertEqual(self.skipped, [])
        bad = [r.name for r in self.results if r.status in (qr.CRASH, qr.TIMEOUT)]
        self.assertEqual(bad, [])

    def test_counts_match_running_the_check_directly(self) -> None:
        for r in self.results:
            direct = qr.run_check(ROOT / "tools" / f"{r.name}.py", ROOT)
            self.assertEqual(
                (direct.status, direct.errors, direct.warnings), (r.status, r.errors, r.warnings)
            )

    def test_report_is_deterministic_on_the_repo(self) -> None:
        again, skipped = qr.run_all(ROOT / "tools", ROOT)
        self.assertEqual(qr.render(self.results, self.skipped), qr.render(again, skipped))

    def test_report_parses_as_its_own_previous(self) -> None:
        parsed = qr.parse_previous(qr.render(self.results, self.skipped))
        self.assertEqual(set(parsed), {r.name for r in self.results})

    def test_running_the_checks_changes_no_tracked_file(self) -> None:
        listing = sorted(
            p.relative_to(ROOT).as_posix() for p in (ROOT / "site").rglob("*") if p.is_file()
        )
        sizes = {rel: (ROOT / rel).stat().st_mtime_ns for rel in listing}
        qr.run_all(ROOT / "tools", ROOT)
        after = {rel: (ROOT / rel).stat().st_mtime_ns for rel in listing}
        self.assertEqual(sizes, after)
        self.assertEqual(
            sorted(
                p.relative_to(ROOT).as_posix() for p in (ROOT / "site").rglob("*") if p.is_file()
            ),
            listing,
        )


if __name__ == "__main__":
    unittest.main()
