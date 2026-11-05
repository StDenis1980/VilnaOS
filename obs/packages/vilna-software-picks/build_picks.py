#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the «Рекомендовано Vilna» metadata for GNOME Software.

Reads ``picks.toml`` and writes two files:

* ``vilna-software-picks.xml``: an AppStream catalog of ``merge="append"``
  components. Installed in ``/usr/share/swcatalog/xml``, it adds to the
  Flathub metadata of each picked app:
  - ``GnomeSoftware::DeploymentFeatured`` = selector, so the app can appear
    in the Vilna section on the Explore page;
  - the ``Featured`` category, so it is highlighted on its category page;
  - ``Vilna::Pick`` = the pick's category, which Gloxi and the Herkos
    dialog read to know an app is in the picks (ADR 0012, points 5 and 6).
  Apps installed by default get only ``Vilna::Pick``.
* ``deployment-featured.ini``: the title of the Vilna section (installed in
  ``/usr/share/gnome-software``).

See gnome-software's doc/vendor-customisation.md ("Deployment Featured
Apps") for the mechanism. ``check`` validates the list against the ADR 0012
rules without writing anything.

Standard library only (Python 3.11+): it runs inside the OBS build.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent

# The ten main languages of Vilna (ADR 0009).
LANGUAGES = ("uk", "en", "es", "pl", "de", "fr", "it", "ro", "cs", "lt")

# GNOME Software shows the deployment section only with at least this many
# resolvable apps (N_TILES in src/gs-overview-page.c), picking 12 at random.
MIN_FEATURED = 12
# ADR 0012: "орієнтовно 30–60 програм".
MIN_TOTAL, MAX_TOTAL = 30, 60

# "flathub-community" only by a separate ADR (ADR 0019: Signal).
SOURCES = {"flathub-verified", "flathub-community", "vilna"}

# ADR 0011: Tinta Office replaces these; people install them themselves.
REPLACED_BY_TINTA = {
    "org.libreoffice.LibreOffice",
    "org.mozilla.Thunderbird",
    "org.gnome.Papers",
    "org.gnome.Evince",
    "org.gnome.Calendar",
    "org.gnome.Contacts",
}

# Licences that are not free (OSI/FSF) and must never be in the picks.
NONFREE_MARKERS = ("LicenseRef-proprietary", "proprietary", "NonFree")

APP_ID = re.compile(r"^[A-Za-z_][\w-]*(\.[A-Za-z_][\w-]*){2,}$")
SELECTOR = re.compile(r"^[a-z][a-z0-9-]*$")
FIELDS = {"id", "category", "license", "source", "default", "why"}

XML_NAME = "vilna-software-picks.xml"
INI_NAME = "deployment-featured.ini"


class PicksError(ValueError):
    pass


def load(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def validate(data: dict, defaults: list[str] | None = None) -> list[str]:
    """Return the problems found in the picks; empty when all is well."""
    problems: list[str] = []
    section = data.get("section", {})
    selector = section.get("selector", "")
    if not SELECTOR.match(selector):
        problems.append(f"section.selector {selector!r} must match {SELECTOR.pattern}")
    titles = section.get("title", {})
    for lang in LANGUAGES:
        if not str(titles.get(lang, "")).strip():
            problems.append(f"section.title.{lang} is missing (ADR 0009)")
    categories = data.get("categories", {})
    if not categories:
        problems.append("[categories] is empty")

    apps = data.get("app", [])
    seen: set[str] = set()
    used_categories: set[str] = set()
    for n, app in enumerate(apps, 1):
        where = f"app #{n} ({app.get('id', '?')})"
        missing = FIELDS - app.keys()
        extra = app.keys() - FIELDS
        if missing:
            problems.append(f"{where}: missing {', '.join(sorted(missing))}")
            continue
        if extra:
            problems.append(f"{where}: unknown field {', '.join(sorted(extra))}")
        app_id = app["id"]
        if not isinstance(app_id, str) or not APP_ID.match(app_id):
            problems.append(f"{where}: id is not a reverse-DNS application id")
        if app_id in seen:
            problems.append(f"{where}: listed twice")
        seen.add(app_id)
        if app_id in REPLACED_BY_TINTA:
            problems.append(f"{where}: replaced by Tinta Office (ADR 0011)")
        if app["category"] not in categories:
            problems.append(f"{where}: unknown category {app['category']!r}")
        used_categories.add(app["category"])
        lic = app["license"]
        if not isinstance(lic, str) or not lic.strip():
            problems.append(f"{where}: license is empty")
        elif any(m.lower() in lic.lower() for m in NONFREE_MARKERS):
            problems.append(f"{where}: license {lic!r} is not free (ADR 0012)")
        if app["source"] not in SOURCES:
            problems.append(f"{where}: source must be one of {sorted(SOURCES)}")
        elif app["source"] == "flathub-community" and "неофіційн" not in str(app.get("why", "")):
            problems.append(f"{where}: a flathub-community build must say «неофіційна» in why (ADR 0019)")
        if not isinstance(app["default"], bool):
            problems.append(f"{where}: default must be true or false")
        if not str(app["why"]).strip():
            problems.append(f"{where}: why is empty")

    for cat in sorted(set(categories) - used_categories):
        problems.append(f"category {cat!r} has no apps")
    if not MIN_TOTAL <= len(apps) <= MAX_TOTAL:
        problems.append(f"{len(apps)} apps; ADR 0012 asks for {MIN_TOTAL}–{MAX_TOTAL}")
    featured = [a for a in apps if a.get("default") is False]
    if len(featured) < MIN_FEATURED:
        problems.append(
            f"only {len(featured)} non-default apps; GNOME Software hides the "
            f"section below {MIN_FEATURED}"
        )

    if defaults is not None:
        picked_defaults = {a["id"] for a in apps if a.get("default") is True}
        for app_id in sorted(set(defaults) - seen):
            problems.append(f"{app_id} is a default app but not in the picks (ADR 0012)")
        for app_id in sorted(set(defaults) & (seen - picked_defaults)):
            problems.append(f"{app_id} is a default app: set default = true")
        for app_id in sorted(picked_defaults - set(defaults)):
            problems.append(f"{app_id} has default = true but is not in flatpak_defaults")
    return problems


def render_xml(data: dict) -> str:
    selector = data["section"]["selector"]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!-- SPDX-License-Identifier: GPL-3.0-or-later -->",
        "<!-- Generated by build_picks.py from picks.toml; do not edit. -->",
        '<components version="1.0" origin="vilna-software-picks">',
    ]
    for app in data["app"]:
        lines.append('  <component merge="append">')
        lines.append(f"    <id>{escape(app['id'])}</id>")
        lines.append("    <custom>")
        if not app["default"]:
            lines.append(
                f'      <value key="GnomeSoftware::DeploymentFeatured">{escape(selector)}</value>'
            )
        lines.append(f'      <value key="Vilna::Pick">{escape(app["category"])}</value>')
        lines.append("    </custom>")
        if not app["default"]:
            lines.append("    <categories>")
            lines.append("      <category>Featured</category>")
            lines.append("    </categories>")
        lines.append("  </component>")
    lines.append("</components>")
    return "\n".join(lines) + "\n"


def render_ini(data: dict) -> str:
    section = data["section"]
    titles = section["title"]
    lines = [
        "# SPDX-License-Identifier: GPL-3.0-or-later",
        "# Generated by build_picks.py from picks.toml; do not edit.",
        "# The «Рекомендовано Vilna» section on the Explore page of GNOME Software.",
        "[Deployment Featured Apps]",
        f"Selector={section['selector']}",
        f"Title={titles['en']}",
    ]
    for lang in LANGUAGES:
        if lang != "en":
            lines.append(f"Title[{lang}]={titles[lang]}")
    return "\n".join(lines) + "\n"


def flatpak_defaults(packages_yaml: Path) -> list[str]:
    """The ``flatpak_defaults`` ids of image/packages.yaml.

    A small reader for that one flat list, so the check needs no YAML
    library: ``- id`` items until the next top-level key.
    """
    ids: list[str] = []
    inside = False
    for raw in packages_yaml.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        if not line.startswith((" ", "-")):
            inside = line == "flatpak_defaults:"
            continue
        if inside:
            item = line.strip()
            if item.startswith("- "):
                ids.append(item[2:].strip())
    if not ids:
        raise PicksError(f"no flatpak_defaults found in {packages_yaml}")
    return ids


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--picks", type=Path, default=HERE / "picks.toml")
    p.add_argument(
        "--defaults",
        type=Path,
        default=None,
        help="image/packages.yaml: also check that the default apps are in the picks",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="validate picks.toml")
    b = sub.add_parser("build", help="validate and write the XML and the .ini")
    b.add_argument("out", type=Path)
    args = p.parse_args(argv)

    data = load(args.picks)
    defaults = flatpak_defaults(args.defaults) if args.defaults else None
    problems = validate(data, defaults)
    for problem in problems:
        print(f"{args.picks.name}: {problem}", file=sys.stderr)
    if problems:
        return 1

    apps = data["app"]
    featured = sum(1 for a in apps if not a["default"])
    if args.cmd == "build":
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / XML_NAME).write_text(render_xml(data), encoding="utf-8")
        (args.out / INI_NAME).write_text(render_ini(data), encoding="utf-8")
    print(f"{len(apps)} apps, {featured} on the Explore page, {len(data['categories'])} categories")
    return 0


if __name__ == "__main__":
    sys.exit(main())
