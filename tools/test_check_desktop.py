#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_desktop.py (the check of the tracked .desktop files).

Run: python3 tools/test_check_desktop.py
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_desktop as cd  # noqa: E402  (after sys.path.insert above)

APP_FILE = "io.github.stdenis1980.Demo.desktop"
OTHER_FILE = "vilna-demo-watch.desktop"


def translated(key: str, english: str = "Demo", skip: tuple[str, ...] = ()) -> str:
    lines = [f"{key}={english}"]
    lines += [f"{key}[{lang}]={english} {lang}" for lang in cd.LANGS if lang not in skip]
    return "\n".join(lines)


def desktop(extra: str = "", head: str = "", with_name: bool = True) -> str:
    """A good file; ``extra`` is appended to the [Desktop Entry] group."""
    name = translated("Name") if with_name else ""
    return (
        f"# SPDX-License-Identifier: GPL-3.0-or-later\n{head}[Desktop Entry]\n"
        f"Type=Application\n{name}\n{translated('Comment', 'Demo app')}\n"
        "Exec=demo-app\nIcon=io.github.stdenis1980.Demo\nCategories=GNOME;Utility;\n"
        f"Keywords=demo;test;\nKeywords[uk]=демо;тест;\n{extra}\n"
    )


def line_of(text: str, start: str) -> int:
    """The 1-based number of the first line that starts with ``start``."""
    return next(n for n, line in enumerate(text.splitlines(), start=1) if line.startswith(start))


def problems_of(text: str, name: str = APP_FILE) -> list[cd.Problem]:
    """Write the text to a temporary file and check it the way main() does."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / name).write_text(text, encoding="utf-8")
        return cd.check_files(Path(tmp), [name])


def errors_of(text: str, name: str = APP_FILE) -> list[str]:
    return [str(p) for p in problems_of(text, name) if not p.warning]


def warnings_of(text: str, name: str = APP_FILE) -> list[str]:
    return [str(p) for p in problems_of(text, name) if p.warning]


class GoodFiles(unittest.TestCase):
    def test_good_file_passes(self) -> None:
        self.assertEqual(problems_of(desktop()), [])

    def test_comments_empty_lines_and_spaces_around_equals_are_fine(self) -> None:
        text = desktop("\n# a comment\n\nTerminal = false\n", head="# before the group\n\n")
        self.assertEqual(problems_of(text), [])

    def test_extra_group_with_its_own_keys_is_fine(self) -> None:
        text = desktop("Actions=vpn;") + "\n[Desktop Action vpn]\n" + translated("Name", "VPN")
        text += "\nExec=demo-app --vpn\n"
        self.assertEqual(problems_of(text), [])

    def test_file_without_translations_needs_none(self) -> None:
        text = "[Desktop Entry]\nType=Application\nName=Demo\nExec=demo\nNoDisplay=true\n"
        self.assertEqual(problems_of(text, OTHER_FILE), [])

    def test_directory_needs_neither_exec_nor_name(self) -> None:
        self.assertEqual(problems_of("[Desktop Entry]\nType=Directory\n", OTHER_FILE), [])


class Syntax(unittest.TestCase):
    def test_first_group_must_be_desktop_entry(self) -> None:
        text = "[Desktop Action x]\nName=X\n\n[Desktop Entry]\nType=Application\nName=D\nExec=d\n"
        self.assertTrue(any("first group must be [Desktop Entry]" in e for e in errors_of(text)))

    def test_file_without_any_group(self) -> None:
        errors = errors_of("# only a comment\n")
        self.assertTrue(any("first group must be [Desktop Entry]" in e for e in errors))

    def test_key_before_the_first_group(self) -> None:
        errors = errors_of("Type=Application\n[Desktop Entry]\nType=Application\nName=D\nExec=d\n")
        self.assertTrue(any("line 1: key before the first group" in e for e in errors))

    def test_line_that_is_nothing_valid(self) -> None:
        errors = errors_of(desktop("this is not a key value line"))
        self.assertTrue(any("not a comment, empty line, [Group] or Key=Value" in e for e in errors))

    def test_key_with_bad_characters(self) -> None:
        errors = errors_of(desktop("Bad_Key=1\nNa me=2"))
        self.assertEqual(sum("not a comment, empty line" in e for e in errors), 2)

    def test_broken_group_header(self) -> None:
        errors = errors_of(desktop("[Broken group"))
        self.assertTrue(any("invalid group header" in e for e in errors))

    def test_duplicate_key_in_a_group(self) -> None:
        errors = errors_of(desktop("Exec=other"))
        self.assertTrue(any("duplicate key Exec in [Desktop Entry]" in e for e in errors))

    def test_duplicate_translated_key(self) -> None:
        errors = errors_of(desktop("Name[uk]=Інше"))
        self.assertTrue(any("duplicate key Name[uk]" in e for e in errors))

    def test_same_key_in_two_groups_is_not_a_duplicate(self) -> None:
        text = desktop() + "\n[Desktop Action a]\nName=A\nExec=a\n"
        self.assertEqual(errors_of(text), [])

    def test_repeated_group(self) -> None:
        text = desktop() + "\n[Desktop Action a]\nExec=a\n\n[Desktop Action a]\nExec=b\n"
        self.assertTrue(any("group [Desktop Action a] repeated" in e for e in errors_of(text)))

    def test_message_names_the_path_and_the_line(self) -> None:
        errors = errors_of(desktop("Exec=other"))
        self.assertTrue(errors[0].startswith(f"{APP_FILE}: line "), errors)

    def test_file_that_is_not_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / OTHER_FILE).write_bytes(b"[Desktop Entry]\nName=\xff\xfe\n")
            found = cd.check_files(Path(tmp), [OTHER_FILE])
        self.assertEqual([str(p) for p in found], [f"{OTHER_FILE}: the file is not valid UTF-8"])

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            found = cd.check_files(Path(tmp), ["nope.desktop"])
        self.assertEqual(len(found), 1)
        self.assertIn("nope.desktop: cannot read the file", str(found[0]))


class TypeAndRequiredKeys(unittest.TestCase):
    def test_missing_type(self) -> None:
        text = "[Desktop Entry]\nName=D\nExec=d\n"
        self.assertEqual(errors_of(text, OTHER_FILE), [f"{OTHER_FILE}: missing Type"])

    def test_unknown_type(self) -> None:
        text = "[Desktop Entry]\nType=Service\nName=D\nExec=d\n"
        errors = errors_of(text, OTHER_FILE)
        self.assertEqual(len(errors), 1)
        self.assertIn("Type=Service is not one of", errors[0])

    def test_application_without_name(self) -> None:
        errors = errors_of("[Desktop Entry]\nType=Application\nExec=d\n", OTHER_FILE)
        self.assertEqual(errors, [f"{OTHER_FILE}: Type=Application needs Name"])

    def test_application_without_exec(self) -> None:
        errors = errors_of("[Desktop Entry]\nType=Application\nName=D\n", OTHER_FILE)
        self.assertEqual(errors, [f"{OTHER_FILE}: Type=Application needs Exec"])

    def test_application_with_empty_exec(self) -> None:
        errors = errors_of("[Desktop Entry]\nType=Application\nName=D\nExec=\n", OTHER_FILE)
        self.assertEqual(errors, [f"{OTHER_FILE}: Type=Application needs Exec"])

    def test_link_and_directory_are_allowed_types(self) -> None:
        for kind in ("Link", "Directory"):
            self.assertEqual(errors_of(f"[Desktop Entry]\nType={kind}\nName=D\n", OTHER_FILE), [])


class Icons(unittest.TestCase):
    def icon_errors(self, icon: str, name: str = APP_FILE) -> list[str]:
        text = f"[Desktop Entry]\nType=Application\nName=D\nExec=d\nIcon={icon}\n"
        return errors_of(text, name)

    def test_icon_equal_to_the_app_id(self) -> None:
        self.assertEqual(self.icon_errors("io.github.stdenis1980.Demo"), [])

    def test_icon_may_be_a_known_theme_name(self) -> None:
        self.assertEqual(self.icon_errors("vilna-backup"), [])
        self.assertEqual(self.icon_errors("mail-send-symbolic"), [])

    def test_icon_of_another_app_is_an_error(self) -> None:
        errors = self.icon_errors("io.github.stdenis1980.Other")
        self.assertEqual(len(errors), 1)
        self.assertIn(
            "neither the app id io.github.stdenis1980.Demo nor a known icon name", errors[0]
        )

    def test_unknown_theme_name_is_an_error(self) -> None:
        self.assertEqual(len(self.icon_errors("my-private-icon")), 1)

    def test_icon_with_a_path_is_an_error(self) -> None:
        for icon in ("/usr/share/icons/demo", "icons/demo"):
            errors = self.icon_errors(icon)
            self.assertEqual(len(errors), 1, icon)
            self.assertIn("without a path or extension", errors[0])

    def test_icon_with_an_extension_is_an_error(self) -> None:
        for icon in ("io.github.stdenis1980.Demo.svg", "vilna-backup.PNG"):
            self.assertEqual(len(self.icon_errors(icon)), 1, icon)

    def test_file_without_app_id_in_its_name_is_not_checked(self) -> None:
        self.assertEqual(self.icon_errors("whatever-icon", OTHER_FILE), [])

    def test_file_without_icon_is_fine(self) -> None:
        text = "[Desktop Entry]\nType=Application\nName=D\nExec=d\n"
        self.assertEqual(errors_of(text), [])

    def test_app_id_comes_from_the_file_name(self) -> None:
        self.assertEqual(
            self.icon_errors(
                "io.github.stdenis1980.Gloxi-wake", "io.github.stdenis1980.Gloxi-wake.desktop"
            ),
            [],
        )
        self.assertEqual(
            len(
                self.icon_errors(
                    "io.github.stdenis1980.Demo", "io.github.stdenis1980.Gloxi.desktop"
                )
            ),
            1,
        )


class ListValues(unittest.TestCase):
    def test_each_list_key_must_end_with_a_semicolon(self) -> None:
        for key in ("Categories", "Keywords", "MimeType"):
            text = "[Desktop Entry]\nType=Application\nName=D\nExec=d\n" + f"{key}=one;two\n"
            errors = errors_of(text, OTHER_FILE)
            self.assertEqual(len(errors), 1, key)
            self.assertIn(f"{key} must end with ';'", errors[0])

    def test_translated_list_is_checked_too(self) -> None:
        errors = errors_of(desktop("Keywords[de]=eins;zwei"))
        self.assertTrue(any("Keywords[de] must end with ';'" in e for e in errors))

    def test_single_value_with_semicolon_is_fine(self) -> None:
        self.assertEqual(errors_of(desktop("MimeType=x-scheme-handler/demo;")), [])

    def test_escaped_semicolon_is_not_a_terminator(self) -> None:
        self.assertEqual(len(errors_of(desktop("Keywords[de]=eins\\;"))), 1)
        self.assertEqual(errors_of(desktop("Keywords[de]=eins\\\\;")), [])

    def test_non_list_keys_are_not_checked(self) -> None:
        self.assertEqual(errors_of(desktop("X-Note=no semicolon at the end")), [])


class Translations(unittest.TestCase):
    def test_missing_languages_are_listed(self) -> None:
        text = (
            desktop()
            .replace("Name[eu]=Demo eu\n", "")
            .replace("Name[ca@valencia]=Demo ca@valencia\n", "")
        )
        errors = errors_of(text)
        self.assertEqual(
            errors,
            [f"{APP_FILE}: [Desktop Entry] Name is not translated into: ca@valencia eu"],
        )

    def test_each_translated_language_is_required(self) -> None:
        for lang in cd.LANGS:
            text = "[Desktop Entry]\nType=Application\nExec=d\n" + translated("Name", skip=(lang,))
            errors = errors_of(text, OTHER_FILE)
            self.assertEqual(
                errors, [f"{OTHER_FILE}: [Desktop Entry] Name is not translated into: {lang}"]
            )

    def test_a_single_translation_makes_all_languages_required(self) -> None:
        text = "[Desktop Entry]\nType=Application\nName=D\nExec=d\nComment=C\nComment[uk]=К\n"
        errors = errors_of(text, OTHER_FILE)
        self.assertEqual(len(errors), 1)
        self.assertIn(
            "Comment is not translated into: es pl de fr it ro cs lt ca ca@valencia gl eu pt",
            errors[0],
        )

    def test_untranslated_name_next_to_translated_comment_is_fine(self) -> None:
        self.assertEqual(
            problems_of(
                desktop(with_name=False).replace("Type=Application", "Type=Application\nName=Demo")
            ),
            [],
        )

    def test_translation_without_english_text(self) -> None:
        text = "[Desktop Entry]\nType=Application\nName=D\nExec=d\n" + translated(
            "Comment"
        ).replace("Comment=Demo\n", "")
        errors = errors_of(text, OTHER_FILE)
        self.assertEqual(
            errors, [f"{OTHER_FILE}: [Desktop Entry] has Comment[..] but no English Comment"]
        )

    def test_unknown_language_suffix(self) -> None:
        for lang in ("en", "ru", "de_DE", "sr@latin"):
            errors = errors_of(desktop(f"GenericName[{lang}]=X"))
            self.assertEqual(len(errors), 1, lang)
            self.assertIn(f"unknown language suffix in GenericName[{lang}]", errors[0])

    def test_unknown_suffix_on_a_translated_key(self) -> None:
        errors = errors_of(desktop("Name[ru]=Демо"))
        self.assertTrue(any("unknown language suffix in Name[ru]" in e for e in errors))

    def test_empty_translation_is_an_error(self) -> None:
        text = desktop().replace("Comment[pl]=Demo app pl", "Comment[pl]=")
        errors = errors_of(text)
        self.assertEqual(
            errors, [f"{APP_FILE}: line {line_of(text, 'Comment[pl]')}: Comment[pl] is empty"]
        )

    def test_translation_equal_to_english_is_only_a_warning(self) -> None:
        text = desktop().replace("Name[de]=Demo de", "Name[de]=Demo")
        self.assertEqual(errors_of(text), [])
        self.assertEqual(len(warnings_of(text)), 1)
        line = line_of(text, "Name[de]")
        self.assertIn(
            f"warning: line {line}: Name[de] equals the English text", warnings_of(text)[0]
        )

    def test_action_groups_are_checked_too(self) -> None:
        text = (
            desktop()
            + "\n[Desktop Action a]\n"
            + translated("Name", "A", skip=("gl",))
            + "\nExec=a\n"
        )
        errors = errors_of(text)
        self.assertEqual(
            errors, [f"{APP_FILE}: [Desktop Action a] Name is not translated into: gl"]
        )

    def test_known_old_violation_is_a_warning(self) -> None:
        path = next(p for p, rule in cd.LEGACY if rule == "incomplete-translation")
        text = "[Desktop Entry]\nType=Application\nName=D\nExec=d\nComment=C\nComment[uk]=К\n"
        found = cd.check_text(path, text)
        self.assertEqual([p.warning for p in found], [True])
        self.assertIn("warning: [Desktop Entry] Comment is not translated into", str(found[0]))
        # The same file under another name is still an error.
        self.assertEqual([p.warning for p in cd.check_text("other.desktop", text)], [False])


class Main(unittest.TestCase):
    def run_main(self, args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cd.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_exit_0_for_a_good_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / APP_FILE
            path.write_text(desktop(), encoding="utf-8")
            code, out, err = self.run_main([str(path)])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("1 desktop files checked: 0 errors, 0 warnings", out)

    def test_warnings_do_not_fail_the_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / APP_FILE
            text = desktop().replace("Name[de]=Demo de", "Name[de]=Demo")
            path.write_text(text, encoding="utf-8")
            code, out, err = self.run_main([str(path)])
        self.assertEqual(code, 0)
        line = line_of(text, "Name[de]")
        self.assertIn(f"warning: line {line}: Name[de] equals the English text", err)
        self.assertIn("0 errors, 1 warnings", out)

    def test_exit_1_and_message_for_a_bad_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / OTHER_FILE
            path.write_text("[Desktop Entry]\nName=D\n", encoding="utf-8")
            code, out, err = self.run_main([str(path)])
        self.assertEqual(code, 1)
        self.assertIn(f"{path}: missing Type", err)
        self.assertIn("1 errors, 0 warnings", out)

    def test_exit_2_for_an_unknown_option(self) -> None:
        code, _, err = self.run_main(["--bogus"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)

    def test_help_is_not_an_error(self) -> None:
        self.assertEqual(self.run_main(["--help"])[0], 0)


class RealRepo(unittest.TestCase):
    def test_tracked_files_have_no_errors(self) -> None:
        paths = cd.tracked_desktop_files(ROOT)
        self.assertTrue(paths, "the repo has tracked .desktop files")
        errors = [str(p) for p in cd.check_files(ROOT, paths) if not p.warning]
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
