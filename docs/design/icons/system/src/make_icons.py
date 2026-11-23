#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Чернетка системних значків Vilna: теки (places) і symbolic-значки Vilna.

Усе намальовано в коді з нуля, без чужих файлів. Запуск з docs/design/icons/system:
    python3 src/make_icons.py Vilna
Пише тему Vilna: index.theme, scalable/{places,devices}, symbolic/{status,actions}.
Згенеровані файли закомічено; check_icons.py перевіряє, що вони збігаються з генератором.
"""
import pathlib
import sys

NIGHT = "#1F345C"
BLUE = "#2D6BFF"
BLUE_L = "#5B8CFF"
BLUE_XL = "#9DBBFF"
GOLD = "#F5C24A"
ORANGE = "#F0963A"
CREAM = "#F2E8D5"
SYM = "#13213A"  # GNOME перефарбовує symbolic, колір лише для перегляду

# Емблеми на передній стінці теки: 128-px координати, центр ~ (64, 78).
EMBLEMS = {
    "folder": "",
    "user-home": '<path d="M64 60 L84 77 V96 H72 V84 H56 V96 H44 V77Z" />',
    "folder-documents": '<rect x="48" y="60" width="32" height="38" rx="4"/>'
        f'<rect x="54" y="68" width="20" height="4" rx="2" fill="{BLUE}"/>'
        f'<rect x="54" y="77" width="20" height="4" rx="2" fill="{BLUE}"/>'
        f'<rect x="54" y="86" width="13" height="4" rx="2" fill="{BLUE}"/>',
    "folder-download": '<path d="M58 58 H70 V74 H80 L64 92 L48 74 H58Z"/>'
        '<rect x="46" y="94" width="36" height="5" rx="2.5"/>',
    "folder-music": '<path d="M58 62 L80 57 V88 A7 7 0 1 1 75 81 V67 L63 70 V92 A7 7 0 1 1 58 85Z"/>',
    "folder-pictures": '<rect x="44" y="61" width="40" height="34" rx="5"/>'
        f'<path d="M48 91 L60 76 L68 85 L73 79 L80 91Z" fill="{BLUE}"/>'
        f'<circle cx="74" cy="70" r="4" fill="{ORANGE}"/>',
    "folder-videos": '<rect x="44" y="62" width="40" height="32" rx="6"/>'
        f'<path d="M59 70 L72 78 L59 86Z" fill="{BLUE}"/>',
    "user-desktop": '<rect x="44" y="60" width="40" height="28" rx="4"/>'
        '<rect x="58" y="88" width="12" height="6"/><rect x="52" y="94" width="24" height="4" rx="2"/>',
    "folder-templates": '<rect x="48" y="60" width="32" height="38" rx="4" fill="none" stroke-width="5" stroke-dasharray="7 5"/>',
    "folder-publicshare": '<circle cx="54" cy="70" r="6"/><circle cx="74" cy="70" r="6"/>'
        '<path d="M42 96 A12 12 0 0 1 66 96Z"/><path d="M62 96 A12 12 0 0 1 86 96Z"/>',
}


FOLDER_DEFS = f'''<defs>
 <linearGradient id="f" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{BLUE_L}"/><stop offset="1" stop-color="{BLUE}"/></linearGradient>
 <linearGradient id="g" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{GOLD}"/><stop offset="1" stop-color="{ORANGE}"/></linearGradient>
</defs>'''


def svg128(name: str, body: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">\n'
            f"<title>{name}</title>\n{FOLDER_DEFS}\n{body}</svg>\n")


def trash(full: bool) -> str:
    papers = (f'<path d="M40 34 L58 18 L72 30 L54 44Z" fill="{CREAM}"/>'
              f'<path d="M66 36 L82 16 L94 26 L80 44Z" fill="#E4D6BC"/>'
              f'<path d="M50 40 Q64 20 80 40Z" fill="{CREAM}"/>') if full else ""
    return (papers +
            f'<path d="M30 46 H98 L92 110 Q91 116 85 116 H43 Q37 116 36 110Z" fill="#1F4FC2"/>'
            '<path d="M30 44 H98 L92 106 Q91 112 85 112 H43 Q37 112 36 106Z" fill="url(#f)"/>'
            f'<g fill="{CREAM}" opacity=".85"><rect x="47" y="58" width="6" height="42" rx="3"/>'
            '<rect x="61" y="58" width="6" height="42" rx="3"/><rect x="75" y="58" width="6" height="42" rx="3"/></g>'
            f'<rect x="24" y="38" width="80" height="12" rx="6" fill="{NIGHT}"/>'
            '<rect x="24" y="36" width="80" height="11" rx="5.5" fill="url(#g)"/>')


DEVICES = {
    "drive-harddisk": (f'<rect x="14" y="44" width="100" height="52" rx="12" fill="{NIGHT}"/>'
        '<rect x="14" y="38" width="100" height="52" rx="12" fill="url(#f)"/>'
        f'<rect x="24" y="74" width="80" height="6" rx="3" fill="{CREAM}" opacity=".5"/>'
        f'<circle cx="96" cy="54" r="5" fill="{GOLD}"/>'),
    "drive-removable-media": (f'<rect x="50" y="12" width="28" height="22" rx="3" fill="{CREAM}"/>'
        f'<rect x="56" y="18" width="5" height="6" fill="{NIGHT}"/><rect x="67" y="18" width="5" height="6" fill="{NIGHT}"/>'
        f'<rect x="38" y="34" width="52" height="84" rx="12" fill="{NIGHT}"/>'
        '<rect x="38" y="30" width="52" height="84" rx="12" fill="url(#f)"/>'
        '<rect x="38" y="30" width="52" height="10" rx="5" fill="url(#g)"/>'
        f'<circle cx="64" cy="96" r="5" fill="{GOLD}"/>'),
}


INDEX_THEME = """[Icon Theme]
Name=Vilna
Comment=Vilna folders, trash, drives and Vilna symbolic icons; everything else from Adwaita
Inherits=Adwaita,hicolor
Example=folder
Directories=scalable/places,scalable/devices,symbolic/status,symbolic/actions

[scalable/places]
Context=Places
Size=128
MinSize=8
MaxSize=512
Type=Scalable

[scalable/devices]
Context=Devices
Size=128
MinSize=8
MaxSize=512
Type=Scalable

[symbolic/status]
Context=Status
Size=16
MinSize=8
MaxSize=512
Type=Scalable

[symbolic/actions]
Context=Actions
Size=16
MinSize=8
MaxSize=512
Type=Scalable
"""


def folder(name: str) -> str:
    emblem = EMBLEMS[name]
    stroke = f' stroke="{CREAM}"' if "stroke" in emblem else ""
    body = (f'<path d="M12 30 Q12 22 20 22 H48 Q54 22 57 27 L60 32 H108 Q116 32 116 40 V100 H12Z" fill="{NIGHT}"/>'
            f'<rect x="18" y="36" width="92" height="20" rx="4" fill="{CREAM}"/>'
            '<path d="M10 52 Q10 46 16 46 H112 Q118 46 118 52 V102 Q118 110 110 110 H18 Q10 110 10 102Z" fill="#1F4FC2"/>'
            '<path d="M10 50 Q10 44 16 44 H112 Q118 44 118 50 V98 Q118 106 110 106 H18 Q10 106 10 98Z" fill="url(#f)"/>'
            '<path d="M10 50 Q10 44 16 44 H112 Q118 44 118 50 V52 Q90 47 64 50 Q38 53 10 49Z" fill="url(#g)"/>'
            f'<g fill="{CREAM}" opacity=".92"{stroke}>{emblem}</g>')
    return svg128(name, body)


# symbolic 16×16: один колір, сітка Adwaita (2-px лінії, 1-px відступ від краю).
SYMBOLIC = {
    "status": {
        "vilna-masked": '<path d="M2 4 Q8 1 14 4 V8 Q14 13 8 15 Q2 13 2 8Z M5 7 a1.5 1.2 0 1 0 0.01 0Z M11 7 a1.5 1.2 0 1 0 0.01 0Z" fill-rule="evenodd"/>',
        "vilna-local": '<path d="M2 3 H14 V11 H2Z M3.5 4.5 V9.5 H12.5 V4.5Z" fill-rule="evenodd"/><rect x="5" y="12" width="6" height="2" rx="1"/>',
        "vilna-cloud": '<path d="M4.5 13 A3.5 3.5 0 0 1 4 6.05 A4.5 4.5 0 0 1 12.6 6.6 A3.2 3.2 0 0 1 12 13Z"/>',
        "vilna-risk-0": '<path d="M8 1 L14 3.5 V8 Q14 13 8 15 Q2 13 2 8 V3.5Z M8 3 L4 4.8 V8 Q4 11.6 8 13 Q12 11.6 12 8 V4.8Z" fill-rule="evenodd"/>',
        "vilna-risk-1": '<path d="M8 1 L14 3.5 V8 Q14 13 8 15 Q2 13 2 8 V3.5Z M8 3 L4 4.8 V8 Q4 11.6 8 13 Q12 11.6 12 8 V4.8Z" fill-rule="evenodd"/><rect x="7" y="7" width="2" height="2" rx="1"/>',
        "vilna-risk-2": '<path d="M8 1 L14 3.5 V8 Q14 13 8 15 Q2 13 2 8 V3.5Z M8 3 L4 4.8 V8 Q4 11.6 8 13 Q12 11.6 12 8 V4.8Z" fill-rule="evenodd"/><rect x="5.5" y="7" width="2" height="2" rx="1"/><rect x="8.5" y="7" width="2" height="2" rx="1"/>',
        "vilna-risk-3": '<path d="M8 1 L14 3.5 V8 Q14 13 8 15 Q2 13 2 8 V3.5Z M8 3 L4 4.8 V8 Q4 11.6 8 13 Q12 11.6 12 8 V4.8Z" fill-rule="evenodd"/><rect x="7" y="5" width="2" height="2" rx="1"/><rect x="5.5" y="8" width="2" height="2" rx="1"/><rect x="8.5" y="8" width="2" height="2" rx="1"/>',
        "vilna-risk-4": '<path d="M8 1 L14 3.5 V8 Q14 13 8 15 Q2 13 2 8 V3.5Z"/>',
    },
    "actions": {
        "vilna-consent": '<path d="M2 8.5 L6 12.5 L14 4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
        # Ключ по діагоналі: кільце з отвором угорі ліворуч, стрижень і два зубці.
        "vilna-key": '<g transform="rotate(45 5.2 5.2)"><path d="M5.2 1.4 A3.8 3.8 0 0 1 8.85 4.2 H16 V6.2 H15.9 V8 H14.5 V6.2 H13.8 V8 H12.4 V6.2 H8.85 A3.8 3.8 0 1 1 5.2 1.4Z M5.2 3.6 A1.6 1.6 0 1 0 5.21 3.6Z" fill-rule="evenodd"/></g>',
        "vilna-model": '<path d="M8 1 L14 4.5 V11.5 L8 15 L2 11.5 V4.5Z M8 3.3 L4 5.6 L8 7.9 L12 5.6Z M3.8 7.2 V10.5 L7 12.4 V9.1Z M9 9.1 V12.4 L12.2 10.5 V7.2Z" fill-rule="evenodd"/>',
        "vilna-mic": '<rect x="5.5" y="1" width="5" height="9" rx="2.5"/><path d="M3 7.5 A5 5 0 0 0 13 7.5" fill="none" stroke="currentColor" stroke-width="1.6"/><rect x="7.2" y="12" width="1.6" height="3"/>',
    },
}


def symbolic(body: str, name: str) -> str:
    body = body.replace("currentColor", SYM)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
            f'<title>{name}</title><g fill="{SYM}">{body}</g></svg>\n')


def build() -> dict[str, str]:
    """Every file of the theme: relative path -> text."""
    files = {"index.theme": INDEX_THEME}
    for name in EMBLEMS:
        files[f"scalable/places/{name}.svg"] = folder(name)
    files["scalable/places/user-trash.svg"] = svg128("user-trash", trash(False))
    files["scalable/places/user-trash-full.svg"] = svg128("user-trash-full", trash(True))
    for name, body in DEVICES.items():
        files[f"scalable/devices/{name}.svg"] = svg128(name, body)
    for ctx, icons in SYMBOLIC.items():
        for name, body in icons.items():
            files[f"symbolic/{ctx}/{name}-symbolic.svg"] = symbolic(body, name)
    return files


def main() -> int:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "Vilna")
    for rel, text in build().items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
