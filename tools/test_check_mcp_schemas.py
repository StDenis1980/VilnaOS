#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_mcp_schemas.py (the check of the MCP tool schemas).

Every test writes a small MCP server (``mcp/demo/src/vilna_mcp_demo/...``) into a
temporary folder and checks it the way ``main()`` does; there is a test for every
rule, for the ``ast`` reader and for the way files are found, and one for a
good server. The last test runs the check on the repo itself.

Run: python3 tools/test_check_mcp_schemas.py
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_mcp_schemas as cm  # noqa: E402  (after sys.path.insert above)

PKG = "mcp/demo/src/vilna_mcp_demo"
SERVER = f"{PKG}/server.py"
HEADER = (
    "# SPDX-License-Identifier: Apache-2.0\n"
    '"""A demo server."""\n\n'
    "from __future__ import annotations\n\n"
)
BUILDER = """
def _tool(name, description, props, required, **annotations):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": annotations,
    }

"""
GOOD_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "maxLength": 4096},
        "mode": {"type": "string", "enum": ["a", "b"]},
        "count": {"type": "integer", "minimum": 1, "maximum": 10},
        "ratio": {"type": "number", "minimum": 0, "maximum": 1},
        "flag": {"type": "boolean"},
        "names": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 20}},
    },
    "required": ["path"],
    "additionalProperties": False,
}


def tool_source(schema: object, name: str = "demo", extra: str = "") -> str:
    """A server whose only tool has this ``inputSchema`` (a Python value written as source)."""
    return (
        f'{HEADER}TOOLS = [{{"name": {name!r}, "description": "Does it", '
        f'"inputSchema": {schema!r}{extra}}}]\n'
    )


def check(files: dict[str, str | bytes], paths: list[str] | None = None):
    """Write the files into a temporary repo and check them; returns (tools, servers, problems)."""
    with tempfile.TemporaryDirectory() as tmp:
        for name, content in files.items():
            target = Path(tmp) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            target.write_bytes(data)
        return cm.check_files(Path(tmp), paths if paths is not None else list(files))


def errors_of(files: dict[str, str | bytes]) -> list[str]:
    return [str(p) for p in check(files)[2] if not p.warning]


def warnings_of(files: dict[str, str | bytes]) -> list[str]:
    return [str(p) for p in check(files)[2] if p.warning]


def schema_errors(schema: object, **kw: str) -> list[str]:
    return errors_of({SERVER: tool_source(schema, **kw)})


def schema_warnings(schema: object) -> list[str]:
    return warnings_of({SERVER: tool_source(schema)})


def with_props(props: dict, **top: object) -> dict:
    schema: dict = {
        "type": "object",
        "properties": props,
        "required": [],
        "additionalProperties": False,
    }
    schema.update(top)
    return schema


def one(problems: list[str], needle: str) -> bool:
    return any(needle in p for p in problems)


class GoodServer(unittest.TestCase):
    def test_a_good_schema_has_no_problems(self) -> None:
        tools, servers, problems = check({SERVER: tool_source(GOOD_SCHEMA)})
        self.assertEqual((tools, servers, problems), (1, 1, []))

    def test_the_helper_style_of_the_repo_is_read(self) -> None:
        source = (
            HEADER
            + BUILDER
            + '_P = {"type": "string", "maxLength": 10}\n'
            + 'TOOLS = [_tool("a", "A", {"p": _P}, ["p"], readOnlyHint=True),\n'
            + '         _tool("b", "B", {}, [])]\n'
        )
        tools, _, problems = check({SERVER: source})
        self.assertEqual((tools, problems), (2, []))

    def test_message_names_file_tool_line_and_property(self) -> None:
        source = tool_source(with_props({"q": {"type": "string"}}))
        (message,) = warnings_of({SERVER: source})
        self.assertEqual(
            message,
            f'{SERVER}: warning: tool "demo" (line 6): properties.q: string without maxLength',
        )


class Structure(unittest.TestCase):
    def test_type_must_be_object(self) -> None:
        schema = {**GOOD_SCHEMA, "type": "array"}
        self.assertTrue(one(schema_errors(schema), "type must be 'object', not 'array'"))

    def test_missing_type(self) -> None:
        schema = {k: v for k, v in GOOD_SCHEMA.items() if k != "type"}
        self.assertTrue(one(schema_errors(schema), "type is missing"))

    def test_properties_missing(self) -> None:
        schema = {"type": "object", "additionalProperties": False}
        self.assertTrue(one(schema_errors(schema), "no properties"))

    def test_properties_not_an_object(self) -> None:
        schema = {"type": "object", "properties": ["a"], "additionalProperties": False}
        self.assertTrue(one(schema_errors(schema), "properties must be an object"))

    def test_required_must_be_in_properties(self) -> None:
        schema = {**GOOD_SCHEMA, "required": ["path", "nope", "zip"]}
        errors = schema_errors(schema)
        self.assertTrue(one(errors, "required 'nope' is not in properties"))
        self.assertTrue(one(errors, "required 'zip' is not in properties"))
        self.assertEqual(len(errors), 2)

    def test_required_twice(self) -> None:
        schema = {**GOOD_SCHEMA, "required": ["path", "path"]}
        self.assertTrue(one(schema_errors(schema), "required lists 'path' twice"))

    def test_required_not_a_list(self) -> None:
        schema = {**GOOD_SCHEMA, "required": "path"}
        self.assertTrue(one(schema_errors(schema), "required must be a list of names"))

    def test_required_with_something_that_is_not_a_name(self) -> None:
        schema = {**GOOD_SCHEMA, "required": [["path"], 3]}
        errors = schema_errors(schema)
        self.assertEqual(sum("which is not a name" in e for e in errors), 2)

    def test_required_may_be_left_out(self) -> None:
        schema = {k: v for k, v in GOOD_SCHEMA.items() if k != "required"}
        self.assertEqual(schema_errors(schema), [])

    def test_additional_properties_must_be_false(self) -> None:
        for value in (True, {"type": "string"}, 0, None):
            with self.subTest(value=value):
                schema = {**GOOD_SCHEMA, "additionalProperties": value}
                self.assertTrue(one(schema_errors(schema), "additionalProperties is not false"))

    def test_additional_properties_missing(self) -> None:
        schema = {k: v for k, v in GOOD_SCHEMA.items() if k != "additionalProperties"}
        self.assertTrue(one(schema_errors(schema), "additionalProperties is not false"))

    def test_additional_properties_missing_and_no_properties_is_one_pair_of_errors(self) -> None:
        errors = schema_errors({"type": "object"})
        self.assertEqual(len(errors), 2)

    def test_inputschema_is_not_an_object(self) -> None:
        for value in ("object", ["x"], 5, None):
            with self.subTest(value=value):
                self.assertTrue(one(schema_errors(value), "a schema must be an object"))

    def test_tool_without_inputschema(self) -> None:
        source = HEADER + 'TOOLS = [{"name": "demo", "description": "x"}]\n'
        self.assertTrue(one(errors_of({SERVER: source}), "the tool has no inputSchema"))

    def test_tool_that_is_not_a_dictionary(self) -> None:
        source = HEADER + 'TOOLS = ["demo", ("a",)]\n'
        errors = errors_of({SERVER: source})
        self.assertEqual(sum("is not a dictionary" in e for e in errors), 2)


class ToolEntry(unittest.TestCase):
    def test_bad_names(self) -> None:
        for name in ("", "a b", "naïve", "x" * 129, "a/b", "a;b"):
            with self.subTest(name=name):
                self.assertTrue(one(schema_errors(GOOD_SCHEMA, name=name), "is not 1-128"))

    def test_name_that_is_not_a_string(self) -> None:
        source = HEADER + 'TOOLS = [{"name": 5, "description": "x", "inputSchema": {}}]\n'
        self.assertTrue(one(errors_of({SERVER: source}), "is not 1-128"))

    def test_good_names(self) -> None:
        for name in ("a", "list_events", "plan.set-user", "A1", "x" * 128):
            with self.subTest(name=name):
                self.assertEqual(schema_errors(GOOD_SCHEMA, name=name), [])

    def test_missing_name_is_an_error(self) -> None:
        source = HEADER + 'TOOLS = [{"description": "x", "inputSchema": {}}]\n'
        self.assertTrue(one(errors_of({SERVER: source}), "has no readable name"))

    def test_duplicate_names_in_one_list(self) -> None:
        tool = f'{{"name": "t", "description": "x", "inputSchema": {GOOD_SCHEMA!r}}}'
        errors = errors_of({SERVER: f"{HEADER}TOOLS = [{tool}, {tool}]\n"})
        self.assertEqual(sum("used by another tool" in e for e in errors), 1)

    def test_same_name_in_two_servers_is_fine(self) -> None:
        other = "mcp/other/src/vilna_mcp_other/server.py"
        files = {SERVER: tool_source(GOOD_SCHEMA), other: tool_source(GOOD_SCHEMA)}
        self.assertEqual(errors_of(files), [])

    def test_description_missing_or_empty_is_a_warning(self) -> None:
        for extra in ('"description": ""', '"description": "  "', ""):
            with self.subTest(extra=extra):
                source = HEADER + (
                    f'TOOLS = [{{"name": "t", {extra + ", " if extra else ""}'
                    f'"inputSchema": {GOOD_SCHEMA!r}}}]\n'
                )
                self.assertEqual(errors_of({SERVER: source}), [])
                self.assertTrue(one(warnings_of({SERVER: source}), "the tool has no description"))

    def test_annotations_must_be_booleans(self) -> None:
        extra = ', "annotations": {"readOnlyHint": "yes"}'
        errors = schema_errors(GOOD_SCHEMA, extra=extra)
        self.assertTrue(one(errors, "readOnlyHint must be true or false"))

    def test_annotations_must_not_contradict(self) -> None:
        extra = ', "annotations": {"readOnlyHint": True, "destructiveHint": True}'
        self.assertTrue(one(schema_errors(GOOD_SCHEMA, extra=extra), "both true"))

    def test_annotations_must_be_an_object(self) -> None:
        errors = schema_errors(GOOD_SCHEMA, extra=', "annotations": [1]')
        self.assertTrue(one(errors, "annotations: must be an object"))

    def test_good_annotations(self) -> None:
        extra = ', "annotations": {"readOnlyHint": False, "destructiveHint": True}'
        self.assertEqual(schema_errors(GOOD_SCHEMA, extra=extra), [])


class Properties(unittest.TestCase):
    def errors(self, prop: object) -> list[str]:
        return schema_errors(with_props({"p": prop}))

    def warnings(self, prop: object) -> list[str]:
        return schema_warnings(with_props({"p": prop}))

    def test_unknown_type(self) -> None:
        self.assertTrue(one(self.errors({"type": "str"}), "unknown type 'str'"))

    def test_type_list_is_read(self) -> None:
        self.assertEqual(self.errors({"type": ["string", "null"], "maxLength": 3}), [])
        self.assertTrue(one(self.errors({"type": ["string", "date"]}), "unknown type 'date'"))

    def test_type_that_is_not_a_name(self) -> None:
        for value in ({"a": 1}, [], 7):
            with self.subTest(value=value):
                self.assertTrue(one(self.errors({"type": value}), "type must be"))
        self.assertTrue(one(self.errors({"type": [["x"]]}), "unknown type ['x']"))

    def test_enum_must_not_be_empty(self) -> None:
        self.assertTrue(one(self.errors({"type": "string", "enum": []}), "non-empty list"))
        self.assertTrue(one(self.errors({"type": "string", "enum": "ab"}), "non-empty list"))

    def test_enum_with_duplicates(self) -> None:
        errors = self.errors({"type": "string", "enum": ["a", "b", "a"]})
        self.assertTrue(one(errors, "enum lists 'a' twice"))

    def test_enum_one_and_true_are_not_duplicates(self) -> None:
        self.assertEqual(self.errors({"enum": [1, True, "1"]}), [])

    def test_enum_members_must_have_the_type(self) -> None:
        errors = self.errors({"type": "string", "enum": ["a", 2]})
        self.assertTrue(one(errors, "enum member 2 is not of type string"))
        errors = self.errors({"type": "integer", "enum": [1, 2.5]})
        self.assertTrue(one(errors, "enum member 2.5 is not of type integer"))

    def test_enum_with_unhashable_members(self) -> None:
        self.assertEqual(self.errors({"enum": [{"a": 1}, [1]]}), [])

    def test_min_more_than_max(self) -> None:
        self.assertTrue(
            one(self.errors({"type": "integer", "minimum": 5, "maximum": 1}), "minimum 5")
        )
        self.assertTrue(
            one(self.errors({"type": "string", "minLength": 9, "maxLength": 2}), "minLength 9")
        )
        self.assertTrue(
            one(
                self.errors(
                    {"type": "array", "items": {"type": "boolean"}, "minItems": 3, "maxItems": 1}
                ),
                "minItems 3",
            )
        )

    def test_limits_must_be_sane_numbers(self) -> None:
        cases = (
            ({"type": "string", "maxLength": -1}, "maxLength must be a non-negative integer"),
            ({"type": "string", "maxLength": True}, "maxLength must be a non-negative integer"),
            ({"type": "string", "maxLength": 2.5}, "maxLength must be a non-negative integer"),
            ({"type": "string", "maxLength": "9"}, "maxLength must be a non-negative integer"),
            ({"type": "array", "items": {"type": "null"}, "maxItems": -2}, "maxItems must be"),
            ({"type": "integer", "minimum": "1", "maximum": 3}, "minimum must be a finite number"),
            ({"type": "number", "minimum": True, "maximum": 2}, "minimum must be a finite number"),
        )
        for prop, text in cases:
            with self.subTest(prop=prop):
                self.assertTrue(one(self.errors(prop), text), self.errors(prop))

    def test_infinite_limits(self) -> None:
        for low, high, text in (("1", "1e999", "maximum"), ("-1e999", "1", "minimum")):
            with self.subTest(low=low, high=high):
                prop = f'{{"type": "number", "minimum": {low}, "maximum": {high}}}'
                source = HEADER + (
                    'TOOLS = [{"name": "t", "description": "d", "inputSchema": {"type": "object", '
                    f'"properties": {{"p": {prop}}}, "required": [], '
                    '"additionalProperties": False}}]\n'
                )
                self.assertTrue(one(errors_of({SERVER: source}), f"{text} must be a finite number"))

    def test_equal_limits_are_fine(self) -> None:
        self.assertEqual(self.errors({"type": "integer", "minimum": 3, "maximum": 3}), [])
        self.assertEqual(self.errors({"type": "string", "minLength": 2, "maxLength": 2}), [])
        array = {"type": "array", "items": {"type": "null"}, "minItems": 2, "maxItems": 2}
        self.assertEqual(self.errors(array), [])

    def test_property_that_is_not_a_schema(self) -> None:
        for prop in ("string", 3, ["a"], None):
            with self.subTest(prop=prop):
                self.assertTrue(one(self.errors(prop), "a schema must be an object"))

    def test_property_name_that_is_not_a_string(self) -> None:
        errors = schema_errors(with_props({1: {"type": "boolean"}}))
        self.assertTrue(one(errors, "property name 1 is not a string"))

    def test_nested_object_is_checked_like_the_top(self) -> None:
        inner = {
            "type": "object",
            "properties": {"a": {"type": "boolean"}},
            "required": ["b"],
            "additionalProperties": False,
        }
        self.assertTrue(one(self.errors(inner), "properties.p: required 'b' is not in properties"))

    def test_anyof_branches_are_checked(self) -> None:
        schema = {"anyOf": [{"type": "string"}, {"type": "oops"}]}
        errors = self.errors(schema)
        self.assertTrue(one(errors, "properties.p.anyOf[1]: unknown type 'oops'"))
        self.assertTrue(
            one(self.warnings(schema), "properties.p.anyOf[0]: string without maxLength")
        )

    def test_combinator_must_be_a_list(self) -> None:
        self.assertTrue(
            one(self.errors({"oneOf": {"type": "string"}}), "oneOf must be a non-empty list")
        )
        self.assertTrue(one(self.errors({"allOf": []}), "allOf must be a non-empty list"))


class Warnings(unittest.TestCase):
    def warnings(self, prop: object) -> list[str]:
        return schema_warnings(with_props({"p": prop}))

    def assert_warns(self, prop: object, text: str) -> None:
        found = self.warnings(prop)
        self.assertTrue(one(found, text), found)

    def assert_quiet(self, prop: object) -> None:
        self.assertEqual(self.warnings(prop), [])

    def test_string_without_maxlength(self) -> None:
        self.assert_warns({"type": "string"}, "string without maxLength")
        self.assert_warns({"type": "string", "minLength": 1}, "string without maxLength")
        self.assert_warns({"type": "string", "pattern": "^a+$"}, "string without maxLength")

    def test_string_with_enum_or_const_is_bounded(self) -> None:
        self.assert_quiet({"type": "string", "enum": ["a"]})
        self.assert_quiet({"type": "string", "const": "a"})
        self.assert_quiet({"type": "string", "maxLength": 0})

    def test_array_without_maxitems_and_items(self) -> None:
        self.assert_warns({"type": "array", "items": {"type": "boolean"}}, "array without maxItems")
        self.assert_warns({"type": "array", "maxItems": 3}, "array without items")

    def test_array_items_are_checked(self) -> None:
        prop = {"type": "array", "maxItems": 2, "items": {"type": "string"}}
        self.assert_warns(prop, "properties.p[]: string without maxLength")

    def test_array_of_arrays(self) -> None:
        inner = {"type": "array", "items": {"type": "integer"}}
        found = self.warnings({"type": "array", "maxItems": 2, "items": inner})
        self.assertTrue(one(found, "properties.p[]: array without maxItems"))
        self.assertTrue(one(found, "properties.p[][]: integer without minimum and maximum"))

    def test_prefix_items_are_checked(self) -> None:
        prop = {
            "type": "array",
            "maxItems": 2,
            "prefixItems": [{"type": "string"}, {"type": "null"}],
        }
        found = self.warnings(prop)
        self.assertTrue(one(found, "properties.p[0]: string without maxLength"))
        self.assertFalse(one(found, "array without items"))

    def test_numbers_without_bounds(self) -> None:
        for kind in ("integer", "number"):
            with self.subTest(kind=kind):
                self.assert_warns({"type": kind}, f"{kind} without minimum and maximum")
                self.assert_warns({"type": kind, "minimum": 0}, f"{kind} without maximum")
                self.assert_warns({"type": kind, "maximum": 9}, f"{kind} without minimum")
                self.assert_quiet({"type": kind, "minimum": 0, "maximum": 9})

    def test_exclusive_bounds_count(self) -> None:
        self.assert_quiet({"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1})

    def test_enum_of_numbers_is_bounded(self) -> None:
        self.assert_quiet({"type": "integer", "enum": [1, 2, 3]})

    def test_nested_object_without_properties_is_free_form(self) -> None:
        self.assert_warns({"type": "object"}, "no properties: any argument names are accepted")

    def test_nested_object_without_additional_properties_false(self) -> None:
        prop = {"type": "object", "properties": {"a": {"type": "boolean"}}}
        self.assert_warns(prop, "additionalProperties is not false")
        quiet = {**prop, "additionalProperties": False}
        self.assert_quiet(quiet)

    def test_property_without_type(self) -> None:
        self.assert_warns({"description": "anything"}, "no type: any value is accepted")
        self.assert_quiet({"enum": ["a", "b"]})
        self.assert_quiet({"const": 3})

    def test_unknown_keyword_is_a_warning(self) -> None:
        self.assert_warns(
            {"type": "string", "maxLenght": 4, "maxLength": 3}, "unknown keyword 'maxLenght'"
        )
        self.assert_quiet({"type": "string", "maxLength": 3, "x-note": "free", "format": "uri"})

    def test_ref_is_not_followed(self) -> None:
        self.assert_warns({"$ref": "#/$defs/a"}, "$ref is not followed")

    def test_boolean_schema(self) -> None:
        self.assert_warns(True, "accepts any value")
        self.assert_quiet(False)

    def test_boolean_and_null_need_no_limits(self) -> None:
        self.assert_quiet({"type": "boolean"})
        self.assert_quiet({"type": "null"})

    def test_a_type_list_gets_every_rule(self) -> None:
        found = self.warnings({"type": ["string", "integer"]})
        self.assertTrue(one(found, "string without maxLength"))
        self.assertTrue(one(found, "without minimum and maximum"))

    def test_warnings_do_not_fail_but_strict_does(self) -> None:
        files = {SERVER: tool_source(with_props({"q": {"type": "string"}}))}
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in files.items():
                (Path(tmp) / name).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / name).write_text(text, encoding="utf-8")
            self.assertEqual(run_main([SERVER], Path(tmp))[0], 0)
            code, out, _ = run_main(["--strict", SERVER], Path(tmp))
            self.assertEqual(code, 1)
            self.assertIn("1 errors, 0 warnings", out)


class Reader(unittest.TestCase):
    """What the ``ast`` evaluator understands; nothing of it is imported or run."""

    def test_double_star_and_f_strings(self) -> None:
        source = (
            HEADER
            + BUILDER
            + 'MAX = 40\nBASE = {"a": {"type": "string", "maxLength": MAX}}\n'
            + 'TOOLS = [_tool("t", f"At most {MAX}", {**BASE, "b": {"type": "boolean"}}, ["a"])]\n'
        )
        self.assertEqual(check({SERVER: source})[2], [])

    def test_double_star_of_annotations_in_the_helper(self) -> None:
        source = (
            HEADER
            + BUILDER
            + '_RO = {"readOnlyHint": True, "destructiveHint": True}\n'
            + 'TOOLS = [_tool("t", "x", {}, [], **_RO)]\n'
        )
        self.assertTrue(one(errors_of({SERVER: source}), "both true"))

    def test_arithmetic_subscripts_and_list(self) -> None:
        source = (
            HEADER
            + BUILDER
            + "LIMITS = (1700, 4700)\nKINDS = ('a', 'b')\n"
            + 'TOOLS = [_tool("t", "x", {"k": {"type": "integer", "minimum": LIMITS[0], '
            + '"maximum": LIMITS[1] * 2 + 1}, "m": {"type": "string", "enum": list(KINDS)}}, [])]\n'
        )
        self.assertEqual(check({SERVER: source})[2], [])

    def test_subscripts_give_the_values(self) -> None:
        source = (
            HEADER
            + BUILDER
            + "LIMITS = (9, 1)\n"
            + 'TOOLS = [_tool("t", "x", {"k": {"type": "integer", "minimum": LIMITS[0], '
            + '"maximum": LIMITS[1]}}, [])]\n'
        )
        self.assertTrue(one(errors_of({SERVER: source}), "minimum 9 is more than maximum 1"))

    def test_f_strings_keep_their_text(self) -> None:
        source = HEADER + BUILDER + 'N = 3\nTOOLS = [_tool(f"bad name {N}", "x", {}, [])]\n'
        self.assertTrue(one(errors_of({SERVER: source}), 'tool "bad name ?"'))

    def test_constants_of_a_sibling_module_are_followed(self) -> None:
        files = {
            f"{PKG}/limits.py": "MAX_NAME = 20\nKINDS = ('x', 'y')\n",
            SERVER: HEADER
            + BUILDER
            + "from vilna_mcp_demo.limits import KINDS, MAX_NAME\n"
            + 'TOOLS = [_tool("t", "x", {"n": {"type": "string", "maxLength": MAX_NAME},'
            + ' "k": {"enum": list(KINDS)}}, [])]\n',
        }
        tools, _, problems = check(files, [SERVER])
        self.assertEqual((tools, problems), (1, []))

    def test_a_limit_that_is_wrong_in_the_sibling_module_is_seen(self) -> None:
        files = {
            f"{PKG}/limits.py": "MAX_NAME = -4\n",
            SERVER: HEADER
            + BUILDER
            + "from vilna_mcp_demo.limits import MAX_NAME\n"
            + 'TOOLS = [_tool("t", "x", {"n": {"type": "string", "maxLength": MAX_NAME}}, [])]\n',
        }
        errors = [str(p) for p in check(files, [SERVER])[2] if not p.warning]
        self.assertTrue(one(errors, "maxLength must be a non-negative integer"))

    def test_relative_imports_and_module_attributes(self) -> None:
        files = {
            f"{PKG}/__init__.py": "",
            f"{PKG}/plan.py": "MODES = ('on', 'off')\nLIMIT = 7\n",
            SERVER: HEADER
            + BUILDER
            + "from . import plan as plans\nfrom .plan import LIMIT\n"
            + 'TOOLS = [_tool("t", "x", {"m": {"enum": list(plans.MODES)},'
            + ' "n": {"type": "integer", "minimum": 0, "maximum": LIMIT}}, [])]\n',
        }
        self.assertEqual(check(files, [SERVER])[2], [])

    def test_absolute_import_of_a_module_by_name(self) -> None:
        files = {
            f"{PKG}/__init__.py": "",
            f"{PKG}/plan.py": "MODES = ('on', 'off')\n",
            SERVER: HEADER
            + BUILDER
            + "from vilna_mcp_demo import plan as plans\n"
            + 'TOOLS = [_tool("t", "x", {"m": {"enum": list(plans.MODES)}}, [])]\n',
        }
        self.assertEqual(check(files, [SERVER])[2], [])

    def test_a_helper_imported_from_a_sibling_module(self) -> None:
        files = {
            f"{PKG}/mcp.py": BUILDER.replace("_tool", "tool"),
            f"{PKG}/desktop.py": HEADER
            + "from vilna_mcp_demo.mcp import tool\n"
            + 'TOOLS = [tool("battery", "x", {"p": {"type": "string"}}, [], readOnlyHint=True)]\n',
        }
        tools, servers, problems = check(files)
        self.assertEqual((tools, servers), (1, 1))
        self.assertTrue(one([str(p) for p in problems], "properties.p: string without maxLength"))

    def test_helper_with_defaults_keywords_and_a_docstring(self) -> None:
        source = (
            HEADER
            + "def _s(maxlen=12, *, kind='string'):\n"
            + '    """A string property."""\n'
            + '    return {"type": kind, "maxLength": maxlen}\n\n'
            + "def _tool(name, props):\n"
            + '    return {"name": name, "description": "d", "inputSchema": {"type": "object",'
            + ' "properties": props, "required": [], "additionalProperties": False}}\n\n'
            + 'TOOLS = [_tool("t", {"a": _s(), "b": _s(5), "c": _s(maxlen=7, kind="string")})]\n'
        )
        self.assertEqual(check({SERVER: source})[2], [])

    def test_a_name_that_cannot_be_worked_out_is_a_warning_for_that_tool_only(self) -> None:
        source = (
            HEADER
            + BUILDER
            + 'TOOLS = [_tool("a", "x", {"p": SOMETHING}, []),\n'
            + '         _tool("b", "x", {"q": {"type": "string"}}, [])]\n'
        )
        tools, _, problems = check({SERVER: source})
        text = [str(p) for p in problems]
        self.assertEqual(tools, 2)
        self.assertTrue(all(p.warning for p in problems))
        self.assertTrue(one(text, 'tool "a"'))
        self.assertTrue(one(text, 'tool "b" (line '))
        self.assertTrue(one(text, "properties.q: string without maxLength"))

    def test_a_value_that_cannot_be_worked_out_is_not_a_false_error(self) -> None:
        source = (
            HEADER
            + BUILDER
            + 'TOOLS = [_tool("a", "x", {"p": {"type": "string", "maxLength": getlimit()}}, [])]\n'
        )
        self.assertEqual(check({SERVER: source})[2], [])

    def test_helper_with_two_statements_is_not_inlined(self) -> None:
        source = (
            HEADER
            + "def _tool(name):\n    x = 1\n    return {'name': name}\n\n"
            + 'TOOLS = [_tool("a")]\n'
        )
        problems = check({SERVER: source})[2]
        self.assertEqual(len(problems), 1)
        self.assertIn("cannot be read with ast", str(problems[0]))

    def test_helper_called_with_wrong_arguments(self) -> None:
        source = HEADER + BUILDER + 'TOOLS = [_tool("a")]\n'
        self.assertIn("cannot be read with ast", str(check({SERVER: source})[2][0]))

    def test_tools_as_annotated_tuple_sum_and_star(self) -> None:
        tool = '{"name": "%s", "description": "x", "inputSchema": %r}'
        a, b = tool % ("a", GOOD_SCHEMA), tool % ("b", GOOD_SCHEMA)
        for text in (
            f"TOOLS: list[dict] = [{a}, {b}]",
            f"TOOLS = ({a}, {b})",
            f"_A = [{a}]\nTOOLS = _A + [{b}]",
            f"_A = [{a}]\nTOOLS = [*_A, {b}]",
        ):
            with self.subTest(text=text):
                tools, _, problems = check({SERVER: HEADER + text + "\n"})
                self.assertEqual((tools, problems), (2, []))

    def test_cycles_between_names_and_modules_end(self) -> None:
        files = {
            f"{PKG}/a.py": "from vilna_mcp_demo.b import Y\nX = Y\n",
            f"{PKG}/b.py": "from vilna_mcp_demo.a import X\nY = X\n",
            SERVER: HEADER
            + "from vilna_mcp_demo.a import X\nA = B\nB = A\n"
            + 'TOOLS = [{"name": "t", "description": "d", "inputSchema": {"type": "object",'
            + ' "properties": {"p": {"type": "string", "maxLength": X}, "q": A},'
            + ' "required": [], "additionalProperties": False}}]\n',
        }
        tools, _, problems = check(files, [SERVER])
        self.assertEqual(tools, 1)
        self.assertTrue(all(p.warning for p in problems))

    def test_a_server_is_not_run_or_imported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            canary = Path(tmp) / "canary"
            source = (
                HEADER
                + f"import os\nos.mkdir({str(canary)!r})\nraise SystemExit(5)\n"
                + f"TOOLS = [{{'name': 't', 'description': 'd', 'inputSchema': {GOOD_SCHEMA!r}}}]\n"
            )
            target = Path(tmp) / SERVER
            target.parent.mkdir(parents=True)
            target.write_text(source, encoding="utf-8")
            tools, _, problems = cm.check_files(Path(tmp), [SERVER])
            self.assertFalse(canary.exists())
        self.assertEqual((tools, problems), (1, []))

    def test_the_evaluator_gives_up_on_a_huge_file(self) -> None:
        big = "[" + ",".join("0" for _ in range(500)) + "]"
        source = HEADER + f"X = {big}\n" + "TOOLS = [{'name': 'a', 'x': X}]\n"
        with mock.patch.object(cm, "MAX_STEPS", 100):
            problems = check({SERVER: source})[2]
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].warning)
        self.assertIn("too complex", str(problems[0]))

    def test_very_deep_nesting_is_an_error_and_no_traceback(self) -> None:
        source = HEADER + "TOOLS = " + "[" * 3000 + "]" * 3000 + "\n"
        problems = check({SERVER: source})[2]
        self.assertEqual(len(problems), 1)
        self.assertIn("cannot parse the file", str(problems[0]))

    def test_deeply_nested_but_legal_schema(self) -> None:
        schema: dict = {"type": "boolean"}
        for _ in range(80):
            schema = {"type": "array", "maxItems": 1, "items": schema}
        text = errors_of({SERVER: tool_source(with_props({"p": schema}))})
        self.assertEqual(text, [])


class Files(unittest.TestCase):
    def test_syntax_error(self) -> None:
        problems = check({SERVER: "TOOLS = [\n"})[2]
        self.assertEqual(len(problems), 1)
        self.assertFalse(problems[0].warning)
        self.assertTrue(str(problems[0]).startswith(f"{SERVER}: cannot parse the file"))

    def test_not_utf8(self) -> None:
        problems = check({SERVER: b"TOOLS = ['\xff\xfe']\n"})[2]
        self.assertEqual(
            [str(p) for p in problems], [f"{SERVER}: cannot read the file: not UTF-8 text"]
        )

    def test_utf16_file(self) -> None:
        problems = check({SERVER: "TOOLS = []\n".encode("utf-16")})[2]
        self.assertEqual(len(problems), 1)
        self.assertFalse(problems[0].warning)

    def test_bom_and_crlf_and_no_final_newline(self) -> None:
        text = tool_source(GOOD_SCHEMA).replace("\n", "\r\n").rstrip("\r\n")
        data = b"\xef\xbb\xbf" + text.encode("utf-8")
        tools, _, problems = check({SERVER: data})
        self.assertEqual((tools, problems), (1, []))

    def test_null_byte(self) -> None:
        problems = check({SERVER: b"TOOLS = []\x00\n"})[2]
        self.assertEqual(len(problems), 1)
        self.assertFalse(problems[0].warning)

    def test_empty_server_file(self) -> None:
        problems = check({SERVER: ""})[2]
        self.assertTrue(all(p.warning for p in problems))
        self.assertTrue(one([str(p) for p in problems], "no TOOLS list"))

    def test_missing_file(self) -> None:
        problems = check({SERVER: "x = 1\n"}, [SERVER, f"{PKG}/gone.py"])[2]
        self.assertTrue(one([str(p) for p in problems], f"{PKG}/gone.py: cannot read the file"))

    def test_big_file(self) -> None:
        with mock.patch.object(cm, "MAX_FILE_BYTES", 100):
            problems = check({SERVER: tool_source(GOOD_SCHEMA)})[2]
        self.assertEqual(len(problems), 1)
        self.assertIn("larger than 100 bytes", str(problems[0]))

    def test_non_ascii_names_and_spaces_in_paths(self) -> None:
        folder = "mcp/dé mo/src/vilna_mcp_demo"
        problems = check({f"{folder}/server.py": tool_source(GOOD_SCHEMA)})[2]
        self.assertEqual(problems, [])

    @unittest.skipUnless(hasattr(os, "symlink"), "no symbolic links here")
    def test_symlink_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "server.py"
            secret.write_text(tool_source(GOOD_SCHEMA), encoding="utf-8")
            link = Path(tmp) / SERVER
            link.parent.mkdir(parents=True)
            try:
                link.symlink_to(secret)
            except OSError:
                self.skipTest("cannot make a symbolic link")
            tools, _, problems = cm.check_files(Path(tmp), [SERVER])
        self.assertEqual(tools, 0)
        self.assertTrue(problems[0].warning)
        self.assertIn("symbolic link", str(problems[0]))

    @unittest.skipUnless(hasattr(os, "symlink"), "no symbolic links here")
    def test_symlink_to_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / SERVER
            link.parent.mkdir(parents=True)
            try:
                link.symlink_to(link.name)
            except OSError:
                self.skipTest("cannot make a symbolic link")
            problems = cm.check_files(Path(tmp), [SERVER])[2]
        self.assertEqual(len(problems), 1)

    def test_a_directory_named_like_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / SERVER).mkdir(parents=True)
            problems = cm.check_files(Path(tmp), [SERVER])[2]
        self.assertEqual(len(problems), 1)
        self.assertFalse(problems[0].warning)

    def test_server_py_without_tools(self) -> None:
        problems = warnings_of({SERVER: HEADER + "X = 1\n"})
        self.assertTrue(one(problems, "a server.py with no TOOLS list"))

    def test_project_without_any_tools_list(self) -> None:
        problems = warnings_of({f"{PKG}/other.py": "TOOLS = None\n"})
        self.assertTrue(one(problems, "TOOLS"))

    def test_tools_in_a_module_that_is_not_server_py(self) -> None:
        path = f"{PKG}/portals.py"
        tools, servers, problems = check({path: tool_source(GOOD_SCHEMA)})
        self.assertEqual((tools, servers, problems), (1, 1, []))

    def test_problems_are_sorted_by_file(self) -> None:
        other = "mcp/aaa/src/vilna_mcp_aaa/server.py"
        files = {SERVER: tool_source({"type": "x"}), other: tool_source({"type": "y"})}
        paths = [p.path for p in check(files)[2]]
        self.assertEqual(paths, sorted(paths))


class TrackedFiles(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_only_tracked_sources_under_mcp_src_are_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            names = [
                SERVER,
                "mcp/demo/src/vilna_mcp_demo/sub/deep.py",
                "mcp/demo/tests/test_server.py",
                "mcp/demo/src/vilna_mcp_demo/data.json",
                "tools/check_x.py",
                "other/src/x.py",
            ]
            for name in names:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("TOOLS = []\n", encoding="utf-8")
            (root / "mcp/demo/src/vilna_mcp_demo/untracked.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "--"] + names, cwd=root, check=True)
            expected = sorted([SERVER, "mcp/demo/src/vilna_mcp_demo/sub/deep.py"])
            self.assertEqual(cm.tracked_sources(root), expected)

    def test_lists_tools_reads_only_server_py_and_files_that_name_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in (
                ("a/server.py", "x = 1\n"),
                ("a/b.py", "x = 1\n"),
                ("a/c.py", "TOOLS=[]"),
            ):
                (Path(tmp) / name).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / name).write_text(text, encoding="utf-8")
            self.assertTrue(cm.lists_tools(Path(tmp), "a/server.py"))
            self.assertFalse(cm.lists_tools(Path(tmp), "a/b.py"))
            self.assertTrue(cm.lists_tools(Path(tmp), "a/c.py"))
            self.assertTrue(cm.lists_tools(Path(tmp), "a/missing.py"))


def run_main(args: list[str], cwd: Path) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    previous = Path.cwd()
    os.chdir(cwd)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cm.main(args)
    finally:
        os.chdir(previous)
    return code, out.getvalue(), err.getvalue()


class Main(unittest.TestCase):
    def write(self, tmp: str, text: str) -> None:
        target = Path(tmp) / SERVER
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_good_file_is_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.write(tmp, tool_source(GOOD_SCHEMA))
            code, out, err = run_main([SERVER], Path(tmp))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "1 tools in 1 files checked: 0 errors, 0 warnings\n")

    def test_error_is_1_and_goes_to_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.write(tmp, tool_source({**GOOD_SCHEMA, "required": ["zzz"]}))
            code, out, err = run_main([SERVER], Path(tmp))
        self.assertEqual(code, 1)
        self.assertIn(f"{SERVER}: tool \"demo\" (line 6): inputSchema: required 'zzz'", err)
        self.assertIn("1 errors, 0 warnings", out)

    def test_wrong_call_is_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for args in (["--bogus"], ["file.txt"], [SERVER, "x.md"]):
                with self.subTest(args=args):
                    code, _, err = run_main(args, Path(tmp))
                    self.assertEqual(code, 2)
                    self.assertIn("usage:", err)

    def test_help_is_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_main(["--help"], Path(tmp))[0], 0)

    def test_no_tracked_files_is_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cm, "ROOT", Path(tmp)):
            code, _, err = run_main([], Path(tmp))
        self.assertEqual(code, 2)
        self.assertTrue("git ls-files" in err or "no tracked" in err)


class RepoItself(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "not a git checkout")
    def test_the_tools_of_the_repo_have_no_errors(self) -> None:
        paths = [p for p in cm.tracked_sources(ROOT) if cm.lists_tools(ROOT, p)]
        tools, servers, problems = cm.check_files(ROOT, paths)
        errors = [str(p) for p in problems if not p.warning]
        self.assertEqual(errors, [])
        self.assertGreater(tools, 0)
        projects = {Path(p).parts[1] for p in paths}
        self.assertTrue({"files", "mail", "calendar", "settings", "apps", "desktop"} <= projects)
        self.assertGreaterEqual(servers, len(projects))

    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "not a git checkout")
    def test_every_tool_of_the_repo_is_read(self) -> None:
        """No tool of the repo may be left as 'cannot be read with ast'."""
        paths = [p for p in cm.tracked_sources(ROOT) if cm.lists_tools(ROOT, p)]
        problems = [str(p) for p in cm.check_files(ROOT, paths)[2]]
        self.assertEqual([p for p in problems if "cannot be read" in p], [])


if __name__ == "__main__":
    unittest.main()
