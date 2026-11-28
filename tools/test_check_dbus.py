#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_dbus.py (the D-Bus policy and activation file checker).

Every test writes small files into a temporary folder and checks them the way ``main()``
does; there is a test for every rule and one for the good files.

Run: python3 tools/test_check_dbus.py
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_dbus as cd  # noqa: E402  (after sys.path.insert above)

DOCTYPE = (
    '<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"\n'
    ' "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">\n'
)
CONF = "etc/dbus-1/system.d/org.vilna.Demo.conf"
SERVICE = "usr/share/dbus-1/system-services/org.vilna.Demo.service"
UNIT = "units/vilna-demo.service"
SYSTEMD_UNIT = "[Unit]\nDescription=Demo\n\n[Service]\nExecStart=/usr/bin/demo\n"


def policy(*rules: str, context: str = 'user="root"', doctype: str = DOCTYPE) -> str:
    body = "\n".join(f"    {r}" for r in rules)
    head = f"<?xml version='1.0'?>\n{doctype}<busconfig>\n"
    return f"{head}  <policy {context}>\n{body}\n  </policy>\n</busconfig>\n"


GOOD_POLICY = policy(
    '<allow own="org.vilna.Demo"/>',
    '<allow send_destination="org.vilna.Demo" send_interface="org.vilna.Demo1"/>',
    '<allow own_prefix="org.vilna.demo"/>',
)


def activation(
    name: str | None = "org.vilna.Demo",
    exec_: str | None = "/bin/false",
    unit: str | None = "vilna-demo.service",
    extra: str = "",
) -> str:
    lines = ["[D-BUS Service]"]
    for key, value in (("Name", name), ("Exec", exec_), ("SystemdService", unit)):
        if value is not None:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n" + extra


class Tree:
    """A temporary folder with files; ``tracked`` is the list that git ls-files would give."""

    def __init__(self, test: unittest.TestCase, files: dict[str, str | bytes]) -> None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for rel, content in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")
        self.tracked = list(files)

    def check(self, paths: list[str] | None = None, explicit: bool = False):
        return cd.check_files(self.root, paths or self.tracked, self.tracked, explicit)


class CheckCase(unittest.TestCase):
    def messages(self, files: dict[str, str | bytes], **kw) -> list[str]:
        problems, _ = Tree(self, files).check(**kw)
        return [str(p) for p in problems]

    def errors(self, files: dict[str, str | bytes], **kw) -> list[str]:
        problems, _ = Tree(self, files).check(**kw)
        return [str(p) for p in problems if not p.warning]

    def assertOneError(self, files: dict[str, str | bytes], needle: str, **kw) -> None:
        errors = self.errors(files, **kw)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(needle, errors[0])


class GoodFilesTest(CheckCase):
    def test_good_policy_activation_and_unit(self) -> None:
        files = {CONF: GOOD_POLICY, SERVICE: activation(), UNIT: SYSTEMD_UNIT}
        problems, count = Tree(self, files).check()
        self.assertEqual(problems, [])
        self.assertEqual(count, 2)

    def test_systemd_unit_alone_is_not_a_dbus_file(self) -> None:
        problems, count = Tree(self, {UNIT: SYSTEMD_UNIT}).check()
        self.assertEqual((problems, count), ([], 0))

    def test_unrelated_conf_is_left_alone(self) -> None:
        problems, count = Tree(self, {"a/50-x.conf": "[Service]\nMemoryMax=1G\n"}).check()
        self.assertEqual((problems, count), ([], 0))

    def test_policy_found_by_content_outside_dbus_dir(self) -> None:
        files = {"conf/demo.conf": policy('<allow own="evil.Name"/>')}
        self.assertOneError(files, "evil.Name")

    def test_activation_found_by_content_outside_dbus_dir(self) -> None:
        files = {"x/demo.service": activation(unit=None), UNIT: SYSTEMD_UNIT}
        self.assertOneError(files, "SystemdService is missing")

    def test_policy_without_doctype_and_with_comments_is_good(self) -> None:
        text = (
            "<!-- a comment -->\n<busconfig>\n<!-- own -->\n"
            '<policy user="root"><allow own="org.vilna.Demo"/></policy>\n</busconfig>\n'
        )
        self.assertEqual(self.errors({CONF: text}), [])

    def test_other_busconfig_elements_are_not_checked(self) -> None:
        text = DOCTYPE + "<busconfig><limit name='max_replies_per_connection'>1</limit></busconfig>"
        self.assertEqual(self.errors({CONF: text}), [])

    def test_deny_rules_are_free(self) -> None:
        text = policy('<deny own="*"/>', '<deny send_destination="*"/>', "<deny/>")
        self.assertEqual(self.errors({CONF: text}), [])

    def test_user_star_in_default_is_good(self) -> None:
        text = policy('<allow user="*"/>', context='context="default"')
        self.assertEqual(self.errors({CONF: text}), [])


class XmlTest(CheckCase):
    def test_not_xml(self) -> None:
        self.assertOneError({CONF: "<busconfig><policy>"}, "not valid XML")

    def test_empty_file(self) -> None:
        self.assertOneError({CONF: ""}, "not valid XML")

    def test_truncated_policy_found_by_content(self) -> None:
        self.assertOneError({"c/x.conf": "<busconfig><policy user='root'>"}, "not valid XML")

    def test_wrong_root(self) -> None:
        self.assertOneError({CONF: "<config/>"}, "root element is <config>")

    def test_wrong_doctype_name(self) -> None:
        text = policy('<allow own="org.vilna.Demo"/>', doctype="<!DOCTYPE other>\n")
        self.assertOneError({CONF: text}, "DOCTYPE names 'other'")

    def test_general_entity_declaration(self) -> None:
        dtd = '<!DOCTYPE busconfig [<!ENTITY n "org.vilna.Demo">]>\n'
        text = policy('<allow own="org.vilna.Demo"/>', doctype=dtd)
        self.assertOneError({CONF: text}, "declares the entity 'n'")

    def test_parameter_entity_declaration(self) -> None:
        dtd = '<!DOCTYPE busconfig [<!ENTITY % p "x">]>\n'
        text = policy('<allow own="org.vilna.Demo"/>', doctype=dtd)
        self.assertOneError({CONF: text}, "declares the entity 'p'")

    def test_external_entity_is_an_error_and_never_read(self) -> None:
        dtd = '<!DOCTYPE busconfig [<!ENTITY x SYSTEM "file:///etc/hostname">]>\n'
        text = policy('<allow own="org.vilna.Demo"/>', doctype=dtd)
        self.assertOneError({CONF: text}, "declares the entity 'x'")

    def test_entity_bomb_stops_at_the_declaration(self) -> None:
        decls = '<!ENTITY a "aaaaaaaaaa">'
        for i in range(10):
            ref = f"&{chr(97 + i)};"
            decls += f'<!ENTITY {chr(98 + i)} "{ref * 4}">'
        text = policy('<allow own="&k;"/>', doctype=f"<!DOCTYPE busconfig [{decls}]>\n")
        self.assertOneError({CONF: text}, "declares the entity")

    def test_entity_declaration_inside_a_comment_is_fine(self) -> None:
        dtd = '<!DOCTYPE busconfig [<!-- <!ENTITY a "b"> -->]>\n'
        text = policy('<allow own="org.vilna.Demo"/>', doctype=dtd)
        self.assertEqual(self.errors({CONF: text}), [])

    def test_duplicate_attribute_is_bad_xml(self) -> None:
        text = policy('<allow own="org.vilna.A" own="org.vilna.B"/>')
        self.assertOneError({CONF: text}, "not valid XML")

    def test_utf8_bom_is_accepted(self) -> None:
        self.assertEqual(self.errors({CONF: b"\xef\xbb\xbf" + GOOD_POLICY.encode()}), [])

    def test_too_big_file(self) -> None:
        big = GOOD_POLICY + "<!--" + "x" * cd.MAX_BYTES + "-->"
        self.assertOneError({CONF: big}, "more than")

    def test_symlink_in_dbus_dir(self) -> None:
        tree = Tree(self, {"real.conf": GOOD_POLICY})
        link = tree.root / CONF
        link.parent.mkdir(parents=True)
        os.symlink(tree.root / "real.conf", link)
        tree.tracked.append(CONF)
        problems, _ = tree.check([CONF])
        self.assertEqual(len(problems), 1)
        self.assertIn("symlink", str(problems[0]))

    def test_missing_file_is_an_error(self) -> None:
        tree = Tree(self, {})
        problems, _ = tree.check([CONF])
        self.assertEqual(len(problems), 1)
        self.assertIn("cannot read", str(problems[0]))


class RuleTest(CheckCase):
    def test_own_foreign_name(self) -> None:
        for name in (
            "com.example.Evil",
            "org.vilna",
            "org.vilna.",
            "org.vilnax.Demo",
            "ORG.VILNA.Demo",
            "org.vilna.a b",
            "org.vilna..a",
            "org.vilna.a.",
            "org.vilna.a\n",
            "org.vilna.Dем",
            " org.vilna.Demo",
            "org.vilna.Demo ",
            "*",
            "org.vilna.*",
        ):
            with self.subTest(name=name):
                text = policy(f'<allow own="{name.replace(chr(10), "&#10;")}"/>')
                errors = self.errors({CONF: text})
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("is not an org.vilna.* name", errors[0])

    def test_own_good_names(self) -> None:
        for name in ("org.vilna.Demo", "org.vilna.mcp.files", "org.vilna.a-b_c.D1"):
            with self.subTest(name=name):
                self.assertEqual(self.errors({CONF: policy(f'<allow own="{name}"/>')}), [])

    def test_own_prefix_rules(self) -> None:
        good = ("org.vilna", "org.vilna.demo", "org.vilna.a.b")
        bad = ("org", "org.vilnax", "org.vilna.", "com.vilna", "*", "")
        for name in good:
            with self.subTest(name=name):
                self.assertEqual(self.errors({CONF: policy(f'<allow own_prefix="{name}"/>')}), [])
        for name in bad:
            with self.subTest(name=name):
                self.assertEqual(
                    len(self.errors({CONF: policy(f'<allow own_prefix="{name}"/>')})), 1
                )

    def test_documented_name_is_accepted(self) -> None:
        text = policy('<allow own="io.example.Documented"/>')
        self.assertEqual(len(self.errors({CONF: text})), 1)
        with mock.patch.object(cd, "DOCUMENTED_NAMES", frozenset({"io.example.Documented"})):
            self.assertEqual(self.errors({CONF: text}), [])

    def test_own_in_default_context_is_a_warning(self) -> None:
        text = policy('<allow own="org.vilna.Demo"/>', context='context="default"')
        messages = self.messages({CONF: text})
        self.assertEqual(len(messages), 1)
        self.assertIn("warning:", messages[0])
        self.assertIn("any user may own it", messages[0])

    def test_own_in_mandatory_or_user_context_is_quiet(self) -> None:
        for context in ('context="mandatory"', 'user="root"', 'group="vilna"'):
            with self.subTest(context=context):
                text = policy('<allow own="org.vilna.Demo"/>', context=context)
                self.assertEqual(self.messages({CONF: text}), [])

    def test_send_destination_star(self) -> None:
        for rule in (
            '<allow send_destination="*"/>',
            '<allow send_destination="*" send_interface="org.vilna.X"/>',
            '<allow send_destination=" * "/>',
        ):
            with self.subTest(rule=rule):
                self.assertOneError({CONF: policy(rule)}, 'send_destination="*"')

    def test_send_destination_named_is_good(self) -> None:
        text = policy(
            '<allow send_destination="org.vilna.Demo"/>', '<allow send_destination="*x"/>'
        )
        self.assertEqual(self.errors({CONF: text}), [])

    def test_allow_without_condition(self) -> None:
        self.assertOneError({CONF: policy("<allow/>")}, "no condition")

    def test_allow_with_only_weak_conditions(self) -> None:
        for attrs in (
            'send_type="method_call"',
            'receive_type="signal"',
            'eavesdrop="true"',
            'send_requested_reply="true" send_type="method_return"',
        ):
            with self.subTest(attrs=attrs):
                self.assertOneError({CONF: policy(f"<allow {attrs}/>")}, "names no peer")

    def test_weak_condition_with_a_peer_is_good(self) -> None:
        text = policy('<allow send_type="method_call" send_destination="org.vilna.Demo"/>')
        self.assertEqual(self.errors({CONF: text}), [])

    def test_empty_attribute(self) -> None:
        for attr in ("send_destination", "send_interface", "own", "receive_sender"):
            with self.subTest(attr=attr):
                self.assertTrue(
                    any("is empty" in e for e in self.errors({CONF: policy(f'<allow {attr}=""/>')}))
                )

    def test_unknown_attribute(self) -> None:
        text = policy('<allow send_destination="org.vilna.Demo" sned_interface="x"/>')
        self.assertOneError({CONF: text}, "unknown attribute 'sned_interface'")

    def test_policy_needs_exactly_one_selector(self) -> None:
        for context in ("", 'user="root" group="x"', 'context="default" user="root"', 'bogus="1"'):
            with self.subTest(context=context):
                text = policy('<allow own="org.vilna.Demo"/>', context=context)
                self.assertOneError({CONF: text}, "exactly one of")

    def test_policy_context_value(self) -> None:
        text = policy('<allow own="org.vilna.Demo"/>', context='context="everyone"')
        self.assertOneError({CONF: text}, "is not default or mandatory")

    def test_policy_child_must_be_allow_or_deny(self) -> None:
        self.assertOneError({CONF: policy("<permit own='org.vilna.Demo'/>")}, "holds <permit>")

    def test_policy_nested_in_other_elements_is_found(self) -> None:
        text = (
            DOCTYPE
            + '<busconfig><include><policy user="root"><allow/></policy></include></busconfig>'
        )
        self.assertOneError({CONF: text}, "no condition")

    def test_each_bad_rule_is_reported_separately_with_its_line(self) -> None:
        text = policy('<allow own="x.y"/>', '<allow send_destination="*"/>', "<allow/>")
        errors = self.errors({CONF: text})
        self.assertEqual(len(errors), 3)
        self.assertEqual(len({e.split("(line ")[1] for e in errors}), 3)
        self.assertTrue(all(e.startswith(f"{CONF}: ") for e in errors))


class ActivationTest(CheckCase):
    def files(self, **kw) -> dict[str, str | bytes]:
        return {SERVICE: activation(**kw), UNIT: SYSTEMD_UNIT}

    def test_missing_systemd_service(self) -> None:
        self.assertOneError(self.files(unit=None), "SystemdService is missing")

    def test_empty_systemd_service(self) -> None:
        self.assertOneError(self.files(unit=""), "SystemdService is missing")

    def test_unit_not_in_repo(self) -> None:
        self.assertOneError(self.files(unit="vilna-other.service"), "no such unit in the repo")

    def test_unit_name_must_be_plain(self) -> None:
        for unit in (
            "../x.service",
            "units/vilna-demo.service",
            "vilna-demo",
            "vilna-demo.timer",
            "vilna demo.service",
            "vilna-demo.service ; x",
            ".service",
            "vilna-demo.service\u200b",
        ):
            with self.subTest(unit=unit):
                self.assertOneError(self.files(unit=unit), "is not a plain *.service name")

    def test_unit_name_longer_than_255_characters(self) -> None:
        unit = "a" * 250 + ".service"
        files = {SERVICE: activation(unit=unit)}
        self.assertOneError(files, "is not a plain *.service name")

    def test_activation_file_is_not_the_unit(self) -> None:
        files = {SERVICE: activation(unit="org.vilna.Demo.service")}
        self.assertOneError(files, "no such unit in the repo")

    def test_unit_in_dbus_dir_is_not_the_unit(self) -> None:
        files = {SERVICE: activation(), "usr/share/dbus-1/vilna-demo.service": SYSTEMD_UNIT}
        self.assertOneError(files, "no such unit in the repo", paths=[SERVICE])

    def test_other_activation_file_with_that_name_is_not_the_unit(self) -> None:
        other = activation(name="org.vilna.Other", unit="org.vilna.Other.service")
        files = {SERVICE: activation(), "x/vilna-demo.service": other}
        self.assertOneError(files, "no such unit in the repo", paths=[SERVICE])

    def test_template_instance(self) -> None:
        files = {
            SERVICE: activation(unit="vilna-demo@1000.service"),
            "u/vilna-demo@.service": SYSTEMD_UNIT,
        }
        self.assertEqual(self.errors(files), [])

    def test_instance_without_template_or_unit(self) -> None:
        files = {SERVICE: activation(unit="vilna-demo@1000.service"), UNIT: SYSTEMD_UNIT}
        self.assertOneError(files, "no such unit in the repo")

    def test_alias_counts(self) -> None:
        unit = SYSTEMD_UNIT + "\n[Install]\nAlias=dbus-org.vilna.Demo.service\n"
        files = {SERVICE: activation(unit="dbus-org.vilna.Demo.service"), UNIT: unit}
        self.assertEqual(self.errors(files), [])

    def test_alias_outside_install_does_not_count(self) -> None:
        unit = SYSTEMD_UNIT + "Alias=dbus-org.vilna.Demo.service\n"
        files = {SERVICE: activation(unit="dbus-org.vilna.Demo.service"), UNIT: unit}
        self.assertOneError(files, "no such unit in the repo")

    def test_foreign_name(self) -> None:
        for name in ("com.example.Evil", "org.vilna", "org.vilna.*", ""):
            with self.subTest(name=name):
                errors = self.errors(self.files(name=name))
                self.assertEqual(len(errors), 1, errors)

    def test_missing_name(self) -> None:
        self.assertOneError(self.files(name=None), "Name is missing")

    def test_exec_missing_or_empty(self) -> None:
        self.assertOneError(self.files(exec_=None), "Exec is missing")
        self.assertOneError(self.files(exec_=""), "Exec is missing")

    def test_file_name_that_differs_from_the_name_is_a_warning(self) -> None:
        files = {"usr/share/dbus-1/system-services/demo.service": activation(), UNIT: SYSTEMD_UNIT}
        messages = self.messages(files)
        self.assertEqual(len(messages), 1)
        self.assertIn("warning: the file name is not org.vilna.Demo.service", messages[0])

    def test_no_group_in_dbus_dir(self) -> None:
        errors = self.errors({SERVICE: "Name=org.vilna.Demo\n"})
        self.assertEqual(
            errors,
            [f"{SERVICE}: line 1: a key before any group", f"{SERVICE}: no [D-BUS Service] group"],
        )

    def test_no_dbus_group_in_dbus_dir(self) -> None:
        errors = self.errors({SERVICE: "[Unit]\nDescription=x\n"})
        self.assertEqual(errors, [f"{SERVICE}: no [D-BUS Service] group"])

    def test_repeated_group_and_key(self) -> None:
        text = activation() + "[D-BUS Service]\nName=org.vilna.Demo\n"
        errors = self.errors({SERVICE: text, UNIT: SYSTEMD_UNIT})
        self.assertTrue(any("group [D-BUS Service] is repeated" in e for e in errors), errors)
        self.assertTrue(any("key Name is repeated" in e for e in errors), errors)

    def test_repeated_key_cannot_hide_a_bad_value(self) -> None:
        text = activation(unit="vilna-demo.service") + "SystemdService=missing.service\n"
        errors = self.errors({SERVICE: text, UNIT: SYSTEMD_UNIT})
        self.assertTrue(any("is repeated" in e for e in errors), errors)
        self.assertTrue(any("missing.service" in e for e in errors), errors)

    def test_garbage_line(self) -> None:
        errors = self.errors({SERVICE: activation() + "this is not a key\n", UNIT: SYSTEMD_UNIT})
        self.assertEqual(len(errors), 1)
        self.assertIn("is not a comment, a group or Key=Value", errors[0])

    def test_comments_crlf_and_spaces_are_fine(self) -> None:
        text = (
            "# c\r\n[D-BUS Service]\r\nName = org.vilna.Demo\r\nExec=/bin/false\r\n"
            "SystemdService = vilna-demo.service\r\n"
        )
        self.assertEqual(self.errors({SERVICE: text, UNIT: SYSTEMD_UNIT}), [])

    def test_nul_byte_and_bom(self) -> None:
        self.assertOneError({SERVICE: activation() + "\x00", UNIT: SYSTEMD_UNIT}, "NUL byte")
        bom = b"\xef\xbb\xbf" + activation().encode()
        self.assertOneError({SERVICE: bom, UNIT: SYSTEMD_UNIT}, "byte order mark")

    def test_activation_symlink(self) -> None:
        tree = Tree(self, {UNIT: SYSTEMD_UNIT, "real.service": activation()})
        link = tree.root / SERVICE
        link.parent.mkdir(parents=True)
        os.symlink(tree.root / "real.service", link)
        tree.tracked.append(SERVICE)
        problems, _ = tree.check([SERVICE])
        self.assertEqual([("symlink" in str(p)) for p in problems], [True])

    def test_symlinked_unit_does_not_leak_its_target(self) -> None:
        tree = Tree(self, {"real.service": SYSTEMD_UNIT, SERVICE: activation()})
        (tree.root / UNIT).parent.mkdir(parents=True)
        os.symlink(tree.root / "real.service", tree.root / UNIT)
        tree.tracked.append(UNIT)
        problems, _ = tree.check([SERVICE])
        names = cd.unit_names(tree.root, tree.tracked)
        self.assertIn("real.service", names)
        self.assertEqual(problems, [])

    def test_untracked_unit_does_not_count(self) -> None:
        tree = Tree(self, {SERVICE: activation(), UNIT: SYSTEMD_UNIT})
        tree.tracked.remove(UNIT)
        problems, _ = tree.check([SERVICE])
        self.assertEqual(len(problems), 1)
        self.assertIn("no such unit in the repo", str(problems[0]))


class LegacyTest(CheckCase):
    def test_legacy_turns_an_error_into_a_warning(self) -> None:
        files = {CONF: policy("<allow/>")}
        with mock.patch.object(cd, "LEGACY", frozenset({(CONF, "unconditional-allow")})):
            problems, _ = Tree(self, files).check()
        self.assertEqual([p.warning for p in problems], [True])
        self.assertTrue(str(problems[0]).startswith(f"{CONF}: warning: "))

    def test_legacy_covers_only_its_rule_and_file(self) -> None:
        files = {
            CONF: policy("<allow/>", '<allow own="x.y"/>'),
            "b/dbus-1/y.conf": policy("<allow/>"),
        }
        with mock.patch.object(cd, "LEGACY", frozenset({(CONF, "unconditional-allow")})):
            problems, _ = Tree(self, files).check()
        self.assertEqual(
            sorted((p.path, p.warning) for p in problems),
            [
                ("b/dbus-1/y.conf", False),
                (CONF, False),
                (CONF, True),
            ],
        )

    def test_the_legacy_table_has_no_stale_entries(self) -> None:
        tracked = set(cd.tracked_files(ROOT))
        for path, _rule in cd.LEGACY:
            self.assertIn(path, tracked)


class OutputTest(CheckCase):
    def test_messages_are_sorted_and_deterministic(self) -> None:
        files = {
            "z/dbus-1/b.conf": policy("<allow/>"),
            "a/dbus-1/b.conf": policy("<allow/>", '<allow own="x.y"/>'),
        }
        first = self.messages(files)
        self.assertEqual(first, self.messages(dict(reversed(list(files.items())))))
        self.assertEqual(
            [m.split(":")[0] for m in first], ["a/dbus-1/b.conf"] * 2 + ["z/dbus-1/b.conf"]
        )

    def test_every_message_is_path_colon_problem(self) -> None:
        files = {CONF: policy("<allow/>", '<allow own="x"/>'), SERVICE: activation(unit=None)}
        for message in self.messages(files):
            self.assertRegex(message, r"^[^:]+: \S")

    def test_problem_str(self) -> None:
        self.assertEqual(str(cd.Problem("a", "b")), "a: b")
        self.assertEqual(str(cd.Problem("a", "b", warning=True)), "a: warning: b")


class MainTest(unittest.TestCase):
    def run_main(self, args: list[str], files: dict[str, str | bytes]) -> tuple[int, str, str]:
        tree = Tree(self, files)
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(cd, "ROOT", tree.root),
            mock.patch.object(cd, "tracked_files", return_value=tree.tracked),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cd.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_good_repo(self) -> None:
        code, out, err = self.run_main(
            [], {CONF: GOOD_POLICY, SERVICE: activation(), UNIT: SYSTEMD_UNIT}
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "2 D-Bus files checked: 0 errors, 0 warnings\n")

    def test_repo_with_no_dbus_files_is_fine(self) -> None:
        code, out, _ = self.run_main([], {UNIT: SYSTEMD_UNIT})
        self.assertEqual((code, out), (0, "0 D-Bus files checked: 0 errors, 0 warnings\n"))

    def test_error_exit_code_and_stream(self) -> None:
        code, out, err = self.run_main([], {CONF: policy("<allow/>")})
        self.assertEqual(code, 1)
        self.assertIn("1 D-Bus files checked: 1 errors, 0 warnings", out)
        self.assertIn(f"{CONF}: <allow> has no condition", err)

    def test_warnings_do_not_fail(self) -> None:
        text = policy('<allow own="org.vilna.Demo"/>', context='context="default"')
        code, out, err = self.run_main([], {CONF: text})
        self.assertEqual(code, 0)
        self.assertIn("0 errors, 1 warnings", out)
        self.assertIn("warning:", err)

    def test_explicit_files(self) -> None:
        files = {CONF: policy("<allow/>"), "etc/dbus-1/system.d/b.conf": GOOD_POLICY}
        code, out, _ = self.run_main(["etc/dbus-1/system.d/b.conf"], files)
        self.assertEqual((code, out), (0, "1 D-Bus files checked: 0 errors, 0 warnings\n"))

    def test_explicit_file_that_is_not_dbus(self) -> None:
        code, out, err = self.run_main([UNIT], {UNIT: SYSTEMD_UNIT})
        self.assertEqual(code, 1)
        self.assertIn("is not a D-Bus activation file", err)
        self.assertIn("1 errors", out)

    def test_explicit_conf_that_is_not_a_policy(self) -> None:
        code, _, err = self.run_main(["a/x.conf"], {"a/x.conf": "[Service]\n"})
        self.assertEqual(code, 1)
        self.assertIn("not valid XML", err)

    def test_wrong_calls_give_2(self) -> None:
        files = {CONF: GOOD_POLICY}
        for args in (
            ["--bogus"],
            ["-x"],
            ["README.md"],
            ["/etc/passwd.conf"],
            ["../x.conf"],
            [CONF + ".missing.conf"],
        ):
            with self.subTest(args=args):
                code, _, err = self.run_main(args, files)
                self.assertEqual(code, 2)
                self.assertNotEqual(err, "")

    def test_parent_folder_in_the_path_is_refused_even_when_the_file_exists(self) -> None:
        tree = Tree(self, {"x.conf": GOOD_POLICY, "sub/keep": ""})
        err = io.StringIO()
        with (
            mock.patch.object(cd, "ROOT", tree.root / "sub"),
            mock.patch.object(cd, "tracked_files", return_value=[]),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(cd.main(["../x.conf"]), 2)
        self.assertIn("must be a .conf or .service path from the repo root", err.getvalue())

    def test_help_is_0(self) -> None:
        code, _, err = self.run_main(["--help"], {})
        self.assertEqual(code, 0)
        self.assertIn("usage:", err)

    def test_git_failure_is_2(self) -> None:
        err = io.StringIO()
        with (
            mock.patch.object(cd, "tracked_files", side_effect=OSError("no git")),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(cd.main([]), 2)
        self.assertIn("cannot list files", err.getvalue())

    def test_tracked_files_runs_only_git_ls_files(self) -> None:
        completed = mock.Mock(stdout=b"a.conf\0b/c.service\0")
        with mock.patch.object(cd.subprocess, "run", return_value=completed) as run:
            self.assertEqual(cd.tracked_files(ROOT), ["a.conf", "b/c.service"])
        self.assertEqual(run.call_args.args[0][:2], ["git", "ls-files"])
        self.assertEqual(run.call_count, 1)


class RealRepoTest(unittest.TestCase):
    def test_real_repo_has_no_errors(self) -> None:
        tracked = cd.tracked_files(ROOT)
        problems, _count = cd.check_files(ROOT, tracked, tracked)
        self.assertEqual([str(p) for p in problems if not p.warning], [])

    def test_main_on_the_real_repo(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cd.main([])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("D-Bus files checked: 0 errors", out.getvalue())


if __name__ == "__main__":
    unittest.main()
