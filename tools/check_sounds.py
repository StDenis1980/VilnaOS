#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check Vilna sound themes under ``sounds/themes/vilna-*/``.

For every theme the checker verifies:

* ``index.theme`` is a valid freedesktop Sound Theme key file: the group
  ``[Sound Theme]`` has a non-empty ``Name`` and ``Directories``, each listed
  directory has its own group with ``OutputProfile`` and exists on disk;
  lines are comments, empty, ``[Group]`` or ``Key=Value``; no key is repeated
  in a group; the file is UTF-8 without a BOM. If any ``Name[lang]`` (or
  ``Comment[lang]``) is present, all 14 Vilna languages are present and no
  unknown language suffix is used;
* each event of the Vilna set (the 18 names in the design note and in
  ``sounds/make_sounds.py``) has a ``<dir>/<event>.oga`` file;
* every sound file is described in ``sounds/SOURCES.md`` with a licence (an
  explicit name or range, or a licence that covers the theme sounds as a group);
* a ``.oga`` file starts with the Ogg signature ``OggS`` and is a Vorbis or
  Opus stream;
* the duration, taken from the granule position of the last Ogg page and the
  sample rate in the Vorbis or Opus header, is within the limits stated in
  ``docs/design/sounds/`` (or, if those docs do not state limits, the fallback
  table: 0.2–0.7 s, ``desktop-login`` up to 2 s).

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but
do not fail the check; they mark things the repo already does today (see
``LEGACY``).

Only ``git ls-files`` is run. Nothing else is executed, nothing is written.

Run: python3 tools/check_sounds.py [THEME_OR_FILE ...]
Without arguments every tracked ``sounds/themes/vilna-*`` theme is checked.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import posixpath
import re
import struct
import subprocess
import os
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

SOUND_THEME_GROUP = "Sound Theme"
THEMES_PREFIX = "sounds/themes/"
SOURCES_REL = "sounds/SOURCES.md"
SOUND_DOCS_DIR = "docs/design/sounds"
OGG_MAGIC = b"OggS"
VORBIS_IDENT = b"\x01vorbis"
OPUS_HEAD = b"OpusHead"
OPUS_RATE = 48_000

# The 18 events of the Vilna set (docs/design/sounds/README.md, make_sounds.py).
REQUIRED_EVENTS: tuple[str, ...] = (
    "message-new-instant",
    "bell",
    "dialog-warning",
    "dialog-error",
    "complete",
    "screen-capture",
    "audio-volume-change",
    "device-added",
    "device-removed",
    "power-plug",
    "power-unplug",
    "battery-low",
    "trash-empty",
    "desktop-login",
    "x-vilna-gloxi-listen",
    "x-vilna-gloxi-done",
    "x-vilna-gloxi-error",
    "x-vilna-herkos-consent",
)
LOGIN_EVENTS = frozenset({"desktop-login"})

# The 14 translated languages; English is the key without a suffix (ADR 0009).
LANGS = (
    "uk",
    "es",
    "pl",
    "de",
    "fr",
    "it",
    "ro",
    "cs",
    "lt",
    "ca",
    "ca@valencia",
    "gl",
    "eu",
    "pt",
)
TRANSLATED_KEYS = ("Name", "Comment")

# Fallback when docs/design/sounds/ does not state limits. Taken from the
# «Правила звучання» sentence of docs/design/sounds/README.md: 0,2–0,7 s,
# login (вхід) up to 2 s.
FALLBACK_MIN_SECONDS = 0.2
FALLBACK_MAX_SECONDS = 0.7
FALLBACK_LOGIN_MAX_SECONDS = 2.0

LICENSE_RE = re.compile(
    r"(?i)\b("
    r"CC0-1\.0|CC-BY(?:-SA)?-\d\.0(?:-or-later)?"
    r"|GPL-\d\.0-or-later|LGPL-\d\.0-or-later|Apache-2\.0|MIT|FSFAP"
    r"|Public domain|суспільне надбання"
    r")\b"
)
BACKTICK_RE = re.compile(r"`([^`]+)`")
RANGE_RE = re.compile(r"^(.*?)(\d+)\.\.(\d+)(\.[A-Za-z0-9]+)?$")
# 0,2–0,7 с  /  0.2-0.7 s
PROSE_RANGE_RE = re.compile(
    r"(?<![\d,.\-])(\d+(?:[.,]\d+)?)\s*[–—-]\s*(\d+(?:[.,]\d+)?)\s*"
    r"(?:с(?:ек(?:унд)?)?|s(?:ec(?:onds?)?)?)\b",
    re.IGNORECASE,
)
# вхід до 2 с  /  login up to 2 s  /  desktop-login: 2 s
PROSE_LOGIN_RE = re.compile(
    r"(?:вхід|desktop-login|login)\s*(?:до|up to|:)?\s*(\d+(?:[.,]\d+)?)\s*"
    r"(?:с(?:ек(?:унд)?)?|s(?:ec(?:onds?)?)?)\b",
    re.IGNORECASE,
)
THEME_LICENSE_HINT_RE = re.compile(r"(?is)(звуки\s+теми|theme\s+sounds|синтезован|synthesi[sz]ed)")
DURATION_CELL_RE = re.compile(
    r"(?i)^(?:до\s+|up\s+to\s+)?(\d+(?:[.,]\d+)?)\s*"
    r"(?:с(?:ек(?:унд)?)?|s(?:ec(?:onds?)?)?)?$"
)
GROUP_LINE = re.compile(r"\[([^\[\]\x00-\x1f\x7f]+)\]")
KEY_NAME = re.compile(r"([A-Za-z0-9-]+)(?:\[([^\[\]\s]+)\])?")

# Violations the repo already has today. They are reported as warnings, not
# errors, so that the check can be switched on in CI; fix the file (or the
# documented limits), then delete the line. Each entry is (path from the repo
# root, rule).
LEGACY: frozenset[tuple[str, str]] = frozenset(
    {
        ("sounds/themes/vilna-bell/stereo/battery-low.oga", "duration-max"),
        ("sounds/themes/vilna-bell/stereo/complete.oga", "duration-max"),
        ("sounds/themes/vilna-bell/stereo/desktop-login.oga", "duration-max"),
        ("sounds/themes/vilna-bell/stereo/device-added.oga", "duration-max"),
        ("sounds/themes/vilna-bell/stereo/device-removed.oga", "duration-max"),
        ("sounds/themes/vilna-bell/stereo/dialog-error.oga", "duration-max"),
        ("sounds/themes/vilna-bell/stereo/dialog-warning.oga", "duration-max"),
        ("sounds/themes/vilna-bell/stereo/message-new-instant.oga", "duration-max"),
        ("sounds/themes/vilna-bell/stereo/power-plug.oga", "duration-max"),
        ("sounds/themes/vilna-bell/stereo/power-unplug.oga", "duration-max"),
        ("sounds/themes/vilna-bell/stereo/trash-empty.oga", "duration-max"),
        ("sounds/themes/vilna-bell/stereo/x-vilna-gloxi-done.oga", "duration-max"),
        ("sounds/themes/vilna-bell/stereo/x-vilna-herkos-consent.oga", "duration-max"),
        ("sounds/themes/vilna-glass/stereo/battery-low.oga", "duration-max"),
        ("sounds/themes/vilna-glass/stereo/complete.oga", "duration-max"),
        ("sounds/themes/vilna-glass/stereo/device-removed.oga", "duration-max"),
        ("sounds/themes/vilna-glass/stereo/dialog-error.oga", "duration-max"),
        ("sounds/themes/vilna-glass/stereo/dialog-warning.oga", "duration-max"),
        ("sounds/themes/vilna-glass/stereo/message-new-instant.oga", "duration-max"),
        ("sounds/themes/vilna-glass/stereo/trash-empty.oga", "duration-max"),
        ("sounds/themes/vilna-glass/stereo/x-vilna-herkos-consent.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/audio-volume-change.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/battery-low.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/bell.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/complete.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/desktop-login.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/device-added.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/device-removed.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/dialog-error.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/dialog-warning.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/message-new-instant.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/power-plug.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/power-unplug.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/screen-capture.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/trash-empty.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/x-vilna-gloxi-done.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/x-vilna-gloxi-error.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/x-vilna-gloxi-listen.oga", "duration-max"),
        ("sounds/themes/vilna-star/stereo/x-vilna-herkos-consent.oga", "duration-max"),
        ("sounds/themes/vilna-water/stereo/screen-capture.oga", "duration-min"),
        ("sounds/themes/vilna-wood/stereo/battery-low.oga", "duration-max"),
        ("sounds/themes/vilna-wood/stereo/complete.oga", "duration-max"),
        ("sounds/themes/vilna-wood/stereo/dialog-error.oga", "duration-max"),
        ("sounds/themes/vilna-wood/stereo/dialog-warning.oga", "duration-max"),
        ("sounds/themes/vilna-wood/stereo/message-new-instant.oga", "duration-max"),
        ("sounds/themes/vilna-wood/stereo/trash-empty.oga", "duration-max"),
        ("sounds/themes/vilna-wood/stereo/x-vilna-herkos-consent.oga", "duration-max"),
    }
)


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
class DurationLimits:
    """Maximum (and optional minimum) duration in seconds for a sound event."""

    min_seconds: float | None
    default_max: float
    event_max: dict[str, float]

    def max_for(self, event: str) -> float:
        return self.event_max.get(event, self.default_max)


FALLBACK_LIMITS = DurationLimits(
    FALLBACK_MIN_SECONDS,
    FALLBACK_MAX_SECONDS,
    {name: FALLBACK_LOGIN_MAX_SECONDS for name in LOGIN_EVENTS},
)


@dataclass(frozen=True)
class OggInfo:
    codec: str
    sample_rate: int
    duration: float
    channels: int
    granule: int
    pre_skip: int = 0


@dataclass(frozen=True)
class SourcesIndex:
    """What ``sounds/SOURCES.md`` says about files and their licences."""

    names: dict[str, str]  # basename, stem or relative path -> licence
    theme_license: str | None
    error: str | None = None


class OggError(ValueError):
    """The file is not a readable Vorbis/Opus Ogg stream."""


def format_seconds(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{text} s"


def parse_number(text: str) -> float:
    return float(text.replace(",", "."))


# ---------------------------------------------------------------------------
# Ogg duration: last-page granule / Vorbis rate, or (granule - pre-skip) / 48 kHz
# for Opus (RFC 7845). The Opus identification header also carries an input
# sample rate, which is not the rate of the granule clock.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _OggPage:
    offset: int
    flags: int
    granule: int
    serial: int
    sequence: int
    segments: tuple[int, ...]
    payload: bytes

    @property
    def continued(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def bos(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def eos(self) -> bool:
        return bool(self.flags & 0x04)


def iter_ogg_pages(data: bytes) -> Iterator[_OggPage]:
    if not data.startswith(OGG_MAGIC):
        raise OggError("missing OggS signature")
    i = 0
    size = len(data)
    while i < size:
        if i + 27 > size:
            if data[i : i + 4] != OGG_MAGIC:
                raise OggError("trailing bytes after the last Ogg page")
            raise OggError("truncated Ogg page header")
        if data[i : i + 4] != OGG_MAGIC:
            raise OggError("trailing bytes after the last Ogg page")
        version = data[i + 4]
        if version != 0:
            raise OggError(f"unsupported Ogg version {version}")
        flags = data[i + 5]
        granule = struct.unpack_from("<q", data, i + 6)[0]
        serial = struct.unpack_from("<I", data, i + 14)[0]
        sequence = struct.unpack_from("<I", data, i + 18)[0]
        nseg = data[i + 26]
        table_at = i + 27
        if table_at + nseg > size:
            raise OggError("truncated Ogg segment table")
        segments = tuple(data[table_at : table_at + nseg])
        payload_at = table_at + nseg
        payload_len = sum(segments)
        if payload_at + payload_len > size:
            raise OggError("truncated Ogg page payload")
        payload = data[payload_at : payload_at + payload_len]
        yield _OggPage(i, flags, granule, serial, sequence, segments, payload)
        i = payload_at + payload_len
    if i != size:
        raise OggError("trailing bytes after the last Ogg page")


def _packets_of(pages: Sequence[_OggPage], serial: int) -> Iterator[bytes]:
    """Yield complete packets of one logical stream, in file order."""
    buf = bytearray()
    pending = False
    for page in pages:
        if page.serial != serial:
            continue
        if page.continued and not pending:
            # A continued page without a packet in progress: drop the first
            # partial packet (we do not have its start).
            buf.clear()
        segs = list(page.segments)
        offset = 0
        for n, length in enumerate(segs):
            buf.extend(page.payload[offset : offset + length])
            offset += length
            if length < 255:
                yield bytes(buf)
                buf.clear()
                pending = False
            elif n == len(segs) - 1:
                pending = True


def _parse_vorbis_ident(packet: bytes) -> tuple[int, int] | None:
    if not packet.startswith(VORBIS_IDENT) or len(packet) < 16:
        return None
    channels = packet[11]
    rate = struct.unpack_from("<I", packet, 12)[0]
    if rate == 0 or channels == 0:
        return None
    return rate, channels


def _parse_opus_head(packet: bytes) -> tuple[int, int, int] | None:
    if not packet.startswith(OPUS_HEAD) or len(packet) < 19:
        return None
    channels = packet[9]
    pre_skip = struct.unpack_from("<H", packet, 10)[0]
    input_rate = struct.unpack_from("<I", packet, 12)[0]
    if channels == 0:
        return None
    return input_rate, channels, pre_skip


def read_ogg_info(data: bytes) -> OggInfo:
    """Return codec, rate and duration of a Vorbis or Opus Ogg file."""
    pages = list(iter_ogg_pages(data))
    if not pages:
        raise OggError("no Ogg pages")
    audio_serial: int | None = None
    codec: str | None = None
    sample_rate = 0
    channels = 0
    pre_skip = 0
    for page in pages:
        if not page.bos:
            continue
        packet = next(_packets_of([page], page.serial), b"")
        vorbis = _parse_vorbis_ident(packet)
        if vorbis is not None:
            audio_serial = page.serial
            codec = "vorbis"
            sample_rate, channels = vorbis
            break
        opus = _parse_opus_head(packet)
        if opus is not None:
            audio_serial = page.serial
            codec = "opus"
            _input_rate, channels, pre_skip = opus
            sample_rate = OPUS_RATE
            break
    if codec is None or audio_serial is None:
        raise OggError("no Vorbis or Opus identification header")
    if codec == "vorbis" and sample_rate == 0:
        raise OggError("Vorbis identification header has sample rate 0")
    last_granule: int | None = None
    for page in pages:
        if page.serial != audio_serial:
            continue
        if page.granule != -1:
            last_granule = page.granule
    if last_granule is None:
        raise OggError("no granule position on the audio stream")
    if codec == "vorbis":
        duration = last_granule / sample_rate
    else:
        duration = (last_granule - pre_skip) / OPUS_RATE
    if duration < 0:
        raise OggError("negative duration (granule position is smaller than pre-skip)")
    return OggInfo(codec, sample_rate, duration, channels, last_granule, pre_skip)


# ---------------------------------------------------------------------------
# Duration limits from docs/design/sounds/, else the fallback table.
# ---------------------------------------------------------------------------


def _markdown_tables(text: str) -> Iterator[list[list[str]]]:
    rows: list[list[str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            # Skip a separator row made of dashes, colons and spaces.
            if cells and all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells if c):
                continue
            rows.append(cells)
        elif rows:
            yield rows
            rows = []
    if rows:
        yield rows


def _duration_from_cell(cell: str) -> float | None:
    text = cell.strip().strip("`")
    match = DURATION_CELL_RE.fullmatch(text)
    if match is None:
        return None
    return parse_number(match.group(1))


def _event_from_cell(cell: str) -> str | None:
    text = cell.strip().strip("`")
    if text in REQUIRED_EVENTS or text in LOGIN_EVENTS:
        return text
    # A bare event id, possibly with a .oga suffix.
    if text.endswith(".oga"):
        text = text[: -len(".oga")]
    if text in REQUIRED_EVENTS:
        return text
    return None


def _limits_from_tables(text: str, limits: DurationLimits) -> DurationLimits:
    event_max = dict(limits.event_max)
    found = False
    for table in _markdown_tables(text):
        useful: dict[str, float] = {}
        for row in table:
            event = next((e for c in row if (e := _event_from_cell(c)) is not None), None)
            duration = next(
                (d for c in row if (d := _duration_from_cell(c)) is not None),
                None,
            )
            if event is not None and duration is not None:
                useful[event] = duration
        if useful:
            event_max.update(useful)
            found = True
    if not found:
        return limits
    return DurationLimits(limits.min_seconds, limits.default_max, event_max)


def _limits_from_prose(text: str, limits: DurationLimits) -> DurationLimits:
    event_max = dict(limits.event_max)
    min_seconds = limits.min_seconds
    default_max = limits.default_max
    range_match = PROSE_RANGE_RE.search(text)
    if range_match is not None:
        left = parse_number(range_match.group(1))
        right = parse_number(range_match.group(2))
        min_seconds, default_max = (left, right) if left <= right else (right, left)
    login_match = PROSE_LOGIN_RE.search(text)
    if login_match is not None:
        login_max = parse_number(login_match.group(1))
        for name in LOGIN_EVENTS:
            event_max[name] = login_max
    if range_match is None and login_match is None:
        return limits
    return DurationLimits(min_seconds, default_max, event_max)


def limits_from_docs_text(text: str) -> DurationLimits:
    """Parse duration limits from a design-note text; fall back to the table."""
    limits = FALLBACK_LIMITS
    limits = _limits_from_prose(text, limits)
    return _limits_from_tables(text, limits)


def load_duration_limits(root: Path) -> DurationLimits:
    folder = root / SOUND_DOCS_DIR
    if not folder.is_dir():
        return FALLBACK_LIMITS
    chunks: list[str] = []
    for path in sorted(folder.rglob("*.md")):
        try:
            chunks.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    if not chunks:
        return FALLBACK_LIMITS
    return limits_from_docs_text("\n\n".join(chunks))


# ---------------------------------------------------------------------------
# sounds/SOURCES.md
# ---------------------------------------------------------------------------


def expand_source_name(name: str) -> list[str]:
    """Expand ``birds-chaffinch-1..8`` or ``glass-bing-1..2.flac`` into names."""
    match = RANGE_RE.fullmatch(name.strip())
    if match is None:
        return [name]
    prefix, start_s, end_s, suffix = match.groups()
    start, end = int(start_s), int(end_s)
    if start > end or end - start > 1000:
        return [name]
    width = len(start_s) if start_s.startswith("0") and len(start_s) == len(end_s) else 0
    suffix = suffix or ""
    names: list[str] = []
    for i in range(start, end + 1):
        number = f"{i:0{width}d}" if width else str(i)
        names.append(f"{prefix}{number}{suffix}")
    return names


def _paragraphs(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    for line in text.splitlines():
        if line.strip():
            buf.append(line)
        elif buf:
            parts.append("\n".join(buf))
            buf = []
    if buf:
        parts.append("\n".join(buf))
    return parts


def parse_sources_text(text: str) -> SourcesIndex:
    names: dict[str, str] = {}
    theme_license: str | None = None
    for block in _paragraphs(text):
        # A markdown table is split into rows so a licence stays with its names.
        pieces = block.splitlines() if "|" in block else [block]
        for piece in pieces:
            if re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?", piece.strip()):
                continue
            license_match = LICENSE_RE.search(piece)
            if license_match is None:
                # A table row may put the licence in a later cell; search the
                # whole block once when the piece itself has names but no licence.
                license_match = LICENSE_RE.search(block)
            if license_match is None:
                continue
            licence = license_match.group(1)
            if THEME_LICENSE_HINT_RE.search(piece) or THEME_LICENSE_HINT_RE.search(block):
                theme_license = licence
            for raw in BACKTICK_RE.findall(piece):
                for expanded in expand_source_name(raw.strip()):
                    names[expanded] = licence
                    base = posixpath.basename(expanded)
                    names[base] = licence
                    stem, ext = posixpath.splitext(base)
                    if ext:
                        names[stem] = licence
    return SourcesIndex(names, theme_license)


def load_sources(root: Path) -> SourcesIndex:
    path = root / SOURCES_REL
    if not path.is_file():
        return SourcesIndex({}, None, f"{SOURCES_REL}: missing {SOURCES_REL}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        return SourcesIndex({}, None, f"{SOURCES_REL}: cannot read the file: {err.strerror or err}")
    except UnicodeDecodeError:
        return SourcesIndex({}, None, f"{SOURCES_REL}: the file is not valid UTF-8")
    if text.startswith("\ufeff"):
        text = text[1:]
    parsed = parse_sources_text(text)
    if not parsed.names and parsed.theme_license is None:
        return SourcesIndex(
            {},
            None,
            f"{SOURCES_REL}: no file name and no licence for the theme sounds",
        )
    return parsed


def file_license(rel: str, sources: SourcesIndex) -> str | None:
    """Return the licence that covers ``rel``, or None if it is not described."""
    path = rel.replace("\\", "/")
    candidates = [path, posixpath.basename(path)]
    stem, ext = posixpath.splitext(posixpath.basename(path))
    candidates.append(stem)
    if ext:
        candidates.append(stem + ext)
    for name in candidates:
        if name in sources.names:
            return sources.names[name]
    if path.startswith(THEMES_PREFIX) and sources.theme_license:
        return sources.theme_license
    return None


# ---------------------------------------------------------------------------
# index.theme (desktop-entry style key file)
# ---------------------------------------------------------------------------


def parse_theme_file(path: str, text: str) -> tuple[list[Group], list[Problem]]:
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


def check_theme_structure(path: str, groups: list[Group]) -> list[Problem]:
    problems: list[Problem] = []
    if not groups or groups[0].name != SOUND_THEME_GROUP:
        problems.append(Problem(path, f"the first group must be [{SOUND_THEME_GROUP}]"))
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


def check_theme_keys(path: str, groups: list[Group]) -> tuple[list[str], list[Problem]]:
    problems: list[Problem] = []
    theme = next((g for g in groups if g.name == SOUND_THEME_GROUP), None)
    directories: list[str] = []
    if theme is None:
        return directories, problems
    name = theme.get("Name")
    if name is None:
        problems.append(Problem(path, "missing Name"))
    elif not name:
        problems.append(Problem(path, "Name is empty"))
    raw_dirs = theme.get("Directories")
    if raw_dirs is None:
        problems.append(Problem(path, "missing Directories"))
    elif not raw_dirs.strip():
        problems.append(Problem(path, "Directories is empty"))
    else:
        directories = [part.strip() for part in raw_dirs.split(",") if part.strip()]
        if not directories:
            problems.append(Problem(path, "Directories is empty"))
    inherits = theme.get("Inherits")
    if inherits is not None and not inherits.strip():
        problems.append(Problem(path, "Inherits is empty"))
    group_names = {g.name for g in groups}
    for directory in directories:
        if "/" in directory or directory in {".", ".."} or directory.startswith("."):
            problems.append(Problem(path, f"directory '{directory}' is not a plain folder name"))
            continue
        if directory not in group_names:
            problems.append(
                Problem(
                    path,
                    f"Directories lists '{directory}' but [{directory}] is missing",
                )
            )
            continue
        section = next(g for g in groups if g.name == directory)
        profile = section.get("OutputProfile")
        if profile is None or not profile:
            problems.append(Problem(path, f"[{directory}] is missing OutputProfile"))
    return directories, problems


def check_translations(path: str, group: Group) -> list[Problem]:
    problems: list[Problem] = []
    for key in TRANSLATED_KEYS:
        langs = {lg for _, k, lg, _ in group.entries if k == key and lg is not None}
        if not langs:
            continue
        unknown = sorted(langs - set(LANGS))
        for lang in unknown:
            shown = f"{key}[{lang}]"
            number = next(n for n, k, lg, _ in group.entries if k == key and lg == lang)
            problems.append(Problem(path, f"line {number}: unknown language suffix in {shown}"))
        missing = [lang for lang in LANGS if lang not in langs]
        if missing:
            problems.append(
                Problem(path, f"[{group.name}] {key} is not translated into: {', '.join(missing)}")
            )
        for number, k, lang, value in group.entries:
            if k != key or lang is None:
                continue
            if value == "":
                problems.append(Problem(path, f"line {number}: {key}[{lang}] is empty"))
    return problems


# ---------------------------------------------------------------------------
# Theme directory and sound files
# ---------------------------------------------------------------------------


def _is_vilna_theme_dir(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return (
        len(parts) == 3
        and parts[0] == "sounds"
        and parts[1] == "themes"
        and parts[2].startswith("vilna-")
    )


def theme_name_from_path(rel: str) -> str | None:
    """Return ``vilna-wood`` for a path under ``sounds/themes/vilna-wood/``."""
    parts = PurePosixPath(rel.replace("\\", "/")).parts
    if len(parts) >= 3 and parts[0] == "sounds" and parts[1] == "themes":
        name = parts[2]
        if name.startswith("vilna-"):
            return name
    return None


def check_index_theme(rel: str, text: str) -> tuple[list[str], list[Problem]]:
    if text.startswith("\ufeff"):
        problems = [Problem(rel, "the file starts with a byte order mark (BOM)")]
        text = text[1:]
    else:
        problems = []
    groups, parse_problems = parse_theme_file(rel, text)
    problems += parse_problems
    problems += check_theme_structure(rel, groups)
    directories, key_problems = check_theme_keys(rel, groups)
    problems += key_problems
    for group in groups:
        problems += check_translations(rel, group)
    return directories, problems


def _existing_event_files(directory: Path) -> dict[str, list[str]]:
    """Map event name -> file names found in one OutputProfile folder."""
    found: dict[str, list[str]] = {}
    if not directory.is_dir():
        return found
    for child in sorted(directory.iterdir()):
        if not child.is_file() or child.name.startswith("."):
            continue
        name = child.name
        if name.endswith(".disabled"):
            event = name[: -len(".disabled")]
        else:
            event, _sep, ext = name.partition(".")
            if not _sep:
                event = name
        found.setdefault(event, []).append(name)
    return found


def check_sound_file(
    rel: str,
    data: bytes,
    event: str,
    limits: DurationLimits,
    sources: SourcesIndex,
) -> list[Problem]:
    problems: list[Problem] = []
    if sources.error is None and file_license(rel, sources) is None:
        problems.append(
            Problem(rel, f"not described in {SOURCES_REL} with a licence", rule="sources")
        )
    try:
        info = read_ogg_info(data)
    except OggError as err:
        problems.append(Problem(rel, str(err), rule="ogg"))
        return problems
    maximum = limits.max_for(event)
    if info.duration > maximum:
        problems.append(
            Problem(
                rel,
                f"duration {format_seconds(info.duration)} is longer than the "
                f"{format_seconds(maximum)} limit",
                rule="duration-max",
            )
        )
    if limits.min_seconds is not None and info.duration < limits.min_seconds:
        problems.append(
            Problem(
                rel,
                f"duration {format_seconds(info.duration)} is shorter than the "
                f"{format_seconds(limits.min_seconds)} limit",
                rule="duration-min",
            )
        )
    return problems


def check_theme(
    root: Path,
    theme_rel: str,
    limits: DurationLimits,
    sources: SourcesIndex,
) -> list[Problem]:
    theme_dir = root / theme_rel
    index_rel = f"{theme_rel}/index.theme"
    index_path = root / index_rel
    problems: list[Problem] = []
    directories: list[str] = ["stereo"]
    if not index_path.is_file():
        problems.append(Problem(index_rel, "missing index.theme"))
    else:
        try:
            raw = index_path.read_bytes()
            text = raw.decode("utf-8")
        except OSError as err:
            problems.append(Problem(index_rel, f"cannot read the file: {err.strerror or err}"))
        except UnicodeDecodeError:
            problems.append(Problem(index_rel, "the file is not valid UTF-8"))
        else:
            directories, index_problems = check_index_theme(index_rel, text)
            problems += index_problems
            if not directories:
                directories = ["stereo"]
    for directory in directories:
        if "/" in directory or directory in {".", ".."}:
            continue
        folder = theme_dir / directory
        if not folder.is_dir():
            problems.append(
                Problem(f"{theme_rel}/{directory}", f"directory '{directory}' is missing")
            )
            continue
        found = _existing_event_files(folder)
        for event in REQUIRED_EVENTS:
            expected = f"{event}.oga"
            names = found.get(event, [])
            if expected in names:
                continue
            rel = f"{theme_rel}/{directory}/{expected}"
            if names:
                shown = ", ".join(sorted(names))
                problems.append(
                    Problem(rel, f"event {event} has no {directory}/{expected} (found {shown})")
                )
            else:
                problems.append(Problem(rel, f"event {event} has no {directory}/{expected}"))
        known_expected = {f"{event}.oga" for event in REQUIRED_EVENTS}
        for event, names in found.items():
            for name in names:
                rel = f"{theme_rel}/{directory}/{name}"
                if name not in known_expected:
                    problems.append(
                        Problem(rel, "unexpected file in the sound theme", rule="extra")
                    )
                    continue
                path = folder / name
                try:
                    data = path.read_bytes()
                except OSError as err:
                    problems.append(Problem(rel, f"cannot read the file: {err.strerror or err}"))
                    continue
                problems += check_sound_file(rel, data, event, limits, sources)
    return problems


def check_sources_recordings(
    root: Path, sources: SourcesIndex, tracked: Sequence[str]
) -> list[Problem]:
    """Every tracked file in ``sounds/sources/`` must appear in SOURCES.md."""
    problems: list[Problem] = []
    if sources.error is not None:
        return problems
    for rel in tracked:
        if not rel.startswith("sounds/sources/") or rel.endswith("/"):
            continue
        if file_license(rel, sources) is None:
            problems.append(
                Problem(rel, f"not described in {SOURCES_REL} with a licence", rule="sources")
            )
    return problems


def downgrade(problem: Problem) -> Problem:
    """Turn a known old violation (see LEGACY) into a warning."""
    if (problem.path, problem.rule) in LEGACY:
        return Problem(problem.path, problem.message, warning=True, rule=problem.rule)
    return problem


def check_themes(
    root: Path,
    theme_rels: Sequence[str],
    tracked: Sequence[str] | None = None,
) -> list[Problem]:
    limits = load_duration_limits(root)
    sources = load_sources(root)
    problems: list[Problem] = []
    if sources.error is not None:
        problems.append(Problem(SOURCES_REL, sources.error.split(": ", 1)[-1], rule="sources"))
    for theme_rel in theme_rels:
        problems += check_theme(root, theme_rel, limits, sources)
    if tracked is not None:
        problems += check_sources_recordings(root, sources, tracked)
    return [downgrade(p) for p in problems]


def tracked_theme_paths(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "sounds/themes/vilna-*", "sounds/sources"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        errors="surrogateescape",
    ).stdout
    return [p for p in out.split("\0") if p]


def themes_from_tracked(paths: Iterable[str]) -> list[str]:
    names: set[str] = set()
    for rel in paths:
        theme = theme_name_from_path(rel)
        if theme is not None:
            names.add(f"{THEMES_PREFIX}{theme}")
    return sorted(names)


def themes_from_args(root: Path, args: Sequence[str]) -> tuple[list[str], list[Problem]]:
    """Resolve command-line paths to ``sounds/themes/vilna-*`` directories."""
    problems: list[Problem] = []
    names: list[str] = []
    seen: set[str] = set()
    for arg in args:
        path = Path(arg)
        if not path.is_absolute():
            path = root / arg
        rel: str
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            problems.append(Problem(arg, "path is outside the checked tree"))
            continue
        theme = theme_name_from_path(rel)
        bare = f"{THEMES_PREFIX}{path.name}"
        if (
            theme is None
            and path.is_dir()
            and path.name.startswith("vilna-")
            and _is_vilna_theme_dir(bare)
            and (root / "sounds" / "themes" / path.name).exists()
        ):
            theme = path.name
            rel = bare
        if theme is None:
            problems.append(
                Problem(arg, "not a sounds/themes/vilna-* theme, index.theme or sound file")
            )
            continue
        theme_rel = f"{THEMES_PREFIX}{theme}"
        if theme_rel not in seen:
            seen.add(theme_rel)
            names.append(theme_rel)
    return names, problems


def main(argv: list[str] | None = None) -> int:
    try:
        args = sys.argv[1:] if argv is None else argv
        if any(a.startswith("-") for a in args):
            print("usage: python3 tools/check_sounds.py [THEME_OR_FILE ...]", file=sys.stderr)
            return 0 if args in (["-h"], ["--help"]) else 2
        root = Path.cwd() if args else ROOT
        tracked: list[str] | None
        if args:
            theme_rels, problems = themes_from_args(root, args)
            tracked = None
            if not theme_rels and not problems:
                print("no sounds/themes/vilna-* theme in the arguments", file=sys.stderr)
                return 2
        else:
            problems = []
            try:
                tracked = tracked_theme_paths(ROOT)
            except (OSError, subprocess.CalledProcessError) as err:
                print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
                return 2
            theme_rels = themes_from_tracked(tracked)
            if not theme_rels:
                print("no tracked sounds/themes/vilna-* themes found", file=sys.stderr)
                return 2
        if theme_rels:
            problems += check_themes(root if args else ROOT, theme_rels, tracked)
        for problem in problems:
            print(problem, file=sys.stderr)
        errors = sum(not p.warning for p in problems)
        print(
            f"{len(theme_rels)} sound themes checked: "
            f"{errors} errors, {len(problems) - errors} warnings"
        )
        return 1 if errors else 0
    except (OSError, ValueError) as err:  # no traceback for a bad name, argument or bytes
        print(f"{os.path.basename(__file__)}: cannot check: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # A file name that is not UTF-8 is printed escaped, not as a traceback.
    sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    sys.exit(main())
