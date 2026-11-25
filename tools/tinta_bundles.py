#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The Tinta Office bundles that go into Vilna's Flatpak repository (ADR 0011, 0024).

    python3 tools/tinta_bundles.py check          # the pins agree with the image
    python3 tools/tinta_bundles.py downloads      # "URL SHA256 FILE" per line, for CI
    python3 tools/tinta_bundles.py apps           # "ID CLI VERSION" per line, for CI
    python3 tools/tinta_bundles.py catalogue DIR  # every app is in the store's catalogue

``image/tinta-bundles.toml`` pins each app's release bundle by tag and
SHA-256. ``check`` makes sure that file is well formed and that it, the
image's ``flatpak_defaults`` (image/packages.yaml) and the first-boot list of
the "vilna" remote (obs/packages/vilna-flatpak-defaults/flatpaks-vilna.list)
name the same apps, so an app cannot be in the image without a pinned,
checked bundle, nor a bundle in the repository that the image never installs.
``downloads`` prints what CI fetches; CI checks every hash before it imports
a bundle. ``catalogue`` reads the "vilna" remote's app catalogue as Flatpak
keeps it (``/var/lib/flatpak/appstream/vilna/x86_64/active``: appstream.xml
and icons/), which is what GNOME Software shows: every pinned app must be
there with a name, a summary, a launcher and an icon, or the store would not
show it and a person who removed it could not install it again. Stdlib only;
reads no network.
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLES = ROOT / "image" / "tinta-bundles.toml"
PACKAGES_YAML = ROOT / "image" / "packages.yaml"
VILNA_LIST = ROOT / "obs" / "packages" / "vilna-flatpak-defaults" / "flatpaks-vilna.list"

# Gloxi is in the same repository but built by this repository's own CI.
NOT_TINTA = {"io.github.stdenis1980.Gloxi"}
PREFIX = "io.github.stdenis1980."
FIELDS = {"id", "name", "cli", "repo", "tag", "file", "sha256"}
# Only Tinta Office's own repositories (ADR 0011, table).
REPOS = {
    "StDenis1980/wordcraft",
    "StDenis1980/gridcraft",
    "StDenis1980/deckcraft",
    "StDenis1980/pdfcraft",
    "StDenis1980/ibercorreo",
}
ID_RE = re.compile(r"^io\.github\.stdenis1980\.[a-z][a-z0-9]*$")
TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?)$")
CLI_RE = re.compile(r"^[a-z][a-z0-9-]*-cli$")
FILE_RE = re.compile(r"^tinta-[a-z]+-(?P<version>[0-9A-Za-z.\-]+)-linux-x86_64\.flatpak$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class BundleError(Exception):
    pass


def load(path: Path = BUNDLES) -> list[dict]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise BundleError(f"cannot read {path}: {e}") from e
    bundles = data.get("bundle")
    if not isinstance(bundles, list) or not bundles:
        raise BundleError(f"{path}: no [[bundle]] entries")
    return bundles


def url(b: dict) -> str:
    return f"https://github.com/{b['repo']}/releases/download/{b['tag']}/{b['file']}"


def validate(bundles: list[dict]) -> list[str]:
    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    for n, b in enumerate(bundles, 1):
        where = f"bundle {n} ({b.get('id', '?')})"
        if not isinstance(b, dict):
            problems.append(f"bundle {n}: not a table")
            continue
        keys = set(b)
        if keys != FIELDS:
            missing, extra = sorted(FIELDS - keys), sorted(keys - FIELDS)
            problems.append(
                f"{where}: fields must be {sorted(FIELDS)} (missing {missing}, extra {extra})"
            )
            continue
        if not all(isinstance(b[k], str) and b[k] for k in FIELDS):
            problems.append(f"{where}: every field is a non-empty string")
            continue
        if not ID_RE.match(b["id"]) or b["id"] in NOT_TINTA:
            problems.append(f"{where}: id must be a Tinta app id {PREFIX}<name>")
        if b["id"] in seen_ids:
            problems.append(f"{where}: id listed twice")
        seen_ids.add(b["id"])
        if not CLI_RE.match(b["cli"]):
            problems.append(f"{where}: cli must be the app's command line tool, like pdfcraft-cli")
        if b["repo"] not in REPOS:
            problems.append(f"{where}: repo must be one of {sorted(REPOS)}")
        tag = TAG_RE.match(b["tag"])
        file = FILE_RE.match(b["file"])
        if not tag:
            problems.append(f"{where}: tag must look like v1.2.3")
        if not file:
            problems.append(
                f"{where}: file must look like tinta-<app>-<version>-linux-x86_64.flatpak"
            )
        if tag and file and tag.group(1) != file.group("version"):
            problems.append(f"{where}: tag {b['tag']} and file {b['file']} name different versions")
        if b["file"] in seen_files:
            problems.append(f"{where}: file listed twice")
        seen_files.add(b["file"])
        if not SHA_RE.match(b["sha256"]):
            problems.append(f"{where}: sha256 must be 64 lowercase hex digits")
    return problems


def read_list(path: Path) -> list[str]:
    ids = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            ids.append(line)
    return ids


def image_tinta(packages_yaml: Path) -> list[str]:
    """Tinta ids in the image's flatpak_defaults (a flat ``- id`` list)."""
    ids: list[str] = []
    inside = False
    for raw in packages_yaml.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        if not line.startswith((" ", "-")):
            inside = line == "flatpak_defaults:"
            continue
        item = line.strip()
        if inside and item.startswith("- "):
            app = item[2:].strip()
            if app.startswith(PREFIX) and app not in NOT_TINTA:
                ids.append(app)
    return ids


def agree(bundles: list[dict], image: list[str], first_boot: list[str]) -> list[str]:
    pinned = {b.get("id") for b in bundles if isinstance(b, dict)}
    problems = []
    for name, ids in (
        ("image/packages.yaml flatpak_defaults", image),
        ("flatpaks-vilna.list", first_boot),
    ):
        for app in sorted(set(ids) - pinned):
            problems.append(f"{app} is in {name} but has no bundle in image/tinta-bundles.toml")
        for app in sorted(pinned - set(ids)):
            problems.append(f"{app} has a bundle but is not in {name}")
        if len(ids) != len(set(ids)):
            problems.append(f"{name} lists an app twice")
    return problems


# The sizes GNOME Software asks for; Flatpak's catalogue has these two.
ICON_SIZES = ("64x64", "128x128")


def read_catalogue(folder: Path) -> ET.Element:
    """The AppStream catalogue of a remote, as ``flatpak update --appstream`` keeps it."""
    for name, opener in (("appstream.xml.gz", gzip.open), ("appstream.xml", open)):
        path = folder / name
        if path.is_file():
            try:
                with opener(path, "rb") as f:
                    return ET.parse(f).getroot()
            except (OSError, ET.ParseError, EOFError) as e:
                raise BundleError(f"cannot read {path}: {e}") from e
    raise BundleError(f"{folder}: no appstream.xml(.gz): was the catalogue fetched?")


def in_catalogue(bundles: list[dict], root: ET.Element, folder: Path) -> list[str]:
    """Why a pinned app would not show in the store, one line per problem."""
    components = {}
    for c in root.iter("component"):
        cid = (c.findtext("id") or "").strip()
        if cid:
            components.setdefault(cid, c)
    problems = []
    for b in bundles:
        app = b["id"]
        c = components.get(app)
        if c is None:
            problems.append(
                f"{app} ({b['name']}) is not in the catalogue: the store does not show it"
            )
            continue
        if c.get("type") != "desktop-application":
            problems.append(
                f"{app}: type is {c.get('type')!r}, the store lists only desktop-application"
            )
        if not (c.findtext("name") or "").strip():
            problems.append(f"{app}: no name")
        if not (c.findtext("summary") or "").strip():
            problems.append(f"{app}: no summary")
        if (c.findtext("launchable") or "").strip() != f"{app}.desktop":
            problems.append(f"{app}: no launchable {app}.desktop")
        cached = {(i.text or "").strip() for i in c.findall("icon") if i.get("type") == "cached"}
        if not cached:
            problems.append(f"{app}: no cached icon")
        for size in ICON_SIZES:
            if not any((folder / "icons" / size / name).is_file() for name in cached):
                problems.append(f"{app}: no {size} icon in {folder / 'icons' / size}")
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("command", choices=["check", "downloads", "apps", "catalogue"])
    p.add_argument(
        "folder",
        nargs="?",
        type=Path,
        help="catalogue: the remote's appstream folder (appstream.xml.gz and icons/)",
    )
    p.add_argument("--bundles", type=Path, default=BUNDLES)
    p.add_argument("--packages", type=Path, default=PACKAGES_YAML)
    p.add_argument("--first-boot", type=Path, default=VILNA_LIST)
    args = p.parse_args(argv)
    try:
        bundles = load(args.bundles)
        problems = validate(bundles)
        if args.command == "check":
            problems += agree(bundles, image_tinta(args.packages), read_list(args.first_boot))
        elif args.command == "catalogue" and not problems:
            if args.folder is None:
                p.error("catalogue needs the appstream folder")
            problems += in_catalogue(bundles, read_catalogue(args.folder), args.folder)
    except (BundleError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if problems:
        for line in problems:
            print(f"error: {line}", file=sys.stderr)
        return 1
    if args.command == "check":
        print(f"{len(bundles)} Tinta Office bundles pinned, in the image and on first boot")
    elif args.command == "catalogue":
        print(f"{len(bundles)} Tinta Office apps in the store's catalogue, with icons")
    elif args.command == "downloads":
        for b in bundles:
            print(url(b), b["sha256"], b["file"])
    else:
        for b in bundles:
            print(b["id"], b["cli"], b["tag"].removeprefix("v"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
