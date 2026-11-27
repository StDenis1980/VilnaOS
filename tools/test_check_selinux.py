#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_selinux.py (the check of the SELinux policy sources).

Every test writes small files into a temporary folder and checks them the way ``main()``
does; there is a test for every rule and one for the good files.

Run: python3 tools/test_check_selinux.py
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_selinux as cs  # noqa: E402  (after sys.path.insert above)

SPDX = "# SPDX-License-Identifier: GPL-3.0-or-later\n"
CIL_SPDX = "; SPDX-License-Identifier: GPL-3.0-or-later\n"

GOOD_TE = (
    SPDX
    + """policy_module(demo, 1.0.0)

gen_require(`
    type init_t;
    type bin_t;
')

type demo_t;
type demo_exec_t;
type demo_var_t alias demo_old_t;
demo_helper(demo_var_t)
demo_read(demo_t)

# The daemon reads its own files; nothing else.
allow demo_t demo_var_t:file { read open getattr };
allow demo_t self:capability { chown setuid };

optional_policy(`
    demo_helper(demo_t)
')
"""
)
GOOD_IF = (
    SPDX
    + """## <summary>Demo interfaces.</summary>
interface(`demo_read',`
    gen_require(`
        type demo_var_t;
    ')
    allow $1 demo_var_t:file read_file_perms;
    demo_helper($1)
')

interface(`demo_helper',`
    allow $1 self:process signal;
')
"""
)
GOOD_FC = (
    SPDX
    + """/usr/bin/demo          --  gen_context(system_u:object_r:demo_exec_t,s0)
/var/lib/demo(/.*)?        gen_context(system_u:object_r:demo_var_t,s0)
/var/lib/demo/old          system_u:object_r:demo_old_t:s0
/var/lib/demo/none         <<none>>
/usr/bin/tool              --  gen_context(system_u:object_r:bin_t,s0)
"""
)
GOOD_FILES = {
    "herkos/selinux/demo.te": GOOD_TE,
    "herkos/selinux/demo.if": GOOD_IF,
    "herkos/selinux/demo.fc": GOOD_FC,
}

GOOD_CIL = (
    CIL_SPDX
    + """; The unit talks to the daemon socket.
(optional demo_connect
    (allow init_t unconfined_service_t (unix_stream_socket (connectto))))
"""
)


def check(files: dict[str, str]) -> list[cs.Problem]:
    """Write the files to a temporary folder and check them all (every key is tracked)."""
    with tempfile.TemporaryDirectory() as tmp:
        for rel, text in files.items():
            path = Path(tmp) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
        return cs.check_files(Path(tmp), list(files))


def errors(problems: list[cs.Problem]) -> list[cs.Problem]:
    return [p for p in problems if not p.warning]


def warnings(problems: list[cs.Problem]) -> list[cs.Problem]:
    return [p for p in problems if p.warning]


def te(body: str, name: str = "herkos/selinux/demo.te") -> dict[str, str]:
    return {name: SPDX + body}


class Base(unittest.TestCase):
    def assertErrors(self, problems: list[cs.Problem], *rules: str) -> None:  # noqa: N802
        self.assertEqual(sorted(p.rule for p in errors(problems)), sorted(rules), problems)

    def assertClean(self, problems: list[cs.Problem]) -> None:  # noqa: N802
        self.assertEqual([str(p) for p in problems], [])


class GoodFiles(Base):
    def test_good_module_has_no_problems(self) -> None:
        self.assertClean(check(GOOD_FILES))

    def test_good_cil_has_no_problems(self) -> None:
        self.assertClean(check({"herkos/selinux/demo.cil": GOOD_CIL}))

    def test_the_cil_module_of_the_repo_is_good(self) -> None:
        rel = "herkos/selinux/herkos.cil"
        self.assertTrue((ROOT / rel).is_file())
        self.assertEqual(errors(cs.check_files(ROOT, [rel])), [])

    def test_problem_text_has_path_first(self) -> None:
        problems = check(te("allow a_t self:capability sys_admin;\n"))
        self.assertTrue(str(problems[0]).startswith("herkos/selinux/demo.te: line 2: "))

    def test_result_is_sorted_and_repeatable(self) -> None:
        files = {
            **te("allow b_t self:capability sys_admin;\nallow a_t self:capability sys_admin;\n"),
            **te("allow c_t self:capability sys_admin;\n", "docs/z.te"),
        }
        first = check(files)
        self.assertEqual(first, check(dict(reversed(list(files.items())))))
        self.assertEqual([p.path for p in first], sorted(p.path for p in first))
        self.assertEqual(
            [(p.path, p.line) for p in first][1:],
            [("herkos/selinux/demo.te", 2), ("herkos/selinux/demo.te", 3)],
        )


class FileContextTypes(Base):
    def fc(self, line: str, te_text: str = GOOD_TE) -> list[cs.Problem]:
        return check(
            {
                "herkos/selinux/demo.te": te_text,
                "herkos/selinux/demo.if": GOOD_IF,
                "herkos/selinux/demo.fc": SPDX + line + "\n",
            }
        )

    def test_type_defined_with_type(self) -> None:
        self.assertClean(self.fc("/a -- gen_context(system_u:object_r:demo_t,s0)"))

    def test_type_defined_as_alias(self) -> None:
        self.assertClean(self.fc("/a -- gen_context(system_u:object_r:demo_old_t,s0)"))

    def test_type_defined_with_typealias(self) -> None:
        text = GOOD_TE + "typealias demo_t alias demo_other_t;\n"
        self.assertClean(self.fc("/a gen_context(system_u:object_r:demo_other_t,s0)", text))

    def test_type_with_attributes_and_alias_list(self) -> None:
        text = SPDX + "type one_t alias { two_t three_t }, domain, mlsprocessread;\n"
        for name in ("one_t", "two_t", "three_t"):
            self.assertClean(self.fc(f"/a gen_context(system_u:object_r:{name},s0)", text))

    def test_type_taken_from_the_base_policy_by_require(self) -> None:
        self.assertClean(self.fc("/a gen_context(system_u:object_r:bin_t,s0)"))

    def test_type_in_a_require_block_of_the_plain_form(self) -> None:
        text = SPDX + "require {\n    type etc_t, var_t;\n}\n"
        self.assertClean(self.fc("/a gen_context(system_u:object_r:var_t,s0)", text))

    def test_second_type_of_a_gen_require_list(self) -> None:
        text = SPDX + "gen_require(`\n    type etc_t, var_t;\n    class file read;\n')\n"
        self.assertClean(self.fc("/a gen_context(system_u:object_r:var_t,s0)", text))

    def test_undefined_type_is_an_error(self) -> None:
        problems = self.fc("/a gen_context(system_u:object_r:ghost_t,s0)")
        self.assertErrors(problems, "fc-type")
        self.assertIn("ghost_t", problems[0].message)
        self.assertEqual(problems[0].line, 2)

    def test_type_that_only_looks_like_a_defined_one_is_undefined(self) -> None:
        for name in ("demo_", "demo_t2", "xdemo_t", "demo"):
            problems = self.fc(f"/a gen_context(system_u:object_r:{name},s0)")
            self.assertErrors(problems, "fc-type")

    def test_type_named_only_in_a_comment_is_undefined(self) -> None:
        text = SPDX + "# type ghost_t;\ntype real_t;\n"
        problems = self.fc("/a gen_context(system_u:object_r:ghost_t,s0)", text)
        self.assertErrors(problems, "fc-type")

    def test_type_named_only_in_a_neverallow_or_allow_is_undefined(self) -> None:
        text = SPDX + "type real_t;\nallow real_t ghost_t:file read;\n"
        problems = self.fc("/a gen_context(system_u:object_r:ghost_t,s0)", text)
        self.assertErrors(problems, "fc-type")

    def test_attribute_is_not_a_type(self) -> None:
        text = SPDX + "attribute ghost_t;\ntype real_t;\n"
        problems = self.fc("/a gen_context(system_u:object_r:ghost_t,s0)", text)
        self.assertErrors(problems, "fc-type")

    def test_type_transition_is_not_a_definition(self) -> None:
        text = SPDX + "type_transition a_t b_t:file ghost_t;\ntype real_t;\n"
        problems = self.fc("/a gen_context(system_u:object_r:ghost_t,s0)", text)
        self.assertErrors(problems, "fc-type")

    def test_type_of_another_module_does_not_count(self) -> None:
        problems = check(
            {
                "herkos/selinux/one.te": SPDX + "type one_t;\n",
                "herkos/selinux/two.te": SPDX + "type two_t;\n",
                "herkos/selinux/two.fc": SPDX + "/a gen_context(system_u:object_r:one_t,s0)\n",
            }
        )
        self.assertErrors(problems, "fc-type")

    def test_fc_in_another_folder_does_not_use_this_te(self) -> None:
        problems = check(
            {
                "herkos/selinux/demo.te": GOOD_TE,
                "herkos/selinux/demo.if": GOOD_IF,
                "docs/demo.fc": SPDX + "/a gen_context(system_u:object_r:demo_t,s0)\n",
            }
        )
        self.assertErrors(problems, "fc-type")

    def test_fc_with_no_te_is_an_error(self) -> None:
        problems = check({"herkos/selinux/demo.fc": GOOD_FC})
        self.assertErrors(problems, "fc-type")
        self.assertIn("no .te or .cil", problems[0].message)

    def test_fc_with_no_types_needs_no_te(self) -> None:
        self.assertClean(check({"herkos/selinux/demo.fc": SPDX + "/a <<none>>\n"}))

    def test_fc_comment_and_blank_lines_are_ignored(self) -> None:
        text = "# object_r:ghost_t\n\n/a gen_context(system_u:object_r:demo_t,s0) # object_r:x_t\n"
        self.assertClean(self.fc(text.strip()))

    def test_every_undefined_type_gets_its_line(self) -> None:
        text = (
            "/a gen_context(system_u:object_r:bad1_t,s0)\n\n"
            "/b gen_context(system_u:object_r:bad2_t,s0)"
        )
        problems = self.fc(text)
        self.assertEqual([p.line for p in problems], [2, 4])

    def test_fc_types_in_ifdef_blocks_are_checked(self) -> None:
        text = (
            "ifdef(`distro_suse',`\n/a gen_context(system_u:object_r:ghost_t,s0)\n')\n"
            "/b gen_context(system_u:object_r:demo_t,s0)"
        )
        self.assertErrors(self.fc(text), "fc-type")

    def test_type_defined_in_the_cil_of_the_module(self) -> None:
        files = {
            "herkos/selinux/demo.cil": CIL_SPDX + "(type demo_t)\n",
            "herkos/selinux/demo.fc": SPDX + "/a gen_context(system_u:object_r:demo_t,s0)\n",
        }
        self.assertClean(check(files))

    def test_type_defined_in_an_unrelated_te_is_not_enough_for_type_in_a_require_of_it(
        self,
    ) -> None:
        files = {
            "herkos/selinux/one.te": SPDX + "gen_require(`\n type x_t;\n')\n",
            "herkos/selinux/two.te": SPDX + "type two_t;\n",
            "herkos/selinux/two.fc": SPDX + "/a gen_context(system_u:object_r:x_t,s0)\n",
        }
        self.assertErrors(check(files), "fc-type")


class SysAdmin(Base):
    def one(self, rule: str) -> list[cs.Problem]:
        return check(te(rule + "\n"))

    def test_plain_sys_admin(self) -> None:
        self.assertErrors(self.one("allow demo_t self:capability sys_admin;"), "sys-admin")

    def test_sys_admin_in_a_set(self) -> None:
        self.assertErrors(
            self.one("allow demo_t self:capability { chown sys_admin setuid };"), "sys-admin"
        )

    def test_sys_admin_over_several_lines(self) -> None:
        rule = "allow demo_t\n    self:capability\n    {\n chown\n sys_admin\n};"
        problems = self.one(rule)
        self.assertErrors(problems, "sys-admin")
        self.assertEqual(problems[0].line, 2)

    def test_sys_admin_for_another_type_and_for_user_namespaces(self) -> None:
        self.assertErrors(self.one("allow demo_t other_t:capability sys_admin;"), "sys-admin")
        self.assertErrors(self.one("allow demo_t self:cap_userns sys_admin;"), "sys-admin")
        self.assertErrors(self.one("allow demo_t self:{ file capability } sys_admin;"), "sys-admin")

    def test_sys_admin_in_a_set_of_types(self) -> None:
        self.assertErrors(self.one("allow { a_t b_t } self:capability sys_admin;"), "sys-admin")

    def test_a_comment_does_not_excuse_sys_admin(self) -> None:
        rule = "# We really need it, honestly, for the mounts.\n"
        rule += "allow demo_t self:capability sys_admin; # needed for mounts"
        self.assertErrors(self.one(rule), "sys-admin")

    def test_sys_admin_with_spaces_and_tabs(self) -> None:
        self.assertErrors(self.one("allow\tdemo_t   self:capability\tsys_admin ;"), "sys-admin")

    def test_two_statements_on_one_line_are_both_read(self) -> None:
        rule = "allow a_t self:capability chown; allow b_t self:capability sys_admin;"
        self.assertErrors(self.one(rule), "sys-admin")

    def test_sys_admin_in_an_interface(self) -> None:
        text = SPDX + "interface(`demo_x',`\n allow $1 self:capability sys_admin;\n')\n"
        self.assertErrors(check({"herkos/selinux/demo.if": text}), "sys-admin")

    def test_sys_admin_inside_optional_policy_and_ifdef(self) -> None:
        rule = "optional_policy(`\n ifdef(`x',`\n allow a_t self:capability sys_admin;\n')\n')"
        self.assertErrors(self.one(rule), "sys-admin")

    def test_other_capabilities_are_fine(self) -> None:
        self.assertClean(self.one("allow demo_t self:capability { net_bind_service kill };"))

    def test_names_that_only_contain_sys_admin_are_fine(self) -> None:
        for perm in ("sys_admin_ok", "my_sys_admin", "sys_administrator", "sys_admin2"):
            self.assertClean(self.one(f"allow demo_t self:capability {perm};"))

    def test_sys_admin_with_another_class_is_not_a_capability(self) -> None:
        self.assertClean(self.one("allow demo_t demo_t:file sys_admin;"))

    def test_neverallow_dontaudit_auditallow_are_not_grants(self) -> None:
        for kind in ("neverallow", "dontaudit", "auditallow"):
            self.assertClean(self.one(f"{kind} demo_t self:capability sys_admin;"))

    def test_sys_admin_in_a_comment_is_not_a_grant(self) -> None:
        self.assertClean(self.one("# allow demo_t self:capability sys_admin;"))
        self.assertClean(self.one("type x_t; # allow demo_t self:capability sys_admin;"))
        self.assertClean(self.one("dnl allow demo_t self:capability sys_admin;"))

    def test_sys_admin_after_a_comment_with_a_quote(self) -> None:
        rule = "# it's fine\nallow demo_t self:capability sys_admin;"
        self.assertErrors(self.one(rule), "sys-admin")

    def test_hash_inside_a_string_does_not_start_a_comment(self) -> None:
        rule = 'type_transition a_t b_t:file c_t "x#y"; allow d_t self:capability sys_admin;'
        self.assertErrors(self.one(rule), "sys-admin")

    def test_crlf_line_endings(self) -> None:
        text = SPDX + "allow demo_t self:capability sys_admin;\r\n"
        problems = check({"herkos/selinux/demo.te": text.replace("\n", "\r\n")})
        self.assertErrors(problems, "sys-admin")


class Wildcards(Base):
    def one(self, text: str) -> list[cs.Problem]:
        return check(te(text + "\n"))

    def test_star_in_permissions_without_comment(self) -> None:
        self.assertErrors(self.one("allow demo_t demo_var_t:file *;"), "wildcard")

    def test_star_in_the_class_and_in_types(self) -> None:
        self.assertErrors(self.one("allow demo_t demo_var_t:* read;"), "wildcard")
        self.assertErrors(self.one("allow * demo_var_t:file read;"), "wildcard")
        self.assertErrors(self.one("allow demo_t *:file read;"), "wildcard")

    def test_complement_is_a_wildcard(self) -> None:
        self.assertErrors(self.one("allow demo_t demo_var_t:file ~{ write };"), "wildcard")
        self.assertErrors(self.one("allow demo_t ~demo_var_t:file read;"), "wildcard")
        self.assertErrors(self.one("allow ~{ a_t b_t } demo_var_t:file read;"), "wildcard")

    def test_star_in_a_set_and_over_lines(self) -> None:
        self.assertErrors(self.one("allow demo_t demo_var_t:file {\n read\n *\n};"), "wildcard")

    def test_comment_on_the_same_line(self) -> None:
        text = "allow demo_t demo_var_t:file *; # the daemon owns this folder completely"
        self.assertClean(self.one(text))

    def test_comment_on_a_later_line_of_the_rule(self) -> None:
        text = "allow demo_t demo_var_t:file\n    *; # the daemon owns this folder completely"
        self.assertClean(self.one(text))

    def test_comment_right_above(self) -> None:
        text = "# The daemon owns this folder and may do anything in it.\n"
        self.assertClean(self.one(text + "allow demo_t demo_var_t:file *;"))

    def test_comment_block_above_in_several_lines(self) -> None:
        text = "# The daemon owns\n# this folder.\nallow demo_t demo_var_t:file *;"
        self.assertClean(self.one(text))

    def test_blank_line_between_comment_and_rule_does_not_count(self) -> None:
        text = "# The daemon owns this folder and may do anything in it.\n\n"
        self.assertErrors(self.one(text + "allow demo_t demo_var_t:file *;"), "wildcard")

    def test_comment_of_the_rule_above_does_not_cover_the_next_rule(self) -> None:
        text = (
            "# The daemon owns this folder and may do anything in it.\n"
            "allow demo_t demo_var_t:file *;\n"
            "allow demo_t demo_var_t:dir *;"
        )
        problems = self.one(text)
        self.assertErrors(problems, "wildcard")
        self.assertEqual(problems[0].line, 4)

    def test_spdx_line_is_not_an_explanation(self) -> None:
        text = "allow demo_t demo_var_t:file *;\n"
        self.assertErrors(check({"herkos/selinux/demo.te": SPDX + text}), "wildcard")
        self.assertErrors(check({"herkos/selinux/demo.te": text}), "wildcard")

    def test_empty_or_decoration_comments_are_not_explanations(self) -> None:
        for comment in ("#", "# TODO", "######", "# ---- ----", "# 1234567890123", "#  x  "):
            text = f"{comment}\nallow demo_t demo_var_t:file *;"
            self.assertErrors(self.one(text), "wildcard")

    def test_comment_below_the_rule_does_not_count(self) -> None:
        text = "allow demo_t demo_var_t:file *;\n\n# The daemon owns this folder and more."
        self.assertErrors(self.one(text), "wildcard")

    def test_explained_wildcard_still_fails_for_sys_admin(self) -> None:
        text = "# Explained at length, with all the words.\nallow a_t self:capability *;"
        self.assertClean(self.one(text))
        text = (
            "# Explained at length, with all the words.\nallow a_t self:capability { * sys_admin };"
        )
        self.assertErrors(self.one(text), "sys-admin")

    def test_star_in_comments_strings_and_other_statements_is_fine(self) -> None:
        self.assertClean(self.one("# allow a_t b_t:file *;"))
        self.assertClean(self.one("neverallow * demo_var_t:file *;"))
        self.assertClean(self.one("dontaudit demo_t *:file ~{ read };"))
        self.assertClean(self.one('type_transition a_t b_t:file c_t "*.so";'))
        self.assertClean(self.one("auditallow demo_t demo_var_t:file *;"))

    def test_m4_argument_lists_with_star_are_not_a_wildcard(self) -> None:
        text = "interface(`demo_x',`\n allow $1 demo_var_t:file { read $* };\n')"
        self.assertClean(check({"herkos/selinux/demo.if": SPDX + text + "\n"}))

    def test_star_in_an_interface_body_is_checked(self) -> None:
        text = "interface(`demo_x',`\n allow $1 demo_var_t:file *;\n')\n"
        self.assertErrors(check({"herkos/selinux/demo.if": SPDX + text}), "wildcard")

    def test_the_line_of_the_error_is_the_first_line_of_the_rule(self) -> None:
        problems = self.one("type a_t;\n\nallow demo_t\n  demo_var_t:file *;")
        self.assertEqual(problems[0].line, 4)


class Interfaces(Base):
    def test_call_of_an_interface_of_an_if_file(self) -> None:
        self.assertClean(check(GOOD_FILES))

    def test_call_of_an_interface_of_another_if_file(self) -> None:
        files = {
            "herkos/selinux/lib.if": SPDX + "interface(`lib_use',`\n')\n",
            **te("lib_use(demo_t)\n"),
        }
        self.assertClean(check(files))

    def test_template_and_define_are_definitions(self) -> None:
        files = {
            "herkos/selinux/lib.if": SPDX + "template(`lib_t_one',`\n')\ndefine(`lib_m',`x')\n",
            **te("lib_t_one(demo)\nlib_m(demo)\n"),
        }
        self.assertClean(check(files))

    def test_undefined_interface_is_an_error(self) -> None:
        problems = check(te("demo_missing(demo_t)\n"))
        self.assertErrors(problems, "interface")
        self.assertIn("demo_missing", problems[0].message)
        self.assertEqual(problems[0].line, 2)

    def test_misspelled_interface_is_an_error(self) -> None:
        files = {
            **GOOD_FILES,
            "herkos/selinux/demo.te": GOOD_TE.replace("demo_read(", "demo_raed("),
        }
        self.assertErrors(check(files), "interface")

    def test_interface_defined_only_in_a_comment_is_undefined(self) -> None:
        files = {
            "herkos/selinux/lib.if": SPDX + "# interface(`lib_use',`')\n",
            **te("lib_use(demo_t)\n"),
        }
        self.assertErrors(check(files), "interface")

    def test_interface_with_the_same_prefix_is_not_the_same(self) -> None:
        files = {
            "herkos/selinux/lib.if": SPDX + "interface(`lib_use',`\n')\n",
            **te("lib_use_more(demo_t)\nlib_us(demo_t)\n"),
        }
        self.assertErrors(check(files), "interface", "interface")

    def test_call_inside_optional_policy_must_exist_too(self) -> None:
        problems = check(te("optional_policy(`\n demo_missing(demo_t)\n')\n"))
        self.assertErrors(problems, "interface")

    def test_call_in_an_if_file_is_checked(self) -> None:
        text = SPDX + "interface(`demo_a',`\n demo_missing($1)\n')\n"
        self.assertErrors(check({"herkos/selinux/demo.if": text}), "interface")

    def test_interface_calling_another_interface_of_the_file(self) -> None:
        text = SPDX + "interface(`demo_a',`\n')\ninterface(`demo_b',`\n demo_a($1)\n')\n"
        self.assertClean(check({"herkos/selinux/demo.if": text}))

    def test_reference_policy_interface_is_a_warning(self) -> None:
        problems = check(te("files_read_etc_files(demo_t)\nsystemd_use_fds(demo_t)\n"))
        self.assertEqual(errors(problems), [])
        self.assertEqual(len(warnings(problems)), 2)
        self.assertIn("cannot be checked offline", problems[0].message)

    def test_reference_policy_pattern_macros_are_warnings(self) -> None:
        problems = check(te("read_files_pattern(demo_t, a_t, b_t)\ncan_exec(demo_t, bin_t)\n"))
        self.assertEqual(errors(problems), [])
        self.assertEqual(len(warnings(problems)), 2)

    def test_local_prefix_is_never_external(self) -> None:
        for name in ("mymod_x", "vilna_x", "files", "unknownlayer_x", "x"):
            self.assertErrors(check(te(f"{name}(demo_t)\n")), "interface")

    def test_a_warning_is_given_once_for_each_file_and_name(self) -> None:
        problems = check(te("files_a(demo_t)\nfiles_a(demo_t)\nfiles_b(demo_t)\n"))
        self.assertEqual(len(warnings(problems)), 2)

    def test_keywords_are_not_calls(self) -> None:
        text = (
            "policy_module(demo, 1.0)\ngen_require(`\n type a_t;\n')\n"
            "ifdef(`x',`\n type b_t;\n')\nifndef(`y',`\n type c_t;\n')\n"
            "tunable_policy(`t',`\n allow b_t c_t:file read;\n')\n"
            "gen_tunable(t, false)\nifelse(a,b,c,d)\ndnl a note: foo(x)\n"
        )
        self.assertClean(check(te(text)))

    def test_interface_in_a_comment_is_not_a_call(self) -> None:
        self.assertClean(check(te("# demo_missing(demo_t)\ntype a_t; # other_missing(x)\n")))

    def test_interface_name_inside_a_string_is_not_a_call(self) -> None:
        text = 'type_transition a_t b_t:file c_t "demo_missing(x)";\n'
        self.assertClean(check(te(text)))

    def test_call_after_m4_quote_and_in_arguments(self) -> None:
        problems = check(te("demo_outer(`demo_missing(demo_t)')\n"))
        self.assertErrors(problems, "interface", "interface")

    def test_name_after_dollar_or_digit_is_not_a_call(self) -> None:
        text = SPDX + "template(`demo_t1',`\n type $1_t;\n $1_domain($1)\n')\n"
        self.assertClean(check({"herkos/selinux/demo.if": text}))

    def test_call_with_space_before_the_bracket(self) -> None:
        self.assertErrors(check(te("demo_missing (demo_t)\n")), "interface")


class CilRules(Base):
    def one(self, text: str, name: str = "herkos/selinux/demo.cil") -> list[cs.Problem]:
        return check({name: CIL_SPDX + text})

    def test_sys_admin(self) -> None:
        self.assertErrors(self.one("(allow a_t self (capability (sys_admin)))\n"), "sys-admin")

    def test_sys_admin_in_a_list_and_over_lines(self) -> None:
        text = "(allow a_t\n  b_t\n  (capability (chown\n   sys_admin)))\n"
        problems = self.one(text)
        self.assertErrors(problems, "sys-admin")
        self.assertEqual(problems[0].line, 2)

    def test_sys_admin_of_user_namespaces(self) -> None:
        self.assertErrors(self.one("(allow a_t self (cap_userns (sys_admin)))\n"), "sys-admin")

    def test_sys_admin_in_a_macro_block_and_optional(self) -> None:
        text = "(block b (macro m ((type t)) (optional o (allow t self (capability (sys_admin))))))"
        self.assertErrors(self.one(text), "sys-admin")

    def test_sys_admin_in_a_classpermissionset(self) -> None:
        text = "(classpermission cp)\n(classpermissionset cp (capability (sys_admin)))\n"
        self.assertErrors(self.one(text), "sys-admin")

    def test_comment_does_not_excuse_sys_admin(self) -> None:
        text = "; We need it for the mounts, said the author.\n"
        text += "(allow a_t self (capability (sys_admin))) ; for the mounts\n"
        self.assertErrors(self.one(text), "sys-admin")

    def test_other_capabilities_and_look_alike_names_are_fine(self) -> None:
        self.assertClean(self.one("(allow a_t self (capability (chown sys_admin_x)))\n"))
        self.assertClean(self.one("(allow a_t self (file (sys_admin)))\n"))

    def test_not_grants_are_not_read(self) -> None:
        self.assertClean(self.one("(neverallow a_t self (capability (sys_admin)))\n"))
        self.assertClean(self.one("(dontaudit a_t self (capability (sys_admin)))\n"))
        self.assertClean(self.one("(auditallow a_t self (capability (sys_admin)))\n"))

    def test_sys_admin_in_a_comment_or_a_string_is_not_a_grant(self) -> None:
        self.assertClean(self.one("; (allow a_t self (capability (sys_admin)))\n"))
        self.assertClean(
            self.one('(filecon "/x (allow a self (capability (sys_admin)))" file ())\n')
        )

    def test_all_without_comment(self) -> None:
        self.assertErrors(self.one("(allow a_t b_t (file (all)))\n"), "wildcard")

    def test_not_and_star_without_comment(self) -> None:
        self.assertErrors(self.one("(allow a_t b_t (file ((not (write)))))\n"), "wildcard")
        self.assertErrors(self.one("(allow a_t b_t (file (*)))\n"), "wildcard")
        self.assertErrors(self.one("(allow all b_t (file (read)))\n"), "wildcard")
        self.assertErrors(self.one("(allow a_t (not b_t) (file (read)))\n"), "wildcard")

    def test_all_with_a_comment(self) -> None:
        self.assertClean(self.one("(allow a_t b_t (file (all))) ; it owns the whole folder\n"))
        self.assertClean(self.one("; It owns the whole folder.\n(allow a_t b_t (file (all)))\n"))

    def test_comment_inside_a_multiline_rule(self) -> None:
        text = "(allow a_t b_t\n  (file (all))) ; it owns the whole folder\n"
        self.assertClean(self.one(text))

    def test_blank_line_or_short_comment_does_not_count(self) -> None:
        self.assertErrors(
            self.one("; It owns the whole folder.\n\n(allow a_t b_t (file (all)))\n"), "wildcard"
        )
        self.assertErrors(self.one("; ok\n(allow a_t b_t (file (all)))\n"), "wildcard")
        self.assertErrors(self.one("(allow a_t b_t (file (all))) ;;;;;;;;;;;;;;\n"), "wildcard")

    def test_comment_of_the_rule_above_does_not_cover_the_next_one(self) -> None:
        text = (
            "; It owns the whole folder.\n(allow a_t b_t (file (all)))\n"
            "(allow a_t b_t (dir (all)))\n"
        )
        problems = self.one(text)
        self.assertErrors(problems, "wildcard")
        self.assertEqual(problems[0].line, 4)

    def test_word_all_as_part_of_a_name_is_fine(self) -> None:
        self.assertClean(self.one("(allow all_t b_t (file (read_all notall)))\n"))

    def test_syntax_errors(self) -> None:
        for text in ("(allow a_t b_t\n", "(allow a_t))\n", '(filecon "x\n', ")\n"):
            problems = self.one(text)
            self.assertErrors(problems, "syntax")
            self.assertTrue(problems[0].message.startswith("CIL syntax: line "))

    def test_brackets_in_strings_and_comments_do_not_count(self) -> None:
        text = '(filecon "/a(b" file ()) ; ( ( (\n(type a_t)\n'
        self.assertClean(self.one(text))

    def test_escaped_quote_in_a_string(self) -> None:
        self.assertClean(self.one('(filecon "/a\\"(" file ())\n'))

    def test_very_deep_nesting_does_not_crash(self) -> None:
        depth = 50000
        problems = self.one("(" * depth + "allow" + ")" * depth + "\n")
        self.assertEqual(errors(problems), [])
        self.assertErrors(self.one("(" * depth + "\n"), "syntax")

    def test_empty_and_comment_only_files(self) -> None:
        self.assertClean(self.one(""))
        self.assertClean(self.one("; only a comment\n"))

    def test_bare_atoms_and_empty_lists_at_top_level(self) -> None:
        self.assertClean(self.one('() atom "str" ()\n'))


class CilReferences(Base):
    def files(self, text: str, extra: str = "") -> dict[str, str]:
        return {"herkos/selinux/demo.cil": CIL_SPDX + text, "herkos/selinux/lib.cil": extra}

    def test_call_of_a_macro(self) -> None:
        text = "(macro m ((type t)) (typeattribute t))\n(call m (a_t))\n"
        self.assertClean(check(self.files(text, CIL_SPDX)))

    def test_call_of_a_macro_of_another_file(self) -> None:
        extra = CIL_SPDX + "(macro m ((type t)) (typeattribute t))\n"
        self.assertClean(check(self.files("(call m (a_t))\n", extra)))

    def test_call_of_a_macro_in_a_block(self) -> None:
        text = "(block b (macro m ((type t)) (typeattribute t)))\n(call b.m (a_t))\n"
        self.assertClean(check(self.files(text, CIL_SPDX)))

    def test_unknown_macro_is_an_error(self) -> None:
        problems = check(self.files("(call missing (a_t))\n", CIL_SPDX))
        self.assertErrors(problems, "macro")
        self.assertEqual(problems[0].line, 2)

    def test_unknown_macro_inside_optional_is_a_warning(self) -> None:
        problems = check(self.files("(optional o (call missing (a_t)))\n", CIL_SPDX))
        self.assertEqual(errors(problems), [])
        self.assertEqual(len(warnings(problems)), 1)

    def test_macro_named_in_a_comment_or_as_parameter_is_unknown(self) -> None:
        text = "; (macro m ((type t)) (typeattribute t))\n(call m (a_t))\n"
        self.assertErrors(check(self.files(text, CIL_SPDX)), "macro")
        text = "(macro other ((name m)) (typeattribute t))\n(call m (a_t))\n"
        self.assertErrors(check(self.files(text, CIL_SPDX)), "macro")

    def test_filecon_type_defined(self) -> None:
        text = '(type a_t)\n(filecon "/a" file (system_u object_r a_t ((s0) (s0))))\n'
        self.assertClean(check(self.files(text, CIL_SPDX)))

    def test_filecon_type_undefined(self) -> None:
        text = '(type a_t)\n(filecon "/a" file (system_u object_r b_t ((s0) (s0))))\n'
        problems = check(self.files(text, CIL_SPDX))
        self.assertErrors(problems, "fc-type")
        self.assertEqual(problems[0].line, 3)

    def test_filecon_type_in_a_block_and_in_a_named_context(self) -> None:
        text = (
            "(block b (type a_t))\n(context c (system_u object_r b.a_t ((s0) (s0))))\n"
            '(filecon "/a" file c)\n(filecon "/b" file (system_u object_r a_t ((s0) (s0))))\n'
        )
        self.assertClean(check(self.files(text, CIL_SPDX)))

    def test_filecon_type_defined_as_a_macro_parameter_is_undefined(self) -> None:
        text = (
            "(macro m ((type ghost_t)) (typeattribute ghost_t))\n"
            '(filecon "/a" file (system_u object_r ghost_t ((s0) (s0))))\n'
        )
        self.assertErrors(check(self.files(text, CIL_SPDX)), "fc-type")

    def test_filecon_type_defined_by_the_te_of_the_module(self) -> None:
        files = {
            "herkos/selinux/demo.te": SPDX + "type a_t;\n",
            "herkos/selinux/demo.cil": CIL_SPDX
            + '(filecon "/a" file (system_u object_r a_t ((s0) (s0))))\n',
        }
        self.assertClean(check(files))

    def test_filecon_with_no_type_or_unknown_context_is_skipped(self) -> None:
        self.assertClean(check(self.files('(filecon "/a" file ())\n(filecon "/b" file c)\n', "")))


class DocsBlocks(Base):
    def doc(self, text: str) -> list[cs.Problem]:
        return check({"docs/page.md": text})

    def test_policy_block_with_sys_admin(self) -> None:
        text = "# T\n\ntext\n\n```te\nallow a_t self:capability sys_admin;\n```\n"
        problems = self.doc(text)
        self.assertErrors(problems, "sys-admin")
        self.assertEqual(problems[0].line, 6)
        self.assertEqual(problems[0].path, "docs/page.md")

    def test_block_languages(self) -> None:
        for lang in ("te", "selinux", "sepolicy", "refpolicy", "if", "TE"):
            text = f"```{lang}\nallow a_t b_t:file *;\n```\n"
            self.assertErrors(self.doc(text), "wildcard")

    def test_cil_block(self) -> None:
        text = "```cil\n(allow a_t self (capability (sys_admin)))\n```\n"
        self.assertErrors(self.doc(text), "sys-admin")

    def test_block_without_language_that_holds_a_rule(self) -> None:
        self.assertErrors(self.doc("```\nallow a_t self:capability sys_admin;\n```\n"), "sys-admin")
        text = "```\n(allow a_t self (capability (sys_admin)))\n```\n"
        self.assertErrors(self.doc(text), "sys-admin")

    def test_tilde_fence_and_indented_fence(self) -> None:
        text = "~~~te\nallow a_t self:capability sys_admin;\n~~~\n"
        self.assertErrors(self.doc(text), "sys-admin")
        text = "1. step\n\n   ```te\n   allow a_t self:capability sys_admin;\n   ```\n"
        self.assertErrors(self.doc(text), "sys-admin")

    def test_long_fence_holds_a_short_one(self) -> None:
        text = "````te\n```\nallow a_t self:capability sys_admin;\n````\n"
        self.assertErrors(self.doc(text), "sys-admin")

    def test_unclosed_block_runs_to_the_end(self) -> None:
        self.assertErrors(self.doc("```te\nallow a_t self:capability sys_admin;\n"), "sys-admin")

    def test_other_languages_and_prose_are_ignored(self) -> None:
        text = (
            "```sh\nallow a_t self:capability sys_admin;\n```\n"
            "```\nls -l\necho allow\n```\n"
            "allow a_t self:capability sys_admin;\n"
            "```toml\nx = 'allow a_t self:capability sys_admin;'\n```\n"
        )
        self.assertClean(self.doc(text))

    def test_explained_wildcard_in_a_block(self) -> None:
        text = "```te\n# It owns the whole folder.\nallow a_t b_t:file *;\n```\n"
        self.assertClean(self.doc(text))

    def test_cil_block_that_cannot_be_read_is_a_warning(self) -> None:
        problems = self.doc("```cil\n(allow a_t\n```\n")
        self.assertEqual(errors(problems), [])
        self.assertEqual(len(warnings(problems)), 1)

    def test_block_does_not_define_types_or_call_interfaces(self) -> None:
        text = (
            "```te\ndemo_missing(a_t)\ntype a_t;\n```\n"
            "```\n/a gen_context(system_u:object_r:x_t,s0)\n```\n"
        )
        self.assertClean(self.doc(text))

    def test_line_numbers_after_other_blocks(self) -> None:
        text = "```sh\nx\n```\n\n```te\n\nallow a_t self:capability sys_admin;\n```\n"
        self.assertEqual(self.doc(text)[0].line, 7)

    def test_md_with_invalid_bytes_or_bom_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = "```te\nallow a_t self:capability sys_admin;\n```\n"
            (Path(tmp) / "a.md").write_bytes(b"\xef\xbb\xbf\xff\n" + body.encode())
            problems = cs.check_files(Path(tmp), ["a.md"])
        self.assertErrors(problems, "sys-admin")

    def test_te_file_under_docs_is_checked_like_any_other(self) -> None:
        problems = check({"docs/x.te": SPDX + "allow a_t self:capability sys_admin;\n"})
        self.assertErrors(problems, "sys-admin")


class Files(Base):
    def test_bom_is_an_error_and_the_rest_is_still_checked(self) -> None:
        text = "\ufeff" + SPDX + "allow a_t self:capability sys_admin;\n"
        self.assertErrors(check({"herkos/selinux/demo.te": text}), "file", "sys-admin")

    def test_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.te").write_bytes(b"type a_t; \xff\xfe\n")
            problems = cs.check_files(Path(tmp), ["a.te"])
        self.assertErrors(problems, "file")
        self.assertIn("UTF-8", problems[0].message)

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            problems = cs.check_files(Path(tmp), ["gone.te", "gone.md"])
        self.assertEqual([p.rule for p in problems], ["", "file"])
        self.assertTrue(all(not p.warning for p in problems))

    def test_empty_files(self) -> None:
        self.assertClean(check({"herkos/selinux/demo.te": "", "herkos/selinux/demo.if": ""}))

    def test_file_without_final_newline(self) -> None:
        problems = check({"herkos/selinux/demo.te": "allow a_t self:capability sys_admin;"})
        self.assertErrors(problems, "sys-admin")

    def test_statement_without_semicolon_at_the_end(self) -> None:
        problems = check({"herkos/selinux/demo.te": "allow a_t self:capability sys_admin"})
        self.assertErrors(problems, "sys-admin")

    def test_the_same_path_twice_is_checked_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.te").write_text("allow a_t self:capability sys_admin;\n")
            problems = cs.check_files(Path(tmp), ["a.te", "a.te"])
        self.assertEqual(len(problems), 1)

    def test_legacy_problem_is_a_warning(self) -> None:
        legacy = frozenset({("herkos/selinux/demo.te", "sys-admin")})
        with mock.patch.object(cs, "LEGACY", legacy):
            problems = check(te("allow a_t self:capability sys_admin;\n"))
        self.assertEqual(errors(problems), [])
        self.assertEqual(len(warnings(problems)), 1)
        self.assertIn("warning: LEGACY: line 2: ", str(problems[0]))

    def test_legacy_covers_only_its_file_and_rule(self) -> None:
        legacy = frozenset({("herkos/selinux/demo.te", "sys-admin")})
        with mock.patch.object(cs, "LEGACY", legacy):
            problems = check(te("allow a_t b_t:file *;\n"))
            other = check(te("allow a_t self:capability sys_admin;\n", "herkos/selinux/other.te"))
        self.assertErrors(problems, "wildcard")
        self.assertErrors(other, "sys-admin")

    def test_legacy_is_empty_today(self) -> None:
        self.assertEqual(cs.LEGACY, frozenset())


class BigInput(Base):
    """Odd and large files are read in a short time (a loose limit: the cost must not grow with
    the square of the size)."""

    def timed(self, name: str, text: str) -> list[cs.Problem]:
        start = time.monotonic()
        problems = check({name: text})
        self.assertLess(time.monotonic() - start, 10)
        return problems

    def test_many_rules_with_comments(self) -> None:
        problems = self.timed(
            "herkos/selinux/a.te", "# the daemon owns it\nallow a b:c *;\n" * 20000
        )
        self.assertEqual(len(problems), 0)
        problems = self.timed("herkos/selinux/a.te", "allow a b:c *;\n" * 20000)
        self.assertEqual(len(problems), 20000)

    def test_many_calls_and_blocks_that_never_close(self) -> None:
        self.assertEqual(len(self.timed("herkos/selinux/a.te", "demo_x(y)\n" * 20000)), 1)
        for text in (
            "gen_require(`\n",
            "require {\n",
            "type a_t\n",
            "typealias a alias ",
            "allow ",
        ):
            self.assertEqual(self.timed("herkos/selinux/a.te", text * 50000), [])

    def test_many_cil_forms(self) -> None:
        problems = self.timed("herkos/selinux/a.cil", "(allow a b (file (read)))\n" * 50000)
        self.assertEqual(problems, [])
        self.assertEqual(self.timed("herkos/selinux/a.cil", "(" * 50000 + ")" * 50000), [])

    def test_one_very_long_line(self) -> None:
        self.assertEqual(self.timed("herkos/selinux/a.te", "x" * 2_000_000), [])
        self.assertEqual(self.timed("docs/a.md", "`" * 2_000_000), [])


class Main(Base):
    def run_main(self, args: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old = Path.cwd()
        try:
            if cwd:
                os.chdir(cwd)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cs.main(args)
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()

    def test_help_and_unknown_options(self) -> None:
        self.assertEqual(self.run_main(["-h"])[0], 0)
        self.assertEqual(self.run_main(["--help"])[0], 0)
        self.assertEqual(self.run_main(["--bad"])[0], 2)
        self.assertEqual(self.run_main(["-h", "x.te"])[0], 2)

    def test_wrong_file_type_is_exit_2(self) -> None:
        code, _, err = self.run_main(["a.txt"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)

    def test_good_and_bad_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in GOOD_FILES.items():
                (Path(tmp) / Path(rel).name).write_text(text)
            names = sorted(Path(r).name for r in GOOD_FILES)
            code, out, err = self.run_main(names, Path(tmp))
            self.assertEqual((code, err), (0, ""))
            self.assertIn("3 files checked: 0 errors", out)
            (Path(tmp) / "bad.te").write_text("allow a_t self:capability sys_admin;\n")
            code, out, err = self.run_main([*names, "bad.te"], Path(tmp))
        self.assertEqual(code, 1)
        self.assertIn("bad.te: line 1: allow gives sys_admin", err)
        self.assertIn("1 errors", out)

    def test_warnings_do_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.te").write_text("files_read_etc_files(a_t)\n")
            code, out, err = self.run_main(["a.te"], Path(tmp))
        self.assertEqual(code, 0)
        self.assertIn("a.te: warning: line 1: ", err)
        self.assertIn("0 errors, 1 warnings", out)

    def test_no_arguments_use_git_ls_files(self) -> None:
        listing = "herkos/selinux/herkos.cil\0docs/a.md\0docs/a.png\0README.md\0"
        run = mock.Mock(return_value=mock.Mock(stdout=listing))
        with mock.patch.object(cs.subprocess, "run", run):
            files = cs.tracked_files(ROOT)
        self.assertEqual(files, ["docs/a.md", "herkos/selinux/herkos.cil"])
        self.assertEqual(run.call_args.args[0][:2], ["git", "ls-files"])

    def test_git_failure_is_exit_2(self) -> None:
        with mock.patch.object(cs, "tracked_files", side_effect=OSError("no git")):
            code, _, err = self.run_main([])
        self.assertEqual(code, 2)
        self.assertIn("git ls-files", err)

    def test_nothing_to_check_is_exit_2(self) -> None:
        with mock.patch.object(cs, "tracked_files", return_value=[]):
            self.assertEqual(self.run_main([])[0], 2)

    def test_only_git_ls_files_is_ever_run(self) -> None:
        with mock.patch.object(cs.subprocess, "run", wraps=cs.subprocess.run) as run:
            cs.tracked_files(ROOT)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:2], ["git", "ls-files"])

    def test_the_repo_has_no_errors(self) -> None:
        code, out, err = self.run_main([])
        self.assertEqual(code, 0, err)
        self.assertIn(" 0 errors", out)


if __name__ == "__main__":
    unittest.main()
