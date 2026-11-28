#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the tracked SVG files of the repo for safety and correctness.

For every file the checker verifies:

* the file is rejected *before* it is parsed if its text contains ``<!DOCTYPE``
  or ``<!ENTITY`` (a defence against XXE and billion-laughs entities);
* the root element is ``<svg>`` in the SVG namespace
  (``http://www.w3.org/2000/svg``); a ``viewBox`` is present (if the root has
  only ``width``/``height``, that is a warning);
* ``<script>``, any ``on*`` attribute, ``<foreignObject>``, and ``@import`` in
  a ``<style>`` element are errors;
* ``href`` / ``xlink:href`` (and ``url(...)`` in attributes or ``<style>``)
  must not use ``http(s):``, ``file:``, ``javascript:``, ``vbscript:``, or an
  absolute filesystem path; a ``data:`` URI is allowed only for ``image/png``
  and ``image/jpeg``, and even then it is a warning;
* every ``url(#id)`` and ``href="#id"`` names an existing ``id``, and ``id``
  values are unique in the file;
* an absolute or home path (``/home/…``, ``/Users/…``, ``~/…``, ``C:\\…``) in
  any attribute, text or comment is an error (personal data, typical of
  ``sodipodi:docname`` and ``inkscape:export-filename``); the message never
  repeats the path. Other ``inkscape:`` / ``sodipodi:`` attributes and
  elements are a warning;
* a file larger than 200 KiB is a warning;
* an icon whose path contains ``/symbolic/`` should use at most one fill or
  stroke colour besides ``none`` and ``currentColor`` (a warning if it uses
  more).

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but
do not fail the check; they mark things the repo already does today, or
things that are allowed with a reminder.

Only ``git ls-files`` is run (to find the files). Nothing else is executed,
nothing is read from the network and nothing is written.

Run: python3 tools/check_svg.py [FILE.svg ...]
Without arguments every tracked ``*.svg`` file is checked.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
SODIPODI_NS = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
EDITOR_NS = {
    INKSCAPE_NS: "inkscape",
    SODIPODI_NS: "sodipodi",
}
SIZE_LIMIT = 200 * 1024
ALLOWED_DATA = frozenset({"image/png", "image/jpeg"})
# Schemes that load a remote resource, a local file, or a script.
BAD_SCHEMES = frozenset({"http", "https", "file", "javascript", "vbscript"})
# Paints that do not count as a colour of a symbolic icon.
SKIP_PAINTS = frozenset(
    {
        "none",
        "currentcolor",
        "transparent",
        "inherit",
        "initial",
        "unset",
        "context-fill",
        "context-stroke",
    }
)

DOCTYPE_OR_ENTITY = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)
# url(#id), url('#id') or url("#id"), with optional spaces.
URL_REF = re.compile(r"url\(\s*(['\"]?)#([^)'\"]*?)\1\s*\)", re.IGNORECASE)
# Any url(...) whose value has no nested ')'.
URL_ANY = re.compile(r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)", re.IGNORECASE)
CSS_PAINT = re.compile(r"(?<![\w-])(fill|stroke)\s*:\s*([^;}{]+)", re.IGNORECASE)
# Home directories and Windows absolute paths. The message never repeats them.
# A Windows drive must not be the last letter of a URL scheme (http://, file://).
PERSONAL_PATH = re.compile(
    r"/home/|/Users/|~/|(?<![A-Za-z0-9+.-])[A-Za-z]:[\\/]",
)
# Other Unix absolute paths (still personal-looking editor leftovers).
UNIX_ABS = re.compile(
    r"(?:^|[\s\"'=,(])/(?:usr|tmp|var|opt|etc|root|mnt|media|data|srv|run)/"
)
SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
HEX_COLOR = re.compile(r"^#([0-9A-Fa-f]{3,4}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")
COMMENT = re.compile(r"<!--(.*?)-->", re.DOTALL)
EDITOR_PREFIX = re.compile(r"^(inkscape|sodipodi):", re.IGNORECASE)

# http(s) URLs can contain "/home/" or "/Users/" in the path; those are not files.
HTTP_URL = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False

    def __str__(self) -> str:
        return f"{self.path}: {'warning: ' if self.warning else ''}{self.message}"


def local_name(tag: str) -> str:
    """The local part of an ElementTree tag or attribute name."""
    return tag.rsplit("}", 1)[-1]


def namespace(tag: str) -> str | None:
    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]
    return None


def has_filesystem_path(value: str) -> bool:
    """True if ``value`` contains a home directory or an absolute filesystem path."""
    # Drop http(s) URLs so a web path like https://example.com/home/x is not flagged.
    # file: URLs are kept: they often wrap a real home path.
    remainder = HTTP_URL.sub(" ", value)
    return PERSONAL_PATH.search(remainder) is not None or UNIX_ABS.search(remainder) is not None


def scheme_of(value: str) -> str:
    match = SCHEME.match(value.strip())
    return match.group(1).lower() if match else ""


def data_media_type(value: str) -> str:
    """The media type of a ``data:`` URI; empty if the URI has no type or no comma."""
    rest = value.strip()[5:]  # after 'data:'
    if "," not in rest:
        return ""
    header = rest.split(",", 1)[0]
    media = header.split(";", 1)[0].strip().lower()
    return media or "text/plain"


def is_absolute_href(value: str) -> bool:
    """A filesystem or protocol-relative absolute path (not a ``#`` fragment)."""
    text = value.strip()
    if text.startswith("/") or text.startswith("\\"):
        return True
    return re.match(r"[A-Za-z]:[\\/]", text) is not None


def normalize_color(value: str) -> str | None:
    """A comparable colour, or None if this paint does not count (none, currentColor, url())."""
    paint = value.strip()
    if not paint or paint.lower().startswith("url("):
        return None
    # A fallback after a url() is rare in these icons; take the first token-like value.
    low = paint.split()[0].lower() if paint.split() else ""
    if low in SKIP_PAINTS:
        return None
    if HEX_COLOR.match(paint):
        hex_digits = paint[1:]
        if len(hex_digits) in {3, 4}:
            hex_digits = "".join(ch * 2 for ch in hex_digits)
        return f"#{hex_digits.lower()}"
    return re.sub(r"\s+", "", paint).lower()


def parsed_data_problem(path: str, value: str, where: str) -> Problem:
    """A warning for png/jpeg ``data:`` URIs, an error for any other ``data:``."""
    media = data_media_type(value)
    if media in ALLOWED_DATA:
        return Problem(
            path,
            f"{where} uses a data: URI of type {media}",
            warning=True,
        )
    shown = media or "no media type"
    return Problem(
        path,
        f"{where} uses a data: URI of type {shown} (only image/png and image/jpeg are allowed)",
    )


def href_target_problem(path: str, value: str, where: str) -> Problem | None:
    """An error for a forbidden scheme or an absolute path; data: is handled elsewhere."""
    text = value.strip()
    if not text or text.startswith("#"):
        return None
    if scheme_of(text) == "data":
        return None
    scheme = scheme_of(text)
    if scheme in BAD_SCHEMES:
        return Problem(path, f"{where} points at a {scheme}: URI")
    if is_absolute_href(text):
        return Problem(path, f"{where} points at an absolute path")
    return None


def fragment_id(value: str) -> str | None:
    """The id of a ``#fragment`` reference, or None if this is not a fragment."""
    text = value.strip()
    if not text.startswith("#"):
        return None
    return unquote(text[1:])


def iter_urls(value: str) -> Iterator[str]:
    for match in URL_ANY.finditer(value):
        yield match.group(2).strip()


def collect_ids(root: ET.Element) -> dict[str, int]:
    """Map each id to the number of times it appears in the document."""
    counts: dict[str, int] = {}
    for element in root.iter():
        values = [
            element.attrib[name]
            for name in ("id", f"{{{XML_NS}}}id")
            if name in element.attrib
        ]
        for ident in values:
            counts[ident] = counts.get(ident, 0) + 1
    return counts


def check_doctype(path: str, text: str) -> list[Problem]:
    match = DOCTYPE_OR_ENTITY.search(text)
    if not match:
        return []
    found = match.group(0).upper()
    return [
        Problem(
            path,
            f"contains {found} (rejected before parsing, to block XXE)",
        )
    ]


def check_root(path: str, root: ET.Element) -> list[Problem]:
    problems: list[Problem] = []
    if namespace(root.tag) != SVG_NS or local_name(root.tag) != "svg":
        ns = namespace(root.tag)
        shown = local_name(root.tag)
        if ns:
            problems.append(
                Problem(
                    path,
                    f"the root element is <{shown}> in '{ns}', not <svg> in the SVG namespace",
                )
            )
        else:
            problems.append(
                Problem(
                    path,
                    f"the root element is <{shown}> without a namespace, "
                    "not <svg> in the SVG namespace",
                )
            )
        return problems
    viewbox = (root.get("viewBox") or "").strip()
    if viewbox:
        return problems
    has_size = bool((root.get("width") or "").strip() or (root.get("height") or "").strip())
    if has_size:
        problems.append(
            Problem(path, "no viewBox (only width/height)", warning=True)
        )
    else:
        problems.append(Problem(path, "missing viewBox"))
    return problems


def check_personal_paths(path: str, text: str, root: ET.Element) -> list[Problem]:
    """Flag home/absolute paths without repeating them (they may be personal data)."""

    def describe(name: str) -> str:
        prefix = EDITOR_NS.get(namespace(name) or "", "")
        attr = f"{prefix}:{local_name(name)}" if prefix else local_name(name)
        return f"attribute {attr}"

    for element in root.iter():
        for name, value in element.attrib.items():
            if has_filesystem_path(value):
                return [
                    Problem(
                        path,
                        f"{describe(name)} contains an absolute or home path (personal data)",
                    )
                ]
        if has_filesystem_path(element.text or "") or has_filesystem_path(element.tail or ""):
            return [
                Problem(path, "text content contains an absolute or home path (personal data)")
            ]
    for comment in COMMENT.finditer(text):
        if has_filesystem_path(comment.group(1)):
            return [Problem(path, "a comment contains an absolute or home path (personal data)")]
    return []


def check_editor_metadata(path: str, root: ET.Element) -> list[Problem]:
    """Warn about leftover inkscape:/sodipodi: attributes and elements."""
    problems: list[Problem] = []
    reported: set[str] = set()
    for element in root.iter():
        ns = namespace(element.tag)
        if ns in EDITOR_NS:
            prefix = EDITOR_NS[ns]
            key = f"element {prefix}:{local_name(element.tag)}"
            if key not in reported:
                reported.add(key)
                problems.append(
                    Problem(
                        path,
                        f"{key} is editor metadata",
                        warning=True,
                    )
                )
        for name in element.attrib:
            attr_ns = namespace(name)
            local = local_name(name)
            if attr_ns in EDITOR_NS:
                prefix = EDITOR_NS[attr_ns]
                key = f"attribute {prefix}:{local}"
            elif EDITOR_PREFIX.match(local):
                key = f"attribute {local}"
            else:
                continue
            if key not in reported:
                reported.add(key)
                problems.append(
                    Problem(path, f"{key} is editor metadata", warning=True)
                )
    return problems


def check_dangerous(path: str, root: ET.Element) -> list[Problem]:
    """<script>, on*, <foreignObject>, @import, and bad href / url() / data: values."""
    problems: list[Problem] = []
    for element in root.iter():
        local = local_name(element.tag)
        low = local.lower()
        if low == "script":
            problems.append(Problem(path, "contains a <script> element"))
        if low == "foreignobject":
            problems.append(Problem(path, "contains a <foreignObject> element"))
        if low == "style":
            css = "".join(element.itertext())
            if re.search(r"@import\b", css, re.IGNORECASE):
                problems.append(Problem(path, "<style> contains @import"))
            problems += check_url_values(path, css, "<style>")
        for name, value in element.attrib.items():
            attr = local_name(name)
            if attr.lower().startswith("on") and len(attr) > 2:
                problems.append(Problem(path, f"event handler attribute {attr}="))
            if attr.lower() == "href" or name == f"{{{XLINK_NS}}}href":
                label = "xlink:href" if namespace(name) == XLINK_NS else "href"
                if scheme_of(value) == "data":
                    problems.append(parsed_data_problem(path, value, label))
                else:
                    found = href_target_problem(path, value, label)
                    if found:
                        problems.append(found)
            problems += check_url_values(path, value, f"attribute {attr}")
    return problems


def check_url_values(path: str, text: str, where: str) -> list[Problem]:
    problems: list[Problem] = []
    for raw in iter_urls(text):
        if raw.startswith("#"):
            continue
        if scheme_of(raw) == "data":
            problems.append(parsed_data_problem(path, raw, f"url() in {where}"))
            continue
        found = href_target_problem(path, raw, f"url() in {where}")
        if found:
            problems.append(found)
    return problems


def iter_reference_texts(element: ET.Element) -> list[str]:
    """Attribute values and <style> bodies that may contain url(#id) references."""
    values = list(element.attrib.values())
    if local_name(element.tag).lower() == "style":
        values.append("".join(element.itertext()))
    elif element.text:
        values.append(element.text)
    return values


def check_ids_and_refs(path: str, root: ET.Element) -> list[Problem]:
    counts = collect_ids(root)
    problems: list[Problem] = []
    for ident, count in counts.items():
        if count > 1:
            problems.append(Problem(path, f"id '{ident}' is used {count} times"))
    seen_missing: set[str] = set()
    for element in root.iter():
        for value in iter_reference_texts(element):
            for match in URL_REF.finditer(value):
                ident = unquote(match.group(2).strip())
                key = f"url(#{ident})"
                if ident not in counts and key not in seen_missing:
                    seen_missing.add(key)
                    problems.append(
                        Problem(path, f"url(#{ident}) refers to no element with that id")
                    )
        for name, value in element.attrib.items():
            if local_name(name).lower() != "href" and name != f"{{{XLINK_NS}}}href":
                continue
            ident = fragment_id(value)
            if ident is None:
                continue
            key = f"href=#{ident}"
            if ident not in counts and key not in seen_missing:
                seen_missing.add(key)
                label = "xlink:href" if namespace(name) == XLINK_NS else "href"
                problems.append(
                    Problem(path, f"{label}='#{ident}' refers to no element with that id")
                )
    return problems


def paints_of(root: ET.Element) -> list[str]:
    """Explicit fill/stroke colours used in the document, already normalised."""
    colours: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        colour = normalize_color(raw)
        if colour and colour not in seen:
            seen.add(colour)
            colours.append(colour)

    for element in root.iter():
        for attr in ("fill", "stroke"):
            if attr in element.attrib:
                add(element.attrib[attr])
        if "style" in element.attrib:
            for _prop, raw in CSS_PAINT.findall(element.attrib["style"]):
                add(raw)
        if local_name(element.tag).lower() == "style":
            for _prop, raw in CSS_PAINT.findall("".join(element.itertext())):
                add(raw)
    return colours


def check_symbolic_colours(path: str, root: ET.Element) -> list[Problem]:
    if "/symbolic/" not in path.replace("\\", "/"):
        return []
    colours = paints_of(root)
    if len(colours) <= 1:
        return []
    listed = ", ".join(colours)
    return [
        Problem(
            path,
            f"symbolic icon uses more than one fill/stroke colour: {listed}",
            warning=True,
        )
    ]


def parse_xml(path: str, text: str) -> tuple[ET.Element | None, list[Problem]]:
    try:
        return ET.fromstring(text), []
    except ET.ParseError as err:
        return None, [Problem(path, f"not well-formed XML: {err}")]


def check_text(path: str, text: str, *, size: int | None = None) -> list[Problem]:
    """All problems of one SVG file. ``size`` is the file size in bytes, if known."""
    problems: list[Problem] = []
    if size is not None and size > SIZE_LIMIT:
        problems.append(
            Problem(
                path,
                f"file is {size} bytes (over {SIZE_LIMIT} bytes)",
                warning=True,
            )
        )
    blocked = check_doctype(path, text)
    problems += blocked
    if blocked:
        return problems
    root, parse_problems = parse_xml(path, text)
    if root is None:
        return problems + parse_problems
    problems += check_root(path, root)
    problems += check_dangerous(path, root)
    problems += check_ids_and_refs(path, root)
    problems += check_personal_paths(path, text, root)
    problems += check_editor_metadata(path, root)
    problems += check_symbolic_colours(path, root)
    return problems


def check_files(root: Path, paths: Sequence[str]) -> list[Problem]:
    """Check the given files (paths relative to ``root`` or absolute)."""
    problems: list[Problem] = []
    for rel in paths:
        try:
            data = (root / rel).read_bytes()
        except OSError as err:
            problems.append(Problem(rel, f"cannot read the file: {err.strerror or err}"))
            continue
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            problems.append(Problem(rel, "the file is not valid UTF-8"))
            if len(data) > SIZE_LIMIT:
                problems.append(
                    Problem(
                        rel,
                        f"file is {len(data)} bytes (over {SIZE_LIMIT} bytes)",
                        warning=True,
                    )
                )
            continue
        problems += check_text(rel, text, size=len(data))
    return problems


def tracked_svg_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.svg"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        errors="surrogateescape",
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if any(a.startswith("-") for a in args):
        print("usage: python3 tools/check_svg.py [FILE.svg ...]", file=sys.stderr)
        return 0 if args in (["-h"], ["--help"]) else 2
    if any("\0" in a for a in args):
        print("a file name cannot hold a NUL byte", file=sys.stderr)
        return 2
    if args:
        paths = args
    else:
        try:
            paths = tracked_svg_files(ROOT)
        except (OSError, subprocess.CalledProcessError) as err:
            print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
            return 2
    if not paths:
        print("no tracked *.svg files found", file=sys.stderr)
        return 2
    problems = check_files(Path.cwd() if args else ROOT, paths)
    for problem in problems:
        print(problem, file=sys.stderr)
    errors = sum(not p.warning for p in problems)
    print(
        f"{len(paths)} SVG files checked: {errors} errors, {len(problems) - errors} warnings"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    # A file name that is not UTF-8 is printed escaped, not as a traceback.
    sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    sys.exit(main())
