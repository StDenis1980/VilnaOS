#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_shell_extension.py (the check of GNOME Shell extensions).

Run: python3 tools/test_check_shell_extension.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_shell_extension as cse  # noqa: E402  (after sys.path.insert above)

UUID = "demo@example.org"
SCHEMA_ID = "org.gnome.shell.extensions.demo"
GOOD_META = {
    "uuid": UUID,
    "name": "Demo",
    "description": "A demo.",
    "shell-version": ["46", "47"],
    "settings-schema": SCHEMA_ID,
}


def schema_xml(keys: str = "", extra: str = "", path: str | None = None) -> str:
    """A schema file with the given ``<key>`` elements (and elements before the schema)."""
    attr = f' path="{path}"' if path is not None else ' path="/org/gnome/shell/extensions/demo/"'
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<schemalist>\n'
        f'{extra}<schema id="{SCHEMA_ID}"{attr}>\n{keys}</schema>\n</schemalist>\n'
    )


def key(name: str, type_: str, default: str, inner: str = "") -> str:
    text = escape(default)
    return f'<key name="{name}" type="{type_}">{inner}<default>{text}</default></key>\n'


GOOD_KEYS = (
    key("flag", "b", "true")
    + key("count", "i", "8", '<range min="0" max="100"/>')
    + key("names", "as", "['us']")
    + key("place", "(sdd)", "('', 0.0, 0.0)")
    + key("theme", "s", "'wood'", '<choices><choice value="wood"/><choice value="bell"/></choices>')
)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def extension(
        self,
        meta: dict[str, object] | str | None = None,
        schema: str | None = None,
        folder: str = UUID,
        schema_file: str = f"{SCHEMA_ID}.gschema.xml",
        image: str | None = None,
    ) -> list[cse.Problem]:
        """Build the extension in a temporary repo and check it."""
        body = json.dumps(GOOD_META) if meta is None else meta
        if not isinstance(body, str):
            body = json.dumps(body)
        self.write(f"{folder}/metadata.json", body)
        self.write(f"{folder}/extension.js", "// demo\n")
        if schema is not None:
            self.write(f"{folder}/schemas/{schema_file}", schema)
        if image is not None:
            self.write("image/packages.yaml", image)
        return cse.check_files(
            self.root, [f"{folder}/metadata.json"], "image/packages.yaml" if image else None
        )

    def messages(self, problems: list[cse.Problem], warning: bool = False) -> list[str]:
        return [p.message for p in problems if p.warning == warning]

    def assertOnly(self, problems: list[cse.Problem], *fragments: str) -> None:
        """Exactly one error per fragment, in order, and no warning."""
        errors = self.messages(problems)
        self.assertEqual(len(errors), len(fragments), errors)
        for text, fragment in zip(errors, fragments, strict=True):
            self.assertIn(fragment, text)
        self.assertEqual(self.messages(problems, True), [])


class GoodExtension(Base):
    def test_a_good_extension_has_no_problems(self) -> None:
        self.assertEqual(self.extension(schema=schema_xml(GOOD_KEYS)), [])

    def test_an_extension_without_settings_has_no_problems(self) -> None:
        meta = {k: v for k, v in GOOD_META.items() if k != "settings-schema"}
        self.assertEqual(self.extension(meta), [])

    def test_all_the_optional_fields_are_fine(self) -> None:
        meta = dict(
            GOOD_META,
            **{
                "version": 3,
                "version-name": "0.3",
                "url": "https://example.org",
                "gettext-domain": "demo",
                "session-modes": ["user", "unlock-dialog", "gdm"],
            },
        )
        self.assertEqual(self.extension(meta, schema_xml(GOOD_KEYS)), [])

    def test_the_problem_text_names_the_file(self) -> None:
        problems = self.extension(dict(GOOD_META, uuid="other@example.org"), schema_xml())
        self.assertTrue(str(problems[0]).startswith(f"{UUID}/metadata.json: "))

    def test_a_warning_is_marked_in_the_text(self) -> None:
        text = str(cse.Problem("a/b.json", "something", warning=True))
        self.assertEqual(text, "a/b.json: warning: something")


class Uuid(Base):
    def test_uuid_must_be_the_folder_name(self) -> None:
        problems = self.extension(dict(GOOD_META, uuid="other@example.org"), schema_xml())
        self.assertOnly(problems, "uuid 'other@example.org' is not the name of its folder")

    def test_uuid_is_case_sensitive(self) -> None:
        problems = self.extension(dict(GOOD_META, uuid="Demo@example.org"), schema_xml())
        self.assertOnly(problems, "is not the name of its folder")

    def test_uuid_is_required(self) -> None:
        meta = {k: v for k, v in GOOD_META.items() if k != "uuid"}
        self.assertOnly(self.extension(meta, schema_xml()), "uuid must be a string")

    def test_uuid_must_be_a_string(self) -> None:
        self.assertOnly(self.extension(dict(GOOD_META, uuid=5), schema_xml()), "uuid must be")

    def test_uuid_has_no_spaces_or_slashes(self) -> None:
        for bad in ("demo @example.org", "a/b@example.org", "", "demo@exämple.org"):
            with self.subTest(uuid=bad):
                problems = self.extension(dict(GOOD_META, uuid=bad), schema_xml(), folder="x")
                self.assertIn("uuid must be a string", self.messages(problems)[0])

    def test_name_and_description_are_required(self) -> None:
        for field in ("name", "description"):
            with self.subTest(field=field):
                meta = {k: v for k, v in GOOD_META.items() if k != field}
                self.assertOnly(self.extension(meta, schema_xml()), f"{field} must be")

    def test_blank_name_is_an_error(self) -> None:
        self.assertOnly(self.extension(dict(GOOD_META, name="  "), schema_xml()), "name must be")


class ShellVersion(Base):
    def versions(self, value: object) -> list[cse.Problem]:
        return self.extension(dict(GOOD_META, **{"shell-version": value}), schema_xml())

    def test_shell_version_is_required(self) -> None:
        meta = {k: v for k, v in GOOD_META.items() if k != "shell-version"}
        self.assertOnly(self.extension(meta, schema_xml()), "shell-version must be a non-empty")

    def test_shell_version_must_be_an_array(self) -> None:
        for value in ("46", 46, {"46": 1}, None):
            with self.subTest(value=value):
                self.assertOnly(self.versions(value), "shell-version must be a non-empty")

    def test_an_empty_array_is_an_error(self) -> None:
        self.assertOnly(self.versions([]), "shell-version must be a non-empty")

    def test_numbers_are_not_strings(self) -> None:
        self.assertOnly(self.versions(["46", 47]), "shell-version has 47: versions must be strings")

    def test_valid_versions(self) -> None:
        for good in (["3.38"], ["40"], ["46", "47.beta", "48.rc", "49.alpha", "50.1"]):
            with self.subTest(versions=good):
                self.assertEqual(self.versions(good), [])

    def test_invalid_versions(self) -> None:
        for bad in ("", "x", "46.", ".46", "046", "46.x", "v46", "46 ", "4 6", "-1"):
            with self.subTest(version=bad):
                self.assertOnly(self.versions([bad]), "which is not a version")

    def test_a_repeated_version_is_an_error(self) -> None:
        self.assertOnly(self.versions(["46", "47", "46"]), "'46' twice")


class ImageVersions(Base):
    def run_with(self, versions: list[str], image: str | None) -> list[cse.Problem]:
        meta = dict(GOOD_META, **{"shell-version": versions})
        return self.extension(meta, schema_xml(), image=image)

    def test_no_line_in_packages_yaml_is_one_warning(self) -> None:
        problems = self.run_with(["46"], "base:\n  pattern: x\n")
        self.assertEqual(self.messages(problems), [])
        self.assertEqual(len(self.messages(problems, True)), 1)
        self.assertIn("no gnome_shell_versions", self.messages(problems, True)[0])

    def test_the_image_version_must_be_listed(self) -> None:
        problems = self.run_with(["46", "47"], 'gnome_shell_versions: ["49"]\n')
        self.assertEqual(
            self.messages(problems),
            ["the image has GNOME Shell 49, which shell-version does not list"],
        )

    def test_a_version_the_image_does_not_have_is_a_warning(self) -> None:
        problems = self.run_with(["49", "50"], 'gnome_shell_versions: ["49"]\n')
        self.assertEqual(self.messages(problems), [])
        self.assertEqual(
            self.messages(problems, True),
            ["shell-version '50' is not a Shell version of the image"],
        )

    def test_matching_versions_have_no_problems(self) -> None:
        self.assertEqual(self.run_with(["49"], "gnome_shell_versions: [49]\n"), [])

    def test_every_image_version_is_checked(self) -> None:
        problems = self.run_with(["49"], "gnome_shell_versions: ['49', '50']  # Aeon\n")
        self.assertEqual(
            self.messages(problems),
            ["the image has GNOME Shell 50, which shell-version does not list"],
        )

    def test_a_block_list_is_read(self) -> None:
        image = "top:\n  gnome_shell_versions:\n    - '48'\n    - 49\nother: 1\n"
        self.assertEqual(cse.image_versions(image), ["48", "49"])

    def test_no_key_is_none(self) -> None:
        self.assertIsNone(cse.image_versions("a: 1\nb: [1]\n"))

    def test_a_commented_out_key_is_not_read(self) -> None:
        self.assertIsNone(cse.image_versions('# gnome_shell_versions: ["49"]\n'))

    def test_an_invalid_shell_version_hides_the_image_comparison(self) -> None:
        problems = self.run_with([49], 'gnome_shell_versions: ["49"]\n')
        self.assertOnly(problems, "versions must be strings")


class OtherFields(Base):
    def test_session_modes_must_be_known(self) -> None:
        for value in (["user", "lock"], "user", [1], ["User"]):
            with self.subTest(value=value):
                problems = self.extension(dict(GOOD_META, **{"session-modes": value}), schema_xml())
                self.assertOnly(problems, "session-modes must be an array of")

    def test_version_must_be_an_integer(self) -> None:
        for value in ("3", 3.5, True, None):
            with self.subTest(value=value):
                self.assertOnly(
                    self.extension(dict(GOOD_META, version=value), schema_xml()),
                    "version must be an integer",
                )

    def test_string_fields_must_be_strings(self) -> None:
        for field in ("version-name", "url", "gettext-domain"):
            with self.subTest(field=field):
                self.assertOnly(
                    self.extension(dict(GOOD_META, **{field: 3}), schema_xml()),
                    f"{field} must be a string",
                )

    def test_invalid_json(self) -> None:
        self.assertOnly(self.extension("{not json"), "not valid JSON")

    def test_a_json_array_is_not_metadata(self) -> None:
        self.assertOnly(self.extension("[]"), "the top level must be a JSON object")

    def test_a_repeated_key_is_an_error(self) -> None:
        text = '{"uuid": "demo@example.org", "uuid": "demo@example.org", "name": "a"}'
        self.assertOnly(self.extension(text), "the key 'uuid' is written twice")

    def test_nan_is_not_json(self) -> None:
        self.assertOnly(self.extension('{"version": NaN}'), "NaN is not valid JSON")

    def test_a_byte_order_mark_is_one_error(self) -> None:
        problems = self.extension("\ufeff" + json.dumps(GOOD_META), schema_xml())
        self.assertOnly(problems, "byte order mark")

    def test_a_file_that_is_not_utf8(self) -> None:
        self.write(f"{UUID}/extension.js", "")
        (self.root / UUID / "metadata.json").write_bytes(b"\xff\xfe{}")
        problems = cse.check_files(self.root, [f"{UUID}/metadata.json"])
        self.assertEqual(self.messages(problems), ["the file is not valid UTF-8"])

    def test_a_missing_file_is_an_error(self) -> None:
        problems = cse.check_files(self.root, ["nowhere/metadata.json"])
        self.assertIn("cannot read the file", self.messages(problems)[0])

    def test_a_huge_file_is_refused(self) -> None:
        self.write(f"{UUID}/metadata.json", " " * (cse.MAX_BYTES + 1))
        problems = cse.check_files(self.root, [f"{UUID}/metadata.json"])
        self.assertIn("bigger than", self.messages(problems)[0])


class SettingsSchema(Base):
    def test_the_schema_must_exist(self) -> None:
        problems = self.extension()
        self.assertOnly(problems, f"settings-schema '{SCHEMA_ID}' is not in a")
        self.assertIn("schemas found: none", self.messages(problems)[0])

    def test_the_schema_id_must_be_in_the_file(self) -> None:
        other = schema_xml(GOOD_KEYS).replace(SCHEMA_ID, "org.gnome.shell.extensions.other")
        problems = self.extension(
            schema=other, schema_file="org.gnome.shell.extensions.other.gschema.xml"
        )
        self.assertOnly(problems, "is not in a")
        self.assertIn(
            "schemas found: org.gnome.shell.extensions.other)", self.messages(problems)[0]
        )

    def test_the_file_must_be_named_after_the_schema(self) -> None:
        problems = self.extension(schema=schema_xml(GOOD_KEYS), schema_file="settings.gschema.xml")
        self.assertOnly(problems, f"must be named {SCHEMA_ID}.gschema.xml")

    def test_only_gschema_xml_files_count(self) -> None:
        problems = self.extension(schema=schema_xml(GOOD_KEYS), schema_file=f"{SCHEMA_ID}.xml")
        self.assertOnly(problems, "is not in a")

    def test_settings_schema_must_be_a_string(self) -> None:
        for value in (5, "", ["a"]):
            with self.subTest(value=value):
                problems = self.extension(
                    dict(GOOD_META, **{"settings-schema": value}), schema_xml()
                )
                self.assertIn("settings-schema must be a non-empty string", self.messages(problems))

    def test_a_schemas_folder_without_the_key_is_a_warning(self) -> None:
        meta = {k: v for k, v in GOOD_META.items() if k != "settings-schema"}
        problems = self.extension(meta, schema_xml(GOOD_KEYS))
        self.assertEqual(self.messages(problems), [])
        self.assertEqual(
            self.messages(problems, True), ["there is a schemas/ folder but no settings-schema"]
        )

    def test_a_schema_that_is_not_xml(self) -> None:
        self.assertOnly(self.extension(schema="<schemalist><schema>"), "not well-formed XML")

    def test_the_root_must_be_schemalist(self) -> None:
        self.assertOnly(self.extension(schema="<schemas/>"), "not <schemalist>")

    def test_a_schema_folder_with_a_broken_file_still_finds_the_good_one(self) -> None:
        self.write(f"{UUID}/schemas/broken.gschema.xml", "<")
        problems = self.extension(schema=schema_xml(GOOD_KEYS))
        self.assertOnly(problems, "not well-formed XML")

    def test_the_schema_id_must_be_valid(self) -> None:
        xml = schema_xml(GOOD_KEYS).replace(f'id="{SCHEMA_ID}"', 'id="9bad..id"')
        problems = self.extension(schema=xml)
        self.assertIn("the id is missing or not valid", self.messages(problems)[0])

    def test_two_schemas_with_the_same_id(self) -> None:
        body = '<key name="a" type="b"><default>true</default></key>'
        one = f'<schema id="{SCHEMA_ID}">{body}</schema>'
        xml = f"<schemalist>{one}{one}</schemalist>"
        problems = self.extension(schema=xml)
        self.assertOnly(problems, "the id is also in")

    def test_a_path_must_start_and_end_with_a_slash(self) -> None:
        for bad in ("org/demo/", "/org/demo", "/org//demo/"):
            with self.subTest(path=bad):
                problems = self.extension(schema=schema_xml(GOOD_KEYS, path=bad))
                self.assertOnly(problems, "must start and end with '/'")

    def test_a_schema_without_a_path_is_fine(self) -> None:
        xml = schema_xml(GOOD_KEYS).replace(' path="/org/gnome/shell/extensions/demo/"', "")
        self.assertEqual(self.extension(schema=xml), [])

    def test_an_unknown_element_in_the_schemalist(self) -> None:
        xml = schema_xml(GOOD_KEYS).replace("</schemalist>", "<oops/></schemalist>")
        self.assertOnly(self.extension(schema=xml), "unknown element <oops> in <schemalist>")


class Keys(Base):
    def check(self, keys: str) -> list[cse.Problem]:
        return self.extension(schema=schema_xml(keys))

    def test_a_key_name_is_lowercase_words_with_dashes(self) -> None:
        for bad in ("Flag", "a_b", "a--b", "-a", "a-", "1a", "", "a b"):
            with self.subTest(name=bad):
                problems = self.check(key(bad, "b", "true"))
                self.assertIn("the name must be lowercase", self.messages(problems)[0])

    def test_a_key_name_with_digits_is_fine(self) -> None:
        self.assertEqual(self.check(key("a1-b2", "b", "true")), [])

    def test_a_key_name_used_twice(self) -> None:
        problems = self.check(key("a", "b", "true") + key("a", "b", "false"))
        self.assertOnly(problems, "key 'a' is used twice")

    def test_a_key_needs_a_type(self) -> None:
        problems = self.check('<key name="a"><default>true</default></key>')
        self.assertOnly(problems, "exactly one of the attributes type, enum and flags")

    def test_a_key_with_two_kinds(self) -> None:
        problems = self.check('<key name="a" type="b" enum="x"><default>true</default></key>')
        self.assertOnly(problems, "exactly one of the attributes")

    def test_a_key_needs_one_default(self) -> None:
        none = '<key name="a" type="b"></key>'
        two = '<key name="a" type="b"><default>true</default><default>false</default></key>'
        for keys in (none, two):
            with self.subTest(keys=keys):
                self.assertIn("exactly one <default>", self.messages(self.check(keys))[0])

    def test_an_unknown_element_in_a_key(self) -> None:
        keys = '<key name="a" type="b"><default>true</default><wat/></key>'
        self.assertOnly(self.check(keys), "unknown element <wat>")

    def test_an_invalid_type_string(self) -> None:
        for bad in ("", "z", "a", "(s", "a{vs}", "a{(s)s}", "ss", "(s)x", "mm", "{s}", "*"):
            with self.subTest(type=bad):
                problems = self.check(key("a", bad, "''"))
                self.assertIn("is not a valid GVariant type string", self.messages(problems)[0])

    def test_valid_type_strings(self) -> None:
        for good in ("b", "y", "as", "aas", "a{sv}", "a{ss}", "(ii)", "()", "mi", "a(sdd)", "v"):
            with self.subTest(type=good):
                self.assertTrue(cse.is_type(good))

    def test_a_default_of_the_wrong_type(self) -> None:
        cases = {
            "b": ("1", "'true'", "True", ""),
            "i": ("true", "'1'", "1.5", "[]", "99999999999"),
            "y": ("256", "-1"),
            "s": ("hello", "1", "true", "'open"),
            "as": ("'a'", "['a', 1]", "[1]"),
            "(sdd)": ("('', 0.0)", "('', 0.0, 0.0, 1)", "['a', 1.0, 2.0]"),
            "d": ("'1.0'", "true", "1.0.0"),
        }
        for type_, defaults in cases.items():
            for default in defaults:
                with self.subTest(type=type_, default=default):
                    problems = self.check(key("a", type_, default))
                    self.assertEqual(len(self.messages(problems)), 1, default)
                    self.assertIn("does not fit type", self.messages(problems)[0])

    def test_defaults_that_fit(self) -> None:
        cases = {
            "b": ("true", "false", " true "),
            "i": ("0", "-5", "0x10", "010", "2147483647"),
            "u": ("4294967295",),
            "y": ("255", "0"),
            "x": ("-9223372036854775808",),
            "d": ("0.0", "1", "-1.5e3", ".5", "5.", "inf", "-inf", "nan"),
            "s": ("''", '""', "'a\\nb'", "'\\u00e9'", "'it\\'s'", '"it\'s"'),
            "as": ("[]", "@as []", "['a', 'b']", "['a', 'b',]"),
            "ai": ("[1, 2, 3]", "@ai []"),
            "(sdd)": ("('', 0.0, 0.0)", "('x', 1, -2)"),
            "(ddas)": ("(0.0, 0.0, @as [])",),
            "mi": ("nothing", "just 5", "5"),
            "a{sv}": ("{}", "{'a': <1>, 'b': <'x'>}", "@a{sv} {}"),
            "a{ss}": ("{'a': 'b'}",),
            "v": ("<1>", "<'x'>", "<[1, 2]>", "<@as []>", "<(1, 'a')>"),
            "ay": ("[]", "[1, 2]", "b'ab'", "@ay []"),
            "o": ("'/org/gnome'", "'/'"),
            "g": ("'ii'", "''", "'a{sv}'"),
            "()": ("()",),
            "q": ("byte 5".replace("byte", "uint16"),),
        }
        for type_, defaults in cases.items():
            for default in defaults:
                with self.subTest(type=type_, default=default):
                    problems = self.check(key("a", type_, default))
                    self.assertEqual(problems, [], default)

    def test_an_object_path_and_a_signature_are_checked(self) -> None:
        for type_, bad in (("o", "'org/gnome'"), ("o", "'/a/'"), ("o", "''"), ("g", "'z'")):
            with self.subTest(type=type_, default=bad):
                problems = self.check(key("a", type_, bad))
                self.assertIn("does not fit type", self.messages(problems)[0])

    def test_an_empty_default_is_an_error(self) -> None:
        self.assertIn("does not fit type", self.messages(self.check(key("a", "s", "")))[0])

    def test_a_variant_must_say_what_it_holds(self) -> None:
        for bad in ("<[]>", "<nothing>", "<{}>", "1", "<1", "<>"):
            with self.subTest(default=bad):
                self.assertIn("does not fit type", self.messages(self.check(key("a", "v", bad)))[0])

    def test_an_annotation_must_match_the_key_type(self) -> None:
        problems = self.check(key("a", "as", "@ai []"))
        self.assertIn("has type 'ai', the key has type 'as'", self.messages(problems)[0])

    def test_an_array_of_mixed_elements_in_a_variant(self) -> None:
        problems = self.check(key("a", "v", "<[1, 'a']>"))
        self.assertIn("the elements have types", self.messages(problems)[0])

    def test_a_string_escape_must_be_known(self) -> None:
        self.assertIn("unknown escape", self.messages(self.check(key("a", "s", "'\\q'")))[0])

    def test_a_value_nested_too_deep_is_an_error_not_a_crash(self) -> None:
        for type_, default in (("v", "<" * 5000), ("v", "<" * 70 + "1" + ">" * 70)):
            with self.subTest(type=type_, depth=len(default)):
                problems = self.check(key("a", type_, default))
                self.assertIn("nested more than", self.messages(problems)[0])

    def test_a_value_nested_not_too_deep_is_fine(self) -> None:
        self.assertEqual(self.check(key("a", "v", "<" * 20 + "1" + ">" * 20)), [])

    def test_text_after_the_value(self) -> None:
        self.assertIn("unexpected text", self.messages(self.check(key("a", "b", "true false")))[0])

    def test_the_xml_default_is_read_with_entities(self) -> None:
        keys = '<key name="a" type="s"><default>\'a &amp; b &lt; c\'</default></key>'
        self.assertEqual(self.check(keys), [])

    def test_a_default_in_a_cdata_section(self) -> None:
        self.assertEqual(
            self.check('<key name="a" type="s"><default><![CDATA[\'x\']]></default></key>'), []
        )


class RangeAndChoices(Base):
    def check(self, type_: str, default: str, inner: str) -> list[cse.Problem]:
        return self.extension(schema=schema_xml(key("a", type_, default, inner)))

    def test_the_default_in_range(self) -> None:
        for default in ("0", "50", "100"):
            with self.subTest(default=default):
                self.assertEqual(self.check("i", default, '<range min="0" max="100"/>'), [])

    def test_the_default_out_of_range(self) -> None:
        for default in ("-1", "101"):
            with self.subTest(default=default):
                problems = self.check("i", default, '<range min="0" max="100"/>')
                self.assertIn("outside <range> 0..100", self.messages(problems)[0])

    def test_min_bigger_than_max(self) -> None:
        problems = self.check("i", "5", '<range min="10" max="0"/>')
        self.assertIn("min 10 is bigger than max 0", self.messages(problems)[0])

    def test_a_range_needs_min_and_max(self) -> None:
        for inner in ('<range min="0"/>', '<range max="0"/>', "<range/>"):
            with self.subTest(inner=inner):
                self.assertIn("<range> has no", self.messages(self.check("i", "0", inner))[0])

    def test_a_range_is_for_numbers(self) -> None:
        problems = self.check("s", "'a'", '<range min="a" max="b"/>')
        self.assertIn("<range> is for numbers, the type is 's'", self.messages(problems)[0])

    def test_range_limits_must_fit_the_type(self) -> None:
        problems = self.check("y", "5", '<range min="0" max="300"/>')
        self.assertIn("max='300' does not fit 'y'", self.messages(problems)[0])

    def test_a_double_range(self) -> None:
        self.assertEqual(self.check("d", "0.5", '<range min="0.0" max="1.0"/>'), [])
        problems = self.check("d", "1.5", '<range min="0.0" max="1.0"/>')
        self.assertIn("outside <range>", self.messages(problems)[0])

    def test_the_default_must_be_a_choice(self) -> None:
        inner = '<choices><choice value="a"/><choice value="b"/></choices>'
        self.assertEqual(self.check("s", "'a'", inner), [])
        problems = self.check("s", "'c'", inner)
        self.assertIn("is not one of <choices>", self.messages(problems)[0])

    def test_choices_with_quotes_in_a_value(self) -> None:
        inner = '<choices><choice value="it\'s"/><choice value="b"/></choices>'
        self.assertEqual(self.check("s", '"it\'s"', inner), [])

    def test_choices_of_numbers(self) -> None:
        inner = '<choices><choice value="1"/><choice value="2"/></choices>'
        self.assertEqual(self.check("i", "2", inner), [])
        self.assertIn("is not one of <choices>", self.messages(self.check("i", "3", inner))[0])

    def test_a_choice_must_fit_the_type(self) -> None:
        inner = '<choices><choice value="x"/></choices>'
        self.assertIn("<choice value='x'>", self.messages(self.check("i", "1", inner))[0])

    def test_choices_without_a_choice(self) -> None:
        self.assertIn("no <choice>", self.messages(self.check("s", "'a'", "<choices/>"))[0])

    def test_a_choice_needs_a_value(self) -> None:
        inner = "<choices><choice/></choices>"
        self.assertIn("has no value", self.messages(self.check("s", "'a'", inner))[0])

    def test_range_and_choices_together(self) -> None:
        inner = '<range min="0" max="9"/><choices><choice value="1"/></choices>'
        self.assertIn("cannot be used together", self.messages(self.check("i", "1", inner))[0])


ENUMS = (
    '<enum id="org.gnome.shell.extensions.demo.Size">'
    '<value nick="small" value="0"/><value nick="big" value="1"/></enum>\n'
    '<flags id="org.gnome.shell.extensions.demo.Parts">'
    '<value nick="head" value="1"/><value nick="tail" value="2"/></flags>\n'
)
SIZE = "org.gnome.shell.extensions.demo.Size"
PARTS = "org.gnome.shell.extensions.demo.Parts"


class Enums(Base):
    def check(self, keys: str, extra: str = ENUMS) -> list[cse.Problem]:
        return self.extension(schema=schema_xml(keys, extra))

    def test_an_enum_key_with_a_nick(self) -> None:
        keys = f'<key name="a" enum="{SIZE}"><default>\'big\'</default></key>'
        self.assertEqual(self.check(keys), [])

    def test_a_default_that_is_not_a_nick(self) -> None:
        keys = f'<key name="a" enum="{SIZE}"><default>\'huge\'</default></key>'
        self.assertIn("'huge', which is not a nick", self.messages(self.check(keys))[0])

    def test_an_enum_default_must_be_quoted(self) -> None:
        for default in ("big", "1", "['big']"):
            with self.subTest(default=default):
                keys = f'<key name="a" enum="{SIZE}"><default>{default}</default></key>'
                self.assertIn("is not s", self.messages(self.check(keys))[0])

    def test_an_unknown_enum(self) -> None:
        nope = "org.gnome.shell.extensions.demo.Nope"
        keys = f'<key name="a" enum="{nope}"><default>\'x\'</default></key>'
        self.assertIn(
            "is not an <enum> of the schemas in this folder", self.messages(self.check(keys))[0]
        )

    def test_flags_defaults(self) -> None:
        good = f"<key name=\"a\" flags=\"{PARTS}\"><default>['head', 'tail']</default></key>"
        self.assertEqual(self.check(good), [])
        empty = f'<key name="a" flags="{PARTS}"><default>[]</default></key>'
        self.assertEqual(self.check(empty), [])
        bad = f"<key name=\"a\" flags=\"{PARTS}\"><default>['head', 'arm']</default></key>"
        self.assertIn("'arm', which is not a nick", self.messages(self.check(bad))[0])

    def test_enum_and_flags_are_not_exchangeable(self) -> None:
        keys = f'<key name="a" flags="{SIZE}"><default>[]</default></key>'
        self.assertIn("is an <enum>, the key says flags=", self.messages(self.check(keys))[0])

    def test_an_enum_nick_used_twice(self) -> None:
        extra = '<enum id="e.E"><value nick="a" value="0"/><value nick="a" value="1"/></enum>\n'
        self.assertIn("nick 'a' is used twice", self.messages(self.check("", extra))[0])

    def test_an_enum_value_used_twice(self) -> None:
        extra = '<enum id="e.E"><value nick="a" value="0"/><value nick="b" value="0"/></enum>\n'
        self.assertIn("value 0 is used twice", self.messages(self.check("", extra))[0])

    def test_an_enum_value_must_be_a_number(self) -> None:
        extra = '<enum id="e.E"><value nick="a" value="x"/></enum>\n'
        self.assertIn("has value 'x'", self.messages(self.check("", extra))[0])

    def test_an_empty_enum(self) -> None:
        self.assertIn("no <value>", self.messages(self.check("", '<enum id="e.E"/>\n'))[0])

    def test_an_enum_id_used_twice(self) -> None:
        extra = ENUMS + ENUMS.split("\n")[0] + "\n"
        self.assertIn("the id is used twice", self.messages(self.check("", extra))[0])

    def test_an_enum_from_another_file_of_the_folder(self) -> None:
        keys = f'<key name="a" enum="{SIZE}"><default>\'big\'</default></key>'
        self.write(
            f"{UUID}/schemas/enums.gschema.xml",
            f"<schemalist>{ENUMS}</schemalist>",
        )
        self.assertEqual(self.extension(schema=schema_xml(keys, "")), [])

    def test_range_cannot_go_with_an_enum(self) -> None:
        keys = (
            f'<key name="a" enum="{SIZE}"><range min="0" max="1"/><default>\'big\'</default></key>'
        )
        self.assertIn("<range> cannot be used with enum=", self.messages(self.check(keys))[0])


class Cli(Base):
    def run_main(self, args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cse.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_a_good_file_exits_0(self) -> None:
        self.extension(schema=schema_xml(GOOD_KEYS))
        with contextlib.chdir(self.root):
            code, out, err = self.run_main([f"{UUID}/metadata.json"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "1 extensions checked: 0 errors, 0 warnings\n")

    def test_an_error_exits_1_and_is_printed_to_stderr(self) -> None:
        self.extension(dict(GOOD_META, uuid="x@y"), schema_xml(GOOD_KEYS))
        with contextlib.chdir(self.root):
            code, out, err = self.run_main([f"{UUID}/metadata.json"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith(f"{UUID}/metadata.json: uuid 'x@y'"))
        self.assertIn("1 errors, 0 warnings", out)

    def test_warnings_alone_exit_0(self) -> None:
        meta = {k: v for k, v in GOOD_META.items() if k != "settings-schema"}
        self.extension(meta, schema_xml(GOOD_KEYS))
        with contextlib.chdir(self.root):
            code, out, err = self.run_main([f"{UUID}/metadata.json"])
        self.assertEqual(code, 0)
        self.assertIn(": warning: ", err)
        self.assertIn("0 errors, 1 warnings", out)

    def test_an_unknown_option_exits_2(self) -> None:
        self.assertEqual(self.run_main(["--nope"])[0], 2)

    def test_help_exits_0(self) -> None:
        self.assertEqual(self.run_main(["--help"])[0], 0)

    def test_a_file_that_is_not_metadata_json_exits_2(self) -> None:
        code, _, err = self.run_main(["package.json"])
        self.assertEqual(code, 2)
        self.assertIn("must be a metadata.json", err)

    def test_a_missing_file_exits_2(self) -> None:
        with contextlib.chdir(self.root):
            code, _, err = self.run_main(["nowhere/metadata.json"])
        self.assertEqual(code, 2)
        self.assertIn("no such file", err)

    def test_git_failing_exits_2(self) -> None:
        with mock.patch.object(cse, "tracked_metadata", side_effect=OSError("no git")):
            code, _, err = self.run_main([])
        self.assertEqual(code, 2)
        self.assertIn("cannot list files with git ls-files", err)

    def test_no_extension_exits_2(self) -> None:
        with mock.patch.object(cse, "tracked_metadata", return_value=[]):
            self.assertEqual(self.run_main([])[0], 2)


class TrackedFiles(Base):
    def test_only_extensions_are_taken(self) -> None:
        self.write("a/metadata.json", "{}")
        self.write("a/extension.js", "")
        self.write("b/metadata.json", '{"name": "not an extension"}')
        self.write("c/metadata.json", '{"uuid": "c"}')
        self.assertTrue(cse.is_extension(self.root, "a/metadata.json"))
        self.assertFalse(cse.is_extension(self.root, "b/metadata.json"))
        self.assertTrue(cse.is_extension(self.root, "c/metadata.json"))

    def test_a_broken_json_is_still_checked(self) -> None:
        self.write("a/metadata.json", "{")
        self.assertTrue(cse.is_extension(self.root, "a/metadata.json"))


class Legacy(Base):
    def test_a_legacy_error_becomes_a_warning(self) -> None:
        message = "uuid 'other@example.org' is not the name of its folder 'demo@example.org'"
        path = f"{UUID}/metadata.json"
        legacy = {(path, message): "old"}
        with mock.patch.object(cse, "LEGACY", legacy):
            problems = self.extension(dict(GOOD_META, uuid="other@example.org"), schema_xml())
        self.assertEqual([(p.message, p.warning) for p in problems], [(message, True)])

    def test_the_legacy_table_is_empty_or_names_real_files(self) -> None:
        tracked = set(cse.tracked_metadata(ROOT))
        for path, _ in cse.LEGACY:
            self.assertIn(path, tracked)


class Repo(unittest.TestCase):
    """The tool on the repo itself."""

    def test_the_repo_has_no_errors(self) -> None:
        paths = cse.tracked_metadata(ROOT)
        self.assertIn("shell-extension/vilna@stdenis1980.github.io/metadata.json", paths)
        problems = cse.check_files(ROOT, paths, cse.PACKAGES_YAML)
        self.assertEqual([str(p) for p in problems if not p.warning], [])

    def test_the_vilna_schema_is_read_to_the_end(self) -> None:
        folder = "shell-extension/vilna@stdenis1980.github.io/schemas"
        ids, enums, problems, broken = cse.check_schema_files(ROOT, folder)
        self.assertEqual((problems, broken), ([], False))
        self.assertIn("org.gnome.shell.extensions.vilna", ids)
        self.assertGreaterEqual(len(enums), 4)


if __name__ == "__main__":
    unittest.main()
