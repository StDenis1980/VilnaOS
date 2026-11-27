#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_licence_mix.py (can the licences of the repo be mixed).

Run: python3 tools/test_check_licence_mix.py
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

import check_licence_mix as clm  # noqa: E402  (after sys.path.insert above)

GPL = "GPL-3.0-or-later"
PYPI = '{ registry = "https://pypi.org/simple" }'
SHA = "sha256:" + "ab" * 32
CARGO_SUM = "cd" * 32
CRATES = "registry+https://github.com/rust-lang/crates.io-index"

LICENSING = """# Licensing

| Licence | Where |
| --- | --- |
| **GPL-3.0-or-later** ([text](LICENSES/GPL-3.0-or-later.txt)) | the default: `image/`, `core/` |
| **Apache-2.0** ([text](LICENSES/Apache-2.0.txt)) | shared: `mcp/`, `gateway/`, `sdk/` |
"""
APACHE_TEXT = "                                 Apache License\n Version 2.0, January 2004\n"


# --- builders for small fake repos -------------------------------------------------


def pyproject(
    name: str, lic: str | None = GPL, deps: tuple[str, ...] = (), extra: str = "", header: str = GPL
) -> str:
    lines = [f"# SPDX-License-Identifier: {header}", "[project]", f'name = "{name}"']
    lines.append('version = "1.0.0"')
    if lic is not None:
        lines.append(f'license = "{lic}"')
    lines.append("dependencies = [" + ", ".join(f'"{d}"' for d in deps) + "]")
    return "\n".join(lines) + "\n" + extra


def uv_package(name: str, source: str, version: str = "1.0.0", extra: str = "") -> str:
    local = "editable" in source
    text = f'[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {source}\n{extra}'
    if not local:
        text += (
            f'sdist = {{ url = "https://files.pythonhosted.org/{name}.tar.gz", hash = "{SHA}" }}\n'
        )
    return text + "\n"


def uv_lock(*packages: str) -> str:
    return 'version = 1\nrevision = 3\nrequires-python = ">=3.11"\n\n' + "".join(packages)


def dep_list(*entries: str) -> str:
    return "dependencies = [\n" + "".join(f"    {e},\n" for e in entries) + "]\n"


def local_dep(name: str, path: str) -> str:
    return f'{{ name = "{name}", source = {{ editable = "{path}" }} }}'


def python_project(folder: str, name: str, lic: str, deps: tuple[str, ...] = ()) -> dict[str, str]:
    """A project with an uv.lock: ``deps`` are (name, relative path) of other own projects."""
    entries = tuple(local_dep(n, p) for n, p in deps)
    lock = uv_lock(uv_package(name, '{ editable = "." }', extra=dep_list(*entries) if deps else ""))
    for dep_name, path in deps:
        lock += uv_package(dep_name, f'{{ editable = "{path}" }}')
    return {
        f"{folder}/pyproject.toml": pyproject(name, lic, header=lic),
        f"{folder}/uv.lock": lock,
    }


def base_repo() -> dict[str, str]:
    return {
        "LICENSING.md": LICENSING,
        "mcp/LICENSE": APACHE_TEXT,
        "gateway/LICENSE": APACHE_TEXT,
        "sdk/LICENSE": APACHE_TEXT,
    }


def cargo_toml(name: str, lic: str, deps: str = "") -> str:
    return (
        f'# SPDX-License-Identifier: {lic}\n[package]\nname = "{name}"\nversion = "0.1.0"\n'
        f'license = "{lic}"\n\n[dependencies]\n{deps}'
    )


def spec(name: str, lic: str, requires: tuple[str, ...] = (), extra: str = "") -> str:
    lines = [f"Name:           {name}", "Version:        1.0", f"License:        {lic}"]
    lines += [f"Requires:       {r}" for r in requires]
    return "\n".join(lines) + "\n" + extra + "\n%description\nx\n"


class Repo:
    def __init__(self, test: unittest.TestCase, files: dict[str, str]) -> None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for path, text in files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        self.paths = sorted(files)

    def check(self) -> clm.Report:
        return clm.check(self.root, self.paths)


def problems(report: clm.Report, rule: str | None = None, warning: bool | None = None) -> list[str]:
    return [
        str(p)
        for p in report.problems
        if (rule is None or p.rule == rule) and (warning is None or p.warning == warning)
    ]


def errors(report: clm.Report) -> list[str]:
    return problems(report, warning=False)


# --- the licence tables and expressions --------------------------------------------


class ExpressionTests(unittest.TestCase):
    def test_normalise_id(self) -> None:
        cases = {
            "GPL-2.0+": "gpl-2.0-or-later",
            "GPL-2.0": "gpl-2.0-only",
            "GPL-3.0": "gpl-3.0-only",
            "LGPL-2.1": "lgpl-2.1-only",
            "AGPL-3.0+": "agpl-3.0-or-later",
            "GPL-3.0-or-later": "gpl-3.0-or-later",
            "MIT": "mit",
            "Apache-2.0": "apache-2.0",
            "BSD-3-Clause": "bsd-3-clause",
        }
        for given, want in cases.items():
            with self.subTest(given):
                self.assertEqual(clm.normalise_id(given), want)

    def test_category(self) -> None:
        cases = {
            "MIT": "permissive",
            "mit": "permissive",
            "Apache-2.0": "permissive",
            "LGPL-3.0-only": "weak",
            "MPL-2.0": "weak",
            "GPL-3.0-or-later": "strong",
            "GPL-2.0+": "strong",
            "AGPL-3.0-only": "strong",
            "CC-BY-SA-4.0": "review",
            "SSPL-1.0": "review",
            "Something-1.0": "unknown",
            "LicenseRef-custom": "unknown",
        }
        for lic, want in cases.items():
            with self.subTest(lic):
                self.assertEqual(clm.category(lic), want)
        self.assertEqual(clm.category("GPL-2.0-only", "Classpath-exception-2.0"), "review")
        self.assertEqual(clm.category("MIT", "LLVM-exception"), "review")

    def test_apache_target(self) -> None:
        cases = [
            ("Apache-2.0", clm.OK),
            ("MIT", clm.OK),
            ("MIT OR GPL-3.0-or-later", clm.OK),
            ("GPL-3.0-or-later OR MIT", clm.OK),
            ("MIT AND GPL-3.0-or-later", clm.BAD),
            ("GPL-3.0-or-later", clm.BAD),
            ("GPL-2.0+", clm.BAD),
            ("GPL-2.0-only OR GPL-3.0-only", clm.BAD),
            ("AGPL-3.0-only", clm.BAD),
            ("LGPL-3.0-only", clm.OK),
            ("MPL-2.0 AND MIT", clm.OK),
            ("GPL-2.0-only WITH Classpath-exception-2.0", clm.REVIEW),
            ("CC-BY-SA-4.0", clm.REVIEW),
            ("Foo-1.0", clm.UNKNOWN),
            ("LicenseRef-x", clm.UNKNOWN),
            ("(MIT OR Foo-1.0) AND Apache-2.0", clm.OK),
            ("Foo-1.0 AND GPL-3.0-only", clm.BAD),
            ("Foo-1.0 OR GPL-3.0-only", clm.UNKNOWN),
            ("MIT AND Foo-1.0", clm.UNKNOWN),
            ("MIT OR (Apache-2.0 AND GPL-3.0-only)", clm.OK),
            ("(Apache-2.0 OR MIT) AND Unicode-3.0", clm.OK),
            ("not a licence ((", clm.UNKNOWN),
            ("", clm.UNKNOWN),
            ("NOASSERTION", clm.UNKNOWN),
        ]
        for expr, want in cases:
            with self.subTest(expr):
                self.assertEqual(clm.fit(expr, "apache"), want)

    def test_gpl3_target(self) -> None:
        cases = [
            ("GPL-2.0-only", clm.BAD),
            ("GPL-2.0", clm.BAD),
            ("GPL-2.0-only OR MIT", clm.OK),
            ("GPL-2.0-only AND MIT", clm.BAD),
            ("GPL-2.0-or-later", clm.OK),
            ("GPL-2.0+", clm.OK),
            ("AGPL-3.0-only", clm.OK),
            ("Apache-2.0", clm.OK),
            ("LGPL-2.1-only", clm.OK),
            ("EPL-2.0", clm.OK),
            ("EPL-1.0", clm.REVIEW),
            ("CDDL-1.0", clm.REVIEW),
            ("Foo-1.0", clm.UNKNOWN),
        ]
        for expr, want in cases:
            with self.subTest(expr):
                self.assertEqual(clm.fit(expr, "gpl3"), want)

    def test_parse_expression(self) -> None:
        self.assertEqual(clm.parse_expression("MIT"), ("id", "MIT", None))
        self.assertEqual(
            clm.parse_expression("MIT OR Apache-2.0 AND ISC"),
            (
                "or",
                [("id", "MIT", None), ("and", [("id", "Apache-2.0", None), ("id", "ISC", None)])],
            ),
        )
        self.assertEqual(
            clm.parse_expression("GPL-2.0-only WITH Classpath-exception-2.0"),
            ("id", "GPL-2.0-only", "Classpath-exception-2.0"),
        )
        self.assertEqual(
            clm.parse_expression("(MIT OR ISC) AND Zlib"),
            ("and", [("or", [("id", "MIT", None), ("id", "ISC", None)]), ("id", "Zlib", None)]),
        )
        for bad in ("", "  ", "MIT OR", "(MIT", "MIT AND AND ISC", "a b"):
            with self.subTest(bad):
                self.assertIsNone(clm.parse_expression(bad))

    def test_licence_ids_and_gpl_only(self) -> None:
        self.assertEqual(
            clm.licence_ids("MIT OR (Apache-2.0 AND GPL-2.0+)"),
            {"mit", "apache-2.0", "gpl-2.0-or-later"},
        )
        self.assertEqual(clm.licence_ids("???"), set())
        self.assertTrue(clm.gpl_only("GPL-3.0-or-later"))
        self.assertFalse(clm.gpl_only("MIT OR GPL-3.0-or-later"))
        self.assertFalse(clm.gpl_only("NOASSERTION"))
        self.assertFalse(clm.gpl_only("Foo-1.0"))

    def test_sbom_helpers_are_imported_not_copied(self) -> None:
        for name in ("split_license", "collect_components", "link_locks", "parse_licensing"):
            self.assertTrue(hasattr(clm.sbom, name), name)
            self.assertFalse(hasattr(clm, name), f"{name} was copied")


# --- the reading helpers -----------------------------------------------------------


class HelperTests(unittest.TestCase):
    def test_lock_licence(self) -> None:
        self.assertEqual(clm.lock_licence({"license": "MIT"}), "MIT")
        self.assertEqual(clm.lock_licence({"license": {"text": "BSD-3-Clause"}}), "BSD-3-Clause")
        self.assertEqual(clm.lock_licence({"license-expression": "MIT OR ISC"}), "MIT OR ISC")
        self.assertEqual(clm.lock_licence({"license-expression": "ISC", "license": "MIT"}), "ISC")
        for none in ({}, {"license": 5}, {"license": ""}, {"license": "((("}, {"license": []}):
            with self.subTest(str(none)):
                self.assertIsNone(clm.lock_licence(none))

    def test_requirement_names(self) -> None:
        self.assertEqual(
            clm.requirement_names(
                ["Foo_Bar>=1", "baz[extra]==2 ; python_version>'3'", 7, "", "-e x"]
            ),
            ["foo-bar", "baz"],
        )
        self.assertEqual(clm.requirement_names("not a list"), [])

    def test_declared_python_names(self) -> None:
        data = {
            "project": {
                "dependencies": ["a>=1"],
                "optional-dependencies": {"x": ["b"], "y": ["c"]},
            },
            "dependency-groups": {"dev": ["d", {"include-group": "x"}]},
        }
        self.assertEqual(clm.declared_python_names(data), ["a", "b", "c", "d"])
        self.assertEqual(clm.declared_python_names({}), [])
        self.assertEqual(clm.declared_python_names({"project": "x", "dependency-groups": 3}), [])

    def test_declared_paths(self) -> None:
        uv = {"tool": {"uv": {"sources": {"a": {"path": "../a"}, "b": {"git": "x"}, "c": 1}}}}
        self.assertEqual(clm.declared_paths(uv, "mcp/x"), ["mcp/a"])
        cargo = {
            "dependencies": {"a": {"path": "../a"}, "b": "1.0", "c": {"version": "1"}},
            "dev-dependencies": {"d": {"path": "../../d"}},
            "build-dependencies": {"e": {"path": "e"}},
            "target": {"cfg(unix)": {"dependencies": {"f": {"path": "../f"}}}},
            "workspace": {"dependencies": {"g": {"path": "crates/g"}}},
        }
        self.assertEqual(
            sorted(clm.declared_paths(cargo, "rs/app")),
            sorted(["rs/a", "d", "rs/app/e", "rs/f", "rs/app/crates/g"]),
        )
        self.assertEqual(clm.declared_paths({}, ""), [])

    def test_spec_requires(self) -> None:
        text = (
            "Name: x\nRequires:       python3 >= 3.11\nRequires: a, b >= 2 c\n"
            "Requires(post): d\nRequires: %{python_module e}\nRecommends: f\nSuggests: g\n"
            "%description\nRequires: nope\n"
        )
        self.assertEqual(clm.spec_requires(text), ["python3", "a", "b", "c", "d"])
        self.assertEqual(clm.spec_requires(""), [])
        self.assertEqual(clm.spec_requires("Requires: a\nRequires: a\n"), ["a"])

    def test_imported_roots(self) -> None:
        source = (
            "import os, gloxi.sub\nfrom herkos.x import y\nfrom . import z\nfrom .q import r\n"
            "def f():\n    import late as l\n    from pkg import (a,\n b)\n"
        )
        self.assertEqual(
            sorted(name for _, name in clm.imported_roots(source)),
            ["gloxi", "herkos", "late", "os", "pkg"],
        )
        with self.assertRaises(SyntaxError):
            clm.imported_roots("def (:")


# --- the checks on fake repos ------------------------------------------------------


class CleanRepoTests(unittest.TestCase):
    def test_a_sound_repo_has_no_errors(self) -> None:
        files = base_repo()
        files |= python_project("core/app", "app", GPL, (("lib", "../../mcp/lib"),))
        files |= python_project("mcp/lib", "lib", "Apache-2.0")
        report = Repo(self, files).check()
        self.assertEqual(errors(report), [])
        self.assertEqual(report.components, 2)

    def test_gpl_may_use_apache_and_apache_may_use_apache(self) -> None:
        files = base_repo()
        files |= python_project("mcp/a", "a", "Apache-2.0", (("b", "../b"),))
        files |= python_project("mcp/b", "b", "Apache-2.0")
        files |= python_project("core/g", "g", GPL, (("a", "../../mcp/a"),))
        self.assertEqual(errors(Repo(self, files).check()), [])

    def test_no_files_no_problems(self) -> None:
        report = Repo(self, {"README.md": "x\n"}).check()
        self.assertEqual((report.problems, report.components, report.third_party), ([], 0, 0))


class ApacheNeedsGplTests(unittest.TestCase):
    def test_lock_file_dependency(self) -> None:
        files = base_repo()
        files |= python_project("mcp/a", "a", "Apache-2.0", (("g", "../../core/g"),))
        files |= python_project("core/g", "g", GPL)
        found = errors(Repo(self, files).check())
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("mcp/a/pyproject.toml: "), found[0])
        self.assertIn("reaches GPL-3.0-or-later code of the repo: a -> g [lock file]", found[0])
        self.assertNotIn("warning", found[0])

    def test_declared_dependency_without_a_lock(self) -> None:
        files = base_repo()
        files["mcp/a/pyproject.toml"] = pyproject("a", "Apache-2.0", ("g>=1",), header="Apache-2.0")
        files["core/g/pyproject.toml"] = pyproject("g", GPL)
        found = problems(Repo(self, files).check(), "apache-gpl-dep", False)
        self.assertEqual(len(found), 1)
        self.assertIn("a -> g [dependencies]", found[0])

    def test_name_is_read_by_pep_503(self) -> None:
        files = base_repo()
        files["mcp/a/pyproject.toml"] = pyproject(
            "a", "Apache-2.0", ("Vilna_Gpl.Lib",), header="Apache-2.0"
        )
        files["core/g/pyproject.toml"] = pyproject("vilna-gpl-lib", GPL)
        self.assertEqual(len(problems(Repo(self, files).check(), "apache-gpl-dep")), 1)

    def test_uv_sources_path(self) -> None:
        files = base_repo()
        files["mcp/a/pyproject.toml"] = pyproject(
            "a",
            "Apache-2.0",
            extra='[tool.uv.sources]\nx = { path = "../../core/g" }\n',
            header="Apache-2.0",
        )
        files["core/g/pyproject.toml"] = pyproject("g", GPL)
        found = problems(Repo(self, files).check(), "apache-gpl-dep")
        self.assertEqual(len(found), 1)
        self.assertIn("[path dependency]", found[0])

    def test_through_another_apache_component(self) -> None:
        files = base_repo()
        files |= python_project("mcp/a", "a", "Apache-2.0", (("b", "../b"), ("g", "../../core/g")))
        files |= python_project("mcp/b", "b", "Apache-2.0", (("g", "../../core/g"),))
        files |= python_project("core/g", "g", GPL)
        found = problems(Repo(self, files).check(), "apache-gpl-dep")
        self.assertEqual(
            sorted(p.split(":")[0] for p in found), ["mcp/a/pyproject.toml", "mcp/b/pyproject.toml"]
        )

    def test_chain_is_shown(self) -> None:
        files = base_repo()
        files["mcp/a/pyproject.toml"] = pyproject("a", "Apache-2.0", ("b",), header="Apache-2.0")
        files["mcp/b/pyproject.toml"] = pyproject("b", "Apache-2.0", ("g",), header="Apache-2.0")
        files["core/g/pyproject.toml"] = pyproject("g", GPL)
        found = [p for p in problems(Repo(self, files).check(), "apache-gpl-dep") if "mcp/a" in p]
        self.assertEqual(len(found), 1)
        self.assertIn("a -> b -> g [dependencies]", found[0])

    def test_gpl_behind_a_gpl_component_is_reported_once(self) -> None:
        files = base_repo()
        files["mcp/a/pyproject.toml"] = pyproject("a", "Apache-2.0", ("g1",), header="Apache-2.0")
        files["core/g1/pyproject.toml"] = pyproject("g1", GPL, ("g2",))
        files["core/g2/pyproject.toml"] = pyproject("g2", GPL)
        found = problems(Repo(self, files).check(), "apache-gpl-dep")
        self.assertEqual(len(found), 1)
        self.assertIn("a -> g1", found[0])

    def test_dual_licensed_component_is_fine(self) -> None:
        files = base_repo()
        files["mcp/a/pyproject.toml"] = pyproject("a", "Apache-2.0", ("d",), header="Apache-2.0")
        files["core/d/pyproject.toml"] = pyproject("d", "Apache-2.0 OR GPL-3.0-or-later")
        self.assertEqual(errors(Repo(self, files).check()), [])

    def test_a_component_that_is_gpl_and_something_else(self) -> None:
        files = base_repo()
        files["mcp/a/pyproject.toml"] = pyproject("a", "Apache-2.0", ("d",), header="Apache-2.0")
        files["core/d/pyproject.toml"] = pyproject("d", "MIT AND GPL-3.0-or-later")
        self.assertEqual(len(problems(Repo(self, files).check(), "apache-gpl-dep")), 1)

    def test_apache_part_by_expression_outside_a_folder_rule(self) -> None:
        files = base_repo()
        files["tools/a/pyproject.toml"] = pyproject("a", "Apache-2.0", ("g",), header="Apache-2.0")
        files["core/g/pyproject.toml"] = pyproject("g", GPL)
        self.assertEqual(len(problems(Repo(self, files).check(), "apache-gpl-dep")), 1)

    def test_a_gpl_component_is_not_an_apache_part(self) -> None:
        files = base_repo()
        files["core/a/pyproject.toml"] = pyproject("a", GPL, ("g",))
        files["core/g/pyproject.toml"] = pyproject("g", GPL)
        self.assertEqual(errors(Repo(self, files).check()), [])

    def test_cargo_path_dependency(self) -> None:
        files = base_repo()
        files["sdk/rs/a/Cargo.toml"] = cargo_toml(
            "a", "Apache-2.0", 'g = { path = "../../../core/rs/g" }\n'
        )
        files["core/rs/g/Cargo.toml"] = cargo_toml("g", GPL)
        found = problems(Repo(self, files).check(), "apache-gpl-dep", False)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("sdk/rs/a/Cargo.toml: "))
        self.assertIn("a -> g [path dependency]", found[0])

    def test_cargo_lock_dependency(self) -> None:
        files = base_repo()
        files["sdk/rs/Cargo.toml"] = cargo_toml("a", "Apache-2.0")
        files["sdk/rs/g/Cargo.toml"] = cargo_toml("g", GPL)
        files["sdk/rs/Cargo.lock"] = (
            'version = 4\n\n[[package]]\nname = "a"\nversion = "0.1.0"\n'
            'dependencies = [\n "g",\n]\n\n[[package]]\nname = "g"\nversion = "0.1.0"\n'
        )
        found = problems(Repo(self, files).check(), "apache-gpl-dep", False)
        self.assertEqual(len(found), 1)
        self.assertIn("[lock file]", found[0])

    def test_gpl_in_apache_folder_is_an_error_not_a_duplicate_warning(self) -> None:
        files = base_repo()
        files["mcp/x/pyproject.toml"] = pyproject("x", GPL)
        report = Repo(self, files).check()
        self.assertEqual(len(problems(report, "apache-gpl-header", False)), 1)
        self.assertEqual(problems(report, "sbom"), [])

    def test_a_gpl_component_in_an_apache_folder_counts_as_an_apache_part(self) -> None:
        files = base_repo()
        files |= python_project("mcp/a", "a", GPL, (("g", "../../core/g"),))
        files |= python_project("core/g", "g", GPL)
        report = Repo(self, files).check()
        self.assertEqual(len(problems(report, "apache-gpl-dep", False)), 1)
        self.assertEqual(len(problems(report, "apache-gpl-header", False)), 1)

    def test_rpm_requires_a_gpl_rpm_is_a_warning(self) -> None:
        files = base_repo()
        files["obs/packages/a/a.spec"] = spec("a", "Apache-2.0", ("g >= 1.0", "python3"))
        files["obs/packages/g/g.spec"] = spec("g", GPL)
        report = Repo(self, files).check()
        found = problems(report, "rpm-requires-gpl")
        self.assertEqual(len(found), 1)
        self.assertIn("warning", found[0])
        self.assertIn("a -> g [Requires]", found[0])
        self.assertEqual(errors(report), [])

    def test_rpm_recommends_is_not_looked_at(self) -> None:
        files = base_repo()
        files["obs/packages/a/a.spec"] = spec("a", "Apache-2.0")
        files["obs/packages/a/a.spec"] = files["obs/packages/a/a.spec"].replace(
            "%description", "Recommends:     g\nSuggests:       g\n%description"
        )
        files["obs/packages/g/g.spec"] = spec("g", GPL)
        self.assertEqual(problems(Repo(self, files).check(), "rpm-requires-gpl"), [])

    def test_rpm_requires_a_requires_in_a_section_is_ignored(self) -> None:
        files = base_repo()
        files["obs/packages/a/a.spec"] = spec(
            "a", "Apache-2.0", extra="%package -n a-sub\nRequires: g\n"
        )
        files["obs/packages/g/g.spec"] = spec("g", GPL)
        self.assertEqual(problems(Repo(self, files).check(), "rpm-requires-gpl"), [])

    def test_gpl_rpm_may_require_apache_rpm(self) -> None:
        files = base_repo()
        files["obs/packages/g/g.spec"] = spec("g", GPL, ("a",))
        files["obs/packages/a/a.spec"] = spec("a", "Apache-2.0")
        self.assertEqual(problems(Repo(self, files).check(), "rpm-requires-gpl"), [])


class ThirdPartyTests(unittest.TestCase):
    def repo(self, lic_line: str, lic: str = "Apache-2.0") -> Repo:
        files = base_repo()
        folder = "mcp/a" if lic == "Apache-2.0" else "core/a"
        dep = uv_package("lib", PYPI, extra=lic_line)
        entries = dep_list('{ name = "lib" }')
        files[f"{folder}/pyproject.toml"] = pyproject("a", lic, ("lib",), header=lic)
        files[f"{folder}/uv.lock"] = uv_lock(
            uv_package("a", '{ editable = "." }', extra=entries), dep
        )
        return Repo(self, files)

    def test_known_gpl_package_under_apache_is_an_error(self) -> None:
        found = errors(self.repo('license = "GPL-3.0-only"\n').check())
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("mcp/a/pyproject.toml: "))
        self.assertIn("pypi package lib 1.0.0 under GPL-3.0-only", found[0])

    def test_license_table_and_expression_keys(self) -> None:
        for line in (
            'license = { text = "AGPL-3.0-or-later" }\n',
            'license-expression = "GPL-2.0+"\n',
        ):
            with self.subTest(line):
                self.assertEqual(len(problems(self.repo(line).check(), "apache-gpl-third")), 1)

    def test_permissive_and_dual_packages_are_fine(self) -> None:
        for lic in ("MIT", "BSD-3-Clause", "MIT OR GPL-3.0-or-later", "LGPL-2.1-only", "MPL-2.0"):
            with self.subTest(lic):
                report = self.repo(f'license = "{lic}"\n').check()
                self.assertEqual(problems(report, "apache-gpl-third"), [])
                self.assertEqual(report.unknown, {})

    def test_no_licence_in_the_lock_is_unknown_and_never_an_error(self) -> None:
        report = self.repo("").check()
        self.assertEqual(errors(report), [])
        self.assertEqual(
            list(report.unknown),
            [
                (
                    "pypi",
                    "lib",
                    "1.0.0",
                )
            ],
        )
        found = problems(report, "unknown-licence")
        self.assertEqual(
            found,
            [
                "mcp/a/uv.lock: warning: 1 third-party packages carry no licence "
                "in the lock: unknown"
            ],
        )

    def test_licence_this_check_does_not_know_is_a_warning(self) -> None:
        report = self.repo('license = "Frobnicate-1.0"\n').check()
        self.assertEqual(errors(report), [])
        found = problems(report, "unknown-licence", True)
        self.assertEqual(len(found), 1)
        self.assertIn("'Frobnicate-1.0' is not known", found[0])

    def test_licence_that_needs_a_look_is_a_warning(self) -> None:
        report = self.repo('license = "CC-BY-SA-4.0"\n').check()
        self.assertEqual(errors(report), [])
        self.assertEqual(len(problems(report, "licence-review", True)), 1)

    def test_gpl_with_exception_is_a_warning(self) -> None:
        report = self.repo('license = "GPL-2.0-only WITH Classpath-exception-2.0"\n').check()
        self.assertEqual(errors(report), [])
        self.assertEqual(len(problems(report, "licence-review", True)), 1)

    def test_a_licence_that_is_no_expression_counts_as_unknown(self) -> None:
        report = self.repo('license = "see the file"\n').check()
        self.assertEqual(errors(report), [])
        self.assertEqual(len(report.unknown), 1)

    def test_gpl_package_under_a_gpl_component_is_fine(self) -> None:
        self.assertEqual(errors(self.repo('license = "GPL-3.0-only"\n', GPL).check()), [])

    def test_gpl2_only_under_gpl3_is_an_error(self) -> None:
        found = problems(self.repo('license = "GPL-2.0-only"\n', GPL).check(), "gpl2-only")
        self.assertEqual(len(found), 1)
        self.assertIn("cannot be combined with GPL-3.0", found[0])

    def test_gpl2_or_later_and_dual_under_gpl3_are_fine(self) -> None:
        for lic in ("GPL-2.0-or-later", "GPL-2.0-only OR MIT", "LGPL-2.1-only"):
            with self.subTest(lic):
                self.assertEqual(errors(self.repo(f'license = "{lic}"\n', GPL).check()), [])

    def test_cargo_lock_licence(self) -> None:
        files = base_repo()
        files["sdk/rs/Cargo.toml"] = cargo_toml("a", "Apache-2.0")
        files["sdk/rs/Cargo.lock"] = (
            'version = 4\n\n[[package]]\nname = "a"\nversion = "0.1.0"\ndependencies = ["x"]\n\n'
            f'[[package]]\nname = "x"\nversion = "1.0.0"\nsource = "{CRATES}"\n'
            f'checksum = "{CARGO_SUM}"\nlicense = "GPL-3.0-only"\n'
        )
        found = problems(Repo(self, files).check(), "apache-gpl-third", False)
        self.assertEqual(len(found), 1)
        self.assertIn("cargo package x 1.0.0", found[0])

    def test_pypi_names_are_compared_normalised(self) -> None:
        files = base_repo()
        files["mcp/a/pyproject.toml"] = pyproject(
            "a", "Apache-2.0", ("My_Lib",), header="Apache-2.0"
        )
        files["mcp/a/uv.lock"] = uv_lock(
            uv_package("a", '{ editable = "." }', extra=dep_list('{ name = "my-lib" }')),
            uv_package("My_Lib", PYPI, extra='license = "GPL-3.0-only"\n'),
        )
        self.assertEqual(len(problems(Repo(self, files).check(), "apache-gpl-third")), 1)

    def test_same_package_in_two_locks_is_listed_once(self) -> None:
        files = base_repo()
        for folder in ("mcp/a", "mcp/b"):
            name = folder.split("/")[1]
            files[f"{folder}/pyproject.toml"] = pyproject(
                name, "Apache-2.0", ("lib",), header="Apache-2.0"
            )
            files[f"{folder}/uv.lock"] = uv_lock(
                uv_package(name, '{ editable = "." }', extra=dep_list('{ name = "lib" }')),
                uv_package("lib", PYPI),
            )
        report = Repo(self, files).check()
        self.assertEqual(len(report.unknown), 1)
        self.assertEqual(report.third_party, 1)
        self.assertEqual(
            sorted(next(iter(report.unknown.values()))), ["mcp/a/uv.lock", "mcp/b/uv.lock"]
        )
        self.assertEqual(len(problems(report, "unknown-licence")), 2)


class SourceImportTests(unittest.TestCase):
    def files(self, code: str, path: str = "mcp/a/src/vilna_a/server.py") -> dict[str, str]:
        files = base_repo()
        files["mcp/a/pyproject.toml"] = pyproject("a", "Apache-2.0", header="Apache-2.0")
        files["mcp/a/src/vilna_a/__init__.py"] = "# SPDX-License-Identifier: Apache-2.0\n"
        files["core/g/pyproject.toml"] = pyproject("g", GPL)
        files["core/g/src/gloxi/__init__.py"] = "# SPDX-License-Identifier: GPL-3.0-or-later\n"
        files[path] = "# SPDX-License-Identifier: Apache-2.0\n" + code
        return files

    def test_import_of_gpl_code_is_an_error(self) -> None:
        for code in ("import gloxi\n", "from gloxi.x import y\n", "def f():\n    import gloxi.z\n"):
            with self.subTest(code):
                found = errors(Repo(self, self.files(code)).check())
                self.assertEqual(len(found), 1, found)
                self.assertIn("imports gloxi", found[0])
                self.assertTrue(found[0].startswith("mcp/a/src/vilna_a/server.py:"), found[0])

    def test_line_number_is_given(self) -> None:
        found = errors(Repo(self, self.files("\n\nimport gloxi\n")).check())
        self.assertTrue(found[0].startswith("mcp/a/src/vilna_a/server.py:4: "), found[0])

    def test_import_in_tests_is_a_warning(self) -> None:
        files = self.files("import gloxi\n", "mcp/a/tests/test_x.py")
        report = Repo(self, files).check()
        self.assertEqual(errors(report), [])
        self.assertEqual(len(problems(report, "apache-gpl-import", True)), 1)

    def test_other_imports_are_fine(self) -> None:
        code = (
            "import os\nfrom . import x\nfrom .y import z\nimport vilna_a\nfrom vilna_a import q\n"
        )
        self.assertEqual(errors(Repo(self, self.files(code)).check()), [])

    def test_gpl_files_may_import_gpl_and_apache_code(self) -> None:
        files = self.files("import os\n")
        files["core/g/src/gloxi/main.py"] = "import gloxi\nimport vilna_a\n"
        self.assertEqual(errors(Repo(self, files).check()), [])

    def test_a_name_both_ship_is_not_a_finding(self) -> None:
        files = self.files("import shared\n")
        files["mcp/a/src/shared/__init__.py"] = ""
        files["core/g/src/shared/__init__.py"] = ""
        self.assertEqual(errors(Repo(self, files).check()), [])

    def test_a_file_that_is_not_python_is_a_warning(self) -> None:
        report = Repo(self, self.files("def (:\n")).check()
        self.assertEqual(errors(report), [])
        self.assertEqual(len(problems(report, "read", True)), 1)

    def test_dual_licensed_package_may_be_imported(self) -> None:
        files = self.files("import gloxi\n")
        files["core/g/pyproject.toml"] = pyproject("g", "MIT OR GPL-3.0-or-later")
        self.assertEqual(errors(Repo(self, files).check()), [])


class FolderTests(unittest.TestCase):
    def test_gpl_header_in_an_apache_folder(self) -> None:
        files = base_repo()
        files["mcp/a/x.py"] = "# SPDX-License-Identifier: GPL-3.0-or-later\n"
        found = errors(Repo(self, files).check())
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("mcp/a/x.py: header says GPL-3.0-or-later"), found[0])

    def test_other_headers_are_fine(self) -> None:
        files = base_repo()
        files["mcp/a/a.py"] = "# SPDX-License-Identifier: Apache-2.0\n"
        files["mcp/a/b.py"] = "# SPDX-License-Identifier: MIT OR GPL-3.0-or-later\n"
        files["mcp/a/c.py"] = "# SPDX-License-Identifier: MIT\n"
        files["mcp/a/d.py"] = "no header\n"
        files["gateway/e.py"] = "// SPDX-License-Identifier: Apache-2.0\n"
        self.assertEqual(errors(Repo(self, files).check()), [])

    def test_gpl_header_in_a_gpl_folder_is_fine(self) -> None:
        files = base_repo()
        files["core/a.py"] = "# SPDX-License-Identifier: GPL-3.0-or-later\n"
        self.assertEqual(errors(Repo(self, files).check()), [])

    def test_binary_files_are_skipped(self) -> None:
        files = base_repo()
        files["mcp/a/x.png"] = "# SPDX-License-Identifier: GPL-3.0-or-later\n"
        self.assertEqual(errors(Repo(self, files).check()), [])
        repo = Repo(self, base_repo())
        (repo.root / "mcp/blob.bin").write_bytes(b"\xff\xfe\x00 SPDX")
        repo.paths.append("mcp/blob.bin")
        self.assertEqual(errors(repo.check()), [])

    def test_missing_licence_file(self) -> None:
        files = base_repo()
        del files["gateway/LICENSE"]
        files["gateway/x.py"] = "x = 1\n"
        found = errors(Repo(self, files).check())
        self.assertEqual(
            found,
            [
                "gateway/LICENSE: gateway/LICENSE is not tracked, but LICENSING.md gives gateway/ "
                "to Apache-2.0"
            ],
        )

    def test_licence_file_with_other_text(self) -> None:
        files = base_repo()
        files["sdk/LICENSE"] = "GNU GENERAL PUBLIC LICENSE\n"
        found = errors(Repo(self, files).check())
        self.assertEqual(len(found), 1)
        self.assertIn("sdk/LICENSE is not the Apache-2.0 text", found[0])

    def test_empty_folder_needs_no_licence_file(self) -> None:
        self.assertEqual(errors(Repo(self, {"LICENSING.md": LICENSING}).check()), [])

    def test_no_licensing_file_means_no_folder_rules(self) -> None:
        files = {"mcp/a/x.py": "# SPDX-License-Identifier: GPL-3.0-or-later\n"}
        self.assertEqual(errors(Repo(self, files).check()), [])


class ComponentTests(unittest.TestCase):
    def test_declared_nothing_is_a_warning(self) -> None:
        files = base_repo()
        files["core/a/pyproject.toml"] = pyproject("a", None, header="")
        files["core/a/pyproject.toml"] = files["core/a/pyproject.toml"].replace(
            "# SPDX-License-Identifier: \n", ""
        )
        report = Repo(self, files).check()
        self.assertEqual(errors(report), [])
        found = problems(report, "unknown-licence")
        self.assertEqual(found, ["core/a/pyproject.toml: warning: a declares no licence"])

    def test_licence_that_is_not_known(self) -> None:
        files = base_repo()
        files["core/a/pyproject.toml"] = pyproject("a", "Frobnicate-1.0", header=GPL)
        found = problems(Repo(self, files).check(), "unknown-licence", True)
        self.assertEqual(len(found), 1)
        self.assertIn("'Frobnicate-1.0' is not known to this check", found[0])

    def test_licence_that_needs_a_look(self) -> None:
        files = base_repo()
        files["core/a/pyproject.toml"] = pyproject("a", "CC-BY-SA-4.0", header=GPL)
        report = Repo(self, files).check()
        self.assertEqual(errors(report), [])
        self.assertEqual(len(problems(report, "licence-review", True)), 1)

    def test_unreadable_manifest_is_an_error(self) -> None:
        files = base_repo()
        files["core/a/pyproject.toml"] = "[project\n"
        found = problems(Repo(self, files).check(), "sbom", False)
        self.assertEqual(len(found), 1)
        self.assertIn("not valid TOML", found[0])

    def test_problems_are_sorted(self) -> None:
        files = base_repo()
        files |= python_project("mcp/a", "a", "Apache-2.0", (("g", "../../core/g"),))
        files |= python_project("core/g", "g", GPL)
        files["zzz/pyproject.toml"] = pyproject("z", "Frobnicate-1.0", header=GPL)
        paths = [p.path for p in Repo(self, files).check().problems]
        self.assertIn("zzz/pyproject.toml", paths)
        self.assertIn("mcp/a/pyproject.toml", paths)
        self.assertEqual(paths, sorted(paths))


class LegacyTests(unittest.TestCase):
    def test_a_legacy_entry_turns_an_error_into_a_warning(self) -> None:
        files = base_repo()
        files |= python_project("mcp/a", "a", "Apache-2.0", (("g", "../../core/g"),))
        files |= python_project("core/g", "g", GPL)
        repo = Repo(self, files)
        self.assertEqual(len(errors(repo.check())), 1)
        with mock.patch.dict(clm.LEGACY, {("mcp/a/pyproject.toml", "apache-gpl-dep"): "old"}):
            report = repo.check()
        self.assertEqual(errors(report), [])
        found = problems(report, "apache-gpl-dep", True)
        self.assertEqual(len(found), 1)
        self.assertIn("warning: LEGACY: ", found[0])

    def test_the_legacy_table_is_empty_today(self) -> None:
        self.assertEqual(clm.LEGACY, {})


# --- the command line --------------------------------------------------------------


class MainTests(unittest.TestCase):
    def run_main(
        self, args: list[str], files: dict[str, str] | None = None
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        repo = Repo(self, files if files is not None else base_repo())
        argv = [str(repo.root) if a == "{root}" else a for a in args]
        with (
            mock.patch.object(clm, "tracked_files", return_value=repo.paths),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = clm.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_good_repo_exits_0(self) -> None:
        code, out, err = self.run_main(["{root}"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("0 own components, 0 third-party packages: 0 errors, 0 warnings", out)

    def test_error_exits_1_and_prints_it(self) -> None:
        files = base_repo()
        files |= python_project("mcp/a", "a", "Apache-2.0", (("g", "../../core/g"),))
        files |= python_project("core/g", "g", GPL)
        code, out, err = self.run_main(["{root}"], files)
        self.assertEqual(code, 1)
        self.assertIn("mcp/a/pyproject.toml: ", err)
        self.assertIn("1 errors", out)

    def test_warnings_alone_exit_0_and_the_unknown_list_is_printed(self) -> None:
        files = base_repo()
        files["mcp/a/pyproject.toml"] = pyproject("a", "Apache-2.0", ("lib",), header="Apache-2.0")
        files["mcp/a/uv.lock"] = uv_lock(
            uv_package("a", '{ editable = "." }', extra=dep_list('{ name = "lib" }')),
            uv_package("lib", PYPI, version="2.5"),
        )
        code, out, err = self.run_main(["{root}"], files)
        self.assertEqual(code, 0)
        self.assertIn("third-party packages with an unknown licence: 1", out)
        self.assertIn("  pypi lib 2.5  (mcp/a/uv.lock)", out)
        self.assertIn("warning", err)

    def test_help(self) -> None:
        for flag in ("-h", "--help"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(clm.main([flag]), 0)
            self.assertIn("usage", out.getvalue())

    def test_wrong_calls_exit_2(self) -> None:
        for args in (["--bad"], ["a", "b"], ["-x", "y"]):
            with self.subTest(args):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(clm.main(args), 2)
                self.assertIn("usage", err.getvalue())

    def test_not_a_folder_exits_2(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(clm.main(["/definitely/not/here"]), 2)
        self.assertIn("not a folder", err.getvalue())

    def test_no_tracked_files_exits_2(self) -> None:
        code, _, err = self.run_main(["{root}"], {})
        self.assertEqual(code, 2)
        self.assertIn("no tracked files", err)

    def test_git_failure_exits_2(self) -> None:
        for failure in (OSError("no git"), subprocess.CalledProcessError(128, "git")):
            with self.subTest(repr(failure)):
                err = io.StringIO()
                with (
                    tempfile.TemporaryDirectory() as tmp,
                    mock.patch.object(clm, "tracked_files", side_effect=failure),
                    contextlib.redirect_stderr(err),
                ):
                    self.assertEqual(clm.main([tmp]), 2)
                self.assertIn("cannot list files with git ls-files", err.getvalue())

    def test_only_git_ls_files_is_run(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="b\0a\0\0")
        with mock.patch.object(clm.subprocess, "run", return_value=done) as run:
            self.assertEqual(clm.tracked_files(Path(".")), ["a", "b"])
        self.assertEqual(run.call_args.args[0], ["git", "ls-files", "-z"])

    def test_nothing_is_written(self) -> None:
        repo = Repo(self, base_repo())
        before = sorted(p.relative_to(repo.root).as_posix() for p in repo.root.rglob("*"))
        repo.check()
        after = sorted(p.relative_to(repo.root).as_posix() for p in repo.root.rglob("*"))
        self.assertEqual(before, after)


@unittest.skipUnless((ROOT / ".git").exists(), "not a git checkout")
class RealRepoTests(unittest.TestCase):
    def test_the_repo_has_no_licence_mix_errors(self) -> None:
        try:
            files = clm.tracked_files(ROOT)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git is not available")
        report = clm.check(ROOT, files)
        self.assertEqual(errors(report), [])
        self.assertGreater(report.components, 20)
        self.assertGreater(report.third_party, 50)

    def test_the_apache_parts_are_found(self) -> None:
        try:
            files = clm.tracked_files(ROOT)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git is not available")
        rules = clm.sbom.parse_licensing((ROOT / "LICENSING.md").read_text(encoding="utf-8"))
        components = clm.sbom.collect_components(ROOT, files, rules, [])
        apache = {c.source for c in components if clm.is_apache_part(c, rules)}
        self.assertIn("privacy-gateway/pyproject.toml", apache)
        self.assertIn("mcp/files/pyproject.toml", apache)
        self.assertNotIn("gloxi/pyproject.toml", apache)


if __name__ == "__main__":
    unittest.main()
