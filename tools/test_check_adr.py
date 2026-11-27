#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_adr.py (the check of the ADR registry).

Run: python3 tools/test_check_adr.py
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_adr as ca  # noqa: E402  (after sys.path.insert above)

ADR = ca.ADR_DIR
README = ca.README


def adr_text(
    number: str,
    title: str,
    status: str = "Прийнято",
    body: str = "Чому треба вирішувати.",
    related: str = "",
) -> str:
    """A well-formed ADR with a Ukrainian heading."""
    related_line = f"- Пов'язано: {related}\n" if related else ""
    return (
        f"# {number}. {title}\n"
        f"\n"
        f"- Стан: {status}\n"
        f"- Дата: 2026-11-01\n"
        f"{related_line}"
        f"\n"
        f"## Контекст\n"
        f"{body}\n"
    )


def readme_text(rows: list[tuple[str, str, str, str]]) -> str:
    """A registry README; each row is (number, filename, title, status)."""
    lines = [
        "# Архітектурні рішення (ADR)",
        "",
        "Тут записуємо рішення.",
        "",
        "| № | Рішення | Стан |",
        "| --- | --- | --- |",
    ]
    for number, name, title, status in rows:
        lines.append(f"| [{number}]({name}) | {title} | {status} |")
    lines.append("")
    lines.append("## Шаблон")
    lines.append("")
    lines.append("```markdown")
    lines.append("# NNNN. Назва")
    lines.append("")
    lines.append("- Стан: Запропоновано | Прийнято | Замінено (NNNN)")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def tree(
    *adrs: tuple[str, str], rows: list[tuple[str, str, str, str]] | None = None
) -> dict[str, str]:
    """A folder of ADR files plus a matching README (unless ``rows`` is given)."""
    files: dict[str, str] = {}
    auto: list[tuple[str, str, str, str]] = []
    for name, text in adrs:
        files[f"{ADR}/{name}"] = text
        number = name.split("-", 1)[0]
        heading = text.splitlines()[0] if text else ""
        title = heading.split(". ", 1)[1] if ". " in heading else name
        status_line = next((line for line in text.splitlines() if line.startswith("- Стан:")), "")
        status = ca.status_word(status_line[len("- Стан:") :]) or "Прийнято"
        auto.append((number, name, title, status))
    files[README] = readme_text(auto if rows is None else rows)
    return files


def check(files: dict[str, str], tracked: list[str] | None = None) -> list[ca.Problem]:
    """Write ``files`` to a temporary folder and check the tracked paths."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, bytes):
                path.write_bytes(text)
            else:
                path.write_bytes(text.encode("utf-8"))
        names = tracked if tracked is not None else list(files)
        return ca.check_files(root, names)


def errors(files: dict[str, str], tracked: list[str] | None = None) -> list[str]:
    return [str(p) for p in check(files, tracked) if not p.warning]


def warnings(files: dict[str, str], tracked: list[str] | None = None) -> list[str]:
    return [str(p) for p in check(files, tracked) if p.warning]


def good_pair() -> dict[str, str]:
    return tree(
        ("0001-litsenzii.md", adr_text("0001", "Ліцензії: GPL-3.0-or-later")),
        ("0002-pershe-seredovyshche.md", adr_text("0002", "Перше робоче середовище: GNOME")),
    )


class GoodRegistry(unittest.TestCase):
    def test_two_accepted_adrs_pass(self) -> None:
        self.assertEqual(check(good_pair()), [])

    def test_proposed_and_proposal_are_both_valid(self) -> None:
        files = tree(
            ("0001-litsenzii.md", adr_text("0001", "Ліцензії", "Прийнято")),
            ("0002-propozytsiia.md", adr_text("0002", "Пропозиція щодо мови", "Пропозиція")),
            (
                "0003-zaproponovano.md",
                adr_text("0003", "Запропоноване рішення про канал", "Запропоновано"),
            ),
        )
        self.assertEqual(check(files), [])

    def test_status_commentary_and_punctuation_are_ignored(self) -> None:
        files = tree(
            (
                "0001-litsenzii.md",
                adr_text("0001", "Ліцензії", "Прийнято (Denys, 2026-11-02); доповнено"),
            ),
            rows=[("0001", "0001-litsenzii.md", "Ліцензії", "прийнято.")],
        )
        self.assertEqual(check(files), [])

    def test_dot_slash_href_and_anchor_are_fine(self) -> None:
        files = tree(
            ("0001-litsenzii.md", adr_text("0001", "Ліцензії")),
            rows=[("0001", "./0001-litsenzii.md#контекст", "Ліцензії", "Прийнято")],
        )
        self.assertEqual(check(files), [])

    def test_link_to_an_existing_adr_passes(self) -> None:
        related = "[ADR 0001](0001-litsenzii.md)"
        files = tree(
            ("0001-litsenzii.md", adr_text("0001", "Ліцензії")),
            (
                "0002-pershe-seredovyshche.md",
                adr_text("0002", "Перше робоче середовище", related=related),
            ),
        )
        self.assertEqual(check(files), [])

    def test_fenced_example_is_not_a_link_or_a_table_row(self) -> None:
        # The template in the README and a fenced name in the ADR must be ignored.
        body = "Приклад:\n\n```md\n[ні](0009-nesnishnii.md)\n```\n"
        files = tree(("0001-litsenzii.md", adr_text("0001", "Ліцензії", body=body)))
        self.assertEqual(check(files), [])


class Filenames(unittest.TestCase):
    def test_uppercase_slug_is_an_error(self) -> None:
        files = {f"{ADR}/0001-Licensing.md": adr_text("0001", "Ліцензії"), README: readme_text([])}
        found = errors(files)
        self.assertTrue(any("slug must be lowercase" in item for item in found), found)

    def test_underscore_in_the_slug_is_an_error(self) -> None:
        files = {f"{ADR}/0001-local_model.md": adr_text("0001", "Модель"), README: readme_text([])}
        found = errors(files)
        self.assertTrue(any("slug must be lowercase" in item for item in found), found)

    def test_leading_trailing_or_doubled_hyphen_is_an_error(self) -> None:
        for name in ("0001-.md", "0001-foo-.md", "0001--foo.md"):
            files = {f"{ADR}/{name}": adr_text("0001", "Назва"), README: readme_text([])}
            found = errors(files)
            self.assertTrue(any("hyphen" in item for item in found), (name, found))

    def test_other_file_in_the_folder_is_an_error(self) -> None:
        files = good_pair()
        files[f"{ADR}/notes.md"] = "# Нотатки\n"
        found = errors(files)
        self.assertTrue(any("filename must be NNNN-slug.md" in item for item in found), found)

    def test_duplicate_numbers_are_an_error(self) -> None:
        files = tree(
            ("0001-litsenzii.md", adr_text("0001", "Ліцензії")),
            ("0001-insha-nazva.md", adr_text("0001", "Інша назва")),
        )
        found = errors(files)
        self.assertEqual(len(found), 2, found)
        self.assertTrue(
            all("number 0001 is used by more than one file" in item for item in found),
            found,
        )


class TitleAndStatus(unittest.TestCase):
    def test_missing_heading_is_an_error(self) -> None:
        text = "- Стан: Прийнято\n\n## Контекст\nЧому.\n"
        files = tree(("0001-litsenzii.md", text))
        found = errors(files)
        self.assertTrue(any("H1 of the form" in item for item in found), found)

    def test_heading_number_must_match_the_filename(self) -> None:
        files = tree(("0001-litsenzii.md", adr_text("0002", "Ліцензії")))
        found = errors(files)
        self.assertTrue(
            any("heading number 0002 does not match the filename" in item for item in found),
            found,
        )

    def test_empty_heading_title_is_an_error(self) -> None:
        text = "# 0001.  \n\n- Стан: Прийнято\n"
        files = tree(("0001-litsenzii.md", text))
        found = errors(files)
        self.assertTrue(any("H1 of the form" in item for item in found), found)

    def test_missing_status_field_is_an_error(self) -> None:
        text = "# 0001. Ліцензії\n\n- Дата: 2026-11-01\n"
        files = tree(("0001-litsenzii.md", text))
        found = errors(files)
        self.assertTrue(any("missing the '- Стан:' field" in item for item in found), found)

    def test_empty_status_is_an_error(self) -> None:
        text = "# 0001. Ліцензії\n\n- Стан:  \n"
        files = tree(("0001-litsenzii.md", text))
        found = errors(files)
        self.assertTrue(any("the status is empty" in item for item in found), found)

    def test_unknown_status_is_an_error(self) -> None:
        files = tree(("0001-litsenzii.md", adr_text("0001", "Ліцензії", "Чернетка")))
        found = errors(files)
        self.assertTrue(any("unknown status 'Чернетка'" in item for item in found), found)

    def test_proposal_and_proposed_are_not_the_same_word(self) -> None:
        files = tree(
            ("0001-sajt.md", adr_text("0001", "Сайт на двох хостах", "Пропозиція")),
            rows=[("0001", "0001-sajt.md", "Сайт на двох хостах", "Запропоновано")],
        )
        found = errors(files)
        self.assertEqual(len(found), 1, found)
        self.assertIn(
            "status 'Пропозиція' does not match the table column 'Запропоновано'",
            found[0],
        )

    def test_legacy_status_mismatch_of_0032_is_a_warning(self) -> None:
        files = tree(
            (
                "0032-site-hosts-dns.md",
                adr_text("0032", "Сайт на двох хостах", "Пропозиція"),
            ),
            rows=[
                ("0032", "0032-site-hosts-dns.md", "Сайт на двох хостах", "Запропоновано"),
            ],
        )
        self.assertEqual(errors(files), [])
        found = warnings(files)
        # 0001..0031 are also missing; the status mismatch itself must be a warning.
        self.assertTrue(
            any(
                "0032-site-hosts-dns.md" in item
                and "status 'Пропозиція' does not match the table column 'Запропоновано'"
                in item
                and "warning:" in item
                for item in found
            ),
            found,
        )


class Table(unittest.TestCase):
    def test_file_not_in_the_table_is_an_error(self) -> None:
        files = tree(
            ("0001-litsenzii.md", adr_text("0001", "Ліцензії")),
            ("0002-seredovyshche.md", adr_text("0002", "Середовище")),
            rows=[("0001", "0001-litsenzii.md", "Ліцензії", "Прийнято")],
        )
        found = errors(files)
        self.assertTrue(
            any("0002-seredovyshche.md: not listed in" in item for item in found),
            found,
        )

    def test_table_row_without_a_file_is_an_error(self) -> None:
        files = tree(
            ("0001-litsenzii.md", adr_text("0001", "Ліцензії")),
            rows=[
                ("0001", "0001-litsenzii.md", "Ліцензії", "Прийнято"),
                ("0002", "0002-seredovyshche.md", "Середовище", "Прийнято"),
            ],
        )
        found = errors(files)
        self.assertTrue(
            any("points to missing file 0002-seredovyshche.md" in item for item in found),
            found,
        )

    def test_file_listed_twice_is_an_error(self) -> None:
        files = tree(
            ("0001-litsenzii.md", adr_text("0001", "Ліцензії")),
            rows=[
                ("0001", "0001-litsenzii.md", "Ліцензії", "Прийнято"),
                ("0001", "0001-litsenzii.md", "Ліцензії ще раз", "Прийнято"),
            ],
        )
        found = errors(files)
        self.assertTrue(any("appears more than once in the table" in item for item in found), found)

    def test_table_number_must_match_the_linked_file(self) -> None:
        files = tree(
            ("0001-litsenzii.md", adr_text("0001", "Ліцензії")),
            ("0002-seredovyshche.md", adr_text("0002", "Середовище")),
            rows=[
                ("0001", "0002-seredovyshche.md", "Середовище", "Прийнято"),
                ("0002", "0001-litsenzii.md", "Ліцензії", "Прийнято"),
            ],
        )
        found = errors(files)
        self.assertTrue(
            any(
                "table number 0001 does not match the linked file 0002-seredovyshche.md"
                in item
                for item in found
            ),
            found,
        )

    def test_missing_readme_is_an_error(self) -> None:
        files = {
            f"{ADR}/0001-litsenzii.md": adr_text("0001", "Ліцензії"),
        }
        found = errors(files)
        self.assertTrue(
            any(f"{README}: the registry file is missing" in item for item in found),
            found,
        )

    def test_readme_without_a_table_is_an_error(self) -> None:
        files = {
            f"{ADR}/0001-litsenzii.md": adr_text("0001", "Ліцензії"),
            README: "# Архітектурні рішення\n\nЩе немає таблиці.\n",
        }
        found = errors(files)
        self.assertTrue(any("no registry table found" in item for item in found), found)

    def test_unknown_table_status_is_an_error(self) -> None:
        files = tree(
            ("0001-litsenzii.md", adr_text("0001", "Ліцензії", "Прийнято")),
            rows=[("0001", "0001-litsenzii.md", "Ліцензії", "Чернетка")],
        )
        found = errors(files)
        self.assertTrue(any("unknown status 'Чернетка'" in item for item in found), found)


class Gaps(unittest.TestCase):
    def test_a_missing_number_is_a_warning(self) -> None:
        files = tree(
            ("0001-litsenzii.md", adr_text("0001", "Ліцензії")),
            ("0003-mova.md", adr_text("0003", "Мова реалізації")),
        )
        self.assertEqual(errors(files), [])
        found = warnings(files)
        self.assertEqual(
            found,
            [
                f"{README}: warning: missing ADR number 0002 "
                f"(a number may be in an open pull request)"
            ],
        )

    def test_link_to_a_gap_is_a_warning(self) -> None:
        related = "[ADR 0002](0002-khmarni-modeli.md)"
        files = tree(
            ("0001-litsenzii.md", adr_text("0001", "Ліцензії")),
            (
                "0003-zapis.md",
                adr_text("0003", "Запис дзвінків", related=related),
            ),
        )
        self.assertEqual(errors(files), [])
        found = warnings(files)
        self.assertTrue(
            any(
                "0003-zapis.md" in item
                and "link to 0002-khmarni-modeli.md points at missing ADR 0002" in item
                and "warning:" in item
                for item in found
            ),
            found,
        )


class Superseded(unittest.TestCase):
    def test_superseded_with_an_existing_number_passes(self) -> None:
        files = tree(
            ("0001-stara-litsenziia.md", adr_text("0001", "Стара ліцензія", "Замінено (0002)")),
            ("0002-nova-litsenziia.md", adr_text("0002", "Нова ліцензія", "Прийнято")),
        )
        self.assertEqual(check(files), [])

    def test_superseded_with_a_filename_or_a_link_passes(self) -> None:
        for status in (
            "Замінено (0002-nova-litsenziia.md)",
            "Замінено ([0002](0002-nova-litsenziia.md))",
        ):
            files = tree(
                ("0001-stara.md", adr_text("0001", "Старе рішення", status)),
                ("0002-nova-litsenziia.md", adr_text("0002", "Нове рішення")),
            )
            self.assertEqual(check(files), [], status)

    def test_superseded_without_a_target_is_an_error(self) -> None:
        files = tree(("0001-stara.md", adr_text("0001", "Старе рішення", "Замінено")))
        found = errors(files)
        self.assertTrue(
            any("must name an existing ADR in parentheses" in item for item in found),
            found,
        )

    def test_superseded_pointing_at_a_missing_adr_is_an_error(self) -> None:
        files = tree(("0001-stara.md", adr_text("0001", "Старе рішення", "Замінено (0002)")))
        found = errors(files)
        self.assertTrue(any("points to missing ADR 0002" in item for item in found), found)

    def test_superseded_must_not_point_at_itself(self) -> None:
        files = tree(("0001-stara.md", adr_text("0001", "Старе рішення", "Замінено (0001)")))
        found = errors(files)
        self.assertTrue(any("must not point at this same ADR" in item for item in found), found)

    def test_superseded_filename_must_exist(self) -> None:
        files = tree(
            ("0001-stara.md", adr_text("0001", "Старе", "Замінено (0002-nesnishnii.md)")),
            ("0002-nova.md", adr_text("0002", "Нове")),
        )
        found = errors(files)
        self.assertTrue(
            any("points to missing ADR 0002-nesnishnii.md" in item for item in found),
            found,
        )


class Links(unittest.TestCase):
    def test_link_to_a_missing_file_is_an_error(self) -> None:
        related = "[ADR 0001](0001-nesnishnii.md)"
        files = tree(
            ("0001-litsenzii.md", adr_text("0001", "Ліцензії")),
            ("0002-seredovyshche.md", adr_text("0002", "Середовище", related=related)),
        )
        found = errors(files)
        self.assertTrue(
            any(
                "link to 0001-nesnishnii.md points at a missing ADR file" in item
                for item in found
            ),
            found,
        )

    def test_wrong_slug_for_an_existing_number_is_an_error(self) -> None:
        related = "[ADR 0001](0001-insha-slug.md)"
        files = tree(
            ("0001-litsenzii.md", adr_text("0001", "Ліцензії")),
            ("0002-seredovyshche.md", adr_text("0002", "Середовище", related=related)),
        )
        found = errors(files)
        self.assertTrue(
            any("0001-insha-slug.md" in item and "missing ADR file" in item for item in found),
            found,
        )


class Encoding(unittest.TestCase):
    def test_byte_order_mark_is_one_clear_error(self) -> None:
        # The file is not added to the registry (it cannot be parsed), so the
        # table stays empty and the only error is the BOM itself.
        files = {
            f"{ADR}/0001-litsenzii.md": "\ufeff" + adr_text("0001", "Ліцензії"),
            README: readme_text([]),
        }
        found = errors(files)
        self.assertEqual(
            found,
            [f"{ADR}/0001-litsenzii.md: the file starts with a byte order mark (BOM)"],
        )

    def test_file_that_is_not_utf8_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ADR / "0001-litsenzii.md"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"# 0001. \xff\xfe\n")
            (root / README).write_text(readme_text([]), encoding="utf-8")
            found = [
                str(p)
                for p in ca.check_files(root, [f"{ADR}/0001-litsenzii.md", README])
                if not p.warning
            ]
        self.assertIn(f"{ADR}/0001-litsenzii.md: the file is not valid UTF-8", found)

    def test_message_names_the_path_and_the_line(self) -> None:
        files = tree(("0001-litsenzii.md", adr_text("0002", "Ліцензії")))
        found = errors(files)
        self.assertTrue(found[0].startswith(f"{ADR}/0001-litsenzii.md: line 1: "), found)


class Main(unittest.TestCase):
    def test_help_is_not_an_error(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = ca.main(["-h"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", err.getvalue())

    def test_unknown_flag_is_exit_2(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ca.main(["--nope"]), 2)

    def test_unknown_path_is_exit_2(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = ca.main(["docs/adr/0000-missing.md"])
        self.assertEqual(code, 2)
        self.assertIn("not a tracked file", err.getvalue())

    def test_main_on_a_temp_repo_returns_0_when_only_warnings(self) -> None:
        files = tree(
            ("0001-litsenzii.md", adr_text("0001", "Ліцензії")),
            ("0003-mova.md", adr_text("0003", "Мова реалізації")),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, text in files.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            tracked = list(files)
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(ca, "ROOT", root),
                mock.patch.object(ca, "tracked_files", return_value=tracked),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = ca.main([])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("2 ADR files checked: 0 errors, 1 warnings", out.getvalue())
        self.assertIn("missing ADR number 0002", err.getvalue())

    def test_main_returns_1_when_there_is_an_error(self) -> None:
        files = tree(("0001-litsenzii.md", adr_text("0001", "Ліцензії", "Чернетка")))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, text in files.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(ca, "ROOT", root),
                mock.patch.object(ca, "tracked_files", return_value=list(files)),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = ca.main([])
        self.assertEqual(code, 1)
        self.assertIn("unknown status", err.getvalue())


class RealRepo(unittest.TestCase):
    def test_current_repo_has_no_errors(self) -> None:
        problems = ca.check_files(ca.ROOT, ca.tracked_files(ca.ROOT))
        self.assertEqual([str(p) for p in problems if not p.warning], [])
        messages = [str(p) for p in problems if p.warning]
        self.assertTrue(any("missing ADR number 0020" in item for item in messages), messages)
        self.assertTrue(
            any(
                "0032-site-hosts-dns.md" in item
                and "status 'Пропозиція' does not match the table column 'Запропоновано'"
                in item
                for item in messages
            ),
            messages,
        )
        self.assertTrue(
            any("0020-gloxi-cloud-models.md" in item for item in messages),
            messages,
        )


if __name__ == "__main__":
    unittest.main()
