#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the GNOME Shell extensions of the repo: ``metadata.json`` and the settings schema.

Every tracked ``metadata.json`` that belongs to an extension (its folder has an
``extension.js``, or the file has ``uuid`` or ``shell-version``) is read as JSON (a
duplicate key is an error) and checked:

* ``uuid`` is a string of ``[-a-zA-Z0-9@._]`` and equals the name of the folder;
  ``name`` and ``description`` are non-empty strings;
* ``shell-version`` is a non-empty array of strings (``"46"``, ``"3.38"``, ``"47.beta"``),
  not numbers, with no repeat and no leading zero;
* the versions the image has are in ``shell-version`` (an extension that does not list the
  Shell of the image is not loaded). The image's versions come from the optional line
  ``gnome_shell_versions: ["49"]`` of ``image/packages.yaml`` (a flow list or a block list).
  A version in ``shell-version`` that the image does not have is a warning (the extension
  may be ready for newer Shells). Without the line nothing can be compared: one warning;
* ``session-modes`` (if present) is an array of ``user``, ``unlock-dialog``, ``gdm``;
  ``version`` is an integer, ``version-name``, ``url`` and ``gettext-domain`` are strings;
* ``settings-schema`` (if present) is the id of a ``<schema>`` in a ``schemas/*.gschema.xml``
  file of the extension, and the file is named ``<id>.gschema.xml``. A schema folder with no
  ``settings-schema`` is a warning (the extension cannot find its settings).

Every ``schemas/*.gschema.xml`` of the extension is read as XML and checked like
``glib-compile-schemas`` would: the root is ``<schemalist>``; schema ids are valid and unique;
a ``path`` starts and ends with ``/``; a key name is lowercase words with dashes and is unique
in its schema; a key has exactly one of ``type``, ``enum`` and ``flags``, and exactly one
``<default>``; a ``type`` is a valid GVariant type string; the ``<default>`` is parsed as GVariant
text of that type (a ``true`` for ``type="i"`` is an error, so is ``300`` for ``y``); an enum
or flags key names an ``<enum>`` or ``<flags>`` of the schemas of the same folder and its
default is a nick of it; a ``<range>`` is only for numbers, min <= max, and the default is in
it; ``<choices>`` values have the type of the key and the default is one of them.

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but do not fail the
check. Only ``git ls-files`` is run (to find the files). Nothing else is executed, nothing is
read from the network and nothing is written.

Run: python3 tools/check_shell_extension.py [metadata.json ...]
Without arguments every tracked extension is checked. With arguments, paths are relative to
the current folder and the image versions are not read. Exit code: 0 all good (warnings
allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import json
import re
import subprocess
import os
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

PACKAGES_YAML = "image/packages.yaml"
MAX_BYTES = 2_000_000
MAX_DEPTH = 64
UUID = re.compile(r"[-a-zA-Z0-9@._]+")
VERSION_PART = r"(?:0|[1-9][0-9]*)"
VERSION = re.compile(rf"{VERSION_PART}(?:\.(?:{VERSION_PART}|alpha|beta|rc))*")
SESSION_MODES = ("user", "unlock-dialog", "gdm")
SCHEMA_ID = re.compile(r"[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)*")
KEY_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
NICK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
KEY_CHILDREN = ("default", "summary", "description", "range", "choices", "aliases")
SCHEMA_CHILDREN = ("key", "child", "override", "description", "summary")
INTEGER_RANGES = {
    "y": (0, 2**8 - 1),
    "n": (-(2**15), 2**15 - 1),
    "q": (0, 2**16 - 1),
    "i": (-(2**31), 2**31 - 1),
    "u": (0, 2**32 - 1),
    "x": (-(2**63), 2**63 - 1),
    "t": (0, 2**64 - 1),
    "h": (-(2**31), 2**31 - 1),
}
NUMERIC = frozenset(INTEGER_RANGES) | {"d"}
# Problems the repo already has: (path, text of the message) -> why. Warnings, not errors.
# Fix the problem, then delete the line.
LEGACY: dict[tuple[str, str], str] = {}


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False

    def __str__(self) -> str:
        return f"{self.path}: {'warning: ' if self.warning else ''}{self.message}"


# --- GVariant types ----------------------------------------------------------


def type_end(text: str, start: int = 0) -> int | None:
    """Index after the one complete concrete GVariant type at ``start``, or ``None``."""
    if start >= len(text):
        return None
    head = text[start]
    if head in "bynqiuxthdsogv":
        return start + 1
    if head in "am":
        return type_end(text, start + 1)
    if head == "(":
        pos = start + 1
        while pos < len(text) and text[pos] != ")":
            nxt = type_end(text, pos)
            if nxt is None:
                return None
            pos = nxt
        return pos + 1 if pos < len(text) else None
    if head == "{":
        if start + 1 >= len(text) or text[start + 1] not in "bynqiuxthdsog":
            return None
        value_end = type_end(text, start + 2)
        if value_end is None or value_end >= len(text) or text[value_end] != "}":
            return None
        return value_end + 1
    return None


def is_type(text: str) -> bool:
    return type_end(text) == len(text)


def is_signature(text: str) -> bool:
    """A D-Bus signature: zero or more complete types."""
    pos = 0
    while pos < len(text):
        nxt = type_end(text, pos)
        if nxt is None:
            return False
        pos = nxt
    return True


def tuple_items(type_: str) -> list[str]:
    items = []
    pos = 1
    while type_[pos] != ")":
        end = type_end(type_, pos)
        assert end is not None
        items.append(type_[pos:end])
        pos = end
    return items


# --- GVariant text -----------------------------------------------------------


class GVariantError(ValueError):
    """The text is not a GVariant of the wanted type."""


ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
NUMBER = re.compile(r"-?(?:0[xX][0-9a-fA-F]+|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)")
PREFIXES = {
    "boolean": "b",
    "byte": "y",
    "int16": "n",
    "uint16": "q",
    "int32": "i",
    "uint32": "u",
    "int64": "x",
    "uint64": "t",
    "handle": "h",
    "double": "d",
    "string": "s",
    "objectpath": "o",
    "signature": "g",
}
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Parser:
    """Parse the GVariant text format; ``parse(want)`` returns ``(type, value)``.

    ``want`` is the type the text must have, or ``None`` when it has to tell its own type
    (inside ``<...>``). Nothing is evaluated; every error is a ``GVariantError``.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.depth = 0

    def fail(self, message: str) -> GVariantError:
        return GVariantError(f"{message} (at character {self.pos + 1})")

    def skip(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t\r\n":
            self.pos += 1

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def expect(self, char: str) -> None:
        self.skip()
        if self.peek() != char:
            raise self.fail(f"expected '{char}'")
        self.pos += 1

    def parse_all(self, want: str | None) -> tuple[str, object]:
        result = self.parse(want)
        self.skip()
        if self.pos < len(self.text):
            raise self.fail("unexpected text after the value")
        return result

    def parse(self, want: str | None) -> tuple[str, object]:
        self.depth += 1
        try:
            if self.depth > MAX_DEPTH:
                raise self.fail(f"the value is nested more than {MAX_DEPTH} levels deep")
            return self.parse_one(want)
        finally:
            self.depth -= 1

    def parse_one(self, want: str | None) -> tuple[str, object]:
        self.skip()
        char = self.peek()
        if not char:
            raise self.fail("the value is empty")
        if char == "@":
            return self.annotated(want)
        word = WORD.match(self.text, self.pos)
        if word and self.text[word.start() : word.end()] in PREFIXES:
            if self.text[word.end() : word.end() + 1] not in " \t\r\n":
                raise self.fail(f"'{word.group()}' needs a value after it")
            self.pos = word.end()
            return self.checked(PREFIXES[word.group()], want, self.parse(PREFIXES[word.group()]))
        if want and want[0] == "m" and not self.starts_maybe_word():
            return want, ("just", self.parse(want[1:])[1])
        if char == "<":
            return self.variant(want)
        if char == "[":
            return self.array(want)
        if char == "(":
            return self.tuple(want)
        if char == "{":
            return self.braces(want)
        if char in "'\"":
            return self.checked(self.string_type(want), want, self.string())
        if char == "b" and self.text[self.pos + 1 : self.pos + 2] in ("'", '"'):
            self.pos += 1
            raw = self.string()[1]
            if not isinstance(raw, str):
                raise self.fail("not a byte string")
            return self.checked("ay", want, ("ay", [ord(c) for c in raw]))
        if word:
            return self.word(word.group(), want)
        return self.number(want)

    def starts_maybe_word(self) -> bool:
        word = WORD.match(self.text, self.pos)
        return bool(word and word.group() in ("nothing", "just"))

    def annotated(self, want: str | None) -> tuple[str, object]:
        end = type_end(self.text, self.pos + 1)
        if end is None:
            raise self.fail("'@' must be followed by a valid GVariant type")
        annotation = self.text[self.pos + 1 : end]
        self.pos = end
        value = self.parse(annotation)
        return self.checked(annotation, want, value)

    def checked(self, got: str, want: str | None, value: tuple[str, object]) -> tuple[str, object]:
        if want is not None and got != want:
            raise self.fail(f"the value has type '{got}', the key has type '{want}'")
        return value

    def string_type(self, want: str | None) -> str:
        return want if want in ("s", "o", "g") and want is not None else "s"

    def string(self) -> tuple[str, object]:
        quote = self.text[self.pos]
        self.pos += 1
        out: list[str] = []
        while True:
            if self.pos >= len(self.text):
                raise self.fail("a string is not closed")
            char = self.text[self.pos]
            self.pos += 1
            if char == quote:
                break
            if char != "\\":
                out.append(char)
                continue
            if self.pos >= len(self.text):
                raise self.fail("a string is not closed")
            esc = self.text[self.pos]
            self.pos += 1
            if esc in ESCAPES:
                out.append(ESCAPES[esc])
            elif esc in "\\'\"":
                out.append(esc)
            elif esc in "uU":
                size = 4 if esc == "u" else 8
                digits = self.text[self.pos : self.pos + size]
                if len(digits) != size or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                    raise self.fail(f"\\{esc} needs {size} hex digits")
                try:
                    out.append(chr(int(digits, 16)))
                except (ValueError, OverflowError):
                    raise self.fail(f"\\{esc}{digits} is not a character") from None
                self.pos += size
            else:
                raise self.fail(f"unknown escape '\\{esc}' in a string")
        return "s", "".join(out)

    def word(self, word: str, want: str | None) -> tuple[str, object]:
        if word in ("true", "false"):
            self.pos += len(word)
            return self.checked("b", want, ("b", word == "true"))
        if word in ("inf", "nan"):
            self.pos += len(word)
            return self.checked("d", want, ("d", float(word)))
        if word == "nothing":
            self.pos += len(word)
            if want is None or want[0] != "m":
                raise self.fail("'nothing' needs a maybe type (use @mT nothing)")
            return want, None
        if word == "just":
            self.pos += len(word)
            if want is None or want[0] != "m":
                raise self.fail("'just' needs a maybe type (use @mT just ...)")
            _, inner = self.parse(want[1:])
            return want, ("just", inner)
        raise self.fail(f"unknown word '{word}' (a string needs quotes)")

    def number(self, want: str | None) -> tuple[str, object]:
        if self.text.startswith("-inf", self.pos):
            self.pos += 4
            return self.checked("d", want, ("d", float("-inf")))
        match = NUMBER.match(self.text, self.pos)
        if not match:
            raise self.fail(f"cannot read the value '{self.text[self.pos : self.pos + 12]}'")
        token = match.group()
        self.pos = match.end()
        if self.pos < len(self.text) and (
            self.text[self.pos].isalnum() or self.text[self.pos] == "_"
        ):
            raise self.fail(f"'{token}{self.text[self.pos]}' is not a number")
        is_float = not re.fullmatch(r"-?(?:0[xX][0-9a-fA-F]+|[0-9]+)", token)
        if want == "d":
            return "d", float(token) if is_float else float(self.integer(token))
        if want is not None and want not in INTEGER_RANGES:
            raise self.fail(f"a number, but the key has type '{want}'")
        if is_float:
            raise self.fail(f"'{token}' is not an integer")
        number = self.integer(token)
        type_ = want or "i"
        low, high = INTEGER_RANGES[type_]
        if not low <= number <= high:
            raise self.fail(f"{token} is out of range for type '{type_}' ({low}..{high})")
        return type_, number

    @staticmethod
    def octal(token: str) -> bool:
        return len(token) > 1 and token[0] == "0" and token.isdigit()

    def integer(self, token: str) -> int:
        try:
            if self.octal(token.lstrip("-")):
                return int(token, 8)
            return int(token, 0)
        except ValueError:
            raise self.fail(f"'{token}' is not a valid number") from None

    def variant(self, want: str | None) -> tuple[str, object]:
        if want not in (None, "v"):
            raise self.fail(f"a variant <...>, but the key has type '{want}'")
        self.pos += 1
        inner = self.parse(None)
        self.expect(">")
        return "v", inner

    def items(self, close: str, want: str | None) -> list[tuple[str, object]]:
        """Comma-separated values up to ``close``; each is parsed as ``want`` (or any)."""
        out: list[tuple[str, object]] = []
        self.skip()
        if self.peek() == close:
            self.pos += 1
            return out
        while True:
            item = self.parse(want)
            if want is None and out and item[0] != out[0][0]:
                raise self.fail(f"the elements have types '{out[0][0]}' and '{item[0]}'")
            out.append(item)
            self.skip()
            if self.peek() == ",":
                self.pos += 1
                self.skip()
                if self.peek() == close:
                    self.pos += 1
                    return out
                continue
            self.expect(close)
            return out

    def array(self, want: str | None) -> tuple[str, object]:
        if want is not None and want[0] != "a":
            raise self.fail(f"an array [...], but the key has type '{want}'")
        self.pos += 1
        elem = want[1:] if want else None
        values = self.items("]", elem)
        if not values and want is None:
            raise self.fail("an empty array needs its type (use @aT [])")
        type_ = want or "a" + values[0][0]
        return type_, [v for _, v in values]

    def tuple(self, want: str | None) -> tuple[str, object]:
        if want is not None and want[0] != "(":
            raise self.fail(f"a tuple (...), but the key has type '{want}'")
        self.pos += 1
        kinds = tuple_items(want) if want else None
        values: list[tuple[str, object]] = []
        self.skip()
        if self.peek() == ")":
            self.pos += 1
        else:
            while True:
                index = len(values)
                if kinds is not None and index >= len(kinds):
                    raise self.fail(f"too many items in the tuple, the type is '{want}'")
                values.append(self.parse(kinds[index] if kinds else None))
                self.skip()
                if self.peek() == ",":
                    self.pos += 1
                    self.skip()
                    if self.peek() == ")":
                        self.pos += 1
                        break
                    continue
                self.expect(")")
                break
        if kinds is not None and len(values) != len(kinds):
            raise self.fail(
                f"the tuple has {len(values)} items, the type '{want}' has {len(kinds)}"
            )
        return want or "(" + "".join(t for t, _ in values) + ")", tuple(v for _, v in values)

    def braces(self, want: str | None) -> tuple[str, object]:
        if want is not None and want[0] not in "a{":
            raise self.fail(f"a dictionary {{...}}, but the key has type '{want}'")
        entry = want.startswith("{") if want else False
        elem = want[1:] if want and want[0] == "a" else want
        if want and want[0] == "a" and not elem.startswith("{"):
            raise self.fail(f"a dictionary {{...}}, but the key has type '{want}'")
        key_type = elem[1] if elem else None
        value_type = elem[2:-1] if elem else None
        self.pos += 1
        if entry:
            key = self.parse(key_type)
            self.expect(",")
            value = self.parse(value_type)
            self.expect("}")
            return want or "{" + key[0] + value[0] + "}", (key[1], value[1])
        pairs: list[tuple[tuple[str, object], tuple[str, object]]] = []
        self.skip()
        if self.peek() == "}":
            self.pos += 1
        else:
            while True:
                key = self.parse(key_type)
                self.expect(":")
                value = self.parse(value_type)
                if key_type is None:
                    key_type, value_type = key[0], value[0]
                pairs.append((key, value))
                self.skip()
                if self.peek() == ",":
                    self.pos += 1
                    self.skip()
                    if self.peek() == "}":
                        self.pos += 1
                        break
                    continue
                self.expect("}")
                break
        if want is None:
            if not pairs:
                raise self.fail("an empty dictionary needs its type (use @a{KV} {})")
            want = f"a{{{pairs[0][0][0]}{pairs[0][1][0]}}}"
        return want, [(k[1], v[1]) for k, v in pairs]


def parse_gvariant(text: str, type_: str) -> object:
    """The value of GVariant ``text`` of ``type_``; ``GVariantError`` if it does not fit."""
    parsed = Parser(text).parse_all(type_)[1]
    check_special(type_, parsed)
    return parsed


def check_special(type_: str, value: object) -> None:
    """Object paths and signatures are strings with a grammar of their own."""
    if type_ == "o" and not (
        isinstance(value, str) and (value == "/" or re.fullmatch(r"(?:/[A-Za-z0-9_]+)+", value))
    ):
        raise GVariantError(f"{value!r} is not a D-Bus object path")
    if type_ == "g" and not (isinstance(value, str) and is_signature(value)):
        raise GVariantError(f"{value!r} is not a D-Bus signature")


# --- the schema --------------------------------------------------------------


def shown(text: str, limit: int = 60) -> str:
    """The text for a message: quoted, and cut when it is long."""
    return repr(text if len(text) <= limit else text[:limit] + "...")


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def children(element: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in element if c.tag == name]


@dataclass
class Enumeration:
    kind: str  # enum or flags
    nicks: dict[str, int]


def collect_enums(path: str, root: ET.Element) -> tuple[dict[str, Enumeration], list[Problem]]:
    enums: dict[str, Enumeration] = {}
    problems: list[Problem] = []
    for element in root:
        if element.tag not in ("enum", "flags"):
            continue
        enum_id = element.get("id", "")
        label = f"<{element.tag}> '{enum_id}'"
        if not SCHEMA_ID.fullmatch(enum_id):
            problems.append(Problem(path, f"{label}: the id is missing or not valid"))
        elif enum_id in enums:
            problems.append(Problem(path, f"{label}: the id is used twice"))
        nicks: dict[str, int] = {}
        values: set[int] = set()
        for value in element:
            if value.tag != "value":
                problems.append(Problem(path, f"{label}: unknown element <{local(value.tag)}>"))
                continue
            nick, number = value.get("nick", ""), value.get("value", "")
            if not NICK.fullmatch(nick):
                problems.append(Problem(path, f"{label}: a <value> has no valid nick"))
                continue
            try:
                parsed = int(number, 0)
            except ValueError:
                problems.append(Problem(path, f"{label}: nick '{nick}' has value '{number}'"))
                continue
            if not -(2**31) <= parsed < 2**32:
                problems.append(Problem(path, f"{label}: nick '{nick}': value {parsed} is too big"))
            if nick in nicks:
                problems.append(Problem(path, f"{label}: nick '{nick}' is used twice"))
            if parsed in values:
                problems.append(Problem(path, f"{label}: value {parsed} is used twice"))
            nicks[nick] = parsed
            values.add(parsed)
        if not nicks:
            problems.append(Problem(path, f"{label}: no <value>"))
        enums.setdefault(enum_id, Enumeration(element.tag, nicks))
    return enums, problems


def check_key(
    path: str, schema_id: str, key: ET.Element, enums: dict[str, Enumeration]
) -> list[Problem]:
    name = key.get("name", "")
    label = f"schema {schema_id}, key '{name}'"
    problems: list[Problem] = []

    def bad(message: str) -> None:
        problems.append(Problem(path, f"{label}: {message}"))

    if not KEY_NAME.fullmatch(name):
        bad("the name must be lowercase words joined by single dashes")
    for child in key:
        if child.tag not in KEY_CHILDREN:
            bad(f"unknown element <{local(child.tag)}>")
    kinds = [a for a in ("type", "enum", "flags") if a in key.attrib]
    if len(kinds) != 1:
        bad("it needs exactly one of the attributes type, enum and flags")
        return problems
    defaults = children(key, "default")
    if len(defaults) != 1:
        bad(f"it needs exactly one <default>, it has {len(defaults)}")
        return problems
    default_text = "".join(defaults[0].itertext()).strip()
    kind = kinds[0]
    if kind == "type":
        problems += check_typed_key(path, label, key, key.attrib["type"], default_text)
        return problems
    enum_id = key.attrib[kind]
    enum = enums.get(enum_id)
    if enum is None:
        bad(f"{kind}='{enum_id}' is not an <{kind}> of the schemas in this folder")
        return problems
    if enum.kind != kind:
        bad(f"'{enum_id}' is an <{enum.kind}>, the key says {kind}=")
        return problems
    for forbidden in ("range", "choices"):
        if children(key, forbidden):
            bad(f"<{forbidden}> cannot be used with {kind}=")
    want = "s" if kind == "enum" else "as"
    try:
        value = parse_gvariant(default_text, want)
    except GVariantError as err:
        bad(f"the default {shown(default_text)} is not {want}: {err}")
        return problems
    picked = [value] if kind == "enum" else value
    assert isinstance(picked, list)
    for nick in picked:
        if nick not in enum.nicks:
            bad(f"the default has '{nick}', which is not a nick of '{enum_id}'")
    return problems


def check_typed_key(
    path: str, label: str, key: ET.Element, type_: str, default_text: str
) -> list[Problem]:
    problems: list[Problem] = []

    def bad(message: str) -> None:
        problems.append(Problem(path, f"{label}: {message}"))

    if not is_type(type_):
        bad(f"type='{type_}' is not a valid GVariant type string")
        return problems
    try:
        default = parse_gvariant(default_text, type_)
    except GVariantError as err:
        bad(f"the default {shown(default_text)} does not fit type '{type_}': {err}")
        return problems
    ranges, choices = children(key, "range"), children(key, "choices")
    if len(ranges) + len(choices) > 1:
        bad("<range> and <choices> cannot be used together, or twice")
        return problems
    if ranges:
        problems += check_range(path, label, ranges[0], type_, default)
    if choices:
        problems += check_choices(path, label, choices[0], type_, default)
    return problems


def check_range(
    path: str, label: str, element: ET.Element, type_: str, default: object
) -> list[Problem]:
    if type_ not in NUMERIC:
        return [Problem(path, f"{label}: <range> is for numbers, the type is '{type_}'")]
    limits: list[int | float] = []
    for attr in ("min", "max"):
        text = element.get(attr)
        if text is None:
            return [Problem(path, f"{label}: <range> has no {attr}")]
        try:
            limits.append(parse_gvariant(text, type_))
        except GVariantError as err:
            return [
                Problem(path, f"{label}: <range> {attr}='{text}' does not fit '{type_}': {err}")
            ]
    low, high = limits
    if low > high:
        return [Problem(path, f"{label}: <range> min {low} is bigger than max {high}")]
    assert isinstance(default, (int, float))
    if not low <= default <= high:
        return [Problem(path, f"{label}: the default {default} is outside <range> {low}..{high}")]
    return []


def check_choices(
    path: str, label: str, element: ET.Element, type_: str, default: object
) -> list[Problem]:
    problems: list[Problem] = []
    values: list[object] = []
    for choice in element:
        if choice.tag not in ("choice", "alias"):
            problems.append(Problem(path, f"{label}: <choices> has <{local(choice.tag)}>"))
            continue
        if choice.tag == "alias":
            continue
        text = choice.get("value")
        if text is None:
            problems.append(Problem(path, f"{label}: a <choice> has no value"))
            continue
        try:
            if type_ in ("s", "o", "g"):
                check_special(type_, text)
                values.append(text)
            else:
                values.append(parse_gvariant(text, type_))
        except GVariantError as err:
            problems.append(Problem(path, f"{label}: <choice value='{text}'>: {err}"))
    if not values and not problems:
        problems.append(Problem(path, f"{label}: <choices> has no <choice>"))
    elif values and default not in values:
        problems.append(Problem(path, f"{label}: the default {default!r} is not one of <choices>"))
    return problems


def check_schema_element(
    path: str, schema: ET.Element, enums: dict[str, Enumeration], seen: dict[str, str]
) -> list[Problem]:
    schema_id = schema.get("id", "")
    problems: list[Problem] = []
    if not SCHEMA_ID.fullmatch(schema_id):
        problems.append(Problem(path, f"<schema id='{schema_id}'>: the id is missing or not valid"))
    elif schema_id in seen:
        problems.append(Problem(path, f"schema {schema_id}: the id is also in {seen[schema_id]}"))
    else:
        seen[schema_id] = path
    schema_path = schema.get("path")
    if schema_path is not None and not (
        schema_path.startswith("/") and schema_path.endswith("/") and "//" not in schema_path
    ):
        problems.append(
            Problem(path, f"schema {schema_id}: path '{schema_path}' must start and end with '/'")
        )
    names: set[str] = set()
    for child in schema:
        if child.tag not in SCHEMA_CHILDREN:
            problems.append(
                Problem(path, f"schema {schema_id}: unknown element <{local(child.tag)}>")
            )
        elif child.tag == "key":
            name = child.get("name", "")
            if name in names:
                problems.append(Problem(path, f"schema {schema_id}: key '{name}' is used twice"))
            names.add(name)
            problems += check_key(path, schema_id, child, enums)
    return problems


# --- files -------------------------------------------------------------------


def read_text(root: Path, rel: str) -> tuple[str | None, list[Problem]]:
    """The text of a file; a byte order mark is one clear error, then ignored."""
    try:
        data = (root / rel).read_bytes()
    except OSError as err:
        return None, [Problem(rel, f"cannot read the file: {err.strerror or err}")]
    if len(data) > MAX_BYTES:
        return None, [Problem(rel, f"the file is bigger than {MAX_BYTES} bytes")]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, [Problem(rel, "the file is not valid UTF-8")]
    if text.startswith("\ufeff"):
        return text[1:], [Problem(rel, "the file starts with a byte order mark (BOM)")]
    return text, []


def check_schema_files(
    root: Path, folder: str
) -> tuple[dict[str, str], dict[str, Enumeration], list[Problem], bool]:
    """Read every ``*.gschema.xml`` of ``folder``.

    Returns ``({schema id: file}, enums, problems, some file could not be read)``.
    """
    problems: list[Problem] = []
    directory = root / folder
    names = sorted(p.name for p in directory.glob("*.gschema.xml")) if directory.is_dir() else []
    parsed: list[tuple[str, ET.Element]] = []
    enums: dict[str, Enumeration] = {}
    for name in names:
        rel = f"{folder}/{name}"
        text, found = read_text(root, rel)
        problems += found
        if text is None:
            continue
        try:
            element = ET.fromstring(text)
        except ET.ParseError as err:
            problems.append(Problem(rel, f"not well-formed XML: {err}"))
            continue
        if element.tag != "schemalist":
            problems.append(
                Problem(rel, f"the root element is <{local(element.tag)}>, not <schemalist>")
            )
            continue
        found_enums, found = collect_enums(rel, element)
        problems += found
        for enum_id, enum in found_enums.items():
            if enum_id in enums:
                problems.append(
                    Problem(rel, f"<{enum.kind}> '{enum_id}': the id is also in another file")
                )
            enums.setdefault(enum_id, enum)
        parsed.append((rel, element))
    seen: dict[str, str] = {}
    for rel, element in parsed:
        for child in element:
            if child.tag == "schema":
                problems += check_schema_element(rel, child, enums, seen)
            elif child.tag not in ("enum", "flags", "description"):
                problems.append(
                    Problem(rel, f"unknown element <{local(child.tag)}> in <schemalist>")
                )
    return seen, enums, problems, len(parsed) < len(names)


def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"the key '{key}' is written twice")
        result[key] = value
    return result


def reject_constant(name: str) -> object:
    raise ValueError(f"{name} is not valid JSON")


def image_versions(text: str) -> list[str] | None:
    """The versions of ``gnome_shell_versions`` in packages.yaml, or ``None`` if not written."""
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        match = re.match(r"^(\s*)gnome_shell_versions:\s*(.*?)\s*(?:#.*)?$", raw)
        if not match:
            continue
        rest = match.group(2)
        if rest.startswith("["):
            flow = rest.strip("[]")
            return [unquote(i) for i in flow.split(",") if i.strip()] if rest.endswith("]") else []
        found: list[str] = []
        indent = len(match.group(1))
        for later in lines[index + 1 :]:
            item = re.match(r"^(\s*)-\s*(.*?)\s*(?:#.*)?$", later)
            if item and len(item.group(1)) >= indent:
                found.append(unquote(item.group(2)))
            elif later.strip() and not later.lstrip().startswith("#"):
                break
        return found
    return None


def unquote(item: str) -> str:
    item = item.strip()
    if len(item) >= 2 and item[0] == item[-1] and item[0] in "'\"":
        return item[1:-1]
    return item


def check_metadata_fields(rel: str, data: dict[str, object], folder: str) -> list[Problem]:
    problems: list[Problem] = []
    uuid = data.get("uuid")
    if not isinstance(uuid, str) or not UUID.fullmatch(uuid):
        problems.append(Problem(rel, "uuid must be a string of letters, digits and - @ . _"))
    elif uuid != Path(folder).name:
        problems.append(
            Problem(rel, f"uuid '{uuid}' is not the name of its folder '{Path(folder).name}'")
        )
    for field in ("name", "description"):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(Problem(rel, f"{field} must be a non-empty string"))
    for field in ("version-name", "url", "gettext-domain"):
        if field in data and not isinstance(data[field], str):
            problems.append(Problem(rel, f"{field} must be a string"))
    if "version" in data and (
        not isinstance(data["version"], int) or isinstance(data["version"], bool)
    ):
        problems.append(Problem(rel, "version must be an integer"))
    modes = data.get("session-modes")
    if "session-modes" in data and not (
        isinstance(modes, list) and all(isinstance(m, str) and m in SESSION_MODES for m in modes)
    ):
        problems.append(
            Problem(rel, f"session-modes must be an array of {', '.join(SESSION_MODES)}")
        )
    return problems


def check_shell_versions(
    rel: str, data: dict[str, object], image: list[str] | None, image_known: bool
) -> list[Problem]:
    versions = data.get("shell-version")
    if not isinstance(versions, list) or not versions:
        return [Problem(rel, "shell-version must be a non-empty array of strings")]
    problems: list[Problem] = []
    good: list[str] = []
    for item in versions:
        if not isinstance(item, str):
            problems.append(
                Problem(rel, f"shell-version has {json.dumps(item)}: versions must be strings")
            )
        elif not VERSION.fullmatch(item):
            problems.append(Problem(rel, f"shell-version has '{item}', which is not a version"))
        elif item in good:
            problems.append(Problem(rel, f"shell-version has '{item}' twice"))
        else:
            good.append(item)
    if problems or not image_known:
        return problems
    if image is None:
        return [
            Problem(
                rel,
                f"{PACKAGES_YAML} has no gnome_shell_versions: shell-version is not "
                "compared with the image",
                warning=True,
            )
        ]
    for version in image:
        if version not in good:
            problems.append(
                Problem(
                    rel, f"the image has GNOME Shell {version}, which shell-version does not list"
                )
            )
    for version in good:
        if version not in image:
            problems.append(
                Problem(
                    rel,
                    f"shell-version '{version}' is not a Shell version of the image",
                    warning=True,
                )
            )
    return problems


def check_settings_schema(
    root: Path, rel: str, data: dict[str, object], folder: str
) -> list[Problem]:
    schema_folder = f"{folder}/schemas" if folder else "schemas"
    ids, _, problems, broken = check_schema_files(root, schema_folder)
    wanted = data.get("settings-schema")
    if "settings-schema" not in data:
        if ids:
            problems.append(
                Problem(rel, "there is a schemas/ folder but no settings-schema", warning=True)
            )
        return problems
    if not isinstance(wanted, str) or not wanted:
        return [*problems, Problem(rel, "settings-schema must be a non-empty string")]
    if wanted not in ids and broken:
        return problems  # a file that cannot be read may hold it; that is reported already
    if wanted not in ids:
        names = ", ".join(sorted(ids)) or "none"
        problems.append(
            Problem(
                rel,
                f"settings-schema '{wanted}' is not in a {schema_folder}/*.gschema.xml file "
                f"(schemas found: {names})",
            )
        )
    elif ids[wanted] != f"{schema_folder}/{wanted}.gschema.xml":
        problems.append(
            Problem(rel, f"the file of schema '{wanted}' must be named {wanted}.gschema.xml")
        )
    return problems


def check_metadata(
    root: Path, rel: str, image: list[str] | None = None, image_known: bool = False
) -> list[Problem]:
    """Every problem of the extension whose ``metadata.json`` is ``rel``."""
    text, problems = read_text(root, rel)
    if text is None:
        return problems
    try:
        data = json.loads(text, object_pairs_hook=reject_duplicates, parse_constant=reject_constant)
    except (ValueError, RecursionError) as err:
        return [*problems, Problem(rel, f"not valid JSON: {err}")]
    if not isinstance(data, dict):
        return [*problems, Problem(rel, "the top level must be a JSON object")]
    folder = Path(rel).parent.as_posix()
    folder = "" if folder == "." else folder
    problems += check_metadata_fields(rel, data, folder if folder else str(root.resolve().name))
    problems += check_shell_versions(rel, data, image, image_known)
    problems += check_settings_schema(root, rel, data, folder)
    return problems


def is_extension(root: Path, rel: str) -> bool:
    if (root / Path(rel).parent / "extension.js").is_file():
        return True
    try:
        data = json.loads((root / rel).read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return True  # unreadable: let check_metadata say what is wrong
    return isinstance(data, dict) and ("uuid" in data or "shell-version" in data)


def apply_legacy(problems: Sequence[Problem]) -> list[Problem]:
    return [
        Problem(p.path, p.message, True) if (p.path, p.message) in LEGACY and not p.warning else p
        for p in problems
    ]


def check_files(root: Path, paths: Sequence[str], image_from: str | None = None) -> list[Problem]:
    """Check the ``metadata.json`` files ``paths`` (relative to ``root``)."""
    image: list[str] | None = None
    image_known = False
    if image_from is not None:
        image_known = True
        text, found = read_text(root, image_from)
        if text is not None:
            image = image_versions(text)
    problems: list[Problem] = []
    for rel in sorted(paths):
        problems += check_metadata(root, rel, image, image_known)
    return apply_legacy(problems)


def tracked_metadata(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "metadata.json", "*/metadata.json"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p and is_extension(root, p))


def main(argv: list[str] | None = None) -> int:
    try:
        args = sys.argv[1:] if argv is None else argv
        usage = "usage: python3 tools/check_shell_extension.py [metadata.json ...]"
        if any(a.startswith("-") for a in args):
            print(usage, file=sys.stderr)
            return 0 if args in (["-h"], ["--help"]) else 2
        if any(Path(a).name != "metadata.json" for a in args):
            print(f"every file must be a metadata.json\n{usage}", file=sys.stderr)
            return 2
        if args:
            paths = args
            base = Path.cwd()
        else:
            base = ROOT
            try:
                paths = tracked_metadata(ROOT)
            except (OSError, subprocess.CalledProcessError) as err:
                print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
                return 2
        if not paths:
            print("no tracked extension metadata.json found", file=sys.stderr)
            return 2
        missing = [a for a in args if not (base / a).is_file()]
        if missing:
            print(f"no such file: {', '.join(missing)}", file=sys.stderr)
            return 2
        problems = check_files(base, paths, None if args else PACKAGES_YAML)
        for problem in problems:
            print(problem, file=sys.stderr)
        errors = sum(not p.warning for p in problems)
        print(f"{len(paths)} extensions checked: {errors} errors, {len(problems) - errors} warnings")
        return 1 if errors else 0
    except (OSError, ValueError) as err:  # no traceback for a bad name, argument or bytes
        print(f"{os.path.basename(__file__)}: cannot check: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
