#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_systemd.py (the check of the tracked systemd units).

Run: python3 tools/test_check_systemd.py
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

import check_systemd as cs  # noqa: E402  (after sys.path.insert above)

HARDENED = "NoNewPrivileges=yes\n"
EXEC_KEYS = (
    "ExecStart",
    "ExecStartPre",
    "ExecStartPost",
    "ExecStop",
    "ExecStopPost",
    "ExecReload",
    "ExecCondition",
)


def service(
    extra: str = "",
    install: str = "WantedBy=multi-user.target\n",
    desc: str = "Demo",
    hardened: bool = True,
) -> str:
    """A good service; ``extra`` goes to the end of [Service]."""
    return (
        "# SPDX-License-Identifier: GPL-3.0-or-later\n"
        f"[Unit]\nDescription={desc}\n\n"
        f"[Service]\nExecStart=/usr/bin/demo\n{HARDENED if hardened else ''}{extra}\n"
        f"[Install]\n{install}"
    )


def check(units: dict[str, str], only: tuple[str, ...] = ()) -> list[cs.Problem]:
    """Write the units to a temporary folder and check them all (every key is tracked)."""
    with tempfile.TemporaryDirectory() as tmp:
        for rel, text in units.items():
            path = Path(tmp) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
        return cs.check_files(Path(tmp), list(units), only)


def errors(units: dict[str, str]) -> list[str]:
    return [str(p) for p in check(units) if not p.warning]


def warnings(units: dict[str, str]) -> list[str]:
    return [str(p) for p in check(units) if p.warning]


def one(text: str, name: str = "demo.service") -> list[str]:
    return errors({name: text})


class GoodUnits(unittest.TestCase):
    def test_good_service_passes(self) -> None:
        self.assertEqual(check({"demo.service": service()}), [])

    def test_good_socket_with_its_service(self) -> None:
        socket = "[Unit]\nDescription=S\n[Socket]\nListenStream=/run/demo.sock\n[Install]\n"
        socket += "WantedBy=sockets.target\n"
        self.assertEqual(check({"demo.socket": socket, "demo.service": service("")}), [])

    def test_good_timer_and_path_with_their_service(self) -> None:
        timer = (
            "[Unit]\nDescription=T\n[Timer]\nOnCalendar=daily\n[Install]\nWantedBy=timers.target\n"
        )
        path = "[Unit]\nDescription=P\n[Path]\nPathChanged=/etc\n[Install]\nWantedBy=paths.target\n"
        units = {"a.timer": timer, "a.service": service(), "b.path": path, "b.service": service()}
        self.assertEqual(check(units), [])

    def test_comments_empty_lines_and_spaces_around_equals(self) -> None:
        text = "; semicolon comment\n# hash comment\n\n[Unit]\nDescription = Demo\n"
        text += "[Service]\nExecStart = /usr/bin/demo\nNoNewPrivileges = yes\n"
        self.assertEqual(one(text), [])

    def test_repeated_groups_and_keys_are_fine_for_systemd(self) -> None:
        text = service("Environment=A=1\nEnvironment=B=2\n") + "[Service]\nUser=nobody\n"
        self.assertEqual(one(text), [])


class Syntax(unittest.TestCase):
    def test_line_that_is_nothing_valid(self) -> None:
        found = one(service("this is not a key value line\n"))
        self.assertEqual(len(found), 1)
        self.assertIn("not a comment, empty line, [Group] or Key=Value", found[0])

    def test_bad_key_characters(self) -> None:
        self.assertEqual(len(one(service("Bad Key=1\n9Key=2\n"))), 2)

    def test_key_before_the_first_group(self) -> None:
        found = one("Description=x\n" + service())
        self.assertEqual(found, ["demo.service: line 1: key before the first group"])

    def test_broken_group_header(self) -> None:
        found = one(service("[Service\n"))
        self.assertTrue(any("invalid group header '[Service'" in f for f in found), found)

    def test_message_names_the_path_and_the_line(self) -> None:
        found = one(service("???\n"))
        self.assertTrue(found[0].startswith("demo.service: line 8: "), found)

    def test_continuation_joins_lines(self) -> None:
        text = service("ExecStartPre=/usr/bin/a \\\n   --flag \\\n   --other\n")
        self.assertEqual(one(text), [])

    def test_a_continued_value_is_one_value(self) -> None:
        # The second physical line is not a command start: it is part of ExecStart.
        text = service("ExecStop=/usr/bin/stop \\\n  relative-argument\n")
        self.assertEqual(one(text), [])

    def test_comment_inside_a_continuation(self) -> None:
        text = service("ExecStartPre=/usr/bin/a \\\n# a comment\n  --flag\n")
        self.assertEqual(one(text), [])

    def test_continuation_at_the_end_of_the_file(self) -> None:
        found = one(service() + "Environment=A=\\")
        self.assertEqual(len(found), 1)
        self.assertIn("the file ends after a '\\' continuation", found[0])

    def test_space_after_the_backslash_is_not_a_continuation(self) -> None:
        found = one(service("ExecStartPre=/usr/bin/a \\ \n--flag\n"))
        self.assertEqual(len(found), 1)
        self.assertIn("not a comment, empty line", found[0])

    def test_byte_order_mark_is_one_clear_error(self) -> None:
        found = one("\ufeff" + service())
        self.assertEqual(found, ["demo.service: the file starts with a byte order mark (BOM)"])

    def test_file_that_is_not_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.service").write_bytes(b"[Unit]\nDescription=\xff\xfe\n")
            found = cs.check_files(Path(tmp), ["a.service"])
        self.assertEqual([str(p) for p in found], ["a.service: the file is not valid UTF-8"])

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            found = cs.check_files(Path(tmp), ["a.service"], only=["b.service"])
        self.assertIn("b.service: cannot read the file", str(found[0]))


class Groups(unittest.TestCase):
    def test_group_of_another_unit_type_is_an_error(self) -> None:
        for suffix, group in ((".socket", "Service"), (".timer", "Socket"), (".path", "Timer")):
            text = f"[Unit]\nDescription=x\n[{group}]\nExecStart=/bin/x\n"
            found = errors({f"a{suffix}": text, "a.service": service()})
            self.assertTrue(
                any(f"group [{group}] is not valid in a {suffix} unit" in f for f in found),
                (suffix, found),
            )

    def test_service_group_in_a_socket_is_wrong_and_socket_group_in_a_service_too(self) -> None:
        found = one(service() + "[Socket]\nListenStream=/run/x\n")
        self.assertEqual(
            found, ["demo.service: line 11: group [Socket] is not valid in a .service unit"]
        )

    def test_unknown_group_name(self) -> None:
        found = one(service() + "[Surprise]\nA=b\n")
        self.assertTrue(any("group [Surprise] is not valid" in f for f in found), found)

    def test_group_names_are_case_sensitive(self) -> None:
        found = one(service().replace("[Install]", "[install]"))
        self.assertTrue(any("group [install] is not valid" in f for f in found), found)

    def test_x_groups_are_allowed(self) -> None:
        self.assertEqual(one(service() + "[X-Vilna]\nNote=1\n"), [])

    def test_missing_type_group(self) -> None:
        found = errors({"a.service": "[Unit]\nDescription=x\n"})
        self.assertEqual(found, ["a.service: missing the [Service] group"])
        socket = "[Unit]\nDescription=x\n"
        found = errors({"a.socket": socket, "a.service": service()})
        self.assertEqual(found, ["a.socket: missing the [Socket] group"])

    def test_install_group_is_allowed_in_every_type(self) -> None:
        text = (
            "[Unit]\nDescription=x\n[Timer]\nOnCalendar=daily\n[Install]\nWantedBy=timers.target\n"
        )
        self.assertEqual(errors({"a.timer": text, "a.service": service()}), [])


class Description(unittest.TestCase):
    def test_missing_description(self) -> None:
        text = service().replace("Description=Demo\n", "")
        self.assertEqual(one(text), ["demo.service: [Unit] needs a Description"])

    def test_empty_description(self) -> None:
        self.assertEqual(one(service(desc="")), ["demo.service: [Unit] needs a Description"])

    def test_description_in_another_group_does_not_count(self) -> None:
        text = service().replace("[Unit]\nDescription=Demo\n", "[Unit]\n")
        text += "[X-Other]\nDescription=Demo\n"
        self.assertEqual(one(text), ["demo.service: [Unit] needs a Description"])

    def test_every_unit_type_needs_one(self) -> None:
        timer = "[Timer]\nOnCalendar=daily\n"
        found = errors({"a.timer": timer, "a.service": service()})
        self.assertEqual(found, ["a.timer: [Unit] needs a Description"])


class ExecLines(unittest.TestCase):
    def exec_errors(self, line: str) -> list[str]:
        return one(service(line + "\n"))

    def test_relative_command_is_an_error(self) -> None:
        for key in EXEC_KEYS:
            found = self.exec_errors(f"{key}=demo --flag")
            self.assertEqual(len(found), 1, key)
            self.assertIn(f"{key} must start with an absolute path", found[0])
            self.assertIn("found 'demo'", found[0])

    def test_absolute_command_passes(self) -> None:
        for key in EXEC_KEYS:
            self.assertEqual(self.exec_errors(f"{key}=/usr/bin/demo --flag"), [], key)

    def test_prefixes_are_allowed(self) -> None:
        for prefix in ("-", "@", "+", "!", "!!", ":", "-+", "+-", "@-", "!-"):
            self.assertEqual(self.exec_errors(f"ExecStartPre={prefix}/usr/bin/demo"), [], prefix)

    def test_prefix_does_not_excuse_a_relative_path(self) -> None:
        for prefix in ("-", "@", "+", "!!", ":"):
            self.assertEqual(len(self.exec_errors(f"ExecStartPre={prefix}demo")), 1, prefix)

    def test_space_after_the_prefix_is_an_error(self) -> None:
        # systemd 257: "Empty path in command line, ignoring: - /bin/true".
        found = self.exec_errors("ExecStartPre=- /usr/bin/demo")
        self.assertEqual(found, ["demo.service: line 8: ExecStartPre has a space after its prefix"])

    def test_quoted_command(self) -> None:
        self.assertEqual(self.exec_errors('ExecStartPre="/usr/bin/demo" x'), [])
        self.assertEqual(len(self.exec_errors('ExecStartPre="demo" x')), 1)

    def test_path_specifier_may_start_the_command(self) -> None:
        self.assertEqual(self.exec_errors("ExecStartPre=%h/bin/demo"), [])

    def test_variable_or_other_specifier_is_not_a_path(self) -> None:
        self.assertEqual(len(self.exec_errors("ExecStartPre=$HOME/demo")), 1)
        self.assertEqual(len(self.exec_errors("ExecStartPre=${DEMO}/demo")), 1)
        found = self.exec_errors("ExecStartPre=%n/demo")
        self.assertEqual(len(found), 1)
        self.assertIn("ExecStartPre must start with an absolute path", found[0])

    def test_empty_value_resets_the_list(self) -> None:
        self.assertEqual(self.exec_errors("ExecStartPre="), [])

    def test_only_the_command_is_checked_not_its_arguments(self) -> None:
        self.assertEqual(self.exec_errors("ExecStartPre=/bin/sh -c 'x' relative"), [])

    def test_continued_exec_start_is_judged_by_its_first_word(self) -> None:
        text = service().replace("ExecStart=/usr/bin/demo", "ExecStart=demo \\\n  --flag")
        self.assertEqual(len(one(text)), 1)

    def test_socket_exec_lines_are_checked_too(self) -> None:
        socket = "[Unit]\nDescription=S\n[Socket]\nListenStream=/run/x\nExecStartPost=chmod 600 x\n"
        found = errors({"a.socket": socket, "a.service": service()})
        self.assertEqual(len(found), 1)
        self.assertIn("ExecStartPost must start with an absolute path", found[0])

    def test_line_number_in_the_message(self) -> None:
        found = self.exec_errors("ExecStop=stop-it")
        self.assertTrue(found[0].startswith("demo.service: line 8: ExecStop must start"), found)


class Specifiers(unittest.TestCase):
    TEMPLATE = "pkg/demo@.service"

    def test_template_must_use_i(self) -> None:
        found = errors({self.TEMPLATE: service()})
        self.assertEqual(
            found, [f"{self.TEMPLATE}: a template unit must use %i or %I at least once"]
        )

    def test_template_with_i_or_upper_i_passes(self) -> None:
        for specifier in ("%i", "%I"):
            self.assertEqual(
                errors({self.TEMPLATE: service(f"User=u-{specifier}\n")}), [], specifier
            )

    def test_template_that_only_mentions_i_in_a_comment(self) -> None:
        self.assertEqual(len(errors({self.TEMPLATE: service("# runs for uid %i\n")})), 1)

    def test_escaped_percent_is_not_the_specifier(self) -> None:
        self.assertEqual(len(errors({self.TEMPLATE: service("Environment=X=%%i\n")})), 1)

    def test_other_specifiers_do_not_make_a_template_valid(self) -> None:
        self.assertEqual(len(errors({self.TEMPLATE: service("Environment=X=%n\n")})), 1)

    def test_plain_unit_must_not_use_i(self) -> None:
        for specifier in ("%i", "%I"):
            found = one(service(f"User=u-{specifier}\n"))
            self.assertEqual(len(found), 1, specifier)
            self.assertIn("%i or %I in a unit that is not a template", found[0])
            self.assertIn("line 8:", found[0])

    def test_escaped_percent_is_fine_in_a_plain_unit(self) -> None:
        self.assertEqual(one(service("Environment=X=%%i %%I\n")), [])

    def test_other_specifiers_are_fine_in_a_plain_unit(self) -> None:
        self.assertEqual(one(service("Environment=X=%n %u %C\n")), [])

    def test_instance_unit_may_use_i(self) -> None:
        self.assertEqual(errors({"demo@1000.service": service("User=%i\n")}), [])

    def test_template_socket_is_a_template_too(self) -> None:
        socket = "[Unit]\nDescription=S\n[Socket]\nListenStream=/run/demo/%i.sock\n"
        units = {"demo@.socket": socket, "demo@.service": service("User=%i\n")}
        self.assertEqual(errors(units), [])
        units["demo@.socket"] = socket.replace("%i", "x")
        self.assertEqual(
            errors(units), ["demo@.socket: a template unit must use %i or %I at least once"]
        )


class Partners(unittest.TestCase):
    SOCKET = "[Unit]\nDescription=S\n[Socket]\nListenStream=/run/x\n"
    TIMER = "[Unit]\nDescription=T\n[Timer]\nOnCalendar=daily\n"
    PATH = "[Unit]\nDescription=P\n[Path]\nPathChanged=/etc\n"

    def test_missing_service_next_to_each_type(self) -> None:
        for name, text in (
            ("a.socket", self.SOCKET),
            ("a.timer", self.TIMER),
            ("a.path", self.PATH),
        ):
            found = errors({name: text})
            self.assertEqual(len(found), 1, name)
            self.assertIn("no a.service next to it", found[0])

    def test_service_in_another_folder_is_not_next_to_it(self) -> None:
        found = errors({"one/a.timer": self.TIMER, "two/a.service": service()})
        self.assertEqual(len(found), 1)
        self.assertIn("no a.service next to it", found[0])

    def test_service_with_another_name_is_not_a_partner(self) -> None:
        self.assertEqual(len(errors({"a.timer": self.TIMER, "b.service": service()})), 1)

    def test_template_socket_needs_the_template_service(self) -> None:
        socket = self.SOCKET.replace("/run/x", "/run/%i")
        found = errors({"a@.socket": socket, "a.service": service()})
        self.assertEqual(len(found), 1)
        self.assertIn("no a@.service next to it", found[0])
        self.assertEqual(errors({"a@.socket": socket, "a@.service": service("User=%i\n")}), [])

    def test_instance_socket_may_use_the_template_service(self) -> None:
        units = {"a@1.socket": self.SOCKET, "a@.service": service("User=%i\n")}
        self.assertEqual(errors(units), [])

    def test_explicit_unit_instead_of_the_same_name(self) -> None:
        timer = self.TIMER + "Unit=other.service\n"
        units = {"a.timer": timer, "elsewhere/other.service": service()}
        self.assertEqual(errors(units), [])

    def test_explicit_unit_that_is_not_in_the_repo(self) -> None:
        timer = self.TIMER + "Unit=ghost.service\n"
        found = errors({"a.timer": timer, "a.service": service()})
        self.assertEqual(len(found), 1)
        self.assertIn("Unit=ghost.service is not a unit of the repo", found[0])

    def test_explicit_unit_replaces_the_same_name_rule(self) -> None:
        # Unit= is set, so a.service is not needed.
        timer = self.TIMER + "Unit=other.service\n"
        self.assertEqual(errors({"a.timer": timer, "other.service": service()}), [])

    def test_path_unit_key(self) -> None:
        path = self.PATH + "Unit=ghost.service\n"
        found = errors({"a.path": path, "a.service": service()})
        self.assertIn("Unit=ghost.service is not a unit of the repo", found[0])

    def test_socket_names_its_service_with_service_key(self) -> None:
        socket = self.SOCKET + "Service=other.service\n"
        self.assertEqual(errors({"a.socket": socket, "other.service": service()}), [])
        found = errors({"a.socket": self.SOCKET + "Service=ghost.service\n"})
        self.assertIn("Service=ghost.service is not a unit of the repo", found[0])

    def test_explicit_instance_of_a_template_in_the_repo(self) -> None:
        template = {"job@.service": service("User=%i\n")}
        timer = self.TIMER + "Unit=job@7.service\n"
        self.assertEqual(errors({"a.timer": timer, **template}), [])
        # A template timer may name the instance with %i; its own service is not needed then.
        timer = self.TIMER.replace("daily", "%i") + "Unit=job@%i.service\n"
        self.assertEqual(errors({"a@.timer": timer, **template}), [])
        self.assertEqual(
            len(errors({"a.timer": self.TIMER + "Unit=other@7.service\n", **template})), 1
        )

    def test_service_and_install_rules_do_not_need_a_partner(self) -> None:
        self.assertEqual(errors({"a.service": service()}), [])


class InstallTargets(unittest.TestCase):
    def test_target_passes(self) -> None:
        for target in ("multi-user.target", "graphical-session.target", "anything.target"):
            self.assertEqual(one(service(install=f"WantedBy={target}\n")), [], target)

    def test_unit_of_the_repo_passes(self) -> None:
        units = {"a.service": service(install="WantedBy=b.service\n"), "b.service": service()}
        self.assertEqual(errors(units), [])

    def test_unit_that_is_not_in_the_repo(self) -> None:
        found = one(service(install="WantedBy=ghost.service\n"))
        self.assertEqual(len(found), 1)
        self.assertIn(
            "WantedBy=ghost.service is neither a .target nor a unit of the repo", found[0]
        )

    def test_required_by_is_checked_too(self) -> None:
        found = one(service(install="RequiredBy=ghost.socket\n"))
        self.assertIn("RequiredBy=ghost.socket is neither", found[0])

    def test_every_listed_name_is_checked(self) -> None:
        found = one(service(install="WantedBy=multi-user.target ghost.service other.path\n"))
        self.assertEqual(len(found), 2)

    def test_instance_of_a_template_of_the_repo(self) -> None:
        units = {
            "a@.service": service("User=%i\n", install="WantedBy=b@%i.service b@1000.service\n"),
            "b@.service": service("User=%i\n"),
        }
        self.assertEqual(errors(units), [])

    def test_only_the_install_group_is_checked(self) -> None:
        self.assertEqual(one(service("WantedBy=ghost.service\n")), [])

    def test_line_number_in_the_message(self) -> None:
        found = one(service(install="WantedBy=ghost.service\n"))
        self.assertTrue(found[0].startswith("demo.service: line 10: WantedBy=ghost"), found)

    def test_known_old_problem_is_a_warning(self) -> None:
        units = {"pkg/a.service": service(install="WantedBy=ghost.service\n")}
        with mock.patch.object(cs, "LEGACY", frozenset({("pkg/a.service", "install-target")})):
            found = check(units)
        self.assertEqual([p.warning for p in found], [True])
        self.assertIn("pkg/a.service: warning: line 10: WantedBy=ghost.service", str(found[0]))
        self.assertEqual([p.warning for p in check(units)], [False])


class Hardening(unittest.TestCase):
    def bare(self, extra: str = "") -> dict[str, str]:
        return {"demo.service": service(extra, hardened=False)}

    def test_none_of_the_keys_is_a_warning_not_an_error(self) -> None:
        self.assertEqual(errors(self.bare()), [])
        self.assertEqual(
            warnings(self.bare()),
            [
                "demo.service: warning: none of NoNewPrivileges=yes, ProtectSystem=, "
                "ProtectHome=, PrivateTmp=yes"
            ],
        )

    def test_any_one_key_is_enough(self) -> None:
        for line in (
            "NoNewPrivileges=yes",
            "NoNewPrivileges=true",
            "ProtectSystem=strict",
            "ProtectSystem=full",
            "ProtectHome=read-only",
            "ProtectHome=yes",
            "PrivateTmp=yes",
        ):
            self.assertEqual(warnings(self.bare(line + "\n")), [], line)

    def test_a_key_that_is_switched_off_does_not_count(self) -> None:
        for line in (
            "NoNewPrivileges=no",
            "PrivateTmp=false",
            "ProtectSystem=no",
            "ProtectHome=no",
            "ProtectSystem=",
        ):
            self.assertEqual(len(warnings(self.bare(line + "\n"))), 1, line)

    def test_the_last_assignment_wins(self) -> None:
        self.assertEqual(len(warnings(self.bare("ProtectSystem=strict\nProtectSystem=no\n"))), 1)
        self.assertEqual(warnings(self.bare("ProtectSystem=no\nProtectSystem=strict\n")), [])

    def test_the_key_must_be_in_the_service_group(self) -> None:
        text = service().replace("[Unit]\n", "[Unit]\nProtectSystem=strict\n").replace(HARDENED, "")
        self.assertEqual(len(warnings({"demo.service": text})), 1)

    def test_only_services_get_the_warning(self) -> None:
        socket = "[Unit]\nDescription=S\n[Socket]\nListenStream=/run/x\n"
        self.assertEqual(warnings({"a.socket": socket, "a.service": service()}), [])

    def test_table_lists_every_service(self) -> None:
        units = {
            "a/one.service": service("ProtectSystem=strict\nPrivateTmp=yes\n"),
            "a/two.service": self.bare()["demo.service"],
            "a/one.timer": "[Unit]\nDescription=T\n[Timer]\nOnCalendar=daily\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in units.items():
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_text(text, encoding="utf-8")
            table = cs.hardening_table(Path(tmp), list(units))
        self.assertEqual(
            table.splitlines(),
            [
                "| Unit | NoNewPrivileges | ProtectSystem | ProtectHome | PrivateTmp |",
                "| --- | --- | --- | --- | --- |",
                "| `a/one.service` | `yes` | `strict` | — | `yes` |",
                "| `a/two.service` | — | — | — | — |",
            ],
        )


class Main(unittest.TestCase):
    def run_main(self, units: dict[str, str], args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in units.items():
                path = Path(tmp) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            with (
                mock.patch.object(cs, "ROOT", Path(tmp)),
                mock.patch.object(cs, "tracked_units", return_value=sorted(units)),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = cs.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_exit_0_for_good_units(self) -> None:
        code, out, err = self.run_main({"a.service": service()}, [])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "1 systemd units checked: 0 errors, 0 warnings\n")

    def test_warnings_do_not_fail_the_check(self) -> None:
        code, out, err = self.run_main({"a.service": service(hardened=False)}, [])
        self.assertEqual(code, 0)
        self.assertIn("a.service: warning: none of", err)
        self.assertIn("0 errors, 1 warnings", out)

    def test_exit_1_for_errors(self) -> None:
        code, out, err = self.run_main({"a.service": service(desc="")}, [])
        self.assertEqual(code, 1)
        self.assertIn("a.service: [Unit] needs a Description", err)
        self.assertIn("1 errors, 0 warnings", out)

    def test_table_option_prints_the_table(self) -> None:
        code, out, _ = self.run_main({"a.service": service()}, ["--table"])
        self.assertEqual(code, 0)
        self.assertIn("| `a.service` | `yes` | — | — | — |", out)

    def test_only_the_named_unit_is_checked_but_partners_are_found(self) -> None:
        units = {"a.service": service(desc=""), "b.timer": Partners.TIMER, "b.service": service()}
        self.assertEqual(self.run_main(units, ["b.timer"])[0], 0)
        self.assertEqual(self.run_main(units, ["a.service"])[0], 1)

    def test_exit_2_for_wrong_calls(self) -> None:
        self.assertEqual(self.run_main({"a.service": service()}, ["--bogus"])[0], 2)
        self.assertEqual(self.run_main({"a.service": service()}, ["nope.service"])[0], 2)
        self.assertEqual(self.run_main({}, [])[0], 2)

    def test_help_is_not_an_error(self) -> None:
        self.assertEqual(self.run_main({"a.service": service()}, ["--help"])[0], 0)


class RealRepo(unittest.TestCase):
    def test_tracked_units_have_no_errors(self) -> None:
        tracked = cs.tracked_units(ROOT)
        self.assertTrue(tracked, "the repo has tracked systemd units")
        found = [str(p) for p in cs.check_files(ROOT, tracked) if not p.warning]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
