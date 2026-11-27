#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/report_drivers.py (driver and firmware packages of the KIWI images).

Every test writes small images and a patterns spec into a temporary folder
and reads them the way ``main()`` does: the classification rules, the spec
and image reading, sub-patterns, the table and that ``--check`` catches a
driver or firmware package that is in Vilna but not in Vilna-Live.

Run: python3 tools/test_report_drivers.py
"""

from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import report_drivers as rd  # noqa: E402  (after sys.path.insert above)

SPEC = rd.PATTERNS_SPEC
LIVE_KIWI = "obs/images/Vilna-Live/Vilna-Live.kiwi"
VILNA_KIWI = "obs/images/Vilna/Vilna.kiwi"
TIK_KIWI = "obs/images/tik-osimage-Vilna/tik-osimage-Vilna.kiwi"

GOOD_SPEC = """\
%define base_name patterns-vilna
Name:           %{base_name}
Version:        0.1
Summary:        Patterns
License:        GPL-3.0-or-later

%description
Requires:       not-a-tag-in-the-description

%package base
Summary:        Base
Provides:       pattern() = vilna_base
Requires:       pattern() = vilna_hw
Requires:       kernel-firmware-all
Requires:       NetworkManager-branding-Vilna
Requires:       gnome-shell >= 49, nautilus
Requires(post): coreutils
Requires:       /usr/bin/gzip
Requires:       user(nobody)
Recommends:     fwupd

%description base
Base.

%package -n patterns-vilna-hw
Summary:        Hardware
Provides:       pattern() = vilna_hw
Requires:       ucode-intel
Requires:       sof-firmware

%description -n patterns-vilna-hw
Hardware.

%files base
%files -n patterns-vilna-hw

%changelog
"""


def kiwi(packages: str = "", collections: str = '<namedCollection name="vilna_base"/>') -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<image schemaversion="7.6" name="X">\n'
        f'  <packages type="image">{collections}{packages}</packages>\n'
        '  <packages type="bootstrap"><package name="filesystem"/></packages>\n'
        "</image>\n"
    )


def args(*argv: str) -> object:
    return rd.parse_args(list(argv))


class Repo:
    """A temporary repository with the three images and the patterns spec."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: list[str] = []

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        if path not in self.files:
            self.files.append(path)

    def good(self, live_extra: str = "", vilna_extra: str = "") -> Repo:
        self.write(SPEC, GOOD_SPEC)
        self.write(LIVE_KIWI, kiwi('<package name="kernel-default"/>' + live_extra))
        self.write(VILNA_KIWI, kiwi('<package name="kernel-default"/>' + vilna_extra))
        self.write(TIK_KIWI, kiwi('<package name="kernel-default"/>'))
        return self

    def run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rd.run(self.root, sorted(self.files), rd.parse_args(list(argv)))
        return code, out.getvalue(), err.getvalue()


class TempRepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repo(Path(self.tmp.name))


class ClassifyTest(unittest.TestCase):
    def test_every_class_has_a_matching_package(self) -> None:
        examples = {
            "kernel-firmware-iwlwifi": "firmware",
            "kernel-firmware-all": "firmware",
            "ucode-amd": "microcode",
            "sof-firmware": "audio",
            "alsa-ucm-conf": "audio",
            "pipewire-alsa": "audio",
            "wireplumber": "audio",
            "bluez-firmware": "bluetooth",
            "bluez-cups": "bluetooth",
            "bcm43xx-firmware": "firmware",
            "Mesa-dri": "graphics",
            "libvulkan_nouveau": "graphics",
            "xf86-video-amdgpu": "graphics",
            "nvidia-open-driver-G06-signed-kmp-default": "graphics",
            "xf86-input-libinput": "input",
            "libinput-udev": "input",
            "libwacom-data": "input",
            "iio-sensor-proxy": "sensors",
            "sane-backends": "scanning",
            "sane-airscan": "scanning",
            "hplip-sane": "scanning",
            "cups-filters": "printing",
            "printer-driver-brlaser": "printing",
            "epson-inkjet-printer-escpr": "printing",
            "hplip-hpijs": "printing",
            "OpenPrintingPPDs-postscript": "printing",
            "ipp-usb": "printing",
            "NetworkManager-wifi": "network",
            "ModemManager": "network",
            "libmbim": "network",
            "fprintd-pam": "fingerprint",
            "libfprint-2-2": "fingerprint",
            "bolt": "thunderbolt",
            "fwupd": "firmware-update",
            "kernel-default": "kernel",
            "framework-laptop-kmp-default": "kernel",
        }
        for name, klass in examples.items():
            with self.subTest(name=name):
                self.assertEqual(rd.classify(name), klass)
        self.assertEqual(set(examples.values()), set(rd.CLASSES))

    def test_branding_translations_and_unknown_are_other(self) -> None:
        for name in ("NetworkManager-branding-Vilna", "cups-devel", "gnome-shell", "bolt-lang"):
            with self.subTest(name=name):
                self.assertEqual(rd.classify(name), rd.OTHER)

    def test_prefix_alone_is_not_enough(self) -> None:
        for name in ("boltzmann", "cupsd-tools-x", "kernel-firmware"):
            with self.subTest(name=name):
                expected = "firmware" if name == "kernel-firmware" else rd.OTHER
                self.assertEqual(rd.classify(name), expected)


class SpecTest(unittest.TestCase):
    def test_dependency_names(self) -> None:
        self.assertEqual(rd.dependency_names("a >= 1.0, b"), ["a", "b"])
        self.assertEqual(rd.dependency_names("a b"), ["a", "b"])
        self.assertEqual(rd.dependency_names("/usr/bin/gzip"), [])
        self.assertEqual(rd.dependency_names("user(nobody)"), [])
        self.assertEqual(rd.dependency_names("(a if b)"), [])
        self.assertEqual(rd.dependency_names("%{undefined}-x"), [])

    def test_packages_patterns_and_macros(self) -> None:
        specs = rd.parse_spec(GOOD_SPEC)
        self.assertEqual(set(specs), {"patterns-vilna", "patterns-vilna-base", "patterns-vilna-hw"})
        base = specs["patterns-vilna-base"]
        self.assertEqual(base.patterns, ["vilna_base"])
        self.assertEqual(base.subpatterns, ["vilna_hw"])
        self.assertEqual(
            base.packages,
            [
                "kernel-firmware-all",
                "NetworkManager-branding-Vilna",
                "gnome-shell",
                "nautilus",
                "coreutils",
                "fwupd",
            ],
        )
        self.assertEqual(specs["patterns-vilna-hw"].patterns, ["vilna_hw"])

    def test_description_text_is_not_read(self) -> None:
        specs = rd.parse_spec(GOOD_SPEC)
        every = {p for pkg in specs.values() for p in pkg.packages}
        self.assertNotIn("not-a-tag-in-the-description", every)


class KiwiTest(unittest.TestCase):
    def test_sections(self) -> None:
        text = (
            '<image name="A"><packages type="image"><namedCollection name="c1"/>'
            '<collection name="c2"/><package name="a"/><ignore name="b"/></packages>'
            '<packages type="iso"><package name="c"/></packages>'
            '<packages type="bootstrap"><package name="d"/></packages>'
            '<packages type="delete"><package name="e"/></packages>'
            '<packages type="uninstall"><package name="f"/></packages></image>'
        )
        image, problems = rd.parse_kiwi("obs/images/A/A.kiwi", text)
        self.assertEqual(problems, [])
        assert image is not None
        self.assertEqual(image.name, "A")
        self.assertEqual(image.packages, ["a", "c", "d"])
        self.assertEqual(image.collections, ["c1", "c2"])
        self.assertEqual(image.removed, ["b", "e", "f"])

    def test_bad_xml_and_root(self) -> None:
        image, problems = rd.parse_kiwi("x.kiwi", "<image><packages></image>")
        self.assertIsNone(image)
        self.assertIn("x.kiwi: not well-formed XML", str(problems[0]))
        image, problems = rd.parse_kiwi("x.kiwi", "<other/>")
        self.assertIsNone(image)
        self.assertIn("root element is <other>", str(problems[0]))

    def test_kiwi_paths(self) -> None:
        files = [
            "obs/images/A/A.kiwi",
            "obs/images/A/config.sh",
            "obs/images/A/sub/B.kiwi",
            "obs/packages/x/x.kiwi",
            "image/A.kiwi",
        ]
        self.assertEqual(rd.kiwi_paths(files), ["obs/images/A/A.kiwi"])


class PatternTest(unittest.TestCase):
    def test_subpatterns_and_removal(self) -> None:
        specs = rd.parse_spec(GOOD_SPEC)
        image, _ = rd.parse_kiwi(
            "obs/images/A/A.kiwi",
            kiwi('<package name="x"/></packages><packages type="delete"><package name="fwupd"/>'),
        )
        assert image is not None
        packages, problems = rd.image_packages(image, specs)
        self.assertEqual(problems, [])
        self.assertTrue({"ucode-intel", "sof-firmware", "kernel-firmware-all", "x"} <= packages)
        self.assertIn("patterns-vilna-hw", packages)
        self.assertNotIn("fwupd", packages)

    def test_pattern_package_named_directly(self) -> None:
        specs = rd.parse_spec(GOOD_SPEC)
        image, _ = rd.parse_kiwi(
            "obs/images/A/A.kiwi", kiwi('<package name="patterns-vilna-hw"/>', collections="")
        )
        assert image is not None
        packages, _ = rd.image_packages(image, specs)
        self.assertEqual(
            packages, {"patterns-vilna-hw", "ucode-intel", "sof-firmware", "filesystem"}
        )

    def test_pattern_package_required_by_name(self) -> None:
        spec = (
            "Name: p\n%package a\nProvides: pattern() = a\nRequires: p-b\n"
            "%package b\nRequires: ucode-amd\n"
        )
        image, _ = rd.parse_kiwi(
            "obs/images/A/A.kiwi", kiwi(collections='<namedCollection name="a"/>')
        )
        assert image is not None
        packages, _ = rd.image_packages(image, rd.parse_spec(spec))
        self.assertEqual(packages, {"p-a", "p-b", "ucode-amd", "filesystem"})

    def test_cycle_ends(self) -> None:
        spec = (
            "Name: p\n%package a\nProvides: pattern() = a\nRequires: pattern() = b\n"
            "Requires: one\n%package b\nProvides: pattern() = b\nRequires: pattern() = a\n"
            "Requires: two\n"
        )
        image, _ = rd.parse_kiwi(
            "obs/images/A/A.kiwi", kiwi(collections='<namedCollection name="a"/>')
        )
        assert image is not None
        packages, _ = rd.image_packages(image, rd.parse_spec(spec))
        self.assertEqual(packages, {"p-a", "p-b", "one", "two", "filesystem"})

    def test_unknown_collection_is_a_note(self) -> None:
        image, _ = rd.parse_kiwi(
            "obs/images/A/A.kiwi", kiwi(collections='<namedCollection name="aeon_base"/>')
        )
        assert image is not None
        packages, problems = rd.image_packages(image, rd.parse_spec(GOOD_SPEC))
        self.assertEqual(packages, {"filesystem"})
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].note)
        self.assertIn("collection aeon_base is not a pattern", str(problems[0]))


class CheckTest(unittest.TestCase):
    IMAGES = {
        "Vilna-Live": {"kernel-default", "vilna-installer"},
        "Vilna": {"kernel-default", "kernel-firmware-all", "dracut-kiwi-oem-repart"},
    }
    PATHS = {"Vilna-Live": LIVE_KIWI, "Vilna": VILNA_KIWI}

    def test_missing_driver_is_an_error_other_is_not(self) -> None:
        problems = rd.check_live(self.IMAGES, self.PATHS, "Vilna-Live", ["Vilna"])
        self.assertEqual(
            [str(p) for p in problems],
            [f"{LIVE_KIWI}: kernel-firmware-all (firmware) is in Vilna but not in Vilna-Live"],
        )

    def test_allowed_gap_is_a_note(self) -> None:
        problems = rd.check_live(
            self.IMAGES, self.PATHS, "Vilna-Live", ["Vilna"], ["kernel-firmware-all"]
        )
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].note)

    def test_extra_in_live_is_fine(self) -> None:
        images = {"Vilna-Live": {"kernel-firmware-all", "ucode-amd"}, "Vilna": {"ucode-amd"}}
        self.assertEqual(rd.check_live(images, self.PATHS, "Vilna-Live", ["Vilna"]), [])

    def test_table(self) -> None:
        lines = rd.table(self.IMAGES)
        self.assertEqual(lines[0], "| class | Vilna-Live | Vilna |")
        self.assertIn("| firmware | 0 | 1 |", lines)
        self.assertIn("| kernel | 1 | 1 |", lines)
        self.assertIn("| other | 1 | 1 |", lines)
        self.assertIn("| firmware | kernel-firmware-all | - | x |", lines)
        self.assertNotIn("dracut-kiwi-oem-repart", "\n".join(lines))


class RunTest(TempRepoCase):
    def test_good_repo_report(self) -> None:
        code, out, err = self.repo.good().run()
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("| class | Vilna-Live | Vilna | tik-osimage-Vilna |"))
        self.assertIn("| microcode | ucode-intel | x | x | x |", out)
        self.assertIn("| firmware-update | fwupd | x | x | x |", out)

    def test_good_repo_check(self) -> None:
        code, out, err = self.repo.good().run("--check")
        self.assertEqual(code, 0, err)
        self.assertIn("0 driver or firmware packages missing", out)

    def test_check_catches_vilna_only_driver(self) -> None:
        repo = self.repo.good(vilna_extra='<package name="libvulkan_nouveau"/>')
        code, _, err = repo.run("--check")
        self.assertEqual(code, 1)
        self.assertIn(
            f"{LIVE_KIWI}: libvulkan_nouveau (graphics) is in Vilna but not in Vilna-Live", err
        )

    def test_check_catches_live_delete(self) -> None:
        delete = '</packages><packages type="delete"><package name="ucode-intel"/>'
        code, _, err = self.repo.good(live_extra=delete).run("--check")
        self.assertEqual(code, 1)
        self.assertIn("ucode-intel (microcode) is in Vilna but not in Vilna-Live", err)
        self.assertIn("ucode-intel (microcode) is in tik-osimage-Vilna", err)

    def test_check_allow_missing(self) -> None:
        delete = '</packages><packages type="delete"><package name="ucode-intel"/>'
        code, _, err = self.repo.good(live_extra=delete).run(
            "--check", "--allow-missing", "ucode-intel"
        )
        self.assertEqual(code, 0, err)
        self.assertIn("note: ucode-intel (microcode) is in Vilna but not in Vilna-Live", err)

    def test_check_unknown_other_package_passes(self) -> None:
        code, _, err = self.repo.good(vilna_extra='<package name="some-new-tool"/>').run("--check")
        self.assertEqual(code, 0, err)

    def test_installed_option(self) -> None:
        repo = self.repo.good(vilna_extra='<package name="bolt"/>')
        code, _, _ = repo.run("--check", "--installed", "tik-osimage-Vilna")
        self.assertEqual(code, 0)

    def test_unknown_image_is_a_wrong_call(self) -> None:
        code, _, err = self.repo.good().run("--check", "--live", "Nope")
        self.assertEqual(code, 2)
        self.assertIn("no such image in obs/images: Nope", err)

    def test_no_images(self) -> None:
        self.repo.write(SPEC, GOOD_SPEC)
        code, _, err = self.repo.run()
        self.assertEqual(code, 2)
        self.assertIn("no tracked *.kiwi files", err)

    def test_broken_kiwi_is_an_error(self) -> None:
        repo = self.repo.good()
        repo.write("obs/images/Broken/Broken.kiwi", "<image>")
        code, _, err = repo.run()
        self.assertEqual(code, 1)
        self.assertIn("obs/images/Broken/Broken.kiwi: not well-formed XML", err)

    def test_untracked_spec_is_a_note(self) -> None:
        repo = self.repo.good()
        repo.files.remove(SPEC)
        code, _, err = repo.run("--check")
        self.assertEqual(code, 0, err)
        self.assertIn(f"{SPEC}: note: not tracked", err)


class MainTest(unittest.TestCase):
    def test_wrong_call(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rd.main(["--nope"]), 2)

    def test_help(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rd.main(["--help"]), 0)

    @unittest.skipIf(shutil.which("git") is None, "git is not installed")
    def test_tracked_files_uses_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repo(root).good()
            (root / "obs/images/Vilna/untracked.kiwi").write_text("<image/>", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", *repo.files], cwd=root, check=True)
            self.assertEqual(rd.tracked_files(root), sorted(repo.files))


if __name__ == "__main__":
    unittest.main()
