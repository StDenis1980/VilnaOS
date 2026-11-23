#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Checks the Vilna icon themes: python3 src/check_icons.py Vilna Vilna-Classic

- the committed files are exactly what make_icons.py draws (no hand edits);
- index.theme lists every folder that has icons, inherits Adwaita;
- every SVG parses, full-colour icons are 128x128 (Vilna-Classic: 256x256), symbolic ones 16x16
  with one colour, so GNOME can recolour them for the theme.
Exit code 1 and a list of problems if something is wrong.
"""
import configparser
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import make_classic  # noqa: E402
import make_icons  # noqa: E402

# theme folder -> (generator, size of full-colour icons, themes it must inherit)
THEMES = {
    "Vilna": (make_icons, "128", ("Adwaita",)),
    "Vilna-Classic": (make_classic, "256", ("Vilna", "Adwaita")),
}

SVG = "{http://www.w3.org/2000/svg}svg"


def problems(theme: pathlib.Path) -> list[str]:
    out = []
    if theme.name not in THEMES:
        return [f"{theme}: unknown theme, expected one of {sorted(THEMES)}"]
    gen, full, parents = THEMES[theme.name]
    expected = gen.build()
    actual = {p.relative_to(theme).as_posix() for p in theme.rglob("*") if p.is_file()}
    for rel in sorted(set(expected) - actual):
        out.append(f"{rel}: missing, run {gen.__name__}.py")
    for rel in sorted(actual - set(expected)):
        out.append(f"{rel}: not drawn by {gen.__name__}.py")
    for rel in sorted(set(expected) & actual):
        if (theme / rel).read_text(encoding="utf-8") != expected[rel]:
            out.append(f"{rel}: differs from {gen.__name__}.py, run it again")

    index = configparser.ConfigParser()
    index.read_string(expected["index.theme"])
    head = index["Icon Theme"]
    inherits = head.get("Inherits", "").split(",")
    for parent in parents:
        if parent not in inherits:
            out.append(f"index.theme: must inherit {parent}, the icons it lacks come from there")
    dirs = set(head.get("Directories", "").split(","))
    for d in dirs - set(index.sections()):
        out.append(f"index.theme: no section for {d}")
    used = {rel.rsplit("/", 1)[0] for rel in expected if rel.endswith(".svg")}
    for d in used - dirs:
        out.append(f"index.theme: {d} not in Directories, GTK would not see its icons")

    for rel, text in expected.items():
        if not rel.endswith(".svg"):
            continue
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            out.append(f"{rel}: not valid XML ({e})")
            continue
        if root.tag != SVG:
            out.append(f"{rel}: root is not <svg>")
        size = "16" if rel.startswith("symbolic/") else full
        if root.get("width") != size or root.get("height") != size:
            out.append(f"{rel}: must be {size}x{size}")
        if rel.startswith("symbolic/"):
            if not rel.endswith("-symbolic.svg"):
                out.append(f"{rel}: name must end in -symbolic.svg")
            colours = set(re.findall(r"#[0-9A-Fa-f]{6}\b", text))
            if len(colours) != 1:
                out.append(f"{rel}: symbolic icon must use one colour, has {sorted(colours)}")
    return out


def main() -> int:
    themes = sys.argv[1:] or list(THEMES)
    failed = False
    for name in themes:
        theme = pathlib.Path(name)
        found = problems(theme)
        for p in found:
            print(f"{theme.name}/{p}")
        print(f"{theme.name}: {len(THEMES.get(theme.name, (make_icons,))[0].build())} files checked, {len(found)} problems")
        failed = failed or bool(found)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
