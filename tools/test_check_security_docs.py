#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_security_docs.py (unittest, standard library only)."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_security_docs as sd  # noqa: E402

TODAY = date(2026, 11, 27)

CONTACT = (
    "## Reporting\n\n"
    "Report vulnerabilities privately through "
    "https://github.com/acme/os/security/advisories/new or write to "
    "security@acme-os.test.\n"
)
POLICY = (
    "## Disclosure policy\n\n"
    "We follow coordinated vulnerability disclosure: we answer within 7 days.\n"
)
SUPPORT = "## Support\n\nThe security support period ends 2031-12.\n"
FIXES = (
    "## Fixes\n\nSecurity updates are released through the update channel and are free of charge.\n"
)
RETENTION = "## Retention\n\nEach security update stays available for 10 years.\n"
ADVISORIES = (
    "## Advisories\n\n"
    "After a fix is released we publish a security advisory with a CVE identifier.\n"
)
INTEGRITY = "## Integrity\n\nUpdates come from a signed repository.\n"
SBOM = "## SBOM\n\nThe SBOM is described in [the SBOM page](docs/sbom.md).\n"
EXPLOITED = (
    "## Exploited vulnerabilities\n\n"
    "An actively exploited vulnerability is reported to the CSIRT and to ENISA within 24 hours.\n"
)
SECTIONS = {
    "contact": CONTACT,
    "cvd-policy": POLICY,
    "support-period": SUPPORT,
    "fix-process": FIXES,
    "update-retention": RETENTION,
    "advisories": ADVISORIES,
    "update-integrity": INTEGRITY,
    "sbom": SBOM,
    "exploited-reporting": EXPLOITED,
}
EXTRA_FILES = {"docs/sbom.md": "# SBOM\n"}


def security_text(skip: tuple[str, ...] = ()) -> str:
    parts = [text for key, text in SECTIONS.items() if key not in skip]
    return "# Security\n\n" + "\n".join(parts)


def tree(security: str | bytes | None = None, **docs: str | bytes) -> dict[str, str | bytes]:
    files: dict[str, str | bytes] = dict(EXTRA_FILES)
    files[sd.SECURITY] = security_text() if security is None else security
    for name, text in docs.items():
        files["docs/" + name.replace("__", "/")] = text
    return files


def run(files: dict[str, str | bytes], today: date = TODAY) -> sd.Report:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, data in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return sd.check_files(root, list(files), today)


def errors(files: dict[str, str | bytes], today: date = TODAY) -> list[str]:
    return [str(p) for p in run(files, today).problems if not p.warning]


def warnings(files: dict[str, str | bytes], today: date = TODAY) -> list[str]:
    return [str(p) for p in run(files, today).problems if p.warning]


def with_support(sentence: str) -> dict[str, str | bytes]:
    text = security_text(skip=("support-period",)) + f"\n## Support\n\n{sentence}\n"
    return tree(text)


class CompletePolicy(unittest.TestCase):
    def test_a_complete_policy_has_no_problems(self) -> None:
        report = run(tree())
        self.assertEqual([str(p) for p in report.problems], [])

    def test_every_statement_is_answered_in_security_md(self) -> None:
        report = run(tree())
        for key in sd.REQUIREMENT_KEYS - sd.FAULT_WHEN_FOUND:
            self.assertEqual(report.answered[key][0], sd.SECURITY, key)

    def test_requirement_keys_are_unique_and_have_a_reference(self) -> None:
        keys = [req.key for req in sd.REQUIREMENTS]
        self.assertEqual(len(keys), len(set(keys)))
        for req in sd.REQUIREMENTS:
            self.assertTrue(req.ref and req.summary, req.key)

    def test_an_answer_in_docs_counts_too(self) -> None:
        files = tree(
            security_text(skip=("sbom",)),
            **{"supply-chain.md": SBOM.replace("docs/sbom.md", "sbom.md")},
        )
        self.assertEqual(warnings(files), [])
        self.assertEqual(errors(files), [])


class MissingStatements(unittest.TestCase):
    def test_each_missing_statement_is_a_warning_that_cites_the_cra(self) -> None:
        for key in SECTIONS:
            with self.subTest(key=key):
                report = run(tree(security_text(skip=(key,))))
                found = [str(p) for p in report.problems]
                self.assertEqual(len([p for p in report.problems if not p.warning]), 0, found)
                requirement = next(r for r in sd.REQUIREMENTS if r.key == key)
                self.assertTrue(
                    any(p.warning and requirement.ref in p.message for p in report.problems),
                    found,
                )
                self.assertNotIn(key, report.answered)

    def test_a_missing_statement_is_never_an_error(self) -> None:
        self.assertEqual(errors(tree("# Security\n\nWrite to us.\n")), [])

    def test_free_of_charge_needs_updates_and_security_in_the_same_block(self) -> None:
        text = security_text(skip=("fix-process",)) + (
            "\nOur apps are free of charge.\n\nSecurity updates are released monthly.\n"
        )
        found = warnings(tree(text))
        self.assertTrue(any("free of charge" in item for item in found), found)

    def test_statements_are_not_joined_across_blocks(self) -> None:
        text = security_text(skip=("support-period",)) + (
            "\nThe security support period is long.\n\nBefore, 2031-12 was the date.\n"
        )
        found = warnings(tree(text))
        self.assertTrue(any("end date of the support period" in item for item in found), found)

    def test_disclosure_with_a_time_frame_is_a_policy(self) -> None:
        text = security_text(skip=("cvd-policy",)) + (
            "\n## Timeline\n\nWe disclose a fixed vulnerability after 90 days.\n"
        )
        self.assertEqual(warnings(tree(text)), [])

    def test_disclosure_without_a_time_frame_is_not_a_policy(self) -> None:
        text = security_text(skip=("cvd-policy",)) + (
            "\n## Disclosure\n\nWe disclose every vulnerability.\n"
        )
        found = warnings(tree(text))
        self.assertTrue(any("disclosure policy" in item for item in found), found)

    def test_reporting_alone_is_not_publishing_advisories(self) -> None:
        # A link to the advisory form is a channel for reports, not a publication.
        found = warnings(tree(security_text(skip=("advisories",))))
        self.assertTrue(any("are published" in item for item in found), found)

    def test_missing_security_md_is_a_warning(self) -> None:
        files = {"docs/README.md": "# Docs\n"}
        found = warnings(files)
        self.assertTrue(any("no SECURITY.md is tracked" in item for item in found), found)
        self.assertEqual(errors(files), [])

    def test_ukrainian_and_spanish_texts_are_understood(self) -> None:
        uk = (
            "## Звіти\n\nПро вразливості повідомляйте приватно: security@acme-os.test.\n\n"
            "## Політика\n\nМи дотримуємося скоординованого розкриття вразливостей.\n\n"
            "## Підтримка\n\nСтрок підтримки безпеки завершується у грудні 2031.\n"
        )
        es = (
            "## Soporte\n\nEl periodo de soporte termina en diciembre de 2031.\n\n"
            "## Divulgación\n\nPolítica de divulgación coordinada de vulnerabilidades.\n"
        )
        for text in (uk, es):
            report = run(tree("# Security\n\n" + text))
            self.assertIn("support-period", report.answered, text)
            self.assertIn("cvd-policy", report.answered, text)


class SupportPeriod(unittest.TestCase):
    def answered(self, sentence: str) -> bool:
        return "support-period" in run(with_support(sentence)).answered

    def test_month_and_year_in_several_formats(self) -> None:
        for sentence in (
            "Security support period: until 2031-12.",
            "Security support period: until 12/2031.",
            "Security support period: until 12.2031.",
            "Security support period: until December 2031.",
            "Security support period: until Dec 2031.",
            "Період підтримки: до грудня 2031 року.",
            "Підтримуємо безпеку до 2031-12.",
            "El periodo de soporte termina en diciembre de 2031.",
            "Soporte hasta septiembre 2032.",
        ):
            with self.subTest(sentence=sentence):
                self.assertTrue(self.answered(sentence))

    def test_year_only_is_not_enough_and_the_warning_says_so(self) -> None:
        found = warnings(with_support("The security support period ends in 2031."))
        self.assertTrue(any("end date of the support period" in item for item in found), found)
        self.assertTrue(any("not month and year" in item for item in found), found)

    def test_a_length_without_a_date_is_not_enough(self) -> None:
        self.assertFalse(self.answered("The security support period is 5 years."))

    def test_an_invalid_month_is_not_a_date(self) -> None:
        self.assertFalse(self.answered("Security support period: until 2031-13."))
        self.assertFalse(self.answered("Security support period: until 13/2031."))

    def test_a_date_without_a_support_word_is_not_enough(self) -> None:
        self.assertFalse(self.answered("The first release is planned for 2031-12."))

    def test_end_date_in_the_past_is_a_warning(self) -> None:
        found = warnings(with_support("Security support period: until 2026-10."))
        self.assertTrue(any("already over" in item for item in found), found)

    def test_end_date_in_the_current_month_or_later_is_fine(self) -> None:
        for sentence in (
            "Security support period: until 2026-11.",
            "Security support period: until 2026-12.",
        ):
            self.assertEqual(warnings(with_support(sentence)), [], sentence)

    def test_the_latest_date_of_the_block_decides(self) -> None:
        sentence = "Supported versions: 1.0 until 2025-01, 2.0 until 2031-12."
        self.assertEqual(warnings(with_support(sentence)), [])

    def test_today_option_changes_the_verdict(self) -> None:
        files = with_support("Security support period: until 2031-12.")
        self.assertEqual(warnings(files, date(2031, 12, 1)), [])
        self.assertTrue(any("already over" in w for w in warnings(files, date(2032, 1, 1))))

    def test_support_shorter_than_five_years_is_a_warning(self) -> None:
        found = warnings(with_support("Security support period: 3 years, until 2029-12."))
        self.assertTrue(any("shorter than five years" in item for item in found), found)
        found = warnings(with_support("Період підтримки: два роки, до 2028-12."))
        self.assertTrue(any("shorter than five years" in item for item in found), found)

    def test_five_years_or_more_is_not_short(self) -> None:
        for sentence in (
            "Security support period: 5 years, until 2031-12.",
            "Security support period: five years, until 2031-12.",
            "Період підтримки: 10 років, до 2036-12.",
        ):
            found = warnings(with_support(sentence))
            self.assertFalse(any("shorter than five years" in item for item in found), found)


class UpdateRetention(unittest.TestCase):
    def answered(self, sentence: str) -> bool:
        text = security_text(skip=("update-retention",)) + f"\n## Retention\n\n{sentence}\n"
        return "update-retention" in run(tree(text)).answered

    def test_ten_years_or_more_is_enough(self) -> None:
        for sentence in (
            "Each security update stays available for 10 years.",
            "Each security update stays available for ten years.",
            "Кожне оновлення безпеки доступне 12 років.",
        ):
            self.assertTrue(self.answered(sentence), sentence)

    def test_less_than_ten_years_is_not_enough(self) -> None:
        for sentence in (
            "Each security update stays available for 5 years.",
            "Each security update stays available for nine years.",
        ):
            self.assertFalse(self.answered(sentence), sentence)

    def test_ten_years_without_a_security_update_is_not_enough(self) -> None:
        self.assertFalse(self.answered("The project exists for 10 years."))


class ContactChannels(unittest.TestCase):
    def only(self, contact: str) -> list[str]:
        return warnings(tree(security_text(skip=("contact",)) + "\n" + contact))

    def test_one_kind_of_channel_is_a_warning(self) -> None:
        text = security_text(skip=("contact",)) + (
            "\n## Reporting\n\nReport vulnerabilities privately through "
            "https://github.com/acme/os/security/advisories/new.\n"
        )
        found = warnings(tree(text))
        self.assertEqual(len(found), 1, found)
        self.assertIn("only one kind of reporting channel (advisory-form)", found[0])

    def test_two_kinds_of_channel_are_fine(self) -> None:
        self.assertEqual(warnings(tree()), [])

    def test_an_email_alone_is_one_kind(self) -> None:
        found = self.only("## Reporting\n\nReport a vulnerability to security@acme-os.test.\n")
        self.assertEqual(len(found), 1, found)
        self.assertIn("(email)", found[0])

    def test_a_public_issue_tracker_is_not_a_private_channel(self) -> None:
        found = self.only(
            "## Reporting\n\nReport a vulnerability at https://github.com/acme/os/issues/new.\n"
        )
        self.assertTrue(any("private way to report" in item for item in found), found)

    def test_a_placeholder_address_is_an_error_and_does_not_count(self) -> None:
        text = security_text(skip=("contact",)) + (
            "\n## Reporting\n\nReport a vulnerability to security@example.com.\n"
        )
        files = tree(text)
        self.assertTrue(any("placeholder" in item for item in errors(files)), errors(files))
        self.assertTrue(any("private way to report" in w for w in warnings(files)))

    def test_todo_and_template_text_are_errors(self) -> None:
        for filler in ("TODO: add the contact", "Write to <your-email>", "TBD", "FIXME later"):
            with self.subTest(filler=filler):
                found = errors(tree(security_text() + f"\n{filler}\n"))
                self.assertTrue(any("placeholder" in item for item in found), found)

    def test_a_placeholder_in_a_code_fence_is_ignored(self) -> None:
        text = security_text() + "\n```\nTODO example.com\n```\n"
        self.assertEqual(errors(tree(text)), [])

    def test_mailto_without_an_address_is_an_error(self) -> None:
        text = security_text() + "\n[write to us](mailto:)\n"
        found = errors(tree(text))
        self.assertTrue(any("mailto link without a valid address" in item for item in found))

    def test_mailto_with_a_subject_is_fine(self) -> None:
        text = security_text() + "\n[mail](mailto:security@acme-os.test?subject=Report)\n"
        self.assertEqual(errors(tree(text)), [])


class Evidence(unittest.TestCase):
    def test_fenced_code_is_not_evidence(self) -> None:
        text = security_text(skip=("sbom",)) + "\n```\nThe SBOM is at [x](docs/sbom.md)\n```\n"
        found = warnings(tree(text))
        self.assertTrue(any("where the SBOM can be found" in item for item in found), found)

    def test_unchecked_task_item_is_not_evidence(self) -> None:
        text = security_text(skip=("sbom",)) + "\n- [ ] The SBOM is at [x](docs/sbom.md)\n"
        found = warnings(tree(text))
        self.assertTrue(any("where the SBOM can be found" in item for item in found), found)

    def test_nested_lines_of_an_unchecked_item_are_not_evidence(self) -> None:
        text = security_text(skip=("sbom",)) + (
            "\n- [ ] Compliance\n  - The SBOM is at [x](docs/sbom.md)\n"
        )
        found = warnings(tree(text))
        self.assertTrue(any("where the SBOM can be found" in item for item in found), found)

    def test_checked_task_item_is_evidence(self) -> None:
        text = security_text(skip=("sbom",)) + "\n- [x] The SBOM is at [x](docs/sbom.md)\n"
        self.assertEqual(warnings(tree(text)), [])

    def test_sbom_without_a_location_is_not_an_answer(self) -> None:
        text = security_text(skip=("sbom",)) + "\n## SBOM\n\nWe keep an SBOM.\n"
        found = warnings(tree(text))
        self.assertTrue(any("where the SBOM can be found" in item for item in found), found)

    def test_roadmap_is_a_plan_not_evidence_but_the_warning_points_to_it(self) -> None:
        roadmap = "# Roadmap\n\nWe will publish the SBOM at https://acme.test/sbom.json.\n"
        files = tree(security_text(skip=("sbom",)), **{"roadmap.md": roadmap})
        found = warnings(files)
        sbom = [item for item in found if "where the SBOM can be found" in item]
        self.assertEqual(len(sbom), 1, found)
        self.assertIn("docs/roadmap.md:3 only plans it", sbom[0])

    def test_design_documents_are_not_read(self) -> None:
        files = tree(security_text(skip=("sbom",)), **{"design__notes.md": SBOM})
        found = warnings(files)
        self.assertTrue(any("where the SBOM can be found" in item for item in found), found)

    def test_files_outside_security_md_and_docs_are_not_read(self) -> None:
        files = tree(security_text(skip=("sbom",)))
        files["README.md"] = SBOM
        found = warnings(files)
        self.assertTrue(any("where the SBOM can be found" in item for item in found), found)

    def test_an_untracked_file_is_not_read(self) -> None:
        files = tree(security_text(skip=("sbom",)), **{"extra.md": SBOM})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, data in files.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(data), encoding="utf-8")
            tracked = [rel for rel in files if rel != "docs/extra.md"]
            report = sd.check_files(root, tracked, TODAY)
        self.assertNotIn("sbom", report.answered)

    def test_backslash_paths_are_normalised(self) -> None:
        files = tree(security_text(skip=("sbom",)), **{"supply-chain.md": SBOM})
        files["docs/supply-chain.md"] = SBOM.replace("docs/sbom.md", "sbom.md")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, data in files.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(data), encoding="utf-8")
            report = sd.check_files(root, [rel.replace("/", "\\") for rel in files], TODAY)
        self.assertEqual(report.problems, [])
        self.assertEqual(report.answered["sbom"][0], "docs/supply-chain.md")


class LinksInAnswers(unittest.TestCase):
    def test_link_to_a_missing_file_in_the_sbom_block_is_an_error(self) -> None:
        text = security_text(skip=("sbom",)) + "\n## SBOM\n\nThe SBOM: [file](docs/nowhere.json).\n"
        found = errors(tree(text))
        self.assertEqual(len(found), 1, found)
        self.assertIn("'docs/nowhere.json'", found[0])
        self.assertIn("answers sbom", found[0])

    def test_link_to_a_tracked_file_anchor_folder_and_url_are_fine(self) -> None:
        for target in ("docs/sbom.md#top", "docs/", "https://acme.test/sbom.json", "#sbom"):
            text = security_text(skip=("sbom",)) + f"\n## SBOM\n\nThe SBOM: [file]({target}).\n"
            self.assertEqual(errors(tree(text)), [], target)

    def test_relative_link_from_a_docs_file_resolves_from_that_folder(self) -> None:
        security = security_text(skip=("sbom",))
        files = tree(security, **{"policy__sbom.md": "## SBOM\n\nThe SBOM: [f](../sbom.md).\n"})
        self.assertEqual(errors(files), [])
        files = tree(security, **{"policy__sbom.md": "## SBOM\n\nThe SBOM: [f](missing.md).\n"})
        self.assertTrue(any("'missing.md'" in item for item in errors(files)))

    def test_link_in_an_unrelated_block_is_not_checked_here(self) -> None:
        text = security_text() + "\n## Elsewhere\n\nSee [nothing](docs/nowhere.md).\n"
        self.assertEqual(errors(tree(text)), [])


class FileProblems(unittest.TestCase):
    def test_security_md_with_bom_is_an_error(self) -> None:
        files = tree(b"\xef\xbb\xbf" + security_text().encode("utf-8"))
        found = errors(files)
        self.assertEqual(len(found), 1, found)
        self.assertIn("byte order mark", found[0])

    def test_security_md_that_is_not_utf8_is_an_error(self) -> None:
        found = errors(tree(b"# Security\n\xff\xfe\n"))
        self.assertTrue(any("not valid UTF-8" in item for item in found), found)

    def test_a_broken_docs_file_is_only_a_warning(self) -> None:
        files = tree(**{"broken.md": b"\xff\xfe"})
        self.assertEqual(errors(files), [])
        self.assertTrue(
            any("docs/broken.md" in item and "UTF-8" in item for item in warnings(files))
        )

    def test_unreadable_file_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = sd.check_files(Path(tmp), [sd.SECURITY], TODAY)
        self.assertTrue(any("cannot read the file" in str(p) for p in report.problems))
        self.assertTrue(any(not p.warning for p in report.problems))

    def test_empty_security_md_is_all_warnings(self) -> None:
        report = run(tree(""))
        self.assertEqual([p for p in report.problems if not p.warning], [])
        keys = len(sd.REQUIREMENT_KEYS - sd.FAULT_WHEN_FOUND)
        self.assertEqual(len([p for p in report.problems if p.warning]), keys)

    def test_the_problem_text_starts_with_the_path(self) -> None:
        for item in warnings(tree("")):
            self.assertTrue(item.startswith("SECURITY.md: warning: "), item)


class Cli(unittest.TestCase):
    def call(self, files: dict[str, str | bytes], argv: list[str]) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, data in files.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(sd, "ROOT", root),
                mock.patch.object(sd, "tracked_files", return_value=list(files)),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = sd.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_clean_run_returns_0(self) -> None:
        code, out, err = self.call(tree(), ["--today", "2026-11-27"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("0 errors, 0 warnings", out)

    def test_warnings_do_not_fail_but_strict_does(self) -> None:
        files = tree("# Security\n")
        code, out, err = self.call(files, [])
        self.assertEqual(code, 0, err)
        self.assertIn("warnings", out)
        code, _out, _err = self.call(files, ["--strict"])
        self.assertEqual(code, 1)

    def test_an_error_returns_1(self) -> None:
        code, out, err = self.call(tree(security_text() + "\nTODO\n"), [])
        self.assertEqual(code, 1)
        self.assertIn("1 errors", out)
        self.assertIn("SECURITY.md: line ", err)

    def test_report_lists_every_statement(self) -> None:
        code, out, _err = self.call(tree(security_text(skip=("sbom",))), ["--report"])
        self.assertEqual(code, 0)
        for req in sd.REQUIREMENTS:
            self.assertIn(f"{req.key} | ", out)
        self.assertIn("sbom | missing | - |", out)
        self.assertIn("contact | answered | SECURITY.md:", out)

    def test_wrong_calls_return_2(self) -> None:
        for argv in (["--nope"], ["file.md"], ["--today"], ["--today", "tomorrow"]):
            code, _out, err = self.call(tree(), argv)
            self.assertEqual(code, 2, argv)
            self.assertTrue(err, argv)

    def test_help_returns_0(self) -> None:
        code, out, _err = self.call(tree(), ["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)

    def test_git_failure_returns_2(self) -> None:
        err = io.StringIO()
        with (
            mock.patch.object(sd, "tracked_files", side_effect=OSError("no git")),
            contextlib.redirect_stderr(err),
        ):
            code = sd.main([])
        self.assertEqual(code, 2)
        self.assertIn("git ls-files", err.getvalue())

    def test_output_is_deterministic(self) -> None:
        files = tree(security_text(skip=("sbom", "contact")))
        first = self.call(files, ["--report", "--today", "2026-11-27"])
        second = self.call(files, ["--report", "--today", "2026-11-27"])
        self.assertEqual(first, second)


class RealRepo(unittest.TestCase):
    def test_current_repo_has_no_errors(self) -> None:
        report = sd.check_files(sd.ROOT, sd.tracked_files(sd.ROOT), TODAY)
        self.assertEqual([str(p) for p in report.problems if not p.warning], [])

    def test_current_repo_warnings_cite_the_cra(self) -> None:
        report = sd.check_files(sd.ROOT, sd.tracked_files(sd.ROOT), TODAY)
        for problem in report.problems:
            self.assertTrue(problem.warning, str(problem))
            self.assertTrue(
                any(token in problem.message for token in ("Art. ", "Annex ")), str(problem)
            )

    def test_security_md_is_tracked_and_has_a_contact(self) -> None:
        report = sd.check_files(sd.ROOT, sd.tracked_files(sd.ROOT), TODAY)
        self.assertEqual(report.answered["contact"][0], sd.SECURITY)


if __name__ == "__main__":
    unittest.main()
