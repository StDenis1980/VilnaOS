#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_svg.py (the check of the tracked SVG files).

Every test writes small files into a temporary folder and checks them the way
``main()`` does; there is a test for every rule and one for the good file.

Run: python3 tools/test_check_svg.py
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

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_svg as cs  # noqa: E402  (after sys.path.insert above)

FILE = "icon.svg"
SYMBOLIC = "icons/symbolic/app-symbolic.svg"
NS = 'xmlns="http://www.w3.org/2000/svg"'
XLINK = 'xmlns:xlink="http://www.w3.org/1999/xlink"'
INK = 'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"'
SODI = 'xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"'
# A 1x1 PNG (pixel), base64, used only in data:-URI tests. Not a secret.
PNG_DATA = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAh"
    "KmMIQAAAABJRU5ErkJggg=="
)
JPEG_DATA = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBD"
# The tests never decode these payloads; only the media type is checked.


def svg(
    body: str = '<circle id="dot" cx="8" cy="8" r="4" fill="#13213A"/>',
    *,
    viewbox: str | None = "0 0 16 16",
    width: str | None = "16",
    height: str | None = "16",
    xmlns: bool = True,
    extra: str = "",
    root: str = "svg",
) -> str:
    """A good SVG; the keyword arguments break it in one place."""
    attrs = ['xmlns="http://www.w3.org/2000/svg"'] if xmlns else []
    if viewbox is not None:
        attrs.append(f'viewBox="{viewbox}"')
    if width is not None:
        attrs.append(f'width="{width}"')
    if height is not None:
        attrs.append(f'height="{height}"')
    if extra:
        attrs.append(extra)
    return f"<{root} {' '.join(attrs)}>{body}</{root}>\n"


def problems_of(text: str, name: str = FILE) -> list[cs.Problem]:
    """Write the text to a temporary file and check it the way main() does."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return cs.check_files(Path(tmp), [name])


def errors_of(text: str, name: str = FILE) -> list[str]:
    return [str(p) for p in problems_of(text, name) if not p.warning]


def warnings_of(text: str, name: str = FILE) -> list[str]:
    return [str(p) for p in problems_of(text, name) if p.warning]


class GoodFile(unittest.TestCase):
    def test_good_file_has_no_problems(self) -> None:
        self.assertEqual(problems_of(svg()), [])

    def test_internal_url_and_href_to_existing_id(self) -> None:
        text = svg(
            '<defs><linearGradient id="g"><stop offset="0" stop-color="#13213A"/>'
            "</linearGradient></defs>"
            '<circle fill="url(#g)" cx="8" cy="8" r="4"/>'
            '<use href="#g"/>',
        )
        self.assertEqual(problems_of(text), [])

    def test_quoted_url_reference(self) -> None:
        text = svg(
            '<defs><clipPath id="c"><rect width="8" height="8"/></clipPath></defs>'
            """<circle clip-path="url('#c')" cx="8" cy="8" r="4"/>"""
            """<rect clip-path='url("#c")' width="4" height="4"/>""",
        )
        self.assertEqual(problems_of(text), [])

    def test_messages_start_with_the_path(self) -> None:
        errors = errors_of("<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>")
        self.assertTrue(errors)
        self.assertTrue(all(e.startswith(f"{FILE}:") for e in errors))


class DoctypeAndEntity(unittest.TestCase):
    def test_doctype_is_rejected_before_parsing(self) -> None:
        text = (
            '<!DOCTYPE svg [<!ENTITY x "y">]>\n'
            f'<svg {NS} viewBox="0 0 16 16"><title>&x;</title></svg>\n'
        )
        errors = errors_of(text)
        self.assertEqual(len(errors), 1)
        self.assertIn("rejected before parsing", errors[0])
        self.assertIn("<!DOCTYPE", errors[0])

    def test_entity_without_doctype_is_rejected(self) -> None:
        text = f'<!ENTITY boom "x">\n<svg {NS} viewBox="0 0 16 16"/>\n'
        errors = errors_of(text)
        self.assertEqual(len(errors), 1)
        self.assertIn("<!ENTITY", errors[0])

    def test_lowercase_doctype_is_rejected(self) -> None:
        text = f'<!doctype svg>\n<svg {NS} viewBox="0 0 16 16"/>\n'
        self.assertTrue(any("<!DOCTYPE" in e for e in errors_of(text)))

    def test_entity_inside_a_comment_is_still_rejected(self) -> None:
        # The scan is textual, before any XML parse, so a comment is not a hiding place.
        text = f'<!-- <!ENTITY x "y"> -->\n<svg {NS} viewBox="0 0 16 16"/>\n'
        self.assertEqual(len(errors_of(text)), 1)


class RootAndViewBox(unittest.TestCase):
    def test_not_well_formed_xml(self) -> None:
        errors = errors_of(f'<svg {NS} viewBox="0 0 16 16"><circle></svg>')
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith(f"{FILE}: not well-formed XML"))

    def test_root_must_be_svg(self) -> None:
        errors = errors_of(svg(root="div"))
        self.assertEqual(len(errors), 1)
        self.assertIn("not <svg> in the SVG namespace", errors[0])

    def test_root_without_svg_namespace(self) -> None:
        errors = errors_of(svg(xmlns=False))
        self.assertEqual(len(errors), 1)
        self.assertIn("without a namespace", errors[0])

    def test_root_in_the_wrong_namespace(self) -> None:
        text = '<svg xmlns="http://www.w3.org/1999/xhtml" viewBox="0 0 16 16"/>\n'
        errors = errors_of(text)
        self.assertEqual(len(errors), 1)
        self.assertIn("1999/xhtml", errors[0])

    def test_only_width_and_height_is_a_warning(self) -> None:
        text = svg(viewbox=None, width="16", height="16")
        self.assertEqual(errors_of(text), [])
        self.assertEqual(warnings_of(text), [f"{FILE}: warning: no viewBox (only width/height)"])

    def test_only_width_is_a_warning(self) -> None:
        text = svg(viewbox=None, width="16", height=None)
        self.assertEqual(warnings_of(text), [f"{FILE}: warning: no viewBox (only width/height)"])

    def test_missing_viewbox_and_size_is_an_error(self) -> None:
        text = svg(viewbox=None, width=None, height=None)
        self.assertEqual(errors_of(text), [f"{FILE}: missing viewBox"])


class DangerousContent(unittest.TestCase):
    def test_script_element(self) -> None:
        text = svg('<script>alert(1)</script>')
        self.assertIn(f"{FILE}: contains a <script> element", errors_of(text))

    def test_script_is_caught_in_any_namespace(self) -> None:
        text = svg('<script xmlns="http://www.w3.org/1999/xhtml"></script>')
        self.assertIn(f"{FILE}: contains a <script> element", errors_of(text))

    def test_onclick_attribute(self) -> None:
        text = svg('<circle cx="8" cy="8" r="4" onclick="alert(1)"/>')
        self.assertEqual(errors_of(text), [f"{FILE}: event handler attribute onclick="])

    def test_on_attribute_is_case_insensitive(self) -> None:
        text = svg('<circle cx="8" cy="8" r="4" onLoad="go()"/>')
        self.assertEqual(errors_of(text), [f"{FILE}: event handler attribute onLoad="])

    def test_opacity_is_not_an_event_handler(self) -> None:
        text = svg('<circle cx="8" cy="8" r="4" opacity="0.5" fill="#13213A"/>')
        self.assertEqual(problems_of(text), [])

    def test_foreign_object(self) -> None:
        text = svg(
            '<foreignObject width="16" height="16">'
            '<p xmlns="http://www.w3.org/1999/xhtml">x</p></foreignObject>'
        )
        self.assertIn(f"{FILE}: contains a <foreignObject> element", errors_of(text))

    def test_href_http(self) -> None:
        text = svg('<image href="http://example.com/x.png" width="16" height="16"/>')
        self.assertEqual(errors_of(text), [f"{FILE}: href points at a http: URI"])

    def test_href_https(self) -> None:
        text = svg('<a href="https://example.com/"><circle cx="8" cy="8" r="4"/></a>')
        self.assertEqual(errors_of(text), [f"{FILE}: href points at a https: URI"])

    def test_href_file(self) -> None:
        text = svg('<image href="file:///etc/passwd" width="16" height="16"/>')
        # file: is the scheme error; the path is not also repeated.
        self.assertEqual(errors_of(text), [f"{FILE}: href points at a file: URI"])

    def test_href_absolute_unix_path(self) -> None:
        text = svg('<image href="/etc/passwd" width="16" height="16"/>')
        errors = errors_of(text)
        self.assertIn(f"{FILE}: href points at an absolute path", errors)
        # The same value is also an absolute path, so the personal-data rule may fire too.
        self.assertTrue(all("passwd" not in e for e in errors))

    def test_href_absolute_windows_path(self) -> None:
        text = svg('<image href="C:\\Users\\x\\a.png" width="16" height="16"/>')
        errors = errors_of(text)
        self.assertTrue(errors)
        # Either the href rule or the personal-data rule (or both) must fire.
        self.assertTrue(
            any("absolute path" in e or "personal data" in e for e in errors)
        )

    def test_href_javascript(self) -> None:
        text = svg('<a href="javascript:alert(1)"><circle cx="8" cy="8" r="4"/></a>')
        self.assertEqual(errors_of(text), [f"{FILE}: href points at a javascript: URI"])

    def test_xlink_href_https(self) -> None:
        text = svg(
            '<use xlink:href="https://example.com/icon.svg#x"/>',
            extra=XLINK,
        )
        self.assertEqual(errors_of(text), [f"{FILE}: xlink:href points at a https: URI"])

    def test_relative_href_is_allowed(self) -> None:
        text = svg('<image href="images/dot.png" width="16" height="16"/>')
        self.assertEqual(problems_of(text), [])

    def test_import_in_style(self) -> None:
        text = svg('<style>@import url("https://example.com/x.css");</style>')
        errors = errors_of(text)
        self.assertIn(f"{FILE}: <style> contains @import", errors)

    def test_import_is_case_insensitive(self) -> None:
        text = svg("<style>@Import 'theme.css';</style>")
        self.assertIn(f"{FILE}: <style> contains @import", errors_of(text))

    def test_url_https_in_css(self) -> None:
        text = svg('<circle style="fill:url(https://example.com/x.png)" cx="8" cy="8" r="4"/>')
        self.assertIn(
            f"{FILE}: url() in attribute style points at a https: URI",
            errors_of(text),
        )


class DataUris(unittest.TestCase):
    def test_data_png_is_a_warning(self) -> None:
        text = svg(f'<image href="{PNG_DATA}" width="16" height="16"/>')
        self.assertEqual(errors_of(text), [])
        warnings = warnings_of(text)
        self.assertEqual(len(warnings), 1)
        self.assertIn("data: URI of type image/png", warnings[0])

    def test_data_jpeg_is_a_warning(self) -> None:
        text = svg(f'<image href="{JPEG_DATA}" width="16" height="16"/>')
        self.assertEqual(errors_of(text), [])
        self.assertIn("image/jpeg", warnings_of(text)[0])

    def test_data_svg_is_an_error(self) -> None:
        text = svg('<image href="data:image/svg+xml;base64,PHN2Zy8+" width="16" height="16"/>')
        errors = errors_of(text)
        self.assertEqual(len(errors), 1)
        self.assertIn("image/svg+xml", errors[0])
        self.assertIn("only image/png and image/jpeg are allowed", errors[0])

    def test_data_in_css_url(self) -> None:
        text = svg(
            '<circle style="fill:url(data:text/html;base64,PGh0bWw+)" cx="8" cy="8" r="4"/>'
        )
        errors = errors_of(text)
        self.assertTrue(any("text/html" in e for e in errors))


class IdsAndRefs(unittest.TestCase):
    def test_duplicate_ids(self) -> None:
        text = svg(
            '<rect id="dup" width="4" height="4"/><circle id="dup" cx="8" cy="8" r="2"/>'
        )
        self.assertEqual(errors_of(text), [f"{FILE}: id 'dup' is used 2 times"])

    def test_missing_url_ref(self) -> None:
        text = svg('<circle fill="url(#missing)" cx="8" cy="8" r="4"/>')
        self.assertEqual(
            errors_of(text),
            [f"{FILE}: url(#missing) refers to no element with that id"],
        )

    def test_missing_href_fragment(self) -> None:
        text = svg('<use href="#nope"/>')
        self.assertEqual(
            errors_of(text),
            [f"{FILE}: href='#nope' refers to no element with that id"],
        )

    def test_missing_xlink_href_fragment(self) -> None:
        text = svg('<use xlink:href="#nope"/>', extra=XLINK)
        self.assertEqual(
            errors_of(text),
            [f"{FILE}: xlink:href='#nope' refers to no element with that id"],
        )

    def test_url_ref_inside_style_element(self) -> None:
        text = svg('<style>.x { fill: url(#missing); }</style><circle class="x"/>')
        self.assertEqual(
            errors_of(text),
            [f"{FILE}: url(#missing) refers to no element with that id"],
        )

    def test_same_missing_id_is_reported_once(self) -> None:
        text = svg(
            '<circle fill="url(#gone)" cx="4" cy="4" r="2"/>'
            '<rect fill="url(#gone)" width="4" height="4"/>'
        )
        self.assertEqual(
            errors_of(text),
            [f"{FILE}: url(#gone) refers to no element with that id"],
        )


class PersonalDataAndEditors(unittest.TestCase):
    def test_home_path_in_attribute(self) -> None:
        text = svg('<title sodipodi:docname="/home/someone/icon.svg">x</title>', extra=SODI)
        errors = errors_of(text)
        self.assertEqual(len(errors), 1)
        self.assertIn("personal data", errors[0])
        self.assertNotIn("someone", errors[0])
        self.assertNotIn("/home/", errors[0])

    def test_windows_users_path_in_attribute(self) -> None:
        text = svg(
            '<image inkscape:export-filename="C:\\Users\\Alex\\out.png" width="16" height="16"/>',
            extra=INK,
        )
        errors = errors_of(text)
        self.assertEqual(len(errors), 1)
        self.assertIn("personal data", errors[0])
        self.assertNotIn("Alex", errors[0])

    def test_home_path_in_text(self) -> None:
        text = svg("<title>exported from /home/someone/art</title>")
        errors = errors_of(text)
        self.assertEqual(
            errors,
            [f"{FILE}: text content contains an absolute or home path (personal data)"],
        )
        self.assertNotIn("someone", errors[0])

    def test_home_path_in_comment(self) -> None:
        text = f'<!-- saved as /home/someone/icon.svg -->\n{svg()}'
        errors = errors_of(text)
        self.assertEqual(
            errors, [f"{FILE}: a comment contains an absolute or home path (personal data)"]
        )

    def test_macos_users_path(self) -> None:
        text = svg('<desc>file was /Users/someone/Desktop/icon.svg</desc>')
        self.assertTrue(any("personal data" in e for e in errors_of(text)))

    def test_other_inkscape_attribute_is_a_warning(self) -> None:
        text = svg('<circle inkscape:label="dot" cx="8" cy="8" r="4" fill="#13213A"/>', extra=INK)
        self.assertEqual(errors_of(text), [])
        self.assertEqual(
            warnings_of(text),
            [f"{FILE}: warning: attribute inkscape:label is editor metadata"],
        )

    def test_sodipodi_element_is_a_warning(self) -> None:
        text = svg('<sodipodi:namedview/>', extra=SODI)
        self.assertEqual(errors_of(text), [])
        self.assertIn("element sodipodi:namedview is editor metadata", warnings_of(text)[0])

    def test_path_in_inkscape_filename_is_an_error_not_just_a_warning(self) -> None:
        text = svg(
            '<circle inkscape:export-filename="/home/someone/out.png" cx="8" cy="8" r="1"/>',
            extra=INK,
        )
        self.assertTrue(any("personal data" in e for e in errors_of(text)))
        # The leftover attribute is still noted as editor metadata.
        self.assertTrue(any("inkscape:export-filename" in w for w in warnings_of(text)))


class FileSize(unittest.TestCase):
    def test_file_over_200_kib_is_a_warning(self) -> None:
        padding = "x" * (cs.SIZE_LIMIT + 1)
        text = svg(f"<!-- {padding} -->")
        self.assertGreater(len(text.encode("utf-8")), cs.SIZE_LIMIT)
        self.assertEqual(errors_of(text), [])
        warnings = warnings_of(text)
        self.assertEqual(len(warnings), 1)
        self.assertIn("over 204800 bytes", warnings[0])

    def test_file_at_the_limit_is_silent(self) -> None:
        problems = cs.check_text(FILE, svg(), size=cs.SIZE_LIMIT)
        self.assertEqual(problems, [])
        problems = cs.check_text(FILE, svg(), size=cs.SIZE_LIMIT + 1)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].warning)


class SymbolicColours(unittest.TestCase):
    def test_one_colour_plus_none_is_fine(self) -> None:
        text = svg(
            '<g fill="#13213A"><path d="M2 8 L14 8" fill="none" stroke="#13213A"/></g>'
        )
        self.assertEqual(problems_of(text, SYMBOLIC), [])

    def test_current_color_is_not_counted(self) -> None:
        text = svg('<path d="M2 8 L14 8" fill="currentColor" stroke="none"/>')
        self.assertEqual(problems_of(text, SYMBOLIC), [])

    def test_two_colours_are_a_warning(self) -> None:
        text = svg(
            '<path d="M2 2h4" fill="#13213A"/><path d="M8 2h4" fill="#FFFFFF"/>'
        )
        self.assertEqual(errors_of(text, SYMBOLIC), [])
        warnings = warnings_of(text, SYMBOLIC)
        self.assertEqual(len(warnings), 1)
        self.assertIn("more than one fill/stroke colour", warnings[0])
        self.assertIn("#13213a", warnings[0])
        self.assertIn("#ffffff", warnings[0])

    def test_hex_forms_of_the_same_colour_count_as_one(self) -> None:
        text = svg(
            '<path d="M2 2h4" fill="#13213A"/><path d="M8 2h4" fill="#13213a"/>'
        )
        self.assertEqual(problems_of(text, SYMBOLIC), [])

    def test_colours_in_style_attribute(self) -> None:
        text = svg(
            '<path d="M2 2h4" style="fill:#13213A"/><path d="M8 2h4" style="fill:#00ff00"/>'
        )
        self.assertTrue(
            any("more than one" in w for w in warnings_of(text, SYMBOLIC))
        )

    def test_not_under_symbolic_dir_is_not_checked(self) -> None:
        text = svg(
            '<path d="M2 2h4" fill="#13213A"/><path d="M8 2h4" fill="#FFFFFF"/>'
        )
        # The file is named *-symbolic.svg but the path has no /symbolic/ segment.
        self.assertEqual(problems_of(text, "docs/design/blavet/svg/mark-symbolic.svg"), [])

    def test_short_and_long_hex_are_the_same_colour(self) -> None:
        text = svg('<path d="M2 2h4" fill="#abc"/><path d="M8 2h4" fill="#aabbcc"/>')
        self.assertEqual(problems_of(text, SYMBOLIC), [])


class FilesAndMain(unittest.TestCase):
    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            problems = cs.check_files(Path(tmp), ["nope.svg"])
        self.assertEqual(len(problems), 1)
        self.assertTrue(str(problems[0]).startswith("nope.svg: cannot read the file"))

    def test_file_that_is_not_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / FILE).write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg">\xff</svg>')
            problems = cs.check_files(Path(tmp), [FILE])
        self.assertEqual([str(p) for p in problems], [f"{FILE}: the file is not valid UTF-8"])

    def test_each_file_is_checked_on_its_own(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / FILE).write_text(svg(), encoding="utf-8")
            other = "bad.svg"
            (Path(tmp) / other).write_text(svg("<script/>"), encoding="utf-8")
            problems = cs.check_files(Path(tmp), [FILE, other])
        self.assertEqual({p.path for p in problems if not p.warning}, {other})

    def run_main(self, args: list[str], cwd: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        previous = Path.cwd()
        os.chdir(cwd)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cs.main(args)
        finally:
            os.chdir(previous)
        return code, out.getvalue(), err.getvalue()

    def test_main_exit_codes_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / FILE
            good.write_text(svg(), encoding="utf-8")
            code, out, err = self.run_main([FILE], Path(tmp))
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(out, "1 SVG files checked: 0 errors, 0 warnings\n")

            good.write_text(svg("<script/>"), encoding="utf-8")
            code, out, err = self.run_main([FILE], Path(tmp))
            self.assertEqual(code, 1)
            self.assertEqual(err, f"{FILE}: contains a <script> element\n")
            self.assertIn("1 errors", out)

    def test_main_warnings_do_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / FILE).write_text(svg(viewbox=None), encoding="utf-8")
            code, out, err = self.run_main([FILE], Path(tmp))
        self.assertEqual(code, 0)
        self.assertIn(f"{FILE}: warning: no viewBox (only width/height)", err)
        self.assertIn("0 errors, 1 warnings", out)

    def test_main_wrong_call_is_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_main(["--bogus"], Path(tmp))
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)

    def test_main_help_is_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_main(["--help"], Path(tmp))[0], 0)


class TrackedFiles(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_only_tracked_svg_files_are_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "sub").mkdir()
            tracked = ["sub/icon.svg", "other.svg", "readme.txt"]
            for rel in [*tracked, "untracked.svg"]:
                (root / rel).write_text("<svg/>", encoding="utf-8")
            subprocess.run(["git", "add", *tracked], cwd=root, check=True)
            self.assertEqual(cs.tracked_svg_files(root), ["other.svg", "sub/icon.svg"])


if __name__ == "__main__":
    unittest.main()
