# SPDX-License-Identifier: GPL-3.0-or-later
"""Check a Gloxi release tag and write its notes (roadmap: 0.1 preview on GitHub Releases).

    python3 tools/release.py gloxi-v0.1.0 notes.md

The tag must be ``gloxi-v`` + the version in pyproject.toml, and the
metainfo must list that version as its newest release: the same number is
then seen by pip, by the Flatpak and by GNOME Software. The notes are the
release's description from the metainfo, plus how to install and check the
bundle. Stdlib only, so it runs anywhere CI does.
"""

from __future__ import annotations

import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "gloxi-v"
APP_ID = "io.github.stdenis1980.Gloxi"


class ReleaseError(Exception):
    pass


def appstream_version(version: str) -> str:
    """PEP 440 → AppStream: pre-releases sort before the release with "~"."""
    return re.sub(r"\.?(dev|a|b|rc)(\d+)", r"~\1\2", version)


def is_prerelease(version: str) -> bool:
    """Everything before 1.0, and every dev/alpha/beta/rc, is a preview."""
    return version.startswith("0.") or bool(re.search(r"(dev|a|b|rc)\d+", version))


def project_version(root: Path = ROOT) -> str:
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def newest_release(root: Path = ROOT) -> ET.Element:
    path = root / "data" / f"{APP_ID}.metainfo.xml"
    releases = ET.parse(path).getroot().find("releases")
    if releases is None or not len(releases):
        raise ReleaseError(f"{path.name} lists no releases")
    return releases[0]  # AppStream: newest first


def check(tag: str, root: Path = ROOT) -> str:
    """The version the tag names, if everything agrees."""
    if not tag.startswith(PREFIX):
        raise ReleaseError(f"a Gloxi tag starts with {PREFIX!r}, not {tag!r}")
    version = tag.removeprefix(PREFIX)
    pyproject = project_version(root)
    if version != pyproject:
        raise ReleaseError(f"the tag says {version}, pyproject.toml says {pyproject}")
    release = newest_release(root)
    wanted = appstream_version(version)
    if release.get("version") != wanted:
        raise ReleaseError(
            f"the newest metainfo release is {release.get('version')}, it should be {wanted}"
        )
    if not release.get("date"):
        raise ReleaseError("the metainfo release has no date")
    return version


def notes(version: str, root: Path = ROOT) -> str:
    release = newest_release(root)
    lines: list[str] = []
    description = release.find("description")
    if description is not None:
        for element in description:
            if element.get("{http://www.w3.org/XML/1998/namespace}lang"):
                continue  # the English text; translations live in the metainfo
            if element.tag == "p":
                lines += [" ".join("".join(element.itertext()).split()), ""]
            elif element.tag in ("ul", "ol"):
                for item in element:
                    if not item.get("{http://www.w3.org/XML/1998/namespace}lang"):
                        lines.append("- " + " ".join("".join(item.itertext()).split()))
                lines.append("")
    lines += [
        "## Install",
        "",
        "```sh",
        "flatpak remote-add --user --if-not-exists flathub "
        "https://dl.flathub.org/repo/flathub.flatpakrepo",
        "flatpak install --user flathub org.gnome.Platform//50",
        "flatpak install --user gloxi.flatpak",
        f"flatpak run {APP_ID}",
        "```",
        "",
        "Check the download first: `sha256sum -c gloxi.flatpak.sha256`.",
        "",
        "The model is not in the bundle. On the first start Gloxi asks before it "
        "downloads Qwen3 4B (about 2.5 GB) and checks its SHA-256.",
    ]
    if is_prerelease(version):
        lines += ["", "This is a preview for testers."]
    return "\n".join(lines).strip() + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    try:
        version = check(argv[1])
    except ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    Path(argv[2]).write_text(notes(version), encoding="utf-8")
    print(f"version={version}")
    print(f"prerelease={'true' if is_prerelease(version) else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
