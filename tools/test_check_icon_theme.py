#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_icon_theme.py (the check of icon theme index.theme files).

Run: python3 tools/test_check_icon_theme.py
"""

from __future__ import annotations

import contextlib
import gzip
import io
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_icon_theme as cit  # noqa: E402  (after sys.path.insert above)

THEME = "Demo"
INDEX = "Demo/index.theme"


def svg(
    size: int = 128, width: str | None = None, height: str | None = None, viewbox: str | None = None
) -> str:
    """A minimal well-formed SVG; pass width/height/viewbox to override or use '' to omit."""
    w = f"{size}" if width is None else width
    h = f"{size}" if height is None else height
    box = f"0 0 {size} {size}" if viewbox is None else viewbox
    attrs = ['xmlns="http://www.w3.org/2000/svg"']
    if w != "":
        attrs.append(f'width="{w}"')
    if h != "":
        attrs.append(f'height="{h}"')
    if box != "":
        attrs.append(f'viewBox="{box}"')
    return f"<svg {' '.join(attrs)}></svg>\n"


def png_bytes(width: int, height: int) -> bytes:
    """A valid RGB PNG of the given pixel size."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + (b"\x00\x00\x00" * width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def index_text(
    directories: str = "scalable/places",
    sections: str | None = None,
    name: str = "Demo",
    comment: str = "A test theme",
    extra_head: str = "",
) -> str:
    if sections is None:
        sections = (
            "[scalable/places]\nContext=Places\nSize=128\nMinSize=8\nMaxSize=512\nType=Scalable\n"
        )
    return (
        f"[Icon Theme]\nName={name}\nComment={comment}\n"
        f"Inherits=hicolor\nExample=folder\nDirectories={directories}\n"
        f"{extra_head}\n{sections}"
    )


def section(rel: str, size: int, context: str, typ: str = "Scalable") -> str:
    return f"[{rel}]\nContext={context}\nSize={size}\nType={typ}\nMinSize=8\nMaxSize=512\n"


def write_theme(
    tmp: Path,
    text: str | None = None,
    icons: dict[str, str | bytes] | None = None,
    name: str = THEME,
) -> Path:
    """Write a theme under ``tmp / name`` and return the index path."""
    theme = tmp / name
    theme.mkdir(parents=True, exist_ok=True)
    index = theme / "index.theme"
    index.write_text(text if text is not None else index_text(), encoding="utf-8")
    files = icons if icons is not None else {"scalable/places/folder.svg": svg()}
    for rel, content in files.items():
        dest = theme / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            dest.write_bytes(content)
        else:
            dest.write_text(content, encoding="utf-8")
    return index


def problems_of(
    text: str | None = None,
    icons: dict[str, str | bytes] | None = None,
    name: str = THEME,
) -> list[cit.Problem]:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        write_theme(tmp, text=text, icons=icons, name=name)
        return cit.check_themes(tmp, [f"{name}/index.theme"])


def errors_of(
    text: str | None = None,
    icons: dict[str, str | bytes] | None = None,
    name: str = THEME,
) -> list[str]:
    return [str(p) for p in problems_of(text, icons, name) if not p.warning]


def warnings_of(
    text: str | None = None,
    icons: dict[str, str | bytes] | None = None,
    name: str = THEME,
) -> list[str]:
    return [str(p) for p in problems_of(text, icons, name) if p.warning]


def two_dir_index() -> str:
    dirs = "scalable/places,symbolic/status"
    sections = section("scalable/places", 128, "Places") + section("symbolic/status", 16, "Status")
    return index_text(directories=dirs, sections=sections)


class GoodThemes(unittest.TestCase):
    def test_minimal_good_theme_passes(self) -> None:
        self.assertEqual(problems_of(), [])

    def test_comments_empty_lines_and_spaces_around_equals_are_fine(self) -> None:
        text = (
            "# SPDX-License-Identifier: GPL-3.0-or-later\n\n"
            "[Icon Theme]\nName = Demo\nComment = A test theme\n"
            "Directories = scalable/places\n\n"
            "# directory section\n"
            "[scalable/places]\nContext = Places\nSize = 128\nType = Scalable\n"
        )
        self.assertEqual(problems_of(text), [])

    def test_several_directories_and_icons_pass(self) -> None:
        icons = {
            "scalable/places/folder.svg": svg(128),
            "scalable/places/user-home.svg": svg(128),
            "symbolic/status/vilna-local-symbolic.svg": svg(16),
        }
        self.assertEqual(problems_of(two_dir_index(), icons), [])

    def test_size_from_viewbox_alone_is_fine(self) -> None:
        icon = svg(width="", height="", viewbox="0 0 128 128")
        self.assertEqual(problems_of(icons={"scalable/places/folder.svg": icon}), [])

    def test_size_from_width_alone_is_fine(self) -> None:
        icon = svg(width="128", height="128", viewbox="")
        self.assertEqual(problems_of(icons={"scalable/places/folder.svg": icon}), [])

    def test_width_with_px_unit_is_fine(self) -> None:
        icon = svg(width="128px", height="128px")
        self.assertEqual(problems_of(icons={"scalable/places/folder.svg": icon}), [])

    def test_png_of_the_declared_size_is_fine(self) -> None:
        icons = {"scalable/places/folder.png": png_bytes(128, 128)}
        self.assertEqual(problems_of(icons=icons), [])

    def test_svgz_of_the_declared_size_is_fine(self) -> None:
        icons = {"scalable/places/folder.svgz": gzip.compress(svg().encode("utf-8"))}
        self.assertEqual(problems_of(icons=icons), [])

    def test_symbolic_rtl_variant_is_accepted(self) -> None:
        icons = {"symbolic/status/vilna-local-symbolic-rtl.svg": svg(16)}
        text = index_text(
            directories="symbolic/status",
            sections=section("symbolic/status", 16, "Status"),
        )
        self.assertEqual(problems_of(text, icons), [])

    def test_directory_name_ending_in_symbolic_is_treated_as_symbolic(self) -> None:
        text = index_text(
            directories="16x16-symbolic/actions",
            sections=section("16x16-symbolic/actions", 16, "Actions", typ="Fixed"),
        )
        icons = {"16x16-symbolic/actions/edit-copy-symbolic.svg": svg(16)}
        self.assertEqual(problems_of(text, icons), [])

    def test_same_name_in_different_sizes_is_fine(self) -> None:
        text = index_text(
            directories="128x128/places,64x64/places",
            sections=section("128x128/places", 128, "Places", typ="Fixed")
            + section("64x64/places", 64, "Places", typ="Fixed"),
        )
        icons = {
            "128x128/places/folder.svg": svg(128),
            "64x64/places/folder.svg": svg(64),
        }
        self.assertEqual(problems_of(text, icons), [])

    def test_same_name_in_different_contexts_is_fine(self) -> None:
        text = index_text(
            directories="scalable/places,scalable/devices",
            sections=section("scalable/places", 128, "Places")
            + section("scalable/devices", 128, "Devices"),
        )
        icons = {
            "scalable/places/folder.svg": svg(128),
            "scalable/devices/folder.svg": svg(128),
        }
        self.assertEqual(problems_of(text, icons), [])

    def test_fixed_and_threshold_types_are_allowed(self) -> None:
        text = index_text(
            directories="48x48/apps,32x32/apps",
            sections=section("48x48/apps", 48, "Applications", typ="Fixed")
            + section("32x32/apps", 32, "Applications", typ="Threshold"),
        )
        icons = {
            "48x48/apps/demo.svg": svg(48),
            "32x32/apps/demo.svg": svg(32),
        }
        self.assertEqual(problems_of(text, icons), [])

    def test_viewbox_origin_need_not_be_zero(self) -> None:
        icon = svg(viewbox="-4 -4 128 128")
        self.assertEqual(problems_of(icons={"scalable/places/folder.svg": icon}), [])

    def test_comma_separated_viewbox_is_fine(self) -> None:
        icon = svg(viewbox="0,0,128,128")
        self.assertEqual(problems_of(icons={"scalable/places/folder.svg": icon}), [])

    def test_empty_declared_directory_is_fine(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            write_theme(tmp, icons={})
            (tmp / THEME / "scalable" / "places").mkdir(parents=True, exist_ok=True)
            self.assertEqual(cit.check_themes(tmp, [INDEX]), [])


class Syntax(unittest.TestCase):
    def test_first_group_must_be_icon_theme(self) -> None:
        text = "[scalable/places]\nContext=Places\nSize=128\nType=Scalable\n\n" + index_text()
        self.assertTrue(any("first group must be [Icon Theme]" in e for e in errors_of(text)))

    def test_file_without_any_group(self) -> None:
        errors = errors_of("# only a comment\n")
        self.assertTrue(any("first group must be [Icon Theme]" in e for e in errors))

    def test_key_before_the_first_group(self) -> None:
        errors = errors_of("Name=Demo\n" + index_text())
        self.assertTrue(any("line 1: key before the first group" in e for e in errors))

    def test_line_that_is_nothing_valid(self) -> None:
        errors = errors_of(index_text() + "\n[scalable/places]\nthis is not a key")
        # The section is repeated; also an invalid line in a new broken header? The
        # file already has [scalable/places]. Append after the whole file as a bare line.
        text = index_text().rstrip() + "\nthis is not a key\n"
        errors = errors_of(text)
        self.assertTrue(any("not a comment, empty line, [Group] or Key=Value" in e for e in errors))

    def test_broken_group_header(self) -> None:
        errors = errors_of(index_text() + "\n[Broken group\n")
        self.assertTrue(any("invalid group header" in e for e in errors))

    def test_duplicate_key_in_a_group(self) -> None:
        text = index_text() + ""  # Size appears once; add a second Size in the section
        text = index_text(
            sections="[scalable/places]\nContext=Places\nSize=128\nType=Scalable\nSize=64\n"
        )
        errors = errors_of(text)
        self.assertTrue(any("duplicate key Size in [scalable/places]" in e for e in errors))

    def test_repeated_group(self) -> None:
        text = index_text() + "\n[scalable/places]\nContext=Places\nSize=128\nType=Scalable\n"
        errors = errors_of(text)
        self.assertTrue(any("group [scalable/places] repeated" in e for e in errors))

    def test_message_names_the_path_and_the_line(self) -> None:
        text = "Name=Demo\n" + index_text()
        errors = errors_of(text)
        self.assertTrue(any(e.startswith(f"{INDEX}: line 1:") for e in errors))

    def test_file_that_is_not_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            theme = tmp / THEME
            theme.mkdir()
            (theme / "index.theme").write_bytes(b"[Icon Theme]\nName=\xff\n")
            found = cit.check_themes(tmp, [INDEX])
        self.assertTrue(any("not valid UTF-8" in str(p) for p in found))

    def test_byte_order_mark_is_one_clear_error(self) -> None:
        text = "\ufeff" + index_text()
        errors = errors_of(text)
        self.assertTrue(any("byte order mark" in e for e in errors))

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            found = cit.check_themes(Path(raw), ["nope/index.theme"])
        self.assertTrue(any("cannot read the file" in str(p) for p in found))
        self.assertTrue(all(p.path.endswith("nope/index.theme") for p in found))


class RequiredKeys(unittest.TestCase):
    def test_missing_name(self) -> None:
        text = index_text().replace("Name=Demo\n", "")
        self.assertTrue(any(f"{INDEX}: [Icon Theme] missing Name" == e for e in errors_of(text)))

    def test_empty_name_is_missing(self) -> None:
        text = index_text().replace("Name=Demo", "Name=")
        self.assertTrue(any("missing Name" in e for e in errors_of(text)))

    def test_missing_comment(self) -> None:
        text = index_text().replace("Comment=A test theme\n", "")
        self.assertTrue(any("missing Comment" in e for e in errors_of(text)))

    def test_missing_directories(self) -> None:
        text = index_text().replace("Directories=scalable/places\n", "")
        self.assertTrue(any("missing Directories" in e for e in errors_of(text)))

    def test_empty_directories(self) -> None:
        text = index_text(directories="")
        # index_text will write Directories=\n
        self.assertTrue(any("Directories is empty" in e for e in errors_of(text)))

    def test_directory_listed_twice(self) -> None:
        text = index_text(directories="scalable/places,scalable/places")
        errors = errors_of(text)
        self.assertTrue(any("listed more than once in Directories" in e for e in errors))

    def test_spaces_around_commas_in_directories_are_fine(self) -> None:
        text = index_text(
            directories="scalable/places, scalable/devices",
            sections=section("scalable/places", 128, "Places")
            + section("scalable/devices", 128, "Devices"),
        )
        icons = {
            "scalable/places/folder.svg": svg(128),
            "scalable/devices/drive-harddisk.svg": svg(128),
        }
        self.assertEqual(problems_of(text, icons), [])


class DirectorySections(unittest.TestCase):
    def test_missing_section(self) -> None:
        text = index_text(
            directories="scalable/places,scalable/devices",
            sections=section("scalable/places", 128, "Places"),
        )
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            write_theme(tmp, text=text)
            (tmp / THEME / "scalable" / "devices").mkdir(parents=True)
            errors = [str(p) for p in cit.check_themes(tmp, [INDEX]) if not p.warning]
        self.assertIn(f"{INDEX}: no section for directory 'scalable/devices'", errors)

    def test_missing_directory_on_disk(self) -> None:
        # write_theme does not create scalable/places unless an icon is written.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            write_theme(tmp, icons={})
            errors = [str(p) for p in cit.check_themes(tmp, [INDEX]) if not p.warning]
        self.assertIn(
            f"{INDEX}: directory 'scalable/places' listed in Directories does not exist",
            errors,
        )

    def test_missing_size(self) -> None:
        text = index_text(sections="[scalable/places]\nContext=Places\nType=Scalable\n")
        self.assertTrue(
            any("[scalable/places] missing Size" in e for e in errors_of(text, icons={}))
        )

    def test_missing_context(self) -> None:
        text = index_text(sections="[scalable/places]\nSize=128\nType=Scalable\n")
        self.assertTrue(
            any("[scalable/places] missing Context" in e for e in errors_of(text, icons={}))
        )

    def test_empty_context(self) -> None:
        text = index_text(sections="[scalable/places]\nContext=\nSize=128\nType=Scalable\n")
        self.assertTrue(
            any("[scalable/places] missing Context" in e for e in errors_of(text, icons={}))
        )

    def test_missing_type(self) -> None:
        text = index_text(sections="[scalable/places]\nContext=Places\nSize=128\n")
        self.assertTrue(
            any("[scalable/places] missing Type" in e for e in errors_of(text, icons={}))
        )

    def test_unknown_type(self) -> None:
        text = index_text(sections="[scalable/places]\nContext=Places\nSize=128\nType=Vector\n")
        errors = errors_of(text, icons={})
        self.assertTrue(
            any("Type must be Fixed, Scalable or Threshold, got 'Vector'" in e for e in errors)
        )

    def test_size_must_be_a_positive_integer(self) -> None:
        for value in ("0", "-1", "128px", "big", "12.5"):
            with self.subTest(value=value):
                text = index_text(
                    sections=f"[scalable/places]\nContext=Places\nSize={value}\nType=Scalable\n"
                )
                errors = errors_of(text, icons={})
                self.assertTrue(
                    any(f"Size must be a positive integer, got '{value}'" in e for e in errors)
                )

    def test_leftover_section_is_a_warning(self) -> None:
        text = index_text() + "\n[scalable/mimetypes]\nContext=MimeTypes\nSize=128\nType=Scalable\n"
        warnings = warnings_of(text)
        self.assertTrue(
            any(
                "section [scalable/mimetypes] is not listed in Directories, "
                "so its icons would be ignored" in w
                for w in warnings
            )
        )
        self.assertEqual(errors_of(text), [])


class IconSize(unittest.TestCase):
    def test_wrong_pixel_size_is_an_error(self) -> None:
        errors = errors_of(icons={"scalable/places/folder.svg": svg(64)})
        self.assertIn(
            f"{THEME}/scalable/places/folder.svg: icon is 64x64, directory Size is 128", errors
        )

    def test_width_and_viewbox_disagree(self) -> None:
        icon = svg(width="128", height="128", viewbox="0 0 64 64")
        errors = errors_of(icons={"scalable/places/folder.svg": icon})
        self.assertTrue(any("width 128 does not match viewBox width 64" in e for e in errors))
        self.assertTrue(any("height 128 does not match viewBox height 64" in e for e in errors))

    def test_not_square(self) -> None:
        icon = svg(width="128", height="64", viewbox="0 0 128 64")
        errors = errors_of(icons={"scalable/places/folder.svg": icon})
        self.assertTrue(any("icon is not square (128x64)" in e for e in errors))

    def test_percent_width_falls_back_to_viewbox(self) -> None:
        icon = svg(width="100%", height="100%", viewbox="0 0 128 128")
        problems = problems_of(icons={"scalable/places/folder.svg": icon})
        # 100% is not a pixel length, but viewBox still gives 128 and matches Size.
        warnings = [str(p) for p in problems if p.warning]
        errors = [str(p) for p in problems if not p.warning]
        self.assertTrue(any("width='100%' is not a pixel length" in m for m in warnings))
        self.assertTrue(any("height='100%' is not a pixel length" in m for m in warnings))
        self.assertEqual(errors, [])

    def test_missing_svg_root(self) -> None:
        errors = errors_of(icons={"scalable/places/folder.svg": "<not-svg></not-svg>\n"})
        self.assertTrue(any("cannot find an <svg> root" in e for e in errors))

    def test_broken_viewbox(self) -> None:
        icon = svg(width="", height="", viewbox="128 128")
        errors = errors_of(icons={"scalable/places/folder.svg": icon})
        self.assertTrue(any("viewBox='128 128' is not four numbers" in e for e in errors))

    def test_png_wrong_size(self) -> None:
        errors = errors_of(icons={"scalable/places/folder.png": png_bytes(32, 32)})
        self.assertIn(
            f"{THEME}/scalable/places/folder.png: icon is 32x32, directory Size is 128", errors
        )

    def test_broken_png(self) -> None:
        errors = errors_of(icons={"scalable/places/folder.png": b"not a png"})
        self.assertTrue(any("not a valid PNG" in e for e in errors))

    def test_broken_svgz(self) -> None:
        errors = errors_of(icons={"scalable/places/folder.svgz": b"not gzip"})
        self.assertTrue(any("not valid gzip-compressed SVG" in e for e in errors))

    def test_icon_in_undeclared_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            write_theme(tmp)
            extra = tmp / THEME / "scalable" / "mimetypes" / "text-x-generic.svg"
            extra.parent.mkdir(parents=True)
            extra.write_text(svg(), encoding="utf-8")
            errors = [str(p) for p in cit.check_themes(tmp, [INDEX]) if not p.warning]
        self.assertIn(
            f"{THEME}/scalable/mimetypes/text-x-generic.svg: "
            "file is not under a directory listed in Directories",
            errors,
        )

    def test_icon_loose_in_the_theme_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            write_theme(tmp)
            (tmp / THEME / "folder.svg").write_text(svg(), encoding="utf-8")
            errors = [str(p) for p in cit.check_themes(tmp, [INDEX]) if not p.warning]
        self.assertIn(
            f"{THEME}/folder.svg: file is not under a directory listed in Directories",
            errors,
        )

    def test_non_icon_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            write_theme(tmp)
            (tmp / THEME / "README.md").write_text("notes\n", encoding="utf-8")
            self.assertEqual(cit.check_themes(tmp, [INDEX]), [])


class SymbolicAndDuplicates(unittest.TestCase):
    def test_symbolic_icon_without_suffix_is_an_error(self) -> None:
        text = index_text(
            directories="symbolic/status",
            sections=section("symbolic/status", 16, "Status"),
        )
        errors = errors_of(text, {"symbolic/status/vilna-local.svg": svg(16)})
        self.assertIn(
            f"{THEME}/symbolic/status/vilna-local.svg: symbolic icon name must end in -symbolic",
            errors,
        )

    def test_non_symbolic_directory_does_not_require_the_suffix(self) -> None:
        self.assertEqual(problems_of(icons={"scalable/places/folder.svg": svg()}), [])

    def test_duplicate_name_same_context_and_size(self) -> None:
        text = index_text(
            directories="scalable/places,128x128/places",
            sections=section("scalable/places", 128, "Places")
            + section("128x128/places", 128, "Places", typ="Fixed"),
        )
        icons = {
            "scalable/places/folder.svg": svg(128),
            "128x128/places/folder.svg": svg(128),
        }
        errors = errors_of(text, icons)
        self.assertEqual(len(errors), 1)
        self.assertIn("duplicate icon 'folder' in Context=Places Size=128:", errors[0])
        self.assertIn("scalable/places/folder.svg", errors[0])
        self.assertIn("128x128/places/folder.svg", errors[0])

    def test_svg_and_png_with_the_same_name_are_duplicates(self) -> None:
        icons = {
            "scalable/places/folder.svg": svg(128),
            "scalable/places/folder.png": png_bytes(128, 128),
        }
        errors = errors_of(icons=icons)
        self.assertTrue(
            any("duplicate icon 'folder' in Context=Places Size=128:" in e for e in errors)
        )

    def test_symbolic_and_regular_names_are_not_the_same_icon(self) -> None:
        text = index_text(
            directories="scalable/places,symbolic/places",
            sections=section("scalable/places", 128, "Places")
            + section("symbolic/places", 16, "Places"),
        )
        # Different Size, so even the same stem would be fine; use different stems that
        # would collide only if the suffix were stripped.
        icons = {
            "scalable/places/folder.svg": svg(128),
            "symbolic/places/folder-symbolic.svg": svg(16),
        }
        self.assertEqual(problems_of(text, icons), [])


class SoundThemesAndDiscovery(unittest.TestCase):
    SOUND = "[Sound Theme]\nName=Vilna Air\nDirectories=stereo\n\n[stereo]\nOutputProfile=stereo\n"

    def test_sound_theme_is_reported_when_passed_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = tmp / "sounds" / "vilna-air"
            path.mkdir(parents=True)
            (path / "index.theme").write_text(self.SOUND, encoding="utf-8")
            found = cit.check_themes(tmp, ["sounds/vilna-air/index.theme"])
        self.assertEqual(
            [str(p) for p in found],
            ["sounds/vilna-air/index.theme: not an icon theme (has [Sound Theme])"],
        )

    def test_sound_theme_is_skipped_in_a_repo_scan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            write_theme(tmp)
            sound = tmp / "sounds" / "vilna-air"
            sound.mkdir(parents=True)
            (sound / "index.theme").write_text(self.SOUND, encoding="utf-8")
            found = cit.check_themes(
                tmp,
                ["Demo/index.theme", "sounds/vilna-air/index.theme"],
                skip_sound=True,
            )
        self.assertEqual(found, [])

    def test_directory_argument_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            write_theme(tmp)
            self.assertEqual(cit.check_themes(tmp, [THEME]), [])

    def test_only_files_from_all_files_are_considered(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            write_theme(tmp)
            stray = tmp / THEME / "scalable" / "places" / "stray.svg"
            stray.write_text(svg(64), encoding="utf-8")
            # The stray file is on disk but not in all_files, as with git ls-files.
            found = cit.check_themes(
                tmp,
                [INDEX],
                all_files=[f"{THEME}/index.theme", f"{THEME}/scalable/places/folder.svg"],
            )
        self.assertEqual(found, [])


class Helpers(unittest.TestCase):
    def test_known_old_violation_is_a_warning(self) -> None:
        problem = cit.Problem(
            "Demo/index.theme", "no section for directory 'x'", rule="missing-section"
        )
        legacy = frozenset({("Demo/index.theme", "missing-section")})
        down = cit.downgrade(problem, legacy)
        self.assertTrue(down.warning)
        self.assertEqual(str(down), "Demo/index.theme: warning: no section for directory 'x'")
        # The same problem under another path stays an error.
        other = cit.Problem("Other/index.theme", problem.message, rule=problem.rule)
        self.assertFalse(cit.downgrade(other, legacy).warning)

    def test_legacy_table_of_the_repo_is_empty(self) -> None:
        self.assertEqual(cit.LEGACY, frozenset())

    def test_is_symbolic_directory(self) -> None:
        self.assertTrue(cit.is_symbolic_directory("symbolic/status"))
        self.assertTrue(cit.is_symbolic_directory("16x16-symbolic/actions"))
        self.assertFalse(cit.is_symbolic_directory("scalable/places"))

    def test_is_symbolic_icon_name(self) -> None:
        self.assertTrue(cit.is_symbolic_icon_name("vilna-local-symbolic"))
        self.assertTrue(cit.is_symbolic_icon_name("vilna-local-symbolic-rtl"))
        self.assertFalse(cit.is_symbolic_icon_name("vilna-local"))
        self.assertFalse(cit.is_symbolic_icon_name("symbolic"))
        self.assertFalse(cit.is_symbolic_icon_name("foo-symbolicbar"))


class Main(unittest.TestCase):
    def run_main(self, args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cit.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_exit_0_for_a_good_theme(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            index = write_theme(tmp)
            code, out, err = self.run_main([str(index)])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("1 icon themes checked: 0 errors, 0 warnings", out)

    def test_warnings_do_not_fail_the_check(self) -> None:
        text = index_text() + "\n[scalable/mimetypes]\nContext=MimeTypes\nSize=128\nType=Scalable\n"
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            index = write_theme(tmp, text=text)
            code, out, err = self.run_main([str(index)])
        self.assertEqual(code, 0)
        self.assertIn("warning: section [scalable/mimetypes] is not listed in Directories", err)
        self.assertIn("0 errors, 1 warnings", out)

    def test_exit_1_and_message_for_a_bad_theme(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            index = write_theme(tmp, icons={"scalable/places/folder.svg": svg(16)})
            code, out, err = self.run_main([str(index)])
        self.assertEqual(code, 1)
        self.assertIn("icon is 16x16, directory Size is 128", err)
        self.assertIn("1 errors, 0 warnings", out)

    def test_exit_2_for_an_unknown_option(self) -> None:
        code, _, err = self.run_main(["--bogus"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)

    def test_help_is_not_an_error(self) -> None:
        self.assertEqual(self.run_main(["--help"])[0], 0)
        self.assertEqual(self.run_main(["-h"])[0], 0)

    def test_explicit_sound_theme_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = tmp / "sounds" / "vilna-air"
            path.mkdir(parents=True)
            index = path / "index.theme"
            index.write_text(SoundThemesAndDiscovery.SOUND, encoding="utf-8")
            code, out, err = self.run_main([str(index)])
        self.assertEqual(code, 1)
        self.assertIn("not an icon theme (has [Sound Theme])", err)


class RealRepo(unittest.TestCase):
    def test_tracked_icon_themes_are_the_two_vilna_themes(self) -> None:
        paths = cit.tracked_icon_themes(ROOT)
        self.assertEqual(
            paths,
            [
                "docs/design/icons/system/Vilna-Classic/index.theme",
                "docs/design/icons/system/Vilna/index.theme",
            ],
        )

    def test_sound_themes_are_not_listed_as_icon_themes(self) -> None:
        indexes = cit.tracked_index_files(ROOT)
        self.assertTrue(any(p.startswith("sounds/themes/") for p in indexes))
        self.assertFalse(any(p.startswith("sounds/") for p in cit.tracked_icon_themes(ROOT)))

    def test_tracked_themes_have_no_errors(self) -> None:
        indexes = cit.tracked_icon_themes(ROOT)
        files = cit.tracked_files(ROOT)
        errors = [str(p) for p in cit.check_themes(ROOT, indexes, files) if not p.warning]
        self.assertEqual(errors, [])

    def test_main_without_arguments_on_the_repo(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cit.main([])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(err.getvalue(), "")
        self.assertIn("2 icon themes checked:", out.getvalue())
        self.assertIn("0 errors", out.getvalue())


if __name__ == "__main__":
    unittest.main()

