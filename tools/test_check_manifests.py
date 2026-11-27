#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_manifests.py (the links of the Herkos manifests to the repo).

Every test writes a small repo (manifest, MCP server, polkit policy, systemd unit) into a
temporary folder and checks it the way ``main()`` does; there is a test for every rule and
one for the good repo.

Run: python3 tools/test_check_manifests.py
"""

from __future__ import annotations

import contextlib
import io
import shutil
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

import check_manifests as cm  # noqa: E402  (after sys.path.insert above)

MANIFEST = f"{cm.MANIFEST_DIR}/org.vilna.mcp.demo.toml"
PACKAGE = "mcp/demo/src/vilna_mcp_demo"
SERVER = f"{PACKAGE}/server.py"
POLICY = "herkos/systemd/org.vilna.herkos.policy"
UNIT = "herkos/systemd/vilna-mcp-demo@.service"
ACTION = "org.vilna.herkos.demo.change"

LAUNCH = '[launch]\nprogram = "vilna-mcp-demo"\nmodule = "vilna_mcp_demo"\nargs = []\n'

GOOD_MANIFEST = (
    'tool = "org.vilna.mcp.demo"\n'
    'description = "Demo"\n'
    + LAUNCH
    + textwrap.dedent(
        f"""
        [operations.list]
        level = "R1"
        summary = "List"
        resource_args = ["path"]

        [operations.write]
        level = "R2"
        summary = "Change"
        undoable = true
        resource_args = ["path"]

        [operations.delete]
        level = "R3"
        summary = "Delete"
        undoable = true
        polkit = "{ACTION}"
        resource_args = ["path"]
        """
    )
)

HEADER = '''"""A server with a Ukrainian text: «Рекомендовано Vilna»."""\n'''
HELPER = """
def _tool(name, description, props, required, **annotations):
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": props, "required": required},
        "annotations": annotations,
    }
"""
GOOD_SERVER = (
    HEADER
    + HELPER
    + """
_PATH = {"type": "string"}
_READ = {"readOnlyHint": True}
TOOLS = [
    _tool("list", "Список тек", {"path": _PATH}, ["path"], **_READ),
    _tool("write", "Write", {"path": _PATH, "text": _PATH}, ["path"],
          readOnlyHint=False, destructiveHint=False),
    _tool("delete", "Delete", {"path": _PATH}, ["path"],
          readOnlyHint=False, destructiveHint=True),
]
"""
)

GOOD_POLICY = f'<policyconfig>\n  <action id="{ACTION}">\n  </action>\n</policyconfig>\n'


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tracked: list[str] = []

    def put(self, rel: str, text: str | bytes) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        if rel not in self.tracked:
            self.tracked.append(rel)

    def build(
        self,
        manifest: str = GOOD_MANIFEST,
        server: str = GOOD_SERVER,
        policy: str | None = GOOD_POLICY,
        unit: str | None = UNIT,
    ) -> None:
        self.put(MANIFEST, manifest)
        self.put(f"{PACKAGE}/__init__.py", "")
        self.put(SERVER, server)
        if policy is not None:
            self.put(POLICY, policy)
        if unit is not None:
            self.put(unit, "[Service]\nExecStart=/usr/bin/vilna-mcp-demo\n")

    def run_checks(self, only: tuple[str, ...] = ()) -> tuple[list[cm.Problem], list[cm.Row]]:
        return cm.check_files(self.root, sorted(self.tracked), only)

    def problems(self) -> list[cm.Problem]:
        return self.run_checks()[0]

    def rules(self, warning: bool | None = None) -> list[str]:
        return sorted(p.rule for p in self.problems() if warning is None or p.warning is warning)

    def messages(self, rule: str) -> list[str]:
        return [p.message for p in self.problems() if p.rule == rule]


def manifest_with(ops: str, launch: str = LAUNCH) -> str:
    return f'tool = "org.vilna.mcp.demo"\n{launch}\n{textwrap.dedent(ops)}'


class GoodRepo(Case):
    def test_good_repo_has_no_problems(self) -> None:
        self.build()
        problems, rows = self.run_checks()
        self.assertEqual([str(p) for p in problems], [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].manifest, "org.vilna.mcp.demo")
        self.assertEqual(rows[0].server, PACKAGE)
        self.assertEqual(rows[0].operations, "3/3")
        self.assertEqual(rows[0].levels, "R1×1, R2×1, R3×1")

    def test_server_code_is_never_run(self) -> None:
        marker = self.root / "ran.txt"
        evil = f'open({str(marker)!r}, "w").write("x")\nraise SystemExit(3)\n' + GOOD_SERVER
        self.build(server=evil)
        self.assertEqual(self.rules(), [])
        self.assertFalse(marker.exists())

    def test_only_limits_the_manifests(self) -> None:
        self.build()
        other = f"{cm.MANIFEST_DIR}/org.vilna.mcp.other.toml"
        self.put(other, "tool = 'org.vilna.mcp.other'\n")
        problems, rows = self.run_checks(only=(MANIFEST,))
        self.assertEqual(problems, [])
        self.assertEqual([r.manifest for r in rows], ["org.vilna.mcp.demo"])


class ManifestFiles(Case):
    def test_invalid_toml_is_an_error(self) -> None:
        self.build(manifest="tool = \n")
        (problem,) = self.problems()
        self.assertFalse(problem.warning)
        self.assertEqual(problem.path, MANIFEST)
        self.assertIn("not valid TOML", problem.message)

    def test_invalid_utf8_is_an_error(self) -> None:
        self.build(manifest="")
        self.put(MANIFEST, b"tool = '\xff\xfe'\n")
        (problem,) = self.problems()
        self.assertIn("not valid UTF-8", problem.message)

    def test_missing_file_is_an_error(self) -> None:
        self.build()
        (self.root / MANIFEST).unlink()
        (problem,) = self.problems()
        self.assertIn("cannot read the file", problem.message)

    def test_odd_values_do_not_crash(self) -> None:
        ops = """
            [operations.list]
            level = 5
            resource_args = "path"
            polkit = 3
            [operations.write]
            level = "R2"
            resource_args = [1, "path"]
            """
        self.build(manifest=manifest_with(ops))
        self.assertEqual(self.rules(False), ["tool-no-operation"])  # only "delete" is left

    def test_manifest_without_operations_is_read(self) -> None:
        self.build(manifest=manifest_with(""))
        self.assertIn("tool-no-operation", self.rules(False))

    def test_only_the_manifest_folder_is_checked(self) -> None:
        self.build()
        self.put("herkos/catalogue/io.example.app.toml", "tool = 'broken\n")
        self.assertEqual(self.rules(), [])
        self.assertEqual(
            cm.manifest_paths(self.tracked),
            [MANIFEST],
        )


class FindingTheServer(Case):
    def test_no_launch_is_a_warning(self) -> None:
        self.build(manifest=manifest_with('[operations.list]\nlevel = "R1"\n', launch=""))
        (problem,) = [p for p in self.problems() if p.rule == "server-not-found"]
        self.assertTrue(problem.warning)
        self.assertIn("no [launch]", problem.message)

    def test_module_that_is_not_in_the_repo_is_a_warning(self) -> None:
        launch = '[launch]\nprogram = "vilna-mcp-demo"\nmodule = "vilna_mcp_nothing"\n'
        self.build(manifest=manifest_with('[operations.list]\nlevel = "R1"\n', launch))
        (problem,) = [p for p in self.problems() if p.rule == "server-not-found"]
        self.assertTrue(problem.warning)
        self.assertIn("vilna_mcp_nothing", problem.message)

    def test_launch_without_module_and_program_is_a_warning(self) -> None:
        self.build(manifest=manifest_with('[operations.list]\nlevel = "R1"\n', "[launch]\n"))
        self.assertIn("neither module nor program", self.messages("server-not-found")[0])

    def test_program_without_module_and_without_script_is_a_warning(self) -> None:
        launch = '[launch]\nprogram = "vilna-mcp-demo"\n'
        self.build(manifest=manifest_with('[operations.list]\nlevel = "R1"\n', launch))
        self.assertIn("not in any [project.scripts]", self.messages("server-not-found")[0])

    def test_program_is_found_through_project_scripts(self) -> None:
        launch = '[launch]\nprogram = "vilna-mcp-demo"\n'
        self.build(manifest=GOOD_MANIFEST.replace(LAUNCH, launch))
        self.put(
            "mcp/demo/pyproject.toml",
            '[project.scripts]\nvilna-mcp-demo = "vilna_mcp_demo.server:main"\n',
        )
        problems, rows = self.run_checks()
        self.assertEqual([p.rule for p in problems], [])
        self.assertEqual(rows[0].server, SERVER)

    def test_two_programs_of_one_package_read_only_their_own_module(self) -> None:
        self.put(
            "mcp/two/pyproject.toml",
            textwrap.dedent("""
            [project.scripts]
            vilna-mcp-one = "vilna_two.one:main"
            vilna-mcp-two = "vilna_two.two:main"
            """),
        )
        self.put("mcp/two/src/vilna_two/__init__.py", "")
        self.put("mcp/two/src/vilna_two/one.py", 'TOOLS = [{"name": "a", "inputSchema": {}}]\n')
        self.put("mcp/two/src/vilna_two/two.py", 'TOOLS = [{"name": "b", "inputSchema": {}}]\n')
        for n in ("one", "two"):
            launch = f'[launch]\nprogram = "vilna-mcp-{n}"\n'
            op = "a" if n == "one" else "b"
            self.put(
                f"{cm.MANIFEST_DIR}/org.vilna.mcp.{n}.toml",
                manifest_with(f'[operations.{op}]\nlevel = "R0"\n', launch),
            )
        _, rows = self.run_checks()
        self.assertEqual([r.operations for r in rows], ["1/1", "1/1"])
        self.assertEqual(
            [r.server for r in rows],
            ["mcp/two/src/vilna_two/one.py", "mcp/two/src/vilna_two/two.py"],
        )

    def test_module_in_several_places_warns_and_uses_the_first(self) -> None:
        self.build()
        self.put("mcp/copy/src/vilna_mcp_demo/__init__.py", "")
        self.put("mcp/copy/src/vilna_mcp_demo/server.py", GOOD_SERVER)
        found = [p for p in self.problems() if p.rule == "server-not-found"]
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].warning)
        self.assertIn("several places", found[0].message)

    def test_tools_are_read_from_every_file_of_the_package(self) -> None:
        without_delete = GOOD_SERVER.split('    _tool("delete"')[0] + "]\n"
        self.build(server=without_delete)
        self.assertEqual(self.rules(False), ["operation-no-tool"])
        self.put(f"{PACKAGE}/more.py", HELPER + 'T = [_tool("delete", "d", {"path": {}}, [])]\n')
        self.assertEqual(self.rules(), [])

    def test_tests_of_the_package_are_not_read(self) -> None:
        self.build()
        self.put(f"{PACKAGE}/tests/test_x.py", HELPER + 'T = [_tool("zzz", "d", {}, [])]\n')
        self.assertEqual(self.rules(), [])

    def test_a_file_that_cannot_be_parsed_is_a_warning(self) -> None:
        self.build()
        self.put(f"{PACKAGE}/broken.py", "def (:\n")
        (problem,) = self.problems()
        self.assertTrue(problem.warning)
        self.assertEqual(problem.rule, "server-not-found")
        self.assertIn("cannot parse", problem.message)

    def test_a_file_that_cannot_be_read_is_a_warning(self) -> None:
        self.build()
        self.put(f"{PACKAGE}/latin.py", b"x = '\xff'\n")
        (problem,) = self.problems()
        self.assertTrue(problem.warning)
        self.assertIn("cannot read", problem.message)

    def test_a_tool_defined_twice_is_a_warning(self) -> None:
        self.build()
        self.put(f"{PACKAGE}/again.py", HELPER + 'T = [_tool("list", "d", {"path": 1}, [])]\n')
        (problem,) = self.problems()
        self.assertTrue(problem.warning)
        self.assertIn("defined twice", problem.message)


class ReadingTools(Case):
    def read(self, source: str, **extra: str) -> dict[str, cm.Tool]:
        sources = {"a.py": textwrap.dedent(source), **extra}
        server, notes = cm.read_server("x", sources)
        self.assertEqual(notes, [])
        return server.tools

    def test_tool_call_with_literals(self) -> None:
        tools = self.read(
            """
            T = [_tool("a", "d", {"p": 1, "q": 2}, ["p"], readOnlyHint=True, other="x")]
            """
        )
        self.assertEqual(tools["a"].properties, frozenset({"p", "q"}))
        self.assertEqual(tools["a"].hints, {"readOnlyHint": True, "other": "x"})

    def test_tool_helper_called_tool(self) -> None:
        tools = self.read('T = [tool("a", "d", {}, [], **READ)]\nREAD = {"readOnlyHint": True}\n')
        self.assertEqual(tools["a"].hints, {"readOnlyHint": True})

    def test_properties_from_a_constant_and_a_merge(self) -> None:
        tools = self.read(
            """
            BASE = {"x": 1}
            PROPS = {**BASE, "y": 2}
            T = [_tool("a", "d", PROPS, []), _tool("b", "d", BASE, [])]
            """
        )
        self.assertEqual(tools["a"].properties, frozenset({"x", "y"}))
        self.assertEqual(tools["b"].properties, frozenset({"x"}))

    def test_unreadable_properties_are_none_not_empty(self) -> None:
        tools = self.read(
            """
            T = [
                _tool("a", "d", make(), []),
                _tool("b", "d", {key: 1}, []),
                _tool("c", "d"),
                _tool("d", "d", {**unknown}, []),
            ]
            """
        )
        self.assertEqual({k: t.properties for k, t in tools.items()}, dict.fromkeys("abcd"))

    def test_constants_that_point_at_each_other_do_not_loop(self) -> None:
        tools = self.read('A = B\nB = A\nT = [_tool("a", "d", A, [], **B)]\n')
        self.assertIsNone(tools["a"].properties)
        self.assertEqual(tools["a"].hints, {})

    def test_constants_of_another_file_of_the_package(self) -> None:
        tools = self.read(
            'T = [tool("a", "d", {}, [], **READ)]\n',
            **{"b.py": 'READ = {"readOnlyHint": True, "destructiveHint": False}\n'},
        )
        self.assertEqual(tools["a"].hints, {"readOnlyHint": True, "destructiveHint": False})

    def test_own_constant_wins_over_the_one_of_another_file(self) -> None:
        tools = self.read(
            'READ = {"readOnlyHint": False}\nT = [tool("a", "d", {}, [], **READ)]\n',
            **{"0-first.py": 'READ = {"readOnlyHint": True}\n'},
        )
        self.assertEqual(tools["a"].hints, {"readOnlyHint": False})

    def test_annotated_constant(self) -> None:
        tools = self.read(
            'READ: dict = {"readOnlyHint": True}\nT = [tool("a", "d", {}, [], **READ)]\n'
        )
        self.assertEqual(tools["a"].hints, {"readOnlyHint": True})

    def test_dict_style_tool(self) -> None:
        tools = self.read(
            """
            SCHEMA = {"type": "object", "properties": {"app_id": {}}}
            TOOLS = [{
                "name": "launch",
                "inputSchema": SCHEMA,
                "annotations": {"readOnlyHint": False, "destructiveHint": False},
            }]
            """
        )
        self.assertEqual(tools["launch"].properties, frozenset({"app_id"}))
        self.assertEqual(tools["launch"].hints, {"readOnlyHint": False, "destructiveHint": False})

    def test_other_dicts_and_calls_are_not_tools(self) -> None:
        tools = self.read(
            """
            INFO = {"name": "server", "version": "1"}
            ENTRY = {"name": entry.name, "inputSchema": {}}
            OTHER = {"name": "x", "type": "file"}
            f = some("a", "b", {}, [])
            g = _tool(name, "d", {}, [])
            h = _tool("only-name")
            """
        )
        self.assertEqual(tools, {})

    def test_the_helper_itself_is_not_a_tool(self) -> None:
        self.assertEqual(self.read(HELPER), {})

    def test_hint_that_is_not_a_literal_is_dropped(self) -> None:
        tools = self.read('T = [_tool("a", "d", {}, [], readOnlyHint=flag, openWorldHint=None)]\n')
        self.assertEqual(tools["a"].hints, {})


class OperationsAgainstTools(Case):
    def test_operation_without_a_tool_is_an_error(self) -> None:
        self.build(manifest=GOOD_MANIFEST + '\n[operations.extra]\nlevel = "R2"\n')
        (problem,) = self.problems()
        self.assertFalse(problem.warning)
        self.assertEqual(problem.rule, "operation-no-tool")
        self.assertEqual(problem.path, MANIFEST)
        self.assertIn("operations.extra", problem.message)

    def test_tool_without_an_operation_is_an_error(self) -> None:
        self.build(server=GOOD_SERVER.replace("]\n", '    _tool("more", "d", {}, []),\n]\n', 1))
        (problem,) = self.problems()
        self.assertFalse(problem.warning)
        self.assertEqual(problem.rule, "tool-no-operation")
        self.assertIn("'more'", problem.message)

    def test_forbidden_operation_needs_no_tool(self) -> None:
        self.build(manifest=GOOD_MANIFEST + '\n[operations.format]\nlevel = "forbidden"\n')
        problems, rows = self.run_checks()
        self.assertEqual(problems, [])
        self.assertEqual(rows[0].operations, "4/3")
        self.assertIn("forbidden×1", rows[0].levels)

    def test_forbidden_operation_may_be_a_tool(self) -> None:
        manifest = GOOD_MANIFEST.replace('level = "R3"', 'level = "forbidden"').replace(
            f'polkit = "{ACTION}"\n', ""
        )
        self.build(manifest=manifest)
        self.assertEqual(self.rules(), [])

    def test_name_with_different_case_is_not_the_same(self) -> None:
        self.build(manifest=GOOD_MANIFEST.replace("operations.list", "operations.List"))
        self.assertEqual(self.rules(False), ["operation-no-tool", "tool-no-operation"])


class ResourceArgs(Case):
    def test_resource_arg_that_is_not_a_property_is_an_error(self) -> None:
        self.build(
            manifest=GOOD_MANIFEST.replace(
                'resource_args = ["path"]', 'resource_args = ["file"]', 1
            )
        )
        (problem,) = self.problems()
        self.assertFalse(problem.warning)
        self.assertEqual(problem.rule, "resource-arg")
        self.assertIn("'file'", problem.message)
        self.assertIn("path", problem.message)

    def test_every_resource_arg_is_checked(self) -> None:
        self.build(
            manifest=GOOD_MANIFEST.replace(
                'resource_args = ["path"]', 'resource_args = ["path", "text"]', 1
            )
        )
        self.assertEqual(self.rules(), ["resource-arg"])  # list has only "path"

    def test_resource_arg_of_a_forbidden_or_missing_tool_is_not_checked(self) -> None:
        manifest = GOOD_MANIFEST + '\n[operations.x]\nlevel = "forbidden"\nresource_args = ["q"]\n'
        self.build(manifest=manifest)
        self.assertEqual(self.rules(), [])

    def test_properties_that_cannot_be_read_skip_the_check(self) -> None:
        server = GOOD_SERVER.replace('{"path": _PATH}, ["path"], **_READ', "props(), [], **_READ")
        self.build(server=server)
        self.assertEqual(self.rules(), [])

    def test_tool_without_properties_in_the_call_skips_the_check(self) -> None:
        server = GOOD_SERVER.replace(
            '_tool("list", "Список тек", {"path": _PATH}, ["path"], **_READ)',
            '_tool("list", "Список тек", **_READ)',
        )
        self.assertIn('"Список тек", **_READ', server)
        self.build(server=server)
        self.assertEqual(self.rules(), [])


class Hints(Case):
    def test_read_only_tool_at_r2_is_a_warning(self) -> None:
        self.build(manifest=GOOD_MANIFEST.replace('level = "R1"', 'level = "R2"'))
        (problem,) = self.problems()
        self.assertTrue(problem.warning)
        self.assertEqual(problem.rule, "read-only-level")
        self.assertIn("readOnlyHint=True", problem.message)
        self.assertIn("R2", problem.message)

    def test_read_only_tool_at_r0_is_a_warning(self) -> None:
        self.build(manifest=GOOD_MANIFEST.replace('level = "R1"', 'level = "R0"'))
        (problem,) = self.problems()
        self.assertTrue(problem.warning)
        self.assertEqual(problem.rule, "read-only-level")
        self.assertIn("neutral", problem.message)

    def test_read_only_tool_at_r1_is_good(self) -> None:
        self.build()
        self.assertEqual(self.rules(), [])

    def test_destructive_tool_below_r3_is_an_error(self) -> None:
        for level in ("R0", "R1", "R2"):
            with self.subTest(level=level):
                manifest = GOOD_MANIFEST.replace('level = "R3"', f'level = "{level}"').replace(
                    f'polkit = "{ACTION}"\n', ""
                )
                self.build(manifest=manifest)
                problems = [p for p in self.problems() if p.rule == "destructive-level"]
                self.assertEqual(len(problems), 1)
                self.assertFalse(problems[0].warning)
                self.assertIn(f"{level}, lower than R3", problems[0].message)

    def test_destructive_tool_at_r3_and_r4_is_good(self) -> None:
        for level in ("R3", "R4"):
            with self.subTest(level=level):
                self.build(manifest=GOOD_MANIFEST.replace('level = "R3"', f'level = "{level}"'))
                self.assertEqual(self.rules(), [])

    def test_destructive_false_is_good_at_r2(self) -> None:
        self.build()
        self.assertNotIn("destructive-level", self.rules())

    def test_read_with_undoable_is_a_warning(self) -> None:
        self.build(manifest=GOOD_MANIFEST.replace('level = "R1"', 'level = "R1"\nundoable = true'))
        (problem,) = self.problems()
        self.assertTrue(problem.warning)
        self.assertEqual(problem.rule, "undoable-read")
        self.assertIn("operations.list", problem.message)

    def test_undoable_at_r2_is_good_and_r1_without_undoable_is_good(self) -> None:
        self.build()
        self.assertEqual(self.rules(), [])

    def test_undoable_read_is_found_without_a_server_too(self) -> None:
        manifest = manifest_with('[operations.list]\nlevel = "R1"\nundoable = true\n', "")
        self.build(manifest=manifest)
        self.assertEqual(self.rules(True), ["server-not-found", "undoable-read"])


class PolkitActions(Case):
    def test_undeclared_action_is_an_error(self) -> None:
        self.build(policy=GOOD_POLICY.replace(ACTION, "org.vilna.herkos.demo.other"))
        (problem,) = self.problems()
        self.assertFalse(problem.warning)
        self.assertEqual(problem.rule, "polkit-action")
        self.assertIn(ACTION, problem.message)
        self.assertIn("operations.delete", problem.message)

    def test_no_policy_file_at_all_is_an_error(self) -> None:
        self.build(policy=None)
        self.assertEqual(self.rules(), ["polkit-action"])

    def test_action_of_another_policy_file_is_good(self) -> None:
        self.build(policy=None)
        self.put("other/dir/org.vilna.demo.policy", GOOD_POLICY)
        self.assertEqual(self.rules(), [])

    def test_policy_with_doctype_and_single_quotes(self) -> None:
        policy = (
            '<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration '
            f'1.0//EN" "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">\n'
            f"<policyconfig><action  id='{ACTION}'></action></policyconfig>\n"
        )
        self.build(policy=policy)
        self.assertEqual(self.rules(), [])

    def test_a_policy_that_is_not_tracked_does_not_count(self) -> None:
        self.build(policy=None)
        self.put("x/org.vilna.herkos.policy.bak", GOOD_POLICY)
        self.assertEqual(self.rules(), ["polkit-action"])

    def test_action_is_matched_whole(self) -> None:
        self.build(policy=GOOD_POLICY.replace(ACTION, ACTION + "-more"))
        self.assertEqual(self.rules(), ["polkit-action"])


class SystemdUnits(Case):
    def test_missing_unit_is_a_warning(self) -> None:
        self.build(unit=None)
        (problem,) = self.problems()
        self.assertTrue(problem.warning)
        self.assertEqual(problem.rule, "systemd-unit")
        self.assertIn("vilna-mcp-demo@.service", problem.message)

    def test_unit_of_another_program_does_not_count(self) -> None:
        self.build(unit="herkos/systemd/vilna-mcp-other@.service")
        self.assertEqual(self.rules(True), ["systemd-unit"])

    def test_plain_service_is_good(self) -> None:
        self.build(unit="gloxi/systemd/vilna-mcp-demo.service")
        self.assertEqual(self.rules(), [])

    def test_unit_in_any_folder_is_good(self) -> None:
        self.build(unit="some/where/else/vilna-mcp-demo@.service")
        self.assertEqual(self.rules(), [])

    def test_catalogue_program_needs_no_unit(self) -> None:
        launch = '[launch]\nprogram = "vilna-mcp-ext-writer"\nmodule = "vilna_mcp_demo"\n'
        self.build(manifest=GOOD_MANIFEST.replace(LAUNCH, launch), unit=None)
        self.assertEqual(self.rules(), [])

    def test_manifest_without_program_has_no_unit_warning(self) -> None:
        launch = '[launch]\nmodule = "vilna_mcp_demo"\n'
        self.build(manifest=GOOD_MANIFEST.replace(LAUNCH, launch), unit=None)
        self.assertEqual(self.rules(), [])


class Legacy(Case):
    def test_known_old_problem_is_only_a_warning(self) -> None:
        self.build(manifest=GOOD_MANIFEST + '\n[operations.extra]\nlevel = "R2"\n')
        legacy = frozenset({(MANIFEST, "operation-no-tool", "extra")})
        with mock.patch.object(cm, "LEGACY", legacy):
            (problem,) = self.problems()
        self.assertTrue(problem.warning)
        self.assertEqual(problem.rule, "operation-no-tool")
        self.assertIn("warning:", str(problem))

    def test_legacy_is_per_operation(self) -> None:
        manifest = GOOD_MANIFEST + '\n[operations.a]\nlevel = "R2"\n[operations.b]\nlevel = "R2"\n'
        self.build(manifest=manifest)
        legacy = frozenset({(MANIFEST, "operation-no-tool", "a")})
        with mock.patch.object(cm, "LEGACY", legacy):
            problems = self.problems()
        self.assertEqual([(p.name, p.warning) for p in problems], [("a", True), ("b", False)])

    def test_legacy_with_empty_name_covers_the_whole_manifest_rule(self) -> None:
        manifest = GOOD_MANIFEST + '\n[operations.a]\nlevel = "R2"\n[operations.b]\nlevel = "R2"\n'
        self.build(manifest=manifest)
        with mock.patch.object(cm, "LEGACY", frozenset({(MANIFEST, "operation-no-tool", "")})):
            self.assertTrue(all(p.warning for p in self.problems()))

    def test_legacy_of_another_manifest_or_rule_does_not_help(self) -> None:
        self.build(manifest=GOOD_MANIFEST + '\n[operations.extra]\nlevel = "R2"\n')
        for entry in (
            ("other.toml", "operation-no-tool", "extra"),
            (MANIFEST, "resource-arg", "extra"),
        ):
            with self.subTest(entry=entry), mock.patch.object(cm, "LEGACY", frozenset({entry})):
                (problem,) = self.problems()
                self.assertFalse(problem.warning)

    def test_legacy_entries_name_real_rules(self) -> None:
        rules = {"operation-no-tool", "tool-no-operation", "resource-arg", "destructive-level"}
        self.assertLessEqual({rule for _, rule, _ in cm.LEGACY}, rules)


class Table(Case):
    def test_table_lists_every_manifest(self) -> None:
        self.build()
        _, rows = self.run_checks()
        lines = cm.table(rows).splitlines()
        self.assertEqual(lines[0], "| Manifest | Server | Operations (manifest/server) | Levels |")
        self.assertEqual(len(lines), 3)
        self.assertIn(f"`{PACKAGE}`", lines[2])
        self.assertIn("R1×1, R2×1, R3×1", lines[2])

    def test_table_without_a_server(self) -> None:
        self.build(manifest=manifest_with('[operations.list]\nlevel = "R1"\n', ""))
        _, rows = self.run_checks()
        self.assertIn("| org.vilna.mcp.demo | - | 1/- | R1×1 |", cm.table(rows))


class Main(Case):
    def call(self, args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(cm, "ROOT", self.root),
            mock.patch.object(cm, "tracked_files", lambda root: sorted(self.tracked)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cm.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_good_repo_exits_0_and_prints_the_table(self) -> None:
        self.build()
        code, out, err = self.call([])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("| org.vilna.mcp.demo |", out)
        self.assertIn("1 manifests checked: 0 errors, 0 warnings", out)

    def test_error_exits_1_and_goes_to_stderr(self) -> None:
        self.build(manifest=GOOD_MANIFEST + '\n[operations.extra]\nlevel = "R2"\n')
        code, out, err = self.call([])
        self.assertEqual(code, 1)
        self.assertIn(f"{MANIFEST}: operations.extra:", err)
        self.assertIn("1 errors, 0 warnings", out)

    def test_warnings_alone_exit_0(self) -> None:
        self.build(unit=None)
        code, out, err = self.call([])
        self.assertEqual(code, 0)
        self.assertIn(f"{MANIFEST}: warning:", err)
        self.assertIn("0 errors, 1 warnings", out)

    def test_wrong_option_exits_2(self) -> None:
        self.build()
        code, _, err = self.call(["--nope"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)

    def test_help_exits_0(self) -> None:
        self.build()
        code, _, err = self.call(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", err)

    def test_unknown_manifest_exits_2(self) -> None:
        self.build()
        code, _, err = self.call(["herkos/other.toml"])
        self.assertEqual(code, 2)
        self.assertIn("not a tracked Herkos manifest: herkos/other.toml", err)

    def test_a_manifest_argument_with_backslashes(self) -> None:
        self.build()
        code, out, _ = self.call([MANIFEST.replace("/", "\\")])
        self.assertEqual(code, 0)
        self.assertIn("1 manifests checked", out)

    def test_no_manifests_exits_2(self) -> None:
        self.put("README.md", "x")
        code, _, err = self.call([])
        self.assertEqual(code, 2)
        self.assertIn("no tracked manifests", err)

    def test_git_failure_exits_2(self) -> None:
        err = io.StringIO()
        boom = subprocess.CalledProcessError(128, "git")
        with (
            mock.patch.object(cm, "tracked_files", side_effect=boom),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(cm.main([]), 2)
        self.assertIn("cannot list files with git ls-files", err.getvalue())


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class GitFiles(unittest.TestCase):
    def test_tracked_files_lists_only_what_git_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "a b.toml").write_text("x", encoding="utf-8")
            (root / "untracked.toml").write_text("x", encoding="utf-8")
            subprocess.run(["git", "add", "a b.toml"], cwd=root, check=True)
            self.assertEqual(cm.tracked_files(root), ["a b.toml"])


@unittest.skipUnless(
    shutil.which("git") and (ROOT / ".git").exists(), "not a git checkout of the repo"
)
class WholeRepo(unittest.TestCase):
    def test_the_manifests_of_the_repo_pass(self) -> None:
        tracked = cm.tracked_files(ROOT)
        problems, rows = cm.check_files(ROOT, tracked)
        self.assertEqual([str(p) for p in problems if not p.warning], [])
        self.assertEqual(len(rows), len(cm.manifest_paths(tracked)))
        self.assertGreater(len(rows), 0)


if __name__ == "__main__":
    unittest.main()
