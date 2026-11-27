#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_deps.py (the check of dependencies and lock files).

Run: python3 tools/test_check_deps.py
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
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_deps as cd  # noqa: E402  (after sys.path.insert above)

PYPI = '{ registry = "https://pypi.org/simple" }'
SHA = "sha256:" + "ab" * 32
CARGO_SUM = "cd" * 32
CRATES = "registry+https://github.com/rust-lang/crates.io-index"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


# --- builders for small fake repos -------------------------------------------------


def pyproject(
    name: str = "demo",
    version: str | None = "1.0.0",
    requires: str | None = ">=3.11",
    deps: tuple[str, ...] = (),
    extra: str = "",
    dynamic: bool = False,
) -> str:
    lines = ["# SPDX-License-Identifier: GPL-3.0-or-later", "[project]", f'name = "{name}"']
    if version is not None and not dynamic:
        lines.append(f'version = "{version}"')
    if dynamic:
        lines.append('dynamic = ["version"]')
    if requires is not None:
        lines.append(f'requires-python = "{requires}"')
    lines.append("dependencies = [" + ", ".join(f'"{d}"' for d in deps) + "]")
    return "\n".join(lines) + "\n" + extra


def package(
    name: str,
    version: str = "1.0",
    source: str = PYPI,
    hashed: bool = True,
    sdist_url: str = "https://files.pythonhosted.org/x.tar.gz",
    hash_value: str = SHA,
    extra: str = "",
) -> str:
    text = f'[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {source}\n{extra}'
    if hashed:
        text += f'sdist = {{ url = "{sdist_url}", hash = "{hash_value}" }}\n'
    return text + "\n"


def own(
    name: str = "demo", version: str = "1.0.0", source: str = '{ editable = "." }', extra: str = ""
) -> str:
    return package(name, version, source, hashed=False, extra=extra)


def uv_lock(*packages: str, requires: str = ">=3.11") -> str:
    head = f'version = 1\nrevision = 3\nrequires-python = "{requires}"\n\n'
    return head + "".join(packages)


def good_python() -> dict[str, str]:
    return {
        "demo/pyproject.toml": pyproject(deps=("requests>=2",)),
        "demo/uv.lock": uv_lock(
            own(extra='dependencies = [\n    { name = "requests" },\n]\n'),
            package("requests", "2.0"),
        ),
    }


def cargo_toml(
    name: str = "app",
    version: str = "version.workspace = true",
    deps: str = "",
    extra: str = "",
) -> str:
    return (
        f'# SPDX-License-Identifier: GPL-3.0-or-later\n[package]\nname = "{name}"\n{version}\n'
        f"{extra}\n[dependencies]\n{deps}\n"
    )


def workspace_toml(members: str = '["app"]', extra: str = "", version: str = "0.1.0") -> str:
    return (
        "# SPDX-License-Identifier: GPL-3.0-or-later\n"
        f"[workspace]\nmembers = {members}\n{extra}\n"
        f'[workspace.package]\nversion = "{version}"\n'
    )


def cargo_package(
    name: str, version: str = "1.0.0", source: str | None = CRATES, checksum: str | None = CARGO_SUM
) -> str:
    text = f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
    if source is not None:
        text += f'source = "{source}"\n'
    if checksum is not None:
        text += f'checksum = "{checksum}"\n'
    return text + "\n"


def cargo_lock(*packages: str) -> str:
    return "# generated\nversion = 4\n\n" + "".join(packages)


def good_rust() -> dict[str, str]:
    return {
        "rs/Cargo.toml": workspace_toml(),
        "rs/app/Cargo.toml": cargo_toml(),
        "rs/Cargo.lock": cargo_lock(
            cargo_package("app", "0.1.0", None, None), cargo_package("serde")
        ),
    }


def run(files: dict[str, str], tracked: list[str] | None = None) -> cd.Report:
    """Write ``files`` to a temp repo and check them (``tracked`` defaults to all of them)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return cd.check_repo(root, sorted(files) if tracked is None else tracked)


def errors_of(files: dict[str, str]) -> list[str]:
    return [str(p) for p in run(files).problems if not p.warning]


def warnings_of(files: dict[str, str]) -> list[str]:
    return [str(p) for p in run(files).problems if p.warning]


def with_file(files: dict[str, str], rel: str, text: str) -> dict[str, str]:
    return {**files, rel: text}


class Base(unittest.TestCase):
    def assertOneError(self, files: dict[str, str], fragment: str, path: str | None = None) -> None:
        errors = errors_of(files)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(fragment, errors[0])
        if path is not None:
            self.assertTrue(errors[0].startswith(path + ": "), errors[0])

    def assertOneWarning(self, files: dict[str, str], fragment: str) -> None:
        self.assertEqual(errors_of(files), [])
        warnings = warnings_of(files)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn(fragment, warnings[0])


# --- versions, names, paths ----------------------------------------------------------


class Helpers(unittest.TestCase):
    def test_names_follow_pep_503(self) -> None:
        for raw in ("Foo_Bar", "foo.bar", "FOO--bar", "foo-_.bar"):
            self.assertEqual(cd.normalize_name(raw), "foo-bar", raw)

    def test_requirement_names(self) -> None:
        cases = {
            "pytest>=8": "pytest",
            "  Ruff >= 0.6": "Ruff",
            "httpx[http2]>=0.27; python_version > '3.8'": "httpx",
            "vilna-mcp-files": "vilna-mcp-files",
            "pkg @ https://example.org/pkg.whl": "pkg",
            "a": "a",
            "zope.interface (>=5)": "zope.interface",
        }
        for requirement, name in cases.items():
            self.assertEqual(cd.requirement_name(requirement), name, requirement)
        for broken in ("", ">=1", "-x", "foo bar", "name.", "https://x"):
            self.assertIsNone(cd.requirement_name(broken), broken)

    def test_same_version_is_pep_440_equality(self) -> None:
        self.assertTrue(cd.same_version("1.0", "1.0.0"))
        self.assertTrue(cd.same_version("0.1.0.dev0", "0.1.0.dev0"))
        self.assertTrue(cd.same_version("0.1.0-dev0", "0.1.0.dev0"))
        self.assertTrue(cd.same_version("1.0RC1", "1.0rc1"))
        self.assertFalse(cd.same_version("1.0", "1.0.1"))
        self.assertFalse(cd.same_version("1.0.dev0", "1.0"))
        self.assertFalse(cd.same_version("1.0", "not a version"))

    def test_requires_python_allows_311(self) -> None:
        allowed = [
            ">=3.11",
            ">=3.9",
            ">=3.8,<4",
            ">3.10",
            ">=3.11,<3.12",
            "==3.11.*",
            "~=3.10",
            "~=3.11.0",
            "==3.*",
            ">=3.11.5",
            "<3.12",
            "<=3.11",
            "!=3.12.*",
            "==3.11.4",
            ">= 3.11 , < 3.14",
            "<3.11.2",
            "==3.11",
            "!=3.11.0",
            ">=3.11.0rc1",
        ]
        refused = [
            ">=3.12",
            "<3.11",
            ">=3.8,<3.11",
            "==3.12.*",
            "~=3.12",
            "==3.10.*",
            ">3.11.99",
            "!=3.11.*",
            "<=3.10",
            "~=3.12.0",
            "==3.12",
            ">=4",
            "~=2.7",
            "~=3.12",
        ]
        for text in allowed:
            self.assertIs(cd.allows_python(text), True, text)
        for text in refused:
            self.assertIs(cd.allows_python(text), False, text)

    def test_requires_python_that_cannot_be_read(self) -> None:
        for text in ("", "3.11", ">=", "python3", ">=3.11,", "~=3", ">=3.*", ">=3.11 <4"):
            self.assertIsNone(cd.allows_python(text), text)

    def test_resolve_inside(self) -> None:
        self.assertEqual(cd.resolve_inside("mcp/files", "."), "mcp/files")
        self.assertEqual(cd.resolve_inside("mcp/files", "../backup"), "mcp/backup")
        self.assertEqual(cd.resolve_inside("mcp/files", "../.."), "")
        self.assertEqual(cd.resolve_inside("", "sub/x"), "sub/x")
        self.assertEqual(cd.resolve_inside("a", "b/../c"), "a/c")
        for bad in ("../../..", "/etc", "C:\\Users\\x", "c:/x", "", "\\abs", "x/../../../.."):
            self.assertIsNone(cd.resolve_inside("mcp/files", bad), bad)
        self.assertIsNone(cd.resolve_inside("", ".."))
        self.assertIsNone(cd.resolve_inside("", "../x"))
        self.assertIsNone(cd.resolve_inside("a", "..\\..\\x"))


# --- Python --------------------------------------------------------------------------


class PythonProject(Base):
    def test_good_project_passes_without_warnings(self) -> None:
        report = run(good_python())
        self.assertEqual(report.problems, [])
        self.assertEqual(len(report.summaries), 1)
        self.assertEqual(report.summaries[0].third_party, 1)

    def test_missing_lock_is_an_error(self) -> None:
        self.assertOneError(
            {"demo/pyproject.toml": pyproject()},
            "no tracked uv.lock next to it",
            "demo/pyproject.toml",
        )

    def test_untracked_lock_does_not_count(self) -> None:
        files = good_python()
        errors = [str(p) for p in run(files, ["demo/pyproject.toml"]).problems if not p.warning]
        self.assertEqual(errors, ["demo/pyproject.toml: no tracked uv.lock next to it"])

    def test_no_entry_for_the_project_itself(self) -> None:
        files = with_file(good_python(), "demo/uv.lock", uv_lock(package("requests", "2.0")))
        self.assertOneError(files, "no entry for the project itself ('demo')", "demo/uv.lock")

    def test_project_entry_name_is_compared_by_pep_503(self) -> None:
        files = good_python()
        files["demo/pyproject.toml"] = pyproject(name="Vilna_Demo.App", deps=("requests",))
        files["demo/uv.lock"] = uv_lock(own(name="vilna-demo-app"), package("requests"))
        self.assertEqual(errors_of(files), [])

    def test_version_of_the_project_must_match(self) -> None:
        files = with_file(
            good_python(), "demo/pyproject.toml", pyproject(version="1.1.0", deps=("requests",))
        )
        self.assertOneError(files, "is 1.0.0 here but 1.1.0 in demo/pyproject.toml", "demo/uv.lock")

    def test_equivalent_spellings_of_the_version_match(self) -> None:
        files = good_python()
        files["demo/pyproject.toml"] = pyproject(version="1.0", deps=("requests",))
        self.assertEqual(errors_of(files), [])

    def test_dynamic_version_is_a_warning(self) -> None:
        files = with_file(
            good_python(), "demo/pyproject.toml", pyproject(dynamic=True, deps=("requests",))
        )
        self.assertOneWarning(files, "the version is dynamic")

    def test_project_without_version_and_not_dynamic(self) -> None:
        files = with_file(
            good_python(), "demo/pyproject.toml", pyproject(version=None, deps=("requests",))
        )
        self.assertOneError(files, "has no version (and it is not dynamic)")

    def test_entry_of_the_name_that_is_not_the_project(self) -> None:
        files = good_python()
        files["demo/uv.lock"] = uv_lock(package("demo", "1.0.0"), package("requests"))
        self.assertOneError(files, "is not the project itself (source registry")

    def test_editable_entry_of_another_folder_is_not_the_project(self) -> None:
        files = good_python()
        files["demo/uv.lock"] = uv_lock(
            own(source='{ editable = "../other" }'), package("requests")
        )
        self.assertOneError(files, "is not the project itself (source editable = '../other')")

    def test_virtual_project_entry_is_fine(self) -> None:
        files = good_python()
        files["demo/uv.lock"] = uv_lock(
            own(source='{ virtual = "." }', extra='dependencies = [{ name = "requests" }]\n'),
            package("requests"),
        )
        self.assertEqual(errors_of(files), [])

    def test_dependency_missing_from_the_lock(self) -> None:
        files = with_file(
            good_python(), "demo/pyproject.toml", pyproject(deps=("requests", "attrs>=23"))
        )
        self.assertOneError(
            files, "project.dependencies: 'attrs' is not in demo/uv.lock", "demo/pyproject.toml"
        )

    def test_optional_dependency_missing_from_the_lock(self) -> None:
        extra = '[project.optional-dependencies]\nvoice = ["sherpa-onnx>=2"]\n'
        files = with_file(
            good_python(), "demo/pyproject.toml", pyproject(deps=("requests",), extra=extra)
        )
        self.assertOneError(files, "project.optional-dependencies.voice: 'sherpa-onnx'")

    def test_dependency_group_missing_from_the_lock(self) -> None:
        extra = '[dependency-groups]\ndev = ["pytest>=8", "ruff"]\n'
        files = with_file(
            good_python(), "demo/pyproject.toml", pyproject(deps=("requests",), extra=extra)
        )
        errors = errors_of(files)
        self.assertEqual(len(errors), 2, errors)
        self.assertIn("dependency-groups.dev: 'pytest'", errors[0])
        self.assertIn("dependency-groups.dev: 'ruff'", errors[1])

    def test_old_style_dev_dependencies_are_checked(self) -> None:
        extra = '[tool.uv]\ndev-dependencies = ["pytest"]\n'
        files = with_file(
            good_python(), "demo/pyproject.toml", pyproject(deps=("requests",), extra=extra)
        )
        self.assertOneError(files, "tool.uv.dev-dependencies: 'pytest'")

    def test_names_in_the_lock_match_by_pep_503_and_ignore_the_version(self) -> None:
        files = good_python()
        files["demo/pyproject.toml"] = pyproject(deps=("Zope_Interface==99", "typing.extensions"))
        files["demo/uv.lock"] = uv_lock(
            own(
                extra=(
                    'dependencies = [{ name = "zope-interface" }, { name = "typing-extensions" }]\n'
                )
            ),
            package("zope-interface", "5.0"),
            package("typing-extensions", "4.0"),
        )
        self.assertEqual(run(files).problems, [])

    def test_group_that_includes_another_group(self) -> None:
        extra = '[dependency-groups]\ntest = ["requests"]\ndev = [{include-group = "test"}]\n'
        files = with_file(
            good_python(), "demo/pyproject.toml", pyproject(deps=("requests",), extra=extra)
        )
        self.assertEqual(errors_of(files), [])

    def test_include_of_a_group_that_does_not_exist(self) -> None:
        extra = '[dependency-groups]\ndev = [{include-group = "nope"}]\n'
        files = with_file(
            good_python(), "demo/pyproject.toml", pyproject(deps=("requests",), extra=extra)
        )
        self.assertOneError(files, "dependency-groups.dev:")

    def test_requirement_without_a_readable_name(self) -> None:
        files = with_file(good_python(), "demo/pyproject.toml", pyproject(deps=("requests", ">=3")))
        self.assertOneError(files, "cannot read the name in '>=3'")

    def test_lock_that_does_not_list_the_dependency_is_a_warning(self) -> None:
        files = good_python()
        files["demo/uv.lock"] = uv_lock(own(), package("requests", "2.0"))
        self.assertOneWarning(files, "the project's own entry does not list it")

    def test_self_reference_in_extras_is_not_a_problem(self) -> None:
        extra = '[project.optional-dependencies]\nall = ["demo[tools]"]\n'
        files = with_file(
            good_python(), "demo/pyproject.toml", pyproject(deps=("requests",), extra=extra)
        )
        self.assertEqual(run(files).problems, [])


class PythonSources(Base):
    def lock_with(self, *packages: str, **kw: str) -> dict[str, str]:
        files = good_python()
        files["demo/uv.lock"] = uv_lock(
            own(extra='dependencies = [{ name = "requests" }]\n'),
            package("requests"),
            *packages,
            **kw,
        )
        return files

    def test_other_registry_is_an_error(self) -> None:
        files = self.lock_with(
            package("evil", source='{ registry = "https://example.org/simple" }')
        )
        self.assertOneError(
            files,
            "package 'evil' 1.0: registry 'https://example.org/simple' is "
            "not https://pypi.org/simple",
            "demo/uv.lock",
        )

    def test_pypi_over_http_is_an_error(self) -> None:
        files = self.lock_with(package("x", source='{ registry = "http://pypi.org/simple" }'))
        self.assertOneError(files, "registry 'http://pypi.org/simple'")

    def test_local_registry_path_is_an_error(self) -> None:
        files = self.lock_with(package("x", source='{ registry = "../simple" }'))
        self.assertOneError(files, "registry '../simple'")

    def test_editable_inside_the_repo_is_fine(self) -> None:
        files = self.lock_with(own("sibling", "0.1", '{ editable = "../sibling" }'))
        self.assertEqual(errors_of(files), [])

    def test_editable_outside_the_repo_is_an_error(self) -> None:
        files = self.lock_with(own("evil", "0.1", '{ editable = "../../evil" }'))
        self.assertOneError(
            files, "editable path '../../evil' is absolute or leaves the repository"
        )

    def test_absolute_paths_are_errors(self) -> None:
        for kind, value in (("virtual", "/opt/x"), ("directory", "C:/x"), ("path", "/x/a.whl")):
            with self.subTest(kind=kind):
                files = self.lock_with(own("evil", "0.1", f'{{ {kind} = "{value}" }}'))
                self.assertOneError(files, f"{kind} path '{value}' is absolute")

    def test_the_repo_root_is_inside_but_one_step_above_it_is_not(self) -> None:
        files = self.lock_with(own("evil", "0.1", '{ editable = ".." }'))
        self.assertEqual(errors_of(files), [])  # demo/.. is the repo root: inside
        files = self.lock_with(own("evil", "0.1", '{ editable = "../.." }'))
        self.assertOneError(files, "editable path '../..' is absolute or leaves")

    def test_git_needs_a_full_commit(self) -> None:
        good = f'{{ git = "https://github.com/a/b?rev=v1#{COMMIT}" }}'
        self.assertEqual(errors_of(self.lock_with(package("g", source=good, hashed=False))), [])
        for source in (
            '{ git = "https://github.com/a/b" }',
            '{ git = "https://github.com/a/b?rev=main" }',
            '{ git = "https://github.com/a/b?rev=main#0123abc" }',
            f'{{ git = "https://github.com/a/b#{COMMIT}00" }}',
            f'{{ git = "https://github.com/a/b#{COMMIT[:-1]}g" }}',
        ):
            with self.subTest(source=source):
                files = self.lock_with(package("g", source=source, hashed=False))
                self.assertOneError(files, "has no full 40-hex commit after '#'")

    def test_other_source_kinds_are_errors(self) -> None:
        for source in ('{ url = "https://example.org/x.whl" }', "{ weird = 1 }"):
            with self.subTest(source=source):
                files = self.lock_with(package("x", source=source, hashed=False))
                self.assertOneError(files, "is not allowed", "demo/uv.lock")

    def test_package_without_a_source(self) -> None:
        text = '[[package]]\nname = "x"\nversion = "1"\n\n'
        self.assertOneError(self.lock_with(text), "package 'x' 1: source None is not allowed")

    def test_package_entry_without_a_name(self) -> None:
        for text in ('[[package]]\nversion = "1"\n\n', '[[package]]\nname = "x"\nversion = 1\n\n'):
            with self.subTest(text=text):
                errors = errors_of(self.lock_with(text))
                self.assertTrue(
                    any("a package entry without a text name or version" in e for e in errors),
                    errors,
                )

    def test_package_without_any_hash_is_a_warning(self) -> None:
        files = self.lock_with(package("bare", hashed=False))
        self.assertOneWarning(files, "package 'bare' 1.0: no hash at all")

    def test_hash_in_a_wheel_is_enough(self) -> None:
        wheels = f'wheels = [{{ url = "https://files.pythonhosted.org/x.whl", hash = "{SHA}" }}]\n'
        files = self.lock_with(package("w", hashed=False, extra=wheels))
        self.assertEqual(run(files).problems, [])

    def test_local_packages_need_no_hash(self) -> None:
        files = self.lock_with(own("sibling", "0.1", '{ editable = "../sibling" }'))
        self.assertEqual(run(files).problems, [])

    def test_malformed_hash_is_a_warning(self) -> None:
        for bad in ("md5:abcd", "sha256:" + "a" * 63, "sha256:" + "A" * 64):
            with self.subTest(bad=bad):
                files = self.lock_with(package("h", hash_value=bad))
                self.assertOneWarning(files, f"hash '{bad}' is not sha256:<64 hex>")

    def test_download_url_must_be_https(self) -> None:
        files = self.lock_with(package("u", sdist_url="http://files.example.org/u.tar.gz"))
        self.assertOneError(files, "download URL 'http://files.example.org/u.tar.gz' is not https")
        wheels = f'wheels = [{{ url = "ftp://x/y.whl", hash = "{SHA}" }}]\n'
        files = self.lock_with(package("v", hashed=False, extra=wheels))
        self.assertOneError(files, "download URL 'ftp://x/y.whl' is not https")


class PythonRequiresAndFiles(Base):
    def test_requires_python_that_excludes_311(self) -> None:
        for requires in (">=3.12", "<3.11", "==3.10.*"):
            with self.subTest(requires=requires):
                files = good_python()
                files["demo/pyproject.toml"] = pyproject(requires=requires, deps=("requests",))
                files["demo/uv.lock"] = files["demo/uv.lock"].replace(">=3.11", requires)
                self.assertOneError(
                    files,
                    f"requires-python '{requires}' does not allow Python 3.11",
                    "demo/pyproject.toml",
                )

    def test_requires_python_that_cannot_be_read(self) -> None:
        files = good_python()
        files["demo/pyproject.toml"] = pyproject(requires="python3", deps=("requests",))
        errors = errors_of(files)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("cannot read requires-python 'python3'", errors[0])

    def test_requires_python_that_is_not_text(self) -> None:
        files = good_python()
        files["demo/pyproject.toml"] = good_python()["demo/pyproject.toml"].replace(
            'requires-python = ">=3.11"', "requires-python = 3.11"
        )
        self.assertOneError(files, "cannot read requires-python 3.11")

    def test_missing_requires_python_is_a_warning(self) -> None:
        files = good_python()
        files["demo/pyproject.toml"] = pyproject(requires=None, deps=("requests",))
        self.assertOneWarning(files, "no requires-python")

    def test_lock_with_another_requires_python_is_a_warning(self) -> None:
        files = good_python()
        files["demo/uv.lock"] = files["demo/uv.lock"].replace(">=3.11", ">=3.12", 1)
        self.assertOneWarning(
            files, "requires-python '>=3.12' differs from the pyproject's '>=3.11'"
        )

    def test_same_requires_python_in_another_spelling_is_fine(self) -> None:
        files = good_python()
        files["demo/pyproject.toml"] = pyproject(requires=">=3.11, <4", deps=("requests",))
        files["demo/uv.lock"] = files["demo/uv.lock"].replace(">=3.11", "<4,>=3.11", 1)
        self.assertEqual(run(files).problems, [])

    def test_invalid_toml_is_one_error(self) -> None:
        files = with_file(good_python(), "demo/uv.lock", "version = [")
        self.assertOneError(files, "invalid TOML", "demo/uv.lock")
        files = with_file(good_python(), "demo/pyproject.toml", "[project")
        self.assertOneError(files, "invalid TOML", "demo/pyproject.toml")

    def test_bom_is_one_clear_error(self) -> None:
        files = with_file(
            good_python(), "demo/pyproject.toml", "\ufeff" + good_python()["demo/pyproject.toml"]
        )
        self.assertOneError(
            files, "starts with a UTF-8 byte order mark (BOM)", "demo/pyproject.toml"
        )

    def test_file_that_is_not_utf8_and_a_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pyproject.toml").write_bytes(b"[project]\nname = '\xff'")
            report = cd.check_repo(Path(tmp), ["pyproject.toml", "gone/pyproject.toml"])
        messages = [str(p) for p in report.problems]
        self.assertEqual(len(messages), 2, messages)
        self.assertTrue(all("cannot read the file" in m for m in messages), messages)

    def test_pyproject_without_a_project_table_is_a_warning(self) -> None:
        files = {"tools/pyproject.toml": "[tool.ruff]\nline-length = 100\n"}
        self.assertOneWarning(files, "no [project] table: not checked")

    def test_project_without_a_name(self) -> None:
        self.assertOneError(
            {"p/pyproject.toml": "[project]\nversion = '1'\n"}, "[project] has no name"
        )

    def test_path_of_tool_uv_sources_stays_inside(self) -> None:
        good = '[tool.uv.sources]\nrequests = { path = "../sibling", editable = true }\n'
        bad = '[tool.uv.sources]\nrequests = { path = "../../sibling", editable = true }\n'
        files = good_python()
        files["demo/pyproject.toml"] = pyproject(deps=("requests",), extra=good)
        self.assertEqual(errors_of(files), [])
        files["demo/pyproject.toml"] = pyproject(deps=("requests",), extra=bad)
        self.assertOneError(files, "tool.uv.sources.requests: path '../../sibling'")
        both = (
            '[tool.uv.sources]\nrequests = [{ path = "/abs", marker = "os_name == \'nt\'" },'
            ' { path = "../ok" }]\n'
        )
        files["demo/pyproject.toml"] = pyproject(deps=("requests",), extra=both)
        self.assertOneError(files, "path '/abs' is absolute")

    def test_project_in_the_root_of_the_repo(self) -> None:
        files = {
            "pyproject.toml": pyproject(deps=("requests",)),
            "uv.lock": uv_lock(
                own(extra='dependencies = [{ name = "requests" }]\n'), package("requests")
            ),
        }
        self.assertEqual(run(files).problems, [])
        files["uv.lock"] += package("evil", source='{ editable = ".." }', hashed=False)
        self.assertOneError(files, "editable path '..' is absolute or leaves the repository")


# --- Rust ----------------------------------------------------------------------------


class RustWorkspace(Base):
    def test_good_workspace_passes_without_warnings(self) -> None:
        report = run(good_rust())
        self.assertEqual(report.problems, [])
        self.assertEqual(report.summaries[0].third_party, 1)

    def test_missing_lock(self) -> None:
        files = good_rust()
        del files["rs/Cargo.lock"]
        self.assertOneError(files, "no tracked Cargo.lock next to it", "rs/Cargo.toml")

    def test_member_missing_in_the_lock(self) -> None:
        files = with_file(good_rust(), "rs/Cargo.lock", cargo_lock(cargo_package("serde")))
        self.assertOneError(
            files, "no entry for the member 'app' (rs/app/Cargo.toml)", "rs/Cargo.lock"
        )

    def test_member_version_from_the_workspace_must_match(self) -> None:
        files = with_file(
            good_rust(),
            "rs/Cargo.lock",
            cargo_lock(cargo_package("app", "0.0.9", None, None), cargo_package("serde")),
        )
        self.assertOneError(files, "the member 'app' is 0.0.9 here but 0.1.0 in rs/app/Cargo.toml")

    def test_member_with_its_own_version(self) -> None:
        files = good_rust()
        files["rs/app/Cargo.toml"] = cargo_toml(version='version = "2.3.4"')
        self.assertOneError(files, "is 0.1.0 here but 2.3.4")
        files["rs/Cargo.lock"] = cargo_lock(cargo_package("app", "2.3.4", None, None))
        self.assertEqual(errors_of(files), [])

    def test_member_with_workspace_version_but_the_workspace_has_none(self) -> None:
        files = good_rust()
        files["rs/Cargo.toml"] = '[workspace]\nmembers = ["app"]\n'
        self.assertOneError(
            files, "version.workspace = true, but [workspace.package] has no version"
        )

    def test_member_without_a_version_is_0_0_0(self) -> None:
        files = good_rust()
        files["rs/app/Cargo.toml"] = cargo_toml(version="")
        files["rs/Cargo.lock"] = cargo_lock(cargo_package("app", "0.0.0", None, None))
        self.assertEqual(errors_of(files), [])

    def test_registry_crate_with_the_member_name_is_not_the_member(self) -> None:
        files = with_file(good_rust(), "rs/Cargo.lock", cargo_lock(cargo_package("app", "0.1.0")))
        self.assertOneError(files, "no entry for the member 'app'")

    def test_member_that_is_not_tracked(self) -> None:
        files = good_rust()
        files["rs/Cargo.toml"] = workspace_toml(members='["app", "ghost"]')
        self.assertOneError(files, "member 'ghost' has no tracked Cargo.toml", "rs/Cargo.toml")

    def test_untracked_member_manifest_does_not_count(self) -> None:
        files = good_rust()
        tracked = ["rs/Cargo.toml", "rs/Cargo.lock"]
        errors = [str(p) for p in run(files, tracked).problems if not p.warning]
        self.assertEqual(errors, ["rs/Cargo.toml: member 'app' has no tracked Cargo.toml"])

    def test_member_globs_and_exclude(self) -> None:
        files = good_rust()
        files["rs/Cargo.toml"] = workspace_toml(
            members='["crates/*"]', extra='exclude = ["crates/old"]'
        )
        files["rs/crates/app/Cargo.toml"] = cargo_toml()
        files["rs/crates/old/Cargo.toml"] = cargo_toml(name="old")
        del files["rs/app/Cargo.toml"]
        self.assertEqual(errors_of(files), [])
        # without the exclude, "old" must be in the lock
        files["rs/Cargo.toml"] = workspace_toml(members='["crates/*"]')
        self.assertOneError(files, "no entry for the member 'old'")

    def test_glob_matches_one_level_only(self) -> None:
        files = good_rust()
        files["rs/Cargo.toml"] = workspace_toml(members='["crates/*"]')
        files["rs/crates/app/Cargo.toml"] = cargo_toml()
        files["rs/crates/app/deep/Cargo.toml"] = cargo_toml(name="deep")  # a crate inside a crate
        del files["rs/app/Cargo.toml"]
        self.assertEqual(errors_of(files), [])

    def test_glob_with_no_match(self) -> None:
        files = good_rust()
        files["rs/Cargo.toml"] = workspace_toml(members='["app", "tools/*"]')
        self.assertOneError(files, "member 'tools/*' has no tracked Cargo.toml")

    def test_member_outside_the_repo(self) -> None:
        files = good_rust()
        files["rs/Cargo.toml"] = workspace_toml(members='["app", "../../x"]')
        self.assertOneError(files, "member '../../x' is absolute or leaves the repository")

    def test_member_without_package(self) -> None:
        files = good_rust()
        files["rs/app/Cargo.toml"] = "[dependencies]\n"
        self.assertOneError(files, "a workspace member without [package] name", "rs/app/Cargo.toml")

    def test_root_package_is_a_member(self) -> None:
        files = {
            "Cargo.toml": workspace_toml(members="[]")
            + '[package]\nname = "root"\nversion = "0.3.0"\n',
            "Cargo.lock": cargo_lock(cargo_package("root", "0.3.0", None, None)),
        }
        self.assertEqual(run(files).problems, [])
        files["Cargo.lock"] = cargo_lock(cargo_package("root", "0.2.0", None, None))
        self.assertOneError(files, "the member 'root' is 0.2.0 here but 0.3.0")


class RustSources(Base):
    def lock_with(self, *packages: str) -> dict[str, str]:
        return with_file(
            good_rust(),
            "rs/Cargo.lock",
            cargo_lock(cargo_package("app", "0.1.0", None, None), *packages),
        )

    def test_other_sources_are_errors(self) -> None:
        for source in (
            "registry+https://example.org/index",
            "sparse+https://index.crates.io/",
            "registry+http://github.com/rust-lang/crates.io-index",
            "registry+https://github.com/rust-lang/crates.io-index/",
        ):
            with self.subTest(source=source):
                files = self.lock_with(cargo_package("x", source=source))
                self.assertOneError(
                    files, f"source '{source}' is not crates.io or git", "rs/Cargo.lock"
                )

    def test_git_needs_a_full_hex_commit(self) -> None:
        good = f"git+https://github.com/a/b?rev=v1#{COMMIT}"
        self.assertEqual(
            errors_of(self.lock_with(cargo_package("g", source=good, checksum=None))), []
        )
        for source in (
            "git+https://github.com/a/b",
            "git+https://github.com/a/b?branch=main",
            "git+https://github.com/a/b?branch=main#abc123",
            f"git+https://github.com/a/b#{COMMIT}0",
            f"git+https://github.com/a/b#{COMMIT[:-1]}x",
        ):
            with self.subTest(source=source):
                files = self.lock_with(cargo_package("g", source=source, checksum=None))
                self.assertOneError(files, "has no full 40-hex commit after '#'")

    def test_crates_io_package_without_a_checksum_is_an_error(self) -> None:
        files = self.lock_with(cargo_package("nosum", checksum=None))
        self.assertOneError(
            files, "package 'nosum' 1.0.0: crates.io package without a checksum", "rs/Cargo.lock"
        )

    def test_bad_checksum_is_an_error(self) -> None:
        for bad in ("abc", "CD" * 32, "zz" * 32, "cd" * 33):
            with self.subTest(bad=bad):
                files = self.lock_with(cargo_package("badsum", checksum=bad))
                self.assertOneError(files, "is not 64 hex digits")

    def test_git_package_needs_no_checksum_and_path_crates_are_not_third_party(self) -> None:
        git = f"git+https://github.com/a/b#{COMMIT}"
        files = self.lock_with(
            cargo_package("g", source=git, checksum=None),
            cargo_package("localdep", "0.1.0", None, None),
        )
        report = run(files)
        self.assertEqual(report.problems, [])
        self.assertEqual(report.summaries[0].third_party, 1)

    def test_path_dependencies_must_stay_inside_the_repo(self) -> None:
        bad = 'evil = { path = "../../../evil" }'
        for rel, text in (
            ("rs/app/Cargo.toml", cargo_toml(deps=bad)),
            ("rs/app/Cargo.toml", cargo_toml(deps="", extra=f"[dev-dependencies]\n{bad}\n")),
            ("rs/app/Cargo.toml", cargo_toml(deps="", extra=f"[build-dependencies]\n{bad}\n")),
            (
                "rs/app/Cargo.toml",
                cargo_toml(deps="", extra=f"[target.'cfg(unix)'.dependencies]\n{bad}\n"),
            ),
            ("rs/Cargo.toml", workspace_toml(extra=f"[workspace.dependencies]\n{bad}\n")),
            ("rs/Cargo.toml", workspace_toml(extra=f"[patch.crates-io]\n{bad}\n")),
        ):
            with self.subTest(rel=rel, text=text):
                files = with_file(good_rust(), rel, text)
                errors = errors_of(files)
                self.assertEqual(len(errors), 1, errors)
                self.assertTrue(errors[0].startswith(f"{rel}: "), errors[0])
                self.assertIn("'evil': path '../../../evil' is absolute or leaves", errors[0])

    def test_absolute_path_dependency_is_an_error(self) -> None:
        files = with_file(
            good_rust(), "rs/app/Cargo.toml", cargo_toml(deps='evil = { path = "/opt/evil" }')
        )
        self.assertOneError(files, "path '/opt/evil' is absolute")

    def test_path_dependency_inside_the_repo_is_fine(self) -> None:
        files = with_file(
            good_rust(),
            "rs/app/Cargo.toml",
            cargo_toml(
                deps='core = { path = "../../other/core" }\nnear = { path = "../app" }\nserde = "1"'
            ),
        )
        self.assertEqual(errors_of(files), [])

    def test_workspace_dependency_path_is_relative_to_the_workspace(self) -> None:
        files = with_file(
            good_rust(),
            "rs/Cargo.toml",
            workspace_toml(extra='[workspace.dependencies]\nx = { path = "../../.." }\n'),
        )
        self.assertOneError(files, "[workspace.dependencies] 'x'")
        files = with_file(
            good_rust(),
            "rs/Cargo.toml",
            workspace_toml(extra='[workspace.dependencies]\nx = { path = ".." }\n'),
        )
        self.assertEqual(errors_of(files), [])


class WholeRepo(Base):
    def test_crate_in_no_workspace_is_a_warning(self) -> None:
        files = with_file(good_rust(), "lone/Cargo.toml", cargo_toml(name="lone"))
        self.assertOneWarning(files, "is in no Cargo workspace: not checked")

    def test_lock_files_without_a_manifest_are_warnings(self) -> None:
        files = with_file(good_rust(), "x/Cargo.lock", cargo_lock())
        self.assertOneWarning(files, "no Cargo.toml next to this lock file")
        files = with_file(good_python(), "x/uv.lock", uv_lock())
        self.assertOneWarning(files, "no pyproject.toml next to this lock file")

    def test_cargo_lock_next_to_a_manifest_without_workspace(self) -> None:
        files = {"c/Cargo.toml": cargo_toml(name="c"), "c/Cargo.lock": cargo_lock()}
        warnings = warnings_of(files)
        self.assertEqual(len(warnings), 2, warnings)
        self.assertTrue(any("its Cargo.toml has no [workspace]" in w for w in warnings))
        self.assertTrue(any("is in no Cargo workspace" in w for w in warnings))

    def test_both_languages_in_one_repo_pass(self) -> None:
        files = {**good_python(), **good_rust()}
        report = run(files)
        self.assertEqual(report.problems, [])
        self.assertEqual([s.ecosystem for s in report.summaries], ["pypi", "cargo"])

    def test_one_broken_project_does_not_hide_another(self) -> None:
        files = {**good_python(), **good_rust()}
        del files["demo/uv.lock"]
        del files["rs/Cargo.lock"]
        errors = errors_of(files)
        self.assertEqual(len(errors), 2, errors)

    def test_errors_in_the_legacy_table_become_warnings(self) -> None:
        files = with_file(
            good_python(), "demo/pyproject.toml", pyproject(version="9", deps=("requests",))
        )
        legacy = {("demo/uv.lock", "version-mismatch"): "fix in the next release"}
        with mock.patch.object(cd, "LEGACY", legacy):
            report = run(files)
        self.assertEqual([p.warning for p in report.problems], [True])
        self.assertIn("(legacy: fix in the next release)", str(report.problems[0]))
        self.assertEqual(errors_of(files)[0].split(": ")[0], "demo/uv.lock")  # without it: error

    def test_legacy_table_of_the_repo_is_empty_or_its_paths_exist(self) -> None:
        for path, _rule in cd.LEGACY:
            self.assertTrue((ROOT / path).exists(), path)


class Summary(Base):
    def project(self, name: str, ruff: str, folder: str | None = None) -> dict[str, str]:
        folder = folder or name
        return {
            f"{folder}/pyproject.toml": pyproject(name=name, deps=("ruff",)),
            f"{folder}/uv.lock": uv_lock(
                own(name=name, extra='dependencies = [{ name = "ruff" }]\n'),
                package("ruff", ruff),
                package("only-" + name),
            ),
        }

    def test_counts_of_third_party_packages_per_project(self) -> None:
        files = {**self.project("a", "1.0"), **good_rust()}
        report = run(files)
        counts = {(s.ecosystem, s.label): s.third_party for s in report.summaries}
        self.assertEqual(counts, {("pypi", "a"): 2, ("cargo", "rs"): 1})
        lines = cd.summary_lines(report)
        self.assertEqual(
            lines,
            [
                "cargo rs: 1 third-party packages (rs/Cargo.lock)",
                "pypi  a: 2 third-party packages (a/uv.lock)",
            ],
        )

    def test_local_projects_are_not_counted(self) -> None:
        files = good_python()
        files["demo/uv.lock"] += own("sibling", "0.1", '{ editable = "../sibling" }')
        files["demo/uv.lock"] += own("virt", "0.1", '{ virtual = "../virt" }')
        self.assertEqual(run(files).summaries[0].third_party, 1)

    def test_different_versions_in_different_projects_are_a_warning(self) -> None:
        files = {**self.project("a", "1.0"), **self.project("b", "1.0"), **self.project("c", "2.0")}
        warnings = warnings_of(files)
        self.assertEqual(errors_of(files), [])
        self.assertEqual(
            warnings,
            [
                "summary: warning: pypi package 'ruff' has different versions in "
                "different projects: "
                "1.0 (a, b); 2.0 (c)"
            ],
        )

    def test_same_version_everywhere_is_quiet(self) -> None:
        files = {**self.project("a", "1.0"), **self.project("b", "1.0")}
        self.assertEqual(run(files).problems, [])

    def test_two_versions_inside_one_project_are_not_a_difference_between_projects(self) -> None:
        files = self.project("a", "1.0")
        files["a/uv.lock"] += package("ruff", "2.0")
        self.assertEqual(run(files).problems, [])

    def test_versions_are_listed_in_version_order(self) -> None:
        files = {**self.project("a", "1.10"), **self.project("b", "1.9")}
        self.assertIn("1.9 (b); 1.10 (a)", warnings_of(files)[0])

    def test_names_are_compared_by_pep_503_and_languages_do_not_mix(self) -> None:
        files = {**self.project("a", "1.0"), **self.project("b", "2.0")}
        files["b/uv.lock"] = files["b/uv.lock"].replace('name = "ruff"', 'name = "Ruff"')
        files["b/pyproject.toml"] = pyproject(name="b", deps=("RUFF",))
        self.assertEqual(len(warnings_of(files)), 1)
        rust = good_rust()
        rust["rs/Cargo.lock"] += cargo_package("ruff", "9.9.9")
        self.assertEqual(warnings_of({**self.project("a", "1.0"), **rust}), [])

    def test_cargo_projects_are_compared_too(self) -> None:
        one = {f"one/{k.split('/', 1)[1]}": v for k, v in good_rust().items()}
        two = {f"two/{k.split('/', 1)[1]}": v for k, v in good_rust().items()}
        two["two/Cargo.lock"] = two["two/Cargo.lock"].replace('"1.0.0"', '"1.2.0"')
        warnings = warnings_of({**one, **two})
        self.assertEqual(len(warnings), 1)
        self.assertIn("cargo package 'serde' has different versions", warnings[0])
        self.assertIn("1.0.0 (one); 1.2.0 (two)", warnings[0])


# --- main and git ----------------------------------------------------------------------


def run_main(args: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cd.main(args)
    return code, out.getvalue(), err.getvalue()


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class MainAndGit(unittest.TestCase):
    def make_repo(self, root: Path, files: dict[str, str], track: bool = True) -> None:
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        if track:
            subprocess.run(["git", "add", *files], cwd=root, check=True)

    def test_tracked_files_lists_only_the_four_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = {
                **good_python(),
                **good_rust(),
                "demo/README.md": "x",
                "demo/not-pyproject.txt": "y",
            }
            self.make_repo(Path(tmp), files)
            (Path(tmp) / "untracked").mkdir()
            (Path(tmp) / "untracked" / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            self.assertEqual(
                cd.tracked_files(Path(tmp)),
                [
                    "demo/pyproject.toml",
                    "demo/uv.lock",
                    "rs/Cargo.lock",
                    "rs/Cargo.toml",
                    "rs/app/Cargo.toml",
                ],
            )

    def test_good_repo_exit_0_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.make_repo(Path(tmp), {**good_python(), **good_rust()})
            code, out, err = run_main([tmp])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            out.splitlines(),
            [
                "cargo rs: 1 third-party packages (rs/Cargo.lock)",
                "pypi  demo: 1 third-party packages (demo/uv.lock)",
                "2 projects checked: 0 errors, 0 warnings",
            ],
        )

    def test_errors_exit_1_and_warnings_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = with_file(
                good_python(),
                "demo/uv.lock",
                good_python()["demo/uv.lock"] + package("bare", hashed=False),
            )
            self.make_repo(Path(tmp), files)
            code, out, err = run_main([tmp])
            self.assertEqual(code, 0)
            self.assertEqual(err, "demo/uv.lock: warning: package 'bare' 1.0: no hash at all\n")
            self.assertIn("0 errors, 1 warnings", out)
        with tempfile.TemporaryDirectory() as tmp:
            self.make_repo(Path(tmp), {"demo/pyproject.toml": pyproject()})
            code, out, err = run_main([tmp])
            self.assertEqual(code, 1)
            self.assertEqual(err, "demo/pyproject.toml: no tracked uv.lock next to it\n")
            self.assertIn("0 projects checked: 1 errors, 0 warnings", out)

    def test_untracked_files_are_not_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.make_repo(Path(tmp), good_python(), track=False)
            code, out, err = run_main([tmp])
        self.assertEqual(code, 2)
        self.assertIn("no tracked pyproject.toml or Cargo.toml files found", err)

    def test_not_a_git_repo_is_exit_2(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {"GIT_CEILING_DIRECTORIES": str(Path(tmp).parent)}),
        ):
            code, _out, err = run_main([tmp])
        self.assertEqual(code, 2)
        self.assertIn("cannot list files with git ls-files", err)


class Invocation(unittest.TestCase):
    def test_wrong_calls_are_exit_2(self) -> None:
        for args in (["--nope"], ["a", "b"], ["/no/such/folder"], ["-x", "y"]):
            with self.subTest(args=args):
                code, out, err = run_main(args)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("usage: python3 tools/check_deps.py [DIR]", err)

    def test_help_is_exit_0(self) -> None:
        for flag in ("-h", "--help"):
            code, out, err = run_main([flag])
            self.assertEqual((code, err), (0, ""))
            self.assertIn("usage: python3 tools/check_deps.py [DIR]", out)

    def test_main_reads_the_git_files_of_the_repo_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, text in good_python().items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text, encoding="utf-8")
            with (
                mock.patch.object(cd, "ROOT", root),
                mock.patch.object(cd, "tracked_files", return_value=sorted(good_python())),
            ):
                code, out, _err = run_main([])
        self.assertEqual(code, 0)
        self.assertIn("1 projects checked: 0 errors, 0 warnings", out)

    def test_main_does_not_write_anything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, text in good_python().items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text, encoding="utf-8")
            before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
            with mock.patch.object(cd, "tracked_files", return_value=sorted(good_python())):
                run_main([tmp])
            after = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
