#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_spec.py (the RPM spec checker).

Every test writes a small repository (package folders, LICENSING.md and a
piece of obs_sync.py) into a temporary folder and checks it the way
``main()`` does; there is a test for every rule and one for the good package.

Run: python3 tools/test_check_spec.py
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
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_spec as cs  # noqa: E402  (after sys.path.insert above)

FOLDER = "obs/packages/demo"
SPEC = f"{FOLDER}/demo.spec"
CHANGES = f"{FOLDER}/demo.changes"
SEP = "-" * 67
LICENSING = """# Licensing

Apache-2.0 for `privacy-gateway/`, `mcp/`, `sdk/`.

## Graphics under CC-BY-SA-4.0

| What | Where |
| --- | --- |
| Pictures | `obs/packages/art/*.jpg` |
| Sounds | `sounds/` |

Also the package `plymouth-demo`.

## Contributions

`obs/packages/demo/` is mentioned here, outside the CC-BY-SA-4.0 part.
"""
OBS_SYNC = """TARBALLS: dict[str, Tarball] = {
    "mcp-demo": Tarball(
        "mcp/demo", pyproject_version("mcp/demo"), extra=(("mcp/LICENSE", "LICENSE"),)
    ),
    "rusty": Tarball(
        "rusty",
        pyproject_version("rusty"),
        cargo="rusty/Cargo.toml",
    ),
    "sound-demo": Tarball("sounds", file_version("sounds/VERSION")),
}
"""


def spec(
    *,
    name: str = "demo",
    header: str = "GPL-3.0-or-later",
    version: str = "1.0",
    release: str = "0",
    licence: str = "GPL-3.0-or-later",
    url: str = "URL:            https://example.org/demo",
    sources: str = "Source0:        demo.conf",
    preamble: str = "BuildArch:      noarch",
    body: str = "",
    files: str = "%files\n/etc/demo.conf\n",
    changelog: str = "%changelog\n",
) -> str:
    """A good spec; the keyword arguments break it in one place."""
    return (
        f"#\n# spec file for package {name}\n#\n# SPDX-License-Identifier: {header}\n#\n\n"
        "%define flavor plain\n"
        f"Name:           {name}\n"
        f"Version:        {version}\n"
        f"Release:        {release}\n"
        "Summary:        A demo\n"
        f"License:        {licence}\n"
        f"{url}\n{sources}\n{preamble}\n\n"
        "%description\nA demo package.\n\n"
        f"{body}\n"
        "%prep\n\n%build\n\n%install\ninstall -D %{SOURCE0} %{buildroot}/etc/demo.conf\n\n"
        f"{files}\n{changelog}"
    )


def entry(header: str = "Thu Nov 26 10:00:00 UTC 2026 - Demo Person", text: str = "- x") -> str:
    return f"{SEP}\n{header}\n\n{text}\n\n"


GOOD_CHANGES = entry() + entry("Wed Nov 25 09:00:00 UTC 2026 - Demo Person", "- First")


def package(
    folder: str = FOLDER, text: str | None = None, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """The files of a good package folder (with overrides)."""
    name = folder.rsplit("/", 1)[-1]
    files = {
        f"{folder}/{name}.spec": spec(name=name) if text is None else text,
        f"{folder}/{name}.changes": GOOD_CHANGES,
        f"{folder}/demo.conf": "key=value\n",
    }
    files.update(extra or {})
    return files


def problems_of(files: dict[str, str], folders: list[str] | None = None) -> list[cs.Problem]:
    """Write the files (None = delete) into a temporary repo and check the folders."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        everything = {"LICENSING.md": LICENSING, "obs/tools/obs_sync.py": OBS_SYNC, **files}
        tracked = []
        for rel, text in everything.items():
            if text is None:
                continue
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
            tracked.append(rel)
        tracked = sorted(tracked)
        chosen = folders or cs.package_folders(tracked)
        return cs.check_folders(root, chosen, tracked)


def errors_of(files: dict[str, str], folders: list[str] | None = None) -> list[str]:
    return [str(p) for p in problems_of(files, folders) if not p.warning]


def warnings_of(files: dict[str, str], folders: list[str] | None = None) -> list[str]:
    return [str(p) for p in problems_of(files, folders) if p.warning]


def spec_errors(**kwargs: str) -> list[str]:
    return errors_of(package(text=spec(**kwargs)))


def spec_warnings(**kwargs: str) -> list[str]:
    return warnings_of(package(text=spec(**kwargs)))


class GoodPackage(unittest.TestCase):
    def test_good_package_has_no_problems(self) -> None:
        self.assertEqual([str(p) for p in problems_of(package())], [])

    def test_messages_start_with_the_path(self) -> None:
        problem = problems_of(package(text=spec(release="1")))[0]
        self.assertTrue(str(problem).startswith(f"{SPEC}: "))


class Layout(unittest.TestCase):
    def test_name_must_match_the_folder(self) -> None:
        errors = errors_of(package(text=spec(name="other")))
        self.assertEqual(errors, [f"{SPEC}: Name is 'other', not the folder name 'demo'"])

    def test_name_from_a_macro_is_expanded(self) -> None:
        text = spec(name="%{base}").replace("Name:", "%global base demo\nName:", 1)
        self.assertEqual(errors_of(package(text=text)), [])

    def test_spec_named_after_the_folder_is_required(self) -> None:
        files = package()
        files[f"{FOLDER}/other.spec"] = files.pop(SPEC)
        errors = errors_of(files)
        self.assertIn(f"{SPEC}: the file does not exist", errors)
        self.assertIn(
            f"{FOLDER}/other.spec: a second spec in the folder (OBS expects only demo.spec)", errors
        )

    def test_missing_changes_is_a_warning(self) -> None:
        files = package()
        del files[CHANGES]
        self.assertEqual(errors_of(files), [])
        self.assertEqual(
            warnings_of(files),
            [f"{CHANGES}: warning: missing: OBS keeps the changelog of the package there"],
        )

    def test_not_utf8_and_bom(self) -> None:
        self.assertEqual(
            errors_of(package(text=b"Name: \xff\n")),  # type: ignore[arg-type]
            [f"{SPEC}: the file is not valid UTF-8"],
        )
        errors = errors_of(package(text="\ufeff" + spec()))
        self.assertEqual(errors, [f"{SPEC}: the file starts with a byte order mark (BOM)"])

    def test_spdx_header_must_have_the_folder_licence(self) -> None:
        errors = spec_errors(header="MIT")
        self.assertEqual(
            errors,
            [f"{SPEC}: no '# SPDX-License-Identifier: GPL-3.0-or-later' in the first 10 lines"],
        )


class Tags(unittest.TestCase):
    def test_each_required_tag_is_reported(self) -> None:
        for tag in ("Version", "Release", "Summary", "License"):
            with self.subTest(tag=tag):
                text = "\n".join(
                    line for line in spec().splitlines() if not line.startswith(f"{tag}:")
                )
                self.assertIn(
                    f"{SPEC}: the main package has no {tag}:", errors_of(package(text=text))
                )

    def test_missing_url_is_only_a_warning(self) -> None:
        self.assertEqual(spec_errors(url=""), [])
        self.assertEqual(spec_warnings(url=""), [f"{SPEC}: warning: the main package has no URL:"])

    def test_buildarch_or_build_section_is_needed(self) -> None:
        text = spec(preamble="").replace("%build\n", "").replace("%install\n", "%check\n")
        self.assertEqual(
            errors_of(package(text=text)),
            [f"{SPEC}: no BuildArch: and no %build or %install section"],
        )

    def test_build_section_without_buildarch_is_fine(self) -> None:
        self.assertEqual(spec_errors(preamble=""), [])

    def test_subpackage_needs_a_summary(self) -> None:
        body = "%package extra\nRequires: demo\n%description extra\nMore.\n"
        errors = spec_errors(body=body, files="%files\n/a\n%files extra\n/b\n")
        self.assertEqual(errors, [f"{SPEC}: line 20: %package demo-extra has no Summary:"])

    def test_single_value_tag_twice(self) -> None:
        errors = spec_errors(preamble="BuildArch:      noarch\nSummary:        Again")
        self.assertEqual(errors, [f"{SPEC}: line 16: Summary: is set twice for demo"])

    def test_source_number_twice(self) -> None:
        errors = errors_of(
            package(
                text=spec(sources="Source0:        demo.conf\nSource:         demo.conf"),
            )
        )
        self.assertEqual(errors, [f"{SPEC}: line 15: Source0 is used twice"])

    def test_version_with_dash_or_odd_characters(self) -> None:
        for bad in ("1.0-1", "1.0 beta", "1,0"):
            with self.subTest(version=bad):
                errors = spec_errors(version=bad)
                self.assertEqual(
                    errors,
                    [f"{SPEC}: Version {bad!r} may only have 0-9 A-Z a-z . _ + ~ ^"],
                )

    def test_version_with_tilde_plus_and_macro_is_fine(self) -> None:
        for good in ("0.2.0~a1", "50.1+vilna1", "%{flavor}"):
            with self.subTest(version=good):
                self.assertEqual(spec_errors(version=good), [])

    def test_release_must_be_zero(self) -> None:
        self.assertEqual(
            spec_errors(release="1"), [f"{SPEC}: Release is '1', not 0 (OBS counts the builds)"]
        )


class Sections(unittest.TestCase):
    def test_subpackage_without_files(self) -> None:
        body = "%package extra\nSummary: More\n%description extra\nMore.\n"
        self.assertEqual(spec_errors(body=body), [f"{SPEC}: demo-extra has no %files"])

    def test_subpackage_without_description(self) -> None:
        body = "%package -n other-demo\nSummary: More\n"
        errors = spec_errors(body=body, files="%files\n/a\n%files -n other-demo\n/b\n")
        self.assertEqual(errors, [f"{SPEC}: other-demo has no %description"])

    def test_main_package_without_description(self) -> None:
        text = spec().replace("%description\nA demo package.\n", "")
        self.assertEqual(errors_of(package(text=text)), [f"{SPEC}: demo has no %description"])

    def test_main_package_without_files_is_fine(self) -> None:
        body = "%package base\nSummary: Base\n%description base\nBase.\n"
        self.assertEqual(spec_errors(body=body, files="%files base\n/a\n"), [])

    def test_files_for_an_undeclared_package(self) -> None:
        errors = spec_errors(files="%files\n/a\n%files -f list.lang nothere\n/b\n")
        self.assertEqual(
            errors, [f"{SPEC}: line 30: %files for demo-nothere, which is not declared"]
        )

    def test_two_files_sections_for_one_package(self) -> None:
        errors = spec_errors(files="%files\n/a\n%files\n/b\n")
        self.assertEqual(errors, [f"{SPEC}: demo has 2 %files sections"])

    def test_lang_package_and_python_files_are_understood(self) -> None:
        body = "%lang_package\n"
        files = "%files %{python_files}\n/a\n%files lang -f %{name}.lang\n"
        self.assertEqual(spec_errors(body=body, files=files), [])


class Changelog(unittest.TestCase):
    def test_no_changelog(self) -> None:
        self.assertEqual(spec_errors(changelog=""), [f"{SPEC}: there is no %changelog"])

    def test_changelog_must_be_last(self) -> None:
        text = spec(changelog="%changelog\n\n%post\n/bin/true\n")
        self.assertEqual(
            errors_of(package(text=text)), [f"{SPEC}: %changelog is not the last section"]
        )

    def test_two_changelogs(self) -> None:
        text = spec(files="%files\n/a\n%changelog\n")
        self.assertEqual(errors_of(package(text=text)), [f"{SPEC}: 2 %changelog sections"])

    def test_changelog_must_be_empty(self) -> None:
        errors = spec_errors(changelog="%changelog\n* Thu Nov 26 2026 Someone\n- x\n")
        self.assertEqual(
            errors, [f"{SPEC}: %changelog is not empty: the entries belong in the .changes file"]
        )

    def changes_errors(self, text: str) -> list[str]:
        return errors_of(package(extra={CHANGES: text}))

    def test_changes_must_start_with_the_separator(self) -> None:
        self.assertEqual(
            self.changes_errors("hello\n" + GOOD_CHANGES),
            [f"{CHANGES}: the file does not start with a line of 67 '-'"],
        )

    def test_changes_separator_of_wrong_length(self) -> None:
        self.assertEqual(
            self.changes_errors(GOOD_CHANGES + "-" * 60 + "\n"),
            [f"{CHANGES}: line 11: a separator of 60 '-', not 67"],
        )

    def test_changes_header_format(self) -> None:
        for bad in (
            "2026-11-26 - Demo Person",
            "Thu Nov 26 10:00:00 CET 2026 - Demo Person",
            "Thu Nov 26 10:00:00 UTC 2026",
        ):
            with self.subTest(header=bad):
                self.assertEqual(
                    self.changes_errors(entry(bad)),
                    [f"{CHANGES}: line 2: not 'Www Mmm DD HH:MM:SS UTC YYYY - author'"],
                )

    def test_changes_wrong_weekday_and_impossible_date(self) -> None:
        self.assertEqual(
            self.changes_errors(entry("Fri Nov 26 10:00:00 UTC 2026 - Demo Person")),
            [f"{CHANGES}: line 2: 2026-11-26 is a Thu, not Fri"],
        )
        self.assertEqual(
            self.changes_errors(entry("Mon Feb 30 10:00:00 UTC 2026 - Demo Person")),
            [f"{CHANGES}: line 2: the date does not exist"],
        )

    def test_single_digit_day_with_two_spaces(self) -> None:
        self.assertEqual(
            self.changes_errors(entry("Mon Nov  9 23:30:00 UTC 2026 - Demo Person")), []
        )

    def test_changes_must_go_from_newest_to_oldest(self) -> None:
        text = entry("Wed Nov 25 09:00:00 UTC 2026 - A") + entry()
        self.assertEqual(
            self.changes_errors(text),
            [
                f"{CHANGES}: line 7: the entry of 2026-11-26 10:00 is newer than the one "
                "above it (2026-11-25 09:00)"
            ],
        )

    def test_legacy_changes_order_is_a_warning(self) -> None:
        folder = "obs/packages/gloxi"
        text = entry("Wed Nov 25 09:00:00 UTC 2026 - A") + entry()
        files = package(folder, extra={f"{folder}/gloxi.changes": text})
        self.assertEqual(errors_of(files), [])
        self.assertEqual(len(warnings_of(files)), 1)


class Licences(unittest.TestCase):
    def test_good_expressions(self) -> None:
        for good in (
            "GPL-3.0-or-later",
            "GPL-3.0-or-later AND CC-BY-SA-4.0",
            "GPL-3.0-or-later AND (Apache-2.0 OR MIT) WITH LLVM-exception",
        ):
            with self.subTest(licence=good):
                found = cs.licence_ids(SPEC, 1, good)[1]
                self.assertEqual(found, [])

    def test_bad_expressions(self) -> None:
        cases = {
            "GPLv3": "'GPLv3' is not a known SPDX licence identifier",
            "GPL-3.0+": "'GPL-3.0+' is not a known SPDX licence identifier",
            "GPL-3.0-or-later and MIT": "'and' is not a known SPDX licence identifier",
            "(GPL-3.0-or-later": "the brackets do not match or it is empty",
            "GPL-3.0-or-later AND": "it ends with an operator",
            "AND GPL-3.0-or-later": "AND without a licence before it",
            "GPL-3.0-or-later MIT": "'MIT' follows a licence without AND/OR",
            "MIT WITH Nothing-1.0": "'Nothing-1.0' is not a known licence exception",
        }
        for expression, message in cases.items():
            with self.subTest(licence=expression):
                found = [p.message for p in cs.licence_ids(SPEC, 3, expression)[1]]
                self.assertIn(f"line 3: License {expression!r}: {message}", found)

    def test_unknown_identifier_in_a_spec(self) -> None:
        self.assertEqual(
            spec_errors(licence="GPLv3"),
            [f"{SPEC}: line 12: License 'GPLv3': 'GPLv3' is not a known SPDX licence identifier"],
        )

    def test_package_needs_the_licence_of_its_folder(self) -> None:
        self.assertEqual(
            spec_errors(licence="Apache-2.0"),
            [
                f"{SPEC}: line 12: License of demo lacks GPL-3.0-or-later, the licence of "
                "obs/packages/demo/",
                f"{SPEC}: line 12: License of demo has Apache-2.0: only vendored crates "
                "(vendor.tar.xz) or upstream code (_service) bring other licences",
            ],
        )

    def test_apache_folder_from_obs_sync(self) -> None:
        folder = "obs/packages/mcp-demo"
        good = spec(name="mcp-demo", header="Apache-2.0", licence="Apache-2.0")
        self.assertEqual(errors_of(package(folder, good)), [])
        bad = spec(name="mcp-demo", header="Apache-2.0", licence="GPL-3.0-or-later")
        self.assertIn(
            f"{folder}/mcp-demo.spec: line 12: License of mcp-demo lacks Apache-2.0, the licence "
            "of mcp/demo/",
            errors_of(package(folder, bad)),
        )

    def test_cc_by_sa_only_where_licensing_md_lists_it(self) -> None:
        both = "GPL-3.0-or-later AND CC-BY-SA-4.0"
        self.assertEqual(
            spec_errors(licence=both),
            [
                f"{SPEC}: line 12: License of demo has CC-BY-SA-4.0, but LICENSING.md does not "
                "list obs/packages/demo/ or demo under it"
            ],
        )
        for folder in ("obs/packages/art", "obs/packages/plymouth-demo", "obs/packages/sound-demo"):
            with self.subTest(folder=folder):
                name = folder.rsplit("/", 1)[-1]
                text = spec(name=name, licence=both)
                self.assertEqual(errors_of(package(folder, text)), [])

    def test_vendored_crates_bring_their_licences(self) -> None:
        folder = "obs/packages/rusty"
        text = spec(
            name="rusty",
            licence="GPL-3.0-or-later AND (Apache-2.0 OR MIT) AND Unicode-3.0",
            sources="Source0:        %{name}-%{version}.tar.gz\nSource1:        vendor.tar.xz",
        )
        self.assertEqual(errors_of(package(folder, text, {f"{folder}/demo.conf": None})), [])

    def test_third_party_licence_is_listed_as_a_warning(self) -> None:
        service = (
            "<services><service name='download_url'><param name='filename'>up-1.0.tar.gz</param>"
            "</service><service name='verify_file'><param name='file'>_service:download_url:"
            "up-1.0.tar.gz</param><param name='checksum'>ab</param></service></services>"
        )
        text = spec(licence="GPL-2.0-or-later", sources="Source0:        up-%{version}.tar.gz")
        files = package(
            text=text, extra={f"{FOLDER}/_service": service, f"{FOLDER}/demo.conf": None}
        )
        self.assertEqual(errors_of(files), [])
        self.assertEqual(
            warnings_of(files),
            [
                f"{SPEC}: warning: line 12: License of demo is GPL-2.0-or-later: a third-party "
                "program (_service) with its own licence"
            ],
        )

    def test_subpackage_licence_is_checked_too(self) -> None:
        body = "%package extra\nSummary: More\nLicense: MIT\n%description extra\nMore.\n"
        errors = spec_errors(body=body, files="%files\n/a\n%files extra\n/b\n")
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("License of demo-extra" in e for e in errors))


class Sources(unittest.TestCase):
    def test_missing_local_source(self) -> None:
        errors = errors_of(package(extra={f"{FOLDER}/demo.conf": None}))
        self.assertEqual(
            errors,
            [
                f"{SPEC}: line 14: Source0 demo.conf is not in {FOLDER}, not downloaded by "
                "_service and not made by obs_sync.py"
            ],
        )

    def test_missing_patch(self) -> None:
        errors = spec_errors(sources="Source0:        demo.conf\nPatch0:         fix.patch")
        self.assertEqual(len(errors), 1)
        self.assertIn("Patch0 fix.patch is not in", errors[0])

    def test_url_source_is_not_looked_for(self) -> None:
        sources = "Source0:        demo.conf\nSource1:        https://example.org/x.tar.gz"
        self.assertEqual(spec_errors(sources=sources), [])

    def test_source_with_macros_is_expanded(self) -> None:
        files = package(
            text=spec(sources="Source0:        %{name}-%{flavor}.conf"),
            extra={f"{FOLDER}/demo.conf": None, f"{FOLDER}/demo-plain.conf": "x\n"},
        )
        self.assertEqual([str(p) for p in problems_of(files)], [])

    def test_unknown_macro_in_a_source_is_a_warning(self) -> None:
        sources = "Source0:        demo.conf\nSource1:        %{upstream}.tar.gz"
        self.assertEqual(spec_errors(sources=sources), [])
        self.assertEqual(len(spec_warnings(sources=sources)), 1)

    def test_own_tarball_of_obs_sync_is_fine(self) -> None:
        folder = "obs/packages/sound-demo"
        text = spec(
            name="sound-demo",
            licence="GPL-3.0-or-later AND CC-BY-SA-4.0",
            sources="Source0:        %{name}-%{version}.tar.gz",
        )
        files = package(folder, text, {f"{folder}/demo.conf": None})
        self.assertEqual([str(p) for p in problems_of(files)], [])

    def test_own_tarball_nobody_makes_is_a_warning(self) -> None:
        files = package(
            text=spec(sources="Source0:        %{name}-%{version}.tar.xz"),
            extra={f"{FOLDER}/demo.conf": None},
        )
        self.assertEqual(errors_of(files), [])
        self.assertEqual(
            warnings_of(files),
            [
                f"{SPEC}: warning: line 14: Source0 demo-1.0.tar.xz: neither obs/tools/obs_sync.py"
                " (TARBALLS) nor _service makes this tarball"
            ],
        )

    def test_vendor_tarball_only_with_cargo(self) -> None:
        sources = "Source0:        demo.conf\nSource1:        vendor.tar.xz"
        errors = spec_errors(sources=sources)
        self.assertEqual(len(errors), 1)
        self.assertIn("Source1 vendor.tar.xz is not in", errors[0])

    def test_downloaded_source_needs_verify_file(self) -> None:
        service = (
            "<services><service name='download_url'><param name='path'>/d/up.tar.gz</param>"
            "</service></services>"
        )
        files = package(
            text=spec(sources="Source0:        up.tar.gz"),
            extra={f"{FOLDER}/_service": service, f"{FOLDER}/demo.conf": None},
        )
        self.assertIn(
            f"{FOLDER}/_service: up.tar.gz is downloaded but no verify_file checks its checksum",
            errors_of(files),
        )

    def test_broken_service_file(self) -> None:
        files = package(extra={f"{FOLDER}/_service": "<services>"})
        self.assertEqual(len(errors_of(files)), 1)
        self.assertTrue(errors_of(files)[0].startswith(f"{FOLDER}/_service: not well-formed XML"))

    def test_unused_file_is_a_warning(self) -> None:
        files = package(
            extra={
                f"{FOLDER}/README.md": "notes\n",
                f"{FOLDER}/demo-rpmlintrc": "x\n",
                f"{FOLDER}/sub/deep.txt": "not in this folder\n",
            }
        )
        self.assertEqual(errors_of(files), [])
        self.assertEqual(
            warnings_of(files),
            [
                f"{FOLDER}/README.md: warning: the file is in the package folder but no Source "
                "or Patch uses it (it does not get into the package)"
            ],
        )


class AcrossSpecs(unittest.TestCase):
    def test_built_packages(self) -> None:
        text = spec(
            name="python-demo",
            body="%package -n demo-tools\nSummary: T\n%description -n demo-tools\nT.\n"
            "%python_subpackages\n",
            files="%files %{python_files}\n/a\n%files -n demo-tools\n/b\n",
        ).replace("%define flavor plain", "%define pythons python3 python311")
        self.assertEqual(
            cs.built_packages(cs.parse_spec(text)),
            ["demo-tools", "python3-demo", "python311-demo"],
        )

    def test_same_rpm_from_two_specs(self) -> None:
        body = "%package -n other\nSummary: O\n%description -n other\nO.\n"
        text = spec(name="other", body="")
        files = {
            **package(text=spec(body=body, files="%files\n/a\n%files -n other\n/b\n")),
            **package("obs/packages/other", text),
        }
        self.assertEqual(
            errors_of(files),
            [
                f"{SPEC}: other is also built by obs/packages/other/other.spec",
                f"obs/packages/other/other.spec: other is also built by {SPEC}",
            ],
        )


class Main(unittest.TestCase):
    def run_main(self, argv: list[str], root: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old = cs.ROOT
        cs.ROOT = root
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cs.main(argv)
        finally:
            cs.ROOT = old
        return code, out.getvalue(), err.getvalue()

    def repo(self, root: Path, files: dict[str, str]) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        subprocess.run(["git", "add", *files], cwd=root, check=True)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_main_exit_codes_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.repo(root, {"LICENSING.md": LICENSING, **package()})
            (root / f"{FOLDER}/untracked.txt").write_text("x\n", encoding="utf-8")
            code, out, err = self.run_main([], root)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(out, "1 spec files checked: 0 errors, 0 warnings\n")
            self.repo(root, package(text=spec(release="2")))
            code, out, err = self.run_main([FOLDER], root)
            self.assertEqual(code, 1)
            self.assertEqual(err, f"{SPEC}: Release is '2', not 0 (OBS counts the builds)\n")
            self.assertIn("1 errors", out)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_main_warnings_do_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.repo(root, package(text=spec(url="")))
            code, out, _ = self.run_main([], root)
        self.assertEqual(code, 0)
        self.assertIn("0 errors, 1 warnings", out)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_main_unknown_folder_is_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.repo(root, package())
            code, _, err = self.run_main(["obs/packages/nope"], root)
        self.assertEqual(code, 2)
        self.assertIn("not a package folder", err)

    def test_main_wrong_call_is_2_and_help_is_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_main(["--bogus"], Path(tmp))
            self.assertEqual(code, 2)
            self.assertIn("usage:", err)
            self.assertEqual(self.run_main(["-h"], Path(tmp))[0], 0)

    def test_main_outside_git_is_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_main([], Path(tmp))
        self.assertEqual(code, 2)
        self.assertIn("git ls-files", err)


if __name__ == "__main__":
    unittest.main()
