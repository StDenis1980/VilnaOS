#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_versions.py (versions of spec, pyproject, Cargo and the alpha).

Every test writes a small repository (projects, specs, metainfo and the alpha table)
into a temporary folder and checks it the way ``main()`` does; there is a test for
every rule, for the version mapping and one for the good repository.

Run: python3 tools/test_check_versions.py
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_versions as cv  # noqa: E402  (after sys.path.insert above)

GLOXI_SPEC = "obs/packages/gloxi/gloxi.spec"
HERKOS_SPEC = "obs/packages/herkos/herkos.spec"
ALPHA = "docs/alpha-freeze.md"
METAINFO = "gloxi/data/org.demo.Gloxi.metainfo.xml"


def pyproject(name: str, version: str, package: str, extra: str = "") -> str:
    return (
        f'[project]\nname = "{name}"\nversion = "{version}"\n{extra}\n'
        f'[tool.hatch.build.targets.wheel]\npackages = ["src/{package}"]\n'
    )


def spec(name: str, version: str, body: str = "", url: str = "https://example.org") -> str:
    return (
        f"%define pythons python3\nName:           {name}\nVersion:        {version}\n"
        f"Release:        0\nSummary:        S\nURL:            {url}\n"
        f"Source0:        %{{name}}-%{{version}}.tar.gz\n\n%description\nD.\n\n"
        f"%prep\n%autosetup\n\n%build\n{body}\n%install\n\n%files\n/a\n%changelog\n"
    )


def metainfo(*releases: tuple[str, str]) -> str:
    items = "".join(f'<release version="{v}" date="{d}"/>' for v, d in releases)
    return f"<component><id>org.demo.Gloxi</id><releases>{items}</releases></component>"


OBS_SYNC = """
TARBALLS: dict[str, Tarball] = {
    "gloxi": Tarball("gloxi", pyproject_version("gloxi"), extra=(("LICENSE", "LICENSE"),)),
    "herkos": Tarball("herkos", pyproject_version("herkos"), cargo="herkos/rs/Cargo.toml"),
}
"""

CARGO_WORKSPACE = (
    '[workspace]\nmembers = ["crates/core"]\n\n[workspace.package]\nversion = "0.1.0-dev0"\n'
)
CARGO_MEMBER = '[package]\nname = "herkos-core"\nversion.workspace = true\n'

ALPHA_TABLE = """# Alpha

| Компонент | Версія на 23.11.2026 | Для альфи |
| --- | --- | --- |
| Gloxi: сервіс, gloxi-session | 0.2.0~a1 | входить |
| Herkos: herkosd, `herkos-selinux` | 0.1.0~dev0 | входить |
| Нічого | — | не входить |
"""


def repo(**overrides: str | None) -> dict[str, str | None]:
    """A good repository; ``name="path"`` keys are given as ``files=`` below."""
    files: dict[str, str | None] = {
        "gloxi/pyproject.toml": pyproject("gloxi", "0.2.0a1", "gloxi"),
        "gloxi/src/gloxi/__init__.py": '"""Gloxi."""\n\n__version__ = "0.2.0a1"\n',
        METAINFO: metainfo(("0.2.0~a1", "2026-11-10"), ("0.1.0~a1", "2026-11-04")),
        GLOXI_SPEC: spec("gloxi", "0.2.0~a1"),
        "herkos/pyproject.toml": pyproject("herkos", "0.1.0.dev0", "herkos"),
        "herkos/src/herkos/__init__.py": '__version__ = "0.1.0.dev0"\n',
        "herkos/rs/Cargo.toml": CARGO_WORKSPACE,
        "herkos/rs/crates/core/Cargo.toml": CARGO_MEMBER,
        HERKOS_SPEC: spec(
            "herkos", "0.1.0~dev0", "cargo build --manifest-path rs/Cargo.toml --release\n"
        ),
        "obs/tools/obs_sync.py": OBS_SYNC,
        ALPHA: ALPHA_TABLE,
    }
    files.update(overrides)
    return files


def run(files: dict[str, str | None]) -> tuple[list[cv.Problem], list[cv.Row]]:
    """Write the files (a ``None`` value leaves the file out) and check the repository."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        tracked = []
        for rel, text in files.items():
            if text is None:
                continue
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            tracked.append(rel)
        return cv.check_repo(root, tracked)


def problems_of(files: dict[str, str | None], rule: str | None = None) -> list[cv.Problem]:
    found, _ = run(files)
    return [p for p in found if rule is None or p.rule == rule]


def texts(problems: list[cv.Problem]) -> list[str]:
    return [str(p) for p in problems]


class CheckTestCase(unittest.TestCase):
    """The repo's own LEGACY pairs are switched off: the tests make their own problems."""

    def setUp(self) -> None:
        patcher = mock.patch.object(cv, "LEGACY", frozenset())
        patcher.start()
        self.addCleanup(patcher.stop)


class VersionMapTests(unittest.TestCase):
    def test_pep440_to_rpm_examples_of_the_plan(self) -> None:
        for pep, rpm in [
            ("0.1.0.dev0", "0.1.0~dev0"),
            ("0.2.0a1", "0.2.0~a1"),
            ("1.0.0rc1", "1.0.0~rc1"),
            ("1.0.0", "1.0.0"),
        ]:
            with self.subTest(pep=pep):
                self.assertEqual(cv.pep440_to_rpm(pep), rpm)

    def test_pep440_to_rpm_other_forms(self) -> None:
        self.assertEqual(cv.pep440_to_rpm("1.2b3"), "1.2~b3")
        self.assertEqual(cv.pep440_to_rpm("1.0a1.dev2"), "1.0~a1~dev2")
        self.assertEqual(cv.pep440_to_rpm("1.0.post1"), "1.0.post1")
        self.assertEqual(cv.pep440_to_rpm("1.0.post1.dev3"), "1.0.post1~dev3")
        self.assertEqual(cv.pep440_to_rpm("2026.5"), "2026.5")

    def test_pep440_to_rpm_rejects_what_obs_sync_would_not_map(self) -> None:
        for bad in ["", "1!1.0", "1.0+local", "1.0-rc1", "1.0.dev", "1.0rc", "v1.0", "1.0.0.x"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cv.pep440_to_rpm(bad)

    def test_rpm_to_pep440_is_the_reverse(self) -> None:
        for rpm, pep in [
            ("0.1.0~dev0", "0.1.0.dev0"),
            ("0.2.0~a1", "0.2.0a1"),
            ("1.0.0~rc1", "1.0.0rc1"),
            ("1.0.0", "1.0.0"),
            ("1.0~a1~dev2", "1.0a1.dev2"),
        ]:
            with self.subTest(rpm=rpm):
                self.assertEqual(cv.rpm_to_pep440(rpm), pep)
                self.assertEqual(cv.pep440_to_rpm(pep), rpm)

    def test_rpm_to_pep440_rejects_other_rpm_versions(self) -> None:
        for bad in ["50.1+vilna1", "1.0^git1", "", "%{version}"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cv.rpm_to_pep440(bad)

    def test_cargo_to_rpm(self) -> None:
        self.assertEqual(cv.cargo_to_rpm("0.1.0"), "0.1.0")
        self.assertEqual(cv.cargo_to_rpm("0.1.0-dev0"), "0.1.0~dev0")
        self.assertEqual(cv.cargo_to_rpm("1.2.3-rc.1+build5"), "1.2.3~rc.1")
        with self.assertRaises(ValueError):
            cv.cargo_to_rpm("1.2")

    def test_same_version_in_both_spellings(self) -> None:
        self.assertTrue(cv.same_version("0.2.0a1", "0.2.0~a1"))
        self.assertTrue(cv.same_version("0.1", "0.1"))
        self.assertTrue(cv.same_version("50.1+vilna1", "50.1+vilna1"))
        self.assertFalse(cv.same_version("0.1.0", "0.1.0~dev0"))
        self.assertFalse(cv.same_version("0.1", "0.1.0"))


class GoodRepoTests(CheckTestCase):
    def test_good_repo_has_no_problems(self) -> None:
        found, _ = run(repo())
        self.assertEqual(texts(found), [])

    def test_table_rows_show_every_place(self) -> None:
        _, rows = run(repo())
        by_name = {r.component: r for r in rows}
        gloxi = by_name["gloxi"]
        self.assertEqual(
            (gloxi.project, gloxi.spec, gloxi.metainfo, gloxi.alpha),
            ("0.2.0a1", "0.2.0~a1", "0.2.0~a1", ["0.2.0~a1"]),
        )
        herkos = by_name["herkos"]
        self.assertEqual(herkos.project, "0.1.0.dev0 + cargo 0.1.0-dev0")
        self.assertEqual(herkos.alpha, ["0.1.0~dev0"])

    def test_format_table(self) -> None:
        _, rows = run(repo())
        lines = cv.format_table(rows)
        self.assertTrue(lines[0].startswith("| component"))
        self.assertIn("pyproject/Cargo", lines[0])
        self.assertIn("alpha", lines[0])
        self.assertTrue(set(lines[1]) <= set("|- "))
        self.assertEqual({len(line) for line in lines}, {len(lines[0])})
        gloxi = next(line for line in lines if line.startswith("| gloxi"))
        cells = [c.strip() for c in gloxi.strip("|").split("|")]
        self.assertEqual(cells, ["gloxi", "0.2.0a1", "0.2.0~a1", "0.2.0~a1", "0.2.0~a1"])

    def test_empty_repo_has_only_the_missing_alpha_warning(self) -> None:
        found, rows = run({})
        self.assertEqual([p.rule for p in found], ["alpha-missing"])
        self.assertEqual(rows, [])


class ProjectVersionTests(CheckTestCase):
    def test_non_canonical_pep440_is_an_error(self) -> None:
        files = repo(**{"gloxi/pyproject.toml": pyproject("gloxi", "0.2.0-a1", "gloxi")})
        found = problems_of(files, "version-format")
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].warning)
        self.assertIn("gloxi/pyproject.toml: project.version '0.2.0-a1'", str(found[0]))

    def test_version_that_is_not_a_string_is_an_error(self) -> None:
        bad = '[project]\nname = "gloxi"\nversion = 2\n'
        found = problems_of(repo(**{"gloxi/pyproject.toml": bad}), "version-format")
        self.assertEqual(texts(found), ["gloxi/pyproject.toml: project.version is not a string"])

    def test_file_with_a_bom_is_one_error_and_is_still_read(self) -> None:
        bom = "\ufeff" + pyproject("gloxi", "0.2.0a1", "gloxi")
        found, rows = run(repo(**{"gloxi/pyproject.toml": bom}))
        self.assertEqual(
            texts(found), ["gloxi/pyproject.toml: the file starts with a byte order mark (BOM)"]
        )
        self.assertEqual(next(r for r in rows if r.component == "gloxi").project, "0.2.0a1")

    def test_missing_version_is_an_error(self) -> None:
        bad = '[project]\nname = "gloxi"\n'
        found = problems_of(repo(**{"gloxi/pyproject.toml": bad}), "version-format")
        self.assertEqual(texts(found), ["gloxi/pyproject.toml: [project] has no version"])

    def test_dynamic_version_is_a_warning(self) -> None:
        bad = '[project]\nname = "gloxi"\ndynamic = ["version"]\n'
        found = problems_of(repo(**{"gloxi/pyproject.toml": bad}), "version-dynamic")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].warning)

    def test_pyproject_without_project_table_is_ignored(self) -> None:
        files = repo(**{"tools-only/pyproject.toml": "[tool.ruff]\nline-length = 100\n"})
        found, rows = run(files)
        self.assertEqual(texts(found), [])
        self.assertNotIn("tools-only", [r.component for r in rows])

    def test_bad_toml_is_an_error(self) -> None:
        found = problems_of(repo(**{"gloxi/pyproject.toml": "[project\nname="}), "toml")
        self.assertEqual(len(found), 1)
        self.assertIn("gloxi/pyproject.toml: not valid TOML", str(found[0]))

    def test_file_that_is_not_utf8_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "pyproject.toml").write_bytes(b"\xff\xfe\x00")
            found, _ = cv.check_repo(Path(tmp), ["pyproject.toml"])
        self.assertIn("pyproject.toml: the file is not valid UTF-8", texts(found))

    def test_tracked_file_that_is_missing_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            found, _ = cv.check_repo(Path(tmp), ["gloxi/pyproject.toml"])
        self.assertIn("gloxi/pyproject.toml: the file does not exist", texts(found))

    def test_bad_cargo_version_is_an_error(self) -> None:
        bad = CARGO_WORKSPACE.replace("0.1.0-dev0", "0.1")
        found = problems_of(repo(**{"herkos/rs/Cargo.toml": bad}), "version-format")
        self.assertEqual(len(found), 1)
        self.assertIn("herkos/rs/Cargo.toml: version '0.1' is not X.Y.Z[-pre]", str(found[0]))


class SpecVersionTests(CheckTestCase):
    def test_spec_and_pyproject_differ(self) -> None:
        files = repo(**{GLOXI_SPEC: spec("gloxi", "0.2.0")})
        found = problems_of(files, "spec-version")
        self.assertEqual(
            texts(found),
            [
                f"{GLOXI_SPEC}: Version 0.2.0 does not match gloxi/pyproject.toml version "
                "0.2.0a1 (the spec needs 0.2.0~a1)"
            ],
        )
        self.assertFalse(found[0].warning)

    def test_pep440_spelling_in_the_spec_is_an_error(self) -> None:
        files = repo(**{GLOXI_SPEC: spec("gloxi", "0.2.0a1")})
        self.assertEqual(len(problems_of(files, "spec-version")), 1)

    def test_spec_matched_by_name_without_obs_sync(self) -> None:
        files = repo(**{"obs/tools/obs_sync.py": None, GLOXI_SPEC: spec("gloxi", "0.9")})
        self.assertEqual(len(problems_of(files, "spec-version")), 1)

    def test_python_prefix_of_the_spec_name_is_dropped(self) -> None:
        files = repo(
            **{
                "gateway/pyproject.toml": pyproject("vilna-privacy-gateway", "0.1.0.dev0", "pg"),
                "obs/packages/python-vilna-privacy-gateway/python-vilna-privacy-gateway.spec": spec(
                    "python-vilna-privacy-gateway", "0.2"
                ),
            }
        )
        found = problems_of(files, "spec-version")
        self.assertEqual(len(found), 1)
        self.assertIn("gateway/pyproject.toml", str(found[0]))

    def test_name_with_underscore_matches_the_normalized_project_name(self) -> None:
        files = repo(
            **{
                "gateway/pyproject.toml": pyproject("Vilna_Privacy.Gateway", "1.0", "pg"),
                "obs/packages/vilna-privacy-gateway/vilna-privacy-gateway.spec": spec(
                    "vilna-privacy-gateway", "2.0"
                ),
            }
        )
        self.assertEqual(len(problems_of(files, "spec-version")), 1)

    def test_obs_sync_folder_matches_a_spec_with_another_name(self) -> None:
        sync = OBS_SYNC.replace('"gloxi": Tarball("gloxi"', '"gloxi-app": Tarball("gloxi"')
        files = repo(
            **{
                "obs/tools/obs_sync.py": sync,
                "obs/packages/gloxi-app/gloxi-app.spec": spec("gloxi-app", "0.9"),
                GLOXI_SPEC: None,
            }
        )
        found = problems_of(files, "spec-version")
        self.assertEqual(len(found), 1)
        self.assertIn("obs/packages/gloxi-app/gloxi-app.spec", str(found[0]))

    def test_name_and_tarball_pointing_at_different_projects_is_a_warning(self) -> None:
        sync = OBS_SYNC.replace('"gloxi": Tarball("gloxi"', '"gloxi": Tarball("herkos"')
        found = problems_of(repo(**{"obs/tools/obs_sync.py": sync}), "spec-conflict")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].warning)
        self.assertIn("name points at gloxi/pyproject.toml", str(found[0]))
        # The version is compared with the project the tarball is made from.
        self.assertTrue(problems_of(repo(**{"obs/tools/obs_sync.py": sync}), "spec-version"))

    def test_spec_matched_by_a_folder_in_its_build_lines(self) -> None:
        files = repo(
            **{
                "obs/packages/tool/tool.spec": spec("tool", "9.9", "cd gloxi/src && true\n"),
            }
        )
        found = problems_of(files, "spec-version")
        self.assertEqual(len(found), 1)
        self.assertIn("obs/packages/tool/tool.spec", str(found[0]))

    def test_installed_path_of_a_folder_name_is_not_a_match(self) -> None:
        body = "install -D x %{buildroot}%{_datadir}/gloxi/x\n"
        files = repo(**{"obs/packages/tool/tool.spec": spec("tool", "9.9", body)})
        self.assertEqual(problems_of(files, "spec-version"), [])

    def test_spec_matched_by_a_folder_in_its_url(self) -> None:
        url = "https://github.com/x/y/tree/main/gloxi"
        files = repo(**{"obs/packages/tool/tool.spec": spec("tool", "9.9", url=url)})
        self.assertEqual(len(problems_of(files, "spec-version")), 1)

    def test_spec_that_matches_two_projects_is_ambiguous(self) -> None:
        body = "cd gloxi/src && cd herkos/src\n"
        files = repo(**{"obs/packages/tool/tool.spec": spec("tool", "9.9", body)})
        found = problems_of(files, "spec-ambiguous")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].warning)
        self.assertEqual(problems_of(files, "spec-version"), [])
        self.assertEqual(problems_of(files, "spec-unmatched"), [])

    def test_unmatched_specs_are_one_warning_with_the_list(self) -> None:
        files = repo(
            **{
                "obs/packages/zeta/zeta.spec": spec("zeta", "1.0"),
                "obs/packages/alpha/alpha.spec": spec("alpha", "2.0"),
            }
        )
        found = problems_of(files, "spec-unmatched")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].warning)
        self.assertEqual(found[0].path, "obs/packages")
        self.assertIn("2 spec files are not matched", found[0].message)
        self.assertIn("alpha, zeta", found[0].message)
        self.assertNotIn("gloxi,", found[0].message)

    def test_unmatched_spec_still_gets_a_table_row(self) -> None:
        _, rows = run(repo(**{"obs/packages/zeta/zeta.spec": spec("zeta", "1.0")}))
        row = next(r for r in rows if r.component == "zeta")
        self.assertEqual((row.project, row.spec), ("-", "1.0"))

    def test_macro_in_the_version_is_expanded(self) -> None:
        text = spec("gloxi", "%{base}").replace(
            "%define pythons python3", "%define pythons python3\n%define base 0.2.0~a1"
        )
        found, _ = run(repo(**{GLOXI_SPEC: text}))
        self.assertEqual(texts(found), [])

    def test_unknown_macro_in_the_version_is_a_warning(self) -> None:
        found = problems_of(repo(**{GLOXI_SPEC: spec("gloxi", "%{upstream}")}), "spec-macro")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].warning)
        self.assertEqual(
            problems_of(repo(**{GLOXI_SPEC: spec("gloxi", "%{x}")}), "spec-version"), []
        )

    def test_first_version_of_the_preamble_counts(self) -> None:
        text = spec("gloxi", "0.2.0~a1").replace("Release:", "Version:        9.9\nRelease:")
        self.assertEqual(problems_of(repo(**{GLOXI_SPEC: text}), "spec-version"), [])

    def test_source_with_a_folder_path_matches_a_project(self) -> None:
        text = spec("tool", "9.9").replace("Source0:", "Source1:        gloxi/data/x.svg\nSource0:")
        files = repo(**{"obs/packages/tool/tool.spec": text})
        self.assertEqual(len(problems_of(files, "spec-version")), 1)

    def test_source_url_with_a_folder_matches_a_project(self) -> None:
        url = "https://github.com/x/y/blob/main/gloxi/data/x.svg"
        text = spec("tool", "9.9").replace("Source0:", f"Source1:        {url}\nSource0:")
        files = repo(**{"obs/packages/tool/tool.spec": text})
        self.assertEqual(len(problems_of(files, "spec-version")), 1)

    def test_source_of_a_subpackage_does_not_match_a_project(self) -> None:
        text = spec("tool", "9.9").replace(
            "%description\nD.\n", "%description\nD.\n\n%package extra\nSource9: gloxi/data/x.svg\n"
        )
        files = repo(**{"obs/packages/tool/tool.spec": text})
        self.assertEqual(problems_of(files, "spec-version"), [])
        self.assertEqual(len(problems_of(files, "spec-unmatched")), 1)

    def test_version_of_a_subpackage_does_not_count(self) -> None:
        text = spec("gloxi", "0.2.0~a1").replace(
            "%description\nD.\n", "%description\nD.\n\n%package extra\nVersion: 9.9\nSummary: S\n"
        )
        found, _ = run(repo(**{GLOXI_SPEC: text}))
        self.assertEqual(texts(found), [])


class CargoVersionTests(CheckTestCase):
    def test_cargo_and_spec_differ(self) -> None:
        bad = CARGO_WORKSPACE.replace("0.1.0-dev0", "0.1.0")
        found = problems_of(repo(**{"herkos/rs/Cargo.toml": bad}), "cargo-spec")
        self.assertEqual(
            texts(found),
            [
                f"herkos/rs/Cargo.toml: version 0.1.0 does not match {HERKOS_SPEC} Version "
                "0.1.0~dev0 (Cargo needs 0.1.0-dev0)"
            ],
        )
        self.assertFalse(found[0].warning)

    def test_cargo_matches_the_spec_with_a_pre_release_mapped(self) -> None:
        self.assertEqual(problems_of(repo(), "cargo-spec"), [])

    def test_legacy_pair_is_a_warning(self) -> None:
        bad = CARGO_WORKSPACE.replace("0.1.0-dev0", "0.1.0")
        legacy = frozenset({("herkos/rs/Cargo.toml", "cargo-spec")})
        with mock.patch.object(cv, "LEGACY", legacy):
            found = problems_of(repo(**{"herkos/rs/Cargo.toml": bad}), "cargo-spec")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].warning)
        self.assertEqual(str(found[0]).split(": ")[1], "warning")

    def test_pyproject_error_on_the_same_spec_is_not_legacy(self) -> None:
        bad = CARGO_WORKSPACE.replace("0.1.0-dev0", "0.1.0")
        legacy = frozenset({("herkos/rs/Cargo.toml", "cargo-spec")})
        files = repo(**{"herkos/rs/Cargo.toml": bad, HERKOS_SPEC: spec("herkos", "0.9", "")})
        with mock.patch.object(cv, "LEGACY", legacy):
            found = problems_of(files)
        self.assertTrue(any(p.rule == "spec-version" and not p.warning for p in found))

    def test_member_crate_manifest_in_the_build_line_finds_the_workspace(self) -> None:
        body = "cargo build --manifest-path rs/crates/core/Cargo.toml\n"
        bad = CARGO_WORKSPACE.replace("0.1.0-dev0", "0.3.0")
        files = repo(
            **{"herkos/rs/Cargo.toml": bad, HERKOS_SPEC: spec("herkos", "0.1.0~dev0", body)}
        )
        files["obs/tools/obs_sync.py"] = OBS_SYNC.replace(', cargo="herkos/rs/Cargo.toml"', "")
        self.assertEqual(len(problems_of(files, "cargo-spec")), 1)

    def test_cargo_of_obs_sync_is_enough_without_a_build_line(self) -> None:
        bad = CARGO_WORKSPACE.replace("0.1.0-dev0", "0.3.0")
        files = repo(**{"herkos/rs/Cargo.toml": bad, HERKOS_SPEC: spec("herkos", "0.1.0~dev0")})
        self.assertEqual(len(problems_of(files, "cargo-spec")), 1)

    def test_member_manifest_belongs_to_the_nearest_workspace(self) -> None:
        body = "cargo build --manifest-path rs/crates/core/Cargo.toml\n"
        outer = '[workspace]\nmembers = ["x"]\n[workspace.package]\nversion = "7.0.0"\n'
        bad = CARGO_WORKSPACE.replace("0.1.0-dev0", "0.3.0")
        files = repo(
            **{
                "Cargo.toml": outer,
                "herkos/rs/Cargo.toml": bad,
                HERKOS_SPEC: spec("herkos", "0.1.0~dev0", body),
            }
        )
        files["obs/tools/obs_sync.py"] = OBS_SYNC.replace(', cargo="herkos/rs/Cargo.toml"', "")
        found = problems_of(files, "cargo-spec")
        self.assertEqual([p.path for p in found], ["herkos/rs/Cargo.toml"])

    def test_manifest_path_is_relative_to_the_tarball_folder(self) -> None:
        # Without obs_sync.py the folder of the tarball is unknown, so rs/Cargo.toml is
        # looked up from the repo root, where it does not exist.
        bad = CARGO_WORKSPACE.replace("0.1.0-dev0", "0.3.0")
        files = repo(**{"herkos/rs/Cargo.toml": bad, "obs/tools/obs_sync.py": None})
        self.assertEqual(problems_of(files, "cargo-spec"), [])

    def test_standalone_crate_with_its_own_version(self) -> None:
        files = repo(
            **{
                "tool/Cargo.toml": '[package]\nname = "tool"\nversion = "1.0.0"\n',
                "obs/packages/tool/tool.spec": spec("tool", "1.1.0", "cargo build\n"),
            }
        )
        found, rows = run(files)
        self.assertEqual(problems_of(files, "cargo-spec"), [])
        # The name matches no pyproject: the standalone crate is listed without a spec.
        row = next(r for r in rows if r.component == "tool" and r.spec == "-")
        self.assertEqual(row.project, "cargo 1.0.0")
        self.assertTrue(found)

    def test_workspace_version_is_preferred_to_the_version_of_the_root_crate(self) -> None:
        root = CARGO_WORKSPACE + '\n[package]\nname = "herkos"\nversion = "9.9.9"\n'
        self.assertEqual(problems_of(repo(**{"herkos/rs/Cargo.toml": root}), "cargo-spec"), [])

    def test_cargo_workspace_without_a_spec_is_only_in_the_table(self) -> None:
        files = repo(**{"reports/Cargo.toml": CARGO_WORKSPACE})
        found, rows = run(files)
        self.assertEqual(texts(found), [])
        self.assertIn("reports", [r.component for r in rows])

    def test_member_crate_with_workspace_version_is_not_a_project(self) -> None:
        _, rows = run(repo())
        self.assertNotIn("herkos-core", [r.component for r in rows])


class InitVersionTests(CheckTestCase):
    def test_dunder_version_differs(self) -> None:
        bad = '__version__ = "0.2.0"\n'
        found = problems_of(repo(**{"gloxi/src/gloxi/__init__.py": bad}), "init-version")
        self.assertEqual(
            texts(found),
            [
                "gloxi/src/gloxi/__init__.py: __version__ '0.2.0' does not match "
                "gloxi/pyproject.toml version '0.2.0a1'"
            ],
        )
        self.assertFalse(found[0].warning)

    def test_annotated_assignment_is_read(self) -> None:
        bad = '__version__: str = "0.2.0"\n'
        found = problems_of(repo(**{"gloxi/src/gloxi/__init__.py": bad}), "init-version")
        self.assertEqual(len(found), 1)

    def test_package_without_a_version_is_fine(self) -> None:
        found, _ = run(repo(**{"gloxi/src/gloxi/__init__.py": '"""Gloxi."""\n'}))
        self.assertEqual(texts(found), [])

    def test_version_that_is_not_a_literal_is_fine(self) -> None:
        text = "from importlib.metadata import version\n__version__ = version('gloxi')\n"
        found, _ = run(repo(**{"gloxi/src/gloxi/__init__.py": text}))
        self.assertEqual(texts(found), [])

    def test_version_set_inside_a_function_is_not_the_package_version(self) -> None:
        text = 'def f():\n    __version__ = "9"\n'
        self.assertEqual(cv.module_version(text), None)
        self.assertEqual(cv.module_version('__version__ = "1.0"\n'), "1.0")
        self.assertEqual(cv.module_version("def broken(:\n"), None)

    def test_packages_without_hatch_settings_are_found_in_src(self) -> None:
        plain = '[project]\nname = "gloxi"\nversion = "0.2.0a1"\n'
        bad = '__version__ = "0.2.0"\n'
        files = repo(**{"gloxi/pyproject.toml": plain, "gloxi/src/gloxi/__init__.py": bad})
        self.assertEqual(len(problems_of(files, "init-version")), 1)

    def test_init_of_a_nested_package_is_not_checked(self) -> None:
        plain = '[project]\nname = "gloxi"\nversion = "0.2.0a1"\n'
        files = repo(
            **{
                "gloxi/pyproject.toml": plain,
                "gloxi/src/gloxi/gui/__init__.py": '__version__ = "7"\n',
            }
        )
        self.assertEqual(problems_of(files, "init-version"), [])

    def test_hatch_package_outside_src_is_read(self) -> None:
        files = repo(
            **{
                "gloxi/pyproject.toml": pyproject("gloxi", "0.2.0a1", "gloxi").replace(
                    '"src/gloxi"', '"lib/gloxi"'
                ),
                "gloxi/lib/gloxi/__init__.py": '__version__ = "0.2.0"\n',
            }
        )
        found = problems_of(files, "init-version")
        self.assertEqual([p.path for p in found], ["gloxi/lib/gloxi/__init__.py"])

    def test_init_that_is_not_tracked_is_skipped(self) -> None:
        found, _ = run(repo(**{"gloxi/src/gloxi/__init__.py": None}))
        self.assertEqual(texts(found), [])


class MetainfoTests(CheckTestCase):
    def test_newest_release_differs_from_the_program(self) -> None:
        files = repo(**{METAINFO: metainfo(("0.1.0~a1", "2026-11-04"))})
        found = problems_of(files, "metainfo-version")
        self.assertEqual(
            texts(found),
            [
                f"{METAINFO}: warning: the newest release is 0.1.0~a1 but "
                "gloxi/pyproject.toml is 0.2.0a1"
            ],
        )

    def test_newest_is_taken_by_date_not_by_position(self) -> None:
        text = metainfo(("0.1.0~a1", "2026-11-04"), ("0.2.0~a1", "2026-11-10"))
        self.assertEqual(problems_of(repo(**{METAINFO: text}), "metainfo-version"), [])

    def test_older_release_listed_first_is_only_found_wrong_when_it_is_newest(self) -> None:
        text = metainfo(("0.3.0~a1", "2026-12-01"), ("0.2.0~a1", "2026-11-10"))
        self.assertEqual(len(problems_of(repo(**{METAINFO: text}), "metainfo-version")), 1)

    def test_release_without_date_is_the_first_one(self) -> None:
        text = (
            "<component><releases><release version='0.2.0~a1'/>"
            "<release version='0.1.0~a1'/></releases></component>"
        )
        self.assertEqual(cv.newest_release(text), "0.2.0~a1")
        self.assertIsNone(cv.newest_release("<component/>"))

    def test_both_spellings_of_the_version_are_accepted(self) -> None:
        text = metainfo(("0.2.0a1", "2026-11-10"))
        self.assertEqual(problems_of(repo(**{METAINFO: text}), "metainfo-version"), [])

    def test_metainfo_outside_every_project_is_ignored(self) -> None:
        files = repo(**{"thirdparty/app/x.metainfo.xml": metainfo(("6.1", "2026-11-25"))})
        found, _ = run(files)
        self.assertEqual(texts(found), [])

    def test_metainfo_in_a_cargo_project_folder(self) -> None:
        files = repo(**{"herkos/rs/data/x.metainfo.xml": metainfo(("0.5.0", "2026-11-25"))})
        found = problems_of(files, "metainfo-version")
        self.assertEqual(len(found), 1)
        self.assertIn("herkos/rs/Cargo.toml is 0.1.0-dev0", found[0].message)

    def test_python_project_is_preferred_to_a_crate_in_the_same_folder(self) -> None:
        crate = '[package]\nname = "gloxi-crate"\nversion = "1.0.0"\n'
        found = problems_of(repo(**{"gloxi/Cargo.toml": crate}), "metainfo-version")
        self.assertEqual(found, [])

    def test_malformed_metainfo_is_a_warning(self) -> None:
        found = problems_of(repo(**{METAINFO: "<component>"}), "xml")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].warning)

    def test_metainfo_version_is_in_the_table_row(self) -> None:
        files = repo(**{METAINFO: metainfo(("0.1.0~a1", "2026-11-04"))})
        _, rows = run(files)
        self.assertEqual(next(r for r in rows if r.component == "gloxi").metainfo, "0.1.0~a1")


class AlphaFreezeTests(CheckTestCase):
    def table(self, *rows: str) -> str:
        head = "| Компонент | Версія на 23.11.2026 | Для альфи |\n| --- | --- | --- |\n"
        return head + "\n".join(rows) + "\n"

    def test_version_of_a_snapshot_differs_is_a_warning(self) -> None:
        text = self.table("| Gloxi: сервіс | 0.1.0~a1 | входить |")
        found = problems_of(repo(**{ALPHA: text}), "alpha-version")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].warning)
        self.assertEqual(found[0].path, ALPHA)
        self.assertIn("line 3:", found[0].message)
        self.assertIn(
            "gloxi 0.1.0~a1 (Версія на 23.11.2026) but its spec has 0.2.0~a1", str(found[0])
        )

    def test_pep440_spelling_in_the_table_is_accepted(self) -> None:
        text = self.table("| Gloxi: сервіс | 0.2.0a1 | входить |")
        self.assertEqual(problems_of(repo(**{ALPHA: text}), "alpha-version"), [])

    def test_name_before_the_version_names_the_spec(self) -> None:
        text = self.table("| Основа | gloxi 0.9 | входить |")
        found = problems_of(repo(**{ALPHA: text}), "alpha-version")
        self.assertEqual(len(found), 1)
        self.assertIn("shows gloxi 0.9", found[0].message)

    def test_name_that_is_no_spec_is_ignored(self) -> None:
        text = self.table("| Основа | nothing 0.9 | входить |")
        self.assertEqual(problems_of(repo(**{ALPHA: text})), [])

    def test_subpackage_word_of_the_component_names_its_spec(self) -> None:
        text = self.table("| `herkos-selinux`, `herkos-dialog` | 0.2 | входить |")
        found = problems_of(repo(**{ALPHA: text}), "alpha-version")
        self.assertEqual(len(found), 1)
        self.assertIn("herkos 0.2", found[0].message)

    def test_longest_spec_name_wins_for_a_subpackage_word(self) -> None:
        files = repo(
            **{
                "obs/packages/herkos-extra/herkos-extra.spec": spec("herkos-extra", "3.0"),
                ALPHA: self.table("| `herkos-extra-tool` | 3.1 | входить |"),
            }
        )
        found = problems_of(files, "alpha-version")
        self.assertEqual(len(found), 1)
        self.assertIn("herkos-extra 3.1", found[0].message)

    def test_one_version_for_several_packages(self) -> None:
        text = self.table("| Gloxi, Herkos | 0.1.0~dev0 | входить |")
        found = problems_of(repo(**{ALPHA: text}), "alpha-version")
        self.assertEqual(len(found), 1)
        self.assertIn("gloxi 0.1.0~dev0", found[0].message)

    def test_versions_are_paired_with_packages_in_order(self) -> None:
        # "Брендування" is paired with its six specs by ALPHA_ALIASES, one version for each;
        # a spec the repo does not have just leaves its version unused.
        files = repo(
            **{
                "obs/packages/Vilna-release/Vilna-release.spec": spec("Vilna-release", "0.4"),
                "obs/packages/vilna-branding/vilna-branding.spec": spec("vilna-branding", "0.24"),
                ALPHA: self.table("| Брендування: все | 0.4 / 0.4 / 0.15 / 0.6 / 0.1 / 0.3 | x |"),
            }
        )
        found = problems_of(files, "alpha-version")
        self.assertEqual(len(found), 1)
        self.assertIn("vilna-branding 0.15", found[0].message)
        self.assertIn("its spec has 0.24", found[0].message)

    def test_alias_with_a_version_no_spec_has(self) -> None:
        files = repo(
            **{
                "obs/packages/mullvad-vpn/mullvad-vpn.spec": spec("mullvad-vpn", "2026.5"),
                ALPHA: self.table("| VPN: Proton, Mullvad | 4.15.0 / 2026.5 | входить |"),
            }
        )
        self.assertEqual(problems_of(files, "alpha-version"), [])
        files["obs/packages/mullvad-vpn/mullvad-vpn.spec"] = spec("mullvad-vpn", "2026.6")
        self.assertEqual(len(problems_of(files, "alpha-version")), 1)

    def test_versions_that_cannot_be_paired_are_a_warning(self) -> None:
        text = self.table("| Gloxi, Herkos | 0.2.0~a1 / 0.1.0~dev0 / 9 | входить |")
        found = problems_of(repo(**{ALPHA: text}), "alpha-unpaired")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].warning)
        self.assertEqual(problems_of(repo(**{ALPHA: text}), "alpha-version"), [])

    def test_cells_without_a_version_are_skipped(self) -> None:
        text = self.table(
            "| Gloxi | — | входить |", "| Gloxi | ADR | входить |", "| Gloxi | кроки 1–2 | x |"
        )
        self.assertEqual(problems_of(repo(**{ALPHA: text})), [])

    def test_table_without_a_version_column_is_skipped(self) -> None:
        text = "| Програма | Звідки | Для альфи |\n| --- | --- | --- |\n| Gloxi | 0.9 | так |\n"
        self.assertEqual(problems_of(repo(**{ALPHA: text})), [])

    def test_rows_after_the_table_are_not_read(self) -> None:
        text = self.table("| Gloxi | 0.2.0~a1 | x |") + "\nТекст.\n\n| Gloxi | 0.9 | x |\n"
        self.assertEqual(problems_of(repo(**{ALPHA: text})), [])

    def test_missing_alpha_freeze_is_a_warning(self) -> None:
        found = problems_of(repo(**{ALPHA: None}), "alpha-missing")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].warning)

    def test_alpha_versions_are_in_the_table_row_once(self) -> None:
        text = self.table("| Gloxi | 0.2.0~a1 | x |", "| gloxi-session | 0.2.0~a1 | x |")
        _, rows = run(repo(**{ALPHA: text}))
        self.assertEqual(next(r for r in rows if r.component == "gloxi").alpha, ["0.2.0~a1"])

    def test_escaped_bar_in_a_cell_does_not_split_it(self) -> None:
        self.assertEqual(cv.table_cells(r"| a \| b | 1 |"), ["a \\| b", "1"])
        self.assertEqual(cv.table_cells("not a row"), [])


class MainTests(CheckTestCase):
    def call(self, argv: list[str], **patches: object) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            mock.patch.multiple(cv, **patches),
        ):
            code = cv.main(argv)
        return code, out.getvalue(), err.getvalue()

    def run_main(self, files: dict[str, str | None]) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            tracked = []
            for rel, text in files.items():
                if text is not None:
                    path = Path(tmp, rel)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                    tracked.append(rel)
            return self.call([], ROOT=Path(tmp), tracked_files=lambda _root: tracked)

    def test_good_repo_exits_0_with_the_table_on_stdout(self) -> None:
        code, out, err = self.run_main(repo())
        self.assertEqual((code, err), (0, ""))
        self.assertIn("| gloxi", out)
        self.assertIn("0 errors, 0 warnings", out)

    def test_errors_exit_1_and_go_to_stderr(self) -> None:
        code, out, err = self.run_main(repo(**{GLOXI_SPEC: spec("gloxi", "0.2.0")}))
        self.assertEqual(code, 1)
        self.assertIn(f"{GLOXI_SPEC}: Version 0.2.0 does not match", err)
        self.assertIn("1 errors, 1 warnings", out)

    def test_warnings_alone_exit_0(self) -> None:
        code, out, err = self.run_main(repo(**{METAINFO: metainfo(("0.1.0~a1", "2026-11-04"))}))
        self.assertEqual(code, 0)
        self.assertIn("warning:", err)
        self.assertIn("0 errors, 1 warnings", out)

    def test_arguments_are_a_wrong_call(self) -> None:
        code, out, err = self.call(["gloxi"], ROOT=ROOT)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("usage:", err)

    def test_help_exits_0(self) -> None:
        for flag in ("-h", "--help"):
            self.assertEqual(self.call([flag], ROOT=ROOT)[0], 0)

    def test_git_that_fails_is_a_wrong_call(self) -> None:
        def fail(_root: Path) -> list[str]:
            raise subprocess.CalledProcessError(128, "git")

        code, _, err = self.call([], tracked_files=fail)
        self.assertEqual(code, 2)
        self.assertIn("cannot list files with git ls-files", err)

    def test_git_that_is_missing_is_a_wrong_call(self) -> None:
        def missing(_root: Path) -> list[str]:
            raise FileNotFoundError("git")

        self.assertEqual(self.call([], tracked_files=missing)[0], 2)


class RepoTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / ".git").exists(), "not a git checkout")
    def test_tracked_repo_has_no_errors(self) -> None:
        problems, rows = cv.check_repo(ROOT, cv.tracked_files(ROOT))
        errors = [str(p) for p in problems if not p.warning]
        self.assertEqual(errors, [])
        self.assertTrue(any(r.component == "gloxi" for r in rows))

    @unittest.skipUnless((ROOT / ".git").exists(), "not a git checkout")
    def test_legacy_entries_still_match_a_problem(self) -> None:
        # A LEGACY pair that no longer happens must be removed from the table.
        with mock.patch.object(cv, "LEGACY", frozenset()):
            problems, _ = cv.check_repo(ROOT, cv.tracked_files(ROOT))
        found = {(p.path, p.rule) for p in problems if not p.warning}
        self.assertEqual(found, set(cv.LEGACY))


if __name__ == "__main__":
    unittest.main()
