#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_secrets.py (the secrets check).

The fake secrets are put together at run time, so that this file holds nothing the
check itself would report. There is a test for every rule, one for the markers, one for the
masking of values, one for every kind of file that is not read, and one for the repo.

Run: python3 tools/test_check_secrets.py
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_secrets as cs  # noqa: E402  (after sys.path.insert above)

RANDOM = "q8Zr3LmK9xVb2TnW5yHc7DpF4sJa6GeU"  # 32 characters, no marker word inside
OTHER = "Rw4Yp7Nd2Xk9Hb6Tc3Vm8Zs5Lg1Qj0Fe"
GITHUB = "gh" + "p_" + RANDOM + "abcd"
GITHUB_PAT = "github" + "_pat_" + RANDOM + "_" + OTHER
OPENAI = "s" + "k-" + RANDOM + "4821"
OPENAI_PROJ = "s" + "k-proj-" + RANDOM + "4821"
AWS = "AK" + "IA" + "QWERTYUIOP234567"
SLACK = "xo" + "xb-" + "1234567890-" + RANDOM
PEM_HEAD = "-----BEGIN " + "RSA PRIVATE KEY-----"
PEM_FOOT = "-----END " + "RSA PRIVATE KEY-----"
PEM_BODY = "MIIEowIBAAKCAQEA" + RANDOM + "+/ab=="


def scan(text: str, path: str = "a.txt") -> list[cs.Finding]:
    return cs.check_text(path, text)


def rules(text: str, path: str = "a.txt") -> list[str]:
    return [f.rule for f in scan(text, path)]


def assignment(name: str, value: str, quote: str = '"', sep: str = " = ") -> str:
    return f"{name}{sep}{quote}{value}{quote}\n"


def run_main(args: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    old = Path.cwd()
    if cwd is not None:
        os.chdir(cwd)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cs.main(args)
    finally:
        os.chdir(old)
    return code, out.getvalue(), err.getvalue()


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name)

    def write(self, name: str, content: str | bytes) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path


class PrefixRuleTests(unittest.TestCase):
    def test_github_tokens(self) -> None:
        self.assertEqual(rules(f"x {GITHUB} y\n"), ["github-token"])
        self.assertEqual(rules(f"x {GITHUB_PAT}\n"), ["github-token"])
        for kind in ("o", "u", "s", "r"):
            with self.subTest(kind=kind):
                self.assertEqual(rules("gh" + kind + "_" + RANDOM + "\n"), ["github-token"])

    def test_short_or_wrong_github_prefix_is_not_a_token(self) -> None:
        self.assertEqual(rules("gh" + "p_" + "abc\n"), [])
        self.assertEqual(rules("gh" + "x_" + RANDOM + "\n"), [])

    def test_openai_style_keys(self) -> None:
        self.assertEqual(rules(OPENAI + "\n"), ["api-key"])
        self.assertEqual(rules(OPENAI_PROJ + "\n"), ["api-key"])

    def test_sk_inside_a_word_or_a_hyphenated_phrase_is_not_a_key(self) -> None:
        self.assertEqual(rules("task-" + RANDOM + "\n"), [])
        self.assertEqual(rules("risk-" + RANDOM + "\n"), [])
        self.assertEqual(rules("s" + "k-" + "circle-fade-in-out-slowly\n"), [])
        self.assertEqual(rules("s" + "k-" * 5000 + "\n"), [])

    def test_aws_key(self) -> None:
        self.assertEqual(rules(f"id={AWS}\n"), ["aws-key"])
        self.assertEqual(rules("AS" + "IA" + "QWERTYUIOP234567\n"), ["aws-key"])

    def test_aws_lookalikes(self) -> None:
        self.assertEqual(rules("AK" + "IA" + "SHORT\n"), [])
        self.assertEqual(rules("XAK" + "IA" + "QWERTYUIOP234567\n"), [])
        self.assertEqual(rules("AK" + "IA" + "QWERTYUIOP2345678\n"), [])

    def test_slack_token(self) -> None:
        self.assertEqual(rules(SLACK + "\n"), ["slack-token"])
        self.assertEqual(rules("xo" + "xb-1\n"), [])
        self.assertEqual(rules("xo" + "xp-123456789012\n"), ["slack-token"])
        self.assertEqual(rules("xo" + "xp-123456789\n"), [])

    def test_line_numbers_and_order(self) -> None:
        found = scan(f"nothing\n{OPENAI}\n\n{GITHUB}\n")
        self.assertEqual([(f.line, f.rule) for f in found], [(2, "api-key"), (4, "github-token")])

    def test_the_same_token_twice_on_a_line_is_one_finding(self) -> None:
        self.assertEqual(rules(f"{GITHUB} {GITHUB}\n"), ["github-token"])
        self.assertEqual(rules(f"{GITHUB}\n{GITHUB}\n"), ["github-token", "github-token"])

    def test_two_tokens_on_one_line(self) -> None:
        self.assertEqual(rules(f"{GITHUB} {SLACK}\n"), ["github-token", "slack-token"])

    def test_crlf_and_last_line_without_newline(self) -> None:
        self.assertEqual(rules(f"a\r\n{GITHUB}\r\n"), ["github-token"])
        self.assertEqual(rules(GITHUB), ["github-token"])


class PrivateKeyTests(unittest.TestCase):
    def test_header_with_a_body(self) -> None:
        found = scan(f"{PEM_HEAD}\n{PEM_BODY}\n{PEM_FOOT}\n")
        self.assertEqual([(f.line, f.rule) for f in found], [(1, "private-key")])

    def test_kinds_of_header(self) -> None:
        for kind in ("", "RSA ", "EC ", "DSA ", "OPENSSH ", "ENCRYPTED ", "PGP "):
            with self.subTest(kind=kind):
                head = "-----BEGIN " + kind + "PRIVATE KEY-----"
                self.assertEqual(rules(head + "\n" + PEM_BODY + "\n"), ["private-key"])
        self.assertEqual(rules("-----BEGIN " + "PGP PRIVATE KEY BLOCK-----\n"), ["private-key"])

    def test_public_key_and_certificate_are_fine(self) -> None:
        self.assertEqual(rules("-----BEGIN PUBLIC KEY-----\n" + PEM_BODY + "\n"), [])
        self.assertEqual(rules("-----BEGIN CERTIFICATE-----\n" + PEM_BODY + "\n"), [])

    def test_marker_in_the_header_line_or_the_body(self) -> None:
        self.assertEqual(rules(PEM_HEAD + " FAKE\n" + PEM_BODY + "\n"), [])
        self.assertEqual(rules(PEM_HEAD + "\nFAKE" + RANDOM + "\n" + PEM_BODY + "\n"), [])
        self.assertEqual(rules(f"# example\n{PEM_HEAD}\n{PEM_BODY}\n"), ["private-key"])

    def test_marker_after_the_key_does_not_help(self) -> None:
        text = f"{PEM_HEAD}\n{PEM_BODY}\n{PEM_FOOT}\nthis is a test of the parser\n"
        self.assertEqual(rules(text), ["private-key"])

    def test_marker_in_code_below_a_header_does_not_help(self) -> None:
        text = f'KEY = "{PEM_HEAD}"\n\ndef test_it():\n    pass\n'
        self.assertEqual(rules(text), ["private-key"])

    def test_two_different_bodies_have_two_ids(self) -> None:
        first = scan(f"{PEM_HEAD}\n{PEM_BODY}\n")[0]
        second = scan(f"{PEM_HEAD}\n{OTHER}{OTHER}\n")[0]
        self.assertNotEqual(first.ident, second.ident)


class AssignmentTests(unittest.TestCase):
    def test_quoted_values(self) -> None:
        for name in (
            "token",
            "password",
            "api_key",
            "API_KEY",
            "apikey",
            "client_secret",
            "passwd",
        ):
            with self.subTest(name=name):
                self.assertEqual(rules(assignment(name, RANDOM), "a.py"), ["secret-assignment"])

    def test_other_places_for_an_assignment(self) -> None:
        cases = {
            "a.toml": assignment("token", RANDOM),
            "a.json": f'  "password": "{RANDOM}",\n',
            "a.yml": f"password: {RANDOM}\n",
            "a.env": f"API_KEY={RANDOM}\n",
            "a.conf": f"db_password = {RANDOM}\n",
            "a.py": f'    token: str = "{RANDOM}"\n',
            "a.sh": f"export GH_TOKEN='{RANDOM}'\n",
            "a.js": f"const token = '{RANDOM}';\n",
        }
        for path, text in cases.items():
            with self.subTest(path=path):
                self.assertEqual(rules(text, path), ["secret-assignment"])

    def test_unquoted_value_in_source_code_is_a_variable(self) -> None:
        for suffix in (".py", ".rs", ".js", ".ts", ".c", ".go", ".java"):
            with self.subTest(suffix=suffix):
                self.assertEqual(rules(f"token = {RANDOM}\n", "a" + suffix), [])

    def test_not_a_secret(self) -> None:
        for text in (
            'token = os.environ["HOME_TOKEN_NAME_1"]\n',
            'password = "${DATABASE_PASSWORD_VALUE}"\n',
            'password = "<your password here 12>"\n',
            "token = secrets.token_urlsafe(32)\n",
            'token = "short"\n',
            'token == "' + RANDOM + '"\n',
            'token != "' + RANDOM + '"\n',
            'password = "this is a long sentence with words"\n',
            'password = "some_long_snake_case_name"\n',
            'password = "' + "a" * 40 + '"\n',
            'password = "' + "ab12" * 10 + '"\n',
            'password = "/run/secrets/db_password_2024_file"\n',
            'password = "https://example.org/get/token/abcdefghij123"\n',
            "token = None\n",
            "token=" + "=" + RANDOM + "\n",
            "token = https://host.org/" + RANDOM + "\n",
            "token = " + RANDOM + "[0]\n",
            "token = " + RANDOM + "(1)\n",
            'token: Optional[str] = "x"\n',
            "msgid 'Password for {name}'\n",
            'secret = ""\n',
            "password = $PASSWORD_FROM_THE_VAULT\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(rules(text, "a.toml"), [])

    def test_names_that_hold_a_path_or_a_label(self) -> None:
        for name in ("password_file", "token_path", "secret_name", "api_key_env", "token_url"):
            with self.subTest(name=name):
                self.assertEqual(rules(assignment(name, RANDOM), "a.toml"), [])

    def test_other_names_are_not_looked_at(self) -> None:
        self.assertEqual(rules(assignment("checksum", RANDOM), "a.toml"), [])
        self.assertEqual(rules(assignment("name", RANDOM), "a.toml"), [])

    def test_hex_and_base64_values(self) -> None:
        self.assertEqual(
            rules(assignment("token", "9f2c41a8e7b3d05c6a1f8e24b7d90c35"), "a.toml"),
            ["secret-assignment"],
        )
        self.assertEqual(
            rules(assignment("secret", "dGhpcyBpcyBhbiBvcGFxdWUgdmFsdWU9"), "a.toml"),
            ["secret-assignment"],
        )

    def test_short_value_needs_three_kinds_of_characters(self) -> None:
        self.assertEqual(
            rules(assignment("password", "Zx9!qL2#vB7n"), "a.toml"), ["secret-assignment"]
        )
        self.assertEqual(rules(assignment("password", "zx9qlvb7nkrt"), "a.toml"), [])

    def test_value_is_cut_at_the_next_delimiter(self) -> None:
        self.assertEqual(rules(f"token = {RANDOM}  # comment\n", "a.conf"), ["secret-assignment"])
        self.assertEqual(rules(f"f(token={RANDOM})\n", "a.conf"), ["secret-assignment"])

    def test_json_escaped_text_does_not_make_a_value(self) -> None:
        line = '{"text": "max_tokens = 512\\nignore_previous = false"}\n'
        self.assertEqual(rules(line, "a.jsonl"), [])

    def test_prefix_rule_and_assignment_report_a_value_once(self) -> None:
        self.assertEqual(rules(assignment("token", GITHUB), "a.toml"), ["github-token"])
        self.assertEqual(rules(f"OPENAI_API_KEY={OPENAI}\n", "a.env"), ["api-key"])

    def test_two_assignments_on_one_line(self) -> None:
        text = f'a(token="{RANDOM}", password="{OTHER}")\n'
        self.assertEqual(rules(text, "a.py"), ["secret-assignment", "secret-assignment"])


class MarkerTests(unittest.TestCase):
    def test_marker_inside_the_value(self) -> None:
        self.assertEqual(rules("s" + "k-" + "FAKE" + RANDOM + "\n"), [])
        self.assertEqual(rules("s" + "k-" + RANDOM + "-example-4821\n"), [])
        self.assertEqual(rules("gh" + "p_" + RANDOM + "test\n"), [])
        self.assertEqual(rules(assignment("token", "fake" + RANDOM), "a.toml"), [])
        self.assertEqual(rules("AKIAIOSFODNN7" + "EXAMPLE\n"), [])

    def test_marker_as_a_word_on_the_line(self) -> None:
        for note in ("# FAKE", "# example value", "// a test token", "-- Test"):
            with self.subTest(note=note):
                self.assertEqual(rules(f"{OPENAI} {note}\n"), [])
                self.assertEqual(
                    rules(assignment("token", RANDOM).strip() + f" {note}\n", "a.toml"), []
                )

    def test_marker_must_start_a_word(self) -> None:
        for note in ("# latest", "# contest", "# attested", "# defake"):
            with self.subTest(note=note):
                self.assertEqual(rules(f"{OPENAI} {note}\n"), ["api-key"])

    def test_marker_on_another_line_does_not_help(self) -> None:
        self.assertEqual(rules(f"# fake\n{OPENAI}\n# test\n"), ["api-key"])


class MaskingTests(TempDirCase):
    SECRETS = (GITHUB, GITHUB_PAT, OPENAI, OPENAI_PROJ, AWS, SLACK, RANDOM, PEM_BODY)

    def test_no_value_is_repeated(self) -> None:
        text = (
            f"{GITHUB}\n{GITHUB_PAT}\n{OPENAI}\n{OPENAI_PROJ}\n{AWS}\n{SLACK}\n"
            f"{PEM_HEAD}\n{PEM_BODY}\n{PEM_FOOT}\n" + assignment("password", OTHER) + "\n"
        )
        findings = scan(text, "a.toml")
        self.assertEqual(len(findings), 8)
        for finding in findings:
            shown = str(finding)
            for secret in self.SECRETS + (OTHER,):
                for part in (secret, secret[8:], secret[-12:]):
                    self.assertNotIn(part, shown)

    def test_main_prints_no_value(self) -> None:
        path = self.write(
            "x.toml", f"{GITHUB}\n{OPENAI}\n" + assignment("password", OTHER) + f"{SLACK}\n"
        )
        code, out, err = run_main([path.name], self.dir)
        self.assertEqual(code, 1)
        for secret in (GITHUB, OPENAI, OTHER, SLACK, RANDOM):
            self.assertNotIn(secret, out + err)
            self.assertNotIn(secret[6:], out + err)
        self.assertIn("x.toml: line 1: github-token: gh" + "p_...", err)
        self.assertIn("4 errors", out)

    def test_message_shape(self) -> None:
        text = str(scan(GITHUB + "\n", "dir/a.txt")[0])
        self.assertRegex(
            text,
            r"^dir/a\.txt: line 1: github-token: gh"
            r"p_\.\.\. \(\d+ characters, id [0-9a-f]{8}\)$",
        )

    def test_id_is_stable_and_differs_between_values(self) -> None:
        one = scan(OPENAI + "\n")[0].ident
        self.assertEqual(one, scan("a " + OPENAI + "\n")[0].ident)
        self.assertNotEqual(one, scan(OPENAI_PROJ + "\n")[0].ident)


class LegacyTests(TempDirCase):
    def test_listed_finding_is_a_warning(self) -> None:
        found = scan(OPENAI + "\n", "old.txt")
        table = {("old.txt", "api-key", found[0].ident): "reason"}
        with mock.patch.object(cs, "LEGACY", table):
            result = cs.apply_legacy(found, whole_repo=True)
        self.assertEqual([f.warning for f in result], [True])
        self.assertIn("old.txt: warning: line 1: api-key", str(result[0]))

    def test_a_new_value_in_a_listed_file_is_an_error(self) -> None:
        old = scan(OPENAI + "\n", "old.txt")[0]
        found = scan(OPENAI + "\n" + OPENAI_PROJ + "\n", "old.txt")
        with mock.patch.object(cs, "LEGACY", {("old.txt", "api-key", old.ident): "r"}):
            result = cs.apply_legacy(found, whole_repo=True)
        self.assertEqual([f.warning for f in result], [True, False])

    def test_the_same_value_in_another_file_is_an_error(self) -> None:
        old = scan(OPENAI + "\n", "old.txt")[0]
        found = scan(OPENAI + "\n", "new.txt")
        with mock.patch.object(cs, "LEGACY", {("old.txt", "api-key", old.ident): "r"}):
            result = cs.apply_legacy(found, whole_repo=False)
        self.assertEqual([f.warning for f in result], [False])

    def test_the_same_value_under_another_rule_is_an_error(self) -> None:
        found = scan(OPENAI + "\n", "old.txt")
        with mock.patch.object(cs, "LEGACY", {("old.txt", "github-token", found[0].ident): "r"}):
            result = cs.apply_legacy(found, whole_repo=False)
        self.assertEqual([f.warning for f in result], [False])

    def test_an_entry_that_matches_nothing_is_a_warning_for_the_whole_repo_only(self) -> None:
        table = {("gone.txt", "api-key", "00000000"): "r"}
        with mock.patch.object(cs, "LEGACY", table):
            whole = cs.apply_legacy([], whole_repo=True)
            some = cs.apply_legacy([], whole_repo=False)
        self.assertEqual(len(whole), 1)
        self.assertTrue(whole[0].warning)
        self.assertIn(
            "gone.txt: warning: LEGACY entry api-key id 00000000 matches nothing", str(whole[0])
        )
        self.assertEqual(some, [])

    def test_the_real_table_has_no_secret_values(self) -> None:
        for key, reason in cs.LEGACY.items():
            self.assertEqual(len(key), 3)
            self.assertRegex(key[2], r"^[0-9a-f]{8}$")
            self.assertTrue(reason)
            self.assertIn(key[1], cs.RULES)


class FileReadingTests(TempDirCase):
    def find(self, name: str) -> tuple[list[cs.Finding], dict[str, int]]:
        return cs.check_files(self.dir, [name])

    def test_text_file(self) -> None:
        self.write("a.txt", GITHUB + "\n")
        found, stats = self.find("a.txt")
        self.assertEqual([f.rule for f in found], ["github-token"])
        self.assertEqual(stats["scanned"], 1)

    def test_binary_file_is_skipped(self) -> None:
        self.write("a.bin", b"\x00\x01" + GITHUB.encode())
        found, stats = self.find("a.bin")
        self.assertEqual((found, stats["binary"], stats["scanned"]), ([], 1, 0))

    def test_nul_after_the_first_8_kib_is_still_text(self) -> None:
        self.write("a.txt", GITHUB.encode() + b"\n" + b"a" * 9000 + b"\x00")
        found, _ = self.find("a.txt")
        self.assertEqual([f.rule for f in found], ["github-token"])

    def test_utf16_with_a_byte_order_mark_is_read(self) -> None:
        self.write("a.txt", (GITHUB + "\n").encode("utf-16"))
        found, stats = self.find("a.txt")
        self.assertEqual([f.rule for f in found], ["github-token"])
        self.assertEqual(stats["binary"], 0)

    def test_bytes_that_are_not_utf8(self) -> None:
        self.write("a.txt", b"\xfa\xfb caf\xe9 " + GITHUB.encode() + b"\n\x80\x81\n")
        found, _ = self.find("a.txt")
        self.assertEqual([f.rule for f in found], ["github-token"])

    def test_symlink_is_not_followed(self) -> None:
        target = self.write("outside/secret.txt", GITHUB + "\n")
        link = self.dir / "link.txt"
        link.symlink_to(target)
        found, stats = self.find("link.txt")
        self.assertEqual((found, stats["links"]), ([], 1))

    def test_dangling_symlink_and_directory_do_not_fail(self) -> None:
        (self.dir / "dangling").symlink_to(self.dir / "nowhere")
        (self.dir / "folder").mkdir()
        for name in ("dangling", "folder"):
            with self.subTest(name=name):
                found, stats = self.find(name)
                self.assertEqual((found, stats["links"]), ([], 1))

    def test_fifo_is_not_opened(self) -> None:
        os.mkfifo(self.dir / "pipe")
        found, stats = self.find("pipe")
        self.assertEqual((found, stats["links"]), ([], 1))

    def test_missing_file_is_an_error(self) -> None:
        found, _ = self.find("gone.txt")
        self.assertEqual([(f.warning, f.rule) for f in found], [(False, "unreadable")])
        self.assertIn("gone.txt: cannot read", str(found[0]))

    def test_large_file_is_a_warning(self) -> None:
        self.write("big.txt", GITHUB + "\n" + "a" * 200)
        with mock.patch.object(cs, "MAX_BYTES", 100):
            found, stats = self.find("big.txt")
        self.assertEqual([(f.warning, f.rule) for f in found], [(True, "too-large")])
        self.assertEqual(stats["large"], 1)
        self.assertIn("not scanned", str(found[0]))

    def test_empty_file(self) -> None:
        self.write("a.txt", "")
        self.assertEqual(self.find("a.txt")[0], [])


class MainTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.object(cs, "LEGACY", {})
        patch.start()
        self.addCleanup(patch.stop)

    def make_repo(self, files: dict[str, str]) -> Path:
        for name, content in files.items():
            self.write(name, content)
        git = ["git", "-C", str(self.dir)]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        return self.dir

    def test_clean_repo(self) -> None:
        repo = self.make_repo({"a.txt": "hello\n", "b/c.py": "x = 1\n"})
        with mock.patch.object(cs, "ROOT", repo):
            code, out, err = run_main([])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("2 files (2 scanned", out)
        self.assertIn("0 errors, 0 warnings", out)

    def test_only_tracked_files_are_checked(self) -> None:
        repo = self.make_repo({"a.txt": "hello\n"})
        self.write("untracked.txt", GITHUB + "\n")
        with mock.patch.object(cs, "ROOT", repo):
            code, out, _ = run_main([])
        self.assertEqual(code, 0)
        self.assertIn("1 files", out)

    def test_finding_in_a_tracked_file_fails(self) -> None:
        repo = self.make_repo({"sub/a.txt": GITHUB + "\n"})
        with mock.patch.object(cs, "ROOT", repo):
            code, out, err = run_main([])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("sub/a.txt: line 1: github-token: "))
        self.assertIn("1 errors, 0 warnings", out)

    def test_warnings_do_not_fail(self) -> None:
        repo = self.make_repo({"a.txt": OPENAI + "\n"})
        ident = scan(OPENAI + "\n")[0].ident
        with (
            mock.patch.object(cs, "ROOT", repo),
            mock.patch.object(cs, "LEGACY", {("a.txt", "api-key", ident): "r"}),
        ):
            code, out, err = run_main([])
        self.assertEqual(code, 0)
        self.assertIn("a.txt: warning: line 1: api-key", err)
        self.assertIn("0 errors, 1 warnings", out)

    def test_not_a_git_repo_is_a_wrong_call(self) -> None:
        with mock.patch.object(cs, "ROOT", self.dir):
            code, _, err = run_main([])
        self.assertEqual(code, 2)
        self.assertIn("cannot list files with git ls-files", err)

    def test_files_on_the_command_line(self) -> None:
        self.write("one.txt", GITHUB + "\n")
        self.write("two.txt", "fine\n")
        code, out, err = run_main(["one.txt", "two.txt"], self.dir)
        self.assertEqual(code, 1)
        self.assertIn("one.txt: line 1", err)
        self.assertIn("2 files", out)
        self.assertEqual(run_main(["two.txt"], self.dir)[0], 0)

    def test_command_line_does_not_report_stale_legacy_entries(self) -> None:
        self.write("two.txt", "fine\n")
        with mock.patch.object(cs, "LEGACY", {("gone.txt", "api-key", "00000000"): "r"}):
            code, out, err = run_main(["two.txt"], self.dir)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("0 warnings", out)

    def test_wrong_calls(self) -> None:
        self.assertEqual(run_main(["--bogus"])[0], 2)
        self.assertEqual(run_main(["no-such-file-here.txt"], self.dir)[0], 2)
        code, _, err = run_main(["-h"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", err)

    def test_output_is_the_same_every_time(self) -> None:
        repo = self.make_repo({"a.txt": GITHUB + "\n" + OPENAI + "\n", "b.txt": SLACK + "\n"})
        with mock.patch.object(cs, "ROOT", repo):
            first = run_main([])
            second = run_main([])
        self.assertEqual(first, second)

    def test_runs_only_git_ls_files(self) -> None:
        repo = self.make_repo({"a.txt": "hello\n"})
        real_run = subprocess.run
        calls: list[list[str]] = []

        def spy(cmd: list[str], *args: object, **kwargs: object) -> object:
            calls.append(list(cmd))
            return real_run(cmd, *args, **kwargs)  # type: ignore[call-overload]

        with mock.patch.object(cs, "ROOT", repo), mock.patch.object(cs.subprocess, "run", spy):
            run_main([])
        self.assertEqual(calls, [["git", "ls-files", "-z"]])


class HostileInputTests(unittest.TestCase):
    def timed(self, text: str, limit: float = 15.0) -> list[cs.Finding]:
        start = time.monotonic()
        found = scan(text, "a.toml")
        self.assertLess(time.monotonic() - start, limit)
        return found

    def test_one_huge_line(self) -> None:
        text = "a" * 2_000_000 + " " + GITHUB + "\n"
        self.assertEqual([f.rule for f in self.timed(text)], ["github-token"])

    def test_long_runs_that_almost_match(self) -> None:
        for text in (
            "token" * 200_000 + "\n",
            "token=" * 100_000 + "\n",
            "token = " + "a" * 1_000_000 + "\n",
            "password: " + " " * 1_000_000 + "\n",
            "gh" + "p_" * 300_000 + "\n",
            "s" + "k-" + "a-" * 500_000 + "\n",
            "-" * 1_000_000 + "\n",
            "token: " + "[" * 100_000 + "\n",
            "x:" * 200_000 + "\n",
            '"' * 500_000 + "\n",
            "-----BEGIN " * 100_000 + "\n",
            ("-----BEGIN " + "PRIVATE KEY-----\n") * 20_000,
        ):
            with self.subTest(start=text[:12]):
                self.timed(text)

    def test_many_lines(self) -> None:
        text = ("x = 1\n" * 200_000) + GITHUB + "\n"
        found = self.timed(text)
        self.assertEqual([(f.line, f.rule) for f in found], [(200_001, "github-token")])

    def test_every_cut_of_a_text_is_handled(self) -> None:
        text = (
            f"{GITHUB} {AWS} {SLACK}\n{PEM_HEAD}\n{PEM_BODY}\n{PEM_FOOT}\n"
            + assignment("token", RANDOM)
            + f'  "password": "{OTHER}",\n'
        )
        for end in range(len(text) + 1):
            scan(text[:end], "a.json")

    def test_odd_characters(self) -> None:
        for text in (
            "",
            "\n",
            "\x00",
            "\ufeff" + GITHUB,
            "ключ = «" + RANDOM + "»\n",
            "token\u2028= " + RANDOM + "\n",
            "\t" + GITHUB + "\t\n",
            "\r" + GITHUB + "\r",
            "\U0001f600" * 1000,
        ):
            with self.subTest(text=text[:10]):
                scan(text, "a.toml")

    def test_a_value_that_is_too_long_is_not_an_assignment(self) -> None:
        self.assertEqual(rules(assignment("token", RANDOM * 40), "a.toml"), [])


class RepoTests(unittest.TestCase):
    def test_the_repo_has_no_new_secret(self) -> None:
        paths = cs.tracked_files(ROOT)
        found, _ = cs.check_files(ROOT, paths)
        result = cs.apply_legacy(found, whole_repo=True)
        errors = [str(f) for f in result if not f.warning]
        self.assertEqual(errors, [])

    def test_every_legacy_entry_is_still_needed(self) -> None:
        paths = cs.tracked_files(ROOT)
        found, _ = cs.check_files(ROOT, paths)
        result = cs.apply_legacy(found, whole_repo=True)
        stale = [str(f) for f in result if f.note]
        self.assertEqual(stale, [])

    def test_this_file_and_the_check_hold_no_secret(self) -> None:
        for name in ("check_secrets.py", "test_check_secrets.py"):
            with self.subTest(name=name):
                text = (ROOT / "tools" / name).read_text(encoding="utf-8")
                self.assertEqual(scan(text, "tools/" + name), [])

    def test_the_check_prints_one_summary_line(self) -> None:
        code, out, _ = run_main([])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertRegex(
            out, r"^\d+ files \(\d+ scanned, \d+ binary, \d+ links or special, \d+ too large\)"
        )


if __name__ == "__main__":
    unittest.main()
