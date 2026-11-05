#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for obs/packages/vilna-software-picks/build_picks.py.

Run: python3 tools/test_software_picks.py
"""

from __future__ import annotations

import configparser
import copy
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.dont_write_bytecode = True  # the package folder is copied to OBS as is
ROOT = Path(__file__).resolve().parent.parent
PICKS_DIR = ROOT / "obs" / "packages" / "vilna-software-picks"
sys.path.insert(0, str(PICKS_DIR))

import build_picks  # after sys.path.insert above

PACKAGES_YAML = ROOT / "image" / "packages.yaml"


class RealPicks(unittest.TestCase):
    """The committed list follows ADR 0011 and ADR 0012."""

    def setUp(self) -> None:
        self.data = build_picks.load(PICKS_DIR / "picks.toml")

    def test_valid_with_image_defaults(self) -> None:
        defaults = build_picks.flatpak_defaults(PACKAGES_YAML)
        self.assertIn("org.mozilla.firefox", defaults)
        self.assertEqual(build_picks.validate(self.data, defaults), [])

    def test_build_writes_what_gnome_software_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.assertEqual(build_picks.main(["build", str(out)]), 0)
            root = ET.parse(out / build_picks.XML_NAME).getroot()
            ini = configparser.ConfigParser(interpolation=None)
            ini.optionxform = str  # keep Title[uk] as written
            ini.read(out / build_picks.INI_NAME, encoding="utf-8")

        section = ini["Deployment Featured Apps"]
        self.assertEqual(section["Selector"], "vilna")
        self.assertEqual(section["Title[uk]"], "Рекомендовано Vilna")
        for lang in build_picks.LANGUAGES:
            key = "Title" if lang == "en" else f"Title[{lang}]"
            self.assertTrue(section[key])

        components = root.findall("component")
        self.assertEqual(len(components), len(self.data["app"]))
        featured = root.findall("component/custom/value[@key='GnomeSoftware::DeploymentFeatured']")
        self.assertTrue(all(v.text == "vilna" for v in featured))
        self.assertGreaterEqual(len(featured), build_picks.MIN_FEATURED)
        for comp in components:
            self.assertEqual(comp.get("merge"), "append")
            app_id = comp.findtext("id")
            pick = comp.find("custom/value[@key='Vilna::Pick']")
            self.assertIsNotNone(pick, app_id)

        # Default apps are picks, but do not take tiles on the Explore page.
        by_id = {c.findtext("id"): c for c in components}
        firefox = by_id["org.mozilla.firefox"]
        self.assertIsNone(firefox.find("custom/value[@key='GnomeSoftware::DeploymentFeatured']"))
        self.assertIsNone(firefox.find("categories"))
        gimp = by_id["org.gimp.GIMP"]
        self.assertEqual(gimp.findtext("categories/category"), "Featured")


class Rules(unittest.TestCase):
    """Each rule of validate() catches what it should."""

    def setUp(self) -> None:
        self.data = build_picks.load(PICKS_DIR / "picks.toml")

    def problems(self, data: dict, defaults: list[str] | None = None) -> str:
        return "\n".join(build_picks.validate(data, defaults))

    def test_replaced_by_tinta(self) -> None:
        data = copy.deepcopy(self.data)
        data["app"][-1]["id"] = "org.libreoffice.LibreOffice"
        self.assertIn("replaced by Tinta Office", self.problems(data))

    def test_nonfree_licence(self) -> None:
        data = copy.deepcopy(self.data)
        data["app"][-1]["license"] = "LicenseRef-proprietary"
        self.assertIn("not free", self.problems(data))

    def test_duplicate_and_bad_id(self) -> None:
        data = copy.deepcopy(self.data)
        data["app"].append(dict(data["app"][0]))
        data["app"][1]["id"] = "firefox"
        out = self.problems(data)
        self.assertIn("listed twice", out)
        self.assertIn("reverse-DNS", out)

    def test_unknown_category_and_field(self) -> None:
        data = copy.deepcopy(self.data)
        data["app"][-1]["category"] = "office"
        data["app"][-1]["stars"] = 5
        out = self.problems(data)
        self.assertIn("unknown category", out)
        self.assertIn("unknown field stars", out)

    def test_community_build_must_say_so(self) -> None:
        data = copy.deepcopy(self.data)
        signal = next(a for a in data["app"] if a["id"] == "org.signal.Signal")
        self.assertEqual(signal["source"], "flathub-community")
        self.assertEqual(self.problems(data), "")
        signal["why"] = "Signal для комп'ютера."
        self.assertIn("must say «неофіційна»", self.problems(data))
        signal["source"] = "flathub-maybe"
        self.assertIn("source must be one of", self.problems(data))

    def test_missing_title_language(self) -> None:
        data = copy.deepcopy(self.data)
        del data["section"]["title"]["lt"]
        self.assertIn("section.title.lt", self.problems(data))

    def test_too_few_for_the_explore_page(self) -> None:
        data = copy.deepcopy(self.data)
        for app in data["app"]:
            app["default"] = True
        self.assertIn("hides the section", self.problems(data))

    def test_defaults_must_match_the_image(self) -> None:
        defaults = build_picks.flatpak_defaults(PACKAGES_YAML)
        out = self.problems(self.data, defaults + ["org.example.NewDefault"])
        self.assertIn("org.example.NewDefault is a default app but not in the picks", out)
        out = self.problems(self.data, [d for d in defaults if d != "org.gnome.Loupe"])
        self.assertIn("org.gnome.Loupe has default = true", out)
        data = copy.deepcopy(self.data)
        loupe = next(a for a in data["app"] if a["id"] == "org.gnome.Loupe")
        loupe["default"] = False
        self.assertIn("set default = true", self.problems(data, defaults))

    def test_flatpak_defaults_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "packages.yaml"
            path.write_text(
                "base:\n  pattern: x\nflatpak_defaults:\n  # comment\n"
                "  - org.a.One\n  - org.b.Two   # note\n\nnot_in_image:\n  - x.y.z\n",
                encoding="utf-8",
            )
            self.assertEqual(build_picks.flatpak_defaults(path), ["org.a.One", "org.b.Two"])
            path.write_text("base:\n  - a.b.c\n", encoding="utf-8")
            with self.assertRaises(build_picks.PicksError):
                build_picks.flatpak_defaults(path)


if __name__ == "__main__":
    unittest.main()
