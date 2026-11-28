#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the tracked icon themes (freedesktop ``index.theme``) of the repo.

For every directory that contains an ``index.theme`` with an ``[Icon Theme]``
group the checker verifies:

* the file is a valid key-file whose first group is ``[Icon Theme]``; every
  line is a comment, an empty line, a ``[Group]`` or a ``Key=Value``; no key
  is repeated inside a group; ``Name``, ``Comment`` and ``Directories`` are
  set;
* each entry in ``Directories=`` exists as a directory and has a section
  with ``Size`` (a positive integer), ``Context`` (non-empty) and ``Type``
  (``Fixed``, ``Scalable`` or ``Threshold``);
* icon files (``.svg``, ``.svgz``, ``.png``) live in those directories, and
  their pixel size (from ``width``/``height`` or ``viewBox``, or the PNG
  header) matches that ``Size``; ``width``/``height`` and ``viewBox`` must
  agree when both are present; the icon is square;
* an icon in a directory whose path has a ``symbolic`` component (or a name
  ending in ``-symbolic``) has the ``-symbolic`` suffix in its file name;
* no two icons share the same name in the same ``Context`` and ``Size``.

Sound themes (``[Sound Theme]`` without ``[Icon Theme]``) are skipped: they
are not icon themes. Standalone SVG files that do not belong to a theme are
not checked here. SVG safety (scripts, hrefs, editor leftovers, colours) is
not this tool's job.

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed
but do not fail the check; they mark things the repo already does today, or
a leftover section that the theme would ignore.

Only ``git ls-files`` is run (to find the files). Nothing else is executed
and nothing is written.

Run: python3 tools/check_icon_theme.py [THEME ...]
THEME may be an ``index.theme`` file or a theme directory.
Without arguments every tracked icon theme is checked.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import gzip
import re
import subprocess
import os
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

ICON_THEME_GROUP = "Icon Theme"
SOUND_THEME_GROUP = "Sound Theme"
DIRECTORY_TYPES = ("Fixed", "Scalable", "Threshold")
ICON_SUFFIXES = (".svg", ".svgz", ".png")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

GROUP_LINE = re.compile(r"\[([^\[\]\x00-\x1f\x7f]+)\]")
KEY_NAME = re.compile(r"([A-Za-z0-9-]+)(?:\[([^\[\]\s]+)\])?")
# Unitless or px lengths only: theme Size is a pixel value.
PX_LENGTH = re.compile(
    r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(px)?\s*$",
    re.IGNORECASE,
)
SVG_OPEN = re.compile(r"<svg\b[^>]*>", re.IGNORECASE | re.DOTALL)
SVG_ATTR = re.compile(
    r"""(?:^|\s)(width|height|viewBox)\s*=\s*(?:['"]([^'"]*)['"]|([^\s>]+))""",
    re.IGNORECASE,
)
# foo-symbolic or a right-to-left / locale variant such as foo-symbolic-rtl.
SYMBOLIC_NAME = re.compile(r".+-symbolic(?:-[A-Za-z0-9]+)?$")

# Violations the repo already has today. They are reported as warnings, not
# errors, so that the check can be switched on in CI; fix the file, then
# delete the line. Each entry is (path from the repo root, rule).
LEGACY: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False
    rule: str = ""

    def __str__(self) -> str:
        kind = "warning: " if self.warning else ""
        return f"{self.path}: {kind}{self.message}"


@dataclass
class Group:
    name: str
    line: int
    # (line number, key, language or None, value), in file order.
    entries: list[tuple[int, str, str | None, str]] = field(default_factory=list)

    def get(self, key: str, lang: str | None = None) -> str | None:
        for _, k, lg, value in self.entries:
            if k == key and lg == lang:
                return value
        return None


@dataclass(frozen=True)
class Directory:
    """One entry from ``Directories=`` with the keys of its section."""

    rel: str
    size: int
    context: str
    typ: str


def parse(path: str, text: str) -> tuple[list[Group], list[Problem]]:
    """Split a key-file into groups; report lines that are not valid syntax."""
    groups: list[Group] = []
    problems: list[Problem] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            match = GROUP_LINE.fullmatch(line)
            if match is None:
                problems.append(Problem(path, f"line {number}: invalid group header '{line}'"))
            elif any(g.name == match.group(1) for g in groups):
                problems.append(Problem(path, f"line {number}: group [{match.group(1)}] repeated"))
            else:
                groups.append(Group(match.group(1), number))
            continue
        key_text, sep, value = line.partition("=")
        key_match = KEY_NAME.fullmatch(key_text.strip())
        if not sep or key_match is None:
            problems.append(
                Problem(path, f"line {number}: not a comment, empty line, [Group] or Key=Value")
            )
        elif not groups:
            problems.append(Problem(path, f"line {number}: key before the first group"))
        else:
            groups[-1].entries.append(
                (number, key_match.group(1), key_match.group(2), value.strip())
            )
    return groups, problems


def group_map(groups: list[Group]) -> dict[str, Group]:
    return {g.name: g for g in groups}


def split_list(value: str) -> list[str]:
    """Comma-separated freedesktop list, empty items dropped, spaces stripped."""
    return [item.strip() for item in value.split(",") if item.strip()]


def posix(root: Path, path: Path) -> str:
    """A stable path for messages: relative to ``root`` when possible."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def is_symbolic_directory(rel: str) -> bool:
    """True if this theme-relative directory holds symbolic icons."""
    return any(part == "symbolic" or part.endswith("-symbolic") for part in rel.split("/"))


def is_symbolic_icon_name(stem: str) -> bool:
    return SYMBOLIC_NAME.fullmatch(stem) is not None


def parse_px(value: str) -> float | None:
    """A CSS length in pixels (unitless or ``px``); None for any other unit."""
    match = PX_LENGTH.fullmatch(value)
    return float(match.group(1)) if match else None


def parse_viewbox(value: str) -> tuple[float, float] | None:
    """The width and height of an SVG ``viewBox``, or None if it is not four numbers."""
    parts = value.replace(",", " ").split()
    if len(parts) != 4:
        return None
    try:
        width, height = float(parts[2]), float(parts[3])
    except ValueError:
        return None
    return width, height


def svg_root_attributes(text: str) -> dict[str, str] | None:
    """Attributes of the first ``<svg>`` start tag; None if there is no such tag."""
    match = SVG_OPEN.search(text)
    if match is None:
        return None
    found: dict[str, str] = {}
    for attr in SVG_ATTR.finditer(match.group(0)):
        found[attr.group(1).lower()] = attr.group(2) if attr.group(2) is not None else attr.group(3)
    return found


def png_size(data: bytes) -> tuple[int, int] | None:
    """Pixel width and height from a PNG IHDR chunk; None if the file is not a PNG."""
    if len(data) < 24 or data[:8] != PNG_MAGIC or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def svg_size(text: str) -> tuple[float | None, float | None, list[str]]:
    """Pixel width and height of an SVG, plus messages if the attributes disagree."""
    notes: list[str] = []
    attrs = svg_root_attributes(text)
    if attrs is None:
        return None, None, ["cannot find an <svg> root"]
    width = parse_px(attrs["width"]) if "width" in attrs else None
    height = parse_px(attrs["height"]) if "height" in attrs else None
    box = parse_viewbox(attrs["viewbox"]) if "viewbox" in attrs else None
    if "width" in attrs and width is None:
        notes.append(f"width={attrs['width']!r} is not a pixel length")
    if "height" in attrs and height is None:
        notes.append(f"height={attrs['height']!r} is not a pixel length")
    if "viewbox" in attrs and box is None:
        notes.append(f"viewBox={attrs['viewbox']!r} is not four numbers")
    if width is not None and box is not None and width != box[0]:
        notes.append(f"width {width:g} does not match viewBox width {box[0]:g}")
    if height is not None and box is not None and height != box[1]:
        notes.append(f"height {height:g} does not match viewBox height {box[1]:g}")
    pixel_w = width if width is not None else (box[0] if box is not None else None)
    pixel_h = height if height is not None else (box[1] if box is not None else None)
    return pixel_w, pixel_h, notes


def icon_size(path: str, data: bytes, suffix: str) -> tuple[float | None, float | None, list[str]]:
    """Pixel width and height of an icon file, plus messages about the read."""
    if suffix == ".png":
        size = png_size(data)
        if size is None:
            return None, None, ["the file is not a valid PNG (missing or broken IHDR)"]
        return float(size[0]), float(size[1]), []
    if suffix == ".svgz":
        try:
            data = gzip.decompress(data)
        except OSError:
            return None, None, ["the file is not valid gzip-compressed SVG"]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, ["the file is not valid UTF-8"]
    if text.startswith("\ufeff"):
        text = text[1:]
    return svg_size(text)


def matches_size(value: float, size: int) -> bool:
    return value == size


def check_index_structure(path: str, groups: list[Group]) -> list[Problem]:
    problems: list[Problem] = []
    if not groups or groups[0].name != ICON_THEME_GROUP:
        problems.append(Problem(path, f"the first group must be [{ICON_THEME_GROUP}]"))
    for group in groups:
        seen: set[tuple[str, str | None]] = set()
        for number, key, lang, _ in group.entries:
            if (key, lang) in seen:
                shown = key if lang is None else f"{key}[{lang}]"
                problems.append(
                    Problem(path, f"line {number}: duplicate key {shown} in [{group.name}]")
                )
            seen.add((key, lang))
    return problems


def parse_directories(
    path: str, head: Group, groups: dict[str, Group]
) -> tuple[list[Directory], list[Problem]]:
    """Validate ``Directories=`` and each directory section; return the usable ones."""
    problems: list[Problem] = []
    raw = head.get("Directories")
    if raw is None:
        problems.append(
            Problem(path, f"[{ICON_THEME_GROUP}] missing Directories", rule="missing-key")
        )
        return [], problems
    names = split_list(raw)
    if not names:
        problems.append(
            Problem(path, f"[{ICON_THEME_GROUP}] Directories is empty", rule="missing-key")
        )
        return [], problems
    seen: set[str] = set()
    found: list[Directory] = []
    for name in names:
        if name in seen:
            problems.append(
                Problem(
                    path,
                    f"[{ICON_THEME_GROUP}] directory '{name}' listed more than once in Directories",
                )
            )
            continue
        seen.add(name)
        section = groups.get(name)
        if section is None:
            problems.append(
                Problem(path, f"no section for directory '{name}'", rule="missing-section")
            )
            continue
        directory, extra = parse_directory_section(path, section)
        problems += extra
        if directory is not None:
            found.append(directory)
    return found, problems


def parse_directory_section(path: str, section: Group) -> tuple[Directory | None, list[Problem]]:
    problems: list[Problem] = []
    raw_size = section.get("Size")
    raw_context = section.get("Context")
    raw_type = section.get("Type")
    if raw_size is None:
        problems.append(Problem(path, f"[{section.name}] missing Size", rule="missing-key"))
    if raw_context is None or not raw_context:
        problems.append(Problem(path, f"[{section.name}] missing Context", rule="missing-key"))
    if raw_type is None:
        problems.append(Problem(path, f"[{section.name}] missing Type", rule="missing-key"))
    elif raw_type not in DIRECTORY_TYPES:
        problems.append(
            Problem(
                path,
                f"[{section.name}] Type must be Fixed, Scalable or Threshold, got '{raw_type}'",
                rule="bad-type",
            )
        )
        raw_type = None
    size: int | None = None
    if raw_size is not None:
        if raw_size.isdigit() and int(raw_size) > 0:
            size = int(raw_size)
        else:
            problems.append(
                Problem(
                    path,
                    f"[{section.name}] Size must be a positive integer, got '{raw_size}'",
                    rule="bad-size",
                )
            )
    if size is None or not raw_context or raw_type is None:
        return None, problems
    return Directory(section.name, size, raw_context, raw_type), problems


def check_theme_header(path: str, head: Group) -> list[Problem]:
    problems: list[Problem] = []
    if not head.get("Name"):
        problems.append(Problem(path, f"[{ICON_THEME_GROUP}] missing Name", rule="missing-key"))
    if head.get("Comment") is None:
        problems.append(Problem(path, f"[{ICON_THEME_GROUP}] missing Comment", rule="missing-key"))
    return problems


def check_declared_directories(
    root: Path, index_path: Path, directories: Sequence[Directory]
) -> list[Problem]:
    problems: list[Problem] = []
    theme_dir = index_path.parent
    shown = posix(root, index_path)
    for directory in directories:
        target = theme_dir / directory.rel
        if not target.is_dir():
            problems.append(
                Problem(
                    shown,
                    f"directory '{directory.rel}' listed in Directories does not exist",
                    rule="missing-directory",
                )
            )
    return problems


def theme_relative_icons(theme_dir: Path, root: Path, all_files: Sequence[Path]) -> list[Path]:
    """Absolute paths of icon files that live inside ``theme_dir``."""
    theme = theme_dir.resolve()
    found: list[Path] = []
    for path in all_files:
        resolved = path if path.is_absolute() else (root / path)
        try:
            rel = resolved.resolve().relative_to(theme)
        except (ValueError, OSError):
            continue
        if not rel.parts or rel.as_posix() == "index.theme":
            continue
        if resolved.suffix.lower() in ICON_SUFFIXES:
            found.append(resolved)
    return found


def icon_files_on_disk(theme_dir: Path) -> list[Path]:
    if not theme_dir.is_dir():
        return []
    found: list[Path] = []
    for path in theme_dir.rglob("*"):
        if not path.is_file() or path.name == "index.theme":
            continue
        if path.suffix.lower() in ICON_SUFFIXES:
            found.append(path)
    return found


def check_icon_file(
    root: Path, theme_dir: Path, path: Path, by_dir: dict[str, Directory]
) -> tuple[str | None, Directory | None, list[Problem]]:
    """Return (icon name, directory, problems) for one icon file."""
    shown = posix(root, path)
    try:
        rel = path.resolve().relative_to(theme_dir.resolve()).as_posix()
    except ValueError:
        return None, None, [Problem(shown, "file is not inside the theme directory")]
    parent = str(Path(rel).parent.as_posix())
    if parent == ".":
        return (
            None,
            None,
            [
                Problem(
                    shown, "file is not under a directory listed in Directories", rule="undeclared"
                )
            ],
        )
    directory = by_dir.get(parent)
    if directory is None:
        return (
            None,
            None,
            [
                Problem(
                    shown, "file is not under a directory listed in Directories", rule="undeclared"
                )
            ],
        )
    problems: list[Problem] = []
    suffix = path.suffix.lower()
    try:
        data = path.read_bytes()
    except OSError as err:
        return None, directory, [Problem(shown, f"cannot read the file: {err.strerror or err}")]
    if data.startswith(b"\xef\xbb\xbf") and suffix in {".svg", ".svgz"}:
        problems.append(Problem(shown, "the file starts with a byte order mark (BOM)"))
    width, height, notes = icon_size(shown, data, suffix)
    size_known = width is not None and height is not None
    for note in notes:
        # A non-pixel width/height is only a warning when viewBox still gives a size.
        warning = size_known and "is not a pixel length" in note
        problems.append(Problem(shown, note, warning=warning, rule="size-mismatch"))
    if width is not None and height is not None and width != height:
        problems.append(
            Problem(
                shown,
                f"icon is not square ({width:g}x{height:g})",
                rule="size-mismatch",
            )
        )
    if width is not None and not matches_size(width, directory.size):
        shown_h = f"{height:g}" if height is not None else "?"
        problems.append(
            Problem(
                shown,
                f"icon is {width:g}x{shown_h}, directory Size is {directory.size}",
                rule="size-mismatch",
            )
        )
    elif width is None and height is None and not notes:
        problems.append(Problem(shown, "cannot determine the pixel size", rule="size-mismatch"))
    stem = path.stem
    # path.stem of 'foo-symbolic.svgz' after a double suffix is still foo-symbolic.
    if suffix == ".svgz" and stem.endswith(".svg"):
        stem = Path(stem).stem
    if is_symbolic_directory(parent) and not is_symbolic_icon_name(stem):
        problems.append(
            Problem(
                shown,
                "symbolic icon name must end in -symbolic",
                rule="missing-symbolic-suffix",
            )
        )
    return stem, directory, problems


def check_duplicates(index_shown: str, placed: list[tuple[str, Directory, str]]) -> list[Problem]:
    """``placed`` is (icon name, directory, display path)."""
    groups: dict[tuple[str, int, str], list[str]] = defaultdict(list)
    for name, directory, shown in placed:
        groups[directory.context, directory.size, name].append(shown)
    problems: list[Problem] = []
    for (context, size, name), paths in sorted(groups.items()):
        unique = list(dict.fromkeys(paths))
        if len(unique) < 2:
            continue
        listed = ", ".join(unique)
        problems.append(
            Problem(
                index_shown,
                f"duplicate icon '{name}' in Context={context} Size={size}: {listed}",
                rule="duplicate-icon",
            )
        )
    return problems


def check_leftover_sections(
    path: str, groups: Sequence[Group], declared: set[str]
) -> list[Problem]:
    problems: list[Problem] = []
    for group in groups:
        if group.name in {ICON_THEME_GROUP, SOUND_THEME_GROUP} or group.name in declared:
            continue
        problems.append(
            Problem(
                path,
                f"section [{group.name}] is not listed in Directories, "
                "so its icons would be ignored",
                warning=True,
                rule="leftover-section",
            )
        )
    return problems


def is_sound_only(groups: Sequence[Group]) -> bool:
    names = {g.name for g in groups}
    return SOUND_THEME_GROUP in names and ICON_THEME_GROUP not in names


def check_theme_text(
    root: Path,
    index_path: Path,
    text: str,
    icon_files: Sequence[Path],
) -> list[Problem]:
    """All problems of one theme; ``index_path`` is the ``index.theme`` file."""
    shown = posix(root, index_path)
    groups, problems = parse(shown, text)
    if is_sound_only(groups) and not any(not p.warning for p in problems):
        return [
            Problem(shown, f"not an icon theme (has [{SOUND_THEME_GROUP}])", rule="not-icon-theme")
        ]
    problems += check_index_structure(shown, groups)
    groups_by_name = group_map(groups)
    head = groups_by_name.get(ICON_THEME_GROUP)
    if head is None:
        return [downgrade(p) for p in problems]
    problems += check_theme_header(shown, head)
    directories, extra = parse_directories(shown, head, groups_by_name)
    problems += extra
    problems += check_declared_directories(root, index_path, directories)
    problems += check_leftover_sections(shown, groups, {d.rel for d in directories})
    by_dir = {d.rel: d for d in directories}
    theme_dir = index_path.parent
    placed: list[tuple[str, Directory, str]] = []
    for icon in icon_files:
        name, directory, icon_problems = check_icon_file(root, theme_dir, icon, by_dir)
        problems += icon_problems
        if name is not None and directory is not None:
            placed.append((name, directory, posix(root, icon)))
    problems += check_duplicates(shown, placed)
    return [downgrade(p) for p in problems]


def downgrade(problem: Problem, legacy: frozenset[tuple[str, str]] | None = None) -> Problem:
    """Turn a known old violation (see LEGACY) into a warning."""
    table = LEGACY if legacy is None else legacy
    if (problem.path, problem.rule) in table:
        return Problem(problem.path, problem.message, warning=True, rule=problem.rule)
    return problem


def read_index(root: Path, index_rel: str) -> tuple[Path, str | None, list[Problem]]:
    """Read ``index.theme``; the path may be relative to ``root`` or absolute."""
    path = Path(index_rel)
    if not path.is_absolute():
        path = root / path
    shown = posix(root, path) if path.exists() or not Path(index_rel).is_absolute() else index_rel
    # Prefer the caller's spelling when it is already a nice relative path.
    if not Path(index_rel).is_absolute():
        shown = Path(index_rel).as_posix()
        if path.name != "index.theme":
            shown = (Path(index_rel) / "index.theme").as_posix()
    else:
        shown = path.as_posix()
        if path.name != "index.theme":
            shown = (path / "index.theme").as_posix()
    try:
        data = path.read_bytes() if path.is_file() else (path / "index.theme").read_bytes()
    except OSError as err:
        return (
            path if path.suffix == ".theme" else path / "index.theme",
            None,
            [Problem(shown, f"cannot read the file: {err.strerror or err}")],
        )
    index_path = path if path.is_file() else path / "index.theme"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return index_path, None, [Problem(posix(root, index_path), "the file is not valid UTF-8")]
    problems: list[Problem] = []
    if text.startswith("\ufeff"):
        problems.append(
            Problem(posix(root, index_path), "the file starts with a byte order mark (BOM)")
        )
        text = text[1:]
    return index_path, text, problems


def check_themes(
    root: Path,
    indexes: Sequence[str],
    all_files: Sequence[str] | None = None,
    *,
    skip_sound: bool = False,
) -> list[Problem]:
    """Check each theme listed in ``indexes`` (``index.theme`` or a theme directory).

    When ``all_files`` is given it is the universe of paths (relative to ``root``
    or absolute) that may be icons; otherwise the files on disk under each
    theme directory are used. Sound themes are reported unless ``skip_sound``
    is true (the no-argument scan of the repo).
    """
    problems: list[Problem] = []
    for index in indexes:
        index_path, text, read_problems = read_index(root, index)
        problems += read_problems
        if text is None:
            continue
        if skip_sound:
            groups, _ = parse(posix(root, index_path), text)
            if is_sound_only(groups):
                continue
        if all_files is None:
            icons = icon_files_on_disk(index_path.parent)
        else:
            icons = theme_relative_icons(
                index_path.parent,
                root,
                [
                    (root / item) if not Path(item).is_absolute() else Path(item)
                    for item in all_files
                ],
            )
        problems += check_theme_text(root, index_path, text, icons)
    return problems


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [p for p in out.split("\0") if p]


def tracked_index_files(root: Path) -> list[str]:
    return sorted(
        p for p in tracked_files(root) if p == "index.theme" or p.endswith("/index.theme")
    )


def tracked_icon_themes(root: Path) -> list[str]:
    """Tracked ``index.theme`` files that are icon themes, not sound themes."""
    found: list[str] = []
    for rel in tracked_index_files(root):
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            found.append(rel)
            continue
        if text.startswith("\ufeff"):
            text = text[1:]
        groups, _ = parse(rel, text)
        if is_sound_only(groups):
            continue
        found.append(rel)
    return found


def main(argv: list[str] | None = None) -> int:
    try:
        args = sys.argv[1:] if argv is None else argv
        if any(a.startswith("-") for a in args):
            print(
                "usage: python3 tools/check_icon_theme.py [THEME ...]",
                file=sys.stderr,
            )
            return 0 if args in (["-h"], ["--help"]) else 2
        root = Path.cwd() if args else ROOT
        all_files: list[str] | None
        if args:
            indexes = args
            all_files = None
            skip_sound = False
        else:
            try:
                tracked = tracked_files(ROOT)
            except (OSError, subprocess.CalledProcessError) as err:
                print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
                return 2
            indexes = [p for p in tracked if p == "index.theme" or p.endswith("/index.theme")]
            all_files = tracked
            skip_sound = True
            if not indexes:
                print("no tracked index.theme files found", file=sys.stderr)
                return 2
        problems = check_themes(root, indexes, all_files, skip_sound=skip_sound)
        # A no-argument run that found only sound themes should say so clearly.
        if not args and skip_sound:
            checked = tracked_icon_themes(ROOT)
            if not checked and not problems:
                print("no tracked icon themes found", file=sys.stderr)
                return 2
            n_themes = len(checked)
        else:
            n_themes = len(indexes)
        for problem in problems:
            print(problem, file=sys.stderr)
        errors = sum(not p.warning for p in problems)
        print(f"{n_themes} icon themes checked: {errors} errors, {len(problems) - errors} warnings")
        return 1 if errors else 0
    except (OSError, ValueError) as err:  # no traceback for a bad name, argument or bytes
        print(f"{os.path.basename(__file__)}: cannot check: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

