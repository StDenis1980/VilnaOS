# SPDX-License-Identifier: Apache-2.0
"""What the app catalogues say: AppStream files of Flatpak remotes, and the
«Рекомендовано Vilna» picks (obs/packages/vilna-software-picks).

Everything read here is written by other people (app developers, Flathub),
so it is read defensively: a size limit, no external entities (expat does
not fetch them), short fields only, and nothing is ever run.
"""

from __future__ import annotations

import gzip
import io
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

# A Flathub appstream.xml is ~50 MB unpacked; anything far larger is not one.
MAX_APPSTREAM_BYTES = 256 * 1024 * 1024
MAX_FIELD_CHARS = 300
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

# Where GNOME Software reads AppStream catalogues from (vilna-software-picks
# README): the package installs into /usr/share, a person trying it on Aeon
# puts a copy into /var/lib. The later file wins.
PICKS_FILES = (
    Path("/usr/share/swcatalog/xml/vilna-software-picks.xml"),
    Path("/var/lib/swcatalog/xml/vilna-software-picks.xml"),
)
PICK_KEY = "Vilna::Pick"
VERIFIED_KEY = "flathub::verification::verified"

# Flatpak app ids: at least three parts, letters, digits, _ and - (flatpak's
# own rule), at most 255 characters.
_APP_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_-]*){2,}")


def is_app_id(s: object) -> bool:
    return isinstance(s, str) and len(s) <= 255 and bool(_APP_ID.fullmatch(s))


@dataclass
class Component:
    """One app as a catalogue describes it."""

    id: str
    names: dict[str, str] = field(default_factory=dict)  # "" = untranslated
    summaries: dict[str, str] = field(default_factory=dict)
    developer: str = ""
    license: str = ""
    categories: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    custom: dict[str, str] = field(default_factory=dict)

    def name(self, language: str = "") -> str:
        return _localized(self.names, language) or self.id

    def summary(self, language: str = "") -> str:
        return _localized(self.summaries, language)

    @property
    def verified(self) -> bool:
        return self.custom.get(VERIFIED_KEY, "").lower() == "true"

    def words(self) -> str:
        """Everything a search may match, lower case, every language."""
        parts = [self.id, *self.names.values(), *self.summaries.values()]
        parts += self.keywords + self.categories
        return " ".join(parts).casefold()


def _localized(values: dict[str, str], language: str) -> str:
    if language:
        for key in (language, language.split("_")[0].split("-")[0]):
            if values.get(key):
                return values[key]
    return values.get("", "") or values.get("en", "")


def _text(el: ET.Element) -> str:
    text = " ".join("".join(el.itertext()).split())
    return text[:MAX_FIELD_CHARS]


def _app_id(raw: str) -> str:
    # Old catalogues name an app by its desktop file.
    return raw[: -len(".desktop")] if raw.endswith(".desktop") else raw


def _component(el: ET.Element) -> Component | None:
    if el.get("type") not in (None, "desktop", "desktop-application", "console-application"):
        return None
    id_el = el.find("id")
    if id_el is None or not id_el.text:
        return None
    app_id = _app_id(id_el.text.strip())
    if not is_app_id(app_id):
        return None
    c = Component(id=app_id)
    for child in el:
        lang = child.get(XML_LANG, "")
        if child.tag == "name":
            c.names[lang] = _text(child)
        elif child.tag == "summary":
            c.summaries[lang] = _text(child)
        elif child.tag == "developer_name" and not lang and not c.developer:
            c.developer = _text(child)
        elif child.tag == "developer":
            name = child.find("name")
            if name is not None and not name.get(XML_LANG):
                c.developer = _text(name)
        elif child.tag == "project_license":
            c.license = _text(child)
        elif child.tag == "categories":
            c.categories = [_text(x) for x in child.findall("category")][:20]
        elif child.tag == "keywords":
            c.keywords = [_text(x) for x in child.findall("keyword")][:50]
        elif child.tag in ("custom", "metadata"):
            for v in child.findall("value"):
                key = v.get("key")
                if key:
                    c.custom[key[:100]] = _text(v)
    return c


def read_components(path: Path) -> Iterator[Component]:
    """The apps in one AppStream file (``.xml`` or ``.xml.gz``)."""
    raw = path.read_bytes() if path.suffix != ".gz" else _gunzip(path)
    yield from parse_components(raw)


def _gunzip(path: Path) -> bytes:
    with gzip.open(path, "rb") as f:
        data = f.read(MAX_APPSTREAM_BYTES + 1)
    if len(data) > MAX_APPSTREAM_BYTES:
        raise ValueError(f"{path}: larger than an app catalogue can be")
    return data


def parse_components(raw: bytes) -> Iterator[Component]:
    if len(raw) > MAX_APPSTREAM_BYTES:
        raise ValueError("larger than an app catalogue can be")
    for _event, el in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if el.tag == "component":
            c = _component(el)
            el.clear()
            if c is not None:
                yield c


def read_picks(files: Iterable[Path] = PICKS_FILES) -> dict[str, str]:
    """``{app id: section}`` of «Рекомендовано Vilna»; missing files are skipped."""
    picks: dict[str, str] = {}
    for path in files:
        try:
            components = list(read_components(path))
        except (OSError, ValueError, ET.ParseError):
            continue
        for c in components:
            if c.custom.get(PICK_KEY):
                picks[c.id] = c.custom[PICK_KEY]
    return picks
