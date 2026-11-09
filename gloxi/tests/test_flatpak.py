# SPDX-License-Identifier: GPL-3.0-or-later
"""The Flatpak manifest stays in step with the code and with uv.lock."""

from __future__ import annotations

import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_ID = "io.github.stdenis1980.Gloxi"
MANIFEST = ROOT / "flatpak" / f"{APP_ID}.yml"
WHEEL = re.compile(r"url: (https://files\.pythonhosted\.org/\S+/([^/\s]+\.whl))\n\s+sha256: (\w+)")


# The packages the Flatpak carries, each with the uv.lock that pins its wheels.
LOCKS = {"gloxi": ROOT / "uv.lock", "vilna-mcp-settings": ROOT.parent / "mcp/settings/uv.lock"}


def _locked(package: str = "gloxi") -> dict[str, dict]:
    lock = tomllib.loads(LOCKS[package].read_text(encoding="utf-8"))
    return {p["name"]: p for p in lock["package"]}


def _runtime_dependencies(package: str = "gloxi") -> set[str]:
    """Everything a package needs at run time, from its uv.lock (local packages excluded)."""
    locked = _locked(package)
    todo, seen = [package], set()
    while todo:
        name = todo.pop()
        for dep in locked[name].get("dependencies", []):
            if dep["name"] not in seen:
                seen.add(dep["name"])
                todo.append(dep["name"])
    return {n for n in seen if "registry" in locked[n]["source"]}


def test_wheels_match_uv_lock():
    locked: dict[str, dict] = {}
    needed: set[str] = set()
    for package in LOCKS:
        locked.update(_locked(package))
        needed |= _runtime_dependencies(package)
    wheels = WHEEL.findall(MANIFEST.read_text(encoding="utf-8"))
    assert wheels
    names = set()
    for url, filename, sha in wheels:
        name, version = filename.split("-")[:2]
        name = name.replace("_", "-").lower()
        names.add(name)
        assert locked[name]["version"] == version, filename
        assert {"url": url, "hash": f"sha256:{sha}"} in [
            {"url": w["url"], "hash": w["hash"]} for w in locked[name]["wheels"]
        ], filename
    assert names == needed


def test_local_packages_are_copied():
    text = MANIFEST.read_text(encoding="utf-8")
    for pkg in (
        "gloxi/src/gloxi",
        "privacy-gateway/src/privacy_gateway",
        "herkos/src/herkos",
        "mcp-files/src/vilna_mcp_files",
        "mcp-settings/src/vilna_mcp_settings",
    ):
        assert pkg in text
    assert (ROOT / "flatpak" / "gloxi-gtk").is_file()
    assert (ROOT / "flatpak" / "gloxi").is_file()


def test_desktop_metainfo_and_icon_agree():
    data = ROOT / "data"
    desktop = (data / f"{APP_ID}.desktop").read_text(encoding="utf-8")
    assert f"Icon={APP_ID}\n" in desktop
    assert "Exec=gloxi-gtk\n" in desktop
    assert (data / "icons" / f"{APP_ID}.svg").is_file()
    assert (data / "icons" / f"{APP_ID}-symbolic.svg").is_file()
    meta = ET.parse(data / f"{APP_ID}.metainfo.xml").getroot()
    assert meta.findtext("id") == APP_ID
    assert meta.find("launchable").text == f"{APP_ID}.desktop"
    assert meta.findtext("project_license") == "GPL-3.0-or-later"


# The only paths the sandbox may open (ADR 0023, section 6): GNOME's settings
# in dconf, for the settings tool. They are not the person's files.
DCONF_PATHS = {"--filesystem=xdg-run/dconf", "--filesystem=xdg-config/dconf:ro"}


def test_no_file_access_in_the_sandbox():
    """Files come only through the document portal (folders the person chooses)."""
    text = MANIFEST.read_text(encoding="utf-8")
    opened = set(re.findall(r"--filesystem=\S+", text))
    assert opened == DCONF_PATHS
