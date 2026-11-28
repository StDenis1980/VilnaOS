#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the boot splash (Plymouth) and the branding files of the repo.

Four groups of rules, each problem is printed as ``path: message``:

* **Plymouth theme** (``image/plymouth``, ``obs/packages/plymouth-branding-Vilna``):
  the ``.plymouth`` file names a script (``ScriptFile``) that is tracked next to
  it; ``ImageDir``, the file name, ``Name`` and ``Theme=`` of ``plymouthd.defaults``
  agree; every ``Image("...")`` of the script is made by ``make_theme.py`` (or is a
  designer frame); the frame counts of the script and of ``make_theme.py`` are the
  same; ``plymouthd.defaults`` has a numeric ``ShowDelay`` and a positive
  ``DeviceTimeout`` (without them plymouthd aborts); the spec installs the
  files, owns the theme folder and requires the plugins the theme needs.
* **PNG files** (tracked ``*.png`` under ``docs/design``, ``image`` and ``obs``),
  read with the standard library only: signature, chunk lengths and CRC, ``IHDR``,
  the pixel data inflates to exactly the size the header says (never more than
  that is read into memory), nothing after ``IEND``. A size written in the name
  (``-1920x1080.png``; ``-128.png`` in ``docs/design/blavet/png``) must divide the
  real size by the same whole factor. A folder ``X@2x`` has the same files as
  ``X`` at twice the size. Numbered frames have no gaps and one size. A ``tEXt``,
  ``zTXt`` or ``iTXt`` chunk with a file path is an error (personal data); the
  message never repeats the text. Other text chunks and ``eXIf`` are a warning.
* **os-release** (``obs/packages/Vilna-release``): the syntax of os-release(5),
  every field the readers use (``reports/client``, ``gloxi``, ``image/tools``)
  and the identification fields (ID, ID_LIKE, VERSION, ANSI_COLOR, LOGO), the
  values, ``NAME VERSION_ID`` fits the image field of the report protocol, the
  ``@VERSION@`` placeholder is replaced by the spec, ``LOGO`` is an icon of
  ``distribution-logos-Vilna``. HOME_URL, SUPPORT_URL and BUG_REPORT_URL are
  warnings while they are missing (see ``LEGACY``).
* **Release name**: ``VERSION_CODENAME`` of os-release, ``RELEASE_NAME`` of the
  Plymouth script and the release name of the installer are one name, and the
  sign of that release is in the repo.

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but do
not fail the check; they mark things the repo already does today (see
``LEGACY``) or things that are allowed with a reminder.

Only ``git ls-files`` is run (to find the files; outside a git repository the
folder is walked, symbolic links are never followed). Files that are not regular
files, symbolic links and files above the size limits are reported, not read.
Nothing else is executed, nothing is read from the network and nothing is written.

Run: python3 tools/check_branding.py [--root DIR]
Without arguments the tracked files of this repo are checked.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import configparser
import os
import posixpath
import re
import stat
import struct
import subprocess
import sys
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

# --- where things are ---------------------------------------------------------

PLYMOUTH_DIR = "image/plymouth"
PLYMOUTH_SPEC = "obs/packages/plymouth-branding-Vilna/plymouth-branding-Vilna.spec"
RELEASE_FILE = "obs/packages/Vilna-release/os-release"
RELEASE_SPEC = "obs/packages/Vilna-release/Vilna-release.spec"
LOGOS_DIR = "obs/packages/distribution-logos-Vilna"
OBS_SYNC = "obs/tools/obs_sync.py"
FRAMES_DIR = "docs/design/plymouth/logo@2x"  # obs_sync.py puts it into the tarball as frames/
INSTALLER_RELEASE = "installer/src/vilna_installer/release.py"
INSTALLER_START = "installer/src/vilna_installer/start.py"
PROTOCOL = "reports/protocol/src/lib.rs"
PNG_ROOTS = ("docs/design/", "image/", "obs/")
# Folders whose PNG names end in the side of a square image: -128.png.
SIDE_IN_NAME = ("docs/design/blavet/png/",)

# Files that read os-release fields, and how a field name looks there.
FIELD_READERS = (
    ("reports/client/src/device.rs", re.compile(r'"([A-Z][A-Z0-9_]*)"\s*(?:if [^=]*)?=>')),
    ("gloxi/src/gloxi/check.py", re.compile(r'startswith\("([A-Z][A-Z0-9_]*)="\)')),
    ("image/tools/memcheck.py", re.compile(r'startswith\("([A-Z][A-Z0-9_]*)="\)')),
)
# Written by the image build, not by the release package.
OPTIONAL_FIELDS = frozenset({"VILNA_IMAGE"})
# Fields every Vilna os-release has (obs/README.md, image/docs/composition.md).
IDENTITY_FIELDS = (
    "NAME",
    "PRETTY_NAME",
    "ID",
    "ID_LIKE",
    "VERSION",
    "VERSION_ID",
    "VERSION_CODENAME",
    "ANSI_COLOR",
    "LOGO",
)
URL_FIELDS = ("HOME_URL", "SUPPORT_URL", "BUG_REPORT_URL")
# The image field of the report protocol (reports/protocol/src/lib.rs); used if the
# pattern cannot be read from there.
DEFAULT_IMAGE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}$"

# --- limits ---------------------------------------------------------------------

TEXT_LIMIT = 1 << 20  # bytes of a text file that is read
PNG_LIMIT = 64 << 20  # bytes of a PNG file that is read
MAX_PIXELS = 25_000_000  # a boot splash image has no use for more
TEXT_CHUNK_LIMIT = 1 << 20  # bytes of a PNG text chunk that is inflated
BLOCK = 1 << 20  # bytes inflated at a time
WALK_LIMIT = 200_000  # files listed outside a git repository
DEFAULT_MAX_FRAMES = 240

# Problems the repo already has today: (path from the repo root, rule). They are
# warnings, so that the check can be switched on in CI; fix the file, then delete
# the line.
LEGACY: frozenset[tuple[str, str]] = frozenset(
    {
        # "add once the project domain is chosen" (comment in the file).
        (RELEASE_FILE, "missing-url"),
    }
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
# (x start, y start, x step, y step) of the seven Adam7 passes.
ADAM7 = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)
TEXT_CHUNKS = (b"tEXt", b"zTXt", b"iTXt")
# A file path in text: home folders, temp and mount folders, a Windows drive or share,
# file: URLs, ``~/``, or any absolute path with two or more parts.
PATH_IN_TEXT = re.compile(
    r"(?<![\w:./-])/(?:home|Users|root|tmp|mnt|media|var|run|etc|usr|opt|srv)/"
    r"|(?<![\w:./-])/[\w.@+-]+/[\w.@+-]+"
    r"|(?<![\w~])~[/\\]"
    r"|(?<![A-Za-z])[A-Za-z]:[\\/]"
    r"|\\\\[\w.$-]+\\"
    r"|file:/",
    re.IGNORECASE,
)

SIZE_IN_NAME = re.compile(r"-(\d+)x(\d+)\.png$")
SIDE_IN_FILE = re.compile(r"-(\d+)\.png$")
FRAME_NAME = re.compile(r"^(.*?)(\d+)\.png$")
FIELD_LINE = re.compile(r"([A-Z_][A-Z0-9_]*)=(.*)")
PLACEHOLDER = re.compile(r"@([A-Za-z_]+)@")


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
class Repo:
    """The files of a repo and what was found in them."""

    root: Path
    tracked: frozenset[str]
    problems: list[Problem] = field(default_factory=list)
    checked: int = 0
    _bytes: dict[str, bytes | None] = field(default_factory=dict)

    def add(self, path: str, rule: str, message: str, *, warning: bool = False) -> None:
        self.problems.append(Problem(path, message, warning, rule))

    def read(self, rel: str, limit: int, *, required: bool = True) -> bytes | None:
        """The bytes of a regular file inside the repo, or ``None`` with a problem."""
        if rel in self._bytes:
            return self._bytes[rel]
        data = self._read(rel, limit, required)
        self._bytes[rel] = data
        return data

    def _read(self, rel: str, limit: int, required: bool) -> bytes | None:
        if rel not in self.tracked:
            if required:
                self.add(rel, "missing-file", "file is missing")
            return None
        full = self.root / rel
        try:
            info = os.lstat(full)
            if stat.S_ISLNK(info.st_mode):
                self.add(rel, "not-regular", "is a symbolic link, not read")
                return None
            if not stat.S_ISREG(info.st_mode):
                self.add(rel, "not-regular", "is not a regular file, not read")
                return None
            if info.st_size > limit:
                self.add(rel, "too-big", f"file is larger than {limit >> 10} KiB, not read")
                return None
            with open(full, "rb") as handle:
                return handle.read(limit + 1)[:limit]
        except FileNotFoundError:
            self.add(rel, "missing-file", "file is missing")
            return None
        except OSError as err:
            self.add(rel, "unreadable", f"cannot read file: {err.strerror or type(err).__name__}")
            return None

    def text(self, rel: str, *, required: bool = True, strict_lines: bool = False) -> str | None:
        """UTF-8 text of a file; ``strict_lines`` also refuses a BOM, CR and NUL."""
        data = self.read(rel, TEXT_LIMIT, required=required)
        if data is None:
            return None
        if data.startswith(b"\xef\xbb\xbf"):
            if strict_lines:
                self.add(rel, "encoding", "starts with a UTF-8 byte order mark")
            data = data[3:]
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            self.add(rel, "encoding", "is UTF-16 text, not UTF-8")
            return None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self.add(rel, "encoding", "is not valid UTF-8")
            return None
        if "\x00" in text:
            self.add(rel, "encoding", "contains a NUL character")
            return None
        if strict_lines and "\r" in text:
            self.add(rel, "encoding", "has CR characters (Windows line endings)")
        return text


# --- PNG ------------------------------------------------------------------------


@dataclass
class Png:
    width: int
    height: int


def raw_size(width: int, height: int, bits_per_pixel: int, interlace: int) -> int:
    """Bytes of the inflated pixel data, filter bytes included."""

    def plain(w: int, h: int) -> int:
        return h * (1 + (w * bits_per_pixel + 7) // 8) if w > 0 and h > 0 else 0

    if not interlace:
        return plain(width, height)
    total = 0
    for x0, y0, dx, dy in ADAM7:
        total += plain((width - x0 + dx - 1) // dx, (height - y0 + dy - 1) // dy)
    return total


def verify_pixels(parts: list[bytes], expected: int, stride: int | None) -> str | None:
    """Inflate the IDAT data; a problem text if it is not exactly ``expected`` bytes."""
    inflater = zlib.decompressobj()
    total = 0
    try:
        for part in parts:
            data = part
            while True:
                out = inflater.decompress(data, BLOCK)
                if stride and out:
                    first = (-total) % stride
                    marks = out[first::stride]
                    if marks and max(marks) > 4:
                        return "image data has a scanline with an unknown filter type"
                total += len(out)
                if total > expected:
                    return "image data is larger than the size in the header"
                data = inflater.unconsumed_tail
                if not data and len(out) < BLOCK:
                    break
            if inflater.unused_data:
                return "image data continues after the end of the zlib stream"
    except zlib.error:
        return "image data is not a valid zlib stream"
    if not inflater.eof:
        return "image data is cut off"
    if total != expected:
        return "image data is smaller than the size in the header"
    return None


def text_chunk_strings(kind: bytes, body: bytes) -> list[str] | None:
    """The keyword and the text of a PNG text chunk, ``None`` if it is not readable."""
    try:
        keyword, _, rest = body.partition(b"\x00")
        pieces = [keyword.decode("latin-1")]
        if kind == b"tEXt":
            pieces.append(rest.decode("latin-1"))
        elif kind == b"zTXt":
            if not rest or rest[0] != 0:
                return None
            pieces.append(inflate_limited(rest[1:]).decode("latin-1"))
        else:  # iTXt: flag, method, language, translated keyword, text
            if len(rest) < 2:
                return None
            flag, method, tail = rest[0], rest[1], rest[2:]
            language, _, tail = tail.partition(b"\x00")
            translated, _, text = tail.partition(b"\x00")
            if flag:
                if method != 0:
                    return None
                text = inflate_limited(text)
            pieces += [language.decode("latin-1"), translated.decode("utf-8", "replace")]
            pieces.append(text.decode("utf-8", "replace"))
        return pieces
    except (zlib.error, ValueError):
        return None


def inflate_limited(data: bytes) -> bytes:
    out = zlib.decompressobj().decompress(data, TEXT_CHUNK_LIMIT)
    return out


def check_png(rel: str, data: bytes) -> tuple[Png | None, list[Problem]]:
    """Check one PNG file; its size if the header could be read."""
    problems: list[Problem] = []

    def err(rule: str, message: str, *, warning: bool = False) -> None:
        problems.append(Problem(rel, message, warning, rule))

    if not data.startswith(PNG_SIGNATURE):
        err("png-signature", "is not a PNG file (wrong signature)")
        return None, problems
    info: Png | None = None
    header: tuple[int, int, int, int, int] | None = None  # w, h, depth, colour, interlace
    idat: list[bytes] = []
    seen: list[bytes] = []
    pos = len(PNG_SIGNATURE)
    ended = False
    text_paths = text_other = 0
    while pos < len(data):
        if pos + 8 > len(data):
            err("png-chunk", "is cut off inside a chunk header")
            break
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        if not re.fullmatch(rb"[A-Za-z]{4}", kind):
            err("png-chunk", "has a chunk with an invalid type")
            break
        if length > 0x7FFFFFFF or pos + 12 + length > len(data):
            err("png-chunk", f"is cut off inside a {kind.decode()} chunk")
            break
        body = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        if zlib.crc32(kind + body) != crc:
            err("png-crc", f"{kind.decode()} chunk has a wrong checksum")
        pos += 12 + length
        if not seen and kind != b"IHDR":
            err("png-chunk", "first chunk is not IHDR")
        if kind == b"IHDR":
            if seen:
                err("png-chunk", "has a second IHDR chunk")
            elif length != 13:
                err("png-header", "IHDR chunk has a wrong length")
            else:
                width, height, depth, colour, method, filtering, interlace = struct.unpack(
                    ">IIBBBBB", body
                )
                header = (width, height, depth, colour, interlace)
                info = Png(width, height)
                if not (0 < width <= 0x7FFFFFFF and 0 < height <= 0x7FFFFFFF):
                    err("png-header", "has a width or height of 0 or above 2^31-1")
                    header = None
                elif colour not in DEPTHS or depth not in DEPTHS[colour]:
                    err("png-header", "has a colour type and bit depth that PNG does not allow")
                    header = None
                elif method or filtering or interlace not in (0, 1):
                    err("png-header", "has an unknown compression, filter or interlace method")
                    header = None
                elif width * height > MAX_PIXELS:
                    err("png-header", f"is larger than {MAX_PIXELS} pixels, not checked further")
                    header = None
        elif kind == b"IDAT":
            if idat and seen[-1] != b"IDAT":
                err("png-chunk", "IDAT chunks are not one after the other")
            idat.append(body)
        elif kind == b"PLTE":
            if header and header[3] in (0, 4):
                err("png-chunk", "has a palette, which this colour type must not have")
            if length % 3 or not 3 <= length <= 768:
                err("png-chunk", "PLTE chunk has a wrong length")
        elif kind in TEXT_CHUNKS:
            strings = text_chunk_strings(kind, body)
            if strings is None:
                text_other += 1
            elif any(PATH_IN_TEXT.search(s) for s in strings):
                text_paths += 1
            else:
                text_other += 1
        elif kind == b"eXIf":
            err("png-metadata", "has an eXIf chunk (camera or editor metadata)", warning=True)
        seen.append(kind)
        if kind == b"IEND":
            ended = True
            break
    if not ended:
        err("png-chunk", "has no IEND chunk (file is cut off)")
    elif pos < len(data):
        err("png-trailing", f"has {len(data) - pos} bytes after the IEND chunk", warning=True)
    if header is not None:
        width, height, depth, colour, interlace = header
        if colour == 3 and b"PLTE" not in seen:
            err("png-chunk", "has a palette colour type but no PLTE chunk")
        if not idat:
            err("png-data", "has no image data (no IDAT chunk)")
        else:
            bits = depth * CHANNELS[colour]
            expected = raw_size(width, height, bits, interlace)
            stride = None if interlace else 1 + (width * bits + 7) // 8
            problem = verify_pixels(idat, expected, stride)
            if problem:
                err("png-data", problem)
    if text_paths:
        err("png-text-path", f"has {text_paths} text chunk(s) with a file path (personal data)")
    if text_other:
        err("png-metadata", f"has {text_other} text chunk(s)", warning=True)
    return info, problems


def check_pngs(repo: Repo) -> None:
    sizes: dict[str, Png] = {}
    for rel in sorted(p for p in repo.tracked if p.lower().endswith(".png")):
        if not rel.startswith(PNG_ROOTS):
            continue
        data = repo.read(rel, PNG_LIMIT)
        repo.checked += 1
        if data is None:
            continue
        info, problems = check_png(rel, data)
        repo.problems += problems
        if info is not None:
            sizes[rel] = info
    for rel, info in sizes.items():
        check_size_in_name(repo, rel, info)
    check_frames(repo, sizes)
    check_double_size(repo, sizes)


def check_size_in_name(repo: Repo, rel: str, info: Png) -> None:
    """A size in the file name divides the real size by one whole factor."""
    named: tuple[int, int] | None = None
    found = SIZE_IN_NAME.search(rel)
    if found:
        named = (int(found.group(1)), int(found.group(2)))
    elif rel.startswith(SIDE_IN_NAME):
        side = SIDE_IN_FILE.search(rel)
        if side:
            named = (int(side.group(1)), int(side.group(1)))
    if named is None:
        return
    nw, nh = named
    if nw == 0 or nh == 0:
        repo.add(rel, "png-size-name", "has a size of 0 in its name")
    elif info.width % nw or info.height % nh or info.width // nw != info.height // nh:
        repo.add(
            rel,
            "png-size-name",
            f"is {info.width}x{info.height}, not a whole multiple of the {nw}x{nh} in its name",
        )


def check_frames(repo: Repo, sizes: dict[str, Png]) -> None:
    """Numbered frames of one folder: no gaps, one size, not more than the script plays."""
    groups: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for rel in sizes:
        if not rel.startswith("docs/design/plymouth/"):
            continue
        found = FRAME_NAME.match(posixpath.basename(rel))
        if found and found.group(1):
            key = (posixpath.dirname(rel), found.group(1))
            groups.setdefault(key, []).append((int(found.group(2)), rel))
    limit = max_frames(repo)
    for (folder, prefix), items in sorted(groups.items()):
        if len(items) < 2:
            continue
        items.sort()
        numbers = [n for n, _ in items]
        if len(set(numbers)) != len(numbers):
            repo.add(folder, "frames", f"two {prefix}N.png files have the same number")
        missing = sorted(set(range(numbers[0], numbers[-1] + 1)) - set(numbers))
        if missing:
            repo.add(folder, "frames", f"{prefix}{missing[0]}.png is missing (a gap in the frames)")
        if len(items) > limit:
            repo.add(folder, "frames", f"{len(items)} frames, the script plays at most {limit}")
        first_size = sizes[items[0][1]]
        for _, rel in items:
            size = sizes[rel]
            if (size.width, size.height) != (first_size.width, first_size.height):
                repo.add(rel, "frames", "has another size than the first frame of the folder")
            elif size.width != size.height:
                repo.add(rel, "frames", "frame is not square", warning=True)
    if FRAMES_DIR + "/" not in {posixpath.dirname(r) + "/" for r in sizes}:
        repo.add(FRAMES_DIR, "frames", "no PNG frames (the tarball takes them as frames/)")


def check_double_size(repo: Repo, sizes: dict[str, Png]) -> None:
    """A folder ``X@2x`` has the files of ``X`` at twice the size."""
    by_folder: dict[str, dict[str, Png]] = {}
    for rel, info in sizes.items():
        by_folder.setdefault(posixpath.dirname(rel), {})[posixpath.basename(rel)] = info
    for folder, files in sorted(by_folder.items()):
        if not folder.endswith("@2x"):
            continue
        base = by_folder.get(folder[: -len("@2x")])
        if base is None:
            continue
        for name in sorted(set(files) - set(base)):
            repo.add(f"{folder}/{name}", "png-2x", "has no file of this name at the normal size")
        for name in sorted(set(base) - set(files)):
            repo.add(f"{folder}/{name}", "png-2x", "is missing (the normal size has it)")
        for name in sorted(set(files) & set(base)):
            one, two = base[name], files[name]
            if (two.width, two.height) != (2 * one.width, 2 * one.height):
                repo.add(
                    f"{folder}/{name}",
                    "png-2x",
                    f"is {two.width}x{two.height}, not twice {one.width}x{one.height}",
                )


# --- Plymouth ------------------------------------------------------------------


def strip_script_comments(text: str) -> str:
    """The Plymouth script without comments (``#``, ``//`` and ``/* */``), strings kept."""
    out: list[str] = []
    i = 0
    quote = ""
    while i < len(text):
        char = text[i]
        if quote:
            out.append(char)
            if char == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 1
            elif char == quote or char == "\n":
                quote = ""
        elif char in "\"'":
            quote = char
            out.append(char)
        elif char == "#" or text.startswith("//", i):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 1
            out.append(" ")
        else:
            out.append(char)
        i += 1
    return "".join(out)


def script_images(text: str) -> tuple[list[str], list[str]]:
    """Literal image names, and examples (with 0 for the counter) of built names."""
    code = strip_script_comments(text)
    literals = re.findall(r'\bImage\(\s*"([^"]+)"\s*\)', code)
    built = [
        f"{a}0{b}"
        for a, b in re.findall(r'\bImage\(\s*"([^"]*)"\s*\+\s*\w+\s*\+\s*"([^"]*)"\s*\)', code)
    ]
    return literals, built


def generated_images(text: str) -> list[re.Pattern[str]]:
    """Patterns of the PNG files ``make_theme.py`` writes (``{i}`` is a number)."""
    names = [n + ".png" for n in re.findall(r'\bwrite\(\s*args\.out\s*,\s*f?"([^"]+)"', text)]
    names += re.findall(r'(?<![\w"])f?"([\w{}-]+\.png)"', text)
    patterns = []
    for name in sorted(set(names)):
        parts = re.split(r"\{\w+\}", name)
        patterns.append(re.compile(r"\d+".join(re.escape(p) for p in parts)))
    return patterns


def int_constants(text: str, pattern: str) -> dict[str, int]:
    return {m.group(1): int(m.group(2)) for m in re.finditer(pattern, text, re.MULTILINE)}


def max_frames(repo: Repo) -> int:
    text = repo.text(f"{PLYMOUTH_DIR}/vilna.script", required=False)
    if text:
        found = int_constants(text, r"^(MAX_LOGO_FRAMES)\s*=\s*(\d+)\s*;")
        return found.get("MAX_LOGO_FRAMES", DEFAULT_MAX_FRAMES)
    return DEFAULT_MAX_FRAMES


def read_ini(repo: Repo, rel: str) -> configparser.ConfigParser | None:
    text = repo.text(rel, strict_lines=True)
    if text is None:
        return None
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    try:
        parser.read_string(text, source=rel)
    except configparser.Error as err:
        first = str(err).splitlines()[0] if str(err) else type(err).__name__
        repo.add(rel, "ini", f"cannot be parsed: {first.replace(rel, 'file')}")
        return None
    return parser


def check_plymouth(repo: Repo) -> None:
    themes = sorted(p for p in repo.tracked if p.endswith(".plymouth"))
    if not themes:
        repo.add(PLYMOUTH_DIR, "missing-file", "no .plymouth file")
    for rel in themes:
        check_theme(repo, rel)
    check_defaults(repo, themes)
    check_plymouth_spec(repo, themes)


def check_theme(repo: Repo, rel: str) -> None:
    repo.checked += 1
    folder = posixpath.dirname(rel)
    stem = posixpath.basename(rel)[: -len(".plymouth")]
    ini = read_ini(repo, rel)
    if ini is None:
        return

    def value(section: str, key: str) -> str | None:
        if not ini.has_section(section) or not ini.has_option(section, key):
            repo.add(rel, "theme-key", f"[{section}] has no {key}")
            return None
        found = ini.get(section, key).strip()
        if not found:
            repo.add(rel, "theme-key", f"[{section}] {key} is empty")
            return None
        return found

    name = value("Plymouth Theme", "Name")
    value("Plymouth Theme", "Description")
    module = value("Plymouth Theme", "ModuleName")
    if name and name.casefold() != stem.casefold():
        repo.add(rel, "theme-name", f"Name={name} does not match the file name {stem}")
    if module is None:
        return
    if module != "script":
        repo.add(rel, "theme-key", f"ModuleName={module}, but the theme ships a script")
        return
    image_dir = value(module, "ImageDir")
    script_file = value(module, "ScriptFile")
    if image_dir:
        if not image_dir.startswith("/usr/share/plymouth/themes/") or ".." in image_dir.split("/"):
            repo.add(rel, "theme-dir", "ImageDir is not a folder under /usr/share/plymouth/themes")
        elif posixpath.basename(image_dir.rstrip("/")) != stem:
            repo.add(rel, "theme-dir", f"ImageDir does not end in {stem}")
    if script_file:
        if image_dir and posixpath.dirname(script_file) != image_dir.rstrip("/"):
            repo.add(rel, "theme-dir", "ScriptFile is not inside ImageDir")
        script = posixpath.basename(script_file)
        if not script.endswith(".script"):
            repo.add(rel, "theme-script", f"ScriptFile {script} does not end in .script")
        script_rel = posixpath.join(folder, script)
        if script_rel not in repo.tracked:
            repo.add(rel, "theme-script", f"ScriptFile {script} is not tracked next to it")
        else:
            check_script(repo, script_rel, folder)


def check_script(repo: Repo, rel: str, folder: str) -> None:
    repo.checked += 1
    text = repo.text(rel, strict_lines=True)
    maker = repo.text(f"{folder}/make_theme.py", required=False)
    if text is None:
        return
    if maker is None:
        repo.add(rel, "missing-file", f"{folder}/make_theme.py is missing (it makes the images)")
        return
    patterns = generated_images(maker)
    literals, built = script_images(text)
    for name in dict.fromkeys(literals + built):
        if not any(p.fullmatch(name) for p in patterns):
            shown = name.replace("0", "N", 1) if name in built else name
            repo.add(rel, "image-missing", f'Image("{shown}") is not made by make_theme.py')
    pairs = (
        ("CIRCLE_FRAMES", "CIRCLE_FRAMES"),
        ("BLOOM_FRAMES", "BLOOM_FRAMES"),
        ("MAX_LOGO_FRAMES", "MAX_FRAMES"),
    )
    in_script = int_constants(text, r"^([A-Z_]+)\s*=\s*(\d+)\s*;")
    in_maker = int_constants(maker, r"^([A-Z_]+)\s*=\s*(\d+)\b")
    for left, right in pairs:
        if left not in in_script:
            repo.add(rel, "frame-count", f"{left} is not set")
        elif right not in in_maker:
            repo.add(f"{folder}/make_theme.py", "frame-count", f"{right} is not set")
        elif in_script[left] != in_maker[right]:
            repo.add(
                rel,
                "frame-count",
                f"{left}={in_script[left]} but make_theme.py has {right}={in_maker[right]}",
            )


def check_defaults(repo: Repo, themes: list[str]) -> None:
    rel = f"{PLYMOUTH_DIR}/plymouthd.defaults"
    repo.checked += 1
    ini = read_ini(repo, rel)
    if ini is None:
        return
    if not ini.has_section("Daemon"):
        repo.add(rel, "defaults", "has no [Daemon] section")
        return
    theme = ini.get("Daemon", "Theme", fallback="").strip()
    stems = {posixpath.basename(t)[: -len(".plymouth")] for t in themes}
    if not theme:
        repo.add(rel, "defaults", "has no Theme (plymouthd would show the stock theme)")
    elif stems and theme not in stems:
        repo.add(
            rel,
            "defaults",
            f"Theme={theme} is not a theme of the repo ({', '.join(sorted(stems))})",
        )
    for key, minimum in (("ShowDelay", 0.0), ("DeviceTimeout", 0.0)):
        raw = ini.get("Daemon", key, fallback="").strip()
        try:
            number = float(raw)
        except ValueError:
            repo.add(rel, "defaults", f"{key} is missing or not a number (plymouthd aborts)")
            continue
        if number != number or number < minimum or (key == "DeviceTimeout" and number <= 0):
            repo.add(rel, "defaults", f"{key}={raw} is out of range (plymouthd aborts)")


def spec_section(text: str, name: str) -> str:
    """The lines of a spec section (``%install``) up to the next section."""
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        if re.match(r"%(prep|build|install|check|files|changelog|description|package)\b", line):
            inside = line.split()[0] == name
            continue
        if inside:
            lines.append(line)
    return "\n".join(lines)


INSTALL_LINE = re.compile(r"^\s*(?:install|cp|%\{?__install\}?)\b")


def install_sources(section: str) -> set[str]:
    """Words of the ``install`` and ``cp`` commands of a spec section (continuations joined)."""
    words: set[str] = set()
    for line in section.replace("\\\n", " ").splitlines():
        if INSTALL_LINE.match(line):
            words.update(line.split())
    return words


def check_plymouth_spec(repo: Repo, themes: list[str]) -> None:
    text = repo.text(PLYMOUTH_SPEC)
    if text is None:
        return
    repo.checked += 1
    install = spec_section(text, "%install")
    files = spec_section(text, "%files")
    requires = set(re.findall(r"^Requires:\s*(\S+)", text, re.MULTILINE))
    build_requires = set(re.findall(r"^BuildRequires:\s*(\S+)", text, re.MULTILINE))
    installed = install_sources(install)
    for theme in themes:
        stem = posixpath.basename(theme)[: -len(".plymouth")]
        script_names = [posixpath.basename(theme)]
        script_names += [
            posixpath.basename(t)
            for t in repo.tracked
            if posixpath.dirname(t) == posixpath.dirname(theme) and t.endswith(".script")
        ]
        for name in script_names + ["plymouthd.defaults"]:
            if name not in installed:
                repo.add(PLYMOUTH_SPEC, "spec", f"%install does not install {name}")
        if f"themes/{stem}/" not in files and f"themes/{stem}\n" not in files + "\n":
            repo.add(PLYMOUTH_SPEC, "spec", f"%files does not own the theme folder {stem}")
        ini_text = repo.text(theme, required=False) or ""
        if (
            "ModuleName=script" in ini_text.replace(" ", "")
            and "plymouth-plugin-script" not in requires
        ):
            repo.add(PLYMOUTH_SPEC, "spec", "does not require plymouth-plugin-script")
        script = repo.text(theme[: -len(".plymouth")] + ".script", required=False) or ""
        if (
            "Image.Text" in strip_script_comments(script)
            and "plymouth-plugin-label" not in requires
        ):
            repo.add(PLYMOUTH_SPEC, "spec", "does not require plymouth-plugin-label (Image.Text)")
    if "plymouthd.defaults" not in files:
        repo.add(PLYMOUTH_SPEC, "spec", "%files does not own plymouthd.defaults")
    if "plymouth-branding" not in re.findall(r"^Provides:\s*(\S+)", text, re.MULTILINE):
        repo.add(PLYMOUTH_SPEC, "spec", "does not provide plymouth-branding")
    maker = repo.text(f"{PLYMOUTH_DIR}/make_theme.py", required=False) or ""
    if "rsvg-convert" in maker and "rsvg-convert" not in build_requires:
        repo.add(PLYMOUTH_SPEC, "spec", "make_theme.py runs rsvg-convert; BuildRequires lacks it")
    if "python3" not in build_requires:
        repo.add(PLYMOUTH_SPEC, "spec", "BuildRequires lacks python3")
    build = spec_section(text, "%build")
    logo = re.search(r"make_theme\.py\s+(\S+\.svg)\b", build)
    sync = repo.text(OBS_SYNC, required=False)
    if logo and sync is not None and f'"{logo.group(1)}"' not in sync:
        repo.add(
            PLYMOUTH_SPEC,
            "spec",
            f"%build uses {logo.group(1)}, which {OBS_SYNC} does not put into the tarball",
        )


# --- os-release ---------------------------------------------------------------


def unquote_value(raw: str) -> tuple[str | None, str | None]:
    """The value of an os-release line and a problem text (os-release(5) quoting)."""
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        i = 1
        out: list[str] = []
        while i < len(raw):
            char = raw[i]
            if char == "\\" and i + 1 < len(raw) and (quote == '"' or raw[i + 1] == "'"):
                out.append(raw[i + 1] if raw[i + 1] in "\"\\$`'" else "\\" + raw[i + 1])
                i += 2
                continue
            if char == quote:
                if raw[i + 1 :].strip() and not raw[i + 1 :].lstrip().startswith("#"):
                    return None, "has text after the closing quote"
                return "".join(out), None
            out.append(char)
            i += 1
        return None, "has an opening quote without a closing one"
    if re.search(r"\s", raw):
        return None, "has an unquoted value with spaces"
    if re.search(r"[\"'\\$`;&|<>(){}*?]", raw):
        return None, "has an unquoted value with shell characters"
    return raw, None


def parse_os_release(repo: Repo, rel: str, text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        found = FIELD_LINE.fullmatch(stripped)
        if not found or line != line.lstrip() or "=" not in line:
            repo.add(rel, "os-release-syntax", f"line {number} is not FIELD=value")
            continue
        key, raw = found.groups()
        value, problem = unquote_value(raw)
        if value is None:
            repo.add(rel, "os-release-syntax", f"line {number}: {key} {problem}")
            continue
        if key in values:
            repo.add(rel, "os-release-syntax", f"line {number}: {key} is set twice")
        values[key] = value
    return values


def required_fields(repo: Repo) -> dict[str, str]:
    """Fields the repo reads from os-release (and who reads them), plus the identity fields."""
    readers: dict[str, list[str]] = {}
    for rel, pattern in FIELD_READERS:
        text = repo.text(rel, required=False)
        if text is None:
            continue
        for name in dict.fromkeys(pattern.findall(text)):
            if name not in OPTIONAL_FIELDS:
                readers.setdefault(name, []).append(rel)
    fields = {name: "the identification of Vilna" for name in IDENTITY_FIELDS}
    for name, who in readers.items():
        fields[name] = ", ".join(who)
    return fields


def image_pattern(repo: Repo) -> re.Pattern[str]:
    text = repo.text(PROTOCOL, required=False) or ""
    found = re.search(r'static IMAGE:[^;]*?re\(r"([^"]+)"\)', text, re.DOTALL)
    if found:
        try:
            return re.compile(found.group(1))
        except re.error:
            pass
    return re.compile(DEFAULT_IMAGE_PATTERN)


def check_os_release(repo: Repo) -> dict[str, str]:
    text = repo.text(RELEASE_FILE, strict_lines=True)
    spec = repo.text(RELEASE_SPEC)
    if text is None:
        return {}
    repo.checked += 1
    rel = RELEASE_FILE
    values = parse_os_release(repo, rel, text)
    for name, who in required_fields(repo).items():
        if not values.get(name, "").strip():
            repo.add(rel, "missing-field", f"{name} is missing or empty (used by {who})")
    for name in URL_FIELDS:
        if name not in values:
            repo.add(rel, "missing-url", f"{name} is not set")
        elif not re.fullmatch(r"https://[^\s@/]+(/\S*)?", values[name]):
            repo.add(rel, "bad-value", f"{name} is not an https:// address")
    version = spec_version(spec) if spec else None
    if spec is None or version is None:
        if spec is not None:
            repo.add(RELEASE_SPEC, "spec", "has no Version")
    else:
        check_release_spec(repo, spec, text)
    shown = {k: substitute(v, version) for k, v in values.items()}
    for key, value in values.items():
        for name in PLACEHOLDER.findall(value):
            if name != "VERSION":
                repo.add(rel, "bad-value", f"{key} has a placeholder @{name}@ that nothing fills")
        if "@VERSION@" in value and version is None:
            repo.add(rel, "bad-value", f"{key} has @VERSION@ but the spec has no Version")
    check_values(repo, rel, shown)
    return shown


def spec_version(spec: str) -> str | None:
    found = re.search(r"^Version:\s*(\S+)", spec, re.MULTILINE)
    return found.group(1) if found else None


def substitute(value: str, version: str | None) -> str:
    return value.replace("@VERSION@", version) if version else value


def check_values(repo: Repo, rel: str, v: dict[str, str]) -> None:
    def bad(name: str, why: str) -> None:
        repo.add(rel, "bad-value", f"{name} {why}")

    def match(name: str, pattern: str, why: str) -> None:
        if name in v and v[name].strip() and not re.fullmatch(pattern, v[name]):
            bad(name, why)

    match("ID", r"[a-z0-9._-]+", "must be lower-case letters, digits, . _ -")
    match("VERSION_ID", r"[A-Za-z0-9._-]+", "must be letters, digits, . _ -")
    match("VERSION_CODENAME", r"[a-z][a-z0-9_-]*", "must be a lower-case word")
    match("ANSI_COLOR", r"[0-9]+(;[0-9]+)*", "must be numbers separated by ;")
    match("LOGO", r"[a-z0-9._-]+", "must be an icon name")
    match("ID_LIKE", r"[a-z0-9._-]+( [a-z0-9._-]+)*", "must be IDs separated by spaces")
    for name, value in v.items():
        if re.search(r"[\x00-\x1f\x7f]", value):
            bad(name, "has a control character")
    if v.get("NAME") and v.get("ID") and v["NAME"].casefold() != v["ID"].casefold():
        repo.add(rel, "bad-value", "NAME and ID are not the same word", warning=True)
    if v.get("VERSION") and v.get("VERSION_ID") and not v["VERSION"].startswith(v["VERSION_ID"]):
        repo.add(rel, "bad-value", "VERSION does not start with VERSION_ID", warning=True)
    if (
        v.get("PRETTY_NAME")
        and v.get("NAME")
        and v.get("VERSION")
        and v["PRETTY_NAME"] != f"{v['NAME']} {v['VERSION']}"
    ):
        repo.add(rel, "bad-value", "PRETTY_NAME is not NAME and VERSION", warning=True)
    if v.get("NAME") and v.get("VERSION_ID"):
        image = f"{v['NAME']} {v['VERSION_ID']}"
        if not image_pattern(repo).fullmatch(image):
            repo.add(
                rel,
                "client-image",
                "NAME and VERSION_ID do not fit the image field of the report protocol "
                "(the client would send a report the server refuses)",
            )
    logo = v.get("LOGO", "")
    if logo and f"{LOGOS_DIR}/{logo}.svg" not in repo.tracked:
        repo.add(rel, "logo-missing", f"LOGO={logo} is not an icon of {LOGOS_DIR}")


def check_release_spec(repo: Repo, spec: str, os_release: str) -> None:
    repo.checked += 1
    if "@VERSION@" in os_release and "@VERSION@" not in spec_section(spec, "%build"):
        repo.add(RELEASE_SPEC, "spec", "%build does not replace @VERSION@ in os-release")
    install = spec_section(spec, "%install")
    if "os-release" not in install_sources(install) or "/os-release" not in spec_section(
        spec, "%files"
    ):
        repo.add(RELEASE_SPEC, "spec", "does not install os-release (%install or %files)")
    elif not re.search(r"ln\s+-s\S*\s+\.\./usr/lib/os-release\s", install + " "):
        repo.add(RELEASE_SPEC, "spec", "does not link /etc/os-release to /usr/lib/os-release")


# --- one release name ----------------------------------------------------------


def check_release_name(repo: Repo, values: dict[str, str]) -> None:
    codename = values.get("VERSION_CODENAME", "")
    names: dict[str, str] = {}
    script = repo.text(f"{PLYMOUTH_DIR}/vilna.script", required=False)
    if script is not None:
        found = re.search(r'^RELEASE_NAME\s*=\s*"([^"]*)"\s*;', strip_script_comments(script), re.M)
        if found:
            names[f"{PLYMOUTH_DIR}/vilna.script RELEASE_NAME"] = found.group(1)
        else:
            repo.add(f"{PLYMOUTH_DIR}/vilna.script", "release-name", "RELEASE_NAME is not set")
    for rel, key in ((INSTALLER_RELEASE, "NAME"), (INSTALLER_START, "RELEASE")):
        text = repo.text(rel, required=False)
        if text is None:
            continue
        found = re.search(rf'^{key}\s*=\s*"([^"]*)"', text, re.MULTILINE)
        if found:
            names[f"{rel} {key}"] = found.group(1)
        else:
            repo.add(rel, "release-name", f"{key} is not set")
    if not codename:
        return
    for where, name in names.items():
        if name.casefold() != codename.casefold():
            repo.add(
                where.split(" ")[0],
                "release-name",
                f"{where.split(' ')[1]}={name} but VERSION_CODENAME of os-release is {codename}",
            )
    mark = f"{PLYMOUTH_DIR}/release/mark.svg"
    expected = f"docs/design/{codename}/svg/{codename}-mark-dark-bg.svg"
    if mark not in repo.tracked and expected not in repo.tracked:
        repo.add(expected, "missing-file", f"the sign of the release {codename} is missing")


# --- running ----------------------------------------------------------------------


def check_repo(root: Path, tracked: Sequence[str]) -> tuple[list[Problem], int]:
    """All problems (LEGACY ones as warnings) and the number of files looked at."""
    repo = Repo(root, frozenset(tracked))
    check_plymouth(repo)
    check_pngs(repo)
    values = check_os_release(repo)
    check_release_name(repo, values)
    problems = [downgrade(p) for p in dict.fromkeys(repo.problems)]
    return sorted(problems, key=lambda p: (p.path, p.warning, p.message)), repo.checked


def downgrade(problem: Problem) -> Problem:
    """Turn a known old violation (see LEGACY) into a warning."""
    if (problem.path, problem.rule) in LEGACY:
        return Problem(problem.path, problem.message, warning=True, rule=problem.rule)
    return problem


def list_files(root: Path) -> list[str]:
    """Tracked files by ``git ls-files``; outside a git repository, a walk without links."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if out.strip("\0"):
            return sorted(p for p in out.split("\0") if p)
    except (OSError, subprocess.CalledProcessError):
        pass
    found: list[str] = []
    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(names):
            found.append(Path(folder, name).relative_to(root).as_posix())
            if len(found) >= WALK_LIMIT:
                return found
    return found


def main(argv: list[str] | None = None) -> int:
    try:
        args = sys.argv[1:] if argv is None else argv
        usage = "usage: python3 tools/check_branding.py [--root DIR]"
        if args in (["-h"], ["--help"]):
            print(usage, file=sys.stderr)
            return 0
        root = ROOT
        if args:
            if len(args) != 2 or args[0] != "--root":
                print(usage, file=sys.stderr)
                return 2
            root = Path(args[1])
            if not root.is_dir():
                print(f"not a folder: {args[1]}", file=sys.stderr)
                return 2
        tracked = list_files(root)
        if not tracked:
            print("no files found", file=sys.stderr)
            return 2
        problems, checked = check_repo(root, tracked)
        for problem in problems:
            print(problem, file=sys.stderr)
        errors = sum(not p.warning for p in problems)
        print(f"{checked} branding files checked: {errors} errors, {len(problems) - errors} warnings")
        return 1 if errors else 0
    except (OSError, ValueError) as err:  # no traceback for a bad name, argument or bytes
        print(f"{os.path.basename(__file__)}: cannot check: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
