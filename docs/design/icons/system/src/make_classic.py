#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Тема значків Vilna Classic: ті самі предмети, що й у Vilna, але «як справжні»
(об'єм, відблиски, м'які тіні). Намальовано з нуля в коді, нічого не взято з
чужих тем. Symbolic-значки тема бере з Vilna (Inherits=Vilna,Adwaita,hicolor).

Запуск з docs/design/icons/system:
    python3 src/make_classic.py Vilna-Classic
"""
import pathlib
import sys

DEFS = '''<defs>
<filter id="sh" x="-20%" y="-20%" width="140%" height="150%"><feGaussianBlur in="SourceAlpha" stdDeviation="5"/><feOffset dy="7"/><feComponentTransfer><feFuncA type="linear" slope=".35"/></feComponentTransfer><feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge></filter>
<linearGradient id="blue" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#8DB2FF"/><stop offset=".45" stop-color="#3F7BFF"/><stop offset="1" stop-color="#1E4FC8"/></linearGradient>
<linearGradient id="blueback" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2A4A85"/><stop offset="1" stop-color="#162A4F"/></linearGradient>
<linearGradient id="gloss" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".75"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
<linearGradient id="gold" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FFE29A"/><stop offset=".5" stop-color="#F5C24A"/><stop offset="1" stop-color="#C98A1E"/></linearGradient>
<linearGradient id="paper" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FFFDF8"/><stop offset="1" stop-color="#E9DEC8"/></linearGradient>
<linearGradient id="metal" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#8A94A6"/><stop offset=".25" stop-color="#E6EAF0"/><stop offset=".5" stop-color="#B7BFCC"/><stop offset=".75" stop-color="#F3F5F8"/><stop offset="1" stop-color="#7D879A"/></linearGradient>
<linearGradient id="metalv" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#F4F6F9"/><stop offset="1" stop-color="#9AA3B3"/></linearGradient>
<radialGradient id="platter" cx=".38" cy=".35" r=".75"><stop offset="0" stop-color="#FFFFFF"/><stop offset=".35" stop-color="#D5DCE8"/><stop offset=".7" stop-color="#9FAAC0"/><stop offset="1" stop-color="#C9D1DE"/></radialGradient>
<linearGradient id="emboss" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1A3E9E" stop-opacity=".55"/><stop offset="1" stop-color="#fff" stop-opacity=".35"/></linearGradient>
</defs>'''

def svg(name, body):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256"><title>{name}</title>{DEFS}{body}</svg>\n'

def folder(emblem=""):
    return ('<g filter="url(#sh)">'
      '<path d="M24 62 Q24 46 40 46 H98 Q108 46 114 56 L120 66 H218 Q232 66 232 80 V196 H24Z" fill="url(#blueback)"/>'
      # аркуші паперу всередині
      '<rect x="40" y="70" width="176" height="60" rx="6" fill="url(#paper)" transform="rotate(-2 128 100)"/>'
      '<rect x="44" y="78" width="170" height="56" rx="6" fill="#FFFFFF" transform="rotate(1.5 128 100)"/>'
      # передня стінка
      '<path d="M18 100 Q18 88 32 88 H224 Q238 88 238 100 L232 206 Q231 220 217 220 H39 Q25 220 24 206Z" fill="url(#blue)"/>'
      '<path d="M18 100 Q18 88 32 88 H224 Q238 88 238 100 L237 116 Q128 104 19 116Z" fill="url(#gloss)"/>'
      '<path d="M20 98 Q20 90 32 90 H224 Q236 90 236 98" fill="none" stroke="url(#gold)" stroke-width="5"/>'
      f'{emblem}</g>')

def emb(d):  # витиснена емблема
    return (f'<g transform="translate(0 2)" fill="#ffffff" opacity=".45">{d}</g>'
            f'<g fill="#1A3E9E" opacity=".55">{d}</g>')

HOME = '<path d="M128 122 L166 154 V190 H144 V166 H112 V190 H90 V154Z"/>'
DOCS = '<path d="M102 124 H142 L156 138 V194 H102Z M146 124 V136 H156Z" fill-rule="evenodd"/>'
DOWN = '<path d="M116 122 H140 V152 H158 L128 186 L98 152 H116Z"/><rect x="96" y="190" width="64" height="7" rx="3.5"/>'

MUSIC = '<path d="M112 128 L156 118 V176 A12 11 0 1 1 146 165 V138 L122 143 V184 A12 11 0 1 1 112 173Z"/>'
PICS = ('<path d="M92 124 H164 Q170 124 170 130 V186 Q170 192 164 192 H92 Q86 192 86 186 V130 Q86 124 92 124Z'
        ' M94 184 L118 152 L134 170 L144 158 L162 184Z M150 136 A8 8 0 1 0 150.01 136Z" fill-rule="evenodd"/>')
VIDEO = ('<path d="M92 128 H164 Q172 128 172 136 V180 Q172 188 164 188 H92 Q84 188 84 180 V136 Q84 128 92 128Z'
         ' M118 140 L146 158 L118 176Z" fill-rule="evenodd"/>')
DESK = '<path d="M86 124 H170 V178 H86Z M94 132 V170 H162 V132Z" fill-rule="evenodd"/><rect x="120" y="178" width="16" height="10"/><rect x="104" y="188" width="48" height="7" rx="3.5"/>'
TEMPL = ''.join(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="2"/>' for x, y, w, h in
                [(104, 124, 14, 6), (126, 124, 14, 6), (146, 124, 10, 6), (150, 124, 6, 14), (150, 146, 6, 14),
                 (150, 168, 6, 14), (146, 188, 10, 6), (126, 188, 14, 6), (104, 188, 14, 6), (100, 188, 6, 6),
                 (100, 168, 6, 14), (100, 146, 6, 14), (100, 124, 6, 14)])
SHARE = ('<circle cx="108" cy="140" r="12"/><circle cx="148" cy="140" r="12"/>'
         '<path d="M84 192 A24 24 0 0 1 132 192Z"/><path d="M124 192 A24 24 0 0 1 172 192Z"/>')


def trash(full):
    papers = ('<path d="M86 64 L118 34 L140 58 L108 86Z" fill="url(#paper)"/>'
              '<path d="M130 70 L156 30 L178 46 L154 84Z" fill="#F6EEDD"/>'
              '<circle cx="120" cy="72" r="20" fill="#EFE5D0"/><path d="M106 66 Q120 58 134 68" stroke="#CDBF9F" stroke-width="2" fill="none"/>') if full else ''
    ribs = ''.join(f'<path d="M{x} 92 L{x+(x-128)*0.12:.1f} 212" stroke="#7D879A" stroke-width="3" opacity=".6"/>' for x in range(78,184,14))
    return ('<g filter="url(#sh)">' + papers +
      '<path d="M60 88 H196 L182 214 Q180 228 166 228 H90 Q76 228 74 214Z" fill="url(#metal)"/>'
      + ribs +
      '<path d="M60 88 H196 L194 104 H62Z" fill="#000" opacity=".12"/>'
      '<rect x="52" y="76" width="152" height="18" rx="9" fill="url(#metalv)" stroke="#8A94A6" stroke-width="1.5"/>'
      '<rect x="60" y="78" width="136" height="5" rx="2.5" fill="#fff" opacity=".8"/>'
      '<rect x="70" y="214" width="116" height="8" rx="4" fill="url(#gold)" opacity=".9"/></g>')

HDD = ('<g filter="url(#sh)">'
  '<rect x="44" y="28" width="168" height="204" rx="16" fill="#5C6678"/>'
  '<rect x="44" y="24" width="168" height="202" rx="16" fill="url(#metal)"/>'
  '<rect x="52" y="32" width="152" height="186" rx="12" fill="url(#metalv)" opacity=".55"/>'
  + ''.join(f'<circle cx="{x}" cy="{y}" r="6" fill="#C7CED9" stroke="#6C7587" stroke-width="1.5"/><path d="M{x-3} {y} H{x+3}" stroke="#6C7587" stroke-width="1.5"/>' for x,y in [(60,40),(196,40),(60,210),(196,210)]) +
  '<circle cx="124" cy="104" r="64" fill="#5C6678"/>'
  '<circle cx="124" cy="102" r="62" fill="url(#platter)"/>'
  '<circle cx="124" cy="102" r="46" fill="none" stroke="#fff" stroke-opacity=".5" stroke-width="1.5"/>'
  '<circle cx="124" cy="102" r="32" fill="none" stroke="#7D879A" stroke-opacity=".5" stroke-width="1.5"/>'
  '<path d="M84 64 A56 56 0 0 1 150 54" stroke="#fff" stroke-width="6" stroke-linecap="round" opacity=".7" fill="none"/>'
  '<circle cx="124" cy="102" r="14" fill="url(#metalv)" stroke="#6C7587" stroke-width="2"/>'
  '<circle cx="124" cy="102" r="4" fill="#4A5366"/>'
  '<path d="M178 200 L136 120" stroke="#3A4356" stroke-width="15" stroke-linecap="round" opacity=".35" transform="translate(0 4)"/>'
  '<path d="M178 200 L136 120" stroke="url(#gold)" stroke-width="13" stroke-linecap="round"/>'
  '<circle cx="178" cy="200" r="16" fill="url(#gold)" stroke="#B37A18" stroke-width="2"/><circle cx="178" cy="200" r="5" fill="#5C6678"/>'
  '<rect x="64" y="176" width="54" height="22" rx="4" fill="#1F345C"/><rect x="70" y="182" width="30" height="4" rx="2" fill="#F5C24A"/><rect x="70" y="189" width="20" height="3" rx="1.5" fill="#9DBBFF"/>'
  '</g>')

USB = ('<g filter="url(#sh)">'
  '<rect x="98" y="20" width="60" height="50" rx="4" fill="url(#metal)" stroke="#7D879A" stroke-width="1.5"/>'
  '<rect x="110" y="32" width="12" height="12" fill="#3A4356"/><rect x="134" y="32" width="12" height="12" fill="#3A4356"/>'
  '<rect x="76" y="64" width="104" height="172" rx="22" fill="#162A4F"/>'
  '<rect x="76" y="60" width="104" height="170" rx="22" fill="url(#blue)"/>'
  '<path d="M76 82 Q76 60 98 60 H158 Q180 60 180 82 V120 Q128 104 76 120Z" fill="url(#gloss)"/>'
  '<rect x="76" y="60" width="104" height="14" rx="7" fill="url(#gold)"/>'
  '<circle cx="128" cy="196" r="10" fill="url(#gold)"/><circle cx="125" cy="193" r="3" fill="#fff" opacity=".8"/>'
  '</g>')

ICONS = {
  "folder": folder(),
  "user-home": folder(emb(HOME)),
  "folder-documents": folder(emb(DOCS)),
  "folder-download": folder(emb(DOWN)),
  "folder-music": folder(emb(MUSIC)),
  "folder-pictures": folder(emb(PICS)),
  "folder-videos": folder(emb(VIDEO)),
  "user-desktop": folder(emb(DESK)),
  "folder-templates": folder(emb(TEMPL)),
  "folder-publicshare": folder(emb(SHARE)),
  "user-trash": trash(False),
  "user-trash-full": trash(True),
  "drive-harddisk": HDD,
  "drive-removable-media": USB,
}

PLACES = {"drive-harddisk", "drive-removable-media"}

INDEX_THEME = """[Icon Theme]
Name=Vilna Classic
Comment=Vilna folders, trash and drives drawn as real objects; symbolic icons from Vilna
Inherits=Vilna,Adwaita,hicolor
Example=folder
Directories=scalable/places,scalable/devices

[scalable/places]
Context=Places
Size=256
MinSize=8
MaxSize=512
Type=Scalable

[scalable/devices]
Context=Devices
Size=256
MinSize=8
MaxSize=512
Type=Scalable
"""


def build() -> dict[str, str]:
    """Every file of the theme: relative path -> text."""
    files = {"index.theme": INDEX_THEME}
    for name, body in ICONS.items():
        ctx = "devices" if name in PLACES else "places"
        files[f"scalable/{ctx}/{name}.svg"] = svg(name, body)
    return files


def main() -> int:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "Vilna-Classic")
    for rel, text in build().items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
