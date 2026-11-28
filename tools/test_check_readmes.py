#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_readmes.py (the three README files say the same thing).

Run: python3 tools/test_check_readmes.py
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_readmes as cr  # noqa: E402  (after sys.path.insert above)

SWITCH = {
    "README.md": "**English** · [Українська](README.uk.md) · [Español](README.es.md)",
    "README.uk.md": "[English](README.md) · **Українська** · [Español](README.es.md)",
    "README.es.md": "[English](README.md) · [Українська](README.uk.md) · **Español**",
}
WORDS = {"README.md": "Words", "README.uk.md": "Слова", "README.es.md": "Palabras"}
SUFFIX = {"README.md": "", "README.uk.md": ".uk", "README.es.md": ".es"}

TEMPLATE = """\
<p align="center">
  <img src="docs/logo.svg" alt="Vilna" width="180">
</p>

# Vilna

{switch}

{words}: [`herkos/docs/permissions.md`](herkos/docs/permissions.md) and
[guide](docs/getting-started{suffix}.md).

## Try

```sh
# {words} comment
git clone https://example.invalid/VilnaOS.git
cd VilnaOS/gloxi
uv run gloxi
```

## Layout

| Folder | What |
| --- | --- |
| [`gloxi/`](gloxi/) | {words} |

### More

See [roadmap](docs/roadmap.md).
"""


def make(name: str, **extra: str) -> str:
    return TEMPLATE.format(switch=SWITCH[name], words=WORDS[name], suffix=SUFFIX[name], **extra)


def good() -> dict[str, str]:
    return {name: make(name) for name in cr.FILES}


EXTRA_FILES = (
    "docs/getting-started.md",
    "docs/getting-started.uk.md",
    "docs/getting-started.es.md",
    "docs/roadmap.md",
    "docs/logo.svg",
    "herkos/docs/permissions.md",
    "gloxi/x.py",
)


def build(root: Path, files: dict[str, str | bytes], extra: tuple[str, ...] = EXTRA_FILES) -> None:
    for rel in extra:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")


def run(files: dict[str, str | bytes], extra: tuple[str, ...] = EXTRA_FILES) -> cr.Result:
    with tempfile.TemporaryDirectory() as tmp:
        build(Path(tmp), files, extra)
        return cr.check_repo(Path(tmp))


def errors(files: dict[str, str | bytes], extra: tuple[str, ...] = EXTRA_FILES) -> list[str]:
    return [str(p) for p in run(files, extra).problems if not p.warning]


def edit(name: str, old: str, new: str, count: int = 1) -> dict[str, str]:
    """The good set with ``old`` replaced in one file."""
    files = good()
    assert old in files[name], old
    files[name] = files[name].replace(old, new, count)
    return files


def has(problems: list[str], *parts: str) -> bool:
    return any(all(part in p for part in parts) for p in problems)


class Good(unittest.TestCase):
    def test_matching_set_has_no_problems(self) -> None:
        result = run(good())
        self.assertEqual(result.problems, [])
        self.assertEqual(result.files, 3)
        self.assertEqual((result.headings, result.blocks), (12, 3))

    def test_language_suffix_is_ignored(self) -> None:
        # README.uk.md links getting-started.uk.md and README.md links getting-started.md
        self.assertEqual(errors(good()), [])

    def test_translated_comment_in_shell_block_is_ignored(self) -> None:
        self.assertEqual(errors(edit("README.uk.md", "# Слова comment", "# переклад")), [])

    def test_blank_line_in_shell_block_is_ignored(self) -> None:
        self.assertEqual(errors(edit("README.es.md", "cd VilnaOS", "\ncd VilnaOS")), [])

    def test_anchor_may_differ_between_languages(self) -> None:
        files = edit("README.md", "docs/roadmap.md", "docs/roadmap.md#plan")
        files["README.uk.md"] = files["README.uk.md"].replace(
            "docs/roadmap.md", "docs/roadmap.md#план"
        )
        self.assertEqual(errors(files), [])

    def test_external_links_may_differ(self) -> None:
        files = edit("README.md", "See [roadmap]", "See [site](https://a.invalid/) [roadmap]")
        self.assertEqual(errors(files), [])

    def test_trailing_whitespace_and_final_newline_do_not_matter(self) -> None:
        files = good()
        files["README.es.md"] = (
            files["README.es.md"].replace("uv run gloxi", "uv run gloxi   ").rstrip()
        )
        self.assertEqual(errors(files), [])

    def test_setext_heading_equals_atx_heading(self) -> None:
        files = edit("README.uk.md", "## Try\n", "Try\n---\n")
        self.assertEqual(errors(files), [])


class Headings(unittest.TestCase):
    def test_missing_heading(self) -> None:
        found = errors(edit("README.uk.md", "### More\n", ""))
        self.assertTrue(has(found, "README.uk.md", "3 headings, README.md has 4"), found)
        self.assertTrue(has(found, "missing heading", "'More'"), found)

    def test_extra_heading_has_its_line(self) -> None:
        found = errors(edit("README.es.md", "See [roadmap]", "## Extra\nSee [roadmap]"))
        self.assertTrue(has(found, "README.es.md: line 29", "'Extra' is not in README.md"), found)

    def test_wrong_level_has_line_and_other_line(self) -> None:
        found = errors(edit("README.uk.md", "### More", "## More"))
        self.assertTrue(has(found, "README.uk.md: line 27", "level 2", "README.md line 27"), found)

    def test_heading_in_code_block_is_not_a_heading(self) -> None:
        files = edit("README.uk.md", "uv run gloxi\n", "uv run gloxi\n# not a heading\n")
        self.assertEqual(errors(files), [])

    def test_heading_in_comment_is_not_a_heading(self) -> None:
        files = edit("README.uk.md", "## Try\n", "<!--\n## Hidden\n-->\n## Try\n")
        self.assertEqual(errors(files), [])

    def test_hash_without_space_is_not_a_heading(self) -> None:
        files = edit("README.uk.md", "Слова: [", "#hashtag Слова: [")
        self.assertEqual(errors(files), [])

    def test_closing_hashes_are_ignored(self) -> None:
        files = edit("README.uk.md", "## Try\n", "## Try ##\n")
        self.assertEqual(errors(files), [])

    def test_indented_four_spaces_is_code_not_heading(self) -> None:
        files = edit("README.uk.md", "## Try\n", "## Try\n\n    ## code\n")
        self.assertEqual(errors(files), [])

    def test_table_delimiter_is_not_setext(self) -> None:
        text = "| a | b |\n| --- | --- |\n"
        self.assertEqual(cr.parse("README.md", text).headings, [])

    def test_setext_levels(self) -> None:
        doc = cr.parse("README.md", "One\n===\n\nTwo\n---\n")
        self.assertEqual([(h.level, h.text) for h in doc.headings], [(1, "One"), (2, "Two")])

    def test_list_item_before_dashes_is_not_setext(self) -> None:
        self.assertEqual(cr.parse("README.md", "- item\n---\n").headings, [])


class Links(unittest.TestCase):
    def test_missing_link_names_the_reference_line(self) -> None:
        found = errors(edit("README.uk.md", "[`gloxi/`](gloxi/)", "`gloxi/`"))
        self.assertTrue(has(found, "README.uk.md: no link to gloxi", "README.md line 25"), found)

    def test_extra_link_has_its_line(self) -> None:
        found = errors(edit("README.es.md", "See [roadmap]", "See [a](docs/extra.md) [roadmap]"))
        self.assertTrue(has(found, "README.es.md: line 29", "docs/extra.md", "not in"), found)

    def test_different_target(self) -> None:
        found = errors(edit("README.uk.md", "](docs/roadmap.md)", "](docs/other.md)"))
        self.assertTrue(has(found, "no link to docs/roadmap.md"), found)
        self.assertTrue(has(found, "link to docs/other.md is not in"), found)

    def test_image_and_html_attributes_are_links(self) -> None:
        found = errors(edit("README.uk.md", 'src="docs/logo.svg"', 'src="docs/other.svg"'))
        self.assertTrue(has(found, "docs/logo.svg"), found)

    def test_srcset_and_href_are_links(self) -> None:
        text = '<source srcset="a/one.svg 1x, b/two.svg 2x"><a href="c/three.md">x</a>'
        self.assertEqual(
            [lk.target for lk in cr.parse("README.md", text).links],
            ["a/one.svg", "b/two.svg", "c/three.md"],
        )

    def test_reference_definition_is_a_link(self) -> None:
        text = "[x][ref]\n\n[ref]: docs/a.md\n"
        self.assertEqual([lk.target for lk in cr.parse("README.md", text).links], ["docs/a.md"])

    def test_normalisation(self) -> None:
        text = "[a](./docs//a.md?x=1#top) [b](<my file.md>) [c](docs/a%20b.md) [d](/abs.md)"
        self.assertEqual(
            [lk.target for lk in cr.parse("README.md", text).links],
            ["docs/a.md", "my file.md", "docs/a b.md", "abs.md"],
        )

    def test_not_local(self) -> None:
        text = "[a](https://x.invalid/a.md) [b](mailto:a@b.invalid) [c](#top) [d](//h.invalid/x)"
        self.assertEqual(cr.parse("README.md", text).links, [])

    def test_link_in_code_is_not_a_link(self) -> None:
        text = "`[a](x.md)` and\n```\n[b](y.md)\n```\n[c](z.md)\n<!-- [d](w.md) -->"
        self.assertEqual([lk.target for lk in cr.parse("README.md", text).links], ["z.md"])

    def test_link_text_with_code_is_a_link(self) -> None:
        self.assertEqual([lk.target for lk in cr.parse("README.md", "[`d/`](d/)").links], ["d"])

    def test_title_and_parentheses(self) -> None:
        text = '[a](x.md "Title") [b](a_(b).md)'
        self.assertEqual(
            [lk.target for lk in cr.parse("README.md", text).links], ["x.md", "a_(b).md"]
        )

    def test_link_line_numbers(self) -> None:
        doc = cr.parse("README.md", "a\n\n[x](one.md)\n\n\n[y](two.md)\n")
        self.assertEqual([(lk.line, lk.target) for lk in doc.links], [(3, "one.md"), (6, "two.md")])

    def test_link_to_parent_folder_is_kept_outside(self) -> None:
        self.assertEqual(
            [lk.target for lk in cr.parse("README.md", "[a](../x.md)").links], ["../x.md"]
        )


class WrongLanguage(unittest.TestCase):
    def test_uk_links_english_guide(self) -> None:
        found = errors(edit("README.uk.md", "getting-started.uk.md", "getting-started.md"))
        self.assertTrue(
            has(found, "README.uk.md: line 10", "this file is in uk", "getting-started.uk.md"),
            found,
        )

    def test_english_links_spanish_guide(self) -> None:
        found = errors(edit("README.md", "getting-started.md", "getting-started.es.md"))
        self.assertTrue(
            has(found, "README.md: line 10", "this file is in en", "docs/getting-started.md"),
            found,
        )

    def test_no_complaint_when_the_page_has_no_translation(self) -> None:
        # docs/roadmap.md has no .uk variant: the Ukrainian README may link it
        self.assertEqual(errors(good()), [])

    def test_missing_translation_file_means_no_complaint(self) -> None:
        extra = tuple(e for e in EXTRA_FILES if e != "docs/getting-started.uk.md")
        files = edit("README.uk.md", "getting-started.uk.md", "getting-started.md")
        self.assertEqual(errors(files, extra), [])


class CodeBlocks(unittest.TestCase):
    def test_changed_command_has_line(self) -> None:
        found = errors(edit("README.uk.md", "cd VilnaOS/gloxi", "cd VilnaOS/herkos"))
        self.assertTrue(
            has(found, "README.uk.md: line 17", "'cd VilnaOS/herkos'", "'cd VilnaOS/gloxi'"), found
        )

    def test_missing_block(self) -> None:
        files = good()
        files["README.es.md"] = (
            files["README.es.md"].replace("```sh", "~~", 1).replace("```\n", "\n", 1)
        )
        found = errors(files)
        self.assertTrue(has(found, "README.es.md", "code blocks, README.md has"), found)

    def test_extra_block(self) -> None:
        found = errors(edit("README.uk.md", "### More\n", "### More\n\n```sh\nls\n```\n"))
        self.assertTrue(has(found, "README.uk.md", "2 code blocks, README.md has 1"), found)

    def test_missing_command_at_the_end(self) -> None:
        found = errors(edit("README.uk.md", "uv run gloxi\n", ""))
        self.assertTrue(has(found, "missing", "'uv run gloxi'", "README.md line 18"), found)

    def test_extra_command_at_the_end(self) -> None:
        found = errors(edit("README.es.md", "uv run gloxi\n", "uv run gloxi\nuv sync\n"))
        self.assertTrue(has(found, "extra command 'uv sync'"), found)

    def test_language_tag_differs(self) -> None:
        found = errors(edit("README.uk.md", "```sh", "```bash"))
        self.assertTrue(has(found, "README.uk.md: line 14", "bash", "sh"), found)

    def test_untagged_block(self) -> None:
        found = errors(edit("README.uk.md", "```sh", "```"))
        self.assertTrue(has(found, "untagged"), found)

    def test_non_shell_blocks_compare_comments_too(self) -> None:
        files = good()
        for name in ("README.md", "README.uk.md", "README.es.md"):
            files[name] += "\n```toml\n# key\na = 1\n```\n"
        files["README.uk.md"] = files["README.uk.md"].replace("# key", "# ключ")
        self.assertTrue(has(errors(files), "README.uk.md", "'# ключ'"))

    def test_comment_only_difference_in_shell_block_is_fine(self) -> None:
        self.assertEqual(errors(edit("README.md", "# Words comment", "# something else")), [])

    def test_unclosed_block_is_an_error(self) -> None:
        files = good()
        files["README.uk.md"] += "\n```sh\nls\n"
        found = errors(files)
        self.assertTrue(has(found, "README.uk.md", "not closed", "opened at line 31"), found)

    def test_longer_fence_holds_a_shorter_one(self) -> None:
        text = "````md\n```sh\nls\n```\n````\n"
        blocks = cr.parse("README.md", text).blocks
        self.assertEqual(
            [(b.lang, [t for _, t in b.lines]) for b in blocks], [("md", ["```sh", "ls", "```"])]
        )

    def test_tilde_and_indented_fences(self) -> None:
        text = "- item\n\n  ~~~sh\n  ls\n    -l\n  ~~~\n"
        block = cr.parse("README.md", text).blocks[0]
        self.assertEqual((block.lang, [t for _, t in block.lines]), ("sh", ["ls", "  -l"]))

    def test_fence_inside_comment_is_not_a_fence(self) -> None:
        text = "<!--\n```sh\n-->\ntext\n"
        self.assertEqual(cr.parse("README.md", text).blocks, [])

    def test_fence_deep_inside_a_long_comment_is_not_a_fence(self) -> None:
        text = "<!--\nx\n```sh\ny\n```\n-->\n```sh\nreal\n```\n"
        blocks = cr.parse("README.md", text).blocks
        self.assertEqual([(b.line, [t for _, t in b.lines]) for b in blocks], [(7, ["real"])])

    def test_backtick_fence_with_backtick_in_info_is_not_a_fence(self) -> None:
        self.assertEqual(cr.parse("README.md", "``` a`b\nx\n").blocks, [])

    def test_block_line_numbers(self) -> None:
        block = cr.parse("README.md", "x\n\n```sh\nfirst\n\nsecond\n```\n").blocks[0]
        self.assertEqual((block.line, block.lines), (3, ((4, "first"), (5, ""), (6, "second"))))


class Switcher(unittest.TestCase):
    def test_missing_link_to_a_language(self) -> None:
        found = errors(edit("README.uk.md", " · [Español](README.es.md)", ""))
        self.assertTrue(has(found, "README.uk.md: no link to README.es.md", "mutual"), found)

    def test_one_way_link(self) -> None:
        found = errors(edit("README.es.md", "[Українська](README.uk.md) · ", ""))
        self.assertTrue(has(found, "README.es.md: no link to README.uk.md"), found)
        self.assertFalse(has(found, "README.uk.md: no link"), found)

    def test_switcher_is_not_part_of_the_link_comparison(self) -> None:
        # a link to README.uk.md is only in README.md and README.es.md, not in README.uk.md itself
        self.assertEqual(errors(good()), [])

    def test_link_with_anchor_or_dot_slash_counts(self) -> None:
        files = edit("README.md", "(README.uk.md)", "(./README.uk.md#top)")
        self.assertEqual(errors(files), [])

    def test_switcher_in_code_does_not_count(self) -> None:
        files = edit("README.md", "[Українська](README.uk.md)", "`[Українська](README.uk.md)`")
        self.assertTrue(has(errors(files), "README.md: no link to README.uk.md"))


class Files(unittest.TestCase):
    def test_missing_translation(self) -> None:
        files = good()
        del files["README.es.md"]
        found = errors(files)
        self.assertTrue(has(found, "README.es.md: file is missing"), found)

    def test_missing_base_skips_comparison(self) -> None:
        files = good()
        del files["README.md"]
        found = errors(files)
        self.assertTrue(has(found, "README.md: file is missing"), found)
        # each translation still links README.md, so only the missing file is reported
        self.assertEqual(len(found), 1, found)

    def test_utf16(self) -> None:
        files: dict[str, str | bytes] = dict(good())
        files["README.uk.md"] = "\ufeff# Vilna\n".encode("utf-16")
        found = errors(files)
        self.assertTrue(has(found, "README.uk.md: is UTF-16"), found)

    def test_utf16_without_bom_is_not_valid_utf8_or_has_nul(self) -> None:
        files: dict[str, str | bytes] = dict(good())
        files["README.es.md"] = "# Vilna\n".encode("utf-16-le")
        self.assertTrue(has(errors(files), "README.es.md", "NUL"))

    def test_binary(self) -> None:
        files: dict[str, str | bytes] = dict(good())
        files["README.es.md"] = bytes(range(256)) * 20
        found = errors(files)
        self.assertTrue(has(found, "README.es.md", "NUL"), found)

    def test_invalid_utf8(self) -> None:
        files: dict[str, str | bytes] = dict(good())
        files["README.es.md"] = b"# Vilna\n\xff\xfe\xfa\n"
        found = errors(files)
        self.assertTrue(has(found, "README.es.md: is not valid UTF-8 (byte"), found)

    def test_bom_is_a_warning_only(self) -> None:
        files: dict[str, str | bytes] = dict(good())
        files["README.uk.md"] = b"\xef\xbb\xbf" + good()["README.uk.md"].encode()
        result = run(files)
        self.assertEqual(result.errors, 0)
        self.assertEqual(
            [str(p) for p in result.problems],
            ["README.uk.md: line 1: warning: starts with a byte order mark"],
        )

    def test_crlf_and_lone_cr(self) -> None:
        files: dict[str, str | bytes] = dict(good())
        files["README.uk.md"] = good()["README.uk.md"].replace("\n", "\r\n")
        files["README.es.md"] = good()["README.es.md"].replace("\n", "\r")
        self.assertEqual(errors(files), [])

    def test_no_final_newline(self) -> None:
        files = good()
        files["README.uk.md"] = files["README.uk.md"].rstrip("\n")
        self.assertEqual(errors(files), [])

    def test_empty_file(self) -> None:
        files = good()
        files["README.uk.md"] = ""
        found = errors(files)
        self.assertTrue(has(found, "README.uk.md: 0 headings, README.md has 4"), found)
        self.assertTrue(has(found, "README.uk.md: no link to README.md"), found)

    def test_directory_instead_of_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, {k: v for k, v in good().items() if k != "README.es.md"})
            (root / "README.es.md").mkdir()
            found = [str(p) for p in cr.check_repo(root).problems]
        self.assertTrue(has(found, "README.es.md: is not a regular file"), found)

    def test_too_big(self) -> None:
        files = good()
        files["README.es.md"] += "x" * (cr.MAX_BYTES + 1)
        self.assertTrue(has(errors(files), "README.es.md", "more than"))

    def test_symlink_outside_the_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            build(root, {k: v for k, v in good().items() if k != "README.es.md"})
            target = Path(outside) / "secret.md"
            target.write_text("# not for the check\n", encoding="utf-8")
            (root / "README.es.md").symlink_to(target)
            found = [str(p) for p in cr.check_repo(root).problems]
        self.assertTrue(has(found, "README.es.md: is a symlink that leaves the repo"), found)

    def test_symlink_to_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, {k: v for k, v in good().items() if k != "README.es.md"})
            (root / "README.es.md").symlink_to("README.es.md")
            found = [str(p) for p in cr.check_repo(root).problems]
        self.assertTrue(has(found, "README.es.md: cannot be read"), found)

    def test_symlink_inside_the_repo_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, {k: v for k, v in good().items() if k != "README.es.md"})
            (root / "docs").mkdir(exist_ok=True)
            (root / "docs" / "es.md").write_text(good()["README.es.md"], encoding="utf-8")
            (root / "README.es.md").symlink_to("docs/es.md")
            self.assertEqual(cr.check_repo(root).errors, 0)

    def test_broken_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root, {k: v for k, v in good().items() if k != "README.es.md"})
            (root / "README.es.md").symlink_to("nowhere.md")
            found = [str(p) for p in cr.check_repo(root).problems]
        self.assertTrue(has(found, "README.es.md: file is missing"), found)

    def test_names_with_spaces_and_unicode(self) -> None:
        text = "# T\n\n[a](docs/my%20file%20é.md) [b](<docs/ü ö.md>)\n"
        links = [lk.target for lk in cr.parse("README.md", text).links]
        self.assertEqual(links, ["docs/my file é.md", "docs/ü ö.md"])

    def test_root_may_have_spaces_and_unicode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "репо з пробілом"
            root.mkdir()
            build(root, good())
            self.assertEqual(cr.check_repo(root).problems, [])


class Hostile(unittest.TestCase):
    """Odd and large inputs: no traceback, linear time."""

    def assertFast(self, text: str, limit: float = 5.0) -> None:
        files = good()
        files["README.uk.md"] = text
        start = time.monotonic()
        run(files)
        self.assertLess(time.monotonic() - start, limit)

    def test_many_backticks_without_closer(self) -> None:
        self.assertFast("`" * 200_000 + "\n")

    def test_many_alternating_backtick_runs(self) -> None:
        self.assertFast("`a``b```c````" * 20_000)

    def test_many_spans_across_blank_lines(self) -> None:
        self.assertFast("`\n\n" * 50_000)

    def test_many_unclosed_comments(self) -> None:
        self.assertFast("<!-- x\n" * 50_000)

    def test_many_html_tags_without_end(self) -> None:
        self.assertFast('<a href="x" ' * 50_000)

    def test_many_brackets(self) -> None:
        self.assertFast("[" * 100_000 + "](" * 50_000)

    def test_many_links(self) -> None:
        self.assertFast("[a](b.md)\n" * 100_000)

    def test_many_headings(self) -> None:
        self.assertFast("## h\n" * 100_000)

    def test_long_line(self) -> None:
        self.assertFast("x" * 1_500_000)

    def test_many_fences(self) -> None:
        self.assertFast("```\nx\n```\n" * 50_000)

    def test_time_grows_linearly(self) -> None:
        def seconds(n: int) -> float:
            text = "# h\n\n[a](b.md) `c` text\n```sh\nls\n```\n" * n
            start = time.monotonic()
            cr.parse("README.md", text)
            return time.monotonic() - start

        seconds(500)  # warm up
        small, big = seconds(4_000), seconds(32_000)
        self.assertLess(big, max(small, 0.02) * 8 * 3)

    def test_control_characters_and_odd_whitespace(self) -> None:
        text = "# \x01\x7f\u2028\u200b h\n\n\x0b\x0c [a](b\x01.md)\n"
        doc = cr.parse("README.md", text)
        self.assertEqual(len(doc.headings), 1)

    def test_null_in_link_target_text_file(self) -> None:
        files: dict[str, str | bytes] = dict(good())
        files["README.uk.md"] = b"# a\n[x](y\0.md)\n"
        self.assertTrue(has(errors(files), "NUL"))

    def test_deeply_nested_parentheses(self) -> None:
        self.assertFast("[a](" + "(" * 50_000 + ")" * 50_000 + ")\n")

    def test_huge_file_just_below_the_limit(self) -> None:
        files = good()
        files["README.es.md"] = "text\n" * ((cr.MAX_BYTES - 10) // 5)
        start = time.monotonic()
        run(files)
        self.assertLess(time.monotonic() - start, 10.0)

    def test_no_network_and_no_subprocess(self) -> None:
        with (
            mock.patch("socket.socket", side_effect=AssertionError("network used")),
            mock.patch("subprocess.run", side_effect=AssertionError("subprocess used")),
            mock.patch("subprocess.Popen", side_effect=AssertionError("subprocess used")),
        ):
            self.assertEqual(errors(good()), [])


class Legacy(unittest.TestCase):
    def test_known_difference_is_a_warning(self) -> None:
        files = edit("README.uk.md", "### More\n", "")
        with mock.patch.object(cr, "LEGACY", frozenset({("README.uk.md", "headings")})):
            result = run(files)
        self.assertEqual(result.errors, 0)
        self.assertGreater(result.warnings, 0)
        self.assertTrue(all("warning:" in str(p) for p in result.problems))

    def test_other_rule_stays_an_error(self) -> None:
        files = edit("README.uk.md", "cd VilnaOS/gloxi", "cd x")
        with mock.patch.object(cr, "LEGACY", frozenset({("README.uk.md", "headings")})):
            self.assertGreater(run(files).errors, 0)

    def test_legacy_list_is_empty_today(self) -> None:
        self.assertEqual(cr.LEGACY, frozenset())


class Main(unittest.TestCase):
    def call(self, args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cr.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_good_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            build(Path(tmp), good())
            code, out, err = self.call(["--root", tmp])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("3 README files checked", out)
        self.assertIn("0 errors, 0 warnings", out)

    def test_bad_repo_prints_the_place_and_returns_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            build(Path(tmp), edit("README.uk.md", "cd VilnaOS/gloxi", "cd x"))
            code, out, err = self.call(["--root", tmp])
        self.assertEqual(code, 1)
        self.assertIn("README.uk.md: line 17: command is 'cd x'", err)
        self.assertIn("1 errors", out)

    def test_warnings_only_returns_0(self) -> None:
        files: dict[str, str | bytes] = dict(good())
        files["README.uk.md"] = b"\xef\xbb\xbf" + good()["README.uk.md"].encode()
        with tempfile.TemporaryDirectory() as tmp:
            build(Path(tmp), files)
            code, out, _ = self.call(["--root", tmp])
        self.assertEqual(code, 0)
        self.assertIn("0 errors, 1 warnings", out)

    def test_wrong_calls_return_2(self) -> None:
        self.assertEqual(self.call(["x"])[0], 2)
        self.assertEqual(self.call(["--root"])[0], 2)
        self.assertEqual(self.call(["--bogus", "a"])[0], 2)
        self.assertEqual(self.call(["--root", "/nonexistent/folder"])[0], 2)

    def test_empty_folder_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.call(["--root", tmp])[0], 2)

    def test_help(self) -> None:
        code, _, err = self.call(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", err)

    def test_exit_codes_are_0_1_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn(self.call(["--root", tmp])[0], (0, 1, 2))

    def test_run_as_a_script(self) -> None:
        import subprocess

        done = subprocess.run(  # noqa: S603  (the test runs the tool under test)
            [sys.executable, str(ROOT / "tools" / "check_readmes.py")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("3 README files checked", done.stdout)


@unittest.skipUnless(all((ROOT / n).is_file() for n in cr.FILES), "the real README files")
class RealRepo(unittest.TestCase):
    """The real README files: no problems today, and a change in any of them is caught."""

    def real(self) -> dict[str, str]:
        return {n: (ROOT / n).read_text(encoding="utf-8") for n in cr.FILES}

    def check(self, files: dict[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for n, text in files.items():
                (root / n).write_text(text, encoding="utf-8")
            # the files the READMEs point to are the real ones: link a copy of the tree
            for entry in ROOT.iterdir():
                if entry.name in (".git", *cr.FILES):
                    continue
                with contextlib.suppress(OSError):
                    (root / entry.name).symlink_to(entry)
            return [str(p) for p in cr.check_repo(root).problems if not p.warning]

    def test_no_problems_today(self) -> None:
        self.assertEqual(self.check(self.real()), [])
        self.assertEqual(cr.main([]), 0)

    def mutate(self, name: str, old: str, new: str) -> list[str]:
        files = self.real()
        self.assertIn(old, files[name])
        files[name] = files[name].replace(old, new, 1)
        return self.check(files)

    def test_mutation_heading_level(self) -> None:
        self.assertTrue(self.mutate("README.uk.md", "## Дорожня карта", "### Дорожня карта"))

    def test_mutation_heading_removed(self) -> None:
        self.assertTrue(self.mutate("README.es.md", "## Contribuir\n", ""))

    def test_mutation_link_target(self) -> None:
        self.assertTrue(self.mutate("README.es.md", "(docs/benchmarks.md)", "(docs/roadmap.md)"))

    def test_mutation_link_removed(self) -> None:
        self.assertTrue(self.mutate("README.uk.md", "[`sdk/`](sdk/)", "`sdk/`"))

    def test_mutation_command(self) -> None:
        self.assertTrue(self.mutate("README.uk.md", "uv sync", "uv sync --frozen"))

    def test_mutation_code_block_removed(self) -> None:
        files = self.real()
        start = files["README.es.md"].index("```sh")
        end = files["README.es.md"].index("```\n", start + 5) + 4
        files["README.es.md"] = files["README.es.md"][:start] + files["README.es.md"][end:]
        self.assertTrue(self.check(files))

    def test_mutation_language_link(self) -> None:
        self.assertTrue(self.mutate("README.md", "[Español](README.es.md)", "Español"))

    def test_mutation_guide_language(self) -> None:
        self.assertTrue(self.mutate("README.uk.md", "getting-started.uk.md", "getting-started.md"))

    def test_mutation_logo_path(self) -> None:
        self.assertTrue(
            self.mutate("README.es.md", "Vilna_logo_color.svg", "Vilna_logo_dark-bg.svg")
        )


if __name__ == "__main__":
    unittest.main()
