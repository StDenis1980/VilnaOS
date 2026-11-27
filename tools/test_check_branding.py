#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_branding.py (the check of the boot splash and branding files).

Every test writes small files into a temporary folder, the way a real repo has them,
and runs the check on them. There is a test for every rule, one for the good files,
tests with broken, cut-off, huge and hostile PNG files, and a run on the real repo.

Run: python3 tools/test_check_branding.py
"""

from __future__ import annotations

import contextlib
import io
import os
import random
import struct
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_branding as cb  # noqa: E402  (after sys.path.insert above)

# --- building PNG files -------------------------------------------------------


def chunk(kind: bytes, body: bytes, *, crc: int | None = None) -> bytes:
    checksum = zlib.crc32(kind + body) if crc is None else crc
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", checksum)


def ihdr(w: int, h: int, depth: int = 8, colour: int = 6, interlace: int = 0) -> bytes:
    return chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, colour, 0, 0, interlace))


def pixels(w: int, h: int, colour: int = 6, depth: int = 8) -> bytes:
    """Raw scanlines (filter 0) of a transparent image."""
    row = (w * cb.CHANNELS[colour] * depth + 7) // 8
    return (b"\x00" + bytes(row)) * h


def adam7_pixels(w: int, h: int, channels: int = 4, depth: int = 8) -> bytes:
    """Raw data of an interlaced image, made by walking the pixels of each pass."""
    passes = [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4)]
    passes += [(1, 0, 2, 2), (0, 1, 1, 2)]
    raw = bytearray()
    for x0, y0, dx, dy in passes:
        for _ in range(y0, h, dy):
            count = len(range(x0, w, dx))
            if count:
                raw += b"\x00" + bytes((count * channels * depth + 7) // 8)
    return bytes(raw)


def png(
    w: int = 4,
    h: int = 4,
    *,
    colour: int = 6,
    depth: int = 8,
    raw: bytes | None = None,
    before: bytes = b"",
    after: bytes = b"",
    split: int = 0,
    end: bool = True,
) -> bytes:
    """A good PNG; the keyword arguments break it in one place."""
    data = zlib.compress(pixels(w, h, colour, depth) if raw is None else raw)
    if split:
        idat = b"".join(chunk(b"IDAT", data[i : i + split]) for i in range(0, len(data), split))
    else:
        idat = chunk(b"IDAT", data)
    tail = chunk(b"IEND", b"") if end else b""
    return cb.PNG_SIGNATURE + ihdr(w, h, depth, colour) + before + idat + tail + after


def text_chunk(text: str, keyword: str = "Comment") -> bytes:
    return chunk(b"tEXt", keyword.encode("latin-1") + b"\x00" + text.encode("latin-1"))


def ztxt_chunk(text: str) -> bytes:
    return chunk(b"zTXt", b"Comment\x00\x00" + zlib.compress(text.encode("latin-1")))


def itxt_chunk(text: str, *, compressed: bool = False) -> bytes:
    body = text.encode("utf-8")
    if compressed:
        body = zlib.compress(body)
    return chunk(b"iTXt", b"Comment\x00" + bytes([int(compressed), 0]) + b"\x00\x00" + body)


# --- the files of a good repo ------------------------------------------------

PLYMOUTH = """[Plymouth Theme]
Name=Vilna
Description=A test theme
ModuleName=script

[script]
ImageDir=/usr/share/plymouth/themes/vilna
ScriptFile=/usr/share/plymouth/themes/vilna/vilna.script
"""

SCRIPT = """# SPDX-License-Identifier: GPL-3.0-or-later
# Image("not-a-real-image.png") in a comment is not an image.
RELEASE_NAME = "Blavet";
CIRCLE_FRAMES = 30;
BLOOM_FRAMES = 24;
MAX_LOGO_FRAMES = 3;
first = Image("logo-0.png");
glow = Image("glow.png");
release = Image("release-mark.png");
for (i = 0; i < 24; i++) bloom[i] = Image("bloom-" + i + ".png");
name = Image.Text(RELEASE_NAME, 1, 1, 1, 1);
"""

MAKER = '''"""Makes the images."""
CIRCLE_FRAMES = 30
BLOOM_FRAMES = 24
MAX_FRAMES = 3


def main():
    for i, frame in enumerate(frames):
        shutil.copyfile(f, out / f"logo-{i}.png")
        write(args.out, f"bloom-{i}", frame, args.svg_only)
    write(args.out, "glow", glow(), args.svg_only)
    run(["rsvg-convert", "-o", str(out / "release-mark.png"), str(svg)])
'''

DEFAULTS = """# defaults
[Daemon]
Theme=vilna
ShowDelay=0
DeviceTimeout=8
"""

PLYMOUTH_SPEC = """Name:           plymouth-branding-Vilna
Version:        0.4
BuildRequires:  python3
BuildRequires:  rsvg-convert
Requires:       plymouth
Requires:       plymouth-plugin-label
Requires:       plymouth-plugin-script
Provides:       plymouth-branding

%build
python3 make_theme.py Vilna_logo_dark-bg.svg build

%install
dest=%{buildroot}%{_datadir}/plymouth/themes/vilna
install -m 0644 vilna.plymouth vilna.script build/*.png "$dest/"
install -D -m 0644 plymouthd.defaults %{buildroot}%{_datadir}/plymouth/plymouthd.defaults

%files
%{_datadir}/plymouth/themes/vilna/
%{_datadir}/plymouth/plymouthd.defaults

%changelog
"""

OS_RELEASE = """# comment
NAME="Vilna"
PRETTY_NAME="Vilna @VERSION@ Blavet (pre-alpha)"
ID="vilna"
ID_LIKE="opensuse-tumbleweed opensuse suse"
VERSION="@VERSION@ Blavet (pre-alpha)"
VERSION_CODENAME="blavet"
VERSION_ID="@VERSION@"
ANSI_COLOR="0;38;2;0;87;184"
LOGO="vilna-logo"
HOME_URL="https://example.org/"
SUPPORT_URL="https://example.org/help"
BUG_REPORT_URL="https://example.org/bugs"
"""

RELEASE_SPEC = """Name:           Vilna-release
Version:        0.4
Source0:        os-release

%build
sed 's/@VERSION@/%{version}/g' %{SOURCE0} > os-release

%install
install -D -m 0644 os-release %{buildroot}%{_prefix}/lib/os-release
install -d %{buildroot}%{_sysconfdir}
ln -s ../usr/lib/os-release %{buildroot}%{_sysconfdir}/os-release

%files
%{_prefix}/lib/os-release
%{_sysconfdir}/os-release
"""

DEVICE_RS = """pub fn image(os_release: &str) -> Option<String> {
        match key.trim() {
            "VILNA_IMAGE" if !value.is_empty() => return Some(value),
            "NAME" => name = Some(value),
            "VERSION_ID" => version = Some(value),
            _ => {}
        }
}
"""
GLOXI = '                if line.startswith("PRETTY_NAME="):\n'
MEMCHECK = '        if line.startswith("PRETTY_NAME="):\n'
PROTOCOL = """static IMAGE: LazyLock<Option<Regex>> =
    LazyLock::new(|| re(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}$"));
"""
OBS_SYNC = (
    '        extra=(("docs/design/logo/Vilna_logo_dark-bg.svg", "Vilna_logo_dark-bg.svg"),)\n'
)
INSTALLER_RELEASE = 'NAME = "Blavet"\n'
INSTALLER_START = 'RELEASE = "Blavet"\n'

# Frames of the boot logo: logo/ (4x4) and logo@2x/ (8x8).
FRAMES = ("vilna-logo-01.png", "vilna-logo-02.png", "vilna-logo-03.png")
PLY = "docs/design/plymouth"


def good_files() -> dict[str, str | bytes]:
    files: dict[str, str | bytes] = {
        "image/plymouth/vilna.plymouth": PLYMOUTH,
        "image/plymouth/vilna.script": SCRIPT,
        "image/plymouth/make_theme.py": MAKER,
        "image/plymouth/plymouthd.defaults": DEFAULTS,
        cb.PLYMOUTH_SPEC: PLYMOUTH_SPEC,
        cb.RELEASE_FILE: OS_RELEASE,
        cb.RELEASE_SPEC: RELEASE_SPEC,
        f"{cb.LOGOS_DIR}/vilna-logo.svg": "<svg/>",
        "docs/design/blavet/svg/blavet-mark-dark-bg.svg": "<svg/>",
        "reports/client/src/device.rs": DEVICE_RS,
        "gloxi/src/gloxi/check.py": GLOXI,
        "image/tools/memcheck.py": MEMCHECK,
        cb.PROTOCOL: PROTOCOL,
        cb.OBS_SYNC: OBS_SYNC,
        cb.INSTALLER_RELEASE: INSTALLER_RELEASE,
        cb.INSTALLER_START: INSTALLER_START,
        f"{PLY}/dots-6x2.png": png(6, 2),
        f"{PLY}/background-32x18.png": png(64, 36, colour=2),
        "docs/design/blavet/png/blavet-mark-dark-bg-16.png": png(16, 16),
    }
    for name in FRAMES:
        files[f"{PLY}/logo/{name}"] = png(4, 4)
        files[f"{PLY}/logo@2x/{name}"] = png(8, 8)
    return files


class Base(unittest.TestCase):
    """A temporary repo with the good files; tests change it and run the check."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for rel, content in good_files().items():
            self.put(rel, content)

    def put(self, rel: str, content: str | bytes) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.exists():
            path.unlink()
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_bytes(content)

    def drop(self, rel: str) -> None:
        (self.root / rel).unlink()

    def edit(self, rel: str, old: str, new: str) -> None:
        text = (self.root / rel).read_text(encoding="utf-8")
        self.assertIn(old, text, f"{rel} has no {old!r}")
        self.put(rel, text.replace(old, new, 1))

    def run_check(self) -> list[cb.Problem]:
        return cb.check_repo(self.root, cb.list_files(self.root))[0]

    def errors(self) -> list[str]:
        return [str(p) for p in self.run_check() if not p.warning]

    def warnings(self) -> list[str]:
        return [str(p) for p in self.run_check() if p.warning]

    def assertError(self, path: str, text: str) -> None:  # noqa: N802
        found = self.errors()
        self.assertTrue(
            any(e.startswith(path + ":") and text in e for e in found),
            f"no error '{path}: ... {text} ...' in {found}",
        )

    def assertWarning(self, path: str, text: str) -> None:  # noqa: N802
        found = self.warnings()
        self.assertTrue(
            any(w.startswith(path + ": warning:") and text in w for w in found),
            f"no warning '{path}: ... {text} ...' in {found}",
        )

    def assertClean(self) -> None:  # noqa: N802
        problems = [str(p) for p in self.run_check()]
        self.assertEqual(problems, [])


class GoodRepo(Base):
    def test_good_files_have_no_problems(self):
        self.assertClean()

    def test_checked_count_counts_files_looked_at(self):
        _, checked = cb.check_repo(self.root, cb.list_files(self.root))
        # theme, script, defaults, plymouth spec, os-release, release spec and 9 PNG files
        self.assertGreaterEqual(checked, 15)


# --- PNG ------------------------------------------------------------------------

PATH_FILE = f"{PLY}/dots-6x2.png"


class PngFile(Base):
    def set(self, data: bytes, rel: str = PATH_FILE) -> None:
        self.put(rel, data)

    def test_signature_wrong(self):
        self.set(b"GIF89a" + bytes(40))
        self.assertError(PATH_FILE, "wrong signature")

    def test_text_file_with_png_name(self):
        self.set(b"not a png\n")
        self.assertError(PATH_FILE, "wrong signature")

    def test_empty_file(self):
        self.set(b"")
        self.assertError(PATH_FILE, "wrong signature")

    def test_only_signature(self):
        self.set(cb.PNG_SIGNATURE)
        self.assertError(PATH_FILE, "no IEND")

    def test_cut_off_at_every_length(self):
        good = png(6, 2)
        for cut in range(len(good) - 1):
            self.set(good[:cut])
            self.assertTrue(self.errors(), f"cut at {cut} gave no error")

    def test_cut_inside_chunk_header(self):
        self.set(png(6, 2)[:-3])
        self.assertError(PATH_FILE, "cut off")

    def test_wrong_crc(self):
        good = png(6, 2)
        index = good.index(b"IDAT") + 6
        self.set(good[:index] + bytes([good[index] ^ 0xFF]) + good[index + 1 :])
        self.assertError(PATH_FILE, "wrong checksum")

    def test_wrong_crc_of_ihdr(self):
        data = (
            cb.PNG_SIGNATURE
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 6, 2, 8, 6, 0, 0, 0), crc=0)
            + chunk(b"IDAT", zlib.compress(pixels(6, 2)))
            + chunk(b"IEND", b"")
        )
        self.set(data)
        self.assertError(PATH_FILE, "IHDR chunk has a wrong checksum")

    def test_first_chunk_not_ihdr(self):
        data = cb.PNG_SIGNATURE + chunk(b"tIME", bytes(7)) + ihdr(6, 2)
        self.set(data + chunk(b"IDAT", zlib.compress(pixels(6, 2))) + chunk(b"IEND", b""))
        self.assertError(PATH_FILE, "first chunk is not IHDR")

    def test_second_ihdr(self):
        self.set(png(6, 2, before=ihdr(6, 2)))
        self.assertError(PATH_FILE, "second IHDR")

    def test_ihdr_wrong_length(self):
        data = cb.PNG_SIGNATURE + chunk(b"IHDR", bytes(12)) + chunk(b"IEND", b"")
        self.set(data)
        self.assertError(PATH_FILE, "IHDR chunk has a wrong length")

    def test_zero_width(self):
        self.set(cb.PNG_SIGNATURE + ihdr(0, 2) + chunk(b"IEND", b""))
        self.assertError(PATH_FILE, "width or height of 0")

    def test_zero_height(self):
        self.set(cb.PNG_SIGNATURE + ihdr(6, 0) + chunk(b"IEND", b""))
        self.assertError(PATH_FILE, "width or height of 0")

    def test_width_above_two_to_the_31(self):
        self.set(cb.PNG_SIGNATURE + ihdr(0x80000000, 2) + chunk(b"IEND", b""))
        self.assertError(PATH_FILE, "width or height of 0")

    def test_colour_and_depth_combinations(self):
        for colour, depth in ((0, 3), (2, 4), (3, 16), (4, 1), (6, 4), (1, 8), (5, 8), (7, 8)):
            data = cb.PNG_SIGNATURE + ihdr(6, 2, depth, colour) + chunk(b"IEND", b"")
            self.set(data)
            self.assertError(PATH_FILE, "colour type and bit depth")

    def test_every_allowed_colour_and_depth(self):
        for colour, depths in cb.DEPTHS.items():
            for depth in depths:
                plte = chunk(b"PLTE", bytes(3 * 2)) if colour == 3 else b""
                data = png(6, 2, colour=colour, depth=depth, before=plte)
                self.set(data)
                self.assertClean()

    def test_unknown_methods(self):
        bad = struct.pack(">IIBBBBB", 6, 2, 8, 6, 1, 0, 0)
        self.set(cb.PNG_SIGNATURE + chunk(b"IHDR", bad) + chunk(b"IEND", b""))
        self.assertError(PATH_FILE, "unknown compression")
        bad = struct.pack(">IIBBBBB", 6, 2, 8, 6, 0, 0, 2)
        self.set(cb.PNG_SIGNATURE + chunk(b"IHDR", bad) + chunk(b"IEND", b""))
        self.assertError(PATH_FILE, "unknown compression")

    def test_no_idat(self):
        self.set(cb.PNG_SIGNATURE + ihdr(6, 2) + chunk(b"IEND", b""))
        self.assertError(PATH_FILE, "no image data")

    def test_pixel_data_too_short(self):
        self.set(png(6, 2, raw=pixels(6, 1)))
        self.assertError(PATH_FILE, "smaller than the size")

    def test_pixel_data_too_long(self):
        self.set(png(6, 2, raw=pixels(6, 3)))
        self.assertError(PATH_FILE, "larger than the size")

    def test_pixel_data_not_zlib(self):
        data = cb.PNG_SIGNATURE + ihdr(6, 2) + chunk(b"IDAT", b"x" * 40) + chunk(b"IEND", b"")
        self.set(data)
        self.assertError(PATH_FILE, "not a valid zlib stream")

    def test_pixel_data_cut_off(self):
        packed = zlib.compress(pixels(6, 2))[:-6]
        data = cb.PNG_SIGNATURE + ihdr(6, 2) + chunk(b"IDAT", packed) + chunk(b"IEND", b"")
        self.set(data)
        self.assertError(PATH_FILE, "cut off")

    def test_pixel_data_with_a_second_zlib_stream(self):
        packed = zlib.compress(pixels(6, 2))
        data = cb.PNG_SIGNATURE + ihdr(6, 2) + chunk(b"IDAT", packed + packed)
        self.set(data + chunk(b"IEND", b""))
        self.assertError(PATH_FILE, "continues after the end")

    def test_idat_after_the_stream_ended(self):
        packed = zlib.compress(pixels(6, 2))
        data = cb.PNG_SIGNATURE + ihdr(6, 2) + chunk(b"IDAT", packed) + chunk(b"IDAT", b"x")
        self.set(data + chunk(b"IEND", b""))
        self.assertError(PATH_FILE, "continues after the end")

    def test_unknown_filter_type(self):
        raw = (b"\x09" + bytes(24)) + (b"\x00" + bytes(24))
        self.set(png(6, 2, raw=raw))
        self.assertError(PATH_FILE, "unknown filter type")

    def test_filter_types_0_to_4_are_fine(self):
        raw = b"".join(bytes([f]) + bytes(24) for f in (0, 1, 2, 3, 4))
        self.set(png(6, 5, raw=raw), "image/plymouth/x.png")
        self.assertEqual(self.errors(), [])

    def test_idat_split_in_many_chunks(self):
        self.set(png(6, 2, split=5))
        self.assertClean()

    def test_idat_chunks_must_follow_each_other(self):
        packed = zlib.compress(pixels(6, 2))
        data = cb.PNG_SIGNATURE + ihdr(6, 2) + chunk(b"IDAT", packed[:5])
        data += chunk(b"tIME", bytes(7)) + chunk(b"IDAT", packed[5:]) + chunk(b"IEND", b"")
        self.set(data)
        self.assertError(PATH_FILE, "not one after the other")

    def test_interlaced_image(self):
        for w, h in ((1, 1), (2, 2), (5, 3), (6, 2), (9, 9), (33, 17)):
            raw = adam7_pixels(w, h)
            data = cb.PNG_SIGNATURE + ihdr(w, h, interlace=1)
            data += chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
            self.set(data, "image/plymouth/x.png")
            self.assertEqual(
                [e for e in self.errors() if "image/plymouth/x.png" in e], [], f"{w}x{h}"
            )

    def test_interlaced_image_with_wrong_size(self):
        data = cb.PNG_SIGNATURE + ihdr(9, 9, interlace=1)
        data += chunk(b"IDAT", zlib.compress(pixels(9, 9))) + chunk(b"IEND", b"")
        self.set(data)
        self.assertError(PATH_FILE, "the size in the header")

    def test_palette_type_needs_plte(self):
        self.set(png(6, 2, colour=3, depth=8))
        self.assertError(PATH_FILE, "no PLTE")

    def test_palette_with_gray_type(self):
        self.set(png(6, 2, colour=0, before=chunk(b"PLTE", bytes(6))))
        self.assertError(PATH_FILE, "palette, which this colour type must not have")

    def test_palette_wrong_length(self):
        self.set(png(6, 2, colour=3, before=chunk(b"PLTE", bytes(4))))
        self.assertError(PATH_FILE, "PLTE chunk has a wrong length")

    def test_chunk_with_invalid_type(self):
        self.set(png(6, 2, before=chunk(b"I1AT", b"")))
        self.assertError(PATH_FILE, "invalid type")

    def test_chunk_length_above_the_file(self):
        bad = struct.pack(">I", 0xFFFFFFFF) + b"tEXt" + b"abc"
        self.set(png(6, 2, before=bad))
        self.assertError(PATH_FILE, "cut off inside a tEXt chunk")

    def test_no_iend(self):
        self.set(png(6, 2, end=False))
        self.assertError(PATH_FILE, "no IEND")

    def test_bytes_after_iend_are_a_warning(self):
        self.set(png(6, 2, after=b"junk"))
        self.assertEqual(self.errors(), [])
        self.assertWarning(PATH_FILE, "4 bytes after the IEND")

    def test_a_zip_bomb_is_stopped(self):
        # 4x4 pixels, but the data inflates to 200 MB; the check stops after one block.
        packed = zlib.compress(bytes(200 << 20), 1)
        data = cb.PNG_SIGNATURE + ihdr(4, 4) + chunk(b"IDAT", packed) + chunk(b"IEND", b"")
        self.set(data)
        start = time.monotonic()
        self.assertError(PATH_FILE, "larger than the size")
        self.assertLess(time.monotonic() - start, 20)

    def test_a_huge_size_in_the_header_is_not_allocated(self):
        data = cb.PNG_SIGNATURE + ihdr(0x7FFFFFFF, 0x7FFFFFFF) + chunk(b"IEND", b"")
        self.set(data)
        self.assertError(PATH_FILE, "pixels, not checked further")

    def test_the_pixel_limit_is_in_pixels(self):
        info, problems = cb.check_png("a.png", png(5000, 5000, raw=b"x"))
        self.assertEqual(info.width, 5000)
        self.assertNotIn("not checked further", " ".join(map(str, problems)))
        _, problems = cb.check_png("a.png", png(5001, 5000, raw=b"x"))
        self.assertIn("not checked further", " ".join(map(str, problems)))

    def test_the_decoder_is_not_confused_by_random_damage(self):
        good = png(6, 2, before=text_chunk("a") + ztxt_chunk("b") + itxt_chunk("c"))
        rng = random.Random(57)
        for _ in range(400):
            data = bytearray(good)
            for _ in range(rng.randint(1, 4)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            cb.check_png("a.png", bytes(data))  # must not raise

    def test_every_single_byte_flip_is_handled(self):
        good = png(3, 2, before=text_chunk("a") + itxt_chunk("c", compressed=True))
        for index in range(len(good)):
            for value in (0, 0xFF):
                data = bytearray(good)
                data[index] = value
                cb.check_png("a.png", bytes(data))  # must not raise


class PngText(Base):
    def set(self, *chunks: bytes) -> None:
        self.put(PATH_FILE, png(6, 2, before=b"".join(chunks)))

    def test_home_path_in_text(self):
        self.set(text_chunk("exported to /home/alice/Pictures/logo.png"))
        self.assertError(PATH_FILE, "file path (personal data)")

    def test_message_never_repeats_the_text(self):
        self.set(text_chunk("/home/alice/secret-project/logo.png", keyword="/home/bob/key"))
        message = " ".join(self.errors())
        for word in ("alice", "bob", "secret", "/home"):
            self.assertNotIn(word, message)

    def test_paths_of_every_kind(self):
        samples = (
            "/home/alice/a.png",
            "/Users/alice/a.png",
            "/root/a.png",
            "/tmp/build/a.png",
            "/var/tmp/a.png",
            "/opt/tool/a.png",
            "/srv/data",
            "C:\\Users\\Alice\\a.png",
            "d:/work/a.png",
            "\\\\server\\share\\a.png",
            "~/Pictures/a.png",
            "~\\Pictures\\a.png",
            "file:///home/a/a.png",
            "see /root/",
            "from /tmp/",
            "kept in /home/",
            "at /Users/",
            "made at /a/b",
            "Source: /mnt/disk/x",
        )
        for sample in samples:
            self.set(text_chunk(sample))
            self.assertError(PATH_FILE, "file path")

    def test_path_in_ztxt_and_itxt(self):
        for one in (
            ztxt_chunk("saved as /home/alice/a.png"),
            itxt_chunk("saved as /home/alice/a.png"),
            itxt_chunk("saved as /home/alice/a.png", compressed=True),
        ):
            self.set(one)
            self.assertError(PATH_FILE, "file path")

    def test_path_in_the_keyword(self):
        self.set(chunk(b"tEXt", b"/home/alice/x\x00hello"))
        self.assertError(PATH_FILE, "file path")

    def test_path_after_other_chunks(self):
        self.set(text_chunk("fine"), text_chunk("/home/alice/x"))
        self.assertError(PATH_FILE, "file path")

    def test_harmless_text_is_a_warning(self):
        self.set(text_chunk("Created by the Vilna team"))
        self.assertEqual(self.errors(), [])
        self.assertWarning(PATH_FILE, "1 text chunk(s)")

    def test_urls_and_words_are_not_paths(self):
        for sample in (
            "https://example.org/a/b/c.png",
            "http://example.org/a/b",
            "Software: GIMP 2.10 and/or Inkscape",
            "CC-BY-SA-4.0",
            "1/2 and 3/4",
            "image/png",
            "docs/design/plymouth",
            "a:b",
        ):
            self.set(text_chunk(sample))
            self.assertEqual(self.errors(), [], sample)

    def test_unreadable_text_chunks_do_not_crash(self):
        self.set(chunk(b"zTXt", b"Comment\x00\x00not zlib"), chunk(b"iTXt", b"Comment\x00"))
        self.assertEqual(self.errors(), [])
        self.set(chunk(b"iTXt", b"Comment\x00\x01\x05\x00\x00x"), chunk(b"tEXt", b""))
        self.assertEqual(self.errors(), [])

    def test_text_bomb_is_cut(self):
        packed = zlib.compress(b"/home/alice/" + bytes(100 << 20), 1)
        self.set(chunk(b"zTXt", b"Comment\x00\x00" + packed))
        self.assertError(PATH_FILE, "file path")

    def test_one_megabyte_of_slashes_is_linear(self):
        self.set(text_chunk("/" * (1 << 20)))
        start = time.monotonic()
        self.errors()
        self.assertLess(time.monotonic() - start, 10)

    def test_exif_is_a_warning(self):
        self.set(chunk(b"eXIf", b"MM\x00*\x00\x00\x00\x08"))
        self.assertEqual(self.errors(), [])
        self.assertWarning(PATH_FILE, "eXIf")

    def test_other_ancillary_chunks_are_fine(self):
        self.set(chunk(b"gAMA", bytes(4)), chunk(b"pHYs", bytes(9)), chunk(b"tIME", bytes(7)))
        self.assertClean()


class PngSizes(Base):
    def test_size_in_the_name_equal(self):
        self.put(f"{PLY}/dots-6x2.png", png(6, 2))
        self.assertClean()

    def test_size_in_the_name_whole_multiple(self):
        for factor in (2, 3, 10):
            self.put(f"{PLY}/dots-6x2.png", png(6 * factor, 2 * factor))
            self.assertClean()

    def test_size_in_the_name_not_equal(self):
        self.put(f"{PLY}/dots-6x2.png", png(7, 2))
        self.assertError(f"{PLY}/dots-6x2.png", "not a whole multiple of the 6x2")

    def test_size_in_the_name_smaller(self):
        self.put(f"{PLY}/dots-6x2.png", png(3, 1))
        self.assertError(f"{PLY}/dots-6x2.png", "not a whole multiple")

    def test_size_in_the_name_two_different_factors(self):
        self.put(f"{PLY}/dots-6x2.png", png(12, 2))
        self.assertError(f"{PLY}/dots-6x2.png", "not a whole multiple")

    def test_size_in_the_name_swapped(self):
        self.put(f"{PLY}/dots-6x2.png", png(2, 6))
        self.assertError(f"{PLY}/dots-6x2.png", "not a whole multiple")

    def test_size_zero_in_the_name(self):
        self.put(f"{PLY}/dots-0x2.png", png(6, 2))
        self.assertError(f"{PLY}/dots-0x2.png", "size of 0 in its name")

    def test_side_in_the_name_of_a_blavet_icon(self):
        rel = "docs/design/blavet/png/blavet-mark-dark-bg-16.png"
        self.put(rel, png(16, 16))
        self.assertClean()
        self.put(rel, png(16, 32))
        self.assertError(rel, "not a whole multiple of the 16x16")
        self.put(rel, png(15, 15))
        self.assertError(rel, "not a whole multiple of the 16x16")

    def test_a_number_in_the_name_elsewhere_is_not_a_size(self):
        self.put(f"{PLY}/something-99.png", png(4, 4))
        self.assertClean()

    def test_png_outside_the_branding_folders_is_not_looked_at(self):
        self.put("site/public/shot-10x10.png", b"not a png")
        self.assertClean()

    def test_upper_case_extension_is_checked(self):
        self.put(f"{PLY}/LOGO.PNG", b"not a png")
        self.assertError(f"{PLY}/LOGO.PNG", "wrong signature")


class Frames(Base):
    def test_gap(self):
        self.drop(f"{PLY}/logo/vilna-logo-02.png")
        self.drop(f"{PLY}/logo@2x/vilna-logo-02.png")
        self.assertError(f"{PLY}/logo", "vilna-logo-2.png is missing")

    def test_other_size_in_a_folder(self):
        self.put(f"{PLY}/logo/vilna-logo-03.png", png(5, 5))
        self.assertError(f"{PLY}/logo/vilna-logo-03.png", "another size")

    def test_not_square_is_a_warning(self):
        for folder, size in (("logo", (4, 5)), ("logo@2x", (8, 10))):
            for name in FRAMES:
                self.put(f"{PLY}/{folder}/{name}", png(*size))
        self.assertEqual(self.errors(), [])
        self.assertWarning(f"{PLY}/logo/vilna-logo-01.png", "not square")

    def test_more_frames_than_the_script_plays(self):
        self.put(f"{PLY}/logo/vilna-logo-04.png", png(4, 4))
        self.put(f"{PLY}/logo@2x/vilna-logo-04.png", png(8, 8))
        self.assertError(f"{PLY}/logo", "4 frames, the script plays at most 3")

    def test_the_limit_is_read_from_the_script(self):
        self.edit("image/plymouth/vilna.script", "MAX_LOGO_FRAMES = 3", "MAX_LOGO_FRAMES = 4")
        self.edit("image/plymouth/make_theme.py", "MAX_FRAMES = 3", "MAX_FRAMES = 4")
        self.put(f"{PLY}/logo/vilna-logo-04.png", png(4, 4))
        self.put(f"{PLY}/logo@2x/vilna-logo-04.png", png(8, 8))
        self.assertClean()

    def test_two_frames_with_one_number(self):
        self.put(f"{PLY}/logo/vilna-logo-1.png", png(4, 4))
        self.assertError(f"{PLY}/logo", "same number")

    def test_no_frames_at_all(self):
        for folder in ("logo", "logo@2x"):
            for name in FRAMES:
                self.drop(f"{PLY}/{folder}/{name}")
        self.assertError(cb.FRAMES_DIR, "no PNG frames")

    def test_one_frame_is_a_group_of_one(self):
        for folder in ("logo", "logo@2x"):
            for name in FRAMES[1:]:
                self.drop(f"{PLY}/{folder}/{name}")
        self.assertClean()


class DoubleSize(Base):
    def test_2x_file_missing(self):
        self.drop(f"{PLY}/logo@2x/vilna-logo-02.png")
        self.assertError(f"{PLY}/logo@2x/vilna-logo-02.png", "is missing")

    def test_2x_file_without_normal_file(self):
        self.put(f"{PLY}/logo@2x/extra.png", png(8, 8))
        self.assertError(f"{PLY}/logo@2x/extra.png", "no file of this name")

    def test_2x_file_wrong_size(self):
        self.put(f"{PLY}/logo@2x/vilna-logo-02.png", png(9, 9))
        self.assertError(f"{PLY}/logo@2x/vilna-logo-02.png", "not twice 4x4")

    def test_2x_file_same_size_as_normal(self):
        self.put(f"{PLY}/logo@2x/vilna-logo-02.png", png(4, 4))
        self.assertError(f"{PLY}/logo@2x/vilna-logo-02.png", "not twice 4x4")

    def test_2x_folder_without_a_normal_folder_is_not_compared(self):
        self.put("docs/design/blavet/icons@2x/a.png", png(8, 8))
        self.assertClean()


class Files(Base):
    """Symbolic links, odd files and sizes."""

    def test_symlink_to_a_png_outside_is_not_followed(self):
        outside = self.root.parent / "outside-d57.png"
        outside.write_bytes(png(6, 2))
        self.addCleanup(outside.unlink)
        rel = f"{PLY}/dots-6x2.png"
        (self.root / rel).unlink()
        (self.root / rel).symlink_to(outside)
        self.assertError(rel, "symbolic link")

    def test_symlink_to_itself(self):
        rel = f"{PLY}/dots-6x2.png"
        (self.root / rel).unlink()
        (self.root / rel).symlink_to(self.root / rel)
        self.assertError(rel, "symbolic link")

    def test_symlink_for_os_release(self):
        outside = self.root.parent / "outside-os-release-d57"
        outside.write_text(OS_RELEASE)
        self.addCleanup(outside.unlink)
        (self.root / cb.RELEASE_FILE).unlink()
        (self.root / cb.RELEASE_FILE).symlink_to(outside)
        self.assertError(cb.RELEASE_FILE, "symbolic link")

    def test_symlink_loop_between_folders(self):
        (self.root / "loop").mkdir()
        (self.root / "loop" / "back").symlink_to(self.root, target_is_directory=True)
        self.assertClean()

    def test_dangling_symlink(self):
        rel = f"{PLY}/dots-6x2.png"
        (self.root / rel).unlink()
        (self.root / rel).symlink_to(self.root / "nowhere")
        self.assertError(rel, "symbolic link")

    def test_fifo_is_not_opened(self):
        rel = f"{PLY}/dots-6x2.png"
        (self.root / rel).unlink()
        os.mkfifo(self.root / rel)
        self.assertError(rel, "not a regular file")

    def test_file_of_70_megabytes_is_not_read(self):
        rel = f"{PLY}/dots-6x2.png"
        with open(self.root / rel, "r+b") as handle:
            handle.truncate(70 << 20)
        with mock.patch("builtins.open", side_effect=AssertionError("opened")):
            problems = [str(p) for p in cb.check_repo(self.root, [rel])[0]]
        self.assertTrue(any("larger than" in p and rel in p for p in problems), problems)

    def test_size_limit_of_text_files(self):
        self.put(cb.RELEASE_FILE, "A=" + "x" * (cb.TEXT_LIMIT + 10))
        self.assertError(cb.RELEASE_FILE, "larger than")

    def test_unreadable_file(self):
        rel = f"{PLY}/dots-6x2.png"
        (self.root / rel).chmod(0)
        if os.access(self.root / rel, os.R_OK):
            self.skipTest("the tests run with rights that ignore file modes")  # root: not a bug
        self.assertError(rel, "cannot read file")

    def test_names_with_spaces_and_unicode(self):
        self.put(f"{PLY}/Мій логотип 1x1.png", png(1, 1))
        self.put(f"{PLY}/ with space .png", b"bad")
        self.assertError(f"{PLY}/ with space .png", "wrong signature")
        self.assertEqual([e for e in self.errors() if "Мій" in e], [])

    def test_a_missing_tracked_file_is_reported_not_raised(self):
        rel = f"{PLY}/dots-6x2.png"
        self.drop(rel)
        problems = cb.check_repo(self.root, [rel])[0]
        self.assertTrue(any("file is missing" in str(p) and rel in str(p) for p in problems))


class TextEncoding(Base):
    FILES = (
        "image/plymouth/vilna.plymouth",
        "image/plymouth/vilna.script",
        "image/plymouth/plymouthd.defaults",
        cb.RELEASE_FILE,
    )

    def test_utf8_bom(self):
        for rel in self.FILES:
            self.setUp()
            text = (self.root / rel).read_text(encoding="utf-8")
            self.put(rel, b"\xef\xbb\xbf" + text.encode())
            self.assertError(rel, "byte order mark")

    def test_utf16(self):
        for rel in self.FILES:
            self.setUp()
            text = (self.root / rel).read_text(encoding="utf-8")
            self.put(rel, text.encode("utf-16"))
            self.assertError(rel, "UTF-16")
            self.put(rel, text.encode("utf-16-be"))
            self.assertTrue(self.errors(), rel)

    def test_not_utf8(self):
        for rel in self.FILES:
            self.setUp()
            self.put(rel, b"A=\xff\xfe\xfd\n")
            self.assertTrue(self.errors(), rel)

    def test_crlf(self):
        for rel in self.FILES:
            self.setUp()
            text = (self.root / rel).read_text(encoding="utf-8")
            self.put(rel, text.replace("\n", "\r\n").encode())
            self.assertError(rel, "CR characters")

    def test_nul(self):
        for rel in self.FILES:
            self.setUp()
            self.put(rel, b"A=1\x00\n")
            self.assertError(rel, "NUL")

    def test_no_final_newline_is_fine(self):
        for rel in self.FILES:
            self.setUp()
            text = (self.root / rel).read_text(encoding="utf-8")
            self.put(rel, text.rstrip("\n"))
            self.assertClean()

    def test_empty_files_are_reported_not_raised(self):
        for rel in self.FILES:
            self.setUp()
            self.put(rel, b"")
            self.assertTrue(self.errors(), rel)


# --- Plymouth ---------------------------------------------------------------------

THEME = "image/plymouth/vilna.plymouth"
SCRIPT_FILE = "image/plymouth/vilna.script"
DEFAULTS_FILE = "image/plymouth/plymouthd.defaults"


class Theme(Base):
    def test_no_theme_file(self):
        self.drop(THEME)
        self.assertError("image/plymouth", "no .plymouth file")

    def test_missing_keys(self):
        for key in ("Name", "Description", "ModuleName"):
            self.setUp()
            lines = [ln for ln in PLYMOUTH.splitlines() if not ln.startswith(key + "=")]
            self.put(THEME, "\n".join(lines) + "\n")
            self.assertError(THEME, f"[Plymouth Theme] has no {key}")

    def test_missing_script_keys(self):
        for key in ("ImageDir", "ScriptFile"):
            self.setUp()
            lines = [ln for ln in PLYMOUTH.splitlines() if not ln.startswith(key + "=")]
            self.put(THEME, "\n".join(lines) + "\n")
            self.assertError(THEME, f"[script] has no {key}")

    def test_empty_value(self):
        self.edit(THEME, "Description=A test theme", "Description=")
        self.assertError(THEME, "Description is empty")

    def test_no_script_section(self):
        self.put(THEME, "[Plymouth Theme]\nName=Vilna\nDescription=x\nModuleName=script\n")
        self.assertError(THEME, "[script] has no ImageDir")

    def test_other_module(self):
        self.edit(THEME, "ModuleName=script", "ModuleName=two-step")
        self.assertError(THEME, "ModuleName=two-step")

    def test_name_differs_from_file(self):
        self.edit(THEME, "Name=Vilna", "Name=Other")
        self.assertError(THEME, "does not match the file name")

    def test_name_case_is_ignored(self):
        self.edit(THEME, "Name=Vilna", "Name=VILNA")
        self.assertClean()

    def test_script_file_not_tracked(self):
        self.drop(SCRIPT_FILE)
        self.assertError(THEME, "vilna.script is not tracked next to it")

    def test_script_file_other_name(self):
        self.edit(THEME, "themes/vilna/vilna.script", "themes/vilna/other.script")
        self.assertError(THEME, "other.script is not tracked")

    def test_script_file_wrong_extension(self):
        self.edit(THEME, "vilna.script", "vilna.txt")
        self.assertError(THEME, "does not end in .script")

    def test_script_file_outside_image_dir(self):
        self.edit(THEME, "ScriptFile=/usr/share/plymouth/themes/vilna/", "ScriptFile=/tmp/")
        self.assertError(THEME, "ScriptFile is not inside ImageDir")

    def test_image_dir_not_under_themes(self):
        self.edit(THEME, "ImageDir=/usr/share/plymouth/themes/vilna", "ImageDir=/opt/vilna")
        self.assertError(THEME, "ImageDir is not a folder under /usr/share/plymouth/themes")

    def test_image_dir_with_dotdot(self):
        self.edit(
            THEME,
            "ImageDir=/usr/share/plymouth/themes/vilna",
            "ImageDir=/usr/share/plymouth/themes/../../../etc/vilna",
        )
        self.assertError(THEME, "ImageDir is not a folder under")

    def test_image_dir_other_theme(self):
        self.edit(
            THEME,
            "ImageDir=/usr/share/plymouth/themes/vilna",
            "ImageDir=/usr/share/plymouth/themes/x",
        )
        self.assertError(THEME, "ImageDir does not end in vilna")

    def test_image_dir_with_trailing_slash_is_fine(self):
        self.edit(THEME, "themes/vilna\n", "themes/vilna/\n")
        self.assertClean()

    def test_ini_with_duplicate_key(self):
        self.edit(THEME, "Name=Vilna", "Name=Vilna\nName=Vilna")
        self.assertError(THEME, "cannot be parsed")

    def test_ini_without_section(self):
        self.put(THEME, "Name=Vilna\n")
        self.assertError(THEME, "cannot be parsed")

    def test_ini_error_does_not_repeat_the_path(self):
        self.put(THEME, "Name=Vilna\n")
        message = " ".join(self.errors())
        self.assertNotIn(str(self.root), message)

    def test_keys_are_case_sensitive(self):
        self.edit(THEME, "ScriptFile=", "scriptfile=")
        self.assertError(THEME, "[script] has no ScriptFile")


class Defaults(Base):
    def test_missing_file(self):
        self.drop(DEFAULTS_FILE)
        self.assertError(DEFAULTS_FILE, "file is missing")

    def test_no_daemon_section(self):
        self.put(DEFAULTS_FILE, "[Other]\nTheme=vilna\n")
        self.assertError(DEFAULTS_FILE, "no [Daemon] section")

    def test_no_theme(self):
        self.edit(DEFAULTS_FILE, "Theme=vilna\n", "")
        self.assertError(DEFAULTS_FILE, "no Theme")

    def test_other_theme(self):
        self.edit(DEFAULTS_FILE, "Theme=vilna", "Theme=bgrt")
        self.assertError(DEFAULTS_FILE, "Theme=bgrt is not a theme of the repo")

    def test_no_show_delay(self):
        self.edit(DEFAULTS_FILE, "ShowDelay=0\n", "")
        self.assertError(DEFAULTS_FILE, "ShowDelay is missing")

    def test_no_device_timeout(self):
        self.edit(DEFAULTS_FILE, "DeviceTimeout=8\n", "")
        self.assertError(DEFAULTS_FILE, "DeviceTimeout is missing")

    def test_values_not_numbers(self):
        for bad in ("abc", "", "1e", "0x5", "--1"):
            self.setUp()
            self.edit(DEFAULTS_FILE, "DeviceTimeout=8", f"DeviceTimeout={bad}")
            self.assertError(DEFAULTS_FILE, "DeviceTimeout is missing or not a number")

    def test_nan(self):
        for bad in ("nan", "NaN", "-nan"):
            self.setUp()
            self.edit(DEFAULTS_FILE, "ShowDelay=0", f"ShowDelay={bad}")
            self.assertError(DEFAULTS_FILE, "ShowDelay=")

    def test_zero_device_timeout_is_not_allowed(self):
        self.edit(DEFAULTS_FILE, "DeviceTimeout=8", "DeviceTimeout=0")
        self.assertError(DEFAULTS_FILE, "DeviceTimeout=0 is out of range")

    def test_negative_values(self):
        self.edit(DEFAULTS_FILE, "ShowDelay=0", "ShowDelay=-1")
        self.assertError(DEFAULTS_FILE, "ShowDelay=-1 is out of range")

    def test_fractions_are_fine(self):
        self.edit(DEFAULTS_FILE, "DeviceTimeout=8", "DeviceTimeout=0.5")
        self.assertClean()


class ScriptImages(Base):
    def test_image_not_made_by_make_theme(self):
        self.edit(SCRIPT_FILE, 'Image("glow.png")', 'Image("halo.png")')
        self.assertError(SCRIPT_FILE, 'Image("halo.png") is not made by make_theme.py')

    def test_built_name_not_made(self):
        self.edit(SCRIPT_FILE, '"bloom-" + i', '"petal-" + i')
        self.assertError(SCRIPT_FILE, 'Image("petal-N.png")')

    def test_built_name_with_wrong_suffix(self):
        self.edit(SCRIPT_FILE, '+ ".png")', '+ ".jpg")')
        self.assertError(SCRIPT_FILE, "is not made by make_theme.py")

    def test_commented_out_images_are_ignored(self):
        self.edit(SCRIPT_FILE, "first =", '// Image("zzz.png")\n/* Image("yyy.png") */\nfirst =')
        self.assertClean()

    def test_hash_inside_a_string_is_not_a_comment(self):
        self.edit(SCRIPT_FILE, "first =", 'x = "#"; broken = Image("halo.png"); first =')
        self.assertError(SCRIPT_FILE, "halo.png")

    def test_image_after_a_hash_in_code_is_a_comment(self):
        self.edit(SCRIPT_FILE, "first =", 'x = 1; # Image("halo.png")\nfirst =')
        self.assertClean()

    def test_designer_frames_are_accepted(self):
        self.edit(SCRIPT_FILE, "first =", 'f = Image("logo-" + i + ".png");\nfirst =')
        self.assertClean()

    def test_make_theme_missing(self):
        self.drop("image/plymouth/make_theme.py")
        self.assertError(SCRIPT_FILE, "make_theme.py is missing")

    def test_frame_counts_differ(self):
        for name in ("CIRCLE_FRAMES", "BLOOM_FRAMES"):
            self.setUp()
            self.edit(SCRIPT_FILE, f"{name} = ", f"{name} = 1")
            self.assertError(SCRIPT_FILE, f"{name}=1")

    def test_max_frames_differ(self):
        self.edit("image/plymouth/make_theme.py", "MAX_FRAMES = 3", "MAX_FRAMES = 5")
        self.assertError(SCRIPT_FILE, "MAX_LOGO_FRAMES=3 but make_theme.py has MAX_FRAMES=5")

    def test_frame_count_missing_in_script(self):
        self.edit(SCRIPT_FILE, "BLOOM_FRAMES = 24;\n", "")
        self.assertError(SCRIPT_FILE, "BLOOM_FRAMES is not set")

    def test_frame_count_missing_in_make_theme(self):
        self.edit("image/plymouth/make_theme.py", "BLOOM_FRAMES = 24\n", "")
        self.assertError("image/plymouth/make_theme.py", "BLOOM_FRAMES is not set")


class PlymouthSpec(Base):
    def test_missing_spec(self):
        self.drop(cb.PLYMOUTH_SPEC)
        self.assertError(cb.PLYMOUTH_SPEC, "file is missing")

    def test_theme_not_installed(self):
        self.edit(cb.PLYMOUTH_SPEC, "vilna.plymouth vilna.script", "vilna.script")
        self.assertError(cb.PLYMOUTH_SPEC, "does not install vilna.plymouth")

    def test_script_not_installed(self):
        self.edit(cb.PLYMOUTH_SPEC, "vilna.plymouth vilna.script", "vilna.plymouth")
        self.assertError(cb.PLYMOUTH_SPEC, "does not install vilna.script")

    def test_defaults_not_installed(self):
        self.edit(cb.PLYMOUTH_SPEC, "install -D -m 0644 plymouthd.defaults", "true")
        self.assertError(cb.PLYMOUTH_SPEC, "does not install plymouthd.defaults")

    def test_install_in_another_section_does_not_count(self):
        self.edit(cb.PLYMOUTH_SPEC, "%install", "%check")
        self.assertError(cb.PLYMOUTH_SPEC, "does not install vilna.plymouth")

    def test_theme_folder_not_owned(self):
        self.edit(cb.PLYMOUTH_SPEC, "%{_datadir}/plymouth/themes/vilna/\n", "")
        self.assertError(cb.PLYMOUTH_SPEC, "%files does not own the theme folder vilna")

    def test_theme_folder_without_slash_is_owned(self):
        self.edit(cb.PLYMOUTH_SPEC, "themes/vilna/\n%{_datadir}", "themes/vilna\n%{_datadir}")
        self.assertClean()

    def test_defaults_not_owned(self):
        self.edit(
            cb.PLYMOUTH_SPEC,
            "%{_datadir}/plymouth/plymouthd.defaults\n\n%changelog",
            "\n%changelog",
        )
        self.assertError(cb.PLYMOUTH_SPEC, "does not own plymouthd.defaults")

    def test_script_plugin_required(self):
        self.edit(cb.PLYMOUTH_SPEC, "Requires:       plymouth-plugin-script\n", "")
        self.assertError(cb.PLYMOUTH_SPEC, "does not require plymouth-plugin-script")

    def test_label_plugin_required_for_text(self):
        self.edit(cb.PLYMOUTH_SPEC, "Requires:       plymouth-plugin-label\n", "")
        self.assertError(cb.PLYMOUTH_SPEC, "does not require plymouth-plugin-label")

    def test_label_plugin_not_needed_without_text(self):
        self.edit(cb.PLYMOUTH_SPEC, "Requires:       plymouth-plugin-label\n", "")
        self.edit(SCRIPT_FILE, "name = Image.Text(RELEASE_NAME, 1, 1, 1, 1);\n", "")
        self.assertClean()

    def test_label_plugin_not_needed_when_text_is_commented_out(self):
        self.edit(cb.PLYMOUTH_SPEC, "Requires:       plymouth-plugin-label\n", "")
        self.edit(SCRIPT_FILE, "name = Image.Text(", "# name = Image.Text(")
        self.assertClean()

    def test_must_provide_plymouth_branding(self):
        self.edit(cb.PLYMOUTH_SPEC, "Provides:       plymouth-branding\n", "")
        self.assertError(cb.PLYMOUTH_SPEC, "does not provide plymouth-branding")

    def test_rsvg_convert_in_build_requires(self):
        self.edit(cb.PLYMOUTH_SPEC, "BuildRequires:  rsvg-convert\n", "")
        self.assertError(cb.PLYMOUTH_SPEC, "BuildRequires lacks it")

    def test_python_in_build_requires(self):
        self.edit(cb.PLYMOUTH_SPEC, "BuildRequires:  python3\n", "")
        self.assertError(cb.PLYMOUTH_SPEC, "BuildRequires lacks python3")

    def test_logo_not_in_the_tarball(self):
        text = (self.root / cb.OBS_SYNC).read_text()
        self.put(cb.OBS_SYNC, text.replace("Vilna_logo_dark-bg.svg", "other.svg"))
        self.assertError(cb.PLYMOUTH_SPEC, "which obs/tools/obs_sync.py does not put")

    def test_missing_obs_sync_is_not_an_error(self):
        self.drop(cb.OBS_SYNC)
        self.assertClean()


# --- os-release ---------------------------------------------------------------------

OSR = cb.RELEASE_FILE


def without(field: str, text: str = OS_RELEASE) -> str:
    return "".join(ln + "\n" for ln in text.splitlines() if not ln.startswith(field + "="))


class OsRelease(Base):
    def test_each_identity_field_is_required(self):
        for name in cb.IDENTITY_FIELDS:
            self.setUp()
            self.put(OSR, without(name))
            self.assertError(OSR, f"{name} is missing or empty")

    def test_fields_read_by_the_reports_client_are_required(self):
        for name in ("NAME", "VERSION_ID"):
            self.setUp()
            self.put(OSR, without(name))
            self.assertError(OSR, f"{name} is missing or empty (used by")

    def test_message_says_who_reads_the_field(self):
        self.put(OSR, without("PRETTY_NAME"))
        self.assertError(OSR, "gloxi/src/gloxi/check.py")

    def test_a_field_a_new_reader_needs_is_required(self):
        self.edit("reports/client/src/device.rs", '"NAME" =>', '"BUILD_ID" => b = 1,\n "NAME" =>')
        self.assertError(OSR, "BUILD_ID is missing or empty (used by reports/client")

    def test_vilna_image_is_not_required(self):
        repo = cb.Repo(self.root, frozenset(good_files()))
        self.assertNotIn("VILNA_IMAGE", cb.required_fields(repo))
        self.assertClean()

    def test_empty_field(self):
        self.edit(OSR, 'NAME="Vilna"', 'NAME=""')
        self.assertError(OSR, "NAME is missing or empty")

    def test_blank_field(self):
        self.edit(OSR, 'LOGO="vilna-logo"', 'LOGO="  "')
        self.assertError(OSR, "LOGO is missing or empty")

    def test_missing_urls_are_warnings_while_legacy(self):
        text = OS_RELEASE
        for name in cb.URL_FIELDS:
            text = without(name, text)
        self.put(OSR, text)
        self.assertEqual(self.errors(), [])
        for name in cb.URL_FIELDS:
            self.assertWarning(OSR, f"{name} is not set")

    def test_missing_url_is_an_error_without_legacy(self):
        self.put(OSR, without("HOME_URL"))
        with mock.patch.object(cb, "LEGACY", frozenset()):
            self.assertError(OSR, "HOME_URL is not set")

    def test_url_must_be_https(self):
        for bad in ("http://example.org", "ftp://example.org", "example.org", "https://a b"):
            self.setUp()
            self.edit(OSR, 'HOME_URL="https://example.org/"', f'HOME_URL="{bad}"')
            self.assertError(OSR, "HOME_URL is not an https:// address")

    def test_url_with_user_info_is_refused(self):
        self.edit(OSR, 'HOME_URL="https://example.org/"', 'HOME_URL="https://user@example.org/"')
        self.assertError(OSR, "HOME_URL is not an https:// address")


class OsReleaseSyntax(Base):
    def test_line_without_equal_sign(self):
        self.put(OSR, OS_RELEASE + "NAME Vilna\n")
        self.assertError(OSR, "is not FIELD=value")

    def test_spaces_around_equal_sign(self):
        self.edit(OSR, 'ID="vilna"', 'ID = "vilna"')
        self.assertError(OSR, "is not FIELD=value")

    def test_lower_case_field_name(self):
        self.put(OSR, OS_RELEASE + 'name="x"\n')
        self.assertError(OSR, "is not FIELD=value")

    def test_indented_line(self):
        self.edit(OSR, 'ID="vilna"', ' ID="vilna"')
        self.assertError(OSR, "is not FIELD=value")

    def test_unclosed_quote(self):
        self.edit(OSR, 'NAME="Vilna"', 'NAME="Vilna')
        self.assertError(OSR, "NAME has an opening quote without a closing one")

    def test_text_after_the_closing_quote(self):
        self.edit(OSR, 'NAME="Vilna"', 'NAME="Vilna" more')
        self.assertError(OSR, "NAME has text after the closing quote")

    def test_unquoted_value_with_spaces(self):
        self.edit(OSR, 'NAME="Vilna"', "NAME=Vilna OS")
        self.assertError(OSR, "NAME has an unquoted value with spaces")

    def test_unquoted_value_with_shell_characters(self):
        self.edit(OSR, 'ANSI_COLOR="0;38;2;0;87;184"', "ANSI_COLOR=0;38;2;0;87;184")
        self.assertError(OSR, "ANSI_COLOR has an unquoted value with shell characters")

    def test_unquoted_and_single_quoted_values_are_fine(self):
        self.edit(OSR, 'ID="vilna"', "ID=vilna")
        self.edit(OSR, 'NAME="Vilna"', "NAME='Vilna'")
        self.assertClean()

    def test_comment_after_a_quoted_value_is_fine(self):
        self.edit(OSR, 'ID="vilna"', 'ID="vilna" # the id')
        self.assertClean()

    def test_duplicate_field(self):
        self.put(OSR, OS_RELEASE + 'NAME="Other"\n')
        self.assertError(OSR, "NAME is set twice")

    def test_escaped_quote_inside_a_value(self):
        self.edit(OSR, 'PRETTY_NAME="Vilna', 'PRETTY_NAME="Vi\\"lna')
        self.assertEqual([e for e in self.errors() if "FIELD" in e or "quote" in e], [])

    def test_empty_lines_and_comments_are_fine(self):
        self.put(OSR, "\n\n# x\n   # y\n" + OS_RELEASE + "\n\n")
        self.assertClean()

    def test_file_of_only_comments(self):
        self.put(OSR, "# nothing\n")
        self.assertError(OSR, "NAME is missing or empty")

    def test_hostile_values_do_not_crash(self):
        for text in (
            "A=" + '"' * 5000 + "\n",
            "A=" + "\\" * 5001 + "\n",
            "=\n",
            "\n".join(f"K{i}=v" for i in range(5000)),
            'NAME="' + "x" * 100000 + '"\n',
            "NAME=\x1b[31mred\x1b[0m\n",
        ):
            self.put(OSR, text)
            self.assertTrue(self.errors())


class OsReleaseValues(Base):
    def test_id_must_be_lower_case(self):
        self.edit(OSR, 'ID="vilna"', 'ID="Vilna"')
        self.assertError(OSR, "ID must be lower-case")

    def test_codename_must_be_a_lower_case_word(self):
        for bad in ("Blavet", "bla vet", "1blavet", ""):
            self.setUp()
            self.edit(OSR, 'VERSION_CODENAME="blavet"', f'VERSION_CODENAME="{bad}"')
            self.assertTrue(self.errors(), bad)

    def test_ansi_color(self):
        for bad in ("blue", "1;", ";1", "1;;2", "1;a"):
            self.setUp()
            self.edit(OSR, 'ANSI_COLOR="0;38;2;0;87;184"', f'ANSI_COLOR="{bad}"')
            self.assertError(OSR, "ANSI_COLOR must be numbers separated by ;")

    def test_id_like(self):
        self.edit(OSR, 'ID_LIKE="opensuse-tumbleweed opensuse suse"', 'ID_LIKE="SUSE  x"')
        self.assertError(OSR, "ID_LIKE must be IDs separated by spaces")

    def test_logo_must_be_an_icon_name(self):
        self.edit(OSR, 'LOGO="vilna-logo"', 'LOGO="../vilna-logo"')
        self.assertError(OSR, "LOGO must be an icon name")

    def test_logo_icon_must_exist(self):
        self.drop(f"{cb.LOGOS_DIR}/vilna-logo.svg")
        self.assertError(OSR, "LOGO=vilna-logo is not an icon of obs/packages/distribution-logos")

    def test_control_characters(self):
        self.edit(OSR, 'LOGO="vilna-logo"', 'LOGO="vilna-logo\x1b"')
        self.assertError(OSR, "LOGO has a control character")

    def test_version_id_with_space(self):
        self.edit(OSR, 'VERSION_ID="@VERSION@"', 'VERSION_ID="1 2"')
        self.assertError(OSR, "VERSION_ID must be letters, digits")

    def test_name_and_id_differ_is_a_warning(self):
        self.edit(OSR, 'NAME="Vilna"', 'NAME="Valna"')
        self.assertWarning(OSR, "NAME and ID are not the same word")

    def test_version_not_starting_with_version_id_is_a_warning(self):
        self.edit(OSR, 'VERSION="@VERSION@ Blavet', 'VERSION="Blavet @VERSION@')
        self.assertWarning(OSR, "VERSION does not start with VERSION_ID")

    def test_pretty_name_differing_is_a_warning(self):
        self.edit(OSR, 'PRETTY_NAME="Vilna @VERSION@', 'PRETTY_NAME="Vilna OS @VERSION@')
        self.assertWarning(OSR, "PRETTY_NAME is not NAME and VERSION")

    def test_image_field_of_the_protocol(self):
        # The client sends NAME VERSION_ID; the server refuses what its pattern refuses.
        self.edit(OSR, 'NAME="Vilna"', 'NAME="Vilna é"')
        self.assertError(OSR, "do not fit the image field of the report protocol")

    def test_image_field_too_long(self):
        self.edit(OSR, 'NAME="Vilna"', 'NAME="' + "V" * 70 + '"')
        self.assertError(OSR, "do not fit the image field")

    def test_image_pattern_is_read_from_the_protocol(self):
        self.edit(cb.PROTOCOL, "{0,63}", "{0,3}")
        self.assertError(OSR, "do not fit the image field")

    def test_default_image_pattern_when_the_protocol_is_missing(self):
        self.drop(cb.PROTOCOL)
        self.edit(OSR, 'NAME="Vilna"', 'NAME="Vilna é"')
        self.assertError(OSR, "do not fit the image field")

    def test_broken_pattern_in_the_protocol_falls_back(self):
        self.edit(cb.PROTOCOL, "[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}", "(")
        self.assertClean()

    def test_unknown_placeholder(self):
        self.edit(OSR, 'LOGO="vilna-logo"', 'LOGO="vilna-logo-@CODENAME@"')
        self.assertError(OSR, "placeholder @CODENAME@")

    def test_placeholder_without_spec_version(self):
        self.edit(cb.RELEASE_SPEC, "Version:        0.4\n", "")
        self.assertError(cb.RELEASE_SPEC, "has no Version")

    def test_version_of_the_spec_is_put_in(self):
        self.edit(cb.RELEASE_SPEC, "Version:        0.4", "Version:        0.4 beta")
        self.assertClean()  # the first word is the version

    def test_version_of_the_spec_is_checked_in_the_value(self):
        self.edit(cb.RELEASE_SPEC, "Version:        0.4", "Version:        0.4/x")
        self.assertError(OSR, "VERSION_ID must be letters, digits")


class ReleaseSpec(Base):
    def test_missing_spec(self):
        self.drop(cb.RELEASE_SPEC)
        self.assertError(cb.RELEASE_SPEC, "file is missing")

    def test_placeholder_not_replaced(self):
        self.edit(cb.RELEASE_SPEC, "sed 's/@VERSION@/%{version}/g'", "cp")
        self.assertError(cb.RELEASE_SPEC, "%build does not replace @VERSION@")

    def test_no_placeholder_no_sed_needed(self):
        self.put(OSR, OS_RELEASE.replace("@VERSION@", "0.4"))
        self.edit(cb.RELEASE_SPEC, "sed 's/@VERSION@/%{version}/g'", "cp")
        self.assertClean()

    def test_os_release_not_installed(self):
        self.edit(cb.RELEASE_SPEC, "install -D -m 0644 os-release", "true")
        self.assertError(cb.RELEASE_SPEC, "does not install os-release")

    def test_os_release_not_in_files(self):
        self.edit(
            cb.RELEASE_SPEC,
            "%files\n%{_prefix}/lib/os-release\n%{_sysconfdir}/os-release\n",
            "%files\n",
        )
        self.assertError(cb.RELEASE_SPEC, "does not install os-release")

    def test_link_missing(self):
        self.edit(cb.RELEASE_SPEC, "ln -s ../usr/lib/os-release", "cp ../usr/lib/os-release")
        self.assertError(cb.RELEASE_SPEC, "does not link /etc/os-release")

    def test_link_with_flags_is_fine(self):
        self.edit(cb.RELEASE_SPEC, "ln -s ../usr", "ln -sf ../usr")
        self.assertClean()


class ReleaseName(Base):
    def test_script_name_differs(self):
        self.edit(SCRIPT_FILE, 'RELEASE_NAME = "Blavet"', 'RELEASE_NAME = "Chaber"')
        self.assertError(SCRIPT_FILE, "RELEASE_NAME=Chaber but VERSION_CODENAME of os-release")

    def test_case_does_not_matter(self):
        self.edit(SCRIPT_FILE, 'RELEASE_NAME = "Blavet"', 'RELEASE_NAME = "BLAVET"')
        self.assertClean()

    def test_installer_name_differs(self):
        self.edit(cb.INSTALLER_RELEASE, '"Blavet"', '"Chaber"')
        self.assertError(cb.INSTALLER_RELEASE, "NAME=Chaber but VERSION_CODENAME")

    def test_installer_start_name_differs(self):
        self.edit(cb.INSTALLER_START, '"Blavet"', '"Chaber"')
        self.assertError(cb.INSTALLER_START, "RELEASE=Chaber but VERSION_CODENAME")

    def test_release_name_missing_in_script(self):
        self.edit(SCRIPT_FILE, 'RELEASE_NAME = "Blavet";\n', "")
        self.assertError(SCRIPT_FILE, "RELEASE_NAME is not set")

    def test_commented_release_name_does_not_count(self):
        self.edit(SCRIPT_FILE, 'RELEASE_NAME = "Blavet";', '# RELEASE_NAME = "Blavet";')
        self.assertError(SCRIPT_FILE, "RELEASE_NAME is not set")

    def test_installer_name_missing(self):
        self.put(cb.INSTALLER_RELEASE, "pass\n")
        self.assertError(cb.INSTALLER_RELEASE, "NAME is not set")

    def test_installer_files_missing_are_skipped(self):
        self.drop(cb.INSTALLER_RELEASE)
        self.drop(cb.INSTALLER_START)
        self.assertClean()

    def test_sign_of_the_release_must_exist(self):
        mark = "docs/design/blavet/svg/blavet-mark-dark-bg.svg"
        self.drop(mark)
        self.assertError(mark, "the sign of the release blavet is missing")

    def test_sign_in_the_plymouth_folder_is_enough(self):
        self.drop("docs/design/blavet/svg/blavet-mark-dark-bg.svg")
        self.put("image/plymouth/release/mark.svg", "<svg/>")
        self.assertClean()

    def test_new_release_needs_its_own_sign(self):
        for path in (OSR, SCRIPT_FILE, cb.INSTALLER_RELEASE, cb.INSTALLER_START):
            text = (self.root / path).read_text()
            self.put(path, text.replace("Blavet", "Chaber").replace("blavet", "chaber"))
        mark = "docs/design/chaber/svg/chaber-mark-dark-bg.svg"
        self.assertError(mark, "the sign of the release chaber is missing")


# --- the command line ------------------------------------------------------------


def run_main(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cb.main(list(args))
    return code, out.getvalue(), err.getvalue()


class CommandLine(Base):
    def test_good_repo(self):
        code, out, err = run_main("--root", str(self.root))
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(out, r"^\d+ branding files checked: 0 errors, 0 warnings\n$")

    def test_bad_repo(self):
        self.edit(THEME, "Name=Vilna", "Name=Other")
        code, out, err = run_main("--root", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn(f"{THEME}: Name=Other does not match", err)
        self.assertIn("1 errors, 0 warnings", out)

    def test_warnings_do_not_fail(self):
        self.put(OSR, without("HOME_URL"))
        code, out, err = run_main("--root", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn(f"{OSR}: warning: HOME_URL is not set", err)
        self.assertIn("0 errors, 1 warnings", out)

    def test_messages_have_the_form_path_problem(self):
        self.put(PATH_FILE, b"junk")
        self.put(OSR, "x\n")
        _, _, err = run_main("--root", str(self.root))
        self.assertTrue(err)
        for line in err.splitlines():
            self.assertRegex(line, r"^[^:\s][^:]*: \S")

    def test_output_is_sorted_and_stable(self):
        self.put(PATH_FILE, b"junk")
        self.put(OSR, "x\n")
        first = run_main("--root", str(self.root))
        second = run_main("--root", str(self.root))
        self.assertEqual(first, second)
        paths = [line.split(":")[0] for line in first[2].splitlines()]
        self.assertEqual(paths, sorted(paths))

    def test_wrong_calls(self):
        for args in (("--bad",), ("--root",), ("x",), ("--root", "a", "b"), ("--root=x",)):
            code, _, err = run_main(*args)
            self.assertEqual(code, 2, args)
            self.assertIn("usage:", err)

    def test_help(self):
        for flag in ("-h", "--help"):
            code, _, err = run_main(flag)
            self.assertEqual(code, 0)
            self.assertIn("usage:", err)

    def test_root_is_not_a_folder(self):
        code, _, err = run_main("--root", str(self.root / "nothing"))
        self.assertEqual(code, 2)
        self.assertIn("not a folder", err)

    def test_empty_folder(self):
        with tempfile.TemporaryDirectory() as empty:
            code, _, err = run_main("--root", empty)
        self.assertEqual(code, 2)
        self.assertIn("no files found", err)

    def test_folder_that_has_only_unrelated_files_fails_with_errors(self):
        with tempfile.TemporaryDirectory() as other:
            Path(other, "readme.txt").write_text("x")
            code, _, err = run_main("--root", other)
        self.assertEqual(code, 1)
        self.assertIn("no .plymouth file", err)

    def test_root_that_is_a_symlink_to_the_repo(self):
        link = self.root.parent / "link-d57"
        link.symlink_to(self.root, target_is_directory=True)
        self.addCleanup(link.unlink)
        self.assertEqual(run_main("--root", str(link))[0], 0)

    def test_root_with_spaces_and_unicode(self):
        with tempfile.TemporaryDirectory(prefix="Мій репо ") as base:
            target = Path(base) / "a b"
            target.mkdir()
            for rel, content in good_files().items():
                path = target / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content.encode() if isinstance(content, str) else content)
            self.assertEqual(run_main("--root", str(target))[0], 0)

    def test_only_git_ls_files_is_run(self):
        calls = []
        real = cb.subprocess.run

        def spy(cmd, *a, **kw):
            calls.append(cmd)
            return real(cmd, *a, **kw)

        with mock.patch.object(cb.subprocess, "run", spy):
            run_main("--root", str(self.root))
            run_main()
        self.assertTrue(calls)
        self.assertTrue(all(c[:2] == ["git", "ls-files"] for c in calls), calls)

    def test_nothing_is_written(self):
        before = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))
        run_main("--root", str(self.root))
        after = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))
        self.assertEqual(before, after)

    def test_git_missing_falls_back_to_a_walk(self):
        with mock.patch.object(cb.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertEqual(run_main("--root", str(self.root))[0], 0)

    def test_walk_skips_dot_git_and_stops_at_the_limit(self):
        (self.root / ".git").mkdir()
        (self.root / ".git" / "x.png").write_bytes(b"junk")
        with mock.patch.object(cb.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertNotIn(".git/x.png", cb.list_files(self.root))
            with mock.patch.object(cb, "WALK_LIMIT", 3):
                self.assertEqual(len(cb.list_files(self.root)), 3)

    def test_a_file_that_is_not_in_the_list_is_not_read(self):
        self.put(f"{PLY}/untracked.png", b"junk")
        files = [f for f in cb.list_files(self.root) if "untracked" not in f]
        self.assertEqual(cb.check_repo(self.root, files)[0], [])


class DownGrade(unittest.TestCase):
    def test_legacy_pairs_become_warnings(self):
        problem = cb.Problem("a", "m", rule="r")
        with mock.patch.object(cb, "LEGACY", frozenset({("a", "r")})):
            self.assertTrue(cb.downgrade(problem).warning)
            self.assertFalse(cb.downgrade(cb.Problem("a", "m", rule="other")).warning)
            self.assertFalse(cb.downgrade(cb.Problem("b", "m", rule="r")).warning)

    def test_problem_text(self):
        self.assertEqual(str(cb.Problem("p/q", "bad")), "p/q: bad")
        self.assertEqual(str(cb.Problem("p/q", "bad", warning=True)), "p/q: warning: bad")

    def test_every_legacy_entry_is_still_a_problem_of_the_repo(self):
        problems, _ = cb.check_repo(ROOT, cb.list_files(ROOT))
        seen = {(p.path, p.rule) for p in problems}
        for entry in cb.LEGACY:
            self.assertIn(entry, seen, "delete this LEGACY line: the problem is fixed")


class Helpers(unittest.TestCase):
    def test_raw_size(self):
        self.assertEqual(cb.raw_size(6, 2, 32, 0), 2 * (1 + 24))
        self.assertEqual(cb.raw_size(3, 1, 1, 0), 2)  # 3 bits fit one byte
        self.assertEqual(cb.raw_size(9, 1, 1, 0), 3)
        self.assertEqual(cb.raw_size(1, 1, 24, 1), 1 + 3)  # one pixel is in pass 1 only

    def test_raw_size_of_interlaced_images_matches_a_walk_of_the_pixels(self):
        for w in range(1, 20):
            for h in range(1, 20):
                self.assertEqual(cb.raw_size(w, h, 32, 1), len(adam7_pixels(w, h)), f"{w}x{h}")

    def test_strip_comments(self):
        text = 'a = "x#y"; # c\nb = 1; // d\n/* e\n f */ c = "//"; d = \'#\';'
        stripped = cb.strip_script_comments(text)
        self.assertIn('"x#y"', stripped)
        self.assertIn('"//"', stripped)
        self.assertNotIn(" c\n", stripped)
        self.assertNotIn("e\n f", stripped)
        self.assertEqual(cb.strip_script_comments('x = "open'), 'x = "open')
        self.assertEqual(cb.strip_script_comments("/* never closed"), " ")

    def test_script_images(self):
        literals, built = cb.script_images(
            'a = Image("a.png"); b = Image("b-" + i + ".png"); # Image("c.png")\n'
        )
        self.assertEqual((literals, built), (["a.png"], ["b-0.png"]))

    def test_generated_images(self):
        patterns = cb.generated_images(
            'write(args.out, f"x-{i}", f, s)\nwrite(args.out, "y", f, s)\nz = out / "z-1.png"\n'
        )
        self.assertTrue(any(p.fullmatch("x-12.png") for p in patterns))
        self.assertTrue(any(p.fullmatch("y.png") for p in patterns))
        self.assertTrue(any(p.fullmatch("z-1.png") for p in patterns))
        self.assertFalse(any(p.fullmatch("x-.png") for p in patterns))
        self.assertFalse(any(p.fullmatch("yy.png") for p in patterns))


# --- the real repo -------------------------------------------------------------------


class RealRepo(unittest.TestCase):
    def test_no_errors_on_the_repo(self):
        problems, checked = cb.check_repo(ROOT, cb.list_files(ROOT))
        self.assertEqual([str(p) for p in problems if not p.warning], [])
        self.assertGreater(checked, 100)

    def test_every_warning_is_a_known_legacy_one(self):
        problems, _ = cb.check_repo(ROOT, cb.list_files(ROOT))
        for problem in problems:
            self.assertIn((problem.path, problem.rule), cb.LEGACY, str(problem))

    def test_the_command_line_on_the_repo(self):
        code, out, _ = run_main()
        self.assertEqual(code, 0)
        self.assertIn(" 0 errors, ", out)

    def test_the_real_png_files_are_read_by_the_checker(self):
        tracked = [p for p in cb.list_files(ROOT) if p.startswith("docs/design/plymouth/")]
        sizes = {}
        for rel in tracked:
            if rel.endswith(".png"):
                info, problems = cb.check_png(rel, (ROOT / rel).read_bytes())
                self.assertEqual([p for p in problems if not p.warning], [], rel)
                sizes[rel] = (info.width, info.height)
        self.assertEqual(sizes["docs/design/plymouth/background-1920x1080.png"], (1920, 1080))
        self.assertEqual(sizes["docs/design/plymouth/dots-120x35.png"], (120, 35))
        self.assertEqual(sizes["docs/design/plymouth/logo/vilna-logo-01.png"], (362, 362))
        self.assertEqual(sizes["docs/design/plymouth/logo@2x/vilna-logo-01.png"], (724, 724))

    def test_a_real_png_with_a_path_added_is_caught(self):
        rel = "docs/design/plymouth/dots-120x35.png"
        data = (ROOT / rel).read_bytes()
        end = data.rindex(b"\x00\x00\x00\x00IEND")
        dirty = data[:end] + text_chunk("/home/denis/logo.png") + data[end:]
        _, problems = cb.check_png(rel, dirty)
        self.assertTrue(any(p.rule == "png-text-path" for p in problems))


if __name__ == "__main__":
    unittest.main()
