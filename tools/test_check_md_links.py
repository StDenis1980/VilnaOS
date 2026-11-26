#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_md_links.py (local links and anchors in Markdown files).

Run: python3 tools/test_check_md_links.py
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_md_links as cm  # noqa: E402  (after sys.path.insert above)


def check(files: dict[str, str], only: tuple[str, ...] = ()) -> cm.Result:
    """Write the files to a temporary folder and check them; every key is 'tracked'."""
    with tempfile.TemporaryDirectory() as tmp:
        for rel, text in files.items():
            path = Path(tmp) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return cm.check_repo(Path(tmp), list(files), only)


def errors(files: dict[str, str]) -> list[str]:
    return [str(p) for p in check(files).problems if not p.warning]


def doc(text: str) -> dict[str, str]:
    """A repo of one file, docs/a.md, with the given text."""
    return {"docs/a.md": text}


def targets(text: str) -> list[tuple[int, str, str]]:
    return [(link.line, link.target, link.kind) for link in cm.extract_links(text)]


class Extraction(unittest.TestCase):
    def test_link_image_and_definition(self) -> None:
        text = "[a](one.md)\n\n![b](two.png)\n\n[ref]: three.md\n"
        self.assertEqual(
            targets(text),
            [(1, "one.md", "link"), (3, "two.png", "image"), (5, "three.md", "definition")],
        )

    def test_title_angle_brackets_and_parentheses(self) -> None:
        text = "[a](x.md \"Title\") [b](<my file.md>) [c](a_(b).md) [d](y.md 't')"
        self.assertEqual(
            [t for _, t, _ in targets(text)], ["x.md", "my file.md", "a_(b).md", "y.md"]
        )

    def test_image_inside_a_link(self) -> None:
        text = "[![logo](logo.svg)](https://example.org/)"
        self.assertEqual(
            [(t, k) for _, t, k in targets(text)],
            [("logo.svg", "image"), ("https://example.org/", "link")],
        )

    def test_link_text_with_brackets_and_line_break(self) -> None:
        text = "[see [1] and\nmore](x.md)"
        self.assertEqual(targets(text), [(2, "x.md", "link")])

    def test_fenced_blocks_are_skipped(self) -> None:
        text = "```sh\n[a](gone.md)\n```\n[b](kept.md)\n~~~\n[c](gone.md)\n~~~\n"
        self.assertEqual(targets(text), [(4, "kept.md", "link")])

    def test_long_fence_is_not_closed_by_a_short_one(self) -> None:
        text = "````md\n```\n[a](gone.md)\n```\n````\n[b](kept.md)\n"
        self.assertEqual([t for _, t, _ in targets(text)], ["kept.md"])

    def test_indented_fence_in_a_list(self) -> None:
        text = "- item\n\n  ```\n  [a](gone.md)\n  ```\n- [b](kept.md)\n"
        self.assertEqual([t for _, t, _ in targets(text)], ["kept.md"])

    def test_inline_code_is_skipped(self) -> None:
        text = "Use `[a](gone.md)` or ``[b](`gone.md`)`` but [c](kept.md).\n"
        self.assertEqual([t for _, t, _ in targets(text)], ["kept.md"])

    def test_link_text_in_code_is_still_a_link(self) -> None:
        self.assertEqual([t for _, t, _ in targets("[`docs/`](docs/)")], ["docs/"])

    def test_html_comment_is_skipped(self) -> None:
        text = "<!-- [a](gone.md)\n[b](gone.md) -->\n[c](kept.md)\n"
        self.assertEqual(targets(text), [(3, "kept.md", "link")])

    def test_escaped_brackets_and_footnotes_are_not_links(self) -> None:
        text = "\\[a\\](gone.md) and text[^1]\n\n[^1]: a note: not a link\n"
        self.assertEqual(targets(text), [])

    def test_brackets_without_a_link(self) -> None:
        self.assertEqual(targets("array[0] (x.md) and [a] (x.md)"), [])

    def test_windows_line_endings(self) -> None:
        self.assertEqual(
            targets("[a](x.md)\r\n[b](y.md)\r\n"), [(1, "x.md", "link"), (2, "y.md", "link")]
        )


class ExternalLinks(unittest.TestCase):
    def test_http_https_and_mailto_are_counted_and_not_opened(self) -> None:
        text = (
            "[a](http://nowhere.invalid/x) [b](https://nowhere.invalid/y#frag)\n"
            "[c](mailto:someone@example.org) [d](tel:+10000000) [e](//nowhere.invalid/z)\n"
            "[f]: https://nowhere.invalid/def\n"
        )
        with mock.patch("socket.socket", side_effect=AssertionError("network used")):
            result = check(doc(text))
        self.assertEqual(result.problems, [])
        self.assertEqual((result.links, result.external, result.local), (6, 6, 0))

    def test_uppercase_scheme(self) -> None:
        self.assertEqual(check(doc("[a](HTTPS://Example.org/)")).external, 1)

    def test_external_anchor_is_not_checked(self) -> None:
        self.assertEqual(errors(doc("[a](https://example.org/#nothing-here)")), [])


class LocalPaths(unittest.TestCase):
    def test_existing_file_passes(self) -> None:
        files = {
            "docs/a.md": "[b](../b.md) [c](c.md) [d](sub/d.md)",
            "b.md": "",
            "docs/c.md": "",
            "docs/sub/d.md": "",
        }
        self.assertEqual(errors(files), [])

    def test_missing_file_is_an_error_with_path_and_line(self) -> None:
        found = errors({"docs/a.md": "text\n\n[b](missing.md)\n"})
        self.assertEqual(
            found,
            [
                "docs/a.md: line 3: 'missing.md' does not exist "
                "(no tracked file or folder docs/missing.md)"
            ],
        )

    def test_link_is_relative_to_the_file_not_to_the_repo(self) -> None:
        files = {"docs/a.md": "[x](x.md)", "x.md": ""}
        self.assertEqual(len(errors(files)), 1)
        files = {"docs/a.md": "[x](../x.md)", "x.md": ""}
        self.assertEqual(errors(files), [])

    def test_image_and_definition_targets_are_checked(self) -> None:
        found = errors({"a.md": "![p](no.png)\n\n[r]: no.md\n"})
        self.assertEqual(len(found), 2)
        self.assertIn("line 1:", found[0])
        self.assertIn("line 3:", found[1])

    def test_folder_with_tracked_files_counts(self) -> None:
        files = {"a.md": "[d](docs/) [e](docs) [f](docs/deep/)", "docs/deep/x.txt": ""}
        self.assertEqual(errors(files), [])

    def test_folder_without_tracked_files_is_an_error(self) -> None:
        found = errors({"a.md": "[d](empty/)", "other/x.txt": ""})
        self.assertEqual(len(found), 1)
        self.assertIn("does not exist", found[0])

    def test_repo_root_as_a_target(self) -> None:
        self.assertEqual(errors({"docs/a.md": "[root](../) [self](.)"}), [])

    def test_file_with_a_trailing_slash_is_an_error(self) -> None:
        found = errors({"a.md": "[b](b.md/)", "b.md": ""})
        self.assertEqual(len(found), 1)
        self.assertIn("is a file, not a folder", found[0])

    def test_percent_escapes_are_decoded(self) -> None:
        files = {
            "a.md": (
                "[1](my%20file.md) [3](<my file.md>) [2](%D0%BF%D0%BE%D1%80%D0%B0%D0%B4%D0%B0.md)"
            ),
            "my file.md": "",
            "порада.md": "",
        }
        self.assertEqual(errors(files), [])

    def test_wrong_percent_escape_does_not_match(self) -> None:
        self.assertEqual(len(errors({"a.md": "[1](my%2520file.md)", "my file.md": ""})), 1)

    def test_query_is_ignored(self) -> None:
        self.assertEqual(errors({"a.md": "[1](b.md?plain=1)", "b.md": ""}), [])

    def test_names_are_case_sensitive(self) -> None:
        self.assertEqual(len(errors({"a.md": "[1](B.md)", "b.md": ""})), 1)

    def test_path_leaving_the_repository(self) -> None:
        found = errors({"a.md": "[1](../outside.md)"})
        self.assertEqual(len(found), 1)
        self.assertIn("points outside the repository", found[0])

    def test_empty_target(self) -> None:
        found = errors({"a.md": "[1]()"})
        self.assertEqual(len(found), 1)
        self.assertIn("empty link target in link", found[0])

    def test_untracked_file_does_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.md").write_text("[1](local-only.md)", encoding="utf-8")
            (Path(tmp) / "local-only.md").write_text("", encoding="utf-8")
            result = cm.check_repo(Path(tmp), ["a.md"])
        self.assertEqual(result.errors, 1)


class AbsolutePaths(unittest.TestCase):
    def test_absolute_paths_are_errors(self) -> None:
        for target in (
            "/home/denis/notes.md",
            "C:\\Users\\Denis\\notes.md",
            "c:/Users/Denis/notes.md",
            "\\\\server\\share\\x.md",
            "file:///etc/passwd",
            "/docs/a.md",
        ):
            found = errors({"docs/a.md": f"[x](<{target}>)"})
            self.assertEqual(len(found), 1, target)
            self.assertIn("absolute path", found[0], target)

    def test_absolute_definition_and_image(self) -> None:
        found = errors({"a.md": "![i](/home/x/i.png)\n\n[r]: /home/x/r.md\n"})
        self.assertEqual(len(found), 2)

    def test_absolute_path_in_code_is_ignored(self) -> None:
        self.assertEqual(errors({"a.md": "`[x](/home/denis/x.md)`\n\n```\n[x](C:\\x)\n```\n"}), [])


class Slugs(unittest.TestCase):
    def test_github_rules(self) -> None:
        cases = {
            "# Привіт, світе!": ["привіт-світе"],
            "## Що таке «Vilna»?": ["що-таке-vilna"],
            "# Getting Started": ["getting-started"],
            "# Крок 1. Встановлення (Linux)": ["крок-1-встановлення-linux"],
            "# A — B": ["a--b"],
            "# snake_case & kebab-case": ["snake_case--kebab-case"],
            "# Ґанок, Єнот, Їжак": ["ґанок-єнот-їжак"],
            "# 🦉 Сова": ["-сова"],
            "# Title ##": ["title"],
            "# `GET /v1/status/<id>` — збій": ["get-v1statusid--збій"],
            "# [Посилання](x.md) *жирне* _курсив_": ["посилання-жирне-курсив"],
            "Заголовок\n=========": ["заголовок"],
            "Другий рівень\n-------------": ["другий-рівень"],
        }
        for text, want in cases.items():
            self.assertEqual(cm.heading_slugs(text), want, text)

    def test_repeated_headings_get_numbers(self) -> None:
        text = "# Розділ\n## Розділ\n### Розділ\n# Розділ-1\n"
        self.assertEqual(cm.heading_slugs(text), ["розділ", "розділ-1", "розділ-2", "розділ-1-1"])

    def test_headings_in_code_and_comments_do_not_count(self) -> None:
        text = "```\n# Не заголовок\n```\n<!--\n# Теж ні\n-->\n# Справжній\n"
        self.assertEqual(cm.heading_slugs(text), ["справжній"])

    def test_lines_that_are_not_headings(self) -> None:
        text = "#hashtag\n    # indented code\n| a | b |\n| --- | --- |\n- item\n---\n"
        self.assertEqual(cm.heading_slugs(text), [])


class Anchors(unittest.TestCase):
    def test_anchor_in_the_same_file(self) -> None:
        text = "# Вступ\n\n## Що далі?\n\n[1](#вступ) [2](#що-далі) [3](#Вступ)\n"
        self.assertEqual(errors({"a.md": text}), [])

    def test_missing_anchor_in_the_same_file(self) -> None:
        found = errors({"a.md": "# Вступ\n\n[1](#немає-такого)\n"})
        self.assertEqual(
            found, ["a.md: line 3: anchor '#немає-такого' of '#немає-такого' not found in a.md"]
        )

    def test_anchor_in_another_file(self) -> None:
        files = {
            "a.md": "[1](docs/b.md#другий-розділ)",
            "docs/b.md": "# Перший\n## Другий розділ\n",
        }
        self.assertEqual(errors(files), [])

    def test_missing_anchor_in_another_file(self) -> None:
        files = {"a.md": "[1](docs/b.md#третій)", "docs/b.md": "# Перший\n"}
        found = errors(files)
        self.assertEqual(len(found), 1)
        self.assertIn("not found in docs/b.md", found[0])

    def test_the_anchor_belongs_to_the_target_file_not_to_the_source(self) -> None:
        files = {"a.md": "# Тільки тут\n\n[1](b.md#тільки-тут)", "b.md": "# Інше\n"}
        self.assertEqual(len(errors(files)), 1)

    def test_percent_encoded_cyrillic_anchor(self) -> None:
        files = {"a.md": "[1](#%D0%B2%D1%81%D1%82%D1%83%D0%BF)", "b.md": ""}
        files["a.md"] += "\n\n# Вступ\n"
        self.assertEqual(errors(files), [])

    def test_repeated_headings_anchors(self) -> None:
        text = "# Приклад\n# Приклад\n# Приклад\n\n"
        text += "[1](#приклад) [2](#приклад-1) [3](#приклад-2) [4](#приклад-3)\n"
        found = errors({"a.md": text})
        self.assertEqual(len(found), 1)
        self.assertIn("#приклад-3", found[0])

    def test_anchor_on_a_definition_and_on_an_image_link(self) -> None:
        found = errors({"a.md": "# Так\n\n[r]: #ні\n[ok]: #так\n"})
        self.assertEqual(len(found), 1)
        self.assertIn("line 3", found[0])

    def test_explicit_html_anchor(self) -> None:
        text = '<a name="старт"></a>\n<h2 id="end">x</h2>\n\n[1](#старт) [2](#end)\n'
        self.assertEqual(errors({"a.md": text}), [])

    def test_line_anchors_are_accepted(self) -> None:
        files = {"a.md": "[1](b.md#L10) [2](b.md#L10-L20) [3](c.py#L5)", "b.md": "", "c.py": ""}
        self.assertEqual(errors(files), [])

    def test_anchor_on_a_file_that_is_not_markdown_or_on_a_folder(self) -> None:
        files = {"a.md": "[1](c.py#anything) [2](docs/#anything)", "c.py": "", "docs/x.txt": ""}
        self.assertEqual(errors(files), [])

    def test_anchor_of_a_missing_file_reports_the_file(self) -> None:
        found = errors({"a.md": "[1](nope.md#x)"})
        self.assertEqual(len(found), 1)
        self.assertIn("does not exist", found[0])

    def test_empty_anchor_is_ignored(self) -> None:
        self.assertEqual(errors({"a.md": "[1](#) [2](b.md#)", "b.md": ""}), [])

    def test_anchor_is_counted(self) -> None:
        result = check({"a.md": "# А\n[1](#а) [2](b.md)", "b.md": ""})
        self.assertEqual((result.links, result.local, result.anchors), (2, 2, 1))


class LegacyAndMain(unittest.TestCase):
    def test_known_old_broken_link_is_a_warning(self) -> None:
        files = {"docs/a.md": "[1](gone.md)\n[2](other-gone.md)\n"}
        with mock.patch.object(cm, "LEGACY", frozenset({("docs/a.md", "gone.md")})):
            result = check(files)
        self.assertEqual([p.warning for p in result.problems], [True, False])
        self.assertEqual(
            str(result.problems[0]),
            "docs/a.md: line 1: warning: 'gone.md' does not exist "
            "(no tracked file or folder docs/gone.md)",
        )
        self.assertEqual((result.errors, result.warnings), (1, 1))

    def run_main(self, files: dict[str, str], args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in files.items():
                path = Path(tmp) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            with (
                mock.patch.object(cm, "ROOT", Path(tmp)),
                mock.patch.object(cm, "tracked_files", return_value=sorted(files)),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = cm.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_exit_0_for_good_files(self) -> None:
        code, out, err = self.run_main({"a.md": "# Так\n[1](#так) [2](https://example.org/)"}, [])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("1 Markdown files checked: 2 links", out)
        self.assertIn("(1 external, not fetched; 1 local, 1 with an anchor)", out)
        self.assertIn("0 errors, 0 warnings", out)

    def test_exit_1_and_message_for_broken_links(self) -> None:
        code, out, err = self.run_main({"a.md": "x\n[1](no.md)"}, [])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("a.md: line 2: 'no.md' does not exist"), err)
        self.assertIn("1 errors, 0 warnings", out)

    def test_only_the_named_files_are_checked(self) -> None:
        files = {"a.md": "[1](no.md)", "b.md": "[2](a.md)"}
        self.assertEqual(self.run_main(files, ["b.md"])[0], 0)
        self.assertEqual(self.run_main(files, ["a.md"])[0], 1)

    def test_exit_2_for_wrong_calls(self) -> None:
        self.assertEqual(self.run_main({"a.md": ""}, ["--bogus"])[0], 2)
        self.assertEqual(self.run_main({"a.md": ""}, ["nope.md"])[0], 2)
        self.assertEqual(self.run_main({"a.md": ""}, ["a.txt"])[0], 2)
        self.assertEqual(self.run_main({"x.txt": ""}, [])[0], 2)

    def test_help_is_not_an_error(self) -> None:
        self.assertEqual(self.run_main({"a.md": ""}, ["--help"])[0], 0)

    def test_a_file_that_is_not_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.md").write_bytes(b"[1](b.md) \xff\xfe")
            result = cm.check_repo(Path(tmp), ["a.md"])
        self.assertEqual(
            [str(p) for p in result.problems], ["a.md: line 1: the file is not valid UTF-8"]
        )


class RealRepo(unittest.TestCase):
    def test_tracked_markdown_has_no_errors(self) -> None:
        tracked = cm.tracked_files(ROOT)
        result = cm.check_repo(ROOT, tracked)
        self.assertGreater(result.files, 0)
        self.assertEqual([str(p) for p in result.problems if not p.warning], [])


if __name__ == "__main__":
    unittest.main()
