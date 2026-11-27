#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_data_files.py (the JSON and TOML checker).

Every test writes small files into a temporary folder and checks them the way
``main()`` does; there is a test for every rule and one for the good files.

Run: python3 tools/test_check_data_files.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_data_files as cd  # noqa: E402  (after sys.path.insert above)

I18N = "site/src/i18n"
EN = {"hello": "Hello", "link": 'See <a href="{{url}}">here</a>.', "two": "{{a}} and {{b}}"}


def run_check(files: dict[str, bytes], only: list[str] | None = None) -> list[cd.Problem]:
    """Write the files to a temporary folder and check them (all, or those in ``only``)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, data in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(data)
        return cd.check_files(root, sorted(files) if only is None else only)


def errors_of(files: dict[str, bytes], only: list[str] | None = None) -> list[str]:
    return [str(p) for p in run_check(files, only) if not p.warning]


def warnings_of(files: dict[str, bytes], only: list[str] | None = None) -> list[str]:
    return [str(p) for p in run_check(files, only) if p.warning]


def dump(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def languages(**langs: dict[str, str]) -> dict[str, bytes]:
    files = {f"{I18N}/en.json": dump(EN)}
    files.update({f"{I18N}/{lang}.json": dump(strings) for lang, strings in langs.items()})
    return files


class GoodFiles(unittest.TestCase):
    def test_good_json(self) -> None:
        files = {"a.json": b'{"a": [1, 2, {"b": null}], "c": "\xd1\x96"}\n'}
        self.assertEqual(run_check(files), [])

    def test_json_with_crlf_and_scalar_top_level(self) -> None:
        self.assertEqual(run_check({"a.json": b'{"a": 1}\r\n', "b.json": b"[]\n"}), [])
        self.assertEqual(run_check({"c.json": b"null\n", "d.json": b'"text"\n'}), [])

    def test_good_toml(self) -> None:
        text = b'name = "x"\n[table]\nkey = [1, 2]\n\n[[items]]\nid = 1\n'
        self.assertEqual(run_check({"a.toml": text}), [])

    def test_toml_without_final_newline_is_fine(self) -> None:
        self.assertEqual(run_check({"a.toml": b"a = 1"}), [])
        self.assertEqual(run_check({"empty.toml": b""}), [])

    def test_the_same_key_in_different_objects_is_fine(self) -> None:
        self.assertEqual(
            run_check({"a.json": b'{"x": {"k": 1}, "y": {"k": 2}, "z": [{"k": 3}]}\n'}), []
        )

    def test_upper_case_suffix_is_checked_too(self) -> None:
        self.assertEqual(
            errors_of({"A.JSON": b"{"}),
            [
                "A.JSON: invalid JSON: Expecting property name enclosed in double quotes: "
                "line 1 column 2 (char 1)",
                "A.JSON: no newline at the end of the file",
            ],
        )

    def test_upper_case_toml_suffix_is_read_as_toml(self) -> None:
        self.assertEqual(run_check({"A.TOML": b"a = 1"}), [])
        errors = errors_of({"B.TOML": b"a = \n"})
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("B.TOML: invalid TOML"))


class JsonRules(unittest.TestCase):
    def test_invalid_json(self) -> None:
        for text in (b"{", b"", b"{'a': 1}\n", b'{"a": 1,}\n', b"{} {}\n", b"// c\n{}\n"):
            with self.subTest(text=text):
                errors = errors_of({"a.json": text})
                self.assertTrue(any(e.startswith("a.json: invalid JSON: ") for e in errors), errors)

    def test_duplicate_key_at_top_level(self) -> None:
        self.assertEqual(
            errors_of({"a.json": b'{"a": 1, "a": 2}\n'}), ["a.json: duplicate key 'a'"]
        )

    def test_duplicate_key_nested_and_in_arrays(self) -> None:
        text = b'{"o": {"x": 1, "y": 2, "x": 3}, "l": [{"k": 1, "k": 1}]}\n'
        self.assertEqual(
            errors_of({"a.json": text}), ["a.json: duplicate key 'k'", "a.json: duplicate key 'x'"]
        )

    def test_duplicate_key_written_with_an_escape(self) -> None:
        self.assertEqual(
            errors_of({"a.json": b'{"a": 1, "\\u0061": 2}\n'}), ["a.json: duplicate key 'a'"]
        )

    def test_duplicate_key_is_reported_once(self) -> None:
        self.assertEqual(
            errors_of({"a.json": b'{"a": 1, "a": 2, "a": 3}\n'}), ["a.json: duplicate key 'a'"]
        )

    def test_bom(self) -> None:
        self.assertEqual(
            errors_of({"a.json": b"\xef\xbb\xbf{}\n"}),
            ["a.json: starts with a byte order mark (BOM)"],
        )

    def test_bom_does_not_hide_a_real_error(self) -> None:
        errors = errors_of({"a.json": b'\xef\xbb\xbf{"a": 1, "a": 2}\n'})
        self.assertEqual(len(errors), 2)
        self.assertIn("a.json: duplicate key 'a'", errors)

    def test_not_utf8(self) -> None:
        for data in (b'{"a": "\xff"}\n', b'{"a": "\xc3"}\n', b'{"a": "\xed\xa0\x80"}\n'):
            with self.subTest(data=data):
                self.assertEqual(errors_of({"a.json": data}), ["a.json: not valid UTF-8 (byte 7)"])

    def test_utf16_is_not_utf8(self) -> None:
        errors = errors_of({"a.json": '{"a": 1}\n'.encode("utf-16")})
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("a.json: not valid UTF-8"))

    def test_no_final_newline(self) -> None:
        self.assertEqual(
            errors_of({"a.json": b'{"a": 1}'}), ["a.json: no newline at the end of the file"]
        )

    def test_final_newline_is_not_checked_when_the_file_cannot_be_read(self) -> None:
        self.assertEqual(len(errors_of({"a.json": b'{"a": "\xff"}'})), 1)

    def test_nan_and_infinity_are_not_json(self) -> None:
        for text in (b'{"a": NaN}\n', b'{"a": Infinity}\n', b'{"a": -Infinity}\n'):
            with self.subTest(text=text):
                errors = errors_of({"a.json": text})
                self.assertEqual(len(errors), 1)
                self.assertIn("is not valid JSON", errors[0])

    def test_lone_surrogate(self) -> None:
        for text in (b'{"a": "\\ud800"}\n', b'{"\\udc00": 1}\n', b'{"a": ["x", "\\ud83d"]}\n'):
            with self.subTest(text=text):
                self.assertEqual(
                    errors_of({"a.json": text}), ["a.json: a string has a lone surrogate"]
                )

    def test_a_surrogate_pair_is_fine(self) -> None:
        self.assertEqual(run_check({"a.json": b'{"a": "\\ud83d\\ude00"}\n'}), [])

    def test_deep_nesting_is_an_error_not_a_crash(self) -> None:
        text = b"[" * 100000 + b"]" * 100000 + b"\n"
        errors = errors_of({"a.json": text})
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("a.json: invalid JSON"))

    def test_huge_file_is_refused_without_reading_it_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.json"
            with big.open("wb") as handle:
                handle.truncate(cd.MAX_BYTES + 1)
            problems = cd.check_files(Path(tmp), ["big.json"])
        self.assertEqual([str(p) for p in problems], ["big.json: larger than 16 MiB"])

    def test_a_folder_named_like_a_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "d.json").mkdir()
            problems = cd.check_files(Path(tmp), ["d.json"])
        self.assertEqual(len(problems), 1)
        self.assertTrue(str(problems[0]).startswith("d.json: cannot read the file"))

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            problems = cd.check_files(Path(tmp), ["nope.json"])
        self.assertEqual(len(problems), 1)
        self.assertTrue(str(problems[0]).startswith("nope.json: cannot read the file"))


class TomlRules(unittest.TestCase):
    def test_invalid_toml(self) -> None:
        for text in (b"a = ", b"[x\n", b"a = 1\na = 2\n", b"[t]\n[t]\n", b'a = "x\n', b"a b = 1\n"):
            with self.subTest(text=text):
                errors = errors_of({"a.toml": text})
                self.assertEqual(len(errors), 1)
                self.assertTrue(errors[0].startswith("a.toml: invalid TOML: "), errors)

    def test_duplicate_key_in_toml_is_an_error(self) -> None:
        errors = errors_of({"a.toml": b'name = "a"\nname = "b"\n'})
        self.assertEqual(len(errors), 1)
        self.assertIn("invalid TOML", errors[0])

    def test_bom_in_toml(self) -> None:
        self.assertEqual(
            errors_of({"a.toml": b"\xef\xbb\xbfa = 1\n"}),
            ["a.toml: starts with a byte order mark (BOM)"],
        )

    def test_not_utf8_toml(self) -> None:
        self.assertEqual(
            errors_of({"a.toml": b'a = "\xff"\n'}), ["a.toml: not valid UTF-8 (byte 5)"]
        )

    def test_deep_nesting_is_an_error_not_a_crash(self) -> None:
        text = b"a = " + b"[" * 100000 + b"]" * 100000 + b"\n"
        errors = errors_of({"a.toml": text})
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("a.toml: invalid TOML"))


class SiteStrings(unittest.TestCase):
    def test_equal_languages_are_good(self) -> None:
        files = languages(
            de={"hello": "Hallo", "link": '<a href="{{url}}">da</a>', "two": "{{b}} {{a}}"}
        )
        self.assertEqual(run_check(files), [])

    def test_missing_key_is_a_warning(self) -> None:
        files = languages(de={"hello": "Hallo", "link": "{{url}}"})
        files[f"{I18N}/de.json"] = dump({"hello": "Hallo", "link": "{{url}}"})
        self.assertEqual(errors_of(files), [])
        self.assertEqual(
            warnings_of(files), [f"{I18N}/de.json: warning: key 'two' is missing (see en.json)"]
        )

    def test_every_missing_key_is_listed(self) -> None:
        files = languages(de={})
        self.assertEqual(len(warnings_of(files)), 3)
        self.assertEqual(errors_of(files), [])

    def test_extra_key_is_an_error(self) -> None:
        files = languages(de={**EN, "more": "x"})
        self.assertEqual(errors_of(files), [f"{I18N}/de.json: extra key 'more' (not in en.json)"])

    def test_extra_key_in_one_language_does_not_touch_the_others(self) -> None:
        files = languages(de={**EN, "more": "x"}, fr=dict(EN))
        self.assertEqual(len(errors_of(files)), 1)

    def test_a_key_missing_in_english_is_extra_everywhere_else(self) -> None:
        files = languages(de=dict(EN))
        files[f"{I18N}/en.json"] = dump({"hello": "Hello"})
        self.assertEqual(len(errors_of(files)), 2)

    def test_changed_placeholder_name(self) -> None:
        files = languages(de={**EN, "link": '<a href="{{link}}">da</a>'})
        self.assertEqual(
            errors_of(files),
            [
                f"{I18N}/de.json: key 'link': placeholders [{{{{link}}}}] "
                f"differ from en.json [{{{{url}}}}]"
            ],
        )

    def test_placeholder_dropped_or_added(self) -> None:
        for text in ("no placeholder", "{{url}} {{more}}", "{{url}} {{url}}"):
            with self.subTest(text=text):
                files = languages(de={**EN, "link": text})
                self.assertEqual(len(errors_of(files)), 1)

    def test_one_of_two_placeholders_dropped(self) -> None:
        files = languages(de={**EN, "two": "{{a}} only"})
        self.assertEqual(len(errors_of(files)), 1)

    def test_spaces_inside_the_braces_do_not_matter(self) -> None:
        files = languages(de={**EN, "link": "{{ url }}"})
        self.assertEqual(errors_of(files), [])

    def test_single_brace_placeholders(self) -> None:
        en = {"x": "Hi {name}"}
        good = {f"{I18N}/en.json": dump(en), f"{I18N}/de.json": dump({"x": "Hallo {name}"})}
        bad = {f"{I18N}/en.json": dump(en), f"{I18N}/de.json": dump({"x": "Hallo {nom}"})}
        self.assertEqual(run_check(good), [])
        self.assertEqual(len(errors_of(bad)), 1)

    def test_single_and_double_braces_are_different(self) -> None:
        files = languages(de={**EN, "link": "{url}"})
        self.assertEqual(len(errors_of(files)), 1)

    def test_unmatched_brace(self) -> None:
        for text in ("{{url}", "{url}}", "x { y", "x } y"):
            with self.subTest(text=text):
                files = languages(de={**EN, "hello": text})
                errors = errors_of(files)
                self.assertTrue(any("unmatched" in e for e in errors), errors)

    def test_non_string_value(self) -> None:
        for value in (1, None, ["a"], {"a": "b"}, True):
            with self.subTest(value=value):
                files = languages(de={**EN, "hello": value})  # type: ignore[dict-item]
                self.assertEqual(errors_of(files), [f"{I18N}/de.json: must map names to strings"])

    def test_top_level_must_be_an_object(self) -> None:
        for text in (b"[]\n", b'"x"\n', b"null\n"):
            with self.subTest(text=text):
                files = languages()
                files[f"{I18N}/de.json"] = text
                self.assertEqual(errors_of(files), [f"{I18N}/de.json: must map names to strings"])

    def test_english_must_map_names_to_strings_too(self) -> None:
        files = {f"{I18N}/en.json": b"[1]\n", f"{I18N}/de.json": dump({})}
        self.assertEqual(errors_of(files), [f"{I18N}/en.json: must map names to strings"])

    def test_invalid_language_file_is_one_error(self) -> None:
        files = languages()
        files[f"{I18N}/de.json"] = b"{\n"
        errors = errors_of(files)
        self.assertEqual(len(errors), 1)
        self.assertIn("invalid JSON", errors[0])
        self.assertTrue(all(e.startswith(f"{I18N}/de.json: ") for e in errors))

    def test_invalid_english_file_does_not_crash_the_comparison(self) -> None:
        files = {f"{I18N}/en.json": b"{\n", f"{I18N}/de.json": dump(EN)}
        errors = errors_of(files)
        self.assertTrue(errors)
        self.assertTrue(all(e.startswith(f"{I18N}/en.json: ") for e in errors))

    def test_duplicate_key_in_a_language_file(self) -> None:
        files = languages()
        files[f"{I18N}/de.json"] = (
            b'{"hello": "a", "hello": "b", "link": "{{url}}", "two": "{{a}}{{b}}"}\n'
        )
        self.assertEqual(errors_of(files), [f"{I18N}/de.json: duplicate key 'hello'"])

    def test_no_english_file(self) -> None:
        files = {f"{I18N}/de.json": dump(EN)}
        self.assertEqual(errors_of(files), [f"{I18N}/de.json: en.json cannot be read"])

    def test_one_language_checked_against_english_on_disk(self) -> None:
        files = languages(de={"hello": "Hallo"})
        errors = errors_of(files, only=[f"{I18N}/de.json"])
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings_of(files, only=[f"{I18N}/de.json"])), 2)
        files[f"{I18N}/de.json"] = dump({**EN, "x": "y"})
        self.assertEqual(len(errors_of(files, only=[f"{I18N}/de.json"])), 1)

    def test_other_json_folders_are_not_compared(self) -> None:
        files = {"other/en.json": dump(EN), "other/de.json": dump({"x": "y"})}
        self.assertEqual(run_check(files), [])

    def test_only_json_files_of_the_folder_count(self) -> None:
        files = languages(de=dict(EN))
        files[f"{I18N}/notes.toml"] = b"a = 1\n"
        self.assertEqual(run_check(files), [])

    def test_a_language_without_changes_has_no_warnings(self) -> None:
        self.assertEqual(warnings_of(languages(de=dict(EN), uk=dict(EN))), [])

    def test_output_is_sorted_and_stable(self) -> None:
        files = languages(de={"hello": "x"}, cs={"hello": "y"})
        first = [str(p) for p in run_check(files)]
        self.assertEqual(first, [str(p) for p in run_check(files)])
        self.assertEqual(first, sorted(first, key=lambda s: (s.split(": ")[0], s)))


class LegacyList(unittest.TestCase):
    def test_a_known_old_violation_is_only_a_warning(self) -> None:
        path = "docs/design/motion/tokens.json"
        self.assertIn((path, "final-newline"), cd.LEGACY)
        files = {path: b'{"a": 1}'}
        self.assertEqual(errors_of(files), [])
        self.assertEqual(
            warnings_of(files), [f"{path}: warning: no newline at the end of the file"]
        )

    def test_the_same_violation_in_a_new_file_is_an_error(self) -> None:
        self.assertEqual(
            errors_of({"docs/design/motion/new.json": b'{"a": 1}'}),
            ["docs/design/motion/new.json: no newline at the end of the file"],
        )

    def test_a_legacy_file_with_another_violation_is_an_error(self) -> None:
        path = "docs/design/motion/tokens.json"
        self.assertEqual(errors_of({path: b'{"a": 1, "a": 2}'}), [f"{path}: duplicate key 'a'"])

    def test_legacy_entries_are_real_rules(self) -> None:
        rules = {"read", "size", "utf8", "bom", "final-newline", "invalid", "duplicate-key"}
        for _, rule in cd.LEGACY:
            self.assertIn(rule, rules)


class FilesAndMain(unittest.TestCase):
    def run_main(self, args: list[str], cwd: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        previous = Path.cwd()
        os.chdir(cwd)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cd.main(args)
        finally:
            os.chdir(previous)
        return code, out.getvalue(), err.getvalue()

    def test_main_exit_codes_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.json").write_text('{"a": 1}\n', encoding="utf-8")
            (Path(tmp) / "b.toml").write_text("b = 1\n", encoding="utf-8")
            code, out, err = self.run_main(["a.json", "b.toml"], Path(tmp))
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(out, "2 data files checked: 0 errors, 0 warnings\n")

            (Path(tmp) / "a.json").write_text('{"a": 1, "a": 2}\n', encoding="utf-8")
            code, out, err = self.run_main(["a.json", "b.toml"], Path(tmp))
            self.assertEqual(code, 1)
            self.assertEqual(err, "a.json: duplicate key 'a'\n")
            self.assertIn("1 errors, 0 warnings", out)

    def test_main_warnings_do_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for lang, data in languages(de={"hello": "Hallo"}).items():
                (Path(tmp) / lang).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / lang).write_bytes(data)
            code, out, err = self.run_main([f"{I18N}/de.json"], Path(tmp))
        self.assertEqual(code, 0)
        self.assertIn("warning: key 'link' is missing", err)
        self.assertIn("0 errors, 2 warnings", out)

    def test_main_wrong_call_is_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for args in (["--bogus"], ["file.txt"], ["a.json", "x.yaml"], ["-x", "a.json"]):
                with self.subTest(args=args):
                    code, _, err = self.run_main(args, Path(tmp))
                    self.assertEqual(code, 2)
                    self.assertIn("usage:", err)

    def test_main_help_is_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_main(["--help"], Path(tmp))[0], 0)

    def test_main_returns_only_0_1_or_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "bad.json").write_bytes(b"\xff")
            code, _, _ = self.run_main(["bad.json"], Path(tmp))
        self.assertEqual(code, 1)


class TrackedFiles(unittest.TestCase):
    def test_only_tracked_json_and_toml_files_are_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "sub").mkdir()
            tracked = ["sub/a.json", "b.toml", "C.JSON", "readme.txt", "x.json.bak", "my file.json"]
            for rel in [*tracked, "untracked.json"]:
                (root / rel).write_text("", encoding="utf-8")
            subprocess.run(["git", "add", *tracked], cwd=root, check=True)
            self.assertEqual(
                cd.tracked_data_files(root), ["C.JSON", "b.toml", "my file.json", "sub/a.json"]
            )


class WholeRepo(unittest.TestCase):
    def test_the_repo_has_no_errors(self) -> None:
        paths = cd.tracked_data_files(ROOT)
        self.assertTrue(paths)
        errors = [str(p) for p in cd.check_files(ROOT, paths) if not p.warning]
        self.assertEqual(errors, [])


class NeverCrashes(unittest.TestCase):
    """A broken file is a problem to report, never an exception."""

    def test_every_truncation_of_a_json_file(self) -> None:
        text = dump({"a": [1, 2.5, {"b": "\u0456\\"}], "c": None, "d": "{{x}}"}).decode()
        for end in range(len(text) + 1):
            run_check({"a.json": text[:end].encode()})

    def test_every_truncation_of_a_toml_file(self) -> None:
        text = 'name = "x"\n[t]\nk = [1, 2]\n[[i]]\nid = 1\n'
        for end in range(len(text) + 1):
            run_check({"a.toml": text[:end].encode()})

    def test_odd_bytes(self) -> None:
        for data in (
            b"",
            b"\n",
            b"\x00",
            b"\xef\xbb\xbf",
            b"\xef\xbb\xbf\xef\xbb\xbf{}\n",
            b"\r",
            b"\x1a",
        ):
            for name in ("a.json", "a.toml", f"{I18N}/en.json"):
                with self.subTest(data=data, name=name):
                    run_check({name: data})

    def test_odd_i18n_shapes(self) -> None:
        for text in (b"{}\n", b'{"": ""}\n', b'{"a": "{"}\n', b'{"a": "}{"}\n', b'{"a": "{{}}"}\n'):
            with self.subTest(text=text):
                run_check({f"{I18N}/en.json": text, f"{I18N}/de.json": text})


if __name__ == "__main__":
    unittest.main()
