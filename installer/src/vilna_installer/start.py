# SPDX-License-Identifier: GPL-3.0-or-later
"""The first installer screen without GTK: languages, its texts, the start of the plan.

Step 0 of image/docs/installer.md works without the model: the person picks
the language of the conversation, can turn on the screen reader, and chooses
to go on with Gloxi or without the assistant. The result is the start of the
installation plan (installer.md, section 4)::

    {"version": 1, "ui": {"language": "uk", "screen_reader": false},
     "assistant": true, "confirmed_by_user": false}

The texts of this screen are in every language at once (``locale/*.po``),
because the screen changes language as soon as one is picked.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

RELEASE = "Blavet"


@dataclass(frozen=True)
class Language:
    code: str  # the interface language
    name: str  # its own name, as people who speak it write it
    locale: str  # the system locale it starts from


# The ten languages of ADR 0009, in its order; the rest come through Weblate.
LANGUAGES: tuple[Language, ...] = (
    Language("uk", "Українська", "uk_UA.UTF-8"),
    Language("en", "English", "en_US.UTF-8"),
    Language("es", "Español", "es_ES.UTF-8"),
    Language("pl", "Polski", "pl_PL.UTF-8"),
    Language("de", "Deutsch", "de_DE.UTF-8"),
    Language("fr", "Français", "fr_FR.UTF-8"),
    Language("it", "Italiano", "it_IT.UTF-8"),
    Language("ro", "Română", "ro_RO.UTF-8"),
    Language("cs", "Čeština", "cs_CZ.UTF-8"),
    Language("lt", "Lietuvių", "lt_LT.UTF-8"),
)
CODES = tuple(lang.code for lang in LANGUAGES)
MAIN_LOCALES = frozenset(lang.locale for lang in LANGUAGES)

# Any other language the system has a locale for («Other languages»). GNOME
# and the rest of openSUSE are in it; Vilna's own programs and Gloxi stay in
# English until someone translates them, and Gloxi was not checked in it.
LOCALE_RE = re.compile(r"^([a-z]{2,3})(_[A-Z]{2})?\.UTF-8(@[a-z]+)?$")
SUPPORTED = Path("/usr/share/i18n/SUPPORTED")

# Every text of the screen, in English (the msgids of locale/*.po).
WELCOME = "Welcome to Vilna"
RELEASE_CHIP = "Blavet release"
LEAD = (
    "Choose a language. Then I, Gloxi, will help you install the system, "
    "or you can go through the steps yourself."
)
SCREEN_READER = "Screen reader"
WITHOUT = "Without the assistant"
WITH_GLOXI = "Continue with Gloxi"
TITLE = "Install Vilna"
OTHER = "Other languages"
SEARCH = "Search for a language"
TEXTS = (WELCOME, RELEASE_CHIP, LEAD, SCREEN_READER, WITHOUT, WITH_GLOXI, TITLE, OTHER, SEARCH)
# Shown only in a language Vilna has no translation for, so it stays in English.
NOT_TRANSLATED = (
    "The system will be in this language. Vilna's own programs and Gloxi "
    "are not translated into it yet and will speak English; Gloxi may answer "
    "worse in this language."
)


def parse_po(text: str) -> dict[str, str]:
    """msgid → msgstr for every translated, non-fuzzy entry (as in Herkos)."""
    catalog: dict[str, str] = {}
    msgid: list[str] | None = None
    msgstr: list[str] | None = None
    current: list[str] | None = None
    fuzzy = False
    for raw in text.splitlines() + [""]:
        line = raw.strip()
        if not line:
            if msgid is not None and msgstr is not None and not fuzzy:
                key, value = "".join(msgid), "".join(msgstr)
                if key and value:
                    catalog[key] = value
            msgid = msgstr = current = None
            fuzzy = False
        elif line.startswith("#,") and "fuzzy" in line:
            fuzzy = True
        elif line.startswith("#"):
            continue
        elif line.startswith("msgid "):
            msgid = [ast.literal_eval(line[6:])]
            current = msgid
        elif line.startswith("msgstr "):
            msgstr = [ast.literal_eval(line[7:])]
            current = msgstr
        elif line.startswith('"') and current is not None:
            current.append(ast.literal_eval(line))
    return catalog


def catalog(code: str) -> dict[str, str]:
    po = resources.files("vilna_installer.locale") / f"{code}.po"
    return parse_po(po.read_text(encoding="utf-8")) if po.is_file() else {}


class Texts:
    """This screen's texts in one language; English when a text has no translation."""

    def __init__(self, code: str) -> None:
        self.code = code
        self._catalog = catalog(code)

    def __call__(self, message: str) -> str:
        return self._catalog.get(message, message)


def default_language(environ: dict[str, str] | None = None) -> str:
    """The language the image was started in (boot menu sets LANG), else English."""
    env = os.environ if environ is None else environ
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        for part in (env.get(var) or "").split(":"):
            code = part.split(".")[0].split("@")[0].split("_")[0]
            if code in CODES:
                return code
    return "en"


def normalize_locale(name: str) -> str | None:
    """``pt_BR.UTF-8``, ``pt_BR.utf8``, ``sr_RS@latin`` (UTF-8) → ``xx_YY.UTF-8[@mod]``."""
    base, _, modifier = name.strip().partition("@")
    # "@euro" only chose the euro sign in old 8-bit charsets; in UTF-8 it is
    # the same locale (pt_PT.UTF-8@euro showed as a second, raw entry).
    if modifier == "euro":
        modifier = ""
    lang, _, charset = base.partition(".")
    if charset and charset.lower().replace("-", "") != "utf8":
        return None
    result = f"{lang}.UTF-8" + (f"@{modifier}" if modifier else "")
    return result if LOCALE_RE.match(result) else None


def parse_supported(text: str) -> list[str]:
    """UTF-8 locales from glibc's SUPPORTED list (``name charset`` per line)."""
    found = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "UTF-8" and (loc := normalize_locale(parts[0])):
            found.append(loc)
    return found


def system_locales(supported: Path = SUPPORTED) -> list[str]:
    """Every UTF-8 locale the system can use: SUPPORTED, else ``locale -a``."""
    try:
        found = parse_supported(supported.read_text(encoding="utf-8"))
    except OSError:
        found = []
    if not found:
        try:
            out = subprocess.run(
                ["locale", "-a"], capture_output=True, text=True, timeout=10, check=False
            ).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        found = [loc for line in out.splitlines() if (loc := normalize_locale(line))]
    return sorted(set(found))


def other_languages(
    locales: Iterable[str], name_of: Callable[[str], str | None]
) -> list[tuple[str, str]]:
    """(locale, its own name) for every locale but the ten main ones, by name."""
    rows = []
    for loc in set(locales):
        if loc in MAIN_LOCALES or not LOCALE_RE.match(loc):
            continue
        rows.append((loc, name_of(loc) or loc))
    return sorted(rows, key=lambda row: (row[1].casefold(), row[0]))


def language_of(locale: str) -> str:
    match = LOCALE_RE.match(locale)
    if match is None:
        raise ValueError(f"not a UTF-8 locale: {locale!r}")
    return match.group(1)


def start_plan(
    language: str, screen_reader: bool, assistant: bool, locale: str | None = None
) -> dict[str, Any]:
    """The start of the plan: one of the ten languages, or any locale of the system."""
    if language in CODES and locale is None:
        locale = next(lang.locale for lang in LANGUAGES if lang.code == language)
    elif locale is None:
        raise ValueError(f"unknown language {language!r}; one of {', '.join(CODES)} or a locale")
    elif language_of(locale) != language:
        raise ValueError(f"locale {locale!r} is not in language {language!r}")
    return {
        "version": 1,
        "ui": {
            "language": language,
            "locale": locale,
            "translated": language in CODES,
            "screen_reader": bool(screen_reader),
        },
        "assistant": bool(assistant),
        "confirmed_by_user": False,  # only the Herkos screen sets it (installer.md 1.4)
    }


def default_plan_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / "vilna-installer" / "plan.json"


def write_plan(plan: dict[str, Any], path: Path) -> None:
    """Atomically, readable by the person only: the plan will hold their choices."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".plan-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
