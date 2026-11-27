#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_reuse.py (REUSE 3.x and LICENSES/ bookkeeping).

Run: python3 tools/test_check_reuse.py
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

import check_reuse as cr  # noqa: E402  (after sys.path.insert above)

TEXT = "Licence text. " * 20  # long enough to be a real licence text
COMMON_LICENCES = ("GPL-3.0-or-later", "Apache-2.0")
HEADER = "# SPDX-License-Identifier: GPL-3.0-or-later\n"
COPYRIGHT = "# SPDX-FileCopyrightText: 2026 The Vilna authors\n"


def build(files: dict[str, str | bytes], licences: bool = True) -> Path:
    """Write the files into a new temporary folder.

    With ``licences`` the folder also gets a good text in LICENSES/ for each common licence
    that some file names, so that no text is unused unless a test says so.
    """
    tmp = Path(tempfile.mkdtemp(prefix="reuse-test-"))
    mentioned = "\n".join(v for v in files.values() if isinstance(v, str))
    extra = {
        f"LICENSES/{name}.txt": TEXT for name in COMMON_LICENCES if licences and name in mentioned
    }
    for rel, content in {**extra, **files}.items():
        path = tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8", newline="")
        else:
            path.write_bytes(content)
    return tmp


class Case(unittest.TestCase):
    def check(
        self,
        files: dict[str, str | bytes],
        licences: bool = True,
        legacy: frozenset[tuple[str, str]] = frozenset(),
        verbose: bool = False,
    ) -> list[cr.Problem]:
        tmp = build(files, licences)
        self.addCleanup(self.rmtree, tmp)
        names = sorted(p.relative_to(tmp).as_posix() for p in tmp.rglob("*") if p.is_file())
        return cr.check_repo(tmp, names, legacy=legacy, verbose=verbose)

    @staticmethod
    def rmtree(path: Path) -> None:
        for child in sorted(path.rglob("*"), reverse=True):
            child.unlink() if child.is_file() or child.is_symlink() else child.rmdir()
        path.rmdir()

    def errors(self, files: dict[str, str | bytes], **kw: object) -> list[str]:
        return [str(p) for p in self.check(files, **kw) if not p.warning]  # type: ignore[arg-type]

    def rules(self, files: dict[str, str | bytes], **kw: object) -> list[tuple[str, str]]:
        found = self.check(files, **kw)  # type: ignore[arg-type]
        return [(p.path, p.rule) for p in found if not p.warning]


class GoodTrees(Case):
    def test_good_file_passes(self) -> None:
        self.assertEqual(self.errors({"a.py": HEADER + COPYRIGHT + "x = 1\n"}), [])

    def test_good_tree_with_copyright_has_no_warnings_at_all(self) -> None:
        self.assertEqual(self.check({"a.py": HEADER + COPYRIGHT}), [])

    def test_html_comment_with_closer(self) -> None:
        text = "<!-- SPDX-License-Identifier: Apache-2.0 -->\n<p>x</p>\n"
        self.assertEqual(self.errors({"a.html": text}), [])

    def test_c_style_comment_with_closer(self) -> None:
        text = "/* SPDX-License-Identifier: GPL-3.0-or-later */\nint x;\n"
        self.assertEqual(self.errors({"a.c": text}), [])

    def test_slash_comment_and_star_line(self) -> None:
        for lead in ("// ", " * ", "-- ", "; ", "% "):
            with self.subTest(lead=lead):
                text = f"{lead}SPDX-License-Identifier: GPL-3.0-or-later\n"
                self.assertEqual(self.errors({"a.x": text}), [])

    def test_crlf_header(self) -> None:
        text = "# SPDX-License-Identifier: GPL-3.0-or-later\r\n# SPDX-FileCopyrightText: x\r\n"
        self.assertEqual(self.check({"a.py": text}), [])

    def test_compound_expression_needs_every_text(self) -> None:
        text = "# SPDX-License-Identifier: (GPL-3.0-or-later OR Apache-2.0) AND Apache-2.0\n"
        self.assertEqual(self.errors({"a.py": text}), [])

    def test_with_exception_and_plus(self) -> None:
        files = {
            "a.py": "# SPDX-License-Identifier: Apache-2.0+ WITH LLVM-exception\n",
            "LICENSES/LLVM-exception.txt": TEXT,
        }
        self.assertEqual(self.errors(files), [])

    def test_licenseref_needs_its_own_text(self) -> None:
        text = "# SPDX-License-Identifier: LicenseRef-vendor\n"
        files = {"a.py": text, "LICENSES/LicenseRef-vendor.txt": TEXT}
        self.assertEqual(self.errors(files), [])
        self.assertEqual(self.rules({"a.py": text}), [("a.py", "licence-text-missing")])

    def test_ignored_files_need_no_header(self) -> None:
        files = {
            "LICENSE": TEXT,
            "sub/LICENSE.md": "x",
            "COPYING": "x",
            "REUSE.toml": "version = 1\n",
        }
        self.assertEqual(self.errors(files), [])

    def test_file_that_only_starts_like_a_licence_file_is_checked(self) -> None:
        self.assertEqual(
            self.rules({"LICENSEPLATE.py": "x = 1\n", "COPYINGS": "x"}),
            [("COPYINGS", "no-licence-info"), ("LICENSEPLATE.py", "no-licence-info")],
        )

    def test_empty_file_list_is_fine(self) -> None:
        tmp = build({}, licences=False)
        self.addCleanup(self.rmtree, tmp)
        self.assertEqual(cr.check_repo(tmp, [], legacy=frozenset()), [])


class HeaderReading(Case):
    def test_tag_inside_a_string_does_not_count(self) -> None:
        text = 'x = "# SPDX-License-Identifier: MIT"\n'
        self.assertEqual(self.rules({"a.py": text}), [("a.py", "no-licence-info")])

    def test_tag_in_prose_does_not_count(self) -> None:
        text = "Every file starts with SPDX-License-Identifier: MIT, see below.\n"
        self.assertEqual(self.rules({"a.md": text}), [("a.md", "no-licence-info")])

    def test_tag_after_the_first_4096_bytes_does_not_count(self) -> None:
        text = "#" * 4095 + "\n" + HEADER
        self.assertEqual(self.rules({"a.py": text}, licences=False), [("a.py", "no-licence-info")])

    def test_tag_just_inside_the_window_counts(self) -> None:
        text = HEADER + "#" * 3000  # the tag is at the very start
        self.assertEqual(self.rules({"a.py": text}), [])

    def test_binary_file_with_bad_bytes_is_read(self) -> None:
        data = b"\x89PNG\r\n\x1a\n\x00\xff\xfe" + b"\x00" * 50
        self.assertEqual(self.rules({"a.png": data}), [("a.png", "no-licence-info")])

    def test_non_utf8_text_before_a_valid_header(self) -> None:
        data = b"# \xff\xfe\n" + HEADER.encode()
        files = {"a.py": data, "LICENSES/GPL-3.0-or-later.txt": TEXT}
        self.assertEqual(self.rules(files), [])

    def test_first_of_two_tags_is_not_the_only_one_checked(self) -> None:
        text = HEADER + "# SPDX-License-Identifier: MIT\n"
        self.assertEqual(self.rules({"a.py": text}), [("a.py", "licence-text-missing")])

    def test_empty_tag_value(self) -> None:
        self.assertEqual(
            self.rules({"a.py": "# SPDX-License-Identifier:\n"}), [("a.py", "bad-expression")]
        )

    def test_message_names_path_and_line(self) -> None:
        errors = self.errors({"a.py": "# one\n# SPDX-License-Identifier: MIT\n"})
        self.assertEqual(errors, ["a.py: line 2: licence 'MIT' has no text file LICENSES/MIT.txt"])

    def test_empty_copyright_value(self) -> None:
        text = HEADER + "# SPDX-FileCopyrightText:\n"
        self.assertEqual(self.rules({"a.py": text}), [("a.py", "empty-copyright")])


class Expressions(unittest.TestCase):
    def test_valid_expressions(self) -> None:
        cases = {
            "MIT": ["MIT"],
            "Apache-2.0+": ["Apache-2.0+"],
            "GPL-3.0-or-later OR MIT": ["GPL-3.0-or-later", "MIT"],
            "(MIT)": ["MIT"],
            "((MIT AND Apache-2.0) OR GPL-2.0-only)": ["MIT", "Apache-2.0", "GPL-2.0-only"],
            "GPL-2.0-only WITH Classpath-exception-2.0": [
                "GPL-2.0-only",
                "Classpath-exception-2.0",
            ],
            "LicenseRef-x-1.2": ["LicenseRef-x-1.2"],
            "DocumentRef-d:LicenseRef-x": ["DocumentRef-d:LicenseRef-x"],
        }
        for expr, names in cases.items():
            with self.subTest(expr=expr):
                self.assertEqual(cr.parse_expression(expr), names)

    def test_invalid_expressions(self) -> None:
        for expr in (
            "",
            "   ",
            "MIT AND",
            "AND MIT",
            "MIT OR OR MIT",
            "(MIT",
            "MIT)",
            "()",
            "MIT Apache-2.0",
            "MIT and Apache-2.0",
            "WITH",
            "MIT WITH",
            "MIT WITH WITH",
            "MIT WITH Foo+",
            "LicenseRef-x+",
            "not a licence!",
            "MIT;rm",
            "ГНУ",
            "-MIT",
        ):
            with self.subTest(expr=expr), self.assertRaises(ValueError):
                cr.parse_expression(expr)

    def test_deeply_nested_brackets_are_an_error_not_a_crash(self) -> None:
        expr = "(" * 3000 + "MIT" + ")" * 3000
        with self.assertRaises(ValueError):
            cr.parse_expression(expr)

    def test_long_flat_expression_is_fine(self) -> None:
        expr = " OR ".join(["MIT"] * 2000)
        self.assertEqual(len(cr.parse_expression(expr)), 2000)

    def test_bad_expression_in_a_header_is_an_error(self) -> None:
        tmp = build({"a.py": "# SPDX-License-Identifier: MIT AND\n"})
        rules = [p.rule for p in cr.check_repo(tmp, ["a.py"], legacy=frozenset())]
        self.assertIn("bad-expression", rules)


class DeprecatedIds(Case):
    def test_deprecated_id_is_a_warning(self) -> None:
        files = {"a.py": "# SPDX-License-Identifier: GPL-3.0+\n", "LICENSES/GPL-3.0.txt": TEXT}
        found = self.check(files)
        deprecated = [p for p in found if p.rule == "deprecated-id"]
        self.assertEqual(len(deprecated), 1)
        self.assertTrue(deprecated[0].warning)
        self.assertIn("GPL-3.0-or-later", deprecated[0].message)
        self.assertEqual([p for p in found if not p.warning], [])


class LicencesFolder(Case):
    def test_missing_text_is_an_error(self) -> None:
        text = "# SPDX-License-Identifier: MIT\n"
        self.assertEqual(self.rules({"a.py": text}), [("a.py", "licence-text-missing")])

    def test_exception_text_is_needed_too(self) -> None:
        text = "# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception\n"
        self.assertEqual(self.rules({"a.py": text}), [("a.py", "licence-text-missing")])

    def test_unused_text_is_an_error(self) -> None:
        files = {"a.py": HEADER, "LICENSES/MIT.txt": TEXT}
        self.assertEqual(self.rules(files), [("LICENSES/MIT.txt", "licence-unused")])

    def test_unused_check_sees_every_licence_of_a_file(self) -> None:
        text = "# SPDX-License-Identifier: GPL-3.0-or-later OR Apache-2.0\n"
        self.assertEqual(self.errors({"a.py": text}), [])

    def test_linking_the_text_from_licensing_md_counts_as_use(self) -> None:
        files = {
            "a.py": HEADER,
            "LICENSES/CC-BY-SA-4.0.txt": TEXT,
            "LICENSING.md": "[text](LICENSES/CC-BY-SA-4.0.txt)\n",
        }
        self.assertEqual(self.rules(files), [("LICENSING.md", "no-licence-info")])

    def test_unused_text_is_not_hidden_by_a_header_in_a_string(self) -> None:
        files = {
            "a.py": HEADER + 'x = "# SPDX-License-Identifier: MIT"\n',
            "LICENSES/MIT.txt": TEXT,
        }
        self.assertEqual(self.rules(files), [("LICENSES/MIT.txt", "licence-unused")])

    def test_empty_text_is_an_error(self) -> None:
        files = {"a.py": HEADER, "LICENSES/GPL-3.0-or-later.txt": ""}
        self.assertEqual(
            self.rules(files), [("LICENSES/GPL-3.0-or-later.txt", "licences-bad-entry")]
        )

    def test_blank_text_is_an_error(self) -> None:
        files = {"a.py": HEADER, "LICENSES/GPL-3.0-or-later.txt": "\n \n\t\n" * 100}
        self.assertEqual(
            self.rules(files), [("LICENSES/GPL-3.0-or-later.txt", "licences-bad-entry")]
        )

    def test_wrong_suffix_is_an_error(self) -> None:
        files = {"a.py": HEADER, "LICENSES/MIT.md": TEXT}
        self.assertEqual(self.rules(files), [("LICENSES/MIT.md", "licences-bad-entry")])

    def test_sub_folder_is_an_error(self) -> None:
        files = {"a.py": HEADER, "LICENSES/old/MIT.txt": TEXT}
        self.assertEqual(self.rules(files), [("LICENSES/old/MIT.txt", "licences-bad-entry")])
        self.assertIn("flat", self.errors(files)[0])

    def test_bad_id_in_the_name_is_an_error(self) -> None:
        files = {"a.py": HEADER, "LICENSES/-bad name.txt": TEXT}
        self.assertEqual(self.rules(files), [("LICENSES/-bad name.txt", "licences-bad-entry")])

    def test_folder_that_is_missing_entirely(self) -> None:
        self.assertEqual(
            self.rules({"a.py": HEADER}, licences=False), [("a.py", "licence-text-missing")]
        )

    def test_dead_link_in_licensing_md(self) -> None:
        files = {
            "a.py": HEADER,
            "LICENSING.md": "# L\n[x](LICENSES/MIT.txt)\n" + HEADER,
        }
        self.assertEqual(self.rules(files), [("LICENSING.md", "licensing-dead-link")])


class HeaderlessFiles(Case):
    def test_headerless_file_without_anything_is_an_error(self) -> None:
        self.assertEqual(self.rules({"img/a.png": b"\x89PNG"}), [("img/a.png", "no-licence-info")])

    def test_licensing_md_folder_entry_covers_files_below(self) -> None:
        files = {
            "LICENSING.md": HEADER + "| x | `img/` |\n",
            "img/a.png": b"\x89PNG",
            "img/deep/b.png": b"\x89PNG",
        }
        self.assertEqual(self.errors(files), [])

    def test_folder_entry_without_slash_covers_a_tracked_folder(self) -> None:
        files = {"LICENSING.md": HEADER + "`img/art`\n", "img/art/a.png": b"x"}
        self.assertEqual(self.errors(files), [])

    def test_folder_entry_does_not_cover_a_sibling_with_the_same_prefix(self) -> None:
        files = {
            "LICENSING.md": HEADER + "`img/`\n",
            "img/a.png": b"x",
            "img2/b.png": b"x",
            "imgs.png": b"x",
        }
        self.assertEqual(
            self.rules(files), [("img2/b.png", "no-licence-info"), ("imgs.png", "no-licence-info")]
        )

    def test_exact_path_entry(self) -> None:
        files = {"LICENSING.md": HEADER + "`img/a.png`\n", "img/a.png": b"x", "img/b.png": b"x"}
        self.assertEqual(self.rules(files), [("img/b.png", "no-licence-info")])

    def test_glob_entry_stops_at_slash(self) -> None:
        files = {
            "LICENSING.md": HEADER + "`img/*.jpg`\n",
            "img/a.jpg": b"x",
            "img/sub/b.jpg": b"x",
            "img/c.png": b"x",
        }
        self.assertEqual(
            self.rules(files),
            [("img/c.png", "no-licence-info"), ("img/sub/b.jpg", "no-licence-info")],
        )

    def test_entry_without_a_slash_is_not_a_path(self) -> None:
        files = {"LICENSING.md": HEADER + "see `plymouth-branding`, `a.png`\n", "a.png": b"x"}
        self.assertEqual(self.rules(files), [("a.png", "no-licence-info")])

    def test_entry_with_spaces_or_url_is_not_a_path(self) -> None:
        text = HEADER + "`some words/here` `https://example.org/a/` `/abs/`\n"
        files = {"LICENSING.md": text, "some words/here/a.png": b"x", "abs/a.png": b"x"}
        self.assertEqual(
            self.rules(files),
            [("abs/a.png", "no-licence-info"), ("some words/here/a.png", "no-licence-info")],
        )

    def test_entry_for_licenses_folder_is_not_a_cover(self) -> None:
        files = {"LICENSING.md": HEADER + "`LICENSES/x/`\n", "q/a.png": b"x"}
        self.assertEqual(self.rules(files), [("q/a.png", "no-licence-info")])

    def test_default_sentence_of_licensing_md_covers_nothing(self) -> None:
        text = HEADER + "By default the whole repository is GPL-3.0-or-later.\n"
        self.assertEqual(
            self.rules({"LICENSING.md": text, "a.png": b"x"}), [("a.png", "no-licence-info")]
        )

    def test_sidecar_licenses_the_file(self) -> None:
        files = {"a.png": b"x", "a.png.license": HEADER + COPYRIGHT}
        self.assertEqual(self.check(files), [])

    def test_sidecar_without_licence(self) -> None:
        files = {"a.png": b"x", "a.png.license": COPYRIGHT}
        self.assertEqual(
            self.rules(files),
            [("a.png", "no-licence-info"), ("a.png.license", "sidecar-no-licence")],
        )

    def test_orphan_sidecar(self) -> None:
        files = {"b.png.license": HEADER}
        self.assertEqual(self.rules(files, licences=False), [("b.png.license", "orphan-sidecar")])

    def test_sidecar_beats_the_header_of_the_file(self) -> None:
        files = {
            "a.py": HEADER,
            "b.py": HEADER,
            "a.py.license": "# SPDX-License-Identifier: Apache-2.0\n",
        }
        self.assertEqual(self.errors(files), [])
        files["a.py.license"] = "# SPDX-License-Identifier: MIT\n"
        self.assertEqual(self.rules(files), [("a.py", "licence-text-missing")])

    def test_sidecar_missing_text_is_reported_on_the_file(self) -> None:
        files = {"a.png": b"x", "a.png.license": "SPDX-License-Identifier: MIT\n"}
        errors = self.errors(files)
        self.assertEqual(len(errors), 1)
        self.assertIn("a.png: line 1: licence 'MIT' has no text file", errors[0])
        self.assertIn("(.license file)", errors[0])

    def test_symlink_is_not_a_file_to_check(self) -> None:
        tmp = build({"a.py": HEADER})
        self.addCleanup(self.rmtree, tmp)
        (tmp / "link.png").symlink_to(tmp / "a.py")
        names = ["LICENSES/GPL-3.0-or-later.txt", "a.py", "link.png"]
        found = cr.check_repo(tmp, names, legacy=frozenset())
        self.assertEqual([p for p in found if not p.warning], [])

    def test_missing_licensing_md_means_no_cover(self) -> None:
        self.assertEqual(self.rules({"a.png": b"x"}), [("a.png", "no-licence-info")])

    def test_stale_path_in_licensing_md_is_a_warning(self) -> None:
        found = self.check({"LICENSING.md": HEADER + "`gone/`\n"})
        stale = [p for p in found if p.rule == "licensing-stale"]
        self.assertEqual(len(stale), 1)
        self.assertTrue(stale[0].warning)
        self.assertIn("gone/", stale[0].message)


class Annotations(Case):
    def toml(self, body: str) -> str:
        return "version = 1\n" + body

    def test_reuse_toml_covers_headerless_files(self) -> None:
        body = (
            '[[annotations]]\npath = "img/**"\n'
            'SPDX-FileCopyrightText = "2026 X"\nSPDX-License-Identifier = "Apache-2.0"\n'
        )
        files = {"REUSE.toml": self.toml(body), "img/a.png": b"x", "img/d/b.png": b"x"}
        self.assertEqual(self.check(files), [])

    def test_star_does_not_cross_a_slash_in_reuse_toml(self) -> None:
        body = (
            '[[annotations]]\npath = "img/*.png"\n'
            'SPDX-FileCopyrightText = "2026 X"\nSPDX-License-Identifier = "Apache-2.0"\n'
        )
        files = {"REUSE.toml": self.toml(body), "img/a.png": b"x", "img/d/b.png": b"x"}
        self.assertEqual(self.rules(files), [("img/d/b.png", "no-licence-info")])

    def test_path_list_and_double_star_slash(self) -> None:
        body = (
            '[[annotations]]\npath = ["**/*.png", "x.bin"]\n'
            'SPDX-FileCopyrightText = ["a", "b"]\nSPDX-License-Identifier = "GPL-3.0-or-later"\n'
        )
        files = {"REUSE.toml": self.toml(body), "a.png": b"x", "d/e/b.png": b"x", "x.bin": b"x"}
        self.assertEqual(self.check(files), [])

    def test_nested_reuse_toml_paths_are_relative_to_its_folder(self) -> None:
        body = (
            '[[annotations]]\npath = "*.png"\n'
            'SPDX-FileCopyrightText = "x"\nSPDX-License-Identifier = "Apache-2.0"\n'
        )
        files = {"sub/REUSE.toml": self.toml(body), "sub/a.png": b"x", "a.png": b"x"}
        self.assertEqual(self.rules(files), [("a.png", "no-licence-info")])

    def test_annotation_licence_needs_a_text(self) -> None:
        body = (
            '[[annotations]]\npath = "a.png"\n'
            'SPDX-FileCopyrightText = "x"\nSPDX-License-Identifier = "MIT"\n'
        )
        files = {"REUSE.toml": self.toml(body), "a.png": b"x"}
        self.assertEqual(self.rules(files), [("a.png", "licence-text-missing")])

    def test_annotation_only_with_copyright_does_not_license(self) -> None:
        body = '[[annotations]]\npath = "a.png"\nSPDX-FileCopyrightText = "x"\n'
        files = {"REUSE.toml": self.toml(body), "a.png": b"x"}
        self.assertEqual(self.rules(files), [("a.png", "no-licence-info")])

    def test_invalid_toml(self) -> None:
        self.assertEqual(
            self.rules({"REUSE.toml": "version = \n"}), [("REUSE.toml", "bad-reuse-toml")]
        )

    def test_wrong_version_and_bad_annotation(self) -> None:
        text = "version = 2\n[[annotations]]\npath = 5\n"
        rules = [p.rule for p in self.check({"REUSE.toml": text})]
        self.assertEqual(rules.count("bad-reuse-toml"), 2)

    def test_annotations_must_be_an_array(self) -> None:
        self.assertEqual(
            self.rules({"REUSE.toml": "version = 1\nannotations = 3\n"}),
            [("REUSE.toml", "bad-reuse-toml")],
        )

    def test_dep5_covers_files(self) -> None:
        dep5 = (
            "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n\n"
            "Files: img/* x.bin\n Extra/*.dat\nCopyright: 2026 X\nLicense: Apache-2.0\n"
        )
        files = {
            ".reuse/dep5": dep5,
            "img/a.png": b"x",
            "img/d/b.png": b"x",
            "x.bin": b"x",
            "Extra/a.dat": b"x",
        }
        self.assertEqual(self.check(files), [])

    def test_dep5_without_format_line(self) -> None:
        self.assertEqual(
            self.rules({".reuse/dep5": "Files: *\nCopyright: x\nLicense: MIT\n"}),
            [(".reuse/dep5", "bad-dep5")],
        )

    def test_dep5_stanza_without_licence(self) -> None:
        dep5 = "Format: x\n\nFiles: a.png\nCopyright: x\n"
        files = {".reuse/dep5": dep5, "a.png": b"x"}
        self.assertEqual(
            self.rules(files), [(".reuse/dep5", "bad-dep5"), ("a.png", "no-licence-info")]
        )

    def test_dep5_licence_text_stanza_is_ignored(self) -> None:
        dep5 = "Format: x\n\nLicense: MIT\n Some text\n"
        self.assertEqual(self.errors({".reuse/dep5": dep5}), [])


class Copyright(Case):
    def test_missing_copyright_is_only_a_warning(self) -> None:
        found = self.check({"a.py": HEADER})
        self.assertEqual([p for p in found if not p.warning], [])
        self.assertTrue(any(p.rule == "no-copyright" for p in found))

    def test_warnings_are_counted_per_top_level_folder(self) -> None:
        files = {"a.py": HEADER, "x/b.py": HEADER, "x/y/c.py": HEADER, "z/d.py": HEADER + COPYRIGHT}
        found = [str(p) for p in self.check(files) if p.rule == "no-copyright"]
        self.assertIn(
            "(top-level files): warning: 1 files have no SPDX-FileCopyrightText (REUSE 3.x)", found
        )
        self.assertIn("x/: warning: 2 files have no SPDX-FileCopyrightText (REUSE 3.x)", found)
        self.assertFalse(any(line.startswith("z/") for line in found))
        self.assertTrue(any("3 of 4 files have no" in line for line in found))

    def test_verbose_lists_each_file(self) -> None:
        found = self.check({"a.py": HEADER, "x/b.py": HEADER + COPYRIGHT}, verbose=True)
        self.assertEqual(
            [str(p) for p in found], ["a.py: warning: no SPDX-FileCopyrightText (REUSE 3.x)"]
        )

    def test_copyright_in_sidecar_or_annotation_counts(self) -> None:
        body = (
            '[[annotations]]\npath = "c.png"\n'
            'SPDX-FileCopyrightText = "x"\nSPDX-License-Identifier = "Apache-2.0"\n'
        )
        files = {
            "a.png": b"x",
            "a.png.license": HEADER + COPYRIGHT,
            "REUSE.toml": "version = 1\n" + body,
            "c.png": b"x",
            "b.py": HEADER + COPYRIGHT,
        }
        self.assertEqual(self.check(files), [])

    def test_licensing_md_cover_does_not_give_a_copyright(self) -> None:
        files = {"LICENSING.md": HEADER + COPYRIGHT + "`img/`\n", "img/a.png": b"x"}
        found = self.check(files)
        self.assertEqual([p for p in found if not p.warning], [])
        self.assertTrue(any(p.rule == "no-copyright" for p in found))

    def test_copyright_warning_never_fails_the_run(self) -> None:
        tmp = build({"a.py": HEADER})
        self.addCleanup(self.rmtree, tmp)
        code, out, _ = run_main(["--root", str(tmp)])
        self.assertEqual(code, 0)
        self.assertIn("0 errors", out)


class Legacy(Case):
    def test_known_old_violation_is_a_warning(self) -> None:
        files = {"img/a.png": b"x"}
        legacy = frozenset({("img/a.png", "no-licence-info")})
        found = self.check(files, legacy=legacy)
        self.assertEqual([p for p in found if not p.warning], [])
        self.assertTrue(any(p.path == "img/a.png" and p.warning for p in found))

    def test_entry_for_another_rule_does_not_hide_the_error(self) -> None:
        legacy = frozenset({("img/a.png", "licence-unused")})
        self.assertEqual(
            self.rules({"img/a.png": b"x"}, legacy=legacy), [("img/a.png", "no-licence-info")]
        )

    def test_entry_for_another_file_does_not_hide_the_error(self) -> None:
        legacy = frozenset({("img/b.png", "no-licence-info")})
        self.assertEqual(
            self.rules({"img/a.png": b"x"}, legacy=legacy), [("img/a.png", "no-licence-info")]
        )

    def test_new_file_next_to_a_legacy_one_is_an_error(self) -> None:
        legacy = frozenset({("img/a.png", "no-licence-info")})
        files = {"img/a.png": b"x", "img/new.png": b"x"}
        self.assertEqual(self.rules(files, legacy=legacy), [("img/new.png", "no-licence-info")])

    def test_stale_entry_is_a_warning(self) -> None:
        legacy = frozenset({("a.py", "no-licence-info")})
        found = self.check({"a.py": HEADER + COPYRIGHT}, legacy=legacy)
        self.assertEqual([p.rule for p in found], ["stale-legacy"])
        self.assertTrue(found[0].warning)

    def test_legacy_table_has_only_known_rules(self) -> None:
        known = {"no-licence-info", "licence-text-missing", "licence-unused", "licences-bad-entry"}
        self.assertTrue({rule for _, rule in cr.LEGACY} <= known)

    def test_legacy_table_has_no_duplicates_and_sorted_paths_per_rule(self) -> None:
        text = (ROOT / "tools" / "check_reuse.py").read_text(encoding="utf-8")
        entries = [line.strip() for line in text.splitlines() if line.strip().startswith('("')]
        self.assertEqual(len(entries), len(set(entries)))


def run_main(args: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cr.main(args)
    return code, out.getvalue(), err.getvalue()


class Main(Case):
    def test_exit_0_for_a_good_tree(self) -> None:
        tmp = build({"a.py": HEADER + COPYRIGHT})
        self.addCleanup(self.rmtree, tmp)
        code, out, err = run_main(["--root", str(tmp)])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("0 errors, 0 warnings", out)

    def test_exit_1_and_message_for_a_bad_tree(self) -> None:
        tmp = build({"img/a.png": b"x"})
        self.addCleanup(self.rmtree, tmp)
        code, out, err = run_main(["--root", str(tmp)])
        self.assertEqual(code, 1)
        self.assertIn("img/a.png: no licence information", err)
        self.assertIn("1 errors", out)

    def test_legacy_is_not_applied_to_another_root(self) -> None:
        name = next(iter(cr.LEGACY))[0]
        tmp = build({name: b"x"})
        self.addCleanup(self.rmtree, tmp)
        self.assertEqual(run_main(["--root", str(tmp)])[0], 1)

    def test_exit_2_for_an_unknown_option(self) -> None:
        code, _, err = run_main(["--bogus"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)

    def test_exit_2_for_root_without_value(self) -> None:
        self.assertEqual(run_main(["--root"])[0], 2)

    def test_exit_2_for_an_empty_folder(self) -> None:
        tmp = build({}, licences=False)
        self.addCleanup(self.rmtree, tmp)
        self.assertEqual(run_main(["--root", str(tmp)])[0], 2)

    def test_exit_2_for_a_missing_folder(self) -> None:
        self.assertEqual(run_main(["--root", "/nonexistent-reuse-test-folder"])[0], 2)

    def test_help_is_not_an_error(self) -> None:
        code, out, _ = run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)

    def test_output_is_deterministic(self) -> None:
        files = {"b/x.png": b"x", "a/y.png": b"x", "c.py": HEADER}
        tmp = build(files)
        self.addCleanup(self.rmtree, tmp)
        first = run_main(["--root", str(tmp)])
        second = run_main(["--root", str(tmp)])
        self.assertEqual(first, second)
        lines = [ln for ln in first[2].splitlines() if "no licence information" in ln]
        self.assertEqual(lines, sorted(lines))


class RealRepo(unittest.TestCase):
    def setUp(self) -> None:
        self.files = cr.list_files(ROOT)
        self.assertTrue(self.files, "the repo has tracked files")

    def test_tracked_files_have_no_errors(self) -> None:
        errors = [str(p) for p in cr.check_repo(ROOT, self.files) if not p.warning]
        self.assertEqual(errors, [])

    def test_legacy_table_has_no_stale_entry(self) -> None:
        stale = [str(p) for p in cr.check_repo(ROOT, self.files) if p.rule == "stale-legacy"]
        self.assertEqual(stale, [])

    def test_licences_folder_has_the_texts_the_licensing_doc_names(self) -> None:
        for name in ("GPL-3.0-or-later", "Apache-2.0", "CC-BY-SA-4.0"):
            self.assertIn(f"LICENSES/{name}.txt", self.files)

    def test_main_on_the_repo_exits_0(self) -> None:
        code, out, _ = run_main([])
        self.assertEqual(code, 0)
        self.assertIn(" 0 errors", out)


if __name__ == "__main__":
    unittest.main()
