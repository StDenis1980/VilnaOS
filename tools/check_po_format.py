#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the format of the tracked gettext catalogues (``*.po`` and ``*.pot``).

The catalogues are read by a small parser of our own (standard library only): it knows
``msgctxt``, ``msgid``, ``msgid_plural``, ``msgstr`` and ``msgstr[n]``, strings that
continue on the next lines, the escapes ``\\n \\t \\r \\a \\b \\f \\v \\\\ \\" \\ooo \\xhh``,
the flags of ``#,`` (``fuzzy``, ``no-python-format`` ...) and obsolete ``#~`` entries
(they are only counted). Whether a catalogue is *complete* is not checked here.

Every translated entry (a non-empty ``msgstr``; for a plural entry, every non-empty
``msgstr[n]``) is checked:

* placeholders: the same multiset in ``msgid`` and ``msgstr``: ``{name}``, ``{0}``, ``{}``,
  ``{n:02d}``, ``%s``, ``%d``, ``%(name)s``, ``%1$s``; ``%%`` and ``{{`` ``}}`` are not
  placeholders. A flag ``no-python-format`` / ``no-c-format`` / ``no-python-brace-format``
  turns that kind off for the entry. For a plural entry every ``msgstr[n]`` is compared
  with ``msgid_plural``; a numeric placeholder missing in ``msgstr[0]`` is only a warning
  (a language may write "one" instead of "1" there);
* markup tags (``<b>``, ``</b>``, ``<i>``, ``<a href=...>``, ``<tt>`` and the other
  Pango/HTML ones, see ``MARKUP_TAGS``) are the same; only the attributes differing is a
  warning. Other ``<...>`` text, such as ``<text>`` in a usage line, is not markup;
* ``\\n`` and a space at the start and at the end are as in ``msgid`` (warning);
* if ``msgid`` has Vilna, Gloxi, Herkos, Blavet or Tinta, ``msgstr`` has its stem
  (Viln, Glox, Herk, Blavet, Tinta): languages decline the names (warning).

Every catalogue: ``(msgctxt, msgid)`` twice is an error; a fuzzy entry is a warning;
obsolete ``#~`` entries are one warning with their count; a line that is not valid
``.po`` syntax is an error; the file must be UTF-8 without a BOM.

Header of every ``.po`` (not ``.pot``): ``Language`` is the file name without ``.po``
(``ca@valencia`` too), ``Content-Type`` has ``charset=UTF-8``, ``Plural-Forms`` exists
with ``nplurals`` and ``plural``, and ``nplurals`` is what the language has (``PLURALS``).
Where the table is not certain, a different value is a warning, not an error.

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but do not
fail the check; they mark things the repo already does today (see ``LEGACY``).
Only ``git ls-files`` is run. Nothing else is executed, nothing is written.

Run: python3 tools/check_po_format.py [FILE ...]
FILE is a path from the repo root; without it every tracked ``*.po`` and ``*.pot`` is
checked. Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import posixpath
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

# The number of plural forms: (nplurals, certain). Where it comes from:
# * uk (3), pl (3), cs (3), ro (3), lt (3), de (2), es (2), it (2), pt (2), fr (2) are in
#   the table "Plural forms" of the GNU gettext manual (chapter "Programmers", section
#   "Additional functions for plural forms"; checked on 2026-11-27): Ukrainian
#   n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2;
#   Polish and Czech have their own 3-form rules; Romanian and Lithuanian too; German,
#   Spanish, Italian, Portuguese: n != 1; French and Brazilian Portuguese: n>1.
# * ca, ca@valencia, gl, eu are NOT in that table. Catalan, Galician and Basque use
#   "n != 1" (2 forms) in the catalogues of Weblate and Launchpad, as far as I know, but
#   I could not check it in a primary source, so a different value is only a warning.
#   ca@valencia is a variant of ca and has no entry of its own anywhere.
PLURALS: dict[str, tuple[int, bool]] = {
    "uk": (3, True),
    "es": (2, True),
    "pl": (3, True),
    "de": (2, True),
    "fr": (2, True),
    "it": (2, True),
    "ro": (3, True),
    "cs": (3, True),
    "lt": (3, True),
    "ca": (2, False),
    "ca@valencia": (2, False),
    "gl": (2, False),
    "eu": (2, False),
    "pt": (2, True),
}

# Names that languages decline: the stem must stay in the translation.
NAME_STEMS = (
    ("Vilna", "Viln"),
    ("Gloxi", "Glox"),
    ("Herkos", "Herk"),
    ("Blavet", "Blavet"),
    ("Tinta", "Tinta"),
)

# Tags of Pango markup and of the HTML that GTK labels and Qt rich text understand.
MARKUP_TAGS = frozenset(
    {"a", "abbr", "b", "big", "br", "code", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i"}
    | {"kbd", "li", "ol", "p", "pre", "s", "small", "span", "strike", "strong", "sub", "sup"}
    | {"tt", "u", "ul", "markup"}
)
# Names of brace placeholders that hold the number of a plural entry.
NUMERIC_NAMES = frozenset({"n", "count", "num", "number", "total", "amount", "len", "length"})

# The folders of the catalogues of the repo (one .po per language in each).
OLD_FOLDERS = (
    "backup/src/vilna_backup/locale",
    "gloxi/po",
    "herkos/src/herkos/locale",
    "installer/src/vilna_installer/locale",
    "reports/feedback/src/vilna_feedback/locale",
)
# Folders whose catalogues have no ``Language`` in the header (the others have it).
NO_LANGUAGE_FOLDERS = (
    "installer/src/vilna_installer/locale",
    "reports/feedback/src/vilna_feedback/locale",
)

# Problems the repo already has today: (path from the repo root, rule). They are
# warnings, so that the check can be switched on in CI; fix the file, then delete it
# from here. Today: no catalogue has a Plural-Forms header, and those of the installer
# and of the feedback window have no Language header either.
LEGACY: frozenset[tuple[str, str]] = frozenset(
    {(f"{folder}/{lang}.po", "header-plural") for folder in OLD_FOLDERS for lang in PLURALS}
    | {
        (f"{folder}/{lang}.po", "header-language")
        for folder in NO_LANGUAGE_FOLDERS
        for lang in PLURALS
    }
)

PRINTF = (
    r"%(?:\d+\$)?(?:\([^()]*\))?[-+#0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?"
    r"(?:hh|h|ll|l|L|q|j|z|t)?[diouxXeEfFgGcrsaAp]"
)
BRACE = r"\{[A-Za-z0-9_]*(?:\.\w+|\[[^\[\]{}]*\])*(?:![rsa])?(?::[^{}]*)?\}"
PLACEHOLDER = re.compile(rf"%%|\{{\{{|\}}\}}|(?P<printf>{PRINTF})|(?P<brace>{BRACE})")
TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)((?:\s+[^<>]*?)?)\s*(/?)>")
KEYWORD = re.compile(r'(msgctxt|msgid_plural|msgid|msgstr)(?:\[(\d+)\])?[ \t]*(".*")[ \t]*$')
ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    '"': '"',
    "\\": "\\",
    "'": "'",
    "?": "?",
}
NPLURALS = re.compile(r"nplurals\s*=\s*(\d+)\s*;")
PLURAL_EXPR = re.compile(r";\s*plural\s*=\s*\S")
CHARSET = re.compile(r"charset\s*=\s*([^\s;]+)", re.IGNORECASE)


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False
    rule: str = ""

    def __str__(self) -> str:
        kind = "warning: " if self.warning else ""
        return f"{self.path}: {kind}{self.message}"


def error(path: str, message: str, rule: str) -> Problem:
    return Problem(path, message, False, rule)


def warn(path: str, message: str, rule: str) -> Problem:
    return Problem(path, message, True, rule)


@dataclass
class Entry:
    line: int
    msgid: str = ""
    msgctxt: str | None = None
    msgid_plural: str | None = None
    msgstr: dict[int, str] = field(default_factory=dict)
    flags: frozenset[str] = frozenset()
    has_id: bool = False

    @property
    def fuzzy(self) -> bool:
        return "fuzzy" in self.flags


@dataclass
class Catalog:
    entries: list[Entry] = field(default_factory=list)
    obsolete: int = 0
    syntax: list[tuple[int, str]] = field(default_factory=list)


# --- reading a catalogue -----------------------------------------------------


def unquote(raw: str) -> tuple[str, str | None]:
    """The text of a quoted .po string, and an error message (or None)."""
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        return "", "the string is not closed with a quote"
    body = raw[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == '"':
            return "", 'a quote inside a string must be written as \\"'
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(body):
            return "", "the string ends with a lone backslash"
        nxt = body[i]
        if nxt in ESCAPES:
            out.append(ESCAPES[nxt])
            i += 1
        elif nxt in "01234567":
            digits = re.match(r"[0-7]{1,3}", body[i:])
            assert digits is not None
            out.append(chr(int(digits.group(), 8)))
            i += len(digits.group())
        elif nxt == "x":
            digits = re.match(r"[0-9A-Fa-f]{1,2}", body[i + 1 :])
            if digits is None:
                return "", "\\x without hexadecimal digits"
            out.append(chr(int(digits.group(), 16)))
            i += 1 + len(digits.group())
        else:
            return "", f"unknown escape \\{nxt}"
    return "".join(out), None


def parse(text: str) -> Catalog:
    """Read the text of a catalogue; syntax errors are collected, not raised."""
    cat = Catalog()
    cur: Entry | None = None
    pending: set[str] = set()
    target: tuple[str, int] | None = None  # the keyword that continuation lines extend

    def close() -> None:
        nonlocal cur, target
        if cur is not None:
            if not cur.msgstr:
                cat.syntax.append((cur.line, "entry without msgstr"))
            cat.entries.append(cur)
        cur, target = None, None

    def start(line: int) -> Entry:
        nonlocal cur
        cur = Entry(line, flags=frozenset(pending))
        pending.clear()
        return cur

    for number, raw in enumerate(text.split("\n"), start=1):
        stripped = raw.strip()  # also drops the \r of a CRLF file
        if not stripped:
            close()
            continue
        if stripped.startswith("#~"):
            close()
            if re.match(r"#~\s*msgid\s", stripped):
                cat.obsolete += 1
            continue
        if stripped.startswith("#"):
            if cur is not None and cur.msgstr:
                close()
            if stripped.startswith("#,"):
                pending.update(f.strip() for f in stripped[2:].split(",") if f.strip())
            target = None
            continue
        if stripped.startswith('"'):
            if target is None or cur is None:
                cat.syntax.append((number, "a string without a keyword before it"))
                continue
            piece, err = unquote(stripped)
            if err:
                cat.syntax.append((number, err))
                continue
            append(cur, target, piece)
            continue
        match = KEYWORD.match(stripped)
        if match is None:
            word = re.match(r"(msgctxt|msgid_plural|msgid|msgstr(?:\[\d+\])?)\b", stripped)
            if word:
                message = f"the string after {word.group(1)} is not in quotes"
            else:
                message = f"not valid .po syntax: {stripped[:40]!r}"
            cat.syntax.append((number, message))
            target = None
            continue
        word, index, raw_string = match.group(1), match.group(2), match.group(3)
        string, err = unquote(raw_string)
        if err:
            cat.syntax.append((number, err))
            target = None
            continue
        if word in ("msgctxt", "msgid") and cur is not None and cur.msgstr:
            close()
        if word == "msgctxt":
            if cur is not None:
                cat.syntax.append((number, "msgctxt after another keyword of the entry"))
                continue
            start(number).msgctxt = string
            target = ("msgctxt", 0)
        elif word == "msgid":
            if cur is None:
                start(number)
            elif cur.has_id:
                cat.syntax.append((number, "second msgid in one entry"))
                continue
            assert cur is not None
            cur.msgid, cur.has_id, cur.line = string, True, number
            target = ("msgid", 0)
        elif cur is None or not cur.has_id:
            cat.syntax.append((number, f"{word} before msgid"))
        elif word == "msgid_plural":
            if cur.msgstr or cur.msgid_plural is not None:
                cat.syntax.append((number, "misplaced msgid_plural"))
                continue
            cur.msgid_plural = string
            target = ("msgid_plural", 0)
        else:
            add_msgstr(cat, cur, number, index, string)
            target = ("msgstr", int(index or 0))
    close()
    return cat


def append(entry: Entry, target: tuple[str, int], piece: str) -> None:
    """Add a continuation line to the keyword it belongs to."""
    word, index = target
    if word == "msgctxt":
        entry.msgctxt = (entry.msgctxt or "") + piece
    elif word == "msgid":
        entry.msgid += piece
    elif word == "msgid_plural":
        entry.msgid_plural = (entry.msgid_plural or "") + piece
    else:
        entry.msgstr[index] = entry.msgstr.get(index, "") + piece


def add_msgstr(cat: Catalog, entry: Entry, number: int, index: str | None, string: str) -> None:
    if (index is not None) != (entry.msgid_plural is not None):
        what = "msgstr[n] without msgid_plural" if index else "msgstr in a plural entry"
        cat.syntax.append((number, what))
        return
    key = int(index or 0)
    if key in entry.msgstr:
        cat.syntax.append((number, f"msgstr[{key}] twice in one entry"))
        return
    entry.msgstr[key] = string


# --- the text of one entry ---------------------------------------------------


def placeholders(text: str, off: frozenset[str] = frozenset()) -> Counter[str]:
    """The placeholders of ``text`` as a multiset; ``%%``, ``{{`` and ``}}`` are skipped."""
    found: Counter[str] = Counter()
    for m in PLACEHOLDER.finditer(text):
        if m.group("printf") and "no-c-format" not in off:
            found[m.group("printf")] += 1
        elif m.group("brace") and "no-python-brace-format" not in off:
            found[m.group("brace")] += 1
    return found


def is_numeric(token: str) -> bool:
    """Does the placeholder hold a number (so that msgstr[0] may say "one" instead)?"""
    if token.startswith("%"):
        return token[-1] in "diouxXeEfFgG"
    inner = token[1:-1]
    name, _, spec = inner.partition(":")
    name = name.partition("!")[0]
    return name == "" or name.isdigit() or name in NUMERIC_NAMES or spec[-1:] in set("dfeEgGnxXob%")


def tags(text: str) -> tuple[Counter[str], dict[str, set[str]]]:
    """The markup tags as a multiset of names (``/b`` closes), and their attributes."""
    names: Counter[str] = Counter()
    attrs: dict[str, set[str]] = {}
    for m in TAG.finditer(text):
        name = m.group(2).lower()
        if name not in MARKUP_TAGS:
            continue
        key = ("/" if m.group(1) else "") + name
        names[key] += 1
        attrs.setdefault(key, set()).add(" ".join(m.group(3).split()))
    return names, attrs


def show(counter: Counter[str]) -> str:
    return ", ".join(sorted(counter.elements()))


def difference(missing: Counter[str], extra: Counter[str]) -> str:
    parts = [f"{label} {show(c)}" for label, c in (("missing", missing), ("extra", extra)) if c]
    return "; ".join(parts)


def shorten(text: str, limit: int = 48) -> str:
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def where(entry: Entry) -> str:
    return f'line {entry.line} (msgid "{shorten(entry.msgid)}")'


def check_text(path: str, entry: Entry, index: int, source: str, translation: str) -> list[Problem]:
    """Compare one non-empty translation with its source (msgid or msgid_plural)."""
    here = where(entry) + (f" msgstr[{index}]" if entry.msgid_plural is not None else "")
    out: list[Problem] = []
    off = entry.flags
    want, got = placeholders(source, off), placeholders(translation, off)
    missing, extra = want - got, got - want
    if entry.msgid_plural is not None and index == 0:
        # The singular may be written in words: "One file" instead of "{n} files".
        numeric = Counter({k: v for k, v in missing.items() if is_numeric(k)})
        if numeric:
            message = f"{here}: msgstr[0] has no numeric placeholder: {show(numeric)}"
            out.append(warn(path, message, "plural-placeholders"))
            missing -= numeric
        extra -= placeholders(entry.msgid, off)
    if missing or extra:
        message = f"{here}: placeholders differ from the msgid: {difference(missing, extra)}"
        out.append(error(path, message, "placeholders"))
    out += check_markup(path, here, source, translation)
    out += check_edges(path, here, source, translation)
    out += check_names(path, here, source, translation)
    return out


def check_markup(path: str, here: str, source: str, translation: str) -> list[Problem]:
    want, want_attrs = tags(source)
    got, got_attrs = tags(translation)
    if want != got:
        message = f"{here}: markup tags differ from the msgid: {difference(want - got, got - want)}"
        return [error(path, message, "markup")]
    if any(want_attrs[k] != got_attrs[k] for k in want_attrs):
        message = f"{here}: attributes of the markup tags differ from the msgid"
        return [warn(path, message, "markup-attributes")]
    return []


def check_edges(path: str, here: str, source: str, translation: str) -> list[Problem]:
    """A newline or a space at the start or at the end must be kept (or absent)."""
    out: list[Problem] = []
    for ch, name in (("\n", "newline"), (" ", "space")):
        for at, test in (("start", str.startswith), ("end", str.endswith)):
            if test(source, ch) != test(translation, ch):
                have = "has" if test(source, ch) else "has no"
                message = f"{here}: msgid {have} {name} at the {at}, msgstr differs"
                out.append(warn(path, message, "whitespace"))
    return out


def check_names(path: str, here: str, source: str, translation: str) -> list[Problem]:
    """The names Vilna, Gloxi, Herkos, Blavet and Tinta keep their stem in a translation."""
    folded = translation.casefold()
    return [
        warn(path, f"{here}: msgid has {name}, msgstr has no {stem}…", "names")
        for name, stem in NAME_STEMS
        if name in source and stem.casefold() not in folded
    ]


# --- the file ------------------------------------------------------------------


def language_of(path: str) -> str:
    return posixpath.basename(path).rpartition(".")[0]


def header_fields(entries: list[Entry]) -> dict[str, str] | None:
    """The ``Key: value`` lines of the header entry (keys in lower case), or None."""
    for entry in entries:
        if entry.msgid == "" and entry.msgctxt is None and entry.has_id:
            fields: dict[str, str] = {}
            for line in entry.msgstr.get(0, "").split("\n"):
                key, colon, value = line.partition(":")
                if colon:
                    fields.setdefault(key.strip().lower(), value.strip())
            return fields
    return None


def check_header(path: str, entries: list[Entry]) -> list[Problem]:
    fields = header_fields(entries)
    if fields is None:
        return [error(path, 'no header entry (msgid "")', "header")]
    out = check_content_type(path, fields)
    if path.endswith(".pot"):
        return out
    lang = language_of(path)
    declared = fields.get("language")
    if declared is None:
        message = f"header has no Language (the file name says {lang})"
        out.append(error(path, message, "header-language"))
    elif declared != lang:
        message = f"header Language is {declared!r}, the file name says {lang!r}"
        out.append(error(path, message, "header-language"))
    return out + check_plural_forms(path, fields, lang)


def check_content_type(path: str, fields: dict[str, str]) -> list[Problem]:
    value = fields.get("content-type")
    if value is None:
        return [error(path, "header has no Content-Type (need charset=UTF-8)", "header-charset")]
    match = CHARSET.search(value)
    charset = match.group(1) if match else None
    if charset is None or charset.lower() not in ("utf-8", "utf8"):
        message = f"header Content-Type has charset {charset!r}, need UTF-8"
        return [error(path, message, "header-charset")]
    return []


def check_plural_forms(path: str, fields: dict[str, str], lang: str) -> list[Problem]:
    value = fields.get("plural-forms")
    if value is None:
        return [error(path, "header has no Plural-Forms", "header-plural")]
    count = NPLURALS.search(value)
    if count is None or PLURAL_EXPR.search(value) is None:
        message = f"header Plural-Forms is not 'nplurals=N; plural=EXPR;': {value!r}"
        return [error(path, message, "header-plural")]
    expected = PLURALS.get(lang)
    if expected is None:
        message = f"nplurals not checked: no table for the language {lang!r}"
        return [warn(path, message, "header-nplurals")]
    wanted, certain = expected
    if int(count.group(1)) == wanted:
        return []
    message = f"header nplurals is {count.group(1)}, {lang} has {wanted}"
    if not certain:
        return [warn(path, message + " (not certain)", "header-nplurals")]
    return [error(path, message, "header-nplurals")]


def nplurals_of(entries: list[Entry]) -> int | None:
    fields = header_fields(entries)
    match = NPLURALS.search((fields or {}).get("plural-forms", ""))
    return int(match.group(1)) if match else None


def check_entries(path: str, cat: Catalog, is_pot: bool) -> list[Problem]:
    out: list[Problem] = []
    seen: dict[tuple[str | None, str], int] = {}
    nplurals = nplurals_of(cat.entries)
    for entry in cat.entries:
        key = (entry.msgctxt, entry.msgid)
        if key in seen:
            message = f"{where(entry)} is a duplicate of line {seen[key]}"
            out.append(error(path, message, "duplicate"))
        seen.setdefault(key, entry.line)
        if entry.fuzzy:
            out.append(warn(path, f"{where(entry)} is fuzzy", "fuzzy"))
        elif not is_pot and entry.msgid != "":
            out += check_entry(path, entry, nplurals)
    if cat.obsolete:
        out.append(warn(path, f"obsolete (#~) entries: {cat.obsolete}", "obsolete"))
    return out


def check_entry(path: str, entry: Entry, nplurals: int | None) -> list[Problem]:
    """The placeholders, tags, edges and names of a translated, not fuzzy entry."""
    if entry.msgid_plural is None:
        text = entry.msgstr.get(0, "")
        return check_text(path, entry, 0, entry.msgid, text) if text else []
    out: list[Problem] = []
    for index, text in sorted(entry.msgstr.items()):
        if text:
            out += check_text(path, entry, index, entry.msgid_plural, text)
    if nplurals is not None and any(entry.msgstr.values()) and len(entry.msgstr) != nplurals:
        message = f"{where(entry)} has {len(entry.msgstr)} msgstr[n], but nplurals={nplurals}"
        out.append(warn(path, message, "plural-count"))
    return out


def check_catalog(path: str, data: bytes) -> list[Problem]:
    """Check one catalogue given as the bytes of the file."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [error(path, "the file is not valid UTF-8", "encoding")]
    problems: list[Problem] = []
    if text.startswith("\ufeff"):
        problems.append(error(path, "the file starts with a BOM, remove it", "encoding"))
        text = text[1:]
    cat = parse(text)
    problems += [error(path, f"line {n}: {message}", "syntax") for n, message in cat.syntax]
    problems += check_header(path, cat.entries)
    return problems + check_entries(path, cat, path.endswith(".pot"))


def downgrade(problem: Problem) -> Problem:
    """Turn a known old violation (see LEGACY) into a warning."""
    if (problem.path, problem.rule) in LEGACY:
        return Problem(problem.path, problem.message, warning=True, rule=problem.rule)
    return problem


def check_files(root: Path, paths: Sequence[str]) -> list[Problem]:
    """Check the catalogues ``paths`` (from ``root``); old violations become warnings."""
    problems: list[Problem] = []
    for rel in sorted(paths):
        try:
            data = (root / rel).read_bytes()
        except OSError as err:
            problems.append(error(rel, f"cannot read the file: {err.strerror or err}", "read"))
            continue
        problems += [downgrade(p) for p in check_catalog(rel, data)]
    return problems


def tracked_catalogs(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.po", "*.pot"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        errors="surrogateescape",
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    usage = "usage: python3 tools/check_po_format.py [FILE ...]"
    options = [a for a in args if a.startswith("-")]
    if any(o not in ("-h", "--help") for o in options):
        print(usage, file=sys.stderr)
        return 2
    if options:
        print(usage, file=sys.stderr)
        return 0
    try:
        tracked = tracked_catalogs(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    only = [a.replace("\\", "/") for a in args]
    unknown = [a for a in only if a not in tracked]
    if unknown:
        print(f"not a tracked .po or .pot file: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if not tracked:
        print("no tracked .po or .pot files found", file=sys.stderr)
        return 2
    problems = check_files(ROOT, only or tracked)
    for problem in problems:
        print(problem, file=sys.stderr)
    errors = sum(not p.warning for p in problems)
    print(
        f"{len(only or tracked)} catalogues checked: "
        f"{errors} errors, {len(problems) - errors} warnings"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    # A file name that is not UTF-8 is printed escaped, not as a traceback.
    sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    sys.exit(main())
