#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/sbom_image.py (the SPDX 2.3 SBOM of the openSUSE packages in the images).

Each test builds a small temporary repository with fake KIWI files, specs and packages.yaml.

Run: python3 tools/test_sbom_image.py
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import sbom_image as si  # noqa: E402  (after sys.path.insert above)

CREATED = "2026-12-02T09:00:00Z"

PATTERN_SPEC = """\
# SPDX-License-Identifier: GPL-3.0-or-later
Name:           patterns-demo
Version:        0.1
Release:        0
License:        GPL-3.0-or-later

%description
The patterns.

%package base
Summary:        Base
Provides:       pattern() = demo_base
Provides:       pattern-visible()
Requires:       Demo-release
Requires:       glibc
Requires:       bash >= 5.0
Requires:       curl, wget
Requires:       /usr/bin/gzip
Requires:       pkgconfig(glib-2.0)
Requires:       libstdc++6
Requires:       llama.cpp
Requires:       demo-app
Requires:       proton-vpn
Requires:       gnome-branding-Vilna
Requires:       (rich-one if rich-two)
Requires(post): coreutils
Recommends:     recommended-tool
Suggests:       suggested-tool

%files base
%dir %{_docdir}/patterns-demo
"""

APP_SPEC = """\
Name:           demo-app
Version:        1.2
Release:        0
License:        Apache-2.0
Requires:       python311

%package -n demo-app-extra
Summary:        Extra

%package -n gnome-branding-Vilna
Summary:        Branding

%package lang
Summary:        Lang
Requires:       gettext-runtime

%description
Text.
"""

YAML = """\
# comment
base:
  pattern: aeon_base
  packages:
    - {name: not-a-row, license: MIT, status: upstream}

vilna:
  pattern: demo_base
  packages:
    - {name: patterns-demo,   license: GPL-3.0-or-later, status: obs}
    - {name: demo-app,        license: Apache-2.0,       status: obs}   # our own
    - {name: proton-vpn,      license: GPL-3.0-or-later, status: upstream} # from openSUSE
    - {name: llama.cpp,       license: MIT,              status: upstream}
    - {name: xdg-dbus-proxy,  license: LGPL-2.1-or-later, status: upstream}
    - {name: demo-future,     license: MIT,              status: planned}
  enable_services: [a.service]

flatpak_defaults:
  - org.example.App
"""

KIWI = """\
<?xml version="1.0" encoding="utf-8"?>
<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<image schemaversion="7.6" name="{name}">
  <description type="system"><author>x</author></description>
  <preferences>
    <version>{version}</version>
    <type image="oem" format="qcow2"/>
  </preferences>
  <repository type="rpm-md"><source path="obsrepositories:/"/></repository>
  <packages type="image">
{image}
  </packages>
  <packages type="bootstrap">
{bootstrap}
  </packages>
  <packages type="delete">
    <package name="removed-package"/>
  </packages>
</image>
"""


def kiwi(name: str, image: str, bootstrap: str = "", version: str = "0.0.1") -> str:
    return KIWI.format(name=name, version=version, image=image, bootstrap=bootstrap)


DEMO_IMAGE = """\
    <namedCollection name="demo_base"/>
    <package name="kernel-default"/>
    <package name="glibc"/>
"""
DEMO_BOOTSTRAP = """\
    <package name="filesystem"/>
    <package name="Demo-release"/>
"""
LIVE_IMAGE = """\
    <namedCollection name="demo_base"/>
    <package name="demo-installer"/>
    <package name="Demo"/>
    <package name="MozillaFirefox"/>
"""


def repo_files() -> dict[str, str]:
    return {
        "image/packages.yaml": YAML,
        "obs/packages/patterns-demo/patterns-demo.spec": PATTERN_SPEC,
        "obs/packages/demo-app/demo-app.spec": APP_SPEC,
        "obs/packages/Demo-release/Demo-release.spec": "Name: Demo-release\nVersion: 1\n",
        "obs/packages/demo-installer/demo-installer.spec": "Name: demo-installer\nVersion: 1\n",
        "obs/images/Demo/Demo.kiwi": kiwi("Demo", DEMO_IMAGE, DEMO_BOOTSTRAP),
        "obs/images/Demo-Live/Demo-Live.kiwi": kiwi("Demo-Live", LIVE_IMAGE),
    }


def write(root: Path, files: dict[str, str]) -> list[str]:
    """Write ``files`` under ``root``; return their sorted paths (like ``git ls-files``)."""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return sorted(files)


class Repo:
    """A temporary repository; ``build()`` runs the generator on it."""

    def __init__(self, test: unittest.TestCase, files: dict[str, str] | None = None) -> None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.files = write(self.root, files if files is not None else repo_files())

    def build(self, versions: dict[str, str] | None = None) -> tuple[dict, list[si.Problem]]:
        return si.build(self.root, self.files, "0.1", CREATED, versions)


def texts(problems: list[si.Problem], warnings: bool | None = None) -> list[str]:
    return [str(p) for p in problems if warnings is None or p.warning == warnings]


def by_name(doc: dict, name: str) -> dict:
    found = [p for p in doc["packages"] if p["name"] == name]
    assert len(found) == 1, f"{name}: {len(found)} packages"
    return found[0]


def names(doc: dict, kind: str) -> list[str]:
    return [p["name"] for p in doc["packages"] if p["SPDXID"].startswith(f"SPDXRef-{kind}-")]


def contains(doc: dict, image: str) -> set[str]:
    ident = by_name(doc, image)["SPDXID"]
    pkgs = {p["SPDXID"]: p["name"] for p in doc["packages"]}
    return {
        pkgs[r["relatedSpdxElement"]]
        for r in doc["relationships"]
        if r["spdxElementId"] == ident and r["relationshipType"] == "CONTAINS"
    }


def run_main(args: list[str], files: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with (
        mock.patch.object(si, "tracked_files", return_value=files),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        code = si.main(args)
    return code, out.getvalue(), err.getvalue()


class HelperTests(unittest.TestCase):
    def test_license_expression(self) -> None:
        for good in ("MIT", "GPL-3.0-or-later", "Apache-2.0 OR MIT", "(MIT AND BSD-2-Clause)"):
            self.assertEqual(si.license_expression(good), good)
        for bad in (
            "",
            None,
            "MIT OR",
            "AND MIT",
            "MIT MIT",
            "(MIT",
            "MIT)",
            "()",
            "a b c",
            "x" * 300,
        ):
            self.assertIsNone(si.license_expression(bad), bad)

    def test_header_license(self) -> None:
        self.assertEqual(si.header_license("<!-- SPDX-License-Identifier: MIT -->\n"), "MIT")
        self.assertEqual(
            si.header_license("# SPDX-License-Identifier: GPL-3.0-or-later\n"), "GPL-3.0-or-later"
        )
        self.assertIsNone(si.header_license("nothing here"))
        self.assertIsNone(si.header_license("\n" * 40 + "# SPDX-License-Identifier: MIT\n"))

    def test_expand(self) -> None:
        macros = {"name": "foo", "v": "%{name}-1"}
        self.assertEqual(si.expand("%{name}-lang", macros), "foo-lang")
        self.assertEqual(si.expand("%name", macros), "foo")
        self.assertEqual(si.expand("%{v}", macros), "foo-1")
        self.assertEqual(si.expand("%{?name}", macros), "foo")
        self.assertEqual(si.expand("%{other}", macros), "%{other}")
        # a macro that expands to itself ends instead of looping
        self.assertEqual(si.expand("%{a}", {"a": "x%{a}"}).startswith("x"), True)

    def test_sanitize(self) -> None:
        self.assertEqual(si.sanitize("libstdc++6"), "libstdc-6")
        self.assertEqual(si.sanitize("++"), "x")
        self.assertEqual(si.sanitize("a_b c"), "a-b-c")


class RequirementTests(unittest.TestCase):
    def names(self, value: str) -> tuple[list[str], list[str]]:
        problems: list[si.Problem] = []
        return si.requirement_names(value, "spec:1", problems), texts(problems)

    def test_plain_and_versions(self) -> None:
        self.assertEqual(self.names("glibc"), (["glibc"], []))
        self.assertEqual(self.names("bash >= 5.0")[0], ["bash"])
        self.assertEqual(self.names("bash = 5 curl")[0], ["bash", "curl"])
        self.assertEqual(self.names("a, b <= 2, c")[0], ["a", "b", "c"])
        self.assertEqual(self.names("a == 1")[0], ["a"])

    def test_not_package_names(self) -> None:
        self.assertEqual(self.names("/usr/bin/gzip"), ([], []))
        self.assertEqual(self.names("pkgconfig(glib-2.0)"), ([], []))
        self.assertEqual(self.names("libfoo.so.1()(64bit)"), ([], []))
        self.assertEqual(self.names("a pkgconfig(x) b")[0], ["a", "b"])

    def test_warned_and_skipped(self) -> None:
        names, problems = self.names("(a if b)")
        self.assertEqual(names, [])
        self.assertIn("rich dependency", problems[0])
        names, problems = self.names("%{unknown}")
        self.assertEqual(names, [])
        self.assertIn("macro not expanded", problems[0])
        names, problems = self.names("good bad;name 'x'")
        self.assertEqual(names, ["good"])
        self.assertEqual(len(problems), 2)
        self.assertTrue(all("warning" in p for p in problems))

    def test_hostile_values(self) -> None:
        for value in ("a\nb", "a\x00b", "../../etc/passwd", "-rf", "a" * 100000 + "!"):
            names, _ = self.names(value)
            for name in names:
                self.assertRegex(name, si.RPM_NAME_RE)


class SpecTests(unittest.TestCase):
    def parse(self, text: str) -> tuple[si.Spec | None, list[si.Problem]]:
        problems: list[si.Problem] = []
        return si.parse_spec("x.spec", text, problems), problems

    def test_names_and_sections(self) -> None:
        spec, problems = self.parse(APP_SPEC)
        self.assertEqual(problems, [])
        assert spec
        self.assertEqual(spec.name, "demo-app")
        self.assertEqual(
            [s.name for s in spec.sections],
            ["demo-app", "demo-app-extra", "gnome-branding-Vilna", "demo-app-lang"],
        )
        self.assertEqual([v for v, _ in spec.sections[3].requires], ["gettext-runtime"])

    def test_macros_in_names(self) -> None:
        text = (
            "%global base foo\nName: %{base}-core\nVersion: 1\n%package -n %{name}-x\nSummary: s\n"
        )
        spec, _ = self.parse(text)
        assert spec
        self.assertEqual(spec.name, "foo-core")
        self.assertEqual(spec.sections[1].name, "foo-core-x")

    def test_tags_outside_preambles_are_not_read(self) -> None:
        text = "Name: a\n%description\nRequires: nothing\n%install\nProvides: pattern() = no\n"
        spec, _ = self.parse(text)
        assert spec
        self.assertEqual(spec.sections[0].requires, [])
        self.assertEqual(spec.sections[0].patterns, [])

    def test_pattern_sections(self) -> None:
        spec, _ = self.parse(PATTERN_SPEC)
        assert spec
        base = spec.sections[1]
        self.assertEqual(base.name, "patterns-demo-base")
        self.assertEqual(base.patterns, ["demo_base"])
        self.assertIn(("coreutils", "x.spec:26"), [(v, w) for v, w in base.requires])

    def test_no_name_and_nameless_package(self) -> None:
        spec, problems = self.parse("Version: 1\n")
        self.assertIsNone(spec)
        self.assertEqual(texts(problems), ["x.spec: no Name tag"])
        _, problems = self.parse("Name: a\n%package\n")
        self.assertIn("%package without a name", texts(problems)[0])

    def test_comments_and_case(self) -> None:
        spec, _ = self.parse("name: a\n# Requires: hidden\nrequires: seen\n")
        assert spec
        self.assertEqual([v for v, _ in spec.sections[0].requires], ["seen"])


class YamlTests(unittest.TestCase):
    def test_rows(self) -> None:
        problems: list[si.Problem] = []
        rows = si.parse_yaml_rows("image/packages.yaml", YAML, problems)
        self.assertEqual(problems, [])
        self.assertEqual(
            [(r.name, r.status) for r in rows],
            [
                ("patterns-demo", "obs"),
                ("demo-app", "obs"),
                ("proton-vpn", "upstream"),
                ("llama.cpp", "upstream"),
                ("xdg-dbus-proxy", "upstream"),
                ("demo-future", "planned"),
            ],
        )
        self.assertEqual(rows[2].license, "GPL-3.0-or-later")

    def test_no_rows_and_nameless_row(self) -> None:
        problems: list[si.Problem] = []
        self.assertEqual(si.parse_yaml_rows("p.yaml", "vilna:\n  pattern: x\n", problems), [])
        self.assertTrue(problems[0].warning)
        problems = []
        rows = si.parse_yaml_rows("p.yaml", "vilna:\n  packages:\n    - {license: MIT}\n", problems)
        self.assertEqual(rows, [])
        self.assertIn("a package row without a name", texts(problems, False)[0])


class KiwiTests(unittest.TestCase):
    def parse(
        self, text: str, path: str = "obs/images/Demo/Demo.kiwi"
    ) -> tuple[si.Image | None, list[si.Problem]]:
        problems: list[si.Problem] = []
        return si.parse_kiwi(path, text, problems), problems

    def test_image(self) -> None:
        image, problems = self.parse(kiwi("Demo", DEMO_IMAGE, DEMO_BOOTSTRAP, "0.3"))
        self.assertEqual(problems, [])
        assert image
        self.assertEqual(
            (image.name, image.version, image.declared), ("Demo", "0.3", "GPL-3.0-or-later")
        )
        self.assertEqual(
            image.packages,
            {
                "kernel-default": {"kiwi image"},
                "glibc": {"kiwi image"},
                "filesystem": {"kiwi bootstrap"},
                "Demo-release": {"kiwi bootstrap"},
            },
        )
        self.assertEqual(image.collections, ["demo_base"])

    def test_delete_and_uninstall_lists_are_not_read(self) -> None:
        text = kiwi("Demo", "").replace(
            "</image>", '<packages type="uninstall"><package name="gone"/></packages></image>'
        )
        image, _ = self.parse(text)
        assert image
        self.assertEqual(image.packages, {})

    def test_bad_files(self) -> None:
        for text, fragment in (
            ("<image", "not well-formed XML"),
            ("<other/>", "not <image>"),
            (
                '<image name="a b"><preferences><version>1</version></preferences></image>',
                "image name",
            ),
            ('<image name="Demo"><preferences/></image>', "<version>"),
            (
                '<image name="Demo"><preferences><version>../1</version></preferences></image>',
                "<version>",
            ),
        ):
            image, problems = self.parse(text)
            self.assertIsNone(image, text)
            self.assertIn(fragment, texts(problems, False)[0])

    def test_warnings(self) -> None:
        image, problems = self.parse(kiwi("Other", ""), "obs/images/Demo/Demo.kiwi")
        assert image
        self.assertIn("differs from its folder", texts(problems, True)[0])
        text = kiwi("Demo", '<package name="x" profiles="a"/><package name="bad name"/>')
        text = text.replace("<!-- SPDX-License-Identifier: GPL-3.0-or-later -->", "")
        image, problems = self.parse(text)
        assert image
        self.assertEqual(image.declared, si.NOASSERTION)
        self.assertEqual(set(image.packages), {"x"})
        self.assertEqual(len(texts(problems, True)), 3)

    def test_entities_do_not_hang(self) -> None:
        levels = ['<!ENTITY a0 "xxxxxxxxxx">'] + [
            f'<!ENTITY a{n} "{f"&a{n - 1};" * 10}">' for n in range(1, 5)
        ]
        text = (
            f'<?xml version="1.0"?><!DOCTYPE image [{"".join(levels)}]>'
            + kiwi("Demo", "").split("?>", 1)[1]
        )
        image, problems = self.parse(text.replace('name="Demo"', 'name="Demo"'))
        self.assertTrue(image is not None or texts(problems, False))


class BuildTests(unittest.TestCase):
    def test_images_and_packages(self) -> None:
        doc, problems = Repo(self).build()
        self.assertEqual(texts(problems, False), [])
        self.assertEqual(names(doc, "Image"), ["Demo", "Demo-Live"])
        rpms = names(doc, "Rpm")
        self.assertEqual(rpms, sorted(rpms))
        self.assertEqual(
            set(rpms),
            {
                "kernel-default", "glibc", "filesystem", "bash", "curl", "wget", "libstdc++6",
                "llama.cpp", "proton-vpn", "coreutils", "MozillaFirefox",
            },
        )  # fmt: skip

    def test_our_packages_are_left_out(self) -> None:
        doc, problems = Repo(self).build()
        have = set(names(doc, "Rpm"))
        for ours in (
            "Demo-release", "demo-app", "demo-installer", "gnome-branding-Vilna",
            "removed-package", "patterns-demo", "demo-app-extra", "demo-app-lang",
        ):  # fmt: skip
            self.assertNotIn(ours, have)
        self.assertFalse(any("looks like ours" in p for p in texts(problems)))

    def test_look_alike_without_a_spec_is_warned(self) -> None:
        files = repo_files()
        files["obs/images/Demo/Demo.kiwi"] = kiwi("Demo", '<package name="vilna-ghost"/>')
        doc, problems = Repo(self, files).build()
        self.assertNotIn("vilna-ghost", names(doc, "Rpm"))
        self.assertTrue(any("vilna-ghost: looks like ours" in p for p in texts(problems, True)))

    def test_our_branding_suffix_and_planned_rows(self) -> None:
        files = repo_files()
        files["obs/images/Demo/Demo.kiwi"] = kiwi(
            "Demo", '<package name="theme-branding-Vilna"/><package name="demo-future"/>'
        )
        doc, problems = Repo(self, files).build()
        self.assertNotIn("theme-branding-Vilna", names(doc, "Rpm"))
        self.assertNotIn("demo-future", names(doc, "Rpm"))
        warned = texts(problems, True)
        self.assertTrue(any("theme-branding-Vilna: looks like ours" in p for p in warned))
        self.assertFalse(any("demo-future" in p for p in warned))

    def test_recommends_and_suggests_are_not_installed(self) -> None:
        doc, _ = Repo(self).build()
        self.assertEqual({"recommended-tool", "suggested-tool"} & set(names(doc, "Rpm")), set())

    def test_the_package_fields(self) -> None:
        doc, _ = Repo(self).build()
        pkg = by_name(doc, "curl")
        self.assertEqual(pkg["versionInfo"], "NOASSERTION")
        self.assertEqual(pkg["licenseConcluded"], "NOASSERTION")
        self.assertEqual(pkg["licenseDeclared"], "NOASSERTION")
        self.assertEqual(pkg["downloadLocation"], "NOASSERTION")
        self.assertEqual(pkg["externalRefs"][0]["referenceLocator"], "pkg:rpm/opensuse/curl")
        self.assertEqual(pkg["SPDXID"], "SPDXRef-Rpm-curl")
        self.assertNotIn("checksums", pkg)
        self.assertFalse(pkg["filesAnalyzed"])

    def test_purl_is_quoted(self) -> None:
        doc, _ = Repo(self).build()
        locator = by_name(doc, "libstdc++6")["externalRefs"][0]["referenceLocator"]
        self.assertEqual(locator, "pkg:rpm/opensuse/libstdc%2B%2B6")

    def test_spdx_ids_clash(self) -> None:
        files = repo_files()
        files["obs/images/Demo/Demo.kiwi"] = kiwi(
            "Demo", '<package name="lib+a"/><package name="lib-a"/><package name="lib_a"/>'
        )
        doc, problems = Repo(self, files).build()
        self.assertEqual(texts(problems + si.check_document(doc), False), [])
        ids = [p["SPDXID"] for p in doc["packages"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(by_name(doc, "lib+a")["SPDXID"], "SPDXRef-Rpm-lib-a")

    def test_relationships(self) -> None:
        doc, _ = Repo(self).build()
        demo = contains(doc, "Demo")
        live = contains(doc, "Demo-Live")
        self.assertIn("kernel-default", demo)
        self.assertNotIn(
            "kernel-default", live
        )  # Live does not list it itself, the pattern does not either
        self.assertIn("MozillaFirefox", live)
        self.assertNotIn("MozillaFirefox", demo)
        self.assertIn("bash", demo & live)  # through the pattern
        self.assertIn("Demo", live)  # the live ISO carries the image Demo
        described = [
            r["relatedSpdxElement"]
            for r in doc["relationships"]
            if r["relationshipType"] == "DESCRIBES"
        ]
        self.assertEqual(described, ["SPDXRef-Image-Demo", "SPDXRef-Image-Demo-Live"])
        self.assertTrue(
            all(
                r["spdxElementId"] == "SPDXRef-DOCUMENT"
                for r in doc["relationships"]
                if r["relationshipType"] == "DESCRIBES"
            )
        )

    def test_source_info_says_where_it_comes_from(self) -> None:
        doc, _ = Repo(self).build()
        self.assertEqual(
            by_name(doc, "glibc")["sourceInfo"],
            "installed by Demo: kiwi image, pattern demo_base; Demo-Live: pattern demo_base",
        )
        self.assertEqual(
            by_name(doc, "filesystem")["sourceInfo"], "installed by Demo: kiwi bootstrap"
        )

    def test_licence_from_packages_yaml_is_only_a_comment(self) -> None:
        doc, _ = Repo(self).build()
        pkg = by_name(doc, "proton-vpn")
        self.assertIn("GPL-3.0-or-later", pkg["comment"])
        self.assertEqual(pkg["licenseDeclared"], "NOASSERTION")
        self.assertNotIn("comment", by_name(doc, "curl"))

    def test_image_package(self) -> None:
        doc, _ = Repo(self).build()
        image = by_name(doc, "Demo")
        self.assertEqual(image["versionInfo"], "0.0.1")
        self.assertEqual(image["primaryPackagePurpose"], "OPERATING-SYSTEM")
        self.assertEqual(image["licenseDeclared"], "GPL-3.0-or-later")
        self.assertNotIn("externalRefs", image)

    def test_warnings_of_the_fake_repo(self) -> None:
        _, problems = Repo(self).build()
        warned = texts(problems, True)
        self.assertTrue(any("rich dependency" in p for p in warned))
        self.assertTrue(any("xdg-dbus-proxy: status upstream" in p for p in warned))
        self.assertFalse(any("llama.cpp: status upstream" in p for p in warned))
        self.assertTrue(any("of 11 packages have no version" in p for p in warned))

    def test_unknown_pattern_is_warned(self) -> None:
        files = repo_files()
        files["obs/images/Demo/Demo.kiwi"] = kiwi("Demo", '<namedCollection name="elsewhere"/>')
        doc, problems = Repo(self, files).build()
        self.assertTrue(
            any("pattern elsewhere is not provided" in p for p in texts(problems, True))
        )
        self.assertEqual(contains(doc, "Demo"), set())

    def test_unreadable_and_broken_files_are_errors(self) -> None:
        files = repo_files()
        files["obs/images/Demo/Demo.kiwi"] = "<image"
        doc, problems = Repo(self, files).build()
        self.assertTrue(any("not well-formed" in p for p in texts(problems, False)))
        repo = Repo(self)
        (repo.root / "image/packages.yaml").write_bytes(b"\xff\xfe")
        _, problems = repo.build()
        self.assertTrue(any("cannot read" in p for p in texts(problems, False)))
        (repo.root / "obs/images/Demo/Demo.kiwi").unlink()
        _, problems = repo.build()
        self.assertTrue(
            any("obs/images/Demo/Demo.kiwi: cannot read" in p for p in texts(problems, False))
        )

    def test_two_images_with_one_name(self) -> None:
        files = repo_files()
        files["obs/images/Copy/Copy.kiwi"] = kiwi("Demo", "")
        _, problems = Repo(self, files).build()
        self.assertTrue(any("Demo is described twice" in p for p in texts(problems, False)))

    def test_untracked_files_are_not_read(self) -> None:
        repo = Repo(self)
        write(repo.root, {"obs/images/Extra/Extra.kiwi": kiwi("Extra", '<package name="zzz"/>')})
        doc, _ = repo.build()
        self.assertNotIn("zzz", names(doc, "Rpm"))
        self.assertNotIn("Extra", names(doc, "Image"))

    def test_no_images(self) -> None:
        doc, problems = Repo(self, {"image/packages.yaml": YAML}).build()
        self.assertEqual(names(doc, "Rpm"), [])
        self.assertTrue(si.check_document(doc))
        self.assertEqual(texts(problems, False), [])

    def test_without_packages_yaml(self) -> None:
        files = repo_files()
        del files["image/packages.yaml"]
        doc, problems = Repo(self, files).build()
        self.assertIn("image/packages.yaml: warning: is not tracked", texts(problems, True))
        self.assertIn("curl", names(doc, "Rpm"))

    def test_a_package_is_a_pattern_requirement_of_a_second_spec(self) -> None:
        files = repo_files()
        files["obs/packages/more/more.spec"] = (
            "Name: more\nVersion: 1\n%package x\n"
            "Provides: pattern() = demo_base\nRequires: extra-tool\n"
        )
        doc, _ = Repo(self, files).build()
        self.assertIn("extra-tool", contains(doc, "Demo"))


class VersionTests(unittest.TestCase):
    def test_versions_fill_info_and_purl(self) -> None:
        doc, problems = Repo(self).build({"curl": "8.5.0-1.1", "libstdc++6": "14.2.0+git-1"})
        pkg = by_name(doc, "curl")
        self.assertEqual(pkg["versionInfo"], "8.5.0-1.1")
        self.assertEqual(
            pkg["externalRefs"][0]["referenceLocator"], "pkg:rpm/opensuse/curl@8.5.0-1.1"
        )
        self.assertEqual(
            by_name(doc, "libstdc++6")["externalRefs"][0]["referenceLocator"],
            "pkg:rpm/opensuse/libstdc%2B%2B6@14.2.0%2Bgit-1",
        )
        self.assertEqual(by_name(doc, "wget")["versionInfo"], "NOASSERTION")
        self.assertEqual(texts(problems + si.check_document(doc), False), [])
        self.assertTrue(any("of 11 packages have no version" in p for p in texts(problems, True)))

    def test_all_versions_known_and_extra_names(self) -> None:
        repo = Repo(self)
        doc, _ = repo.build()
        every = {n: "1-1" for n in names(doc, "Rpm")}
        every["transitive-lib"] = "2-1"
        doc2, problems = repo.build(every)
        self.assertFalse(any("no version" in p for p in texts(problems)))
        self.assertTrue(
            any("transitive-lib: no image installs it" in p for p in texts(problems, True))
        )
        self.assertNotIn("transitive-lib", names(doc2, "Rpm"))

    def test_load_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.json"
            path.write_text('{"curl": "1:8.5-1.1", "bash": "5.2^git1"}', encoding="utf-8")
            self.assertEqual(si.load_versions(str(path)), {"curl": "1:8.5-1.1", "bash": "5.2^git1"})
            for bad in (
                "[]", "{", '{"a b": "1"}', '{"curl": 8}', '{"curl": ""}', '{"curl": "1 2"}',
                '{"curl": "-1"}', '{"curl": "1\\n"}', '{"../x": "1"}',
            ):  # fmt: skip
                path.write_text(bad, encoding="utf-8")
                with self.assertRaises(ValueError, msg=bad):
                    si.load_versions(str(path))
            with self.assertRaises(ValueError):
                si.load_versions(str(Path(tmp) / "missing.json"))


class DeterminismTests(unittest.TestCase):
    def test_same_files_same_bytes(self) -> None:
        repo = Repo(self)
        first = si.render(repo.build()[0])
        self.assertEqual(first, si.render(repo.build()[0]))
        shuffled = si.build(repo.root, list(reversed(repo.files)), "0.1", CREATED)[0]
        self.assertEqual(first, si.render(shuffled))

    def test_namespace_ignores_created_but_not_content(self) -> None:
        repo = Repo(self)
        doc1, _ = repo.build()
        doc2, _ = si.build(repo.root, repo.files, "0.1", "2030-01-01T00:00:00Z")
        self.assertEqual(doc1["documentNamespace"], doc2["documentNamespace"])
        self.assertNotEqual(si.render(doc1), si.render(doc2))
        write(
            repo.root,
            {"obs/images/Demo/Demo.kiwi": kiwi("Demo", DEMO_IMAGE + '<package name="more"/>')},
        )
        doc3, _ = repo.build()
        self.assertNotEqual(doc1["documentNamespace"], doc3["documentNamespace"])
        doc4, _ = si.build(repo.root, repo.files, "0.2", CREATED)
        self.assertIn("-0.2-", doc4["documentNamespace"])

    def test_output_is_ascii(self) -> None:
        files = repo_files()
        files["image/packages.yaml"] = YAML.replace("license: MIT,", "license: M\u00cfT,")
        text = si.render(Repo(self, files).build()[0])
        text.encode("ascii")
        self.assertIn("\\u00cf", text)


class CheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc, _ = Repo(self).build()

    def problems(self, doc: object) -> list[str]:
        return texts(si.check_document(doc), False)

    def test_the_built_document_is_sound(self) -> None:
        self.assertEqual(self.problems(self.doc), [])

    def mutate(self, change) -> list[str]:  # noqa: ANN001
        doc = copy.deepcopy(self.doc)
        change(doc)
        return self.problems(doc)

    def test_header_fields(self) -> None:
        cases = {
            "spdxVersion": lambda d: d.update(spdxVersion="SPDX-2.2"),
            "dataLicense": lambda d: d.update(dataLicense="MIT"),
            "SPDXID": lambda d: d.update(SPDXID="SPDXRef-X"),
            "name": lambda d: d.update(name=""),
            "documentNamespace": lambda d: d.update(documentNamespace="http://x#y"),
            "creationInfo.created": lambda d: d["creationInfo"].update(created="yesterday"),
            "creationInfo.creators": lambda d: d["creationInfo"].update(creators=[]),
        }
        for key, change in cases.items():
            self.assertTrue(any(key in p for p in self.mutate(change)), key)

    def test_packages(self) -> None:
        def first_rpm(doc: dict) -> dict:
            return next(p for p in doc["packages"] if p["SPDXID"].startswith("SPDXRef-Rpm-"))

        for key in ("name", "versionInfo", "downloadLocation"):
            problems = self.mutate(lambda d, key=key: first_rpm(d).update({key: " "}))
            self.assertTrue(any(f"{key} is missing" in p for p in problems), key)
        for key in ("licenseConcluded", "licenseDeclared"):
            problems = self.mutate(lambda d, key=key: first_rpm(d).update({key: "not a licence!"}))
            self.assertTrue(any(key in p for p in problems), key)
        problems = self.mutate(lambda d: first_rpm(d).update(SPDXID="SPDXRef-bad id"))
        self.assertTrue(any("SPDXID" in p for p in problems))
        problems = self.mutate(lambda d: d["packages"].append(copy.deepcopy(first_rpm(d))))
        self.assertTrue(any("used twice" in p for p in problems))
        self.assertTrue(any("already" in p for p in problems))
        self.assertTrue(self.mutate(lambda d: d["packages"].append("x")))
        self.assertTrue(self.mutate(lambda d: d.update(packages=[])))
        self.assertTrue(self.problems([]))

    def test_purl_must_match(self) -> None:
        def wrong_name(doc: dict) -> None:
            pkg = next(p for p in doc["packages"] if p["name"] == "curl")
            pkg["externalRefs"][0]["referenceLocator"] = "pkg:rpm/opensuse/wget"

        def wrong_version(doc: dict) -> None:
            pkg = next(p for p in doc["packages"] if p["name"] == "curl")
            pkg["versionInfo"] = "1.0"

        def wrong_namespace(doc: dict) -> None:
            pkg = next(p for p in doc["packages"] if p["name"] == "curl")
            pkg["externalRefs"][0]["referenceLocator"] = "pkg:rpm/fedora/curl"

        for change in (wrong_name, wrong_version, wrong_namespace):
            self.assertTrue(any("purl must be" in p for p in self.mutate(change)), change.__name__)

    def test_relationships(self) -> None:
        def add(doc: dict, source: str, kind: str, target: str) -> None:
            doc["relationships"].append(
                {"spdxElementId": source, "relationshipType": kind, "relatedSpdxElement": target}
            )

        self.assertTrue(
            any(
                "unknown relationshipType" in p
                for p in self.mutate(
                    lambda d: add(d, "SPDXRef-Image-Demo", "DEPENDS_ON", "SPDXRef-Rpm-curl")
                )
            )
        )
        self.assertTrue(
            any(
                "not an element" in p
                for p in self.mutate(
                    lambda d: add(d, "SPDXRef-Image-Demo", "CONTAINS", "SPDXRef-Nothing")
                )
            )
        )
        self.assertTrue(
            any(
                "listed twice" in p
                for p in self.mutate(
                    lambda d: d["relationships"].append(copy.deepcopy(d["relationships"][-1]))
                )
            )
        )
        self.assertTrue(
            any(
                "describes no image" in p
                for p in self.mutate(
                    lambda d: d.update(
                        relationships=[
                            r for r in d["relationships"] if r["relationshipType"] != "DESCRIBES"
                        ]
                    )
                )
            )
        )
        self.assertTrue(
            any(
                "is not contained in any image" in p
                for p in self.mutate(
                    lambda d: d.update(
                        relationships=[
                            r
                            for r in d["relationships"]
                            if r["relatedSpdxElement"] != "SPDXRef-Rpm-wget"
                        ]
                    )
                )
            )
        )
        self.assertTrue(
            any(
                "is in no relationship" in p
                for p in self.mutate(
                    lambda d: d["packages"].append({**d["packages"][0], "SPDXID": "SPDXRef-Lonely"})
                )
            )
        )
        self.assertTrue(self.mutate(lambda d: d.update(relationships="x")))
        self.assertTrue(self.mutate(lambda d: d["relationships"].append("x")))
        self.assertTrue(
            any(
                "not an element" in p
                for p in self.mutate(
                    lambda d: add(d, "SPDXRef-Image-Demo", "CONTAINS", "NOASSERTION")
                )
            )
        )


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repo(self)
        self.root = ["--root", str(self.repo.root), "--created", CREATED, "--version", "0.1"]

    def test_document_on_stdout(self) -> None:
        code, out, err = run_main(self.root, self.repo.files)
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["spdxVersion"], "SPDX-2.3")
        self.assertEqual(out, si.render(self.repo.build()[0]))
        self.assertNotIn("SBOM ok", err)
        self.assertIn("warning", err)

    def test_check_prints_no_document(self) -> None:
        code, out, err = run_main(self.root + ["--check"], self.repo.files)
        self.assertEqual((code, out), (0, ""))
        self.assertIn("SBOM ok: 2 images, 11 openSUSE packages", err)
        self.assertIn("own packages left to tools/sbom.py", err)

    def test_out_file(self) -> None:
        path = self.repo.root / "out.json"
        code, out, err = run_main(self.root + ["--out", str(path)], self.repo.files)
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(path.read_text(encoding="utf-8"), si.render(self.repo.build()[0]))
        self.assertIn("SBOM ok", err)

    def test_out_cannot_be_written(self) -> None:
        path = self.repo.root / "no" / "such" / "out.json"
        code, _, err = run_main(self.root + ["--out", str(path)], self.repo.files)
        self.assertEqual(code, 2)
        self.assertIn("cannot write", err)

    def test_errors_write_nothing(self) -> None:
        (self.repo.root / "obs/images/Demo/Demo.kiwi").write_text("<image", encoding="utf-8")
        path = self.repo.root / "out.json"
        code, out, err = run_main(self.root + ["--out", str(path)], self.repo.files)
        self.assertEqual((code, out), (1, ""))
        self.assertFalse(path.exists())
        self.assertIn("SBOM not written: 1 errors", err)
        code, out, _ = run_main(self.root, self.repo.files)
        self.assertEqual((code, out), (1, ""))

    def test_versions_option(self) -> None:
        path = self.repo.root / "versions.json"
        path.write_text('{"curl": "8.5-1.1"}', encoding="utf-8")
        code, out, err = run_main(self.root + ["--versions", str(path)], self.repo.files)
        self.assertEqual(code, 0, err)
        self.assertEqual(by_name(json.loads(out), "curl")["versionInfo"], "8.5-1.1")
        path.write_text("[1]", encoding="utf-8")
        code, out, err = run_main(self.root + ["--versions", str(path)], self.repo.files)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--versions", err)

    def test_wrong_calls(self) -> None:
        for args in (
            ["--nope"],
            ["--version", "1 2"],
            ["--version", "1\n"],
            ["--version", "../x"],
            ["--created", "yesterday"],
            ["--created", "2026-13-45T99:99:99Z"],
        ):
            code, out, err = run_main(self.root[:2] + args, self.repo.files)
            self.assertEqual((code, out), (2, ""), args)
            self.assertTrue(err, args)

    def test_help_is_not_an_error(self) -> None:
        code, out, _ = run_main(["--help"], [])
        self.assertEqual(code, 0)
        self.assertIn("usage: sbom_image.py", out)

    def test_source_date_epoch(self) -> None:
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1764666000"}):
            code, out, _ = run_main(["--root", str(self.repo.root)], self.repo.files)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["creationInfo"]["created"], "2025-12-02T09:00:00Z")
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "soon"}):
            code, out, err = run_main(self.root, self.repo.files)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("SOURCE_DATE_EPOCH", err)

    def test_created_time(self) -> None:
        now = datetime(2026, 12, 2, 8, 0, 0, tzinfo=UTC)
        self.assertEqual(si.created_time(None, {}, now), "2026-12-02T08:00:00Z")
        self.assertEqual(si.created_time(CREATED, {}, now), CREATED)
        self.assertEqual(
            si.created_time(CREATED, {"SOURCE_DATE_EPOCH": "0"}, now), "1970-01-01T00:00:00Z"
        )
        self.assertEqual(si.created_time(CREATED, {"SOURCE_DATE_EPOCH": " "}, now), CREATED)
        with self.assertRaises(ValueError):
            si.created_time(None, {"SOURCE_DATE_EPOCH": "99999999999999999999"}, now)

    def test_not_a_git_repo(self) -> None:
        for error in (OSError("no git"), subprocess.CalledProcessError(128, "git")):
            with (
                mock.patch.object(si, "tracked_files", side_effect=error),
                contextlib.redirect_stderr(io.StringIO()) as err,
            ):
                self.assertEqual(si.main(["--check"]), 2)
            self.assertIn("cannot list files with git ls-files", err.getvalue())

    def test_the_only_command_is_git_ls_files(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="a\0b\0")
        with mock.patch.object(si.subprocess, "run", return_value=done) as run:
            self.assertEqual(si.tracked_files(Path(".")), ["a", "b"])
        self.assertEqual(run.call_args.args[0], ["git", "ls-files", "-z"])


class OptionalValidatorTests(unittest.TestCase):
    """The document is also read by the SPDX reference tools, when they are installed."""

    def test_pyspdxtools_accepts_it(self) -> None:
        try:
            from spdx_tools.spdx.parser.parse_anything import parse_file
            from spdx_tools.spdx.validation.document_validator import validate_full_spdx_document
        except ImportError:
            self.skipTest("spdx-tools is not installed")
        repo = Repo(self)
        for versions in (None, {"curl": "8.5-1.1", "libstdc++6": "14.2.0+git-1"}):
            doc, _ = repo.build(versions)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "doc.spdx.json"
                path.write_text(si.render(doc), encoding="utf-8")
                parsed = parse_file(str(path))
            self.assertEqual(validate_full_spdx_document(parsed), [])


@unittest.skipUnless((ROOT / ".git").exists(), "not a git checkout")
class RealRepoTests(unittest.TestCase):
    def build(self) -> tuple[dict, list[si.Problem]]:
        try:
            files = si.tracked_files(ROOT)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git is not available")
        return si.build(ROOT, files, "0.0.0-dev", CREATED)

    def test_the_repo_has_a_sound_image_sbom(self) -> None:
        doc, problems = self.build()
        self.assertEqual(texts(problems + si.check_document(doc), False), [])
        self.assertEqual(names(doc, "Image"), ["Vilna", "Vilna-Live", "tik-osimage-Vilna"])
        rpms = set(names(doc, "Rpm"))
        self.assertLessEqual(
            {"kernel-default", "systemd-boot", "MozillaFirefox", "proton-vpn"}, rpms
        )
        # our own packages are not here (tools/sbom.py lists them)
        for ours in (
            "Vilna-release",
            "gloxi",
            "vilna-installer",
            "ptyxis",
            "mullvad-vpn",
        ):
            self.assertNotIn(ours, rpms)
        self.assertIn("tik-osimage-Vilna", contains(doc, "Vilna-Live"))
        self.assertIn("MozillaFirefox", contains(doc, "Vilna-Live"))
        self.assertNotIn("MozillaFirefox", contains(doc, "Vilna"))
        self.assertIn("dracut-kiwi-oem-repart", contains(doc, "Vilna"))

    def test_every_package_has_a_name_that_rpm_accepts(self) -> None:
        doc, _ = self.build()
        for name in names(doc, "Rpm"):
            self.assertRegex(name, si.RPM_NAME_RE)


if __name__ == "__main__":
    unittest.main()
