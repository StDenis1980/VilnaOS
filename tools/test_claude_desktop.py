#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/claude_desktop.py (pins of the Claude desktop .deb).

Run: python3 tools/test_claude_desktop.py
"""

from __future__ import annotations

import datetime
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import claude_desktop as cd  # after sys.path.insert above

POOL = f"{cd.REPO}/pool/main/c/claude-desktop"


def packages(*entries: tuple[str, str, str]) -> str:
    return "\n\n".join(
        f"Package: {name}\nVersion: {version}\nArchitecture: {arch}\n"
        f"Filename: pool/main/c/{name}/{name}_{version}_{arch}.deb\nSize: 1234\n"
        f"SHA256: {'ab' * 32}\nDescription: x\n continued line"
        for name, version, arch in entries
    )


class Versions(unittest.TestCase):
    def test_debian_order(self):
        ordered = ["1.9.0", "1.10.0", "1.10.0-1", "1:0.1"]
        for a, b in zip(ordered, ordered[1:]):
            self.assertLess(cd.compare_versions(a, b), 0, (a, b))
            self.assertGreater(cd.compare_versions(b, a), 0, (a, b))
        self.assertLess(cd.compare_versions("2.0~beta1", "2.0"), 0)

    def test_newest_of_the_package_and_arch_only(self):
        text = packages(
            ("claude-desktop", "1.9.0", "amd64"),
            ("claude-desktop", "1.10.0", "amd64"),
            ("claude-desktop", "9.0.0", "arm64"),
            ("other", "99.0", "amd64"),
        )
        self.assertEqual(cd.newest(text, "amd64")["Version"], "1.10.0")

    def test_missing_package_is_an_error(self):
        with self.assertRaises(cd.PinError):
            cd.newest(packages(("other", "1.0", "amd64")), "amd64")


class Release(unittest.TestCase):
    def test_sha256_section(self):
        text = (
            "Origin: x\nSuite: stable\nSHA256:\n"
            f" {'cd' * 32} 500 main/binary-amd64/Packages\n"
            f" {'ef' * 32} 200 main/binary-amd64/Packages.gz\n"
        )
        hashes = cd.release_hashes(text)
        self.assertEqual(hashes["main/binary-amd64/Packages"], ("cd" * 32, 500))
        self.assertIn("main/binary-amd64/Packages.gz", hashes)


class Manifest(unittest.TestCase):
    def setUp(self):
        self.manifest = cd.MANIFEST.read_text(encoding="utf-8")
        self.metainfo = cd.METAINFO.read_text(encoding="utf-8")

    def test_reads_one_pin_per_arch(self):
        self.assertEqual(set(cd.read_pins(self.manifest)), set(cd.ARCHES))

    def new_pins(self, version: str) -> dict[str, cd.Pin]:
        return {
            arch: cd.Pin(arch, f"{POOL}/claude-desktop_{version}_{deb}.deb", "12" * 32, 42)
            for arch, deb in cd.ARCHES.items()
        }

    def test_write_moves_pins_and_release(self):
        new = self.new_pins("1.30000.0")
        m, meta = cd.write_pins(self.manifest, self.metainfo, new, datetime.date(2027, 1, 2))
        self.assertEqual(cd.read_pins(m), new)
        self.assertEqual(cd.check_pins(cd.read_pins(m)), [])
        self.assertEqual(cd.metainfo_version(meta), "1.30000.0")
        self.assertNotIn('version="0.0.0"', meta)
        # The rest of the manifest is untouched.
        self.assertEqual(m.count("\n"), self.manifest.count("\n"))
        # A second move keeps the older release below the newer one.
        m2, meta2 = cd.write_pins(m, meta, self.new_pins("1.30001.0"), datetime.date(2027, 2, 3))
        self.assertLess(meta2.index('"1.30001.0"'), meta2.index('"1.30000.0"'))

    def test_check_rejects_foreign_hosts_and_mixed_versions(self):
        pins = self.new_pins("1.2.3")
        pins["x86_64"] = cd.Pin("x86_64", "https://example.org/claude-desktop_1.2.3_amd64.deb", "12" * 32, 42)
        self.assertTrue(any("not in Anthropic's apt repository" in p for p in cd.check_pins(pins)))
        pins = self.new_pins("1.2.3")
        pins["aarch64"] = self.new_pins("1.2.4")["aarch64"]
        self.assertTrue(any("different versions" in p for p in cd.check_pins(pins)))
        pins = self.new_pins("1.2.3")
        pins["aarch64"] = cd.Pin("aarch64", pins["x86_64"].url, "12" * 32, 42)
        self.assertTrue(any("not the arm64 package" in p for p in cd.check_pins(pins)))


if __name__ == "__main__":
    unittest.main()
