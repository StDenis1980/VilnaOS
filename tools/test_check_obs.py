#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_obs.py (sources, ``_service``, change log, BuildRequires).

Every test writes a small repository (package folders and a piece of
``obs/tools/obs_sync.py``) into a temporary folder and checks it the way
``main()`` does; there is a test for every rule and one for the good package.

Run: python3 tools/test_check_obs.py
"""

from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_obs as co  # noqa: E402  (after sys.path.insert above)

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
FOLDER = "obs/packages/demo"
SPEC = f"{FOLDER}/demo.spec"
CHANGES = f"{FOLDER}/demo.changes"
SERVICE = f"{FOLDER}/_service"
SEP = "-" * 67
CHECKSUM = "ab" * 32
OBS_SYNC = """TARBALLS: dict[str, Tarball] = {
    "demo": Tarball("demo", pyproject_version("demo")),
    "rusty": Tarball("rusty", pyproject_version("rusty"), cargo="rusty/Cargo.toml"),
}
"""


def spec(
    *,
    name: str = "demo",
    sources: str = "Source0:        demo.conf",
    requires: str = "BuildRequires:  fdupes\nBuildRequires:  %{python_module pip >= 1}",
    changelog: str = "%changelog\n",
) -> str:
    """A good spec; the keyword arguments break it in one place."""
    return (
        f"#\n# spec file for package {name}\n#\n\n"
        f"Name:           {name}\n"
        "Version:        1.0\n"
        "Release:        0\n"
        "Summary:        A demo\n"
        "License:        GPL-3.0-or-later\n"
        f"{sources}\n{requires}\nBuildArch:      noarch\n\n"
        "%description\nA demo package.\n\n"
        "%install\ninstall -D %{SOURCE0} %{buildroot}/etc/demo.conf\n\n"
        f"%files\n/etc/demo.conf\n\n{changelog}"
    )


def entry(
    date: str = "Thu Nov 26 10:00:00 UTC 2026",
    author: str = "Demo Person <demo@vilnaos.com>",
    text: str = "- x",
) -> str:
    return f"{SEP}\n{date} - {author}\n\n{text}\n\n"


GOOD_CHANGES = entry() + entry("Wed Nov 25 09:00:00 UTC 2026")


def service(
    *,
    download: str = (
        '<param name="protocol">https</param><param name="host">example.org</param>'
        '<param name="path">/files/demo-1.0.tar.gz</param>'
        '<param name="filename">demo-1.0.tar.gz</param>'
    ),
    verify: str = (
        '<param name="file">_service:download_url:demo-1.0.tar.gz</param>'
        '<param name="verifier">sha256</param>'
        f'<param name="checksum">{CHECKSUM}</param>'
    ),
    extra: str = "",
) -> str:
    return (
        "<services>"
        f'<service name="download_url">{download}</service>'
        f'<service name="verify_file">{verify}</service>'
        f"{extra}</services>"
    )


def package(
    folder: str = FOLDER,
    text: str | None = None,
    extra: dict[str, str | bytes | None] | None = None,
) -> dict[str, str | bytes | None]:
    """The files of a good package folder (with overrides; None deletes a file)."""
    name = folder.rsplit("/", 1)[-1]
    files: dict[str, str | bytes | None] = {
        f"{folder}/{name}.spec": spec(name=name) if text is None else text,
        f"{folder}/{name}.changes": GOOD_CHANGES,
        f"{folder}/demo.conf": "key=value\n",
    }
    files.update(extra or {})
    return files


def problems_of(
    files: dict[str, str | bytes | None],
    folders: list[str] | None = None,
    *,
    tumbleweed_list: str | None = None,
) -> list[co.Problem]:
    """Write the files into a temporary repo and check the folders."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        everything = {"obs/tools/obs_sync.py": OBS_SYNC, **files}
        tracked = []
        for rel, text in everything.items():
            if text is None:
                continue
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
            tracked.append(rel)
        tracked.sort()
        chosen = folders or co.cs.package_folders(tracked)
        return co.check_folders(root, chosen, tracked, tumbleweed_list=tumbleweed_list, now=NOW)


def errors_of(files: dict[str, str | bytes | None], **kwargs: object) -> list[str]:
    return [str(p) for p in problems_of(files, **kwargs) if not p.warning]  # type: ignore[arg-type]


def warnings_of(files: dict[str, str | bytes | None], **kwargs: object) -> list[str]:
    return [str(p) for p in problems_of(files, **kwargs) if p.warning]  # type: ignore[arg-type]


def requires(*lines: str, **kwargs: str) -> dict[str, str | bytes | None]:
    body = "\n".join(f"BuildRequires:  {line}" for line in lines)
    return package(text=spec(requires=body, **kwargs))


def req_errors(*lines: str) -> list[str]:
    return errors_of(requires(*lines))


def changes_errors(text: str) -> list[str]:
    return errors_of(package(extra={CHANGES: text}))


class GoodPackage(unittest.TestCase):
    def test_good_package_has_no_problems(self) -> None:
        self.assertEqual([str(p) for p in problems_of(package())], [])

    def test_messages_start_with_the_path(self) -> None:
        problem = problems_of(package(extra={demo_conf(): None}))[0]
        self.assertTrue(str(problem).startswith(f"{SPEC}: "))

    def test_only_the_chosen_folder_is_reported(self) -> None:
        files = {
            **package(),
            **package("obs/packages/other", extra={"obs/packages/other/demo.conf": None}),
        }
        self.assertEqual(errors_of(files, folders=[FOLDER]), [])
        self.assertEqual(len(errors_of(files, folders=["obs/packages/other"])), 1)

    def test_unreadable_spec_is_one_clear_error(self) -> None:
        self.assertEqual(
            errors_of(package(text=b"Name: \xff\n")),  # type: ignore[arg-type]
            [f"{SPEC}: the file is not valid UTF-8"],
        )

    def test_spec_with_bom_is_still_read(self) -> None:
        errors = errors_of(package(text="\ufeff" + spec()))
        self.assertEqual(errors, [f"{SPEC}: the file starts with a byte order mark (BOM)"])

    def test_crlf_files_are_read(self) -> None:
        files = package(
            text=spec().replace("\n", "\r\n"), extra={CHANGES: GOOD_CHANGES.replace("\n", "\r\n")}
        )
        self.assertEqual(errors_of(files), [])


def demo_conf() -> str:
    return f"{FOLDER}/demo.conf"


class Sources(unittest.TestCase):
    def test_source_missing_from_the_folder(self) -> None:
        errors = errors_of(package(extra={demo_conf(): None}))
        self.assertEqual(
            errors,
            [
                f"{SPEC}: line 10: Source0 demo.conf is not in {FOLDER}, "
                "not downloaded by _service and not made by obs_sync.py"
            ],
        )

    def test_patch_missing_from_the_folder(self) -> None:
        text = spec(sources="Source0:        demo.conf\nPatch1:         fix.patch")
        errors = errors_of(package(text=text))
        self.assertEqual(len(errors), 1)
        self.assertIn("Patch1 fix.patch is not in", errors[0])

    def test_source_in_a_subfolder_is_matched_by_its_file_name(self) -> None:
        text = spec(sources="Source0:        files/demo.conf")
        self.assertEqual(errors_of(package(text=text)), [])

    def test_tarball_made_by_obs_sync_is_fine(self) -> None:
        text = spec(sources="Source0:        %{name}-%{version}.tar.gz")
        self.assertEqual(errors_of(package(text=text)), [])

    def test_own_tarball_without_obs_sync_entry_is_an_error(self) -> None:
        files = package(
            "obs/packages/nope", text=spec(name="nope", sources="Source0: nope-1.0.tar.gz")
        )
        errors = errors_of(files)
        self.assertEqual(len(errors), 1)
        self.assertIn("nope-1.0.tar.gz is not in obs/packages/nope", errors[0])

    def test_vendor_tarball_only_with_cargo(self) -> None:
        text = spec(name="rusty", sources="Source0: vendor.tar.xz")
        self.assertEqual(errors_of(package("obs/packages/rusty", text=text)), [])
        text = spec(sources="Source0: vendor.tar.xz")
        self.assertEqual(len(errors_of(package(text=text))), 1)

    def test_download_of_the_service_is_fine(self) -> None:
        text = spec(sources="Source0:        demo-1.0.tar.gz")
        files = package(text=text, extra={SERVICE: service(), demo_conf(): None})
        self.assertEqual(errors_of(files), [])

    def test_source_with_a_macro_in_a_file_name_is_a_warning(self) -> None:
        text = spec(sources="Source0:        demo-%{unknown}.conf")
        files = package(text=text)
        self.assertEqual(errors_of(files), [])
        self.assertIn("has a macro the checker cannot expand", warnings_of(files)[0])

    def test_source_that_only_another_service_makes_is_a_warning(self) -> None:
        text = spec(sources="Source0:        made.tar.xz")
        other = (
            '<service name="obs_scm"><param name="url">https://example.org/r.git</param></service>'
        )
        files = package(text=text, extra={SERVICE: "<services>" + other + "</services>"})
        self.assertEqual(errors_of(files), [])
        self.assertIn("only the service obs_scm could make it", warnings_of(files)[0])

    def test_url_source_must_be_https(self) -> None:
        text = spec(sources="Source0:        http://example.org/demo.conf")
        errors = errors_of(package(text=text))
        self.assertIn("is not an https URL", errors[0])

    def test_url_source_needs_a_file_or_download_files(self) -> None:
        text = spec(sources="Source0:        https://example.org/dl/demo.tar.gz#/demo-1.0.tar.gz")
        errors = errors_of(package(text=text))
        self.assertEqual(len(errors), 1)
        self.assertIn("demo-1.0.tar.gz is a URL, but the folder has no such file", errors[0])
        fetch = '<services><service name="download_files"/></services>'
        self.assertEqual(errors_of(package(text=text, extra={SERVICE: fetch})), [])
        files = package(text=text, extra={f"{FOLDER}/demo-1.0.tar.gz": "x"})
        self.assertEqual(errors_of(files), [])

    def test_unused_download_is_a_warning(self) -> None:
        files = package(extra={SERVICE: service()})
        self.assertEqual(errors_of(files), [])
        self.assertEqual(
            warnings_of(files),
            [
                f"{SERVICE}: warning: demo-1.0.tar.gz is downloaded "
                "but no Source or Patch of the spec uses it"
            ],
        )

    def test_source_in_the_second_package_is_read_too(self) -> None:
        text = spec().replace(
            "%description\n",
            "%package extra\nSummary: Extra\nSource5: gone.conf\n\n%description\n",
            1,
        )
        errors = errors_of(package(text=text))
        self.assertTrue(any("Source5 gone.conf is not in" in e for e in errors), errors)


class Service(unittest.TestCase):
    def only(self, **kwargs: str) -> list[str]:
        text = spec(sources="Source0:        demo-1.0.tar.gz")
        files = package(text=text, extra={SERVICE: service(**kwargs), demo_conf(): None})
        return errors_of(files)

    def test_good_service(self) -> None:
        self.assertEqual(self.only(), [])

    def test_not_well_formed(self) -> None:
        files = package(extra={SERVICE: "<services><service"})
        errors = errors_of(files)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith(f"{SERVICE}: not well-formed XML"))

    def test_doctype_and_entities_are_refused(self) -> None:
        bomb = '<!DOCTYPE s [<!ENTITY a "aaaa">]><services>&a;</services>'
        self.assertEqual(
            errors_of(package(extra={SERVICE: bomb})),
            [f"{SERVICE}: has a DOCTYPE or ENTITY declaration"],
        )

    def test_http_protocol(self) -> None:
        self.assertEqual(
            self.only(download=download_param(protocol="http")),
            [f"{SERVICE}: demo-1.0.tar.gz: protocol is 'http', not https"],
        )

    def test_missing_protocol(self) -> None:
        errors = self.only(download=download_param(protocol=None))
        self.assertEqual(
            errors, [f"{SERVICE}: demo-1.0.tar.gz: no protocol (OBS then downloads over http)"]
        )

    def test_missing_host(self) -> None:
        errors = self.only(download=download_param(host=""))
        self.assertEqual(errors, [f"{SERVICE}: demo-1.0.tar.gz: no host"])

    def test_path_must_be_absolute(self) -> None:
        errors = self.only(download=download_param(path="files/x.tar.gz"))
        self.assertEqual(
            errors, [f"{SERVICE}: demo-1.0.tar.gz: path is empty or does not start with '/'"]
        )

    def test_url_form(self) -> None:
        good = '<param name="url">https://example.org/x/demo-1.0.tar.gz</param>'
        self.assertEqual(self.only(download=good), [])
        bad = '<param name="url">http://example.org/x/demo-1.0.tar.gz</param>'
        self.assertEqual(self.only(download=bad), [f"{SERVICE}: demo-1.0.tar.gz: url is not https"])

    def test_unsafe_file_names(self) -> None:
        for name in ("../evil.tar.gz", "a/b.tar.gz", ".hidden"):
            errors = self.only(download=download_param(filename=name))
            self.assertTrue(any("not a safe file name" in e for e in errors), (name, errors))

    def test_file_name_comes_from_the_path_without_filename(self) -> None:
        download = download_param(filename=None, path="/files/demo-1.0.tar.gz?raw=1")
        self.assertEqual(self.only(download=download), [])

    def test_no_file_name_at_all(self) -> None:
        errors = self.only(download=download_param(filename=None, path="/"))
        self.assertIn(
            f"{SERVICE}: a download_url has no file name (no filename, no file in the path)", errors
        )

    def test_two_downloads_of_one_file(self) -> None:
        extra = f'<service name="download_url">{download_param()}</service>'
        errors = errors_of(
            package(
                text=spec(sources="Source0: demo-1.0.tar.gz"),
                extra={SERVICE: service(extra=extra), demo_conf(): None},
            )
        )
        self.assertEqual(
            errors, [f"{SERVICE}: demo-1.0.tar.gz: two download_url services save the same file"]
        )

    def test_download_without_verify_file(self) -> None:
        text = spec(sources="Source0:        demo-1.0.tar.gz")
        xml = f'<services><service name="download_url">{download_param()}</service></services>'
        errors = errors_of(package(text=text, extra={SERVICE: xml, demo_conf(): None}))
        self.assertEqual(
            errors,
            [f"{SERVICE}: demo-1.0.tar.gz is downloaded but no verify_file checks its checksum"],
        )

    def test_verify_file_of_another_file(self) -> None:
        verify = verify_param(file="_service:download_url:other.tar.gz")
        errors = self.only(verify=verify)
        self.assertIn(
            f"{SERVICE}: other.tar.gz: verify_file names a file that no download_url saves", errors
        )
        self.assertIn(
            f"{SERVICE}: demo-1.0.tar.gz is downloaded but no verify_file checks its checksum",
            errors,
        )

    def test_verify_file_without_file(self) -> None:
        errors = self.only(verify=verify_param(file=""))
        self.assertIn(f"{SERVICE}: a verify_file has no file", errors)

    def test_weak_verifier(self) -> None:
        errors = self.only(verify=verify_param(verifier="md5", checksum="ab" * 16))
        self.assertIn(f"{SERVICE}: demo-1.0.tar.gz: verifier 'md5' is not sha256 or sha512", errors)

    def test_checksum_length_and_digits(self) -> None:
        for checksum in ("ab" * 31, "zz" * 32, "", CHECKSUM + "0"):
            errors = self.only(verify=verify_param(checksum=checksum))
            self.assertIn(f"{SERVICE}: demo-1.0.tar.gz: checksum is not 64 hex digits", errors)

    def test_sha512_has_its_own_length(self) -> None:
        good = verify_param(verifier="sha512", checksum="cd" * 64)
        self.assertEqual(self.only(verify=good), [])
        bad = verify_param(verifier="sha512", checksum=CHECKSUM)
        self.assertIn("checksum is not 128 hex digits", self.only(verify=bad)[0])


def download_param(
    *,
    protocol: str | None = "https",
    host: str = "example.org",
    path: str = "/files/demo-1.0.tar.gz",
    filename: str | None = "demo-1.0.tar.gz",
) -> str:
    parts = []
    if protocol is not None:
        parts.append(f'<param name="protocol">{protocol}</param>')
    parts.append(f'<param name="host">{host}</param>')
    parts.append(f'<param name="path">{path}</param>')
    if filename is not None:
        parts.append(f'<param name="filename">{filename}</param>')
    return "".join(parts)


def verify_param(
    *,
    file: str = "_service:download_url:demo-1.0.tar.gz",
    verifier: str = "sha256",
    checksum: str = CHECKSUM,
) -> str:
    return (
        f'<param name="file">{file}</param><param name="verifier">{verifier}</param>'
        f'<param name="checksum">{checksum}</param>'
    )


class ChangeLog(unittest.TestCase):
    def test_no_changes_file_and_empty_changelog(self) -> None:
        errors = errors_of(package(extra={CHANGES: None}))
        self.assertEqual(
            errors,
            [f"{SPEC}: no change log: there is no demo.changes and the %changelog has no entry"],
        )

    def test_changes_file_without_entries(self) -> None:
        self.assertEqual(
            changes_errors("nothing here\n"),
            [f"{CHANGES}: no change log: demo.changes has no entry and the %changelog is empty"],
        )

    def test_empty_changes_file(self) -> None:
        self.assertEqual(
            changes_errors(""),
            [f"{CHANGES}: no change log: demo.changes has no entry and the %changelog is empty"],
        )

    def test_spec_changelog_instead_of_changes(self) -> None:
        log = "%changelog\n* Thu Nov 26 2026 Demo Person <demo@vilnaos.com> - 1.0\n- First\n"
        self.assertEqual(errors_of(package(text=spec(changelog=log), extra={CHANGES: None})), [])

    def test_spec_changelog_problems(self) -> None:
        log = (
            "%changelog\n"
            "* Thu Nov 26 2026 Demo Person <demo@example.com> - 1.0\n- a\n"
            "* Feb 30 2026 nobody\n"
            "* Fri Feb 30 2026 Demo <demo@vilnaos.com>\n"
        )
        errors = errors_of(package(text=spec(changelog=log), extra={CHANGES: None}))
        self.assertEqual(len(errors), 3, errors)
        self.assertIn(
            "%changelog: line 0: the address demo@example.com is not on vilnaos.com", errors[0]
        )
        self.assertIn("%changelog: no '* Www Mmm DD YYYY author <address>'", errors[1])
        self.assertIn("the date does not exist", errors[2])

    def test_header_without_a_date(self) -> None:
        text = f"{SEP}\nDemo Person <demo@vilnaos.com>\n\n- x\n\n"
        errors = changes_errors(text)
        self.assertEqual(
            errors[0],
            f"{CHANGES}: line 2: no 'Www Mmm DD HH:MM:SS UTC YYYY - author <address>'",
        )

    def test_date_that_does_not_exist(self) -> None:
        for date in (
            "Fri Feb 30 10:00:00 UTC 2026",
            "Thu Nov 26 25:00:00 UTC 2026",
            "Mon Foo 02 10:00:00 UTC 2026",
        ):
            errors = changes_errors(entry(date))
            self.assertTrue(errors and "line 2:" in errors[0], (date, errors))

    def test_leap_day(self) -> None:
        self.assertEqual(changes_errors(entry("Thu Feb 29 10:00:00 UTC 2024")), [])
        self.assertEqual(len(changes_errors(entry("Wed Feb 29 10:00:00 UTC 2023"))), 1)

    def test_entry_without_address(self) -> None:
        errors = changes_errors(entry(author="Demo Person"))
        self.assertEqual(errors, [f"{CHANGES}: line 2: no address <user@domain> after the date"])

    def test_addresses_that_are_accepted(self) -> None:
        for address in (
            "demo@vilnaos.com",
            "demo@VilnaOS.com",
            "team@mail.vilnaos.com",
            "123+demo@users.noreply.github.com",
            "noreply@example.org",
            "No-Reply@example.org",
        ):
            self.assertEqual(changes_errors(entry(author=f"Demo <{address}>")), [], address)

    def test_addresses_that_are_refused(self) -> None:
        for address in (
            "demo@example.com",
            "demo@vilnaos.com.evil.org",
            "demo@notvilnaos.com",
            "demo@vilnaos.org",
            "demo@evil.org/vilnaos.com",
            "noreplyx@example.org",
            "demo@users.noreply.github.com.evil.org",
        ):
            errors = changes_errors(entry(author=f"Demo <{address}>"))
            self.assertEqual(len(errors), 1, (address, errors))
            self.assertIn("is not on vilnaos.com or a noreply one", errors[0])

    def test_two_addresses_are_one_too_many(self) -> None:
        errors = changes_errors(entry(author="A <a@vilnaos.com> B <b@vilnaos.com>"))
        self.assertEqual(errors, [f"{CHANGES}: line 2: more than one address"])

    def test_every_entry_is_checked(self) -> None:
        text = entry() + entry(author="x") + entry("Wed Nov 25 09:00:00 UTC 2026", "Y <y@evil.org>")
        errors = changes_errors(text)
        self.assertEqual(len(errors), 2)
        self.assertIn("line 7:", errors[0])
        self.assertIn("line 12:", errors[1])

    def test_date_ahead_of_the_clock_is_a_warning(self) -> None:
        files = package(
            extra={
                CHANGES: entry("Fri Nov 27 13:00:00 UTC 2026")
                + entry("Fri Dec 11 10:00:00 UTC 2026")
            }
        )
        self.assertEqual(errors_of(files), [])
        self.assertEqual(
            warnings_of(files),
            [f"{CHANGES}: warning: line 7: the date 2026-12-11 is ahead of the clock"],
        )

    def test_entry_without_text_is_a_warning(self) -> None:
        files = package(extra={CHANGES: entry(text="") + entry("Wed Nov 25 09:00:00 UTC 2026")})
        self.assertEqual(errors_of(files), [])
        self.assertEqual(warnings_of(files), [f"{CHANGES}: warning: line 2: the entry has no text"])

    def test_wrong_weekday_is_left_to_check_spec(self) -> None:
        self.assertEqual(changes_errors(entry("Mon Nov 26 10:00:00 UTC 2026")), [])

    def test_changes_file_not_utf8(self) -> None:
        errors = errors_of(package(extra={CHANGES: b"\xff\xfe"}))
        self.assertEqual(errors[0], f"{CHANGES}: the file is not valid UTF-8")
        self.assertEqual(len(errors), 1)

    def test_changes_file_in_utf16_is_one_error(self) -> None:
        errors = errors_of(package(extra={CHANGES: GOOD_CHANGES.encode("utf-16")}))
        self.assertEqual(errors, [f"{CHANGES}: the file is not valid UTF-8"])


class Parse(unittest.TestCase):
    def deps(self, value: str) -> list[str]:
        found, errors = co.parse_dependencies(value)
        self.assertEqual(errors, [], value)
        return [d.text for d in found]

    def errors(self, value: str) -> list[str]:
        return co.parse_dependencies(value)[1]

    def test_forms(self) -> None:
        self.assertEqual(self.deps("fdupes"), ["fdupes"])
        self.assertEqual(self.deps("cargo >= 1.85"), ["cargo >= 1.85"])
        self.assertEqual(self.deps("a, b >= 1, c"), ["a", "b >= 1", "c"])
        self.assertEqual(self.deps("a b c"), ["a", "b", "c"])
        self.assertEqual(
            self.deps("a = 1.0-2 b == 3 c < 4 d > 5 e <= 6"),
            ["a = 1.0-2", "b == 3", "c < 4", "d > 5", "e <= 6"],
        )
        self.assertEqual(self.deps("pkgconfig(gtk4) >= 4.10"), ["pkgconfig(gtk4) >= 4.10"])
        self.assertEqual(self.deps("/usr/bin/env"), ["/usr/bin/env"])
        self.assertEqual(self.deps("foo >= 1:2.0~rc1"), ["foo >= 1:2.0~rc1"])
        self.assertEqual(
            self.deps("libstdc++-devel gcc-c++ python3.13"),
            ["libstdc++-devel", "gcc-c++", "python3.13"],
        )

    def test_python_module(self) -> None:
        found, errors = co.parse_dependencies("%{python_module hatchling >= 1.26}")
        self.assertEqual(errors, [])
        self.assertEqual(
            [(d.name, d.operator, d.version, d.module) for d in found],
            [("hatchling", ">=", "1.26", True)],
        )

    def test_python_module_with_a_broken_inside(self) -> None:
        self.assertTrue(self.errors("%{python_module hatchling >=}"))

    def test_macros_and_rich_dependencies_are_not_looked_at(self) -> None:
        found, errors = co.parse_dependencies("%{some_macro} (a or b) %{?x:y}")
        self.assertEqual(errors, [])
        self.assertTrue(all(d.opaque for d in found) and len(found) == 3)

    def test_empty_items(self) -> None:
        for value in ("", "   ", "a,,b", "a,", ",a"):
            self.assertIn("an empty item (stray comma or empty value)", self.errors(value), value)

    def test_operator_without_a_version(self) -> None:
        self.assertEqual(self.errors("cargo >="), ["cargo >=: no version after the operator"])
        self.assertEqual(self.errors("cargo >= , b"), ["cargo >=: no version after the operator"])

    def test_version_without_an_operator(self) -> None:
        self.assertEqual(self.errors("cargo 1.85"), ["cargo 1.85: a version needs an operator"])

    def test_unknown_operator(self) -> None:
        self.assertIn("cargo: unknown operator '=>'", self.errors("cargo => 1.85"))
        self.assertIn("cargo: unknown operator '=<'", self.errors("cargo =< 1"))

    def test_operator_stuck_to_the_name(self) -> None:
        errors = self.errors("cargo>=1.85")
        self.assertEqual(
            errors, ["'cargo>=1.85': the operator is stuck to the name, write 'name >=1.85'"]
        )

    def test_operator_alone(self) -> None:
        self.assertEqual(self.errors(">= 1"), ["the operator '>=' has no package before it"])

    def test_invalid_names(self) -> None:
        for name in ("a;b", "a$b", "a'b", "a|b", "ä"):
            self.assertEqual(self.errors(name), [f"{name!r} is not a valid package name"], name)

    def test_commas_inside_brackets_do_not_split(self) -> None:
        found, errors = co.parse_dependencies("%{python_module pip >= 1}, (a, b)")
        self.assertEqual(errors, [])
        self.assertEqual(len(found), 2)

    def test_unbalanced_brackets_do_not_hang_or_crash(self) -> None:
        for value in ("%{python_module pip", "(((", "}}}", "%{" * 50, "a )( b"):
            co.parse_dependencies(value)

    def test_long_value_is_fast(self) -> None:
        start = datetime.now(UTC)
        found, errors = co.parse_dependencies(", ".join(f"pkg{i} >= {i}" for i in range(20000)))
        self.assertEqual((len(found), errors), (20000, []))
        self.assertLess((datetime.now(UTC) - start).total_seconds(), 10)


class BuildRequires(unittest.TestCase):
    def test_good_forms(self) -> None:
        self.assertEqual(
            req_errors("cargo >= 1.85", "pkgconfig(gtk4)", "%{python_module base >= 3.11}"), []
        )

    def test_problems_name_the_line(self) -> None:
        errors = req_errors("fdupes", "cargo >=")
        self.assertEqual(
            errors, [f"{SPEC}: line 12: BuildRequires cargo >=: no version after the operator"]
        )

    def test_the_same_dependency_twice(self) -> None:
        errors = req_errors("fdupes", "meson", "fdupes")
        self.assertEqual(errors, [f"{SPEC}: line 13: BuildRequires fdupes is listed twice"])

    def test_a_range_is_not_a_duplicate(self) -> None:
        self.assertEqual(req_errors("rust >= 1.80", "rust < 2"), [])

    def test_two_on_one_line_twice(self) -> None:
        self.assertEqual(len(req_errors("a b a")), 1)

    def test_package_that_needs_itself(self) -> None:
        errors = req_errors("demo")
        self.assertEqual(
            errors, [f"{SPEC}: line 11: BuildRequires demo is a package this spec builds itself"]
        )

    def test_package_that_needs_its_own_subpackage(self) -> None:
        text = spec(requires="BuildRequires:  demo-extra").replace(
            "%changelog",
            "%package extra\nSummary: x\n\n%description extra\nx\n\n%files extra\n\n%changelog",
        )
        errors = errors_of(package(text=text))
        self.assertEqual(
            errors,
            [f"{SPEC}: line 11: BuildRequires demo-extra is a package this spec builds itself"],
        )

    def test_name_of_the_project_that_no_spec_builds(self) -> None:
        for name in ("vilna-missing", "herkos-missing", "gloxi-missing", "python3-vilna-missing"):
            errors = req_errors(name)
            self.assertEqual(len(errors), 1, name)
            self.assertIn("a name of this project that no spec in obs/packages builds", errors[0])

    def test_python_module_of_the_project_that_no_spec_builds(self) -> None:
        errors = req_errors("%{python_module vilna-missing}")
        self.assertEqual(len(errors), 1)
        self.assertIn("vilna-missing: a name of this project", errors[0])

    def test_name_of_the_project_that_a_spec_builds(self) -> None:
        files = {
            **package(),
            **package("obs/packages/vilna-lib", extra={"obs/packages/vilna-lib/demo.conf": "x"}),
        }
        files[SPEC] = spec(requires="BuildRequires:  vilna-lib")
        self.assertEqual(errors_of(files), [])

    def test_python_module_of_a_python_package_of_the_project(self) -> None:
        files = {
            **package(),
            **package(
                "obs/packages/python-vilna-lib",
                extra={"obs/packages/python-vilna-lib/demo.conf": "x"},
            ),
        }
        files[SPEC] = spec(requires="BuildRequires:  %{python_module vilna-lib}")
        self.assertEqual(errors_of(files), [])

    def test_planned_package(self) -> None:
        yaml = "vilna:\n  packages:\n    - {name: later, license: MIT, status: planned}\n"
        files = {**requires("later"), "image/packages.yaml": yaml}
        errors = errors_of(files)
        self.assertEqual(len(errors), 1)
        self.assertIn("later is 'planned' in image/packages.yaml", errors[0])

    def test_planned_package_that_is_now_built_is_fine(self) -> None:
        yaml = "  - {name: other, license: MIT, status: planned}\n"
        files = {
            **requires("other"),
            **package("obs/packages/other", extra={"obs/packages/other/demo.conf": "x"}),
            "image/packages.yaml": yaml,
        }
        self.assertEqual(errors_of(files), [])

    def test_upstream_and_obs_status_are_not_planned(self) -> None:
        yaml = (
            "  - {name: a, license: MIT, status: upstream}\n"
            "  - {name: b, license: MIT, status: obs}\n"
        )
        files = {**requires("a", "b"), "image/packages.yaml": yaml}
        self.assertEqual(errors_of(files), [])

    def test_cycle_of_two(self) -> None:
        a = package("obs/packages/aaa", text=spec(name="aaa", requires="BuildRequires:  bbb"))
        b = package("obs/packages/bbb", text=spec(name="bbb", requires="BuildRequires:  aaa"))
        errors = errors_of({**a, **b}, folders=["obs/packages/aaa", "obs/packages/bbb"])
        self.assertEqual(
            errors,
            [
                "obs/packages/aaa/aaa.spec: BuildRequires cycle, OBS cannot build it: "
                "aaa -> bbb -> aaa"
            ],
        )

    def test_cycle_of_three_is_reported_once(self) -> None:
        files: dict[str, str | bytes | None] = {}
        for name, nxt in (("aaa", "bbb"), ("bbb", "ccc"), ("ccc", "aaa")):
            files |= package(
                f"obs/packages/{name}", text=spec(name=name, requires=f"BuildRequires:  {nxt}")
            )
        errors = errors_of(files)
        self.assertEqual(
            errors,
            [
                "obs/packages/aaa/aaa.spec: BuildRequires cycle, OBS cannot build it: "
                "aaa -> bbb -> ccc -> aaa"
            ],
        )

    def test_a_chain_is_not_a_cycle(self) -> None:
        files: dict[str, str | bytes | None] = {}
        for name, nxt in (("aaa", "bbb"), ("bbb", "ccc"), ("ccc", "fdupes")):
            files |= package(
                f"obs/packages/{name}", text=spec(name=name, requires=f"BuildRequires:  {nxt}")
            )
        self.assertEqual(errors_of(files), [])

    def test_a_diamond_is_not_a_cycle(self) -> None:
        files: dict[str, str | bytes | None] = {}
        for name, nxt in (("aaa", "bbb, ccc"), ("bbb", "ddd"), ("ccc", "ddd"), ("ddd", "fdupes")):
            files |= package(
                f"obs/packages/{name}", text=spec(name=name, requires=f"BuildRequires:  {nxt}")
            )
        self.assertEqual(errors_of(files), [])

    def test_cycle_through_a_python_module(self) -> None:
        a = package(
            "obs/packages/aaa",
            text=spec(name="aaa", requires="BuildRequires:  %{python_module bbb}"),
        )
        b = package(
            "obs/packages/python-bbb", text=spec(name="python-bbb", requires="BuildRequires:  aaa")
        )
        errors = errors_of({**a, **b})
        self.assertEqual(len(errors), 1)
        self.assertIn("aaa -> python-bbb -> aaa", errors[0])

    def test_cycle_is_reported_only_for_a_chosen_folder(self) -> None:
        a = package("obs/packages/aaa", text=spec(name="aaa", requires="BuildRequires:  bbb"))
        b = package("obs/packages/bbb", text=spec(name="bbb", requires="BuildRequires:  aaa"))
        c = package("obs/packages/ccc")
        self.assertEqual(errors_of({**a, **b, **c}, folders=["obs/packages/ccc"]), [])

    def test_conditional_buildrequires_are_read(self) -> None:
        text = spec(requires="%if 0%{?suse_version}\nBuildRequires:  cargo >=\n%endif")
        self.assertEqual(len(errors_of(package(text=text))), 1)

    def test_macros_defined_in_the_spec_are_expanded(self) -> None:
        text = spec(requires="BuildRequires:  %{dep} >=").replace(
            "Name: ", "%define dep tool\nName: ", 1
        )
        errors = errors_of(package(text=text))
        self.assertEqual(
            errors, [f"{SPEC}: line 12: BuildRequires tool >=: no version after the operator"]
        )


class TumbleweedList(unittest.TestCase):
    LIST = "# Tumbleweed\nfdupes\nmeson  # build\nc_compiler\n\n"

    def with_list(self, *lines: str, listing: str | None = None) -> list[str]:
        files = {**requires(*lines), "obs/tw.txt": listing if listing is not None else self.LIST}
        return errors_of(files, tumbleweed_list="obs/tw.txt")

    def test_names_in_the_list_are_fine(self) -> None:
        self.assertEqual(self.with_list("fdupes", "meson >= 1", "c_compiler"), [])

    def test_a_name_that_is_not_in_the_list(self) -> None:
        errors = self.with_list("fdupes", "nosuchpkg")
        self.assertEqual(
            errors,
            [f"{SPEC}: line 12: BuildRequires nosuchpkg is not in the Tumbleweed list obs/tw.txt"],
        )

    def test_names_that_the_repo_builds_do_not_need_the_list(self) -> None:
        files = {
            **requires("vilna-lib"),
            **package("obs/packages/vilna-lib", extra={"obs/packages/vilna-lib/demo.conf": "x"}),
            "obs/tw.txt": "fdupes\n",
        }
        self.assertEqual(errors_of(files, tumbleweed_list="obs/tw.txt"), [])

    def test_macros_and_provides_in_brackets_are_skipped(self) -> None:
        self.assertEqual(
            self.with_list("pkgconfig(whatever)", "%{python_module whatever}", "(a or b)"), []
        )

    def test_the_default_list_is_used_when_tracked(self) -> None:
        files = {**requires("nosuchpkg"), co.DEFAULT_LIST: self.LIST}
        errors = errors_of(files)
        self.assertEqual(len(errors), 1)
        self.assertIn(f"is not in the Tumbleweed list {co.DEFAULT_LIST}", errors[0])

    def test_without_a_list_only_the_format_is_checked(self) -> None:
        self.assertEqual(req_errors("nosuchpkg"), [])

    def test_list_that_is_not_utf8(self) -> None:
        files = {**requires("fdupes"), "obs/tw.txt": b"\xff\xfe"}
        errors = errors_of(files, tumbleweed_list="obs/tw.txt")
        self.assertEqual(errors, ["obs/tw.txt: the file is not valid UTF-8"])

    def test_list_that_does_not_exist(self) -> None:
        errors = errors_of(requires("fdupes"), tumbleweed_list="obs/none.txt")
        self.assertEqual(errors, ["obs/none.txt: the file does not exist"])

    def test_list_parsing(self) -> None:
        self.assertEqual(co.parse_list("a\n # c\nb # d\n\n  c  \n"), frozenset("abc"))


class Limits(unittest.TestCase):
    def test_file_larger_than_5_mib_is_refused_not_read(self) -> None:
        big = "# " + "x" * (co.MAX_BYTES + 10)
        errors = errors_of(package(text=big))
        self.assertEqual(errors, [f"{SPEC}: the file is larger than 5 MiB"])

    def test_symbolic_link_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / FOLDER).mkdir(parents=True)
            (root / "target.spec").write_text(spec(), encoding="utf-8")
            (root / SPEC).symlink_to(root / "target.spec")
            problems = co.check_folders(root, [FOLDER], [SPEC], now=NOW)
        self.assertEqual([str(p) for p in problems], [f"{SPEC}: the file is a symbolic link"])

    def test_listed_but_missing_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            problems = co.check_folders(Path(tmp), [FOLDER], [SPEC], now=NOW)
        self.assertEqual([str(p) for p in problems], [f"{SPEC}: the file does not exist"])

    def test_file_names_with_spaces_and_unicode(self) -> None:
        folder = "obs/packages/пакет demo"
        files = package(folder, text=spec(name="пакет demo"), extra={f"{folder}/demo.conf": "x"})
        self.assertIsInstance(problems_of(files), list)

    def test_binary_spec_does_not_crash(self) -> None:
        self.assertEqual(len(errors_of(package(text=bytes(range(256))))), 1)  # type: ignore[arg-type]

    def test_many_dependencies_are_fast(self) -> None:
        start = datetime.now(UTC)
        text = spec(requires="\n".join(f"BuildRequires:  p{i} >= 1" for i in range(5000)))
        self.assertEqual(errors_of(package(text=text)), [])
        self.assertLess((datetime.now(UTC) - start).total_seconds(), 20)


class Legacy(unittest.TestCase):
    def test_legacy_pair_becomes_a_warning(self) -> None:
        problem = co.Problem(SPEC, "x", rule="source")
        old = co.LEGACY
        co.LEGACY = frozenset({(SPEC, "source")})
        try:
            self.assertTrue(co.legacy(problem).warning)
            self.assertFalse(co.legacy(co.Problem(SPEC, "x", rule="changes")).warning)
        finally:
            co.LEGACY = old

    def test_the_repo_has_no_legacy_entries_that_hide_errors(self) -> None:
        for path, rule in co.LEGACY:
            self.assertTrue((ROOT / path).exists(), path)
            self.assertTrue(rule)


class Main(unittest.TestCase):
    def run_main(self, argv: list[str], root: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old = co.ROOT
        co.ROOT = root
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = co.main(argv)
        finally:
            co.ROOT = old
        return code, out.getvalue(), err.getvalue()

    def repo(self, root: Path, files: dict[str, str | bytes | None]) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        names = []
        for rel, text in files.items():
            if text is None:
                continue
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
            names.append(rel)
        subprocess.run(["git", "add", *names], cwd=root, check=True)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_exit_codes_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.repo(root, {"obs/tools/obs_sync.py": OBS_SYNC, **package()})
            (root / f"{FOLDER}/untracked.txt").write_text("x\n", encoding="utf-8")
            code, out, err = self.run_main([], root)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(out, "1 packages checked: 0 errors, 0 warnings\n")
            self.repo(root, requires("cargo >="))
            code, out, err = self.run_main([FOLDER + "/"], root)
            self.assertEqual(code, 1)
            self.assertEqual(
                err, f"{SPEC}: line 11: BuildRequires cargo >=: no version after the operator\n"
            )
            self.assertIn("1 errors", out)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_warnings_do_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.repo(
                root,
                {
                    "obs/tools/obs_sync.py": OBS_SYNC,
                    **package(
                        extra={CHANGES: entry(text="") + entry("Wed Nov 25 09:00:00 UTC 2026")}
                    ),
                },
            )
            code, out, _ = self.run_main([], root)
        self.assertEqual(code, 0)
        self.assertIn("0 errors, 1 warnings", out)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_the_list_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.repo(
                root,
                {"obs/tools/obs_sync.py": OBS_SYNC, **requires("nosuchpkg"), "tw.txt": "fdupes\n"},
            )
            self.assertEqual(self.run_main([], root)[0], 0)
            code, _, err = self.run_main(["--tumbleweed-list", str(root / "tw.txt")], root)
            self.assertEqual(code, 1)
            self.assertIn("is not in the Tumbleweed list tw.txt", err)
            self.assertEqual(self.run_main(["--tumbleweed-list", "nope.txt"], root)[0], 2)
            self.assertEqual(self.run_main(["--tumbleweed-list"], root)[0], 2)
            outside = Path(tmp).parent / "outside-list.txt"
            self.assertEqual(self.run_main(["--tumbleweed-list", str(outside)], root)[0], 2)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_unknown_folder_is_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.repo(root, package())
            code, _, err = self.run_main(["obs/packages/nope"], root)
        self.assertEqual(code, 2)
        self.assertIn("not a package folder", err)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_no_packages_is_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.repo(root, {"README.md": "x\n"})
            code, _, err = self.run_main([], root)
        self.assertEqual(code, 2)
        self.assertIn("no package folders", err)

    def test_wrong_call_is_2_and_help_is_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_main(["--bogus"], Path(tmp))
            self.assertEqual(code, 2)
            self.assertIn("usage:", err)
            code, out, _ = self.run_main(["-h"], Path(tmp))
            self.assertEqual(code, 0)
            self.assertIn("usage:", out)

    def test_outside_git_is_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_main([], Path(tmp))
        self.assertEqual(code, 2)
        self.assertIn("git ls-files", err)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_the_repository_has_no_errors(self) -> None:
        code, out, err = self.run_main([], ROOT)
        self.assertEqual((code, err), (0, ""), err)
        self.assertIn("0 errors", out)


if __name__ == "__main__":
    unittest.main()
