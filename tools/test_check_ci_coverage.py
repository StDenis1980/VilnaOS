#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_ci_coverage.py (every check and test is run by CI).

Run: python3 tools/test_check_ci_coverage.py
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_ci_coverage as cc  # noqa: E402  (after sys.path.insert above)

HEAD = "name: CI\non:\n  push:\n  pull_request:\n"
PYPROJECT = '[project]\nname = "demo"\nversion = "0.1.0"\n\n[tool.pytest.ini_options]\n'
WORKSPACE = '[workspace]\nmembers = ["crates/a"]\nresolver = "2"\n'
CRATE = '[package]\nname = "a"\nversion = "0.1.0"\n'


def indent(text: str, by: int) -> str:
    return textwrap.indent(textwrap.dedent(text).strip("\n"), " " * by) + "\n"


def ci(runs: str, directory: str = "", head: str = HEAD, job: str = "build") -> str:
    """A workflow with one job; ``runs`` is the text of one ``run`` script per paragraph."""
    defaults = f"    defaults:\n      run:\n        working-directory: {directory}\n"
    out = head + f"jobs:\n  {job}:\n    runs-on: ubuntu-latest\n"
    out += defaults if directory else ""
    out += "    steps:\n      - uses: actions/checkout@v4\n"
    for script in textwrap.dedent(runs).strip("\n").split("\n\n"):
        out += "      - run: |\n" + indent(script, 10)
    return out


class Tree(unittest.TestCase):
    """A repo in a temporary folder; every file in it counts as tracked."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # The exceptions and old problems of this repo are not those of the test trees.
        for name in ("NOT_A_CHECK", "LEGACY"):
            patch = mock.patch.object(cc, name, type(getattr(cc, name))())
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, rel: str, text: str = "# SPDX-License-Identifier: GPL-3.0-or-later\n") -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def workflow(self, text: str, name: str = "ci.yml") -> None:
        self.write(f".github/workflows/{name}", text)

    def problems(self) -> list[cc.Problem]:
        tracked = sorted(
            p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()
        )
        return cc.check(self.root, tracked)[0]

    def errors(self) -> list[str]:
        return [str(p) for p in self.problems() if not p.warning]

    def warnings(self) -> list[str]:
        return [str(p) for p in self.problems() if p.warning]

    def assertOnly(self, found: list[str], *needles: str) -> None:
        """``found`` has one problem per needle, in order, each containing its needle."""
        self.assertEqual(len(found), len(needles), found)
        for line, needle in zip(found, needles, strict=True):
            self.assertIn(needle, line)


class YamlReader(unittest.TestCase):
    def test_maps_lists_and_nesting(self) -> None:
        doc = cc.parse_yaml("a:\n  b: 1\n  c:\n    - x\n    - y: 2\n      z: 3\nd: 'q'\n")
        self.assertEqual(doc["a"]["b"], "1")
        self.assertEqual(list(doc["a"]["c"][0:1]), ["x"])
        self.assertEqual(dict(doc["a"]["c"][1]), {"y": "2", "z": "3"})
        self.assertEqual(doc["d"], "q")

    def test_list_at_the_indent_of_its_key(self) -> None:
        doc = cc.parse_yaml("steps:\n- run: a\n- run: b\nnext: 1\n")
        self.assertEqual([s["run"] for s in doc["steps"]], ["a", "b"])
        self.assertEqual(doc["next"], "1")

    def test_literal_block_keeps_lines_and_comments(self) -> None:
        doc = cc.parse_yaml("run: |\n  a\n  # not a yaml comment\n    b\n\n  c\nafter: 1\n")
        self.assertEqual(doc["run"], "a\n# not a yaml comment\n  b\n\nc\n")
        self.assertEqual(doc["after"], "1")

    def test_folded_block_joins_lines_and_chomping_strips(self) -> None:
        doc = cc.parse_yaml("a: >\n  one\n  two\n\n  three\nb: |-\n  x\n  y\nc: 1\n")
        self.assertEqual(doc["a"], "one two\nthree\n")
        self.assertEqual(doc["b"], "x\ny")

    def test_block_in_a_list_item(self) -> None:
        doc = cc.parse_yaml("- run: |\n    echo a\n    echo b\n  name: n\n")
        self.assertEqual(doc[0]["run"], "echo a\necho b\n")
        self.assertEqual(doc[0]["name"], "n")

    def test_quoted_and_plain_values(self) -> None:
        doc = cc.parse_yaml(
            "a: \"x: y # not a comment\"\nb: 'it''s'\nc: plain text # comment\n"
            'd: "tab\\there"\ne: python3 -c "print(1)"\n'
        )
        self.assertEqual(doc["a"], "x: y # not a comment")
        self.assertEqual(doc["b"], "it's")
        self.assertEqual(doc["c"], "plain text")
        self.assertEqual(doc["d"], "tab\there")
        self.assertEqual(doc["e"], 'python3 -c "print(1)"')

    def test_multiline_plain_and_quoted(self) -> None:
        doc = cc.parse_yaml('a: one\n  two\nb: "x\n  y"\nc: 1\n')
        self.assertEqual(doc["a"], "one two")
        self.assertEqual(doc["b"], "x y")
        self.assertEqual(doc["c"], "1")

    def test_flow_lists_and_maps(self) -> None:
        doc = cc.parse_yaml('a: ["3.11", "3.13"]\nb: [x,\n  y] # c\nc: {k: v, "q": [1]}\nd: []\n')
        self.assertEqual(list(doc["a"]), ["3.11", "3.13"])
        self.assertEqual(list(doc["b"]), ["x", "y"])
        self.assertEqual(doc["c"]["k"], "v")
        self.assertEqual(list(doc["c"]["q"]), ["1"])
        self.assertEqual(list(doc["d"]), [])

    def test_comments_blank_lines_and_document_marker(self) -> None:
        doc = cc.parse_yaml("# top\n---\n\na: 1  # c\n\n  # indented comment\nb: 2\n")
        self.assertEqual(dict(doc), {"a": "1", "b": "2"})

    def test_empty_value_and_line_numbers(self) -> None:
        doc = cc.parse_yaml("a:\nb:\n  c: 1\n")
        self.assertEqual(doc["a"], "")
        self.assertEqual(doc["b"].line, 3)

    def test_what_is_refused(self) -> None:
        for text in (
            "a:\n\tb: 1\n",
            "a: &x 1\n",
            "a: *x\n",
            "a: 1\na: 2\n",
            'a: "open\n',
            "a: [1, 2\n",
            "a:\n  b: 1\n c: 2\n",
            "a: 'x' y\n",
        ):
            with self.subTest(text=text), self.assertRaises(cc.YamlError):
                cc.parse_yaml(text)


class Commands(unittest.TestCase):
    def commands(self, script: str) -> list[list[str]]:
        found = []
        for line in cc.logical_lines(script):
            found += [words for kind, words in cc.shell_events(line) if kind == "cmd"]
        return found

    def test_operators_split_commands(self) -> None:
        self.assertEqual(
            self.commands("a x && b y; c | d || e"),
            [["a", "x"], ["b", "y"], ["c"], ["d"], ["e"]],
        )

    def test_continuations_and_comments(self) -> None:
        self.assertEqual(
            self.commands("a one \\\n  two # c\n# whole\nb"), [["a", "one", "two"], ["b"]]
        )

    def test_here_document_body_is_not_a_command(self) -> None:
        script = "python3 - <<'PY'\nimport os\nprint('tools/gone.py')\nPY\nb"
        self.assertEqual(self.commands(script), [["python3", "-"], ["b"]])

    def test_redirect_targets_are_dropped(self) -> None:
        self.assertEqual(self.commands("a x > out.txt 2>&1"), [["a", "x", "2"]])
        self.assertEqual(self.commands("a x >> log && b"), [["a", "x"], ["b"]])

    def test_quotes_keep_words_together(self) -> None:
        self.assertEqual(self.commands("a \"x y\" 'p && q'"), [["a", "x y", "p && q"]])

    def test_unbalanced_quote_falls_back_to_words(self) -> None:
        self.assertEqual(self.commands('a "x y'), [["a", '"x', "y"]])

    def test_unwrap_wrappers(self) -> None:
        cases = {
            "FOO=1 python3 x.py": ["python3", "x.py"],
            "sudo -E python3 x.py": ["python3", "x.py"],
            "sudo -u root python3 x.py": ["python3", "x.py"],
            "env A=1 B=2 python3 x.py": ["python3", "x.py"],
            "timeout 300 tests/run.sh": ["tests/run.sh"],
            "timeout -s KILL 5 tests/run.sh": ["tests/run.sh"],
            "uv run --no-sync pytest -v": ["pytest", "-v"],
            "uv run --python 3.11 python x.py": ["python", "x.py"],
            "uv sync --locked": ["uv", "sync", "--locked"],
            "xvfb-run -a pytest": ["pytest"],
            "coverage run --source=a x.py": ["x.py"],
            "if python3 x.py": ["python3", "x.py"],
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(cc.unwrap(text.split()), want)

    def test_join_path(self) -> None:
        self.assertEqual(cc.join_path("a", "../b/c.py"), "b/c.py")
        self.assertEqual(cc.join_path("", "./x"), "x")
        self.assertEqual(cc.join_path("a", ".."), "")
        self.assertIsNone(cc.join_path("a", "../../x"))
        self.assertIsNone(cc.join_path("a", "/etc/x"))

    def test_glob(self) -> None:
        match = lambda pattern, path: bool(cc.glob_regex(pattern).fullmatch(path))  # noqa: E731
        self.assertTrue(match("tests/*.test.js", "tests/a.test.js"))
        self.assertFalse(match("tests/*.test.js", "tests/sub/a.test.js"))
        self.assertTrue(match("**/test_*.py", "a/b/test_x.py"))
        self.assertTrue(match("**/test_*.py", "test_x.py"))
        self.assertTrue(match("t?.py", "t1.py"))
        self.assertTrue(match("t[ab].py", "ta.py"))
        self.assertFalse(match("t[ab].py", "tc.py"))


class Reading(Tree):
    def read(self, text: str) -> cc.Workflow:
        return cc.read_workflow("w.yml", text)

    def test_jobs_steps_and_directories(self) -> None:
        wf = self.read(
            HEAD
            + "defaults:\n  run:\n    working-directory: top\n"
            + "jobs:\n  one:\n    steps:\n      - run: a\n"
            + "      - name: Named\n        run: b\n        working-directory: step\n"
            + "  two:\n    defaults:\n      run:\n        working-directory: job\n"
            + "    steps:\n      - uses: x/y@v1\n        with:\n          run: not-a-step\n"
            + "          working-directory: not-a-step\n      - run: c\n"
        )
        one, two = wf.jobs
        self.assertEqual([(s.label, s.run, s.working_directory) for s in one.steps], [
            ("a", "a", None), ("Named", "b", "step"),
        ])  # fmt: skip
        self.assertEqual(one.working_directory, "top")
        self.assertEqual(two.working_directory, "job")
        self.assertEqual([s.run for s in two.steps], [None, "c"])
        self.assertIsNone(two.steps[0].working_directory)

    def test_triggers_in_all_shapes(self) -> None:
        for head, want in (
            ("on: push\n", {"push"}),
            ("on: [push, schedule]\n", {"push", "schedule"}),
            (
                "on:\n  pull_request:\n    paths: [a]\n  workflow_dispatch:\n",
                {"pull_request", "workflow_dispatch"},
            ),
        ):
            with self.subTest(head=head):
                self.assertEqual(self.read(head + "jobs: {}\n").triggers, frozenset(want))

    def test_only_push_and_pull_request_run_for_a_change(self) -> None:
        self.assertTrue(self.read("on: push\njobs: {}\n").for_changes)
        self.assertFalse(self.read("on: [schedule, workflow_dispatch]\njobs: {}\n").for_changes)

    def test_step_without_name_is_labelled_by_its_command(self) -> None:
        wf = self.read(HEAD + "jobs:\n  j:\n    steps:\n      - run: |\n          python3 x.py\n")
        self.assertEqual(wf.jobs[0].steps[0].label, "python3 x.py")


class ChecksAreRun(Tree):
    def test_a_check_that_is_run_passes(self) -> None:
        self.write("tools/check_a.py")
        self.workflow(ci("python3 tools/check_a.py"))
        self.assertEqual(self.problems(), [])

    def test_a_check_that_is_not_run_is_an_error_that_says_what_to_add(self) -> None:
        self.write("tools/check_a.py")
        self.write("tools/check_b.py")
        self.workflow(ci("python3 tools/check_a.py"))
        found = self.errors()
        self.assertOnly(found, "tools/check_b.py: no step of .github/workflows runs this check")
        self.assertIn("python3 tools/check_b.py", found[0])

    def test_check_in_a_subfolder_tools(self) -> None:
        self.write("gloxi/tools/check_model.py")
        self.workflow(ci("python3 tools/check_model.py", directory="gloxi"))
        self.assertEqual(self.problems(), [])
        self.workflow(ci("python3 tools/check_model.py"))
        self.assertOnly(
            self.errors(),
            ".github/workflows/ci.yml: job 'build', step 'python3 tools/check_model.py'",
            "gloxi/tools/check_model.py: no step",
        )

    def test_working_directory_changes_the_meaning_of_the_path(self) -> None:
        self.write("tools/check_a.py")
        self.write("sub/tools/check_a.py")
        self.workflow(ci("python3 tools/check_a.py", directory="sub"))
        self.assertOnly(self.errors(), "tools/check_a.py: no step")
        self.assertNotIn("sub/", self.errors()[0].split(":")[0])

    def test_step_working_directory_overrides_the_job_one(self) -> None:
        self.write("sub/tools/check_a.py")
        text = ci("echo hi", directory="other")
        text += "      - run: python3 tools/check_a.py\n        working-directory: sub\n"
        self.write("other/.keep", "")
        self.workflow(text)
        self.assertEqual(self.errors(), [])

    def test_ways_to_start_a_script(self) -> None:
        self.write("tools/check_a.py")
        for command in (
            "python3 tools/check_a.py",
            "python tools/check_a.py --flag",
            "python3.11 -u tools/check_a.py",
            "uv run python tools/check_a.py",
            "uv run --no-sync python tools/check_a.py",
            "./tools/check_a.py",
            "cd tools && python3 check_a.py",
            "(cd tools && python3 check_a.py)",
            "sudo -E env X=1 python3 tools/check_a.py",
            "python3 -X dev -W error tools/check_a.py",
            "python3 -m tools.check_a",
            "python3 -m unittest tools/check_a.py",
            "python3 -m unittest discover -s tools -p 'check_*.py'",
        ):
            with self.subTest(command=command):
                self.workflow(ci(command))
                self.assertEqual(self.errors(), [])

    def test_things_that_only_mention_a_script_do_not_run_it(self) -> None:
        self.write("tools/check_a.py")
        for command in (
            "ruff check tools/check_a.py",
            "cat tools/check_a.py",
            "python3 -c 'import tools.check_a'",
            "echo python3 tools/check_a.py",
            "python3 tools/other.py tools/check_a.py",
        ):
            with self.subTest(command=command):
                self.write("tools/other.py")
                self.workflow(ci(command))
                self.assertIn("tools/check_a.py: no step", "\n".join(self.errors()))

    def test_cd_in_a_subshell_does_not_leak(self) -> None:
        self.write("tools/check_a.py")
        self.write("sub/x")
        self.workflow(ci("(cd sub && echo hi); python3 tools/check_a.py"))
        self.assertEqual(self.errors(), [])

    def test_cd_leaks_to_the_next_command_of_the_same_run(self) -> None:
        self.write("sub/tools/check_a.py")
        self.workflow(ci("cd sub\npython3 tools/check_a.py"))
        self.assertEqual(self.errors(), [])


class TestsAreRun(Tree):
    def test_unittest_file_run_as_a_script(self) -> None:
        self.write("tools/test_a.py")
        self.workflow(ci("python3 tools/test_a.py"))
        self.assertEqual(self.problems(), [])

    def test_test_that_is_not_run_is_an_error(self) -> None:
        self.write("tools/test_a.py")
        self.write("image/tools/test_b.py")
        self.write("tools/check_a.py")
        self.workflow(ci("python3 tools/check_a.py"))
        self.assertOnly(
            self.errors(),
            "image/tools/test_b.py: no step of .github/workflows runs this test",
            "tools/test_a.py: no step of .github/workflows runs this test",
        )

    def test_tests_of_a_folder_tools(self) -> None:
        self.write("image/tools/test_memcheck.py")
        self.workflow(ci("python3 image/tools/test_memcheck.py"))
        self.assertEqual(self.problems(), [])

    def test_unittest_discover(self) -> None:
        self.write("tools/test_a.py")
        self.write("tools/test_b.py")
        self.write("other/test_c.py")
        self.workflow(ci("python3 -m unittest discover -s tools"))
        self.assertOnly(self.errors(), "other/test_c.py")
        self.workflow(ci("python3 -m unittest discover -s tools -p 'test_a*.py'"))
        self.assertOnly(self.errors(), "other/test_c.py", "tools/test_b.py")

    def test_unittest_with_module_names(self) -> None:
        self.write("tools/test_a.py")
        self.workflow(ci("python3 -m unittest tools.test_a"))
        self.assertEqual(self.errors(), [])

    def test_pytest_collects_the_testpaths_of_its_project(self) -> None:
        self.write("p/pyproject.toml", PYPROJECT + 'testpaths = ["tests"]\n')
        self.write("p/tests/test_a.py")
        self.write("p/conformance/test_b.py")
        self.workflow(ci("uv run pytest", directory="p"))
        self.assertOnly(self.errors(), "p/conformance/test_b.py: no step")

    def test_extra_pytest_step_for_a_folder_outside_testpaths(self) -> None:
        self.write("p/pyproject.toml", PYPROJECT + 'testpaths = ["tests"]\n')
        self.write("p/tests/test_a.py")
        self.write("p/conformance/test_b.py")
        self.workflow(ci("uv run pytest\n\nuv run pytest conformance", directory="p"))
        self.assertEqual(self.errors(), [])

    def test_pytest_without_testpaths_collects_the_whole_folder(self) -> None:
        self.write("p/pyproject.toml", PYPROJECT)
        self.write("p/anything/test_a.py")
        self.write("p/anything/b_test.py")
        self.workflow(ci("pytest -q", directory="p"))
        self.assertEqual(self.errors(), [])

    def test_pytest_with_a_path_collects_only_that_path(self) -> None:
        self.write("p/pyproject.toml", PYPROJECT + 'testpaths = ["tests"]\n')
        self.write("p/tests/test_a.py")
        self.write("p/tests/test_b.py")
        self.workflow(ci("uv run --no-sync pytest -v tests/test_a.py::Case::test_x", directory="p"))
        self.assertOnly(self.errors(), "p/tests/test_b.py")

    def test_values_of_pytest_options_are_not_paths(self) -> None:
        self.write("p/pyproject.toml", PYPROJECT)
        self.write("p/test_a.py")
        self.workflow(
            ci(
                "pytest -k 'slow and not fast' -m unit --maxfail 2 -p no:cacheprovider",
                directory="p",
            )
        )
        self.assertEqual(self.errors(), [])

    def test_ignored_and_collect_only_do_not_run_tests(self) -> None:
        self.write("p/pyproject.toml", PYPROJECT)
        self.write("p/test_a.py")
        self.write("p/test_b.py")
        self.workflow(ci("pytest --ignore=test_a.py\n\npytest --ignore test_b.py", directory="p"))
        self.assertEqual(self.errors(), [])
        self.workflow(ci("pytest --ignore=test_a.py --ignore test_b.py", directory="p"))
        self.assertOnly(self.errors(), "p/test_a.py", "p/test_b.py")
        self.workflow(ci("pytest --collect-only", directory="p"))
        self.assertOnly(self.errors(), "p/pyproject.toml: no CI step runs pytest")
        self.assertIn("test files in it that nothing else runs: 2", self.errors()[0])

    def test_pytest_through_python_m_and_from_the_root(self) -> None:
        self.write("a/test_x.py")
        self.write("b/test_y.py")
        self.workflow(ci("python3 -m pytest a\n\ncd b && pytest"))
        self.assertEqual(self.errors(), [])

    def test_pytest_glob(self) -> None:
        self.write("t/test_a.py")
        self.write("t/test_b.py")
        self.workflow(ci("pytest t/test_*.py"))
        self.assertEqual(self.errors(), [])
        self.workflow(ci("pytest t/nothing_*.py"))
        self.assertIn(
            "test path 't/nothing_*.py' matches no tracked file", "\n".join(self.errors())
        )

    def test_a_test_in_a_nested_project_is_run_by_its_own_job(self) -> None:
        self.write("outer/pyproject.toml", PYPROJECT)
        self.write("outer/inner/pyproject.toml", PYPROJECT)
        self.write("outer/inner/test_a.py")
        self.workflow(ci("pytest", directory="outer/inner"))
        self.assertOnly(
            self.warnings(), "outer/pyproject.toml: warning: the project has no test_*.py"
        )
        self.assertEqual(self.errors(), [])


class PythonProjects(Tree):
    def test_a_project_with_a_pytest_job_passes(self) -> None:
        self.write("p/pyproject.toml", PYPROJECT)
        self.write("p/tests/test_a.py")
        self.workflow(ci("uv sync --locked\n\nuv run pytest", directory="p"))
        self.assertEqual(self.problems(), [])

    def test_a_project_without_a_job_is_an_error_naming_the_folder(self) -> None:
        self.write("p/pyproject.toml", PYPROJECT)
        self.write("p/tests/test_a.py")
        self.write("q/pyproject.toml", PYPROJECT)
        self.write("q/tests/test_b.py")
        self.workflow(ci("pytest", directory="q"))
        found = self.errors()
        self.assertOnly(found, "p/pyproject.toml: no CI step runs pytest in p")
        self.assertIn("working-directory: p", found[0])
        self.assertIn("test files in it that nothing else runs: 1", found[0])

    def test_pytest_from_a_parent_folder_does_not_make_the_job(self) -> None:
        self.write("p/pyproject.toml", PYPROJECT)
        self.write("p/tests/test_a.py")
        self.workflow(ci("pytest p"))
        self.assertOnly(self.errors(), "p/pyproject.toml: no CI step runs pytest in p")

    def test_cd_into_the_folder_is_as_good_as_a_working_directory(self) -> None:
        self.write("p/pyproject.toml", PYPROJECT)
        self.write("p/tests/test_a.py")
        self.workflow(ci("cd p && uv run pytest"))
        self.assertEqual(self.problems(), [])

    def test_a_job_without_pytest_in_the_folder_is_not_enough(self) -> None:
        self.write("p/pyproject.toml", PYPROJECT)
        self.write("p/tests/test_a.py")
        self.workflow(ci("uv sync --locked\n\nuv run ruff check .", directory="p"))
        self.assertOnly(self.errors(), "p/pyproject.toml: no CI step runs pytest")

    def test_test_files_run_one_by_one_do_not_replace_the_pytest_job(self) -> None:
        self.write("p/pyproject.toml", PYPROJECT)
        self.write("p/test_a.py")
        self.write("p/test_b.py")
        self.workflow(ci("cd p && python3 test_a.py"))
        found = self.errors()
        self.assertOnly(found, "p/pyproject.toml: no CI step runs pytest in p")
        self.assertIn("test files in it that nothing else runs: 1", found[0])

    def test_project_at_the_root(self) -> None:
        self.write("pyproject.toml", PYPROJECT)
        self.write("tests/test_a.py")
        self.workflow(ci("pytest"))
        self.assertEqual(self.problems(), [])
        self.workflow(ci("echo hi"))
        self.assertOnly(self.errors(), "pyproject.toml: no CI step runs pytest in .")

    def test_project_without_tests_is_a_warning(self) -> None:
        self.write("p/pyproject.toml", PYPROJECT)
        self.workflow(ci("echo hi"))
        self.assertEqual(self.errors(), [])
        self.assertOnly(self.warnings(), "p/pyproject.toml: warning: the project has no test_*.py")


class CargoWorkspaces(Tree):
    def tree(self) -> None:
        self.write("rs/Cargo.toml", WORKSPACE)
        self.write("rs/crates/a/Cargo.toml", CRATE)

    def test_workspace_with_cargo_test_passes(self) -> None:
        self.tree()
        self.workflow(ci("cargo fmt --check\n\ncargo test --locked", directory="rs"))
        self.assertEqual(self.problems(), [])

    def test_workspace_without_cargo_test_is_an_error_naming_the_folder(self) -> None:
        self.tree()
        self.workflow(ci("cargo build --locked\n\ncargo clippy", directory="rs"))
        found = self.errors()
        self.assertOnly(
            found, "rs/Cargo.toml: no CI step runs cargo test for the whole workspace in rs"
        )
        self.assertIn("working-directory: rs", found[0])

    def test_members_do_not_need_their_own_job(self) -> None:
        self.tree()
        self.workflow(ci("cargo +stable test", directory="rs"))
        self.assertEqual(self.errors(), [])

    def test_partial_runs_do_not_count(self) -> None:
        self.tree()
        for command in (
            "cargo test -p a",
            "cargo test --package a",
            "cargo test --lib",
            "cargo test --no-run",
            "cargo test --doc",
            "cargo build",
            "cargo nextest list",
        ):
            with self.subTest(command=command):
                self.workflow(ci(command, directory="rs"))
                self.assertOnly(self.errors(), "rs/Cargo.toml: no CI step runs cargo test")

    def test_arguments_after_double_dash_are_not_cargo_flags(self) -> None:
        self.tree()
        self.workflow(ci("cargo test --locked -- --lib", directory="rs"))
        self.assertEqual(self.errors(), [])

    def test_workspace_flag_nextest_and_manifest_path(self) -> None:
        self.tree()
        for command, directory in (
            ("cargo test --workspace --all-features", "rs"),
            ("cargo nextest run", "rs"),
            ("cargo test --manifest-path rs/Cargo.toml", ""),
            ("cargo test --manifest-path=rs/Cargo.toml", ""),
            ("cd rs && cargo test", ""),
        ):
            with self.subTest(command=command):
                self.workflow(ci(command, directory=directory))
                self.assertEqual(self.errors(), [])

    def test_test_in_another_folder_does_not_count(self) -> None:
        self.tree()
        self.write("other/x")
        self.workflow(ci("cargo test", directory="other"))
        self.assertOnly(self.errors(), "rs/Cargo.toml: no CI step runs cargo test")

    def test_crate_outside_a_workspace_needs_cargo_test_too(self) -> None:
        self.write("solo/Cargo.toml", CRATE)
        self.workflow(ci("echo hi"))
        self.assertOnly(self.errors(), "solo/Cargo.toml: no CI step runs cargo test")
        self.workflow(ci("cargo test", directory="solo"))
        self.assertEqual(self.errors(), [])

    def test_unreadable_manifest_is_an_error(self) -> None:
        self.write("rs/Cargo.toml", "[workspace\n")
        self.workflow(ci("echo hi"))
        self.assertOnly(self.errors(), "rs/Cargo.toml: cannot read the manifest")


class Exceptions(Tree):
    def test_script_that_is_not_a_check_and_not_run_is_an_error(self) -> None:
        self.write("tools/build.py")
        self.workflow(ci("echo hi"))
        found = self.errors()
        self.assertOnly(found, "tools/build.py: is in tools/, is not run by CI and is not a check")
        self.assertIn("NOT_A_CHECK", found[0])

    def test_listed_script_is_fine(self) -> None:
        self.write("tools/build.py")
        self.workflow(ci("echo hi"))
        with mock.patch.object(cc, "NOT_A_CHECK", {"tools/build.py": "a builder"}):
            self.assertEqual(self.problems(), [])

    def test_script_that_is_not_a_check_but_is_run_needs_no_exception(self) -> None:
        self.write("tools/build.py")
        self.workflow(ci("python3 tools/build.py --check"))
        self.assertEqual(self.problems(), [])

    def test_exception_without_a_file_is_an_error(self) -> None:
        self.workflow(ci("echo hi"))
        with mock.patch.object(cc, "NOT_A_CHECK", {"tools/gone.py": "was a builder"}):
            self.assertOnly(
                self.errors(), "tools/gone.py: is listed in NOT_A_CHECK but the file does not exist"
            )

    def test_exception_for_a_script_that_is_run_is_a_warning(self) -> None:
        self.write("tools/build.py")
        self.workflow(ci("python3 tools/build.py"))
        with mock.patch.object(cc, "NOT_A_CHECK", {"tools/build.py": "a builder"}):
            self.assertEqual(self.errors(), [])
            self.assertOnly(
                self.warnings(), "tools/build.py: warning: is listed in NOT_A_CHECK but CI runs it"
            )

    def test_a_check_can_be_listed_too(self) -> None:
        self.write("tools/check_net.py")
        self.workflow(ci("echo hi"))
        with mock.patch.object(cc, "NOT_A_CHECK", {"tools/check_net.py": "needs the network"}):
            self.assertEqual(self.problems(), [])

    def test_only_tools_of_the_root_are_loose_scripts(self) -> None:
        self.write("x/tools/build.py")
        self.write("tools/sub/helper.py")
        self.workflow(ci("echo hi"))
        self.assertEqual(self.problems(), [])


class RealExceptions(unittest.TestCase):
    def test_exceptions_are_documented_with_a_reason_and_exist(self) -> None:
        self.assertTrue(cc.NOT_A_CHECK)
        for path, reason in cc.NOT_A_CHECK.items():
            with self.subTest(path=path):
                self.assertGreater(len(reason), 20)
                self.assertTrue(cc.LOOSE_TOOL.match(path) or cc.CHECK_FILE.search(path), path)
                self.assertTrue((ROOT / path).is_file(), path)


class MissingFiles(Tree):
    def test_script_that_does_not_exist_is_an_error_naming_step_and_file(self) -> None:
        self.workflow(ci("python3 tools/gone.py"))
        found = self.errors()
        self.assertOnly(
            found, ".github/workflows/ci.yml: job 'build', step 'python3 tools/gone.py'"
        )
        self.assertIn("script 'tools/gone.py' does not exist", found[0])
        self.assertIn("(line 10)", found[0])

    def test_every_way_to_name_a_script(self) -> None:
        for command in (
            "uv run python tools/gone.py",
            "./tools/gone.py",
            "sh tools/gone.sh",
            "bash -e tools/gone.sh",
            "sh -n tools/gone.sh",
            "timeout 60 tools/gone.sh",
            "node --test tests/gone.js",
            "python3 -m unittest tools/gone.py",
            "pytest tools/gone.py",
            "pytest tools/gone",
        ):
            with self.subTest(command=command):
                self.workflow(ci(command))
                self.assertIn("does not exist", "\n".join(self.errors()))

    def test_existing_files_are_fine(self) -> None:
        self.write("tests/run.sh")
        self.write("deploy/install.sh")
        self.write("tests/a.test.js")
        self.workflow(
            ci("sh -n deploy/install.sh\n\ntimeout 300 tests/run.sh\n\nnode --test tests/*.test.js")
        )
        self.assertEqual(self.errors(), [])

    def test_glob_that_matches_nothing(self) -> None:
        self.workflow(ci("node --test tests/*.test.js"))
        self.assertOnly(self.errors(), "script 'tests/*.test.js' matches no tracked file")

    def test_paths_are_taken_from_the_working_directory(self) -> None:
        self.write("sub/deploy/install.sh")
        self.workflow(ci("sh -n deploy/install.sh", directory="sub"))
        self.assertEqual(self.errors(), [])
        self.workflow(ci("sh -n sub/deploy/install.sh", directory="sub"))
        self.assertIn("'sub/deploy/install.sh' does not exist", "\n".join(self.errors()))

    def test_a_working_directory_that_does_not_exist(self) -> None:
        self.workflow(ci("echo hi", directory="nowhere"))
        self.assertOnly(self.errors(), "working-directory 'nowhere' does not exist")

    def test_a_cd_into_a_missing_folder_makes_the_script_missing(self) -> None:
        self.write("tools/check_a.py")
        self.workflow(ci("cd gone && python3 check_a.py"))
        self.assertIn("check_a.py", "\n".join(self.errors()))

    def test_source_file_argument_of_any_command(self) -> None:
        self.write("image/packages.yaml")
        self.write("obs/build_picks.py")
        self.workflow(ci("python3 obs/build_picks.py --defaults image/packages.yaml check"))
        self.assertEqual(self.errors(), [])
        self.workflow(ci("python3 obs/build_picks.py --defaults image/gone.yaml check"))
        self.assertOnly(self.errors(), "file 'image/gone.yaml' does not exist")
        self.workflow(ci("python3 obs/build_picks.py --defaults=image/gone.yaml"))
        self.assertOnly(self.errors(), "file 'image/gone.yaml' does not exist")
        self.workflow(ci("desktop-file-validate image/gone.desktop"))
        self.assertOnly(self.errors(), "file 'image/gone.desktop' does not exist")

    def test_things_that_need_not_exist_are_not_reported(self) -> None:
        self.write("tools/x")
        for command in (
            "python3 tools/$NAME.py",
            "python3 tools/${{ matrix.script }}.py",
            "cp tools/gone.py build/gone.py",
            "mkdir -p build/x.d && touch build/y.sh",
            "echo hi > tools/out.md",
            "curl -o x.py https://example.org/a/b.py",
            "python3 /usr/lib/x/gone.py",
            "rs/target/debug/herkosd serve",
            "./restic version",
            "python3 build/obs/gen.py",
            "sh dist/run.sh",
            "pytest build/tests",
            "cargo build --manifest-path elsewhere/Cargo.toml",
            "git diff --exit-code docs/gone.md",
            "echo https://github.com/a/b/c.md",
            "ls target/release/gone.json",
            "ls tools/outdir",
            "ruff check build/generated.py",
            "python3 tools/x --out tools/results.json",
            "python3 -c 'print(1)'",
            "sh -c 'tools/gone.sh'",
            "python3 - <<'EOF'\nopen('tools/gone.py')\nEOF",
            'echo "tools/gone.py"; false || true',
            "(sudo apt-get update -qq && sudo apt-get install -y -qq dovecot-imapd) || true",
        ):
            with self.subTest(command=command):
                self.workflow(ci(command))
                self.assertEqual([e for e in self.errors() if "ci.yml:" in e], [])

    def test_unknown_working_directory_is_a_warning_and_the_step_is_skipped(self) -> None:
        self.workflow(ci("python3 tools/gone.py", directory="${{ matrix.dir }}"))
        self.assertEqual(self.errors(), [])
        self.assertOnly(self.warnings(), "warning: job 'build'")
        self.assertIn("cannot tell the working directory '${{ matrix.dir }}'", self.warnings()[0])

    def test_a_cd_to_a_variable_stops_the_checking_of_the_rest(self) -> None:
        self.workflow(ci('cd "$HOME/x" && python3 tools/gone.py'))
        self.assertEqual(self.errors(), [])


class WhichWorkflows(Tree):
    def test_every_workflow_counts_not_only_ci_yml(self) -> None:
        self.write("tools/check_a.py")
        self.workflow(ci("echo hi"))
        self.workflow(ci("python3 tools/check_a.py", job="other"), "other.yml")
        self.assertEqual(self.problems(), [])

    def test_a_workflow_that_runs_only_on_a_schedule_gives_a_warning(self) -> None:
        self.write("tools/check_a.py")
        self.workflow(
            ci("python3 tools/check_a.py", head="on:\n  schedule:\n    - cron: '1 2 * * 1'\n")
        )
        self.assertEqual(self.errors(), [])
        self.assertOnly(
            self.warnings(), "tools/check_a.py: warning: run only by .github/workflows/ci.yml"
        )

    def test_a_push_workflow_is_enough_even_if_another_is_scheduled(self) -> None:
        self.write("tools/check_a.py")
        self.workflow(ci("python3 tools/check_a.py", head="on: [workflow_dispatch]\n"))
        self.workflow(ci("python3 tools/check_a.py"), "push.yml")
        self.assertEqual(self.problems(), [])

    def test_job_names_and_step_numbers_in_messages(self) -> None:
        self.workflow(ci("python3 tools/gone.py", job="unit"))
        self.assertIn("job 'unit'", self.errors()[0])

    def test_a_workflow_that_cannot_be_read_is_an_error(self) -> None:
        self.workflow("jobs:\n  a:\n\tsteps: []\n")
        self.assertOnly(self.errors(), ".github/workflows/ci.yml: cannot read the workflow: line 3")

    def test_no_workflows_at_all(self) -> None:
        self.write("tools/check_a.py")
        found = self.errors()
        self.assertOnly(
            found, ".github/workflows: no workflow files found", "tools/check_a.py: no step"
        )

    def test_yaml_files_only(self) -> None:
        self.write("tools/check_a.py")
        self.write(".github/workflows/notes.md", "python3 tools/check_a.py\n")
        self.assertIn("no workflow files found", "\n".join(self.errors()))


class Legacy(Tree):
    def test_a_known_old_problem_is_a_warning(self) -> None:
        self.write("tools/check_a.py")
        self.workflow(ci("echo hi"))
        self.assertEqual(len(self.errors()), 1)
        legacy = frozenset({("tools/check_a.py", "not-run")})
        with mock.patch.object(cc, "LEGACY", legacy):
            self.assertEqual(self.errors(), [])
            self.assertOnly(self.warnings(), "tools/check_a.py: warning: no step")

    def test_legacy_is_for_one_rule_of_one_file(self) -> None:
        self.write("tools/check_a.py")
        self.write("tools/check_b.py")
        self.workflow(ci("echo hi"))
        legacy = frozenset({("tools/check_a.py", "not-run"), ("tools/check_b.py", "other-rule")})
        with mock.patch.object(cc, "LEGACY", legacy):
            self.assertOnly(self.errors(), "tools/check_b.py")

    def test_problems_are_sorted_errors_first_in_the_summary_counts(self) -> None:
        self.write("tools/check_a.py")
        self.write("p/pyproject.toml", PYPROJECT)
        self.workflow(ci("echo hi"))
        problems, stats = cc.check(
            self.root, ["tools/check_a.py", "p/pyproject.toml", ".github/workflows/ci.yml"]
        )
        self.assertEqual([p.warning for p in problems], [False, True])
        self.assertEqual((stats["workflows"], stats["checks"], stats["python"]), (1, 1, 1))


class Main(Tree):
    def run_main(self, args: list[str]) -> tuple[int, str, str]:
        tracked = sorted(
            p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()
        )
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(cc, "ROOT", self.root),
            mock.patch.object(cc, "tracked_files", return_value=tracked),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cc.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_wrong_call_is_exit_code_2(self) -> None:
        self.assertEqual(self.run_main(["--bogus"])[0], 2)
        self.assertEqual(self.run_main(["x"])[0], 2)

    def test_help_is_exit_code_0(self) -> None:
        code, _, err = self.run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", err)

    def test_git_failure_is_exit_code_2(self) -> None:
        boom = subprocess.CalledProcessError(128, "git")
        err = io.StringIO()
        with (
            mock.patch.object(cc, "tracked_files", side_effect=boom),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(cc.main([]), 2)
        self.assertIn("cannot list files", err.getvalue())

    def test_all_good_is_exit_code_0_with_a_summary(self) -> None:
        self.write("tools/check_a.py")
        self.write("tools/test_check_a.py")
        self.workflow(ci("python3 tools/check_a.py\n\npython3 tools/test_check_a.py"))
        code, out, err = self.run_main([])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            out,
            "1 workflows (2 run steps), 1 checks, 1 test files, 0 Python projects, "
            "0 Cargo workspaces checked: 0 errors, 0 warnings\n",
        )

    def test_problems_go_to_stderr_with_exit_code_1_and_a_summary_to_stdout(self) -> None:
        self.write("tools/check_zz.py")
        self.workflow(ci("echo hi"))
        code, out, err = self.run_main([])
        self.assertEqual(code, 1)
        self.assertIn("tools/check_zz.py: no step", err)
        self.assertIn("1 errors, 0 warnings", out)

    def test_only_warnings_is_exit_code_0(self) -> None:
        self.write("tools/check_zz.py")
        self.workflow(ci("echo hi"))
        with mock.patch.object(cc, "LEGACY", frozenset({("tools/check_zz.py", "not-run")})):
            code, out, err = self.run_main([])
        self.assertEqual(code, 0)
        self.assertIn("0 errors, 1 warnings", out)
        self.assertIn("tools/check_zz.py: warning: no step", err)

    def test_tracked_files_uses_git_ls_files(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="a\nb/c\n")
        with mock.patch.object(subprocess, "run", return_value=done) as run:
            self.assertEqual(cc.tracked_files(Path("/x")), ["a", "b/c"])
        self.assertEqual(run.call_args.args[0], ["git", "ls-files"])


if __name__ == "__main__":
    unittest.main()
