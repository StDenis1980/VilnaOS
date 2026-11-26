#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_polkit.py (the polkit .policy and .rules checker).

Every test writes small files into a temporary folder and checks them the way
``main()`` does; there is a test for every rule and one for the good files.

Run: python3 tools/test_check_polkit.py
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_polkit as cp  # noqa: E402  (after sys.path.insert above)

ACTION = "org.vilna.demo.change"
POLICY_FILE = "org.vilna.demo.policy"
RULES_FILE = "50-demo.rules"


def translated(tag: str, english: str, skip: tuple[str, ...] = ()) -> str:
    lines = [f"<{tag}>{english}</{tag}>"]
    lines += [
        f'<{tag} xml:lang="{lang}">{english} {lang}</{tag}>'
        for lang in cp.LANGS
        if lang not in skip
    ]
    return "\n    ".join(lines)


def action(
    action_id: str = ACTION,
    *,
    description: str | None = None,
    message: str | None = None,
    defaults: str | None = None,
    extra: str = "",
) -> str:
    """One good <action>; the arguments replace its parts."""
    if description is None:
        description = translated("description", "Change things")
    if message is None:
        message = translated("message", "Authentication is required")
    if defaults is None:
        defaults = (
            "<allow_any>no</allow_any><allow_inactive>no</allow_inactive>"
            "<allow_active>auth_admin_keep</allow_active>"
        )
    defaults_xml = f"<defaults>{defaults}</defaults>" if defaults else ""
    id_attr = f' id="{action_id}"' if action_id else ""
    return (
        f"  <action{id_attr}>\n    {description}\n    {message}\n    {defaults_xml}\n"
        f"    {extra}\n  </action>\n"
    )


def policy(*actions: str, root: str = "policyconfig") -> str:
    body = "".join(actions) if actions else action()
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- SPDX-License-Identifier: GPL-3.0-or-later -->\n"
        f"<{root}>\n  <vendor>Vilna</vendor>\n{body}</{root}>\n"
    )


GOOD_RULES = f"""// SPDX-License-Identifier: GPL-3.0-or-later
polkit.addRule(function (action, subject) {{
    if (action.id === "{ACTION}" && subject.isInGroup("wheel")) {{
        return polkit.Result.YES;
    }}
    return polkit.Result.NOT_HANDLED;
}});
"""


def run_check(files: dict[str, str | bytes]) -> list[cp.Problem]:
    """Write the files to a temporary folder and check them the way main() does."""
    with tempfile.TemporaryDirectory() as tmp:
        for name, content in files.items():
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            (Path(tmp) / name).write_bytes(data)
        return cp.check_files(Path(tmp), list(files))


def errors_of(files: dict[str, str | bytes]) -> list[str]:
    return [str(p) for p in run_check(files) if not p.warning]


def warnings_of(files: dict[str, str | bytes]) -> list[str]:
    return [str(p) for p in run_check(files) if p.warning]


def rules_errors(text: str) -> list[str]:
    return errors_of({POLICY_FILE: policy(), RULES_FILE: text})


def rules_warnings(text: str) -> list[str]:
    return warnings_of({POLICY_FILE: policy(), RULES_FILE: text})


def rule(body: str) -> str:
    """A rule file around ``body``, which is a statement list inside the rule function."""
    return f"polkit.addRule(function (action, subject) {{\n{body}\n}});\n"


class GoodFiles(unittest.TestCase):
    def test_good_policy_and_rules_have_no_problems(self) -> None:
        self.assertEqual(run_check({POLICY_FILE: policy(), RULES_FILE: GOOD_RULES}), [])

    def test_two_actions_in_one_file(self) -> None:
        text = policy(action("org.vilna.a"), action("org.vilna.b"))
        self.assertEqual(run_check({POLICY_FILE: text}), [])

    def test_every_allowed_default_value_is_accepted(self) -> None:
        for value in cp.AUTH_VALUES:
            with self.subTest(value=value):
                defaults = (
                    f"<allow_any>{value if value != 'yes' else 'no'}</allow_any>"
                    f"<allow_inactive>{value}</allow_inactive><allow_active>{value}</allow_active>"
                )
                text = policy(action(defaults=defaults))
                self.assertEqual(errors_of({POLICY_FILE: text}), [])

    def test_messages_start_with_the_path(self) -> None:
        text = policy(action(defaults=""))
        self.assertEqual(
            errors_of({POLICY_FILE: text}), [f"{POLICY_FILE}: action {ACTION}: missing <defaults>"]
        )


class PolicyStructure(unittest.TestCase):
    def test_not_well_formed_xml(self) -> None:
        errors = errors_of({POLICY_FILE: "<policyconfig><action></policyconfig>"})
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith(f"{POLICY_FILE}: not well-formed XML"))

    def test_root_must_be_policyconfig(self) -> None:
        self.assertEqual(
            errors_of({POLICY_FILE: policy(root="policy")}),
            [f"{POLICY_FILE}: the root element is <policy>, not <policyconfig>"],
        )

    def test_id_must_start_with_org_vilna(self) -> None:
        text = policy(action("com.example.thing"))
        self.assertEqual(
            errors_of({POLICY_FILE: text}),
            [f"{POLICY_FILE}: action com.example.thing: the id must start with org.vilna."],
        )

    def test_id_must_have_the_dot_after_vilna(self) -> None:
        text = policy(action("org.vilnaos.thing"))
        self.assertEqual(len(errors_of({POLICY_FILE: text})), 1)

    def test_known_old_id_is_only_a_warning(self) -> None:
        legacy = next(iter(cp.LEGACY_IDS))
        text = policy(action(legacy))
        self.assertEqual(errors_of({POLICY_FILE: text}), [])
        self.assertIn(
            f"{POLICY_FILE}: warning: action {legacy}: the id must start with org.vilna.",
            warnings_of({POLICY_FILE: text}),
        )

    def test_action_without_id(self) -> None:
        text = policy(action(""))
        self.assertEqual(errors_of({POLICY_FILE: text}), [f"{POLICY_FILE}: an <action> has no id"])

    def test_duplicate_id_in_two_files(self) -> None:
        files = {"a.policy": policy(), "b.policy": policy()}
        self.assertEqual(
            errors_of(files), [f"b.policy: action {ACTION}: id already used in a.policy"]
        )

    def test_duplicate_id_in_one_file(self) -> None:
        text = policy(action(), action())
        self.assertEqual(
            errors_of({POLICY_FILE: text}),
            [f"{POLICY_FILE}: action {ACTION}: id already used in {POLICY_FILE}"],
        )


class PolicyTexts(unittest.TestCase):
    def test_missing_english_description_and_message(self) -> None:
        description = '<description xml:lang="uk">Змінити</description>'
        message = '<message xml:lang="uk">Потрібне підтвердження</message>'
        errors = errors_of({POLICY_FILE: policy(action(description=description, message=message))})
        self.assertEqual(
            errors,
            [
                f"{POLICY_FILE}: action {ACTION}: missing <description> in English",
                f"{POLICY_FILE}: action {ACTION}: missing <message> in English",
            ],
        )

    def test_description_missing_altogether(self) -> None:
        text = policy(action(description=" "))
        self.assertEqual(
            errors_of({POLICY_FILE: text}),
            [f"{POLICY_FILE}: action {ACTION}: missing <description> in English"],
        )

    def test_empty_english_text(self) -> None:
        text = policy(action(message="<message> </message>"))
        self.assertEqual(
            errors_of({POLICY_FILE: text}),
            [f"{POLICY_FILE}: action {ACTION}: the English <message> is empty"],
        )


class PolicyDefaults(unittest.TestCase):
    def test_missing_defaults(self) -> None:
        text = policy(action(defaults=""))
        self.assertEqual(
            errors_of({POLICY_FILE: text}), [f"{POLICY_FILE}: action {ACTION}: missing <defaults>"]
        )

    def test_each_missing_key_is_reported(self) -> None:
        full = {
            "allow_any": "no",
            "allow_inactive": "no",
            "allow_active": "auth_admin",
        }
        for key in full:
            with self.subTest(key=key):
                defaults = "".join(f"<{k}>{v}</{k}>" for k, v in full.items() if k != key)
                errors = errors_of({POLICY_FILE: policy(action(defaults=defaults))})
                self.assertEqual(
                    errors, [f"{POLICY_FILE}: action {ACTION}: <defaults> has no <{key}>"]
                )

    def test_value_outside_the_list(self) -> None:
        for bad in ("maybe", "auth_root", "YES", ""):
            with self.subTest(value=bad):
                defaults = (
                    f"<allow_any>no</allow_any><allow_inactive>{bad}</allow_inactive>"
                    "<allow_active>no</allow_active>"
                )
                errors = errors_of({POLICY_FILE: policy(action(defaults=defaults))})
                self.assertEqual(len(errors), 1)
                self.assertIn(f"<allow_inactive> is '{bad}', not one of no yes", errors[0])

    def test_allow_any_yes_is_a_warning(self) -> None:
        defaults = (
            "<allow_any>yes</allow_any><allow_inactive>no</allow_inactive>"
            "<allow_active>yes</allow_active>"
        )
        files = {POLICY_FILE: policy(action(defaults=defaults))}
        self.assertEqual(errors_of(files), [])
        self.assertIn(
            f"{POLICY_FILE}: warning: action {ACTION}: allow_any is yes: anybody may do it, "
            "with no session at all",
            warnings_of(files),
        )

    def test_allow_active_yes_alone_is_fine(self) -> None:
        defaults = (
            "<allow_any>no</allow_any><allow_inactive>no</allow_inactive>"
            "<allow_active>yes</allow_active>"
        )
        files = {POLICY_FILE: policy(action(defaults=defaults))}
        self.assertEqual(run_check(files), [])


class PolicyTranslations(unittest.TestCase):
    def test_unknown_language_code(self) -> None:
        description = translated("description", "Change things") + (
            '\n    <description xml:lang="ru">Изменить</description>'
        )
        errors = errors_of({POLICY_FILE: policy(action(description=description))})
        self.assertEqual(
            errors, [f"{POLICY_FILE}: action {ACTION}: <description> has unknown xml:lang='ru'"]
        )

    def test_english_code_is_unknown(self) -> None:
        message = translated("message", "Ask") + '\n    <message xml:lang="en">Ask</message>'
        errors = errors_of({POLICY_FILE: policy(action(message=message))})
        self.assertEqual(
            errors, [f"{POLICY_FILE}: action {ACTION}: <message> has unknown xml:lang='en'"]
        )

    def test_unknown_language_on_another_element(self) -> None:
        text = policy(action(extra='<vendor xml:lang="xx">V</vendor>'))
        self.assertEqual(
            errors_of({POLICY_FILE: text}),
            [f"{POLICY_FILE}: action {ACTION}: <vendor> has unknown xml:lang='xx'"],
        )

    def test_missing_languages_are_a_warning_with_the_list(self) -> None:
        description = translated("description", "Change things", skip=("eu", "pt"))
        files = {POLICY_FILE: policy(action(description=description))}
        self.assertEqual(errors_of(files), [])
        self.assertEqual(
            warnings_of(files),
            [
                f"{POLICY_FILE}: warning: action {ACTION}: <description> is not translated "
                "into: eu pt"
            ],
        )

    def test_message_is_checked_on_its_own(self) -> None:
        message = translated("message", "Ask", skip=("ca@valencia",))
        files = {POLICY_FILE: policy(action(message=message))}
        self.assertEqual(
            warnings_of(files),
            [
                f"{POLICY_FILE}: warning: action {ACTION}: <message> is not translated into: "
                "ca@valencia"
            ],
        )

    def test_no_translation_at_all_lists_every_language(self) -> None:
        files = {POLICY_FILE: policy(action(description="<description>D</description>"))}
        (warning,) = warnings_of(files)
        self.assertTrue(warning.endswith("is not translated into: " + " ".join(cp.LANGS)))

    def test_each_action_has_its_own_translations(self) -> None:
        bad = action("org.vilna.b", message=translated("message", "Ask", skip=("uk",)))
        files = {POLICY_FILE: policy(action("org.vilna.a"), bad)}
        self.assertEqual(
            warnings_of(files),
            [f"{POLICY_FILE}: warning: action org.vilna.b: <message> is not translated into: uk"],
        )


class RulesActionNames(unittest.TestCase):
    def test_known_action_in_every_comparison_form(self) -> None:
        for expression in (
            f'action.id === "{ACTION}"',
            f'action.id == "{ACTION}"',
            f'action.id !== "{ACTION}"',
            f'action.id != "{ACTION}"',
            f'"{ACTION}" === action.id',
            f"action.id === '{ACTION}'",
        ):
            with self.subTest(expression=expression):
                text = rule(f"if ({expression} && subject.user === 'x') return polkit.Result.NO;")
                self.assertEqual(rules_errors(text), [])

    def test_unknown_action_is_an_error_with_its_line(self) -> None:
        text = rule('if (action.id === "org.vilna.nope.change") {\n return polkit.Result.NO;\n}')
        self.assertEqual(
            rules_errors(text),
            [
                f"{RULES_FILE}: line 2: the rule names the action 'org.vilna.nope.change', "
                "but no .policy defines it"
            ],
        )

    def test_unknown_action_in_each_comparison_form(self) -> None:
        for expression in (
            'action.id == "org.vilna.x"',
            'action.id !== "org.vilna.x"',
            '"org.vilna.x" != action.id',
        ):
            with self.subTest(expression=expression):
                self.assertEqual(len(rules_errors(rule(f"if ({expression}) {{ }}"))), 1)

    def test_case_label_of_switch_on_action_id(self) -> None:
        text = rule(
            f'switch (action.id) {{\ncase "{ACTION}":\ncase "org.vilna.gone":\n'
            "    return polkit.Result.NO;\n}"
        )
        self.assertEqual(
            rules_errors(text),
            [
                f"{RULES_FILE}: line 4: the rule names the action 'org.vilna.gone', "
                "but no .policy defines it"
            ],
        )

    def test_other_actions_are_not_checked(self) -> None:
        text = rule('if (action.id === "org.freedesktop.systemd1.manage-units") { }')
        self.assertEqual(rules_errors(text), [])

    def test_action_of_a_policy_in_another_file_counts(self) -> None:
        files = {
            "a.policy": policy(action("org.vilna.x")),
            RULES_FILE: rule('if (action.id === "org.vilna.x") { }'),
        }
        self.assertEqual(errors_of(files), [])

    def test_no_policy_at_all_means_every_vilna_action_is_unknown(self) -> None:
        errors = errors_of({RULES_FILE: rule(f'if (action.id === "{ACTION}") {{ }}')})
        self.assertEqual(len(errors), 1)

    def test_vilna_string_outside_a_comparison_is_a_warning(self) -> None:
        text = rule(f'if (action.id.indexOf("{ACTION}") === 0) {{ }}')
        self.assertEqual(rules_errors(text), [])
        self.assertEqual(
            rules_warnings(text),
            [
                f"{RULES_FILE}: warning: line 2: '{ACTION}' is not compared with action.id, so "
                "it is not checked against the .policy files"
            ],
        )

    def test_action_names_in_comments_are_ignored(self) -> None:
        text = (
            '// action.id === "org.vilna.in.comment"\n'
            '/* case "org.vilna.in.block": */\n' + GOOD_RULES
        )
        self.assertEqual(run_check({POLICY_FILE: policy(), RULES_FILE: text}), [])


class RulesBrackets(unittest.TestCase):
    def test_brackets_inside_strings_are_ignored(self) -> None:
        text = rule('var a = "(" + \')\' + "[{";\nvar b = "}]";')
        self.assertEqual(rules_errors(text), [])

    def test_escaped_quote_does_not_end_a_string(self) -> None:
        text = rule('var a = "say \\") (";')
        self.assertEqual(rules_errors(text), [])

    def test_brackets_inside_comments_are_ignored(self) -> None:
        text = "// ( [ {\n/* ) ] }\n   ( */\n" + rule("var a = 1;")
        self.assertEqual(rules_errors(text), [])

    def test_regex_literals_are_skipped(self) -> None:
        for regex in (
            r"/^gloxi-[0-9]+$/",
            r"/\(/",
            r"/[(]{2}/",
            r"/[/]/",
            r"/^runtime\/([A-Za-z0-9_.-]+)\.(Locale|Debug)\/[^\/]+$/",
            r"/a\/b/gi",
        ):
            with self.subTest(regex=regex):
                text = rule(f"var ok = {regex}.test(subject.user);")
                self.assertEqual(rules_errors(text), [])

    def test_regex_after_return_and_open_paren(self) -> None:
        text = rule("if (/^x\\(/.test(a)) {\n return /\\)/.exec(b);\n}")
        self.assertEqual(rules_errors(text), [])

    def test_division_is_not_a_regex(self) -> None:
        text = rule("var a = (4 + 2) / 3;\nvar b = a / 2 / 1;\nvar c = [1][0] / 2;")
        self.assertEqual(rules_errors(text), [])

    def test_template_literals(self) -> None:
        text = rule("var a = `(${ [1, 2].length }) ] {` + `${ `${ (1) }` }`;")
        self.assertEqual(rules_errors(text), [])

    def test_template_with_escaped_backtick(self) -> None:
        text = rule("var a = `a \\` ( b`;")
        self.assertEqual(rules_errors(text), [])

    def test_bracket_inside_a_template_expression_is_counted(self) -> None:
        errors = rules_errors(rule("var a = `x ${ (1 } y`;"))
        self.assertTrue(errors)
        self.assertTrue(all(RULES_FILE in e for e in errors))

    def test_missing_closing_paren(self) -> None:
        text = "polkit.addRule(function (action, subject) {\n return 1;\n};\n"
        self.assertEqual(rules_errors(text), [f"{RULES_FILE}: line 1: '(' is never closed"])

    def test_missing_closing_brace_is_reported_at_the_opening_line(self) -> None:
        errors = rules_errors("function f() {\n  if (a) {\n    return 1;\n}\n")
        self.assertEqual(errors, [f"{RULES_FILE}: line 1: '{{' is never closed"])

    def test_unexpected_closing_bracket(self) -> None:
        errors = rules_errors("var a = 1;\n});\n")
        self.assertEqual(
            errors,
            [
                f"{RULES_FILE}: line 2: unexpected '}}' with nothing open",
                f"{RULES_FILE}: line 2: unexpected ')' with nothing open",
            ],
        )

    def test_mismatched_brackets(self) -> None:
        errors = rules_errors("var a = [1, 2);\n")
        self.assertEqual(
            errors[0], f"{RULES_FILE}: line 1: ')' does not match '[' opened on line 1"
        )

    def test_unterminated_string(self) -> None:
        errors = rules_errors('var a = "oops;\nvar b = 1;\n')
        self.assertEqual(errors, [f'{RULES_FILE}: line 1: string "... is not closed'])

    def test_unterminated_block_comment(self) -> None:
        errors = rules_errors("var a = 1;\n/* never closed\n")
        self.assertEqual(errors, [f"{RULES_FILE}: line 2: comment /* is never closed"])

    def test_unterminated_template(self) -> None:
        errors = rules_errors("var a = `never closed;\n")
        self.assertEqual(errors, [f"{RULES_FILE}: line 1: template literal ` is never closed"])

    def test_unterminated_regex(self) -> None:
        errors = rules_errors("var a = /abc;\n")
        self.assertEqual(errors, [f"{RULES_FILE}: line 1: regular expression /... is not closed"])

    def test_broken_syntax_gives_no_second_wave_of_warnings(self) -> None:
        text = rule("return polkit.Result.YES;") + "}"
        self.assertEqual(len(rules_errors(text)), 1)
        self.assertEqual(rules_warnings(text), [])


class RulesYesHeuristic(unittest.TestCase):
    def test_unconditional_yes_is_a_warning(self) -> None:
        text = rule("return polkit.Result.YES;")
        self.assertEqual(rules_errors(text), [])
        self.assertEqual(
            rules_warnings(text),
            [
                f"{RULES_FILE}: warning: line 2: return polkit.Result.YES without a check of "
                "subject.user, subject.isInGroup or subject.groups before it"
            ],
        )

    def test_yes_after_a_check_of_the_user_or_group(self) -> None:
        for condition in (
            'subject.user === "herkos"',
            'subject.isInGroup("wheel")',
            'subject.active && subject.local && subject.isInGroup("wheel")',
            'subject.groups.indexOf("wheel") >= 0',
        ):
            with self.subTest(condition=condition):
                text = rule(f"if ({condition}) {{\n return polkit.Result.YES;\n}}")
                self.assertEqual(rules_warnings(text), [])

    def test_check_of_something_else_is_not_enough(self) -> None:
        text = rule("if (subject.active && subject.local) {\n return polkit.Result.YES;\n}")
        self.assertEqual(len(rules_warnings(text)), 1)

    def test_action_check_alone_is_not_enough(self) -> None:
        text = rule(f'if (action.id === "{ACTION}") {{\n return polkit.Result.YES;\n}}')
        self.assertEqual(len(rules_warnings(text)), 1)

    def test_yes_inside_a_nested_if_of_a_checked_block(self) -> None:
        text = rule(
            'if (subject.user === "herkos") {\n if (action.lookup("verb") === "start") {\n'
            "  return polkit.Result.YES;\n }\n}"
        )
        self.assertEqual(rules_warnings(text), [])

    def test_guard_clause_before_the_yes(self) -> None:
        text = rule(
            'if (subject.user !== "herkos") {\n return polkit.Result.NOT_HANDLED;\n}\n'
            "if (action.lookup('x') === 'y') {\n return polkit.Result.YES;\n}"
        )
        self.assertEqual(rules_warnings(text), [])

    def test_guard_clause_with_a_single_statement(self) -> None:
        text = rule(
            'if (subject.user !== "herkos") return polkit.Result.NOT_HANDLED;\n'
            "return polkit.Result.YES;"
        )
        self.assertEqual(rules_warnings(text), [])

    def test_guard_clause_inside_a_switch(self) -> None:
        text = rule(
            'if (subject.user !== "x") {\n return polkit.Result.NOT_HANDLED;\n}\n'
            'switch (action.lookup("k")) {\ncase "a":\n return polkit.Result.YES;\n}'
        )
        self.assertEqual(rules_warnings(text), [])

    def test_a_check_that_does_not_return_is_not_a_guard(self) -> None:
        text = rule('if (subject.user === "x") {\n var a = 1;\n}\nreturn polkit.Result.YES;')
        self.assertEqual(len(rules_warnings(text)), 1)

    def test_guard_in_an_else_branch(self) -> None:
        text = rule(
            'if (subject.user !== "x") {\n return polkit.Result.NO;\n} else {\n'
            " return polkit.Result.YES;\n}"
        )
        self.assertEqual(rules_warnings(text), [])

    def test_guard_after_the_yes_does_not_count(self) -> None:
        text = rule('return polkit.Result.YES;\nif (subject.user === "x") { return 1; }')
        self.assertEqual(len(rules_warnings(text)), 1)

    def test_other_results_never_warn(self) -> None:
        text = rule("return polkit.Result.NO;\nreturn polkit.Result.AUTH_ADMIN;")
        self.assertEqual(rules_warnings(text), [])

    def test_every_yes_is_judged_on_its_own(self) -> None:
        text = rule(
            'if (subject.isInGroup("a")) {\n return polkit.Result.YES;\n}\n'
            "return polkit.Result.YES;"
        )
        (warning,) = rules_warnings(text)
        self.assertIn("line 5:", warning)

    def test_yes_in_a_comment_or_string_is_ignored(self) -> None:
        text = rule('// return polkit.Result.YES;\nvar a = "return polkit.Result.YES;";')
        self.assertEqual(rules_warnings(text), [])


class FilesAndMain(unittest.TestCase):
    def test_bom_is_one_clear_error_and_the_rest_is_still_checked(self) -> None:
        for name, text in ((POLICY_FILE, policy()), (RULES_FILE, GOOD_RULES)):
            with self.subTest(name=name):
                files = {POLICY_FILE: policy().encode(), name: b"\xef\xbb\xbf" + text.encode()}
                self.assertEqual(
                    errors_of(files), [f"{name}: the file starts with a byte order mark (BOM)"]
                )

    def test_bom_does_not_hide_a_real_error(self) -> None:
        text = b"\xef\xbb\xbf" + policy(root="policy").encode()
        errors = errors_of({POLICY_FILE: text})
        self.assertEqual(len(errors), 2)
        self.assertIn("not <policyconfig>", errors[1])

    def test_file_that_is_not_utf8(self) -> None:
        self.assertEqual(
            errors_of({RULES_FILE: b"var a = '\xff';"}),
            [f"{RULES_FILE}: the file is not valid UTF-8"],
        )

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            problems = cp.check_files(Path(tmp), ["nope.rules"])
        self.assertEqual(len(problems), 1)
        self.assertTrue(str(problems[0]).startswith("nope.rules: cannot read the file"))

    def test_crlf_line_endings_keep_the_line_numbers(self) -> None:
        text = GOOD_RULES.replace("\n", "\r\n") + "var a = (;\r\n"
        self.assertEqual(rules_errors(text), [f"{RULES_FILE}: line 8: '(' is never closed"])

    def run_main(self, args: list[str], cwd: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        previous = Path.cwd()
        os.chdir(cwd)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cp.main(args)
        finally:
            os.chdir(previous)
        return code, out.getvalue(), err.getvalue()

    def test_main_exit_codes_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / POLICY_FILE).write_text(policy(), encoding="utf-8")
            (Path(tmp) / RULES_FILE).write_text(GOOD_RULES, encoding="utf-8")
            code, out, err = self.run_main([POLICY_FILE, RULES_FILE], Path(tmp))
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(out, "2 polkit files checked: 0 errors, 0 warnings\n")

            (Path(tmp) / RULES_FILE).write_text("var a = (;", encoding="utf-8")
            code, out, err = self.run_main([POLICY_FILE, RULES_FILE], Path(tmp))
            self.assertEqual(code, 1)
            self.assertEqual(err, f"{RULES_FILE}: line 1: '(' is never closed\n")
            self.assertIn("1 errors, 0 warnings", out)

    def test_main_warnings_do_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / RULES_FILE).write_text(rule("return polkit.Result.YES;"), encoding="utf-8")
            code, out, err = self.run_main([RULES_FILE], Path(tmp))
        self.assertEqual(code, 0)
        self.assertIn(f"{RULES_FILE}: warning: line 2: return polkit.Result.YES", err)
        self.assertIn("0 errors, 1 warnings", out)

    def test_main_wrong_call_is_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for args in (["--bogus"], ["file.txt"], [RULES_FILE, "x.xml"]):
                with self.subTest(args=args):
                    code, _, err = self.run_main(args, Path(tmp))
                    self.assertEqual(code, 2)
                    self.assertIn("usage:", err)

    def test_main_help_is_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_main(["--help"], Path(tmp))[0], 0)


class TrackedFiles(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_only_tracked_polkit_files_are_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "sub").mkdir()
            tracked = [f"sub/{POLICY_FILE}", RULES_FILE, "readme.txt", "x.policy.bak"]
            for rel in [*tracked, "untracked.rules"]:
                (root / rel).write_text("", encoding="utf-8")
            subprocess.run(["git", "add", *tracked], cwd=root, check=True)
            self.assertEqual(cp.tracked_polkit_files(root), [RULES_FILE, f"sub/{POLICY_FILE}"])


class NeverCrashes(unittest.TestCase):
    """A broken file is a problem to report, never an exception."""

    def test_every_truncation_of_a_rules_file(self) -> None:
        text = GOOD_RULES + "var r = /a[/]b/g, t = `x${ y }z`, s = 'q\\'';\n"
        for end in range(len(text) + 1):
            cp.check_rules_text(RULES_FILE, text[:end], {ACTION})

    def test_every_truncation_of_a_policy_file(self) -> None:
        text = policy()
        for end in range(0, len(text), 7):
            cp.check_policy_text(POLICY_FILE, text[:end])

    def test_odd_policy_shapes(self) -> None:
        for text in (
            "",
            "<policyconfig/>",
            "<policyconfig><action/></policyconfig>",
            '<policyconfig><action id="org.vilna.x"><defaults/></action></policyconfig>',
            '<policyconfig><action id="org.vilna.x"><description/><message/></action>'
            "</policyconfig>",
        ):
            with self.subTest(text=text):
                cp.check_policy_text(POLICY_FILE, text)

    def test_odd_rules_shapes(self) -> None:
        for text in (
            "",
            "\n",
            "}",
            "return polkit.Result.YES",
            "if (",
            "case",
            "switch",
            "/",
            "`${",
            "${",
        ):
            with self.subTest(text=text):
                cp.check_rules_text(RULES_FILE, text, set())


if __name__ == "__main__":
    unittest.main()
