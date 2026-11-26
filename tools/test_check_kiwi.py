#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_kiwi.py (KIWI images against the OBS packages).

Every test writes a small repository (images and package folders) into a
temporary folder and checks it the way ``main()`` does; there is a test for
every rule and one for the good repository.

Run: python3 tools/test_check_kiwi.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_kiwi as ck  # noqa: E402  (after sys.path.insert above)

IMAGE = "obs/images/Demo/Demo.kiwi"
PATTERNS = "obs/packages/patterns-demo/patterns-demo.spec"


def kiwi(
    *,
    root: str = "image",
    name: str = "Demo",
    schema: str = ' schemaversion="7.6"',
    description: str = "<description type='system'><author>A</author></description>",
    preferences: str = "<preferences><version>0.0.1</version><type image='oem'/></preferences>",
    packages: str = '<package name="kernel-default"/><package name="Vilna-demo-release"/>',
    collections: str = '<namedCollection name="vilna_demo"/>',
) -> str:
    """A good image; the keyword arguments break it in one place."""
    name_attr = f' name="{name}"' if name else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f"<{root}{schema}{name_attr}>\n  {description}\n  {preferences}\n"
        f'  <packages type="image">{collections}{packages}</packages>\n'
        f'  <packages type="bootstrap"><package name="filesystem"/></packages>\n</{root}>\n'
    )


def spec(name: str, body: str = "", files: str = "%files\n/a\n", tags: str = "") -> str:
    """A spec: ``tags`` go into the main preamble, ``body`` after its %description."""
    return (
        f"%define base {name}\nName:           %{{base}}\nVersion:        1.0\n"
        f"Summary:        S\nLicense:        GPL-3.0-or-later\n{tags}\n%description\nD.\n\n"
        f"{body}\n%install\n\n{files}\n%changelog\n"
    )


PATTERN_SPEC = spec(
    "patterns-demo",
    "%package base\nSummary: Base\nProvides: pattern() = vilna_demo\n"
    "Requires: vilna-tool >= 1.0\nRequires: coreutils\nRequires: /usr/bin/gzip\n"
    "Recommends: (vilna-extra if gnome-shell)\nRequires: user(nobody)\n"
    "%description base\nB.\n",
    files="%files base\n",
)


def repo(**overrides: str | None) -> dict[str, str | None]:
    """A good repository: an image, a pattern and the packages they name."""
    files: dict[str, str | None] = {
        IMAGE: kiwi(),
        PATTERNS: PATTERN_SPEC,
        "obs/packages/Vilna-demo-release/Vilna-demo-release.spec": spec("Vilna-demo-release"),
        "obs/packages/vilna-tool/vilna-tool.spec": spec(
            "vilna-tool",
            "%package -n vilna-tool-data\nSummary: Data\n%description -n vilna-tool-data\nD.\n",
            "%files\n/a\n%files -n vilna-tool-data\n/b\n",
        ),
        "obs/packages/vilna-extra/vilna-extra.spec": spec(
            "vilna-extra",
            "%package helper\nSummary: H\n%description helper\nH.\n",
            "%files\n/a\n%files helper\n/b\n",
            tags="Requires: vilna-lib\n",
        ),
        "obs/packages/vilna-lib/vilna-lib.spec": spec(
            "vilna-lib", tags="Provides: vilna-lib-api\n"
        ),
    }
    files.update(overrides)
    return files


def problems_of(files: dict[str, str | None]) -> tuple[list[ck.Problem], list[str]]:
    """Write the files (None = leave out) into a temporary repo and check it."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        tracked = []
        for rel, text in files.items():
            if text is None:
                continue
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
            tracked.append(rel)
        return ck.check_repo(root, sorted(tracked))


def errors_of(files: dict[str, str | None]) -> list[str]:
    return [str(p) for p in problems_of(files)[0] if not p.warning]


def warnings_of(files: dict[str, str | None]) -> list[str]:
    return [str(p) for p in problems_of(files)[0] if p.warning]


class GoodRepo(unittest.TestCase):
    def test_good_repo_has_no_problems(self) -> None:
        self.assertEqual([str(p) for p in problems_of(repo())[0]], [])

    def test_summary_lists_our_packages_per_image(self) -> None:
        self.assertEqual(
            problems_of(repo())[1],
            [
                "Demo (oem): 3 packages, vilna_demo; ours directly: Vilna-demo-release; ours "
                "through patterns and Requires: 4 (patterns-demo-base, vilna-extra, vilna-lib, "
                "vilna-tool)"
            ],
        )


class ImageFile(unittest.TestCase):
    def test_not_well_formed_xml(self) -> None:
        errors = errors_of(repo(**{IMAGE: "<image>"}))
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith(f"{IMAGE}: not well-formed XML"))

    def test_root_must_be_image(self) -> None:
        self.assertEqual(
            errors_of(repo(**{IMAGE: kiwi(root="system")})),
            [f"{IMAGE}: the root element is <system>, not <image>"],
        )

    def test_schemaversion_and_name_are_required(self) -> None:
        self.assertEqual(
            errors_of(repo(**{IMAGE: kiwi(schema="")})), [f"{IMAGE}: <image> has no schemaversion"]
        )
        self.assertEqual(
            errors_of(repo(**{IMAGE: kiwi(name="")})), [f"{IMAGE}: <image> has no name"]
        )

    def test_name_must_be_the_folder(self) -> None:
        self.assertEqual(
            errors_of(repo(**{IMAGE: kiwi(name="Other")})),
            [f"{IMAGE}: <image name='Other'> is not the folder name 'Demo'"],
        )

    def test_description_is_required(self) -> None:
        self.assertEqual(
            errors_of(repo(**{IMAGE: kiwi(description="")})), [f"{IMAGE}: no <description>"]
        )

    def test_preferences_with_a_version_are_required(self) -> None:
        self.assertEqual(
            errors_of(repo(**{IMAGE: kiwi(preferences="")})), [f"{IMAGE}: no <preferences>"]
        )
        no_version = "<preferences><version> </version></preferences>"
        self.assertEqual(
            errors_of(repo(**{IMAGE: kiwi(preferences=no_version)})),
            [f"{IMAGE}: <preferences> has no <version>"],
        )

    def test_version_in_a_second_preferences_counts(self) -> None:
        two = "<preferences><type image='oem'/></preferences><preferences><version>1</version>"
        self.assertEqual(errors_of(repo(**{IMAGE: kiwi(preferences=two + "</preferences>")})), [])

    def test_package_without_a_name(self) -> None:
        self.assertEqual(
            errors_of(repo(**{IMAGE: kiwi(packages='<package name=""/>')})),
            [f"{IMAGE}: a <package> without a name"],
        )

    def test_bom_is_one_clear_error(self) -> None:
        self.assertEqual(
            errors_of(repo(**{IMAGE: "\ufeff" + kiwi()})),
            [f"{IMAGE}: the file starts with a byte order mark (BOM)"],
        )


class ImagePackages(unittest.TestCase):
    def test_our_package_that_no_spec_builds(self) -> None:
        for name in (
            "vilna-nothere",
            "Vilna-x",
            "gloxi-x",
            "herkos-x",
            "patterns-vilna-x",
            "python3-vilna-x",
            "gnome-branding-Vilna",
        ):
            with self.subTest(name=name):
                packages = f'<package name="Vilna-demo-release"/><package name="{name}"/>'
                self.assertEqual(
                    errors_of(repo(**{IMAGE: kiwi(packages=packages)})),
                    [f"{IMAGE}: package {name} is not built by any spec in obs/packages"],
                )

    def test_openSUSE_packages_are_not_looked_for(self) -> None:
        packages = '<package name="Vilna-demo-release"/><package name="MozillaFirefox"/>'
        self.assertEqual(errors_of(repo(**{IMAGE: kiwi(packages=packages)})), [])

    def test_name_of_a_package_folder_counts_as_ours(self) -> None:
        files = repo(**{"obs/packages/ptyx/ptyx.spec": spec("ptyx-renamed")})
        files[IMAGE] = kiwi(packages='<package name="Vilna-demo-release"/><package name="ptyx"/>')
        self.assertIn(
            f"{IMAGE}: package ptyx is not built by any spec in obs/packages", errors_of(files)
        )

    def test_subpackages_are_found(self) -> None:
        packages = (
            '<package name="Vilna-demo-release"/><package name="vilna-tool-data"/>'
            '<package name="vilna-extra-helper"/>'
        )
        self.assertEqual(errors_of(repo(**{IMAGE: kiwi(packages=packages)})), [])

    def test_package_declared_without_files_is_not_built(self) -> None:
        packages = '<package name="Vilna-demo-release"/><package name="patterns-demo"/>'
        files = repo(**{IMAGE: kiwi(packages=packages)})
        # patterns-demo is the Name of the pattern spec, which has no %files for it.
        self.assertEqual(
            errors_of(files),
            [
                f"{IMAGE}: package patterns-demo is not built by any spec in obs/packages "
                f"({PATTERNS} declares it but has no %files for it)"
            ],
        )

    def test_image_rpm_must_be_an_image(self) -> None:
        packages = '<package name="Vilna-demo-release"/><package name="tik-osimage-Nope"/>'
        self.assertEqual(
            errors_of(repo(**{IMAGE: kiwi(packages=packages)})),
            [f"{IMAGE}: package tik-osimage-Nope is not an image in obs/images"],
        )
        files = repo(**{"obs/images/tik-osimage-Nope/tik-osimage-Nope.kiwi": kiwi(name="x")})
        files[IMAGE] = kiwi(packages=packages)
        self.assertNotIn(
            f"{IMAGE}: package tik-osimage-Nope is not an image in obs/images", errors_of(files)
        )

    def test_our_pattern_must_be_provided(self) -> None:
        self.assertEqual(
            errors_of(repo(**{IMAGE: kiwi(collections='<namedCollection name="vilna_nope"/>')})),
            [f"{IMAGE}: pattern vilna_nope is not provided by any spec in obs/packages"],
        )

    def test_other_collections_are_not_looked_for(self) -> None:
        collections = '<namedCollection name="vilna_demo"/><collection name="base"/>'
        self.assertEqual(errors_of(repo(**{IMAGE: kiwi(collections=collections)})), [])

    def test_package_twice_in_one_image_is_a_warning(self) -> None:
        packages = '<package name="Vilna-demo-release"/>' * 2
        files = repo(**{IMAGE: kiwi(packages=packages)})
        self.assertEqual(errors_of(files), [])
        self.assertEqual(
            warnings_of(files), [f"{IMAGE}: warning: Vilna-demo-release is named twice"]
        )


class PatternRequires(unittest.TestCase):
    def test_pattern_requires_our_package_that_no_spec_builds(self) -> None:
        text = PATTERN_SPEC.replace("Requires: coreutils", "Requires: vilna-gone = 2")
        self.assertEqual(
            errors_of(repo(**{PATTERNS: text})),
            [
                f"{PATTERNS}: pattern vilna_demo requires vilna-gone is not built by any spec in "
                "obs/packages"
            ],
        )

    def test_names_inside_a_rich_dependency_are_checked(self) -> None:
        text = PATTERN_SPEC.replace("(vilna-extra if", "(vilna-gone if")
        errors = errors_of(repo(**{PATTERNS: text}))
        self.assertIn(
            f"{PATTERNS}: pattern vilna_demo requires vilna-gone is not built by any spec in "
            "obs/packages",
            errors,
        )

    def test_capabilities_are_read_as_names(self) -> None:
        self.assertEqual(
            [
                ck.capabilities(v)
                for v in (
                    "a >= 1",
                    "/usr/bin/x",
                    "typelib(Gtk) = 4.0",
                    "(a if b)",
                    "(a >= 1 or b)",
                    "%{name} = 1",
                )
            ],
            [["a"], [], [], ["a", "b"], ["a", "b"], []],
        )


class Unused(unittest.TestCase):
    def test_package_nobody_installs_is_a_warning(self) -> None:
        files = repo(**{"obs/packages/vilna-lonely/vilna-lonely.spec": spec("vilna-lonely")})
        self.assertEqual(errors_of(files), [])
        self.assertEqual(
            warnings_of(files),
            [
                "obs/packages/vilna-lonely/vilna-lonely.spec: warning: no image or pattern "
                "installs vilna-lonely (nobody installs the package)"
            ],
        )

    def test_package_reached_through_provides_is_installed(self) -> None:
        extra = spec("vilna-extra", tags="Requires: vilna-lib-api\n")
        self.assertEqual(
            warnings_of(repo(**{"obs/packages/vilna-extra/vilna-extra.spec": extra})), []
        )

    def test_requires_of_a_package_not_installed_do_not_count(self) -> None:
        text = PATTERN_SPEC.replace("Recommends: (vilna-extra if gnome-shell)\n", "")
        self.assertEqual(
            warnings_of(repo(**{PATTERNS: text})),
            [
                "obs/packages/vilna-extra/vilna-extra.spec: warning: no image or pattern installs "
                "vilna-extra, vilna-extra-helper (nobody installs the package)",
                "obs/packages/vilna-lib/vilna-lib.spec: warning: no image or pattern installs "
                "vilna-lib (nobody installs the package)",
            ],
        )

    def test_folder_without_built_packages_is_not_reported(self) -> None:
        files = repo(**{"obs/packages/vilna-meta/vilna-meta.spec": spec("vilna-meta", files="")})
        self.assertEqual(warnings_of(files), [])


class Specs(unittest.TestCase):
    def test_python_subpackages_and_lang_package(self) -> None:
        text = spec(
            "python-vilna-x",
            "%define pythons python3 python311\n%python_subpackages\n%lang_package\n",
            "%files %{python_files}\n/a\n%files lang\n/b\n",
        )
        names = {r.name for r in ck.parse_spec("x.spec", text) if r.files}
        self.assertEqual(names, {"python3-vilna-x", "python311-vilna-x", "python-vilna-x-lang"})

    @unittest.skipUnless(
        importlib.util.find_spec("check_spec"), "tools/check_spec.py (D7) is not there"
    )
    def test_same_rpm_names_as_check_spec(self) -> None:
        import check_spec

        texts = [PATTERN_SPEC, *(v for k, v in repo().items() if k.endswith(".spec") and v)]
        texts.append(
            spec(
                "python-vilna-x",
                "%define pythons python3\n%python_subpackages\n",
                "%files %{python_files}\n/a\n",
            )
        )
        for text in texts:
            with self.subTest(spec=text.splitlines()[0]):
                ours = sorted(r.name for r in ck.parse_spec("x.spec", text) if r.files)
                theirs = check_spec.built_packages(check_spec.parse_spec(text))
                self.assertEqual(ours, theirs)


class Main(unittest.TestCase):
    def run_main(self, argv: list[str], root: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old = ck.ROOT
        ck.ROOT = root
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ck.main(argv)
        finally:
            ck.ROOT = old
        return code, out.getvalue(), err.getvalue()

    def git_repo(self, root: Path, files: dict[str, str | None]) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        for rel, text in files.items():
            if text is not None:
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text, encoding="utf-8")
        subprocess.run(["git", "add", *(k for k, v in files.items() if v)], cwd=root, check=True)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_main_exit_codes_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.git_repo(root, repo())
            (root / "obs/images/Untracked").mkdir()
            (root / "obs/images/Untracked/Untracked.kiwi").write_text("<x/>", encoding="utf-8")
            code, out, err = self.run_main([], root)
            self.assertEqual((code, err), (0, ""))
            self.assertTrue(out.startswith("Demo (oem): 3 packages"))
            self.assertTrue(
                out.endswith("1 images and 5 package folders checked: 0 errors, 0 warnings\n")
            )
            self.git_repo(root, {IMAGE: kiwi(name="Other")})
            code, out, err = self.run_main([], root)
            self.assertEqual(code, 1)
            self.assertEqual(err, f"{IMAGE}: <image name='Other'> is not the folder name 'Demo'\n")

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_main_warnings_do_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.git_repo(
                root,
                repo(
                    **{
                        IMAGE: kiwi(
                            packages='<package name="Vilna-demo-release"/><package name="a"/>' * 2
                        )
                    }
                ),
            )
            code, out, _ = self.run_main([], root)
        self.assertEqual(code, 0)
        self.assertIn("0 errors, 2 warnings", out)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_main_without_images_is_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.git_repo(root, repo(**{IMAGE: None}))
            code, _, err = self.run_main([], root)
        self.assertEqual(code, 2)
        self.assertIn("no tracked *.kiwi", err)

    def test_main_wrong_call_is_2_and_help_is_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_main(["--bogus"], Path(tmp))
            self.assertEqual(code, 2)
            self.assertIn("usage:", err)
            self.assertEqual(self.run_main(["--help"], Path(tmp))[0], 0)

    def test_main_outside_git_is_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_main([], Path(tmp))
        self.assertEqual(code, 2)
        self.assertIn("git ls-files", err)


if __name__ == "__main__":
    unittest.main()
