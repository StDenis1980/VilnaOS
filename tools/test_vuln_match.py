#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/vuln_match.py (offline matching of the SBOM against OSV records).

Every OSV record in here is made up: the ids, the packages and the version ranges are
invented for the tests. Nothing is read from the network.

Run: python3 tools/test_vuln_match.py
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import sbom  # noqa: E402  (after sys.path.insert above)
import vuln_match as vm  # noqa: E402

CREATED = "2026-11-27T09:00:00Z"
A, B, C = ("a" * 64, "b" * 64, "c" * 64)
INDEX = "registry+https://github.com/rust-lang/crates.io-index"

UV_LOCK = f"""\
version = 1
revision = 3

[[package]]
name = "Foo_Bar"
version = "1.0"
source = {{ registry = "https://pypi.org/simple" }}
sdist = {{ url = "https://files.example/foo_bar-1.0.tar.gz", hash = "sha256:{A}", size = 1 }}

[[package]]
name = "baz"
version = "2.0+local.1"
source = {{ registry = "https://pypi.org/simple" }}
wheels = [
    {{ url = "https://files.example/baz-2.0-py3-none-any.whl", hash = "sha256:{B}", size = 1 }},
]

[[package]]
name = "app"
version = "1.0.0"
source = {{ editable = "." }}
dependencies = [
    {{ name = "foo-bar" }},
    {{ name = "baz" }},
]
"""

CARGO_LOCK = f"""\
version = 4

[[package]]
name = "app-core"
version = "0.3.0"
dependencies = [
 "serde",
]

[[package]]
name = "serde"
version = "1.0.150"
source = "{INDEX}"
checksum = "{C}"
"""


def affected(
    ecosystem: str,
    name: str,
    events: list[tuple[str, str]] | None = None,
    versions: list[str] | None = None,
    kind: str = "ECOSYSTEM",
) -> dict:
    entry: dict = {"package": {"ecosystem": ecosystem, "name": name}}
    if events is not None:
        entry["ranges"] = [{"type": kind, "events": [{k: v} for k, v in events]}]
    if versions is not None:
        entry["versions"] = versions
    return entry


def record(ident: str, *entries: dict, **extra: object) -> dict:
    return {"id": ident, "affected": list(entries), **extra}


def run_match(
    packages: list[vm.Package], records: list[dict]
) -> tuple[list[vm.Finding], list[sbom.Problem]]:
    problems: list[sbom.Problem] = []
    parsed = vm.parse_records(records, "osv.json", problems)
    return vm.match(packages, parsed, problems), problems


def hits(
    ecosystem: str, name: str, version: str, entry: dict, ident: str = "FAKE-1"
) -> list[vm.Finding]:
    found, _ = run_match([vm.Package(ecosystem, name, version)], [record(ident, entry)])
    return found


def pypi_range(events: list[tuple[str, str]], version: str) -> list[vm.Finding]:
    return hits("pypi", "foo", version, affected("PyPI", "foo", events))


def cargo_range(events: list[tuple[str, str]], version: str) -> list[vm.Finding]:
    return hits("cargo", "foo", version, affected("crates.io", "foo", events, kind="SEMVER"))


def write(root: Path, files: dict[str, str]) -> list[str]:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return sorted(files)


class Pep440Tests(unittest.TestCase):
    def test_the_order_of_the_pep(self) -> None:
        ordered = [
            "0.9",
            "1.0.dev1",
            "1.0a1.dev1",
            "1.0a1",
            "1.0a2",
            "1.0b1",
            "1.0rc1",
            "1.0rc1.post1",
            "1.0",
            "1.0+abc",
            "1.0+abc.5",
            "1.0+abc.10",
            "1.0.post1.dev1",
            "1.0.post1",
            "1.0.1",
            "1.1",
            "2.0",
            "1!0.1",
        ]
        keys = [vm.pep440_key(v) for v in ordered]
        self.assertNotIn(None, keys)
        for low, high in zip(ordered, ordered[1:], strict=False):
            self.assertLess(vm.pep440_key(low), vm.pep440_key(high), f"{low} < {high}")

    def test_equal_versions(self) -> None:
        for a, b in [
            ("1.0", "1.0.0"),
            ("1.0", "v1.0"),
            ("1.0a1", "1.0alpha1"),
            ("1.0rc1", "1.0c1"),
            ("1.0.post1", "1.0-1"),
            ("1.0.post1", "1.0.rev1"),
            ("1.0.RC.1", "1.0rc1"),
            ("1.0.dev", "1.0.dev0"),
            ("1.0+Ab-1", "1.0+ab.1"),
        ]:
            self.assertEqual(vm.pep440_key(a), vm.pep440_key(b), f"{a} == {b}")

    def test_not_a_version(self) -> None:
        for bad in ["", "abc", "1.0.", "1..0", "1.0-", "1.0+", "1.0+a..b", "1.0 beta x", "1.x"]:
            self.assertIsNone(vm.pep440_key(bad), bad)

    def test_a_very_long_version_is_not_parsed(self) -> None:
        self.assertIsNone(vm.pep440_key("1." * 100 + "1"))
        self.assertIsNone(vm.semver_key("1.0.0-" + "a" * 200))

    def test_hostile_text_is_quick(self) -> None:
        for text in ["1" * 127 + "x", "1." * 63 + "a", "1.0" + ".a" * 60, "1.0+" + "a." * 60]:
            vm.pep440_key(text)


class SemverTests(unittest.TestCase):
    def test_the_order_of_the_spec(self) -> None:
        ordered = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
            "1.0.1",
            "1.10.0",
            "2.0.0",
        ]
        for low, high in zip(ordered, ordered[1:], strict=False):
            self.assertLess(vm.semver_key(low), vm.semver_key(high), f"{low} < {high}")

    def test_numbers_are_numbers(self) -> None:
        self.assertLess(vm.semver_key("1.9.0"), vm.semver_key("1.10.0"))
        self.assertLess(vm.semver_key("0.2.9"), vm.semver_key("0.10.0"))

    def test_build_metadata_does_not_count(self) -> None:
        self.assertEqual(vm.semver_key("1.0.0+a"), vm.semver_key("1.0.0+b"))
        self.assertEqual(vm.semver_key("1.0.0+a"), vm.semver_key("1.0.0"))

    def test_not_a_version(self) -> None:
        for bad in ["", "1", "1.0", "01.0.0", "1.0.0-", "1.0.0-01", "v1.0.0", "1.0.0.0", "a.b.c"]:
            self.assertIsNone(vm.semver_key(bad), bad)

    def test_the_ecosystem_picks_the_rule(self) -> None:
        self.assertIsNotNone(vm.version_key("pypi", "1.0"))
        self.assertIsNone(vm.version_key("cargo", "1.0"))


class PurlTests(unittest.TestCase):
    def test_pypi_and_cargo(self) -> None:
        self.assertEqual(
            vm.parse_purl("pkg:pypi/foo-bar@1.0"), vm.Package("pypi", "foo-bar", "1.0")
        )
        self.assertEqual(
            vm.parse_purl("pkg:cargo/serde@1.0.150"), vm.Package("cargo", "serde", "1.0.150")
        )

    def test_percent_encoding_and_qualifiers(self) -> None:
        self.assertEqual(
            vm.parse_purl("pkg:pypi/baz@2.0%2Blocal.1"), vm.Package("pypi", "baz", "2.0+local.1")
        )
        self.assertEqual(
            vm.parse_purl("pkg:cargo/x@1.0.0?arch=a#sub"), vm.Package("cargo", "x", "1.0.0")
        )

    def test_other_purls_are_not_packages(self) -> None:
        for other in ["pkg:github/o/r@v1", "pkg:npm/x@1.0.0", "pkg:pypi/noversion", "", "x"]:
            self.assertIsNone(vm.parse_purl(other), other)

    def test_packages_of_a_document(self) -> None:
        doc = {
            "packages": [
                {"externalRefs": [{"referenceType": "purl", "referenceLocator": "pkg:pypi/b@2"}]},
                {"externalRefs": [{"referenceType": "purl", "referenceLocator": "pkg:pypi/a@1"}]},
                {"externalRefs": [{"referenceType": "purl", "referenceLocator": "pkg:pypi/a@1"}]},
                {
                    "externalRefs": [
                        {"referenceType": "cpe23Type", "referenceLocator": "pkg:pypi/c@1"}
                    ]
                },
                {
                    "externalRefs": [
                        {"referenceType": "purl", "referenceLocator": "pkg:github/o/r@v"}
                    ]
                },
                {"name": "no refs"},
                "not a package",
                {"externalRefs": "not a list"},
                {"externalRefs": [7, {"referenceType": "purl", "referenceLocator": 5}]},
            ]
        }
        self.assertEqual(
            vm.packages_of(doc), [vm.Package("pypi", "a", "1"), vm.Package("pypi", "b", "2")]
        )

    def test_not_a_document(self) -> None:
        for doc in (None, [], "x", {"packages": "x"}, {}):
            self.assertEqual(vm.packages_of(doc), [])


class PypiRangeTests(unittest.TestCase):
    def test_introduced_zero_and_fixed(self) -> None:
        events = [("introduced", "0"), ("fixed", "2.0")]
        found = pypi_range(events, "1.9")
        self.assertEqual([f.fixed for f in found], [["2.0"]])
        self.assertEqual(pypi_range(events, "2.0"), [])
        self.assertEqual(pypi_range(events, "2.0.1"), [])
        self.assertEqual(len(pypi_range(events, "0.0.1")), 1)

    def test_a_pre_release_of_the_fix_is_still_affected(self) -> None:
        events = [("introduced", "0"), ("fixed", "2.0")]
        self.assertEqual(len(pypi_range(events, "2.0rc1")), 1)
        self.assertEqual(len(pypi_range(events, "2.0.dev3")), 1)
        self.assertEqual(pypi_range(events, "2.0.post1"), [])

    def test_a_local_version_is_after_its_base(self) -> None:
        events = [("introduced", "0"), ("fixed", "2.0")]
        self.assertEqual(pypi_range(events, "2.0+local.1"), [])
        self.assertEqual(len(pypi_range([("introduced", "0"), ("fixed", "2.1")], "2.0+local.1")), 1)

    def test_introduced_in_the_middle(self) -> None:
        events = [("introduced", "1.2"), ("fixed", "1.5")]
        self.assertEqual(pypi_range(events, "1.1.9"), [])
        self.assertEqual(len(pypi_range(events, "1.2")), 1)
        self.assertEqual(len(pypi_range(events, "1.4.9")), 1)
        self.assertEqual(pypi_range(events, "1.5"), [])
        self.assertEqual(pypi_range(events, "1.2.dev1"), [])

    def test_last_affected_is_inclusive_and_has_no_fix(self) -> None:
        events = [("introduced", "0"), ("last_affected", "1.5")]
        found = pypi_range(events, "1.5")
        self.assertEqual([f.fixed for f in found], [[]])
        self.assertEqual(found[0].row()[3], "-")
        self.assertEqual(pypi_range(events, "1.5.1"), [])

    def test_introduced_only_affects_every_later_version(self) -> None:
        events = [("introduced", "3.0")]
        self.assertEqual(pypi_range(events, "2.9"), [])
        self.assertEqual(len(pypi_range(events, "3.0")), 1)
        self.assertEqual(len(pypi_range(events, "99")), 1)

    def test_two_windows_in_one_range(self) -> None:
        events = [
            ("introduced", "0"),
            ("fixed", "1.0"),
            ("introduced", "2.0"),
            ("fixed", "2.5"),
        ]
        self.assertEqual(len(pypi_range(events, "0.5")), 1)
        self.assertEqual(pypi_range(events, "1.5"), [])
        found = pypi_range(events, "2.2")
        self.assertEqual([f.fixed for f in found], [["2.5"]])
        self.assertEqual(pypi_range(events, "2.5"), [])

    def test_events_in_any_order(self) -> None:
        events = [("fixed", "2.5"), ("introduced", "2.0"), ("fixed", "1.0"), ("introduced", "0")]
        self.assertEqual(pypi_range(events, "1.5"), [])
        self.assertEqual(len(pypi_range(events, "2.2")), 1)

    def test_fixed_in_is_the_lowest_fix_above_the_version(self) -> None:
        events = [("introduced", "0"), ("fixed", "1.2"), ("introduced", "1.4"), ("fixed", "1.9")]
        self.assertEqual([f.fixed for f in pypi_range(events, "1.0")], [["1.2"]])
        self.assertEqual([f.fixed for f in pypi_range(events, "1.5")], [["1.9"]])

    def test_introduced_and_fixed_at_the_same_version_is_empty(self) -> None:
        events = [("introduced", "1.0"), ("fixed", "1.0")]
        for version in ("0.9", "1.0", "1.1"):
            self.assertEqual(pypi_range(events, version), [], version)

    def test_two_ranges_of_one_entry(self) -> None:
        entry = {
            "package": {"ecosystem": "PyPI", "name": "foo"},
            "ranges": [
                {"type": "ECOSYSTEM", "events": [{"introduced": "1.0"}, {"fixed": "1.2"}]},
                {"type": "ECOSYSTEM", "events": [{"introduced": "3.0"}, {"fixed": "3.4"}]},
            ],
        }
        self.assertEqual([f.fixed for f in hits("pypi", "foo", "1.1", entry)], [["1.2"]])
        self.assertEqual([f.fixed for f in hits("pypi", "foo", "3.1", entry)], [["3.4"]])
        self.assertEqual(hits("pypi", "foo", "2.0", entry), [])

    def test_the_limit_event_is_ignored(self) -> None:
        events = [("introduced", "0"), ("limit", "1.0"), ("fixed", "2.0")]
        self.assertEqual(len(pypi_range(events, "1.5")), 1)

    def test_a_semver_range_is_read_as_the_ecosystem_says(self) -> None:
        entry = affected("PyPI", "foo", [("introduced", "0"), ("fixed", "1.0")], kind="SEMVER")
        self.assertEqual(len(hits("pypi", "foo", "0.5", entry)), 1)

    def test_a_git_range_is_skipped(self) -> None:
        entry = affected("PyPI", "foo", [("introduced", "0"), ("fixed", "abc123")], kind="GIT")
        found, problems = run_match([vm.Package("pypi", "foo", "1.0")], [record("FAKE-1", entry)])
        self.assertEqual(found, [])
        self.assertEqual(problems, [])


class CargoRangeTests(unittest.TestCase):
    def test_introduced_zero_and_fixed(self) -> None:
        events = [("introduced", "0.0.0-0"), ("fixed", "1.2.3")]
        found = cargo_range(events, "1.2.2")
        self.assertEqual([f.fixed for f in found], [["1.2.3"]])
        self.assertEqual(cargo_range(events, "1.2.3"), [])
        self.assertEqual(cargo_range(events, "1.10.0"), [])

    def test_numbers_compare_as_numbers(self) -> None:
        events = [("introduced", "1.2.0"), ("fixed", "1.10.0")]
        self.assertEqual(len(cargo_range(events, "1.9.5")), 1)
        self.assertEqual(cargo_range(events, "1.10.0"), [])

    def test_a_pre_release_is_before_its_release(self) -> None:
        events = [("introduced", "0"), ("fixed", "1.0.0")]
        self.assertEqual(len(cargo_range(events, "1.0.0-rc.1")), 1)
        self.assertEqual(cargo_range(events, "1.0.0+build.5"), [])
        self.assertEqual(
            len(cargo_range([("introduced", "1.0.0-rc.2"), ("fixed", "1.0.0")], "1.0.0-rc.10")), 1
        )
        self.assertEqual(
            cargo_range([("introduced", "1.0.0-rc.2"), ("fixed", "1.0.0")], "1.0.0-rc.1"), []
        )

    def test_last_affected(self) -> None:
        events = [("introduced", "0"), ("last_affected", "0.4.2")]
        self.assertEqual(len(cargo_range(events, "0.4.2")), 1)
        self.assertEqual(cargo_range(events, "0.4.3"), [])

    def test_an_ecosystem_range_works_for_crates_too(self) -> None:
        entry = affected("crates.io", "foo", [("introduced", "0"), ("fixed", "2.0.0")])
        self.assertEqual(len(hits("cargo", "foo", "1.0.0", entry)), 1)


class VersionListTests(unittest.TestCase):
    def test_listed_versions(self) -> None:
        entry = affected("PyPI", "foo", versions=["1.0", "1.1"])
        self.assertEqual(len(hits("pypi", "foo", "1.1", entry)), 1)
        self.assertEqual(hits("pypi", "foo", "1.2", entry), [])

    def test_pypi_versions_are_compared_as_versions(self) -> None:
        entry = affected("PyPI", "foo", versions=["1.0"])
        self.assertEqual(len(hits("pypi", "foo", "1.0.0", entry)), 1)
        self.assertEqual(len(hits("pypi", "foo", "1.0.0.0", entry)), 1)

    def test_crates_versions_are_exact(self) -> None:
        entry = affected("crates.io", "foo", versions=["1.0.0"])
        self.assertEqual(len(hits("cargo", "foo", "1.0.0", entry)), 1)
        self.assertEqual(hits("cargo", "foo", "1.0.1", entry), [])

    def test_a_listed_version_has_no_fixed_in(self) -> None:
        entry = affected("PyPI", "foo", versions=["1.0"])
        self.assertEqual(hits("pypi", "foo", "1.0", entry)[0].row()[3], "-")

    def test_versions_and_ranges_together(self) -> None:
        entry = affected("PyPI", "foo", [("introduced", "2.0"), ("fixed", "2.5")], ["1.0"])
        self.assertEqual(len(hits("pypi", "foo", "1.0", entry)), 1)
        self.assertEqual(len(hits("pypi", "foo", "2.1", entry)), 1)
        self.assertEqual(hits("pypi", "foo", "1.5", entry), [])


class NameTests(unittest.TestCase):
    def test_pypi_names_are_normalised(self) -> None:
        entry = affected("PyPI", "Foo_Bar.Baz", [("introduced", "0")])
        self.assertEqual(len(hits("pypi", "foo-bar-baz", "1.0", entry)), 1)
        self.assertEqual(len(hits("pypi", "FOO.bar_baz", "1.0", entry)), 1)

    def test_crate_names_are_exact(self) -> None:
        entry = affected("crates.io", "Serde", [("introduced", "0")])
        self.assertEqual(hits("cargo", "serde", "1.0.0", entry), [])
        self.assertEqual(len(hits("cargo", "Serde", "1.0.0", entry)), 1)

    def test_a_package_of_another_name_is_not_matched(self) -> None:
        entry = affected("PyPI", "foo", [("introduced", "0")])
        self.assertEqual(hits("pypi", "foo2", "1.0", entry), [])
        self.assertEqual(hits("pypi", "fo", "1.0", entry), [])

    def test_the_ecosystems_are_kept_apart(self) -> None:
        pypi_entry = affected("PyPI", "foo", [("introduced", "0")])
        crates_entry = affected("crates.io", "foo", [("introduced", "0")])
        self.assertEqual(hits("cargo", "foo", "1.0.0", pypi_entry), [])
        self.assertEqual(hits("pypi", "foo", "1.0", crates_entry), [])

    def test_the_ecosystem_name_ignores_case(self) -> None:
        entry = affected("pypi", "foo", [("introduced", "0")])
        self.assertEqual(len(hits("pypi", "foo", "1.0", entry)), 1)


class RecordTests(unittest.TestCase):
    def test_other_ecosystems_are_ignored(self) -> None:
        found, problems = run_match(
            [vm.Package("pypi", "foo", "1.0")],
            [
                record(
                    "FAKE-1",
                    affected("npm", "foo", [("introduced", "0")]),
                    affected("Debian:12", "foo", [("introduced", "0")]),
                    affected("Go", "foo", versions=["1.0"]),
                )
            ],
        )
        self.assertEqual((found, problems), ([], []))

    def test_withdrawn_records_are_skipped(self) -> None:
        entry = affected("PyPI", "foo", [("introduced", "0")])
        found, problems = run_match(
            [vm.Package("pypi", "foo", "1.0")],
            [record("FAKE-1", entry, withdrawn="2026-01-01T00:00:00Z")],
        )
        self.assertEqual((found, problems), ([], []))

    def test_aliases_and_summary(self) -> None:
        entry = affected("PyPI", "foo", [("introduced", "0"), ("fixed", "2.0")])
        found, _ = run_match(
            [vm.Package("pypi", "foo", "1.0")],
            [
                record(
                    "FAKE-PYSEC-1", entry, aliases=["CVE-0000-0001", "FAKE-GHSA-x", 7], summary="S"
                )
            ],
        )
        self.assertEqual(found[0].aliases, ["CVE-0000-0001", "FAKE-GHSA-x"])
        self.assertEqual(found[0].summary, "S")
        self.assertEqual(
            found[0].row(), ("foo", "1.0", "FAKE-PYSEC-1 (CVE-0000-0001, FAKE-GHSA-x)", "2.0")
        )

    def test_one_finding_per_package_and_id(self) -> None:
        entry = affected("PyPI", "foo", [("introduced", "0"), ("fixed", "2.0")])
        other = affected("PyPI", "Foo", [("introduced", "0"), ("fixed", "3.0")])
        found, _ = run_match(
            [vm.Package("pypi", "foo", "1.0")],
            [record("FAKE-1", entry, other), record("FAKE-1", entry)],
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].fixed, ["2.0", "3.0"])

    def test_a_package_can_have_many_ids(self) -> None:
        entry = affected("PyPI", "foo", [("introduced", "0"), ("fixed", "2.0")])
        found, _ = run_match(
            [vm.Package("pypi", "foo", "1.0")],
            [record("FAKE-2", entry), record("FAKE-1", entry)],
        )
        self.assertEqual([f.ident for f in found], ["FAKE-1", "FAKE-2"])

    def test_the_findings_are_sorted(self) -> None:
        entry_a = affected("PyPI", "aaa", [("introduced", "0")])
        entry_b = affected("PyPI", "bbb", [("introduced", "0")])
        found, _ = run_match(
            [
                vm.Package("pypi", "bbb", "1.0"),
                vm.Package("pypi", "aaa", "2.0"),
                vm.Package("pypi", "aaa", "1.0"),
            ],
            [record("FAKE-9", entry_b, entry_a)],
        )
        self.assertEqual(
            [(f.name, f.version) for f in found], [("aaa", "1.0"), ("aaa", "2.0"), ("bbb", "1.0")]
        )

    def test_a_record_with_no_id_is_an_error(self) -> None:
        _, problems = run_match([], [{"affected": []}, {"id": ""}, {"id": 5}, "text", 7])
        self.assertEqual(len(problems), 5)
        self.assertTrue(all(not p.warning for p in problems))
        self.assertIn("osv.json: record 1: no id", [str(p) for p in problems])

    def test_a_broken_record_does_not_stop_the_others(self) -> None:
        good = record("FAKE-1", affected("PyPI", "foo", [("introduced", "0")]))
        found, problems = run_match(
            [vm.Package("pypi", "foo", "1.0")],
            [{"id": "FAKE-BAD", "affected": "no"}, 12, good],
        )
        self.assertEqual([f.ident for f in found], ["FAKE-1"])
        self.assertEqual(len(problems), 2)

    def test_broken_affected_entries(self) -> None:
        for bad in (
            7,
            {"package": {"ecosystem": "PyPI"}},
            {"package": {"ecosystem": "PyPI", "name": ""}},
            {"package": {"ecosystem": "PyPI", "name": "foo"}, "ranges": "x"},
            {"package": {"ecosystem": "PyPI", "name": "foo"}, "versions": 3},
            {"package": {"ecosystem": "PyPI", "name": "foo"}, "ranges": [5]},
            {"package": {"ecosystem": "PyPI", "name": "foo"}, "ranges": [{"type": "GIT"}]},
        ):
            found, problems = run_match([vm.Package("pypi", "foo", "1.0")], [record("FAKE-1", bad)])
            self.assertEqual(found, [], bad)
            self.assertTrue(problems and not problems[0].warning, bad)

    def test_an_entry_without_a_package_is_not_an_error(self) -> None:
        found, problems = run_match(
            [vm.Package("pypi", "foo", "1.0")], [record("FAKE-1", {"ranges": []}, {"package": 5})]
        )
        self.assertEqual((found, problems), ([], []))

    def test_an_entry_with_nothing_to_match_is_a_warning(self) -> None:
        found, problems = run_match(
            [vm.Package("pypi", "foo", "1.0")],
            [record("FAKE-1", {"package": {"ecosystem": "PyPI", "name": "foo"}})],
        )
        self.assertEqual(found, [])
        self.assertEqual([p.warning for p in problems], [True])

    def test_a_single_record_object_is_accepted(self) -> None:
        problems: list[sbom.Problem] = []
        records = vm.parse_records(record("FAKE-1"), "osv.json", problems)
        self.assertEqual([r.ident for r in records], ["FAKE-1"])

    def test_an_event_that_is_not_understood_is_skipped_with_a_warning(self) -> None:
        entry = {
            "package": {"ecosystem": "PyPI", "name": "foo"},
            "ranges": [
                {
                    "type": "ECOSYSTEM",
                    "events": [
                        {"introduced": "0"},
                        {"fixed": "not a version"},
                        {"zzz": "1"},
                        {"fixed": 3},
                        "x",
                    ],
                }
            ],
        }
        found, problems = run_match([vm.Package("pypi", "foo", "5.0")], [record("FAKE-1", entry)])
        self.assertEqual(len(found), 1)
        self.assertEqual([p.warning for p in problems], [True] * 4)

    def test_a_version_that_cannot_be_parsed(self) -> None:
        entry = affected("PyPI", "foo", [("introduced", "0")], versions=["weird-1"])
        found, problems = run_match(
            [vm.Package("pypi", "foo", "weird-1")], [record("FAKE-1", entry)]
        )
        self.assertEqual(len(found), 1)  # the listed version equals as a string
        self.assertEqual([p.warning for p in problems], [True])
        found, _ = run_match([vm.Package("pypi", "foo", "weird-2")], [record("FAKE-1", entry)])
        self.assertEqual(found, [])

    def test_no_warning_for_a_package_nobody_reports(self) -> None:
        entry = affected("PyPI", "other", [("introduced", "0")])
        _, problems = run_match(
            [vm.Package("pypi", "weird-1", "not a version")], [record("FAKE-1", entry)]
        )
        self.assertEqual(problems, [])


class OutputTests(unittest.TestCase):
    def finding(self, name: str, version: str, ident: str, fixed: list[str]) -> vm.Finding:
        return vm.Finding("pypi", name, version, ident, [], fixed, "")

    def test_the_table(self) -> None:
        table = vm.render_table(
            [
                self.finding("foo", "1.0", "FAKE-1", ["1.2"]),
                self.finding("longer-name", "10.20.30", "FAKE-22", []),
            ]
        )
        self.assertEqual(
            table,
            "package     | version  | identifier | fixed in\n"
            "------------+----------+------------+---------\n"
            "foo         | 1.0      | FAKE-1     | 1.2\n"
            "longer-name | 10.20.30 | FAKE-22    | -\n",
        )

    def test_the_json(self) -> None:
        found = vm.Finding("cargo", "serde", "1.0.1", "FAKE-1", ["CVE-0-1"], ["1.0.2"], "Sum")
        self.assertEqual(
            json.loads(vm.render_json([found])),
            [
                {
                    "ecosystem": "cargo",
                    "package": "serde",
                    "version": "1.0.1",
                    "id": "FAKE-1",
                    "aliases": ["CVE-0-1"],
                    "fixed_in": ["1.0.2"],
                    "summary": "Sum",
                }
            ],
        )

    def test_control_characters_do_not_reach_the_terminal(self) -> None:
        evil = "FAKE-1\x1b[31m\nfake row\r\u202e\u0000"
        entry = affected("PyPI", "foo", [("introduced", "0")])
        found, _ = run_match(
            [vm.Package("pypi", "foo", "1.0")],
            [record(evil, entry, aliases=["a\nb"], summary="x\ty\x07")],
        )
        table = vm.render_table(found)
        self.assertEqual(table.count("\n"), 3)
        for bad in ("\x1b", "\r", "\u202e", "\x00", "\x07", "\t"):
            self.assertNotIn(bad, table)
        self.assertNotIn("\n", found[0].summary)


class LoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def load(self, text: str | bytes) -> tuple[list[vm.Record] | None, list[sbom.Problem]]:
        path = self.dir / "osv.json"
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        problems: list[sbom.Problem] = []
        return vm.load_osv(str(path), problems), problems

    def test_an_array(self) -> None:
        records, problems = self.load(json.dumps([record("FAKE-1"), record("FAKE-2")]))
        self.assertEqual([r.ident for r in records or []], ["FAKE-1", "FAKE-2"])
        self.assertEqual(problems, [])

    def test_unusable_files(self) -> None:
        for text in ("", "not json", "[1, 2", '"text"', "7", "null", b"\xff\xfe\x00bad"):
            records, problems = self.load(text)
            self.assertIsNone(records, text)
            self.assertEqual(len(problems), 1, text)
            self.assertTrue(str(problems[0]).startswith(str(self.dir / "osv.json") + ": "))

    def test_a_file_that_is_missing(self) -> None:
        problems: list[sbom.Problem] = []
        self.assertIsNone(vm.load_osv(str(self.dir / "nope.json"), problems))
        self.assertIn("cannot read", str(problems[0]))

    def test_a_folder_is_not_a_file(self) -> None:
        problems: list[sbom.Problem] = []
        self.assertIsNone(vm.load_osv(str(self.dir), problems))

    def test_very_deep_json(self) -> None:
        records, problems = self.load("[" * 100000 + "]" * 100000)
        self.assertIsNone(records)
        self.assertEqual(len(problems), 1)

    def test_a_file_over_the_size_limit(self) -> None:
        (self.dir / "osv.json").write_text("[]", encoding="utf-8")
        problems: list[sbom.Problem] = []
        with mock.patch.object(vm, "MAX_FILE_BYTES", 1):
            self.assertIsNone(vm.load_osv(str(self.dir / "osv.json"), problems))
        self.assertIn("larger than", str(problems[0]))


def run_main(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with (
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
        mock.patch.object(socket, "socket", side_effect=AssertionError("network used")),
    ):
        code = vm.main(argv)
    return code, out.getvalue(), err.getvalue()


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.repo = self.dir / "repo"
        self.files = write(self.repo, {"app/uv.lock": UV_LOCK, "app/Cargo.lock": CARGO_LOCK})
        patcher = mock.patch.object(sbom, "tracked_files", lambda root: self.files)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.osv = self.dir / "osv.json"
        self.write_osv(
            [
                record(
                    "FAKE-2026-0001",
                    affected("PyPI", "foo-bar", [("introduced", "0"), ("fixed", "1.1")]),
                    aliases=["CVE-2026-0001"],
                ),
                record("FAKE-2026-0002", affected("crates.io", "serde", versions=["1.0.150"])),
                record(
                    "FAKE-2026-0003",
                    affected("PyPI", "baz", [("introduced", "0"), ("fixed", "2.0")]),
                ),
            ]
        )

    def write_osv(self, records: object) -> None:
        self.osv.write_text(json.dumps(records), encoding="utf-8")

    def main(self, *extra: str) -> tuple[int, str, str]:
        return run_main(["--osv", str(self.osv), "--root", str(self.repo), *extra])

    def test_findings_from_the_repo(self) -> None:
        code, out, err = self.main()
        self.assertEqual(code, 1, err)
        rows = [[c.strip() for c in line.split(" | ")] for line in out.splitlines()]
        self.assertEqual(rows[0], ["package", "version", "identifier", "fixed in"])
        self.assertEqual(
            rows[2:],
            [
                ["foo-bar", "1.0", "FAKE-2026-0001 (CVE-2026-0001)", "1.1"],
                ["serde", "1.0.150", "FAKE-2026-0002", "-"],
            ],
        )
        self.assertIn("3 packages checked against 3 records: 2 vulnerabilities", err)

    def test_the_local_version_is_not_affected_by_the_fix_below_it(self) -> None:
        _, out, _ = self.main()
        self.assertNotIn("FAKE-2026-0003", out)

    def test_report_only_exits_zero(self) -> None:
        code, out, _ = self.main("--report-only")
        self.assertEqual(code, 0)
        self.assertIn("FAKE-2026-0001", out)

    def test_nothing_found_is_zero_and_prints_no_table(self) -> None:
        self.write_osv([record("FAKE-1", affected("PyPI", "unrelated", [("introduced", "0")]))])
        code, out, err = self.main()
        self.assertEqual((code, out), (0, ""))
        self.assertIn("0 vulnerabilities", err)

    def test_json_output(self) -> None:
        code, out, _ = self.main("--json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual([d["id"] for d in data], ["FAKE-2026-0001", "FAKE-2026-0002"])
        self.assertEqual(data[0]["ecosystem"], "pypi")
        self.assertEqual(data[0]["fixed_in"], ["1.1"])

    def test_json_with_nothing_found_is_an_empty_list(self) -> None:
        self.write_osv([])
        code, out, _ = self.main("--json")
        self.assertEqual((code, json.loads(out)), (0, []))

    def test_several_osv_files(self) -> None:
        second = self.dir / "second.json"
        second.write_text(
            json.dumps(
                [
                    record(
                        "FAKE-2026-0009", affected("crates.io", "serde", [("introduced", "1.0.0")])
                    )
                ]
            ),
            encoding="utf-8",
        )
        code, out, _ = run_main(
            ["--osv", str(self.osv), "--osv", str(second), "--root", str(self.repo), "--json"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            [d["id"] for d in json.loads(out)],
            ["FAKE-2026-0001", "FAKE-2026-0002", "FAKE-2026-0009"],
        )

    def test_an_sbom_file(self) -> None:
        doc, _ = sbom.build(self.repo, self.files, "0.0.0-dev", CREATED)
        path = self.dir / "vilna.spdx.json"
        path.write_text(sbom.render(doc), encoding="utf-8")
        code, out, _ = run_main(["--osv", str(self.osv), "--sbom", str(path), "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(len(json.loads(out)), 2)

    def test_an_sbom_file_that_is_not_json(self) -> None:
        path = self.dir / "bad.json"
        path.write_text("{", encoding="utf-8")
        code, out, err = run_main(["--osv", str(self.osv), "--sbom", str(path)])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("cannot read", err)

    def test_an_sbom_file_that_is_very_deep(self) -> None:
        path = self.dir / "deep.json"
        path.write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
        code, out, err = run_main(["--osv", str(self.osv), "--sbom", str(path)])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("deep.json: cannot read", err)

    def test_an_sbom_file_that_is_missing(self) -> None:
        code, _, err = run_main(["--osv", str(self.osv), "--sbom", str(self.dir / "none.json")])
        self.assertEqual(code, 2)
        self.assertIn("none.json: cannot read", err)

    def test_an_sbom_without_packages_finds_nothing(self) -> None:
        path = self.dir / "empty.json"
        path.write_text("{}", encoding="utf-8")
        code, _, err = run_main(["--osv", str(self.osv), "--sbom", str(path)])
        self.assertEqual(code, 0)
        self.assertIn("0 packages checked", err)

    def test_osv_is_required(self) -> None:
        code, out, err = run_main(["--root", str(self.repo)])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--osv", err)

    def test_wrong_calls(self) -> None:
        for argv in (["--nope"], ["--osv"], ["--osv", "x", "--sbom", "a", "--root", "b"]):
            self.assertEqual(run_main(argv)[0], 2, argv)

    def test_help_is_zero(self) -> None:
        self.assertEqual(run_main(["--help"])[0], 0)

    def test_an_osv_file_that_cannot_be_read_is_two(self) -> None:
        code, out, err = run_main(["--osv", str(self.dir / "nope.json"), "--root", str(self.repo)])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("nope.json: cannot read", err)

    def test_one_bad_file_among_good_ones_is_two(self) -> None:
        bad = self.dir / "bad.json"
        bad.write_text("nonsense", encoding="utf-8")
        code, out, _ = run_main(
            ["--osv", str(self.osv), "--osv", str(bad), "--root", str(self.repo)]
        )
        self.assertEqual((code, out), (2, ""))

    def test_a_broken_record_is_an_error_but_the_rest_is_used(self) -> None:
        self.write_osv(
            [
                {"affected": []},
                record("FAKE-1", affected("crates.io", "serde", [("introduced", "0")])),
            ]
        )
        code, out, err = self.main("--report-only")
        self.assertEqual(code, 0)
        self.assertIn("FAKE-1", out)
        self.assertIn("record 1: no id", err)
        self.assertEqual(self.main()[0], 1)

    def test_a_broken_record_alone_is_one(self) -> None:
        self.write_osv([{"affected": []}])
        self.assertEqual(self.main()[0], 1)

    def test_warnings_alone_do_not_fail(self) -> None:
        self.write_osv([record("FAKE-1", {"package": {"ecosystem": "PyPI", "name": "foo-bar"}})])
        code, _, err = self.main()
        self.assertEqual(code, 0)
        self.assertIn("warning: FAKE-1: foo-bar has neither versions nor ranges", err)

    def test_the_sbom_cannot_be_built(self) -> None:
        files = write(self.repo, {"bad/uv.lock": "this is [not toml"})
        with mock.patch.object(sbom, "tracked_files", lambda root: [*self.files, *files]):
            code, out, err = self.main()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("bad/uv.lock", err)

    def test_git_cannot_list_the_files(self) -> None:
        with mock.patch.object(sbom, "tracked_files", side_effect=OSError("no git")):
            code, _, err = self.main()
        self.assertEqual(code, 2)
        self.assertIn("cannot list files with git ls-files", err)

    def test_nothing_is_written(self) -> None:
        before = sorted(p.name for p in self.dir.rglob("*"))
        self.main()
        self.assertEqual(sorted(p.name for p in self.dir.rglob("*")), before)

    def test_no_process_is_started(self) -> None:
        with mock.patch.object(vm.subprocess, "run", side_effect=AssertionError("process")):
            self.assertEqual(self.main()[0], 1)


@unittest.skipUnless((ROOT / ".git").exists(), "not a git checkout")
class RealRepoTests(unittest.TestCase):
    def test_a_made_up_record_for_a_real_package_is_found(self) -> None:
        try:
            files = sbom.tracked_files(ROOT)
        except (OSError, sbom.subprocess.CalledProcessError):
            self.skipTest("git is not available")
        doc, _ = sbom.build(ROOT, files, sbom.DEFAULT_VERSION, CREATED)
        packages = vm.packages_of(doc)
        self.assertTrue({"pypi", "cargo"} <= {p.ecosystem for p in packages})
        pypi = next(p for p in packages if p.ecosystem == "pypi")
        cargo = next(p for p in packages if p.ecosystem == "cargo")
        records = [
            record("FAKE-REAL-1", affected("PyPI", pypi.name, versions=[pypi.version])),
            record("FAKE-REAL-2", affected("crates.io", cargo.name, versions=[cargo.version])),
        ]
        found, problems = run_match(packages, records)
        self.assertEqual(problems, [])
        self.assertLessEqual({"FAKE-REAL-1", "FAKE-REAL-2"}, {f.ident for f in found})

    def test_every_version_of_the_sbom_can_be_compared(self) -> None:
        try:
            files = sbom.tracked_files(ROOT)
        except (OSError, sbom.subprocess.CalledProcessError):
            self.skipTest("git is not available")
        doc, _ = sbom.build(ROOT, files, sbom.DEFAULT_VERSION, CREATED)
        bad = [p for p in vm.packages_of(doc) if vm.version_key(p.ecosystem, p.version) is None]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
