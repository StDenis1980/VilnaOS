#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_sounds.py (Vilna sound themes under sounds/themes/vilna-*).

Run: python3 tools/test_check_sounds.py
"""

from __future__ import annotations

import contextlib
import io
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_sounds as cs  # noqa: E402  (after sys.path.insert above)

THEME = "vilna-test"
THEME_REL = f"sounds/themes/{THEME}"
INDEX = f"{THEME_REL}/index.theme"
STEREO = f"{THEME_REL}/stereo"


def ogg_page(
    payload: bytes,
    *,
    granule: int,
    serial: int = 1,
    seq: int = 0,
    bos: bool = False,
    eos: bool = False,
    continued: bool = False,
) -> bytes:
    """Build one Ogg page. The CRC field is left as zero (the checker ignores it)."""
    if payload:
        segments: list[int] = []
        remaining = len(payload)
        while remaining > 255:
            segments.append(255)
            remaining -= 255
        segments.append(remaining)
    else:
        segments = [0]
    flags = (1 if continued else 0) | (2 if bos else 0) | (4 if eos else 0)
    header = bytearray(b"OggS")
    header.append(0)
    header.append(flags)
    header += struct.pack("<q", granule)
    header += struct.pack("<I", serial)
    header += struct.pack("<I", seq)
    header += b"\x00\x00\x00\x00"
    header.append(len(segments))
    header += bytes(segments)
    return bytes(header) + payload


def vorbis_ident(rate: int = 48_000, channels: int = 1) -> bytes:
    packet = bytearray(b"\x01vorbis")
    packet += struct.pack("<I", 0)
    packet.append(channels)
    packet += struct.pack("<I", rate)
    packet += struct.pack("<i", 0)
    packet += struct.pack("<i", 0)
    packet += struct.pack("<i", 0)
    packet.append((11 << 4) | 8)
    packet.append(1)
    return bytes(packet)


def opus_head(input_rate: int = 48_000, channels: int = 1, pre_skip: int = 312) -> bytes:
    packet = bytearray(b"OpusHead")
    packet.append(1)
    packet.append(channels)
    packet += struct.pack("<H", pre_skip)
    packet += struct.pack("<I", input_rate)
    packet += struct.pack("<h", 0)
    packet.append(0)
    return bytes(packet)


def vorbis_oga(duration: float, rate: int = 48_000, channels: int = 1) -> bytes:
    granule = int(round(duration * rate))
    return ogg_page(vorbis_ident(rate, channels), granule=0, seq=0, bos=True) + ogg_page(
        b"\x05audio", granule=granule, seq=1, eos=True
    )


def opus_oga(
    duration: float,
    *,
    input_rate: int = 16_000,
    pre_skip: int = 312,
    channels: int = 1,
) -> bytes:
    # RFC 7845: duration = (granule - pre-skip) / 48000, regardless of input rate.
    granule = int(round(duration * cs.OPUS_RATE)) + pre_skip
    ident = ogg_page(opus_head(input_rate, channels, pre_skip), granule=0, seq=0, bos=True)
    return ident + ogg_page(b"audio", granule=granule, seq=1, eos=True)


def theme_index(*, name: str = "Vilna Test", translates: bool = False) -> str:
    lines = [
        "[Sound Theme]",
        f"Name={name}",
    ]
    if translates:
        for lang in cs.LANGS:
            lines.append(f"Name[{lang}]={name} {lang}")
    lines += [
        "Inherits=freedesktop",
        "Directories=stereo",
        "",
        "[stereo]",
        "OutputProfile=stereo",
        "",
    ]
    return "\n".join(lines)


def sources_group_license() -> str:
    return (
        "# Sources\n\n"
        "The theme sounds, including the synthesised themes, are released under CC-BY-SA-4.0.\n"
    )


def write_theme(
    root: Path,
    *,
    events: dict[str, bytes] | None = None,
    duration: float = 0.4,
    index: str | None = None,
    sources: str | None = None,
    extra_files: dict[str, bytes] | None = None,
    docs: str | None = None,
) -> None:
    """Write a complete Vilna theme plus SOURCES.md under ``root``."""
    stereo = root / STEREO
    stereo.mkdir(parents=True, exist_ok=True)
    (root / INDEX).write_text(theme_index() if index is None else index, encoding="utf-8")
    if events is None:
        events = {event: vorbis_oga(duration) for event in cs.REQUIRED_EVENTS}
    for event, data in events.items():
        (stereo / f"{event}.oga").write_bytes(data)
    if extra_files:
        for name, data in extra_files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(data, str):
                path.write_text(data, encoding="utf-8")
            else:
                path.write_bytes(data)
    (root / "sounds").mkdir(exist_ok=True)
    (root / cs.SOURCES_REL).write_text(
        sources_group_license() if sources is None else sources, encoding="utf-8"
    )
    if docs is not None:
        docs_dir = root / cs.SOUND_DOCS_DIR
        docs_dir.mkdir(parents=True, exist_ok=True)
        (docs_dir / "README.md").write_text(docs, encoding="utf-8")


def problems_of(
    root: Path, themes: list[str] | None = None, tracked: list[str] | None = None
) -> list[cs.Problem]:
    return cs.check_themes(root, themes if themes is not None else [THEME_REL], tracked)


def errors_of(
    root: Path, themes: list[str] | None = None, tracked: list[str] | None = None
) -> list[str]:
    return [str(p) for p in problems_of(root, themes, tracked) if not p.warning]


def warnings_of(
    root: Path, themes: list[str] | None = None, tracked: list[str] | None = None
) -> list[str]:
    return [str(p) for p in problems_of(root, themes, tracked) if p.warning]


class OggDuration(unittest.TestCase):
    def test_vorbis_duration_is_granule_divided_by_header_rate(self) -> None:
        rate = 24_000
        granule = 12_000
        data = ogg_page(vorbis_ident(rate), granule=0, seq=0, bos=True) + ogg_page(
            b"x", granule=granule, seq=1, eos=True
        )
        info = cs.read_ogg_info(data)
        self.assertEqual(info.codec, "vorbis")
        self.assertEqual(info.sample_rate, rate)
        self.assertEqual(info.granule, granule)
        self.assertAlmostEqual(info.duration, 0.5)

    def test_opus_duration_uses_48_khz_and_pre_skip_not_the_input_rate(self) -> None:
        # 0.25 s at 48 kHz is 12_000 samples after pre-skip. Input rate is 16 kHz
        # on purpose: dividing by it would give the wrong duration (0.75 s).
        pre_skip = 384
        data = opus_oga(0.25, input_rate=16_000, pre_skip=pre_skip)
        info = cs.read_ogg_info(data)
        self.assertEqual(info.codec, "opus")
        self.assertEqual(info.sample_rate, 48_000)
        self.assertEqual(info.pre_skip, pre_skip)
        self.assertAlmostEqual(info.duration, 0.25)

    def test_last_page_with_granule_minus_one_is_skipped(self) -> None:
        data = (
            ogg_page(vorbis_ident(), granule=0, seq=0, bos=True)
            + ogg_page(b"a", granule=24_000, seq=1)
            + ogg_page(b"b", granule=-1, seq=2, eos=True)
        )
        self.assertAlmostEqual(cs.read_ogg_info(data).duration, 0.5)

    def test_missing_signature_is_an_error(self) -> None:
        with self.assertRaises(cs.OggError) as ctx:
            cs.read_ogg_info(b"RIFF....WAVE")
        self.assertIn("missing OggS signature", str(ctx.exception))

    def test_truncated_page_is_an_error(self) -> None:
        data = ogg_page(vorbis_ident(), granule=0, seq=0, bos=True)[:20]
        with self.assertRaises(cs.OggError):
            cs.read_ogg_info(data)

    def test_trailing_bytes_are_an_error(self) -> None:
        data = vorbis_oga(0.3) + b"junk"
        with self.assertRaises(cs.OggError) as ctx:
            cs.read_ogg_info(data)
        self.assertIn("trailing bytes", str(ctx.exception))

    def test_no_vorbis_or_opus_header_is_an_error(self) -> None:
        data = ogg_page(b"\x7fFLAC....", granule=0, seq=0, bos=True) + ogg_page(
            b"x", granule=100, seq=1, eos=True
        )
        with self.assertRaises(cs.OggError) as ctx:
            cs.read_ogg_info(data)
        self.assertIn("no Vorbis or Opus identification header", str(ctx.exception))

    def test_vorbis_rate_zero_is_an_error(self) -> None:
        data = ogg_page(vorbis_ident(rate=0), granule=0, seq=0, bos=True) + ogg_page(
            b"x", granule=100, seq=1, eos=True
        )
        with self.assertRaises(cs.OggError):
            cs.read_ogg_info(data)

    def test_matches_a_real_theme_file(self) -> None:
        path = ROOT / "sounds/themes/vilna-wood/stereo/bell.oga"
        info = cs.read_ogg_info(path.read_bytes())
        self.assertEqual(info.codec, "vorbis")
        self.assertEqual(info.sample_rate, 48_000)
        self.assertGreater(info.duration, 0.2)
        self.assertLess(info.duration, 0.7)


class DurationLimits(unittest.TestCase):
    def test_fallback_when_docs_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            limits = cs.load_duration_limits(Path(tmp))
        self.assertEqual(limits.min_seconds, 0.2)
        self.assertEqual(limits.default_max, 0.7)
        self.assertEqual(limits.max_for("bell"), 0.7)
        self.assertEqual(limits.max_for("desktop-login"), 2.0)

    def test_prose_in_the_design_note_is_used(self) -> None:
        text = "Правила звучання: 0,2–0,7 с, вхід до 2 с; Ogg Vorbis 48 кГц.\n"
        limits = cs.limits_from_docs_text(text)
        self.assertEqual(limits.min_seconds, 0.2)
        self.assertEqual(limits.default_max, 0.7)
        self.assertEqual(limits.max_for("desktop-login"), 2.0)
        self.assertEqual(limits.max_for("complete"), 0.7)

    def test_login_prose_overrides_the_default_range(self) -> None:
        text = "Limits: 0,1–0,5 с, вхід до 3 с.\n"
        limits = cs.limits_from_docs_text(text)
        self.assertEqual(limits.max_for("bell"), 0.5)
        self.assertEqual(limits.max_for("desktop-login"), 3.0)

    def test_table_overrides_the_limit_of_a_named_event(self) -> None:
        text = (
            "0,2–0,7 с, вхід до 2 с.\n\n"
            "| Event | Max |\n"
            "|---|---|\n"
            "| `bell` | 0,4 с |\n"
            "| desktop-login | 3 |\n"
        )
        limits = cs.limits_from_docs_text(text)
        self.assertEqual(limits.max_for("bell"), 0.4)
        self.assertEqual(limits.max_for("desktop-login"), 3.0)
        self.assertEqual(limits.max_for("complete"), 0.7)

    def test_events_table_without_durations_is_ignored(self) -> None:
        text = (
            "0,2–0,7 с, вхід до 2 с.\n\n"
            "| Подія | Коли |\n"
            "|---|---|\n"
            "| `message-new-instant` | нове сповіщення |\n"
            "| `bell` | попередження |\n"
        )
        limits = cs.limits_from_docs_text(text)
        self.assertEqual(limits.max_for("bell"), 0.7)
        self.assertEqual(limits.max_for("message-new-instant"), 0.7)

    def test_real_design_note_is_parsed(self) -> None:
        limits = cs.load_duration_limits(ROOT)
        self.assertEqual(limits.min_seconds, 0.2)
        self.assertEqual(limits.default_max, 0.7)
        self.assertEqual(limits.max_for("desktop-login"), 2.0)


class SourcesMarkdown(unittest.TestCase):
    def test_group_licence_covers_theme_files(self) -> None:
        index = cs.parse_sources_text(sources_group_license())
        self.assertEqual(index.theme_license, "CC-BY-SA-4.0")
        self.assertEqual(
            cs.file_license("sounds/themes/vilna-test/stereo/bell.oga", index),
            "CC-BY-SA-4.0",
        )

    def test_explicit_name_and_range(self) -> None:
        text = (
            "| Clips | Licence |\n"
            "|---|---|\n"
            "| `birds-chaffinch-1..8` | CC0-1.0 |\n"
            "| `glass-bing-1..2.flac` | Public domain |\n"
        )
        index = cs.parse_sources_text(text)
        self.assertEqual(index.names["birds-chaffinch-3"], "CC0-1.0")
        self.assertEqual(index.names["glass-bing-2.flac"], "Public domain")
        self.assertEqual(
            cs.file_license("sounds/sources/birds-chaffinch-3.flac", index),
            "CC0-1.0",
        )
        self.assertEqual(
            cs.file_license("sounds/sources/glass-bing-1.flac", index),
            "Public domain",
        )

    def test_expand_source_name(self) -> None:
        self.assertEqual(
            cs.expand_source_name("birds-chaffinch-1..3"),
            ["birds-chaffinch-1", "birds-chaffinch-2", "birds-chaffinch-3"],
        )
        self.assertEqual(cs.expand_source_name("bell.oga"), ["bell.oga"])

    def test_real_sources_cover_the_recordings_and_the_themes(self) -> None:
        index = cs.parse_sources_text((ROOT / cs.SOURCES_REL).read_text(encoding="utf-8"))
        self.assertEqual(index.theme_license, "CC-BY-SA-4.0")
        self.assertEqual(
            cs.file_license("sounds/sources/birds-chaffinch-8.flac", index),
            "CC0-1.0",
        )
        self.assertEqual(
            cs.file_license("sounds/sources/glass-bing-1.flac", index),
            "Public domain",
        )
        self.assertEqual(
            cs.file_license("sounds/themes/vilna-wood/stereo/bell.oga", index),
            "CC-BY-SA-4.0",
        )


class GoodTheme(unittest.TestCase):
    def test_complete_theme_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root)
            self.assertEqual(problems_of(root), [])

    def test_translated_index_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, index=theme_index(translates=True))
            self.assertEqual(problems_of(root), [])

    def test_comments_empty_lines_and_spaces_around_equals_are_fine(self) -> None:
        index = (
            "# SPDX-License-Identifier: GPL-3.0-or-later\n\n"
            "[Sound Theme]\n"
            "Name = Vilna Test\n"
            "Directories = stereo\n\n"
            "[stereo]\n"
            "OutputProfile = stereo\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, index=index)
            self.assertEqual(problems_of(root), [])

    def test_opus_file_within_the_limit_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = {event: opus_oga(0.4) for event in cs.REQUIRED_EVENTS}
            write_theme(root, events=events)
            self.assertEqual(problems_of(root), [])

    def test_duration_equal_to_the_limit_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = {event: vorbis_oga(0.7) for event in cs.REQUIRED_EVENTS}
            events["desktop-login"] = vorbis_oga(2.0)
            write_theme(root, events=events)
            self.assertEqual(problems_of(root), [])


class IndexTheme(unittest.TestCase):
    def test_missing_index_theme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root)
            (root / INDEX).unlink()
            errors = errors_of(root)
        self.assertTrue(any("missing index.theme" in e for e in errors))

    def test_first_group_must_be_sound_theme(self) -> None:
        index = "[stereo]\nOutputProfile=stereo\n\n[Sound Theme]\nName=X\nDirectories=stereo\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, index=index)
            errors = errors_of(root)
        self.assertTrue(any("first group must be [Sound Theme]" in e for e in errors))

    def test_missing_name(self) -> None:
        index = "[Sound Theme]\nDirectories=stereo\n\n[stereo]\nOutputProfile=stereo\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, index=index)
            self.assertTrue(any("missing Name" in e for e in errors_of(root)))

    def test_empty_name(self) -> None:
        index = "[Sound Theme]\nName=\nDirectories=stereo\n\n[stereo]\nOutputProfile=stereo\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, index=index)
            self.assertTrue(any("Name is empty" in e for e in errors_of(root)))

    def test_missing_directories(self) -> None:
        index = "[Sound Theme]\nName=X\n\n[stereo]\nOutputProfile=stereo\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, index=index)
            self.assertTrue(any("missing Directories" in e for e in errors_of(root)))

    def test_directory_section_missing(self) -> None:
        index = "[Sound Theme]\nName=X\nDirectories=stereo\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, index=index)
            errors = errors_of(root)
        self.assertTrue(any("lists 'stereo'" in e or "[stereo] is missing" in e for e in errors))

    def test_missing_output_profile(self) -> None:
        index = "[Sound Theme]\nName=X\nDirectories=stereo\n\n[stereo]\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, index=index)
            self.assertTrue(any("missing OutputProfile" in e for e in errors_of(root)))

    def test_duplicate_key(self) -> None:
        index = (
            "[Sound Theme]\nName=X\nName=Y\nDirectories=stereo\n\n[stereo]\nOutputProfile=stereo\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, index=index)
            self.assertTrue(any("duplicate key Name" in e for e in errors_of(root)))

    def test_invalid_line(self) -> None:
        index = theme_index() + "this is not a key\n"
        # the extra line is after [stereo]; still invalid
        index = (
            "[Sound Theme]\nName=X\nDirectories=stereo\nnot-a-key\n\n"
            "[stereo]\nOutputProfile=stereo\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, index=index)
            self.assertTrue(
                any("not a comment, empty line, [Group] or Key=Value" in e for e in errors_of(root))
            )

    def test_key_before_the_first_group(self) -> None:
        index = (
            "Name=X\n[Sound Theme]\nName=X\nDirectories=stereo\n\n[stereo]\nOutputProfile=stereo\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, index=index)
            self.assertTrue(any("key before the first group" in e for e in errors_of(root)))

    def test_bom_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root)
            text = (root / INDEX).read_text(encoding="utf-8")
            (root / INDEX).write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
            self.assertTrue(any("byte order mark" in e for e in errors_of(root)))

    def test_not_utf8_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root)
            (root / INDEX).write_bytes(b"[Sound Theme]\nName=\xff\n")
            self.assertTrue(any("not valid UTF-8" in e for e in errors_of(root)))

    def test_missing_stereo_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root)
            for child in (root / STEREO).iterdir():
                child.unlink()
            (root / STEREO).rmdir()
            errors = errors_of(root)
        self.assertTrue(any("directory 'stereo' is missing" in e for e in errors))

    def test_empty_inherits_is_an_error(self) -> None:
        index = (
            "[Sound Theme]\nName=X\nInherits=\nDirectories=stereo\n\n"
            "[stereo]\nOutputProfile=stereo\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, index=index)
            self.assertTrue(any("Inherits is empty" in e for e in errors_of(root)))

    def test_incomplete_translations(self) -> None:
        index = theme_index(translates=True).replace("Name[gl]=Vilna Test gl\n", "")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, index=index)
            errors = errors_of(root)
        self.assertTrue(any("Name is not translated into: gl" in e for e in errors))

    def test_unknown_language_suffix(self) -> None:
        index = theme_index() + "Name[ru]=Тест\n"
        # Name[ru] is in [stereo] if appended after OutputProfile... put it in the theme group.
        index = (
            "[Sound Theme]\nName=Vilna Test\nName[ru]=Тест\n"
            + "".join(f"Name[{lang}]=Vilna Test {lang}\n" for lang in cs.LANGS)
            + "Directories=stereo\n\n[stereo]\nOutputProfile=stereo\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, index=index)
            errors = errors_of(root)
        self.assertTrue(any("unknown language suffix in Name[ru]" in e for e in errors))

    def test_empty_translation_is_an_error(self) -> None:
        index = theme_index(translates=True).replace("Name[pl]=Vilna Test pl", "Name[pl]=")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, index=index)
            self.assertTrue(any("Name[pl] is empty" in e for e in errors_of(root)))


class EventsAndFiles(unittest.TestCase):
    def test_missing_event_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root)
            (root / STEREO / "bell.oga").unlink()
            errors = errors_of(root)
        self.assertTrue(
            any(e == f"{STEREO}/bell.oga: event bell has no stereo/bell.oga" for e in errors)
        )

    def test_ogg_instead_of_oga_is_named_in_the_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root)
            ogg = (root / STEREO / "bell.oga").read_bytes()
            (root / STEREO / "bell.oga").unlink()
            (root / STEREO / "bell.ogg").write_bytes(ogg)
            errors = errors_of(root)
        self.assertTrue(any("found bell.ogg" in e for e in errors))

    def test_unexpected_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, extra_files={f"{STEREO}/readme.txt": b"hello"})
            errors = errors_of(root)
        self.assertTrue(any(f"{STEREO}/readme.txt: unexpected file" in e for e in errors))

    def test_not_ogg_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = {event: vorbis_oga(0.4) for event in cs.REQUIRED_EVENTS}
            events["bell"] = b"RIFF....WAVE"
            write_theme(root, events=events)
            errors = errors_of(root)
        self.assertTrue(any(f"{STEREO}/bell.oga: missing OggS signature" in e for e in errors))

    def test_duration_above_the_limit_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = {event: vorbis_oga(0.4) for event in cs.REQUIRED_EVENTS}
            events["bell"] = vorbis_oga(0.9)
            write_theme(root, events=events)
            errors = errors_of(root)
        self.assertEqual(len(errors), 1)
        self.assertIn("duration 0.9 s is longer than the 0.7 s limit", errors[0])
        self.assertTrue(errors[0].startswith(f"{STEREO}/bell.oga:"))

    def test_login_uses_the_longer_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = {event: vorbis_oga(0.4) for event in cs.REQUIRED_EVENTS}
            events["desktop-login"] = vorbis_oga(1.5)
            write_theme(root, events=events)
            self.assertEqual(errors_of(root), [])
            events["desktop-login"] = vorbis_oga(2.5)
            write_theme(root, events=events)
            errors = errors_of(root)
        self.assertTrue(any("longer than the 2 s limit" in e for e in errors))

    def test_duration_below_the_minimum_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = {event: vorbis_oga(0.4) for event in cs.REQUIRED_EVENTS}
            events["bell"] = vorbis_oga(0.1)
            write_theme(root, events=events)
            errors = errors_of(root)
        self.assertTrue(any("duration 0.1 s is shorter than the 0.2 s limit" in e for e in errors))

    def test_docs_can_raise_the_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = {event: vorbis_oga(0.4) for event in cs.REQUIRED_EVENTS}
            events["bell"] = vorbis_oga(0.9)
            write_theme(root, events=events, docs="Limits: 0,1–1,5 с, вхід до 3 с.\n")
            self.assertEqual(errors_of(root), [])


class SourcesCheck(unittest.TestCase):
    def test_missing_sources_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root)
            (root / cs.SOURCES_REL).unlink()
            errors = errors_of(root)
        self.assertTrue(any(e.startswith("sounds/SOURCES.md: missing") for e in errors))

    def test_file_not_described(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, sources="# Sources\n\nNothing about the theme files.\n")
            errors = errors_of(root)
        self.assertTrue(any("no file name and no licence" in e for e in errors))

    def test_explicit_listing_without_a_group_licence(self) -> None:
        names = ", ".join(f"`{event}.oga`" for event in cs.REQUIRED_EVENTS)
        text = f"# Sources\n\nTheme event files: {names}. Licence: CC-BY-SA-4.0.\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, sources=text)
            # The sentence also has "Theme event files" which may set theme_license;
            # either way every file is covered.
            self.assertEqual(errors_of(root), [])

    def test_theme_file_without_a_group_licence_is_an_error(self) -> None:
        text = "| Clip | Licence |\n|---|---|\n| `birds-chaffinch-1` | CC0-1.0 |\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root, sources=text)
            errors = errors_of(root)
        self.assertTrue(
            any(
                e.endswith("not described in sounds/SOURCES.md with a licence") and "bell.oga" in e
                for e in errors
            )
        )

    def test_source_recording_must_be_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root)
            rec = "sounds/sources/mystery.flac"
            (root / rec).parent.mkdir(parents=True, exist_ok=True)
            (root / rec).write_bytes(b"fLaC")
            errors = errors_of(root, tracked=[rec])
        self.assertTrue(any(f"{rec}: not described in {cs.SOURCES_REL}" in e for e in errors))


class Legacy(unittest.TestCase):
    def test_known_old_duration_is_a_warning(self) -> None:
        path, rule = next(item for item in cs.LEGACY if item[1] == "duration-max")
        problem = cs.downgrade(
            cs.Problem(path, "duration 1 s is longer than the 0.7 s limit", rule=rule)
        )
        self.assertTrue(problem.warning)
        other = cs.downgrade(
            cs.Problem(
                f"{STEREO}/bell.oga",
                "duration 1 s is longer than the 0.7 s limit",
                rule=rule,
            )
        )
        self.assertFalse(other.warning)


class Main(unittest.TestCase):
    def run_main(self, args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cs.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_exit_0_for_a_good_theme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root)
            code, out, err = self.run_main([str(root / THEME_REL)])
            # main resolves paths against cwd, not the temp root, unless we cwd there.
        # Re-run from the temp root.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root)
            old = Path.cwd()
            try:
                import os

                os.chdir(root)
                code, out, err = self.run_main([THEME_REL])
            finally:
                os.chdir(old)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertIn("1 sound themes checked: 0 errors, 0 warnings", out)

    def test_exit_1_for_a_bad_theme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_theme(root)
            (root / STEREO / "bell.oga").unlink()
            import os

            old = Path.cwd()
            try:
                os.chdir(root)
                code, out, err = self.run_main([THEME_REL])
            finally:
                os.chdir(old)
        self.assertEqual(code, 1)
        self.assertIn("event bell has no stereo/bell.oga", err)
        self.assertIn("1 errors", out)

    def test_warnings_do_not_fail_the_check(self) -> None:
        # A path listed in LEGACY, rebuilt in a temporary tree with the same
        # relative path, is reported as a warning and leaves the exit code 0.
        path, _rule = next(
            item for item in cs.LEGACY if "vilna-star" in item[0] and item[0].endswith("/bell.oga")
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # write under sounds/themes/vilna-star
            stereo = root / "sounds/themes/vilna-star/stereo"
            stereo.mkdir(parents=True, exist_ok=True)
            index_path = root / "sounds/themes/vilna-star/index.theme"
            index_path.write_text(theme_index(), encoding="utf-8")
            for event in cs.REQUIRED_EVENTS:
                (stereo / f"{event}.oga").write_bytes(vorbis_oga(0.4 if event != "bell" else 1.2))
            (root / cs.SOURCES_REL).write_text(sources_group_license(), encoding="utf-8")
            import os

            old = Path.cwd()
            try:
                os.chdir(root)
                code, out, err = self.run_main(["sounds/themes/vilna-star"])
            finally:
                os.chdir(old)
        self.assertEqual(code, 0, err)
        self.assertIn("warning:", err)
        self.assertIn("0 errors, 1 warnings", out)
        self.assertIn(path, err)

    def test_exit_2_for_an_unknown_option(self) -> None:
        code, _, err = self.run_main(["--bogus"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)

    def test_help_is_not_an_error(self) -> None:
        self.assertEqual(self.run_main(["--help"])[0], 0)
        self.assertEqual(self.run_main(["-h"])[0], 0)


class RealRepo(unittest.TestCase):
    def test_tracked_themes_have_no_errors(self) -> None:
        paths = cs.tracked_theme_paths(ROOT)
        themes = cs.themes_from_tracked(paths)
        self.assertGreaterEqual(len(themes), 7)
        errors = [str(p) for p in cs.check_themes(ROOT, themes, paths) if not p.warning]
        self.assertEqual(errors, [])

    def test_every_required_event_is_present_on_every_theme(self) -> None:
        paths = cs.tracked_theme_paths(ROOT)
        themes = cs.themes_from_tracked(paths)
        for theme in themes:
            for event in cs.REQUIRED_EVENTS:
                oga = ROOT / theme / "stereo" / f"{event}.oga"
                self.assertTrue(oga.is_file(), oga)


if __name__ == "__main__":
    unittest.main()
