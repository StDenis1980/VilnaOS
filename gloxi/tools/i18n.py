# SPDX-License-Identifier: GPL-3.0-or-later
"""Translation catalogues for Gloxi (ADR 0009), with the standard library only.

    python3 tools/i18n.py extract   # source → po/gloxi.pot
    python3 tools/i18n.py update    # gloxi.pot → every po/<lang>.po (keeps translations)
    python3 tools/i18n.py compile   # po/<lang>.po → src/gloxi/locale/<lang>/LC_MESSAGES/gloxi.mo
    python3 tools/i18n.py check     # what CI runs: all of the above are up to date

Strings are found by reading the source (``ast``): calls to ``_("...")`` and
``N_("...")`` with a literal argument. The .po files are plain GNU gettext
catalogues, so Weblate and Poedit can edit them; the compiled .mo files are
committed so that the package needs no gettext tools to build.
"""

from __future__ import annotations

import argparse
import ast
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The gloxi folder: it is also the root of the RPM source tarball.
ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "src" / "gloxi"
PO_DIR = ROOT / "po"
POT = PO_DIR / "gloxi.pot"
LOCALE_DIR = SOURCES / "locale"
DOMAIN = "gloxi"

# ADR 0009: the main languages. English is the source language.
MAIN_LANGUAGES = ("uk", "es", "pl", "de", "fr", "it", "ro", "cs", "lt")
# Every main language must be complete (decision of 2026-11-09: the alpha ships all ten).
COMPLETE_NOW = MAIN_LANGUAGES

MARKERS = {"_", "N_"}


@dataclass
class Entry:
    msgid: str
    msgstr: str = ""
    refs: list[str] = field(default_factory=list)
    fuzzy: bool = False


# -- extract -----------------------------------------------------------------------


def extract(sources: Path = SOURCES) -> dict[str, Entry]:
    found: dict[str, Entry] = {}
    for path in sorted(sources.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in MARKERS
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value
            ):
                msgid = node.args[0].value
                entry = found.setdefault(msgid, Entry(msgid))
                # File only, no line numbers: moving code should not stale the catalogues.
                if rel not in entry.refs:
                    entry.refs.append(rel)
    return dict(sorted(found.items(), key=lambda kv: (kv[1].refs[0], kv[0])))


# -- .po reading and writing -------------------------------------------------------


def _quote(text: str) -> str:
    out = text.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t")
    lines = out.split("\n")
    parts = [line + "\\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
    if len(parts) <= 1:
        return f'"{parts[0] if parts else ""}"'
    return '""\n' + "\n".join(f'"{p}"' for p in parts)


def _unquote(raw: str) -> str:
    body = raw.strip()[1:-1]
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def read_po(path: Path) -> tuple[str, dict[str, Entry]]:
    """Header msgstr and entries of a catalogue (no plurals or contexts: Gloxi has none)."""
    header = ""
    entries: dict[str, Entry] = {}
    cur: dict = {}
    field_name = None

    def flush():
        nonlocal header
        if "msgid" in cur:
            if cur["msgid"] == "":
                header = cur.get("msgstr", "")
            else:
                e = Entry(cur["msgid"], cur.get("msgstr", ""), cur.get("refs", []))
                e.fuzzy = cur.get("fuzzy", False)
                entries[e.msgid] = e
        cur.clear()

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.rstrip()
        if not line:
            flush()
            field_name = None
        elif line.startswith("#~"):
            continue
        elif line.startswith("#:"):
            if "msgid" in cur:
                flush()
            cur.setdefault("refs", []).extend(line[2:].split())
        elif line.startswith("#,"):
            if "msgid" in cur:
                flush()
            cur["fuzzy"] = "fuzzy" in line
        elif line.startswith("#"):
            continue
        elif line.startswith("msgid "):
            if "msgid" in cur:
                flush()
            field_name = "msgid"
            cur["msgid"] = _unquote(line[6:])
        elif line.startswith("msgstr "):
            field_name = "msgstr"
            cur["msgstr"] = _unquote(line[7:])
        elif line.startswith('"') and field_name:
            cur[field_name] += _unquote(line)
    flush()
    return header, entries


def write_po(path: Path, header: str, entries: dict[str, Entry]) -> str:
    chunks = [f'msgid ""\nmsgstr {_quote(header)}\n']
    for e in entries.values():
        lines = []
        if e.refs:
            lines.append("#: " + " ".join(e.refs))
        if e.fuzzy:
            lines.append("#, fuzzy")
        if "{" in e.msgid:
            lines.append("#, python-brace-format")
        lines.append(f"msgid {_quote(e.msgid)}")
        lines.append(f"msgstr {_quote(e.msgstr)}")
        chunks.append("\n".join(lines) + "\n")
    text = "\n".join(chunks)
    if path is not None:
        path.write_text(text, encoding="utf-8")
    return text


def header_for(lang: str | None) -> str:
    lines = [
        "Project-Id-Version: gloxi\n",
        "Report-Msgid-Bugs-To: https://github.com/StDenis1980/VilnaOS/issues\n",
        f"Language: {lang or ''}\n",
        "MIME-Version: 1.0\n",
        "Content-Type: text/plain; charset=UTF-8\n",
        "Content-Transfer-Encoding: 8bit\n",
    ]
    return "".join(lines)


# -- commands ---------------------------------------------------------------------


def pot_text() -> str:
    return write_po(None, header_for(None), extract())


def merged(lang: str) -> tuple[str, dict[str, Entry]]:
    """``lang``.po brought in line with the current strings, translations kept."""
    path = PO_DIR / f"{lang}.po"
    old = read_po(path)[1] if path.exists() else {}
    out = {}
    for msgid, e in extract().items():
        prev = old.get(msgid)
        if prev:
            out[msgid] = Entry(msgid, prev.msgstr, e.refs, prev.fuzzy)
        else:
            out[msgid] = Entry(msgid, "", e.refs)
    return header_for(lang), out


def mo_bytes(header: str, entries: dict[str, Entry]) -> bytes:
    """GNU .mo for the translated, non-fuzzy entries (format: gettext manual, 'MO Files')."""
    pairs = {"": header}
    pairs.update({e.msgid: e.msgstr for e in entries.values() if e.msgstr and not e.fuzzy})
    keys = sorted(pairs)
    ids = [k.encode() for k in keys]
    strs = [pairs[k].encode() for k in keys]
    n = len(keys)
    key_start = 7 * 4 + 16 * n
    val_start = key_start + sum(len(i) + 1 for i in ids)
    offsets, pos = [], key_start
    for i in ids:
        offsets.append((len(i), pos))
        pos += len(i) + 1
    voffsets, pos = [], val_start
    for s in strs:
        voffsets.append((len(s), pos))
        pos += len(s) + 1
    out = struct.pack("<7I", 0x950412DE, 0, n, 28, 28 + 8 * n, 0, 0)
    out += b"".join(struct.pack("<2I", *o) for o in offsets)
    out += b"".join(struct.pack("<2I", *o) for o in voffsets)
    out += b"".join(i + b"\0" for i in ids)
    out += b"".join(s + b"\0" for s in strs)
    return out


def mo_path(lang: str) -> Path:
    return LOCALE_DIR / lang / "LC_MESSAGES" / f"{DOMAIN}.mo"


def cmd_extract() -> None:
    PO_DIR.mkdir(parents=True, exist_ok=True)
    POT.write_text(pot_text(), encoding="utf-8")


def cmd_update() -> None:
    cmd_extract()
    for lang in MAIN_LANGUAGES:
        header, entries = merged(lang)
        write_po(PO_DIR / f"{lang}.po", header, entries)


def cmd_compile() -> None:
    for lang in MAIN_LANGUAGES:
        header, entries = read_po(PO_DIR / f"{lang}.po")
        target = mo_path(lang)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(mo_bytes(header, entries))


def cmd_check() -> int:
    problems = []
    if not POT.exists() or POT.read_text(encoding="utf-8") != pot_text():
        problems.append("po/gloxi.pot is out of date: run `python3 tools/i18n.py update`")
    for lang in MAIN_LANGUAGES:
        path = PO_DIR / f"{lang}.po"
        if not path.exists():
            problems.append(f"{lang}: no catalogue (ADR 0009 main language)")
            continue
        header, entries = read_po(path)
        new_header, expected = merged(lang)
        if write_po(None, header, entries) != write_po(None, new_header, expected):
            problems.append(f"{lang}.po is out of date: run `python3 tools/i18n.py update`")
        missing = [e.msgid for e in entries.values() if not e.msgstr or e.fuzzy]
        broken = [
            e.msgid for e in entries.values() if e.msgstr and _braces(e.msgid) != _braces(e.msgstr)
        ]
        for msgid in broken:
            problems.append(f"{lang}: placeholders differ in {msgid!r}")
        if lang in COMPLETE_NOW and missing:
            problems.append(f"{lang}: {len(missing)} strings not translated, e.g. {missing[0]!r}")
        if not mo_path(lang).exists() or mo_path(lang).read_bytes() != mo_bytes(header, entries):
            problems.append(f"{lang}: stale .mo: run `python3 tools/i18n.py compile`")
        done = len(entries) - len(missing)
        print(f"{lang}: {done}/{len(entries)} translated")
    for p in problems:
        print(f"i18n: {p}", file=sys.stderr)
    return 1 if problems else 0


def _braces(text: str) -> list[str]:
    return sorted(re.findall(r"\{[^{}]*\}", text))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("command", choices=("extract", "update", "compile", "check"))
    args = p.parse_args(argv)
    if args.command == "check":
        return cmd_check()
    {"extract": cmd_extract, "update": cmd_update, "compile": cmd_compile}[args.command]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
