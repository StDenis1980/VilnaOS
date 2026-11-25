#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/tinta_bundles.py and the first-boot script that installs
Tinta Office (obs/packages/vilna-flatpak-defaults/vilna-flatpak-defaults.sh).

Run: python3 tools/test_tinta_bundles.py
"""

from __future__ import annotations

import copy
import gzip
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import tinta_bundles as tb  # after sys.path.insert above

DEFAULTS = ROOT / "obs" / "packages" / "vilna-flatpak-defaults"
SCRIPT = DEFAULTS / "vilna-flatpak-defaults.sh"


class RealPins(unittest.TestCase):
    def test_committed_pins_check(self) -> None:
        self.assertEqual(tb.main(["check"]), 0)

    def test_all_five_apps(self) -> None:
        ids = {b["id"] for b in tb.load()}
        self.assertEqual(
            ids,
            {
                "io.github.stdenis1980.tintatexto",
                "io.github.stdenis1980.tintacalculo",
                "io.github.stdenis1980.tintapresenta",
                "io.github.stdenis1980.tintapdf",
                "io.github.stdenis1980.ibercorreo",
            },
        )

    def test_download_url(self) -> None:
        b = next(b for b in tb.load() if b["id"].endswith("ibercorreo"))
        self.assertEqual(
            tb.url(b),
            f"https://github.com/StDenis1980/ibercorreo/releases/download/{b['tag']}/{b['file']}",
        )

    def test_mimeapps_point_at_pinned_apps(self) -> None:
        ids = {b["id"] for b in tb.load()}
        text = (DEFAULTS / "gnome-mimeapps.list").read_text(encoding="utf-8")
        apps = {
            line.split("=", 1)[1].removesuffix(".desktop")
            for line in text.splitlines()
            if "=" in line and not line.startswith("#")
        }
        self.assertTrue(apps)
        self.assertLessEqual(apps, ids)

    def test_repository_file_is_the_published_one(self) -> None:
        # Copied unchanged from https://flatpak.vilnaos.com/vilna.flatpakrepo;
        # first boot adds the "vilna" remote from it, so a wrong URL or key
        # would leave Tinta Office uninstalled on every computer.
        import base64
        import configparser

        repo = configparser.ConfigParser(interpolation=None)
        repo.optionxform = str  # keep "GPGKey", "Url"
        repo.read(DEFAULTS / "vilna.flatpakrepo", encoding="utf-8")
        section = repo["Flatpak Repo"]
        self.assertEqual(section["Url"], "https://flatpak.vilnaos.com/repo/")
        key = base64.b64decode(section["GPGKey"], validate=True)
        # An OpenPGP public key packet, and the fingerprint's last 8 bytes
        # (key id 25496EF9222B4D2C) in its signature.
        self.assertEqual(key[0], 0x98)
        self.assertIn(bytes.fromhex("25496EF9222B4D2C"), key)


GOOD = {
    "id": "io.github.stdenis1980.tintapdf",
    "name": "Tinta PDF",
    "cli": "pdfcraft-cli",
    "repo": "StDenis1980/pdfcraft",
    "tag": "v0.18.1",
    "file": "tinta-pdf-0.18.1-linux-x86_64.flatpak",
    "sha256": "a" * 64,
}


class Validate(unittest.TestCase):
    def bad(self, **change: object) -> list[str]:
        b = copy.deepcopy(GOOD)
        b.update(change)
        return tb.validate([b])

    def test_good(self) -> None:
        self.assertEqual(tb.validate([GOOD]), [])

    def test_hash(self) -> None:
        self.assertTrue(self.bad(sha256="A" * 64))
        self.assertTrue(self.bad(sha256="a" * 63))

    def test_versions_must_agree(self) -> None:
        self.assertTrue(self.bad(tag="v0.18.2"))

    def test_only_tinta_repositories(self) -> None:
        self.assertTrue(self.bad(repo="someone/pdfcraft"))
        self.assertTrue(self.bad(repo="StDenis1980/pdfcraft/../x"))

    def test_not_gloxi(self) -> None:
        self.assertTrue(self.bad(id="io.github.stdenis1980.Gloxi"))

    def test_file_name(self) -> None:
        self.assertTrue(self.bad(file="../tinta-pdf-0.18.1-linux-x86_64.flatpak"))
        self.assertTrue(self.bad(file="tinta-pdf-0.18.1-linux-x86_64.flatpak; rm -rf /"))

    def test_fields(self) -> None:
        b = dict(GOOD)
        del b["sha256"]
        self.assertTrue(tb.validate([b]))
        self.assertTrue(tb.validate([{**GOOD, "url": "https://example.org"}]))

    def test_cli(self) -> None:
        self.assertTrue(self.bad(cli="sh -c x"))
        self.assertTrue(self.bad(cli="pdfcraft"))

    def test_twice(self) -> None:
        self.assertTrue(tb.validate([GOOD, GOOD]))


class Agree(unittest.TestCase):
    def test_same_apps(self) -> None:
        self.assertEqual(tb.agree([GOOD], [GOOD["id"]], [GOOD["id"]]), [])

    def test_missing_bundle(self) -> None:
        other = "io.github.stdenis1980.tintatexto"
        self.assertTrue(tb.agree([GOOD], [GOOD["id"], other], [GOOD["id"]]))

    def test_bundle_never_installed(self) -> None:
        self.assertTrue(tb.agree([GOOD], [GOOD["id"]], []))

    def test_image_reader_skips_gloxi_and_flathub(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "packages.yaml"
            p.write_text(
                "flatpak_defaults:\n  - org.mozilla.firefox\n"
                "  - io.github.stdenis1980.tintapdf  # comment\n"
                "  - io.github.stdenis1980.Gloxi\nnot_in_image:\n  - io.github.stdenis1980.x\n",
                encoding="utf-8",
            )
            self.assertEqual(tb.image_tinta(p), ["io.github.stdenis1980.tintapdf"])


class FirstBoot(unittest.TestCase):
    """vilna-flatpak-defaults.sh with a fake flatpak that logs its calls."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.share, self.state, self.log = t / "share", t / "state", t / "calls"
        self.share.mkdir()
        for name in ("flatpaks.list", "flatpaks-vilna.list"):
            (self.share / name).write_text((DEFAULTS / name).read_text(encoding="utf-8"))
        self.fake = t / "flatpak"
        self.fake.write_text(
            '#!/bin/sh\necho "$*" >> "$LOG"\n[ -e "$FAIL" ] && case "$*" in *"$(cat "$FAIL")"*) exit 1;; esac\nexit 0\n'
        )
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.fail = t / "fail"
        self.online = t / "nm-online"  # fake: online unless the file "offline" exists
        self.online.write_text(f'#!/bin/sh\n[ ! -e "{t}/offline" ]\n')
        self.online.chmod(self.online.stat().st_mode | stat.S_IXUSR)
        self.offline = t / "offline"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_script(self) -> subprocess.CompletedProcess:
        env = dict(
            os.environ,
            VILNA_FLATPAK_SHARE=str(self.share),
            VILNA_FLATPAK_STATE=str(self.state),
            VILNA_FLATPAK_BIN=str(self.fake),
            VILNA_FLATPAK_NM_ONLINE=str(self.online),
            LOG=str(self.log),
            FAIL=str(self.fail),
        )
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_without_vilna_repository_file_tinta_waits(self) -> None:
        # Not a failure: the next boot (a package update) brings the file.
        r = self.run_script()
        self.assertEqual(r.returncode, 0)
        self.assertIn("vilna: no repository file", r.stdout)
        self.assertTrue((self.state / "flatpak-defaults-flathub.done").exists())
        self.assertFalse((self.state / "flatpak-defaults-all.done").exists())
        # Next boot: Flathub is not asked again.
        self.log.unlink()
        self.run_script()
        self.assertEqual(self.calls(), [])

    def test_both_remotes(self) -> None:
        (self.share / "vilna.flatpakrepo").write_text("[Flatpak Repo]\n")
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.calls()
        self.assertIn(
            f"remote-add --system --if-not-exists vilna {self.share}/vilna.flatpakrepo", calls
        )
        vilna = [
            c for c in calls if c.startswith("install --system --noninteractive --or-update vilna ")
        ]
        self.assertEqual(len(vilna), 1)
        for app in tb.read_list(DEFAULTS / "flatpaks-vilna.list"):
            self.assertIn(app, vilna[0].split())
        self.assertNotIn("tinta", " ".join(c for c in calls if " flathub " in c))
        self.assertTrue((self.state / "flatpak-defaults-all.done").exists())

    def test_languages_set_before_each_install(self) -> None:
        (self.share / "vilna.flatpakrepo").write_text("[Flatpak Repo]\n")
        self.assertEqual(self.run_script().returncode, 0)
        calls = self.calls()
        lang = "config --system --set languages uk;en;es;pl;de;fr;it;ro;cs;lt"
        self.assertIn(lang, calls)
        for remote in ("flathub", "vilna"):
            install = next(
                i
                for i, c in enumerate(calls)
                if c.startswith(f"install --system --noninteractive --or-update {remote} ")
            )
            self.assertIn(lang, calls[:install])

    def test_a_failed_remote_is_tried_again(self) -> None:
        (self.share / "vilna.flatpakrepo").write_text("[Flatpak Repo]\n")
        self.fail.write_text("--or-update vilna")
        self.assertEqual(self.run_script().returncode, 75)
        self.assertFalse((self.state / "flatpak-defaults-vilna.done").exists())
        self.fail.unlink()
        self.log.unlink()
        self.assertEqual(self.run_script().returncode, 0)
        self.assertTrue(all(" flathub" not in c for c in self.calls()))
        self.assertTrue((self.state / "flatpak-defaults-all.done").exists())

    def test_offline_is_not_a_failure(self) -> None:
        # Build15.6 VM: offline it must neither fail the unit nor call flatpak;
        # 75 makes systemd try again later (SuccessExitStatus, RestartForceExitStatus).
        (self.share / "vilna.flatpakrepo").write_text("[Flatpak Repo]\n")
        self.offline.touch()
        r = self.run_script()
        self.assertEqual(r.returncode, 75)
        self.assertIn("offline", r.stdout)
        self.assertEqual(self.calls(), [])
        self.offline.unlink()
        self.assertEqual(self.run_script().returncode, 0)
        self.assertTrue((self.state / "flatpak-defaults-all.done").exists())

    def test_catalogue_fetched_after_remote_add(self) -> None:
        # flatpak install does not fetch the remote's AppStream: without it the
        # store did not show Tinta Office until GNOME Software refreshed.
        (self.share / "vilna.flatpakrepo").write_text("[Flatpak Repo]\n")
        self.assertEqual(self.run_script().returncode, 0)
        calls = self.calls()
        for remote in ("flathub", "vilna"):
            added = next(
                i for i, c in enumerate(calls) if c.startswith("remote-add") and f" {remote} " in c
            )
            fetch = f"update --system --noninteractive --appstream {remote}"
            self.assertIn(fetch, calls[added:])

    def test_catalogue_failure_does_not_hold_the_apps(self) -> None:
        (self.share / "vilna.flatpakrepo").write_text("[Flatpak Repo]\n")
        self.fail.write_text("--appstream vilna")
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("the store fetches it later", r.stdout)
        self.assertTrue(any(c.startswith("install") and " vilna " in c for c in self.calls()))
        self.assertTrue((self.state / "flatpak-defaults-all.done").exists())

    def test_old_stamp_means_flathub_was_done(self) -> None:
        # A computer from before Tinta: only Tinta is installed now.
        self.state.mkdir()
        (self.state / "flatpak-defaults.done").touch()
        (self.share / "vilna.flatpakrepo").write_text("[Flatpak Repo]\n")
        self.assertEqual(self.run_script().returncode, 0)
        self.assertTrue(all("flathub" not in c for c in self.calls()))
        self.assertTrue(any(c.startswith("install") and " vilna " in c for c in self.calls()))


class Catalogue(unittest.TestCase):
    """What GNOME Software needs to show a Tinta app and install it again."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.bundles = tb.load()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, skip: str = "", drop: str = "", icons: bool = True) -> None:
        parts = ['<components version="0.8" origin="flatpak">']
        for b in self.bundles:
            app = b["id"]
            if app == skip:
                continue
            fields = {
                "name": f"<name>{b['name']}</name>",
                "summary": "<summary>Synthetic summary</summary>",
                "launchable": f'<launchable type="desktop-id">{app}.desktop</launchable>',
                "icon": f'<icon type="cached" width="64" height="64">{app}.png</icon>',
            }
            if app == drop.split(":")[0]:
                fields.pop(drop.split(":")[1])
            body = "".join(fields.values())
            parts.append(f'<component type="desktop-application"><id>{app}</id>{body}</component>')
            if icons:
                for size in tb.ICON_SIZES:
                    (self.dir / "icons" / size).mkdir(parents=True, exist_ok=True)
                    (self.dir / "icons" / size / f"{app}.png").write_bytes(b"png")
        parts.append("</components>")
        with gzip.open(self.dir / "appstream.xml.gz", "wt", encoding="utf-8") as f:
            f.write("".join(parts))

    def problems(self) -> list[str]:
        return tb.in_catalogue(self.bundles, tb.read_catalogue(self.dir), self.dir)

    def test_all_apps_shown(self) -> None:
        self.write()
        self.assertEqual(self.problems(), [])
        self.assertEqual(tb.main(["catalogue", str(self.dir)]), 0)

    def test_missing_app(self) -> None:
        self.write(skip="io.github.stdenis1980.tintapdf")
        problems = self.problems()
        self.assertEqual(len(problems), 1)
        self.assertIn("tintapdf", problems[0])
        self.assertEqual(tb.main(["catalogue", str(self.dir)]), 1)

    def test_no_icon_files(self) -> None:
        self.write(icons=False)
        self.assertTrue(any("icon" in p for p in self.problems()))

    def test_no_launcher_or_summary(self) -> None:
        for field in ("launchable", "summary", "icon", "name"):
            with self.subTest(field=field):
                self.write(drop=f"io.github.stdenis1980.ibercorreo:{field}")
                self.assertTrue(any("ibercorreo" in p for p in self.problems()), field)

    def test_not_fetched(self) -> None:
        self.assertEqual(tb.main(["catalogue", str(self.dir)]), 2)


class Unit(unittest.TestCase):
    """The service must not hold the boot (Build15.6 VM: 1 min 20 s)."""

    def test_does_not_hold_the_boot(self) -> None:
        import configparser

        unit = configparser.ConfigParser(interpolation=None, strict=False)
        unit.optionxform = str
        unit.read(DEFAULTS / "vilna-flatpak-defaults.service", encoding="utf-8")
        self.assertNotEqual(unit["Service"].get("Type"), "oneshot")
        self.assertNotIn("network-online.target", unit["Unit"].get("After", ""))
        self.assertNotIn("network-online.target", unit["Unit"].get("Wants", ""))

    def test_tempfail_is_retried_not_failed(self) -> None:
        text = (DEFAULTS / "vilna-flatpak-defaults.service").read_text(encoding="utf-8")
        self.assertIn("SuccessExitStatus=75", text)
        self.assertIn("RestartForceExitStatus=75", text)
        self.assertIn("TEMPFAIL=75", SCRIPT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
