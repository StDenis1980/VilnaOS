#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/cursor_apps.py (pins of the Cursor and Grok Bot .deb).

Run: python3 tools/test_cursor_apps.py
"""

from __future__ import annotations

import datetime
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import cursor_apps as ca  # after sys.path.insert above

BASE = "https://downloads.cursor.com/production/0123abcd/linux"


class Pins(unittest.TestCase):
    def new_pins(self, app: ca.App, version: str) -> dict[str, ca.Pin]:
        return {
            arch: ca.Pin(arch, f"{BASE}/{deb}/{app.package}_{version}_{deb}.deb", "12" * 32, 42)
            for arch, (_, deb) in ca.ARCHES.items()
        }

    def test_both_manifests_have_one_pin_per_arch(self):
        for app in ca.APPS.values():
            self.assertEqual(set(ca.read_pins(app.manifest.read_text(encoding="utf-8"))), set(ca.ARCHES), app.key)

    def test_write_moves_pins_and_release(self):
        for app in ca.APPS.values():
            manifest = app.manifest.read_text(encoding="utf-8")
            metainfo = app.metainfo.read_text(encoding="utf-8")
            new = self.new_pins(app, "3.32.4")
            m, meta = ca.write_pins(app, manifest, metainfo, new, datetime.date(2027, 1, 2))
            self.assertEqual(ca.read_pins(m), new)
            self.assertEqual(ca.check_pins(app, ca.read_pins(m)), [])
            self.assertEqual(ca.cd.metainfo_version(meta), "3.32.4")
            self.assertNotIn('version="0.0.0"', meta)
            self.assertEqual(m.count("\n"), manifest.count("\n"))
            m2, meta2 = ca.write_pins(app, m, meta, self.new_pins(app, "3.33.0"), datetime.date(2027, 2, 3))
            self.assertLess(meta2.index('"3.33.0"'), meta2.index('"3.32.4"'))

    def test_check_rejects_foreign_hosts_wrong_arch_and_mixed_versions(self):
        app = ca.APPS["grok-bot"]
        pins = self.new_pins(app, "1.3.0")
        pins["x86_64"] = ca.Pin("x86_64", "https://example.org/grok-bot_1.3.0_amd64.deb", "12" * 32, 42)
        self.assertTrue(any("is not on" in p for p in ca.check_pins(app, pins)))
        pins = self.new_pins(app, "1.3.0")
        pins["aarch64"] = ca.Pin("aarch64", pins["x86_64"].url, "12" * 32, 42)
        self.assertTrue(any("not the arm64 package" in p for p in ca.check_pins(app, pins)))
        pins = self.new_pins(app, "1.3.0")
        pins["aarch64"] = self.new_pins(app, "1.3.1")["aarch64"]
        self.assertTrue(any("different versions" in p for p in ca.check_pins(app, pins)))

    def test_placeholder_pins_are_not_accepted(self):
        for app in ca.APPS.values():
            pins = ca.read_pins(app.manifest.read_text(encoding="utf-8"))
            if all(p.size == 0 for p in pins.values()):
                self.assertTrue(ca.check_pins(app, pins))


if __name__ == "__main__":
    unittest.main()
