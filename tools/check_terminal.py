#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the terminal defaults of gnome-branding-Vilna.

The terminal is Ptyxis with a see-through background (30_Vilna.gschema.override).
This checks that the text stays readable: every colour that prints text keeps
4.5:1 against the background, also with the darkest and the lightest wallpaper
showing through at the chosen opacity. It also checks that the override, the
palette file and the pattern agree.

Run: python3 tools/check_terminal.py
"""

from __future__ import annotations

import configparser
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRANDING = ROOT / "obs" / "packages" / "vilna-branding"
OVERRIDE = BRANDING / "30_Vilna.gschema.override"
PATTERN = ROOT / "obs" / "packages" / "patterns-vilna" / "patterns-vilna.spec"
SPEC = BRANDING / "vilna-branding.spec"
PTYXIS = ROOT / "obs" / "packages" / "ptyxis"

TEXT_MIN = 4.5  # WCAG AA for normal text
CURSOR_MIN = 3.0  # WCAG for a non-text element
# Color0, Color7 and Color15 are the black, light grey and white that programs
# draw backgrounds and borders with; they are not text on the default background.
NOT_TEXT = {"Color0", "Color7", "Color15"}

# The letter on each key of the Ukrainian layout (xkb «ua»), as GDK names it:
# a Ptyxis shortcut on a Latin letter also needs the Cyrillic letter of the
# same key, or it does nothing while Українська is on (GTK 4 matches by the
# letter typed).
UA_LETTER = {
    "q": "Cyrillic_shorti", "w": "Cyrillic_tse", "e": "Cyrillic_u",
    "r": "Cyrillic_ka", "t": "Cyrillic_ie", "y": "Cyrillic_en",
    "u": "Cyrillic_ghe", "i": "Cyrillic_sha", "o": "Cyrillic_shcha",
    "p": "Cyrillic_ze", "a": "Cyrillic_ef", "s": "Ukrainian_i",
    "d": "Cyrillic_ve", "f": "Cyrillic_a", "g": "Cyrillic_pe",
    "h": "Cyrillic_er", "j": "Cyrillic_o", "k": "Cyrillic_el",
    "l": "Cyrillic_de", "z": "Cyrillic_ya", "x": "Cyrillic_che",
    "c": "Cyrillic_es", "v": "Cyrillic_em", "b": "Cyrillic_i",
    "n": "Cyrillic_te", "m": "Cyrillic_softsign",
}


def luminance(hex_colour: str) -> float:
    rgb = rgb_of(hex_colour)
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def rgb_of(hex_colour: str) -> list[float]:
    h = hex_colour.lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", h):
        raise ValueError(f"not a #RRGGBB colour: {hex_colour}")
    return [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]


def to_hex(rgb: list[float]) -> str:
    return "#" + "".join(f"{round(c * 255):02X}" for c in rgb)


def blend(top: str, behind: str, opacity: float) -> str:
    """The colour the compositor shows: top at opacity over behind."""
    t, b = rgb_of(top), rgb_of(behind)
    return to_hex([opacity * x + (1 - opacity) * y for x, y in zip(t, b)])


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def override_sections(text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[(.+)\]", line)
        if m:
            current = sections.setdefault(m.group(1), {})
        elif current is not None and "=" in line:
            key, value = line.split("=", 1)
            current[key.strip()] = value.strip()
    return sections


def check() -> list[str]:
    errors: list[str] = []
    sections = override_sections(OVERRIDE.read_text(encoding="utf-8"))
    app = sections.get("org.gnome.Ptyxis", {})
    profile = sections.get("org.gnome.Ptyxis.Profile", {})

    uuid = app.get("default-profile-uuid", "").strip("'")
    if not uuid or f"'{uuid}'" not in app.get("profile-uuids", ""):
        errors.append("org.gnome.Ptyxis: default-profile-uuid must be one of profile-uuids")

    try:
        opacity = float(profile.get("opacity", "1"))
    except ValueError:
        errors.append("org.gnome.Ptyxis.Profile: opacity is not a number")
        opacity = 1.0
    if not 0.8 <= opacity <= 1.0:
        errors.append(f"opacity {opacity} is outside 0.8–1.0: the text would fight the wallpaper")

    palette_id = profile.get("palette", "").strip("'")
    palette_file = BRANDING / f"{palette_id}.palette"
    if not palette_file.is_file():
        errors.append(f"palette '{palette_id}' has no file {palette_file.relative_to(ROOT)}")
        return errors
    if palette_file.name not in SPEC.read_text(encoding="utf-8"):
        errors.append(f"{SPEC.name} does not package {palette_file.name}")

    palette = configparser.ConfigParser(interpolation=None, comment_prefixes=("#",))
    palette.optionxform = str  # keep the key case
    palette.read(palette_file, encoding="utf-8")
    for style in ("Light", "Dark"):
        if style not in palette:
            errors.append(f"{palette_file.name}: no [{style}] section")
            continue
        colours = palette[style]
        background = colours["Background"]
        # The window shows the background at `opacity` over the wallpaper;
        # the darkest and the lightest wallpaper are the worst cases.
        grounds = [background] + [blend(background, w, opacity) for w in ("#000000", "#FFFFFF")]
        for key, value in colours.items():
            if key in NOT_TEXT or key.startswith("Titlebar") or key == "Background":
                continue
            minimum = CURSOR_MIN if key == "Cursor" else TEXT_MIN
            worst = min(contrast(value, g) for g in grounds)
            if worst < minimum:
                errors.append(
                    f"{palette_file.name} [{style}] {key}={value}: {worst:.2f}:1 "
                    f"against the background, needs {minimum}:1"
                )
        title = colours.get("TitlebarForeground")
        if title and contrast(title, colours.get("TitlebarBackground", background)) < TEXT_MIN:
            errors.append(f"{palette_file.name} [{style}]: the title bar text is below {TEXT_MIN}:1")

    pattern = PATTERN.read_text(encoding="utf-8")
    if not re.search(r"^Requires:\s+ptyxis$", pattern, re.M):
        errors.append("patterns-vilna does not require ptyxis")
    if re.search(r"^Requires:\s+gnome-console", pattern, re.M):
        errors.append("patterns-vilna still requires gnome-console")
    term = sections.get("org.gnome.desktop.default-applications.terminal", {})
    if term.get("exec", "").strip("'") != "ptyxis":
        errors.append("org.gnome.desktop.default-applications.terminal: exec is not 'ptyxis'")
    keys = sections.get("org.gnome.settings-daemon.plugins.media-keys", {})
    if "'<Super>Return'" not in keys.get("terminal", ""):
        errors.append("media-keys terminal does not include <Super>Return")
    if any(".custom-keybindings." in name for name in sections):
        # A custom keybinding sits at a path of a relocatable schema: an
        # override of that path is silently ignored (VM test, Build12.26).
        errors.append("a custom keybinding in the override is ignored; use media-keys terminal")
    for name, value in sections.get("org.gnome.Ptyxis.Shortcuts", {}).items():
        alternatives = [a.strip() for a in value.strip("'").split("|") if a.strip()]
        parsed = []
        for alt in alternatives:
            m = re.fullmatch(r"((?:<[a-z]+>)*)(\w+)", alt)
            if not m:
                errors.append(f"Ptyxis shortcut {name}: cannot read {alt!r}")
                continue
            parsed.append((m.group(1), m.group(2)))
        for mods, key in parsed:
            twin = UA_LETTER.get(key.lower())
            if twin and (mods, twin) not in parsed:
                errors.append(
                    f"Ptyxis shortcut {name}: {mods}{key} has no {mods}{twin} "
                    "for the Ukrainian layout"
                )
    # org.vilna.Ptyxis exists only in the Vilna build of Ptyxis; a key the
    # override sets there must be one that build's schema and patch know.
    for name in sections.get("org.vilna.Ptyxis", {}):
        schema = (PTYXIS / "org.vilna.Ptyxis.gschema.xml").read_text(encoding="utf-8")
        patch = (PTYXIS / "vilna-right-click-paste.patch").read_text(encoding="utf-8")
        if f'<key name="{name}"' not in schema or f'"{name}"' not in patch:
            errors.append(f"org.vilna.Ptyxis {name}: not in obs/packages/ptyxis")
    if "kgx" in OVERRIDE.read_text(encoding="utf-8"):
        errors.append("30_Vilna.gschema.override still starts kgx (GNOME Console)")
    return errors


def main() -> int:
    try:
        errors = check()
    except (OSError, ValueError, KeyError, configparser.Error) as err:
        # ValueError covers text that is not UTF-8 and a colour that is no colour.
        print(f"error: cannot read the terminal defaults: {err}", file=sys.stderr)
        return 1
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    if not errors:
        print("terminal defaults: OK")
    return 1 if errors else 0


if __name__ == "__main__":
    # A file name that is not UTF-8 is printed escaped, not as a traceback.
    sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    sys.exit(main())
