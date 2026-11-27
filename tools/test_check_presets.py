#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_presets.py (the check of the tracked systemd preset files).

Run: python3 tools/test_check_presets.py
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

import check_presets as cp  # noqa: E402  (after sys.path.insert above)

UNIT = "[Unit]\nDescription=Demo\n[Service]\nExecStart=/usr/bin/demo\n[Install]\n"
UNIT_ON = UNIT + "WantedBy=multi-user.target\n"
SPEC = (
    "Name: demo\nSource0: 50-demo.preset\nSource1: 50-demo-user.preset\n"
    "%files\n%{_unitdir}/demo.service\n%{_unitdir}/tmpl@.service\n%{_unitdir}/other.service\n"
    "%{_userunitdir}/agent.service\n"
)


def base_files() -> dict[str, bytes | str]:
    return {
        "pkg/demo.spec": SPEC,
        "pkg/demo.service": UNIT_ON,
        "pkg/other.service": UNIT_ON,
        "pkg/tmpl@.service": UNIT_ON,
        "pkg/agent.service": UNIT_ON,
    }


def check(
    preset: bytes | str,
    extra: dict[str, bytes | str] | None = None,
    name: str = "pkg/50-demo.preset",
    only: tuple[str, ...] = (),
) -> list[cp.Problem]:
    """Write a small tree (the preset, a spec, some units) and check it."""
    files = base_files()
    files[name] = preset
    files.update(extra or {})
    with tempfile.TemporaryDirectory() as tmp:
        for rel, content in files.items():
            path = Path(tmp) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return cp.check_files(Path(tmp), sorted(files), only)


def errors(
    preset: bytes | str, extra: dict[str, bytes | str] | None = None, **kw: object
) -> list[str]:
    return [str(p) for p in check(preset, extra, **kw) if not p.warning]  # type: ignore[arg-type]


def warnings(
    preset: bytes | str, extra: dict[str, bytes | str] | None = None, **kw: object
) -> list[str]:
    return [str(p) for p in check(preset, extra, **kw) if p.warning]  # type: ignore[arg-type]


class GoodPresets(unittest.TestCase):
    def test_good_preset_passes(self) -> None:
        self.assertEqual(check("# comment\nenable demo.service\ndisable other.service\n"), [])

    def test_comments_empty_lines_and_semicolon(self) -> None:
        self.assertEqual(check("; one\n\n   # two\n  enable   demo.service  \n"), [])

    def test_tabs_between_the_words(self) -> None:
        self.assertEqual(check("enable\tdemo.service\n"), [])

    def test_empty_file_is_fine(self) -> None:
        self.assertEqual(check(""), [])

    def test_disable_glob_at_the_end(self) -> None:
        self.assertEqual(check("enable demo.service\ndisable *\n"), [])

    def test_template_with_instance(self) -> None:
        self.assertEqual(check("enable tmpl@one.service\n"), [])
        self.assertEqual(check("enable tmpl@.service one two\n"), [])

    def test_template_with_default_instance(self) -> None:
        unit = UNIT + "WantedBy=multi-user.target\nDefaultInstance=one\n"
        self.assertEqual(check("enable tmpl@.service\n", {"pkg/tmpl@.service": unit}), [])

    def test_template_documented_in_a_comment(self) -> None:
        text = "# tmpl@.service gets its instances from a helper\nenable tmpl@.service\n"
        self.assertEqual(check(text), [])

    def test_disable_of_a_template_needs_nothing(self) -> None:
        self.assertEqual(check("disable tmpl@.service\n"), [])

    def test_user_preset_with_its_unit(self) -> None:
        self.assertEqual(check("enable agent.service\n", name="pkg/50-demo-user.preset"), [])

    def test_crlf_free_file_without_final_newline(self) -> None:
        self.assertEqual(check("enable demo.service"), [])


class Syntax(unittest.TestCase):
    def test_unknown_directive(self) -> None:
        found = errors("start demo.service\n")
        self.assertEqual(len(found), 1)
        self.assertIn("unknown directive 'start'", found[0])

    def test_directive_is_case_sensitive(self) -> None:
        self.assertEqual(len(errors("Enable demo.service\n")), 1)

    def test_directive_without_unit(self) -> None:
        self.assertIn("without a unit", errors("enable\n")[0])

    def test_trailing_comment_is_an_error(self) -> None:
        found = errors("enable demo.service # why\n")
        self.assertEqual(len(found), 1)
        self.assertIn("comment after the unit", found[0])

    def test_disable_with_instances(self) -> None:
        self.assertIn("takes only a unit", errors("disable demo.service extra\n")[0])

    def test_instances_for_a_plain_unit(self) -> None:
        self.assertIn("not a template", errors("enable demo.service one\n")[0])

    def test_bad_instance_name(self) -> None:
        self.assertIn("invalid instance", errors("enable tmpl@.service a/b\n")[0])

    def test_no_suffix(self) -> None:
        self.assertIn("no known suffix", errors("enable demo\n")[0])

    def test_unknown_suffix(self) -> None:
        self.assertIn("no known suffix", errors("enable demo.conf\n")[0])

    def test_slash_in_the_name(self) -> None:
        self.assertIn("no slash", errors("enable ../demo.service\n")[0])

    def test_empty_unit_name(self) -> None:
        self.assertIn("empty", errors("enable .service\n")[0])
        self.assertIn("empty", errors("enable @.service\n")[0])

    def test_two_at_signs(self) -> None:
        self.assertIn("more than one @", errors("enable a@b@c.service\n")[0])

    def test_byte_order_mark(self) -> None:
        found = errors(b"\xef\xbb\xbfenable demo.service\n")
        self.assertEqual(
            found, ["pkg/50-demo.preset: line 1: the file starts with a byte order mark"]
        )

    def test_carriage_return(self) -> None:
        found = errors("enable demo.service\r\n")
        self.assertEqual(len(found), 1)
        self.assertIn("carriage return", found[0])

    def test_nul_and_control_characters(self) -> None:
        self.assertIn("control character", errors(b"enable demo\x00.service\n")[0])
        self.assertIn("control character", errors(b"enable demo.service\x1b\n")[0])

    def test_not_utf8(self) -> None:
        self.assertEqual(
            errors(b"enable demo.service \xff\xfe\n"),
            ["pkg/50-demo.preset: the file is not valid UTF-8"],
        )

    def test_message_names_the_path_and_the_line(self) -> None:
        found = errors("enable demo.service\nbogus x\n")
        self.assertTrue(found[0].startswith("pkg/50-demo.preset: line 2: "), found)

    def test_invalid_glob_characters(self) -> None:
        self.assertIn("invalid characters", errors("disable a$*.service\n")[0])

    def test_unicode_unit_name(self) -> None:
        self.assertIn("invalid characters", errors("enable dem\u043e.service\n")[0])

    def test_very_long_line_is_one_problem(self) -> None:
        found = errors("enable " + "a" * 100_000 + ".service\n")
        self.assertEqual(len(found), 1)


class Wildcards(unittest.TestCase):
    def test_enable_star(self) -> None:
        found = errors("enable *\n")
        self.assertEqual(len(found), 1)
        self.assertIn("'enable *' switches on units nobody checked", found[0])

    def test_enable_other_globs(self) -> None:
        self.assertEqual(len(errors("enable demo-*.service\n")), 1)
        self.assertEqual(len(errors("enable ?emo.service\n")), 1)
        self.assertEqual(len(errors("enable [d]emo.service\n")), 1)

    def test_enable_star_after_disable_star(self) -> None:
        found = errors("disable *\nenable *\n")
        self.assertTrue(any("switches on units nobody checked" in f for f in found), found)

    def test_disable_star_is_fine(self) -> None:
        self.assertEqual(check("disable *\n"), [])

    def test_disable_pattern_is_not_resolved(self) -> None:
        self.assertEqual(check("disable nothing-*.service\n"), [])

    def test_enable_star_is_an_error_in_a_user_preset(self) -> None:
        self.assertEqual(len(errors("enable *\n", name="pkg/50-demo-user.preset")), 1)


class Units(unittest.TestCase):
    def test_unknown_unit(self) -> None:
        found = errors("enable ghost.service\n")
        self.assertEqual(len(found), 1)
        self.assertIn(
            "'ghost.service' is neither a unit of the repo nor a known external", found[0]
        )

    def test_unknown_unit_for_disable_too(self) -> None:
        self.assertEqual(len(errors("disable ghost.service\n")), 1)

    def test_unknown_template_instance(self) -> None:
        self.assertEqual(len(errors("enable ghost@one.service\n")), 1)

    def test_typo_in_the_suffix_is_not_the_repo_unit(self) -> None:
        self.assertEqual(len(errors("enable demo.socket\n")), 1)

    def test_case_matters(self) -> None:
        self.assertEqual(len(errors("enable Demo.service\n")), 1)

    def test_external_unit_is_a_warning_with_its_package(self) -> None:
        text = "disable sshd.service\n"
        self.assertEqual(errors(text), [])
        found = warnings(text)
        self.assertEqual(len(found), 1)
        self.assertIn("external unit 'sshd.service' comes from package 'openssh-server'", found[0])

    def test_external_package_named_in_the_image_lists(self) -> None:
        kiwi = '<image><packages type="image"><package name="avahi"/></packages></image>'
        found = warnings("disable avahi-daemon.service\n", {"img/x.kiwi": kiwi})
        self.assertIn("(named in the image lists)", found[0])
        self.assertIn("not named in the image lists", warnings("disable avahi-daemon.service\n")[0])

    def test_package_from_the_pattern_requires(self) -> None:
        spec = "Name: patterns-vilna\n%package base\nRequires: avahi\n"
        files = {cp.PATTERNS_SPEC: spec}
        self.assertIn(
            "(named in the image lists)", warnings("disable avahi-daemon.service\n", files)[0]
        )

    def test_package_from_packages_yaml(self) -> None:
        yaml = "vilna:\n  packages:\n    - {name: avahi, license: x, status: upstream}\n"
        found = warnings("disable avahi-daemon.service\n", {cp.PACKAGES_YAML: yaml})
        self.assertIn("(named in the image lists)", found[0])

    def test_package_named_only_in_a_kiwi_comment_does_not_count(self) -> None:
        kiwi = '<image><!-- <package name="avahi"/> --></image>'
        found = warnings("disable avahi-daemon.service\n", {"img/x.kiwi": kiwi})
        self.assertIn("not named in the image lists", found[0])

    def test_external_instance_of_an_external_template(self) -> None:
        self.assertEqual(errors("disable getty@tty1.service\n"), [])

    def test_repo_unit_wins_over_the_external_table(self) -> None:
        unit = "pkg/sshd.service"
        found = warnings(
            "disable sshd.service\n",
            {unit: UNIT_ON, "pkg/demo.spec": SPEC + "%{_unitdir}/sshd.service\n"},
        )
        self.assertEqual(found, [])

    def test_a_unit_of_the_repo_in_another_folder(self) -> None:
        extra = {
            "elsewhere/far.service": UNIT_ON,
            "elsewhere/far.spec": "Name: far\n%{_unitdir}/far.service\n",
        }
        self.assertEqual(check("enable far.service\n", extra), [])

    def test_unit_without_install_cannot_be_enabled(self) -> None:
        found = errors("enable demo.service\n", extra={"pkg/demo.service": UNIT})
        self.assertEqual(len(found), 1)
        self.assertIn("has no [Install] entry", found[0])

    def test_disable_of_a_unit_without_install_is_fine(self) -> None:
        self.assertEqual(check("disable demo.service\n", {"pkg/demo.service": UNIT}), [])

    def test_empty_install_value_does_not_count(self) -> None:
        unit = UNIT + "WantedBy=\n"
        self.assertEqual(len(errors("enable demo.service\n", {"pkg/demo.service": unit})), 1)

    def test_install_key_in_another_group_does_not_count(self) -> None:
        unit = "[Unit]\nDescription=D\nWantedBy=multi-user.target\n"
        unit += "[Service]\nExecStart=/x\n[Install]\n"
        self.assertEqual(len(errors("enable demo.service\n", {"pkg/demo.service": unit})), 1)

    def test_alias_counts_as_an_install_entry(self) -> None:
        unit = UNIT + "Alias=other-name.service\n"
        self.assertEqual(check("enable demo.service\n", {"pkg/demo.service": unit}), [])

    def test_template_without_instance_and_without_explanation(self) -> None:
        found = errors("enable tmpl@.service\n")
        self.assertEqual(len(found), 1)
        self.assertIn("template 'tmpl@.service' is enabled with no instance", found[0])

    def test_comment_for_another_template_does_not_document(self) -> None:
        text = "# tmpl2@.service is explained here\nenable tmpl@.service\n"
        self.assertEqual(len(errors(text)), 1)

    def test_comment_in_another_file_does_not_document(self) -> None:
        extra = {
            "pkg/60-other.preset": "# tmpl@.service is explained here\n",
            "pkg/demo.spec": SPEC + "Source2: 60-other.preset\n",
        }
        self.assertEqual(len(errors("enable tmpl@.service\n", extra)), 1)

    def test_unit_in_no_spec(self) -> None:
        found = errors(
            "enable demo.service\n", {"pkg/demo.spec": SPEC.replace("demo.service", "x")}
        )
        self.assertTrue(any("is in no spec" in f for f in found), found)

    def test_spec_mention_of_a_drop_in_directory_does_not_count(self) -> None:
        spec = "Name: demo\nSource0: 50-demo.preset\n%{_unitdir}/demo.service.d/50.conf\n"
        found = errors("enable demo.service\n", {"pkg/demo.spec": spec})
        self.assertTrue(any("is in no spec" in f for f in found), found)

    def test_spec_mention_of_a_longer_name_does_not_count(self) -> None:
        spec = "Name: demo\nSource0: 50-demo.preset\n%{_unitdir}/my-demo.service\n"
        found = errors("enable demo.service\n", {"pkg/demo.spec": spec})
        self.assertTrue(any("is in no spec" in f for f in found), found)

    def test_user_unit_in_a_system_preset(self) -> None:
        found = errors("enable agent.service\n")
        self.assertEqual(len(found), 1)
        self.assertIn(
            "system preset line, but the specs install the unit only as a user unit", found[0]
        )

    def test_system_unit_in_a_user_preset(self) -> None:
        found = errors("enable demo.service\n", name="pkg/50-demo-user.preset")
        self.assertEqual(len(found), 1)
        self.assertIn("user preset line, but the specs install the unit only as a system", found[0])

    def test_scope_follows_a_continued_install_line(self) -> None:
        spec = SPEC + "install -D x \\\n  %{buildroot}%{_userunitdir}/cont.service\n"
        extra = {"pkg/demo.spec": spec, "pkg/cont.service": UNIT_ON}
        self.assertEqual(check("enable cont.service\n", extra, name="pkg/50-demo-user.preset"), [])
        self.assertEqual(len(errors("enable cont.service\n", extra)), 1)


class Order(unittest.TestCase):
    def test_duplicate_in_one_file(self) -> None:
        found = errors("enable demo.service\nenable demo.service\n")
        self.assertEqual(len(found), 1)
        self.assertIn("line 2: 'enable demo.service' repeats pkg/50-demo.preset:1", found[0])

    def test_contradiction_in_one_file(self) -> None:
        found = errors("enable demo.service\ndisable demo.service\n")
        self.assertEqual(len(found), 1)
        self.assertIn("contradicts pkg/50-demo.preset:1 (the first one wins)", found[0])

    def test_rule_names(self) -> None:
        rules = [p.rule for p in check("enable demo.service\nenable demo.service\n")]
        self.assertEqual(rules, ["duplicate"])
        rules = [p.rule for p in check("enable demo.service\ndisable demo.service\n")]
        self.assertEqual(rules, ["conflict"])
        rules = [p.rule for p in check("disable *\nenable demo.service\n")]
        self.assertEqual(rules, ["shadowed"])

    def test_line_after_disable_star_is_dead(self) -> None:
        found = errors("disable *\nenable demo.service\n")
        self.assertEqual(len(found), 1)
        self.assertIn("covered by 'disable *'", found[0])

    def test_line_after_a_narrower_glob(self) -> None:
        found = errors("disable de*.service\nenable demo.service\n")
        self.assertIn("covered by 'disable de*.service'", found[0])

    def test_glob_that_does_not_match_covers_nothing(self) -> None:
        self.assertEqual(check("disable x*.service\nenable demo.service\n"), [])

    def test_duplicate_across_files_goes_by_the_file_name(self) -> None:
        extra = {"pkg/90-late.preset": "enable demo.service\n"}
        found = errors(
            "enable demo.service\n",
            extra={**extra, "pkg/demo.spec": SPEC + "Source2: 90-late.preset\n"},
        )
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("pkg/90-late.preset: line 1"), found)

    def test_file_name_order_ignores_the_folder(self) -> None:
        spec2 = "Name: z\nSource0: 10-first.preset\n"
        extra = {"zzz/10-first.preset": "disable demo.service\n", "zzz/z.spec": spec2}
        found = errors("enable demo.service\n", extra=extra)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("pkg/50-demo.preset: line 1"), found)
        self.assertIn("contradicts zzz/10-first.preset:1", found[0])

    def test_system_and_user_lists_are_separate(self) -> None:
        extra = {"pkg/50-demo-user.preset": "disable sshd.service\n"}
        self.assertEqual(errors("enable sshd.service\n", extra), [])

    def test_instance_lines_are_not_duplicates(self) -> None:
        self.assertEqual(check("enable tmpl@.service one\nenable tmpl@.service two\n"), [])

    def test_legacy_duplicate_is_a_warning(self) -> None:
        path = "pkg/50-demo.preset"
        legacy = frozenset({(path, "duplicate", "demo.service")})
        with mock.patch.object(cp, "LEGACY", legacy):
            problems = check("enable demo.service\nenable demo.service\n")
        self.assertEqual([p.warning for p in problems], [True])

    def test_legacy_does_not_hide_another_unit(self) -> None:
        legacy = frozenset({("pkg/50-demo.preset", "duplicate", "demo.service")})
        with mock.patch.object(cp, "LEGACY", legacy):
            problems = check("enable other.service\nenable other.service\n")
        self.assertEqual([p.warning for p in problems], [False])


class Packaging(unittest.TestCase):
    def test_preset_not_listed_as_a_source(self) -> None:
        found = errors(
            "enable demo.service\n", {"pkg/demo.spec": SPEC.replace("50-demo.preset", "x")}
        )
        self.assertEqual(len(found), 1)
        self.assertIn("no package ships it", found[0])

    def test_spec_in_another_folder_does_not_count(self) -> None:
        extra = {
            "pkg/demo.spec": "Name: demo\n",
            "other/o.spec": "Name: o\nSource0: 50-demo.preset\n",
        }
        self.assertTrue(any("no package ships" in e for e in errors("", extra=extra)))

    def test_source_with_a_longer_name_does_not_count(self) -> None:
        spec = SPEC.replace("50-demo.preset", "x-50-demo.preset")
        self.assertEqual(len(errors("", {"pkg/demo.spec": spec})), 1)


class ImageLists(unittest.TestCase):
    YAML = (
        "base:\n  disable_by_default: [sshd.service, smb.service]  # note\n"
        "  enable_services: [demo.service]\n"
    )

    def run_lists(self, preset: str, yaml: str | None = None) -> list[str]:
        return errors(preset, extra={cp.PACKAGES_YAML: yaml or self.YAML})

    def test_agrees(self) -> None:
        self.assertEqual(
            self.run_lists("disable sshd.service\ndisable smb.service\nenable demo.service\n"), []
        )

    def test_unit_that_must_be_off_is_on(self) -> None:
        found = self.run_lists("enable sshd.service\ndisable smb.service\nenable demo.service\n")
        self.assertEqual(len(found), 1)
        self.assertIn("disable_by_default lists 'sshd.service'", found[0])

    def test_unit_that_must_be_off_has_no_line(self) -> None:
        found = self.run_lists("disable smb.service\nenable demo.service\n")
        self.assertIn("no preset line", found[0])

    def test_unit_that_must_be_on_is_off(self) -> None:
        found = self.run_lists("disable sshd.service\ndisable smb.service\ndisable demo.service\n")
        self.assertIn("enable_services lists 'demo.service'", found[0])

    def test_a_glob_decides_for_the_units_it_matches(self) -> None:
        self.assertEqual(self.run_lists("enable demo.service\ndisable *\n"), [])

    def test_a_glob_that_comes_first_wins_over_the_enable(self) -> None:
        found = [e for e in self.run_lists("disable *\n") if "enable_services" in e]
        self.assertEqual(len(found), 1)
        self.assertIn("'disable' at pkg/50-demo.preset:1", found[0])

    def test_missing_keys_are_fine(self) -> None:
        self.assertEqual(self.run_lists("enable demo.service\n", "base:\n  other: [a]\n"), [])


class Reporting(unittest.TestCase):
    def test_only_limits_the_report(self) -> None:
        extra = {"pkg/50-demo-user.preset": "enable ghost.service\n"}
        self.assertEqual(
            errors("enable demo.service\n", extra=extra, only=("pkg/50-demo.preset",)), []
        )
        self.assertEqual(
            len(errors("enable demo.service\n", extra=extra, only=("pkg/50-demo-user.preset",))), 1
        )

    def test_warning_text(self) -> None:
        text = str(cp.Problem("a.preset", "line 1: x", warning=True))
        self.assertEqual(text, "a.preset: warning: line 1: x")

    def test_sorted_by_line_number_not_as_text(self) -> None:
        text = "".join(f"disable ghost{i}.service\n" for i in range(12))
        lines = [int(e.split("line ")[1].split(":")[0]) for e in errors(text)]
        self.assertEqual(lines, list(range(1, 13)))

    def test_same_preset_twice_is_deterministic(self) -> None:
        text = "enable ghost.service\nenable demo.service\nenable demo.service\n"
        self.assertEqual(errors(text), errors(text))


class TableAndHelpers(unittest.TestCase):
    def test_template_of(self) -> None:
        self.assertEqual(cp.template_of("a@b.service"), "a@.service")
        self.assertEqual(cp.template_of("a@.service"), "")
        self.assertEqual(cp.template_of("a.service"), "")

    def test_yaml_list(self) -> None:
        self.assertEqual(
            cp.yaml_list("x:\n  key: ['a.service', b.socket]\n", "key"), ["a.service", "b.socket"]
        )
        self.assertEqual(cp.yaml_list("x: 1\n", "key"), [])

    def test_table_on_the_repo(self) -> None:
        tracked = cp.tracked_files(ROOT)
        table = cp.presets_table(ROOT, tracked)
        self.assertIn("| Preset | Line | Unit | From | Final |", table)
        self.assertIn("`vilna-netguard.service`", table)
        self.assertIn("package avahi | disable", table)

    def test_table_marks_a_line_that_an_earlier_one_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a").mkdir()
            (Path(tmp) / "a/10-x.preset").write_text("disable *\n")
            (Path(tmp) / "a/20-y.preset").write_text("enable sshd.service\n")
            table = cp.presets_table(Path(tmp), ["a/10-x.preset", "a/20-y.preset"])
        self.assertIn("disable (earlier line)", table)


class OnTheRepo(unittest.TestCase):
    def test_the_repo_has_no_errors(self) -> None:
        tracked = cp.tracked_files(ROOT)
        problems = cp.check_files(ROOT, tracked)
        self.assertEqual([str(p) for p in problems if not p.warning], [])

    def test_the_repo_has_presets(self) -> None:
        self.assertGreaterEqual(
            len([t for t in cp.tracked_files(ROOT) if t.endswith(".preset")]), 5
        )

    def test_every_legacy_line_is_still_needed(self) -> None:
        tracked = cp.tracked_files(ROOT)
        with mock.patch.object(cp, "LEGACY", frozenset()):
            problems = cp.check_files(ROOT, tracked)
        needed = {(p.path, p.rule) for p in problems if not p.warning}
        for path, rule, _unit in cp.LEGACY:
            self.assertIn((path, rule), needed)

    def test_every_external_unit_in_the_table_is_used(self) -> None:
        used: set[str] = set()
        for rel in cp.tracked_files(ROOT):
            if rel.endswith(".preset"):
                preset, _ = cp.parse_preset(rel, (ROOT / rel).read_bytes())
                used.update(line.pattern for line in preset.lines)
        self.assertEqual(sorted(set(cp.EXTERNAL) - used), [])

    def test_external_table_has_no_unit_of_the_repo(self) -> None:
        tracked = cp.tracked_files(ROOT)
        names = {Path(t).name for t in tracked if t.endswith(cp.REPO_UNIT_SUFFIXES)}
        self.assertEqual(sorted(set(cp.EXTERNAL) & names), [])

    def test_the_tool_does_not_change_the_tree(self) -> None:
        before = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
        cp.check_files(ROOT, cp.tracked_files(ROOT))
        after = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
        self.assertEqual(before, after)


class CommandLine(unittest.TestCase):
    def run_main(self, args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cp.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_default_run_passes(self) -> None:
        code, out, _ = self.run_main([])
        self.assertEqual(code, 0)
        self.assertIn("preset files checked: 0 errors,", out)

    def test_unknown_option(self) -> None:
        self.assertEqual(self.run_main(["--nope"])[0], 2)

    def test_help(self) -> None:
        code, _, err = self.run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", err)

    def test_path_that_is_not_a_preset(self) -> None:
        code, _, err = self.run_main(["README.md"])
        self.assertEqual(code, 2)
        self.assertIn("not a tracked preset file", err)

    def test_one_preset(self) -> None:
        code, out, _ = self.run_main(["obs/packages/vilna-presets/50-vilna.preset"])
        self.assertEqual(code, 0)
        self.assertIn("1 preset files checked", out)

    def test_backslash_path(self) -> None:
        code, _, _ = self.run_main(["obs\\packages\\vilna-presets\\50-vilna.preset"])
        self.assertEqual(code, 0)

    def test_table_goes_to_stdout(self) -> None:
        code, out, _ = self.run_main(["--table"])
        self.assertEqual(code, 0)
        self.assertIn("| Preset | Line |", out)

    def test_git_failure_is_exit_2(self) -> None:
        with mock.patch.object(cp, "tracked_files", side_effect=OSError("no git")):
            self.assertEqual(self.run_main([])[0], 2)

    def test_error_gives_exit_1(self) -> None:
        bad = [cp.Problem("a.preset", "line 1: broken")]
        with mock.patch.object(cp, "check_files", return_value=bad):
            code, out, err = self.run_main([])
        self.assertEqual(code, 1)
        self.assertIn("a.preset: line 1: broken", err)
        self.assertIn("1 errors", out)

    def test_only_git_ls_files_is_run(self) -> None:
        real = subprocess.run
        calls: list[list[str]] = []

        def spy(cmd, *a, **kw):  # type: ignore[no-untyped-def]
            calls.append(list(cmd))
            return real(cmd, *a, **kw)

        with mock.patch.object(subprocess, "run", spy):
            self.run_main([])
        self.assertTrue(calls)
        self.assertTrue(all(c[:2] == ["git", "ls-files"] for c in calls), calls)


if __name__ == "__main__":
    unittest.main()
