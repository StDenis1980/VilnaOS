#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_shell.py (the check of the tracked shell scripts).

Run: python3 tools/test_check_shell.py
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

import check_shell as cs  # noqa: E402  (after sys.path.insert above)

SH_HEAD = "#!/bin/sh\n# SPDX-License-Identifier: GPL-3.0-or-later\n# Demo.\nset -eu\n"
BASH_HEAD = "#!/bin/bash\n# SPDX-License-Identifier: GPL-3.0-or-later\n# Demo.\nset -euo pipefail\n"


def sh(body: str) -> str:
    """A good ``#!/bin/sh`` script with ``body`` after the header."""
    return SH_HEAD + body + "\n"


def bash(body: str) -> str:
    return BASH_HEAD + body + "\n"


def found(text: str, path: str = "a.sh") -> list[tuple[str, bool]]:
    """(rule, is a warning) of every problem of the script, in order."""
    return [(p.rule, p.warning) for p in cs.check_script(path, text)]


def errors(text: str, path: str = "a.sh") -> list[str]:
    return [p.rule for p in cs.check_script(path, text) if not p.warning]


def warnings(text: str, path: str = "a.sh") -> list[str]:
    return [p.rule for p in cs.check_script(path, text) if p.warning]


class GoodScripts(unittest.TestCase):
    def test_a_good_sh_script_has_no_problems(self) -> None:
        text = sh('here=$(cd "$(dirname "$0")" && pwd)\nif [ -z "$here" ]; then\n\texit 1\nfi')
        self.assertEqual(cs.check_script("a.sh", text), [])

    def test_a_good_bash_script_has_no_problems(self) -> None:
        text = bash('files=(a b)\nfor f in "${files[@]}"; do\n\techo "$f"\ndone\n[[ -n $f ]]')
        self.assertEqual(cs.check_script("a.sh", text), [])

    def test_env_bash_shebang_is_fine(self) -> None:
        text = (
            "#!/usr/bin/env bash\n# SPDX-License-Identifier: GPL-3.0-or-later\nset -euo pipefail\n"
        )
        self.assertEqual(cs.check_script("a.sh", text), [])

    def test_a_script_in_the_style_of_the_site_scripts_passes(self) -> None:
        text = sh(
            "repo=https://example.org/x.git\n"
            'if [ ! -d "$src/.git" ]; then\n'
            '\tgit clone --quiet "$repo" "$src"\n'
            "fi\n"
            'rev=$(git -C "$src" rev-parse HEAD)\n'
            'if [ "$(cat "$www/.revision" 2>/dev/null || true)" = "$rev" ]; then\n'
            "\texit 0\n"
            "fi\n"
            "ls -1dt \"$www\"/*/ 2>/dev/null | grep -v '/cur/$' | tail -n +3 | xargs -r rm -rf\n"
        )
        self.assertEqual(cs.check_script("a.sh", text), [])


class Header(unittest.TestCase):
    def test_no_shebang(self) -> None:
        text = "# SPDX-License-Identifier: GPL-3.0-or-later\nset -eu\n"
        self.assertEqual(errors(text), ["shebang"])

    def test_other_shebangs_are_errors(self) -> None:
        for line in (
            "#!/usr/bin/python3",
            "#!/bin/dash",
            "#! /bin/sh",
            "#!/bin/sh -e",
            "#!/bin/sh ",
        ):
            with self.subTest(line=line):
                text = line + "\n# SPDX-License-Identifier: GPL-3.0-or-later\nset -eu\n"
                self.assertEqual(errors(text), ["shebang"])

    def test_shebang_message_shows_the_line_and_the_choices(self) -> None:
        text = "#!/bin/dash\n# SPDX-License-Identifier: GPL-3.0-or-later\n"
        message = str(cs.check_script("a.sh", text)[0])
        self.assertIn("'#!/bin/dash'", message)
        self.assertIn("#!/usr/bin/env bash", message)
        self.assertIn("line 1", message)

    def test_byte_order_mark_before_the_shebang(self) -> None:
        text = "\ufeff" + SH_HEAD
        self.assertIn("shebang", errors(text))

    def test_empty_file(self) -> None:
        self.assertEqual(errors(""), ["shebang", "spdx"])

    def test_missing_spdx(self) -> None:
        text = "#!/bin/sh\n# Demo.\nset -eu\n"
        self.assertEqual(errors(text), ["spdx"])

    def test_spdx_after_the_fifth_line_does_not_count(self) -> None:
        text = "#!/bin/sh\n#\n#\n#\n#\n# SPDX-License-Identifier: GPL-3.0-or-later\nset -eu\n"
        self.assertEqual(errors(text), ["spdx"])

    def test_spdx_on_the_fifth_line_counts(self) -> None:
        text = "#!/bin/sh\n#\n#\n#\n# SPDX-License-Identifier: GPL-3.0-or-later\nset -eu\n"
        self.assertEqual(errors(text), [])

    def test_spdx_must_be_in_a_comment_and_name_a_licence(self) -> None:
        self.assertEqual(
            errors("#!/bin/sh\necho SPDX-License-Identifier: MIT\nset -eu\n"), ["spdx"]
        )
        self.assertEqual(errors("#!/bin/sh\n# SPDX-License-Identifier:\nset -eu\n"), ["spdx"])

    def test_crlf_line_endings(self) -> None:
        text = SH_HEAD.replace("\n", "\r\n") + "echo hi\r\n"
        self.assertEqual(errors(text), ["crlf"])


class StrictMode(unittest.TestCase):
    def test_sh_without_strict_mode(self) -> None:
        text = "#!/bin/sh\n# SPDX-License-Identifier: GPL-3.0-or-later\necho hi\n"
        problems = cs.check_script("a.sh", text)
        self.assertEqual([p.rule for p in problems], ["strict-mode"])
        self.assertIn("expected 'set -eu'", problems[0].message)

    def test_bash_wants_pipefail_too(self) -> None:
        text = BASH_HEAD.replace("set -euo pipefail", "set -eu")
        problems = cs.check_script("a.sh", text)
        self.assertEqual([p.rule for p in problems], ["strict-mode"])
        self.assertIn("missing -o pipefail", problems[0].message)

    def test_sh_does_not_need_pipefail(self) -> None:
        self.assertEqual(errors(sh("echo hi")), [])

    def test_a_missing_letter_is_named(self) -> None:
        for command, lack in (("set -e", "-u"), ("set -u", "-e"), ("set -x", "-e, -u")):
            with self.subTest(command=command):
                text = SH_HEAD.replace("set -eu", command)
                self.assertIn(f"missing {lack}", cs.check_script("a.sh", text)[0].message)

    def test_the_letters_may_be_split_reordered_or_spelled_out(self) -> None:
        for command in ("set -ue", "set -e -u", "set -eux", "set -o errexit -o nounset"):
            with self.subTest(command=command):
                self.assertEqual(errors(SH_HEAD.replace("set -eu", command)), [])
        for command in ("set -euo pipefail", "set -e -u -o pipefail", "set -euxo pipefail"):
            with self.subTest(command=command):
                self.assertEqual(errors(BASH_HEAD.replace("set -euo pipefail", command)), [])

    def test_bash_with_set_euo_pipefail_in_sh_is_fine(self) -> None:
        self.assertEqual(errors(SH_HEAD.replace("set -eu", "set -euo pipefail")), [])

    def test_set_dashdash_and_set_plus_are_not_strict_mode(self) -> None:
        for command in ("set -- a b", "set +eu", "set"):
            with self.subTest(command=command):
                text = SH_HEAD.replace("set -eu", command)
                self.assertEqual(errors(text), ["strict-mode"])

    def test_strict_mode_in_a_comment_or_a_string_does_not_count(self) -> None:
        text = SH_HEAD.replace("set -eu", "# set -eu\necho 'set -eu'")
        self.assertEqual(errors(text), ["strict-mode"])

    def test_the_twentieth_code_line_counts_the_twenty_first_does_not(self) -> None:
        head = "#!/bin/sh\n# SPDX-License-Identifier: GPL-3.0-or-later\n"
        filler = "echo x\n" * 19
        self.assertEqual(errors(head + filler + "set -eu\n"), [])
        self.assertEqual(errors(head + filler + "echo y\nset -eu\n"), ["strict-mode"])

    def test_comments_empty_lines_and_heredocs_are_not_counted(self) -> None:
        head = "#!/bin/sh\n# SPDX-License-Identifier: GPL-3.0-or-later\n"
        padding = "# a comment\n\n" * 30 + "cat <<EOF\n" + "text\n" * 30 + "EOF\n"
        self.assertEqual(errors(head + padding + "set -eu\n"), [])

    def test_the_lines_of_a_long_command_are_all_counted(self) -> None:
        head = "#!/bin/sh\n# SPDX-License-Identifier: GPL-3.0-or-later\n"
        long_command = "echo a\\\n" + "b\\\n" * 25 + "c\n"
        self.assertEqual(errors(head + long_command + "set -eu\n"), ["strict-mode"])

    def test_kiwi_scripts_only_get_a_warning(self) -> None:
        text = "#!/bin/bash\n# SPDX-License-Identifier: GPL-3.0-or-later\necho hi\n"
        for path in ("obs/images/Vilna/config.sh", "obs/images/x/disk.sh"):
            with self.subTest(path=path):
                self.assertEqual(found(text, path), [("strict-mode", True)])

    def test_the_same_names_elsewhere_and_other_kiwi_files_are_errors(self) -> None:
        text = "#!/bin/bash\n# SPDX-License-Identifier: GPL-3.0-or-later\necho hi\n"
        for path in ("site/config.sh", "obs/images/Vilna/helper.sh", "obs/packages/x/disk.sh"):
            with self.subTest(path=path):
                self.assertEqual(found(text, path), [("strict-mode", False)])

    def test_other_rules_still_apply_to_kiwi_scripts(self) -> None:
        text = bash("curl -s https://example.org/x | sh")
        self.assertEqual(errors(text, "obs/images/Vilna/config.sh"), ["curl-pipe"])


class CurlPipe(unittest.TestCase):
    def test_download_piped_into_a_shell(self) -> None:
        for command in (
            "curl -sSf https://example.org/x | sh",
            "curl -sSf https://example.org/x | sh -s -- -y",
            "wget -qO- https://example.org/x | bash",
            "curl https://example.org/x | sudo bash -s",
            "curl https://example.org/x | tee /tmp/x | sh",
            "curl -s https://example.org/x |\n  sh",
            "curl -s https://example.org/x \\\n  | sh",
            "x=$(curl -s https://example.org/x | sh)",
            "if true; then curl https://example.org/x | /bin/sh; fi",
        ):
            with self.subTest(command=command):
                self.assertEqual(errors(sh(command)), ["curl-pipe"])

    def test_a_download_fed_to_a_shell_in_another_way(self) -> None:
        for command in (
            "bash <(curl -s https://example.org/x)",
            'sh -c "$(curl -fsSL https://example.org/x)"',
            'sh -c "`wget -qO- https://example.org/x`"',
            ". <(curl -s https://example.org/x)",
        ):
            with self.subTest(command=command):
                self.assertIn("curl-pipe", errors(bash(command)))

    def test_the_line_of_the_download_is_reported(self) -> None:
        text = sh("echo a\ncurl -s https://example.org/x | sh")
        self.assertIn("line 6:", str(cs.check_script("a.sh", text)[0]))

    def test_harmless_uses_of_curl_and_wget(self) -> None:
        for command in (
            "curl -o /tmp/x https://example.org/x\nsh /tmp/x",
            "curl -s https://example.org/x | grep sh",
            "curl -s https://example.org/x | sh -c 'cat >/dev/null'",
            "curl -s https://example.org/x || sh /tmp/fallback",
            "curl -s https://example.org/x && sh /tmp/x",
            "echo 'curl https://example.org/x | sh'",
            "# curl https://example.org/x | sh",
            "cat <<EOF\ncurl https://example.org/x | sh\nEOF",
            "sh -c 'echo hi'",
            'sh -c "$(printf hi)"',
            "ls | sh",
        ):
            with self.subTest(command=command):
                self.assertEqual(errors(sh(command)), [])


class Eval(unittest.TestCase):
    def test_eval_with_a_variable(self) -> None:
        for command in (
            'eval "$cmd"',
            "eval $cmd",
            'eval "run $1"',
            'eval "$(ssh-agent -s)"',
            "eval ${x}",
        ):
            with self.subTest(command=command):
                self.assertEqual(errors(sh(command)), ["eval"])

    def test_eval_inside_another_command_is_found(self) -> None:
        self.assertEqual(errors(sh('x=$(eval "$cmd")')), ["eval"])
        self.assertEqual(errors(sh('if true; then eval "$cmd"; fi')), ["eval"])
        self.assertEqual(errors(sh('command eval "$cmd"')), ["eval"])
        self.assertEqual(errors(sh('PATH=/bin eval "$cmd"')), ["eval"])

    def test_eval_of_a_fixed_text_and_the_word_eval_elsewhere(self) -> None:
        for command in (
            "eval 'echo hi'",
            'eval "echo hi"',
            'echo "eval $x"',
            "# eval $x",
            "grep eval $file",
            "cat <<EOF\neval $x\nEOF",
        ):
            with self.subTest(command=command):
                self.assertEqual(errors(sh(command)), [])


class RemoveRecursive(unittest.TestCase):
    def test_a_variable_without_quotes(self) -> None:
        for command in ("rm -rf $dir", "rm -rf $dir/x", 'rm -rf "$a"/$b', "rm -rf ${dir}"):
            with self.subTest(command=command):
                problems = cs.check_script("a.sh", sh(command))
                self.assertEqual([p.rule for p in problems], ["rm-rf"])
                self.assertIn("not in quotes", problems[0].message)

    def test_a_quoted_variable_that_starts_the_path_needs_a_guard(self) -> None:
        for command in (
            'rm -rf "$dir"',
            'rm -rf "${dir}"',
            'rm -rf "$dir/x"',
            'rm -rf "$dir"/x',
            'rm -rf -- "$dir"',
            'rm -rf "$(mktemp -d)"',
            'rm -rf ./"$dir"',
            'rm -rf "$HOME/.cache"',
            'sudo rm -rf "$dir"',
            'if true; then rm -rf "$dir"; fi',
            'rm -rf "${dir:-/tmp/x}"',
            'rm -rf "${dir-}"',
            'DEBUG=1 rm -rf "$dir"',
        ):
            with self.subTest(command=command):
                problems = cs.check_script("a.sh", sh(command))
                self.assertEqual([p.rule for p in problems], ["rm-rf"])
                self.assertIn("${VAR:?}", problems[0].message)

    def test_all_the_ways_to_say_recursive(self) -> None:
        for command in (
            'rm -rf "$d"',
            'rm -fr "$d"',
            'rm -r -f "$d"',
            'rm -Rf "$d"',
            'rm -r "$d"',
            'rm --recursive --force "$d"',
            'rm -f -r "$d"',
        ):
            with self.subTest(command=command):
                self.assertEqual(errors(sh(command)), ["rm-rf"])

    def test_every_operand_is_looked_at(self) -> None:
        text = sh('rm -rf /tmp/x "$a" "${b:?}" "$c"')
        self.assertEqual([p.line for p in cs.check_script("a.sh", text)], [5, 5])

    def test_guarded_or_fixed_paths_are_fine(self) -> None:
        for command in (
            'rm -rf "${dir:?}"',
            'rm -rf "${dir:?not set}/x"',
            'rm -rf "${dir:?}"/*',
            'rm -rf "${dir?}"',
            'rm -rf "/var/tmp/$name"',
            'rm -rf /var/tmp/"$name"',
            'rm -rf "build/$name"',
            "rm -rf deb claude export",
            "rm -rf /etc.kiwi",
        ):
            with self.subTest(command=command):
                self.assertEqual(errors(sh(command)), [])

    def test_rm_without_recursion_is_not_checked(self) -> None:
        for command in ('rm -f "$x"', 'rm "$x"', "rm -f $x", 'echo rm -rf "$x"', "# rm -rf $x"):
            with self.subTest(command=command):
                self.assertEqual(errors(sh(command)), [])

    def test_xargs_rm_has_no_operand_to_look_at(self) -> None:
        self.assertEqual(errors(sh('ls "$www" | xargs -r rm -rf')), [])


class Bashisms(unittest.TestCase):
    def test_each_construct_is_an_error_under_sh(self) -> None:
        for command in (
            "[[ -n $x ]]",
            'if [[ "$a" == b ]]; then echo; fi',
            "while [[ -n $x ]]; do break; done",
            "function f { :; }",
            "function f() { :; }",
            "a=(1 2)",
            "a+=(3)",
            "a[0]=1",
            'echo "${a[@]}"',
            "echo ${#a[@]}",
            "echo ${!a[@]}",
            "declare -a a",
            "declare -A a",
            "typeset -a a",
            "f() { local -a a; }",
            "mapfile -t a < /dev/null",
            "readarray -t a < /dev/null",
            "source ./x",
            "if true; then source ./x; fi",
            '[ "$a" == b ]',
            'test "$a" == b',
            "x=$'a\\nb'",
            "echo $'tab\\t'",
            "echo hi &> /dev/null",
            "echo hi &>> /dev/null",
        ):
            with self.subTest(command=command):
                problems = cs.check_script("a.sh", sh(command))
                self.assertEqual([p.rule for p in problems], ["bashism"], problems)
                self.assertIn("bash construct", problems[0].message)

    def test_the_message_names_the_construct_and_the_line(self) -> None:
        text = sh("echo ok\n[[ -n $x ]]")
        message = str(cs.check_script("a.sh", text)[0])
        self.assertIn("line 6: [[ ]] is a bash construct", message)

    def test_the_same_constructs_are_fine_under_bash(self) -> None:
        for command in (
            "[[ -n $x ]]",
            "function f { :; }",
            "a=(1 2); echo ${a[@]}",
            "source ./x",
            '[ "$a" == b ]',
            "x=$'a'",
            "echo hi &> /dev/null",
        ):
            with self.subTest(command=command):
                self.assertEqual(errors(bash(command)), [])

    def test_plain_sh_is_fine(self) -> None:
        for command in (
            '[ "$a" = b ] && [ -n "$c" ]',
            "f() { local x=1; echo $x; }",
            ". ./x",
            "echo $((1 + 2))",
            'set -- a b; echo "$@" "$#" "$1"',
            "case $x in a|b) echo ab ;; *) echo other ;; esac",
            "x='$'",
            "echo hi >/dev/null 2>&1",
            "echo hi >&2",
            "echo '[[ x ]]' 'function' 'source' '${a[@]}' '$'\"'\"'x'",
            'echo "[[ x ]] ${a:-b} ${#a}"',
            'echo "a=(1) a[0]=1 &> x"',
            "# [[ -n $x ]] source x",
            "cat <<EOF\n[[ -n $x ]]\nfunction f\nEOF",
            "echo function source",
            "sh -c '[[ -n x ]]'",
            "echo a==b",
            'echo "==" "$x"',
            "x=a[0]",
        ):
            with self.subTest(command=command):
                self.assertEqual(errors(sh(command)), [], command)

    def test_a_bash_pattern_in_double_brackets_is_one_problem(self) -> None:
        text = sh("[[ $x =~ ^(a|b)$ && ( -n $y || -z $z ) ]]\n[[ $a > $b ]]")
        self.assertEqual([p.line for p in cs.check_script("a.sh", text)], [5, 6])


class Backticks(unittest.TestCase):
    def test_backticks_are_a_warning(self) -> None:
        for command in ("x=`date`", 'echo "`date`"', "echo `echo \\`date\\``"):
            with self.subTest(command=command):
                self.assertEqual(found(sh(command)), [("backtick", True)])

    def test_the_command_in_backticks_is_checked_too(self) -> None:
        self.assertIn(("curl-pipe", False), found(sh("x=`curl -s https://example.org/x | sh`")))

    def test_backticks_that_are_not_commands(self) -> None:
        for command in (
            "echo '`date`'",
            "# `date`",
            "cat <<EOF\n`date`\nEOF",
            "echo \\`date\\`",
        ):
            with self.subTest(command=command):
                self.assertEqual(found(sh(command)), [])

    def test_backticks_do_not_fail_the_check(self) -> None:
        self.assertEqual(errors(sh("x=`date`")), [])


class SystemWrites(unittest.TestCase):
    def test_writes_without_a_comment_are_a_warning(self) -> None:
        for command in (
            "echo x > /etc/demo",
            "echo x >>/etc/demo",
            "echo x >| /usr/lib/demo",
            "cat /etc/hosts >/etc/hosts.new",
            "echo x | tee /etc/demo",
            "echo x | tee -a /usr/share/demo",
            "cp demo /usr/local/bin/demo",
            "cp -a demo /etc/demo/",
            "install -m 0644 demo /etc/demo",
            "install -d -m 0755 /etc/demo",
            "install -d /etc/a /etc/b",
            "install -d /etc/a /tmp/b",
            "cp demo /etc/demo 2>/dev/null",
            "echo x 1>/etc/demo",
            "install -m 0644 -t /etc/demo demo",
            "ln -s demo /usr/local/bin/demo",
            "ln -sfn demo /etc/demo",
            "mv demo /etc/demo",
            "mv /etc/demo demo",
            "sed -i s/a/b/ /etc/demo",
            "sed -i -e s/a/b/ /etc/demo",
            "sed -i.bak -E 's/a/b/' /etc/demo",
            "dd if=/dev/zero of=/etc/demo bs=1",
            "touch /etc/demo",
            "mkdir -p /usr/local/demo",
            "mkdir -m 0755 /etc/demo",
            "(umask 077 && echo x >/etc/demo)",
            "cat > /etc/demo <<EOF\nx\nEOF",
            'echo x > "/etc/demo"',
        ):
            with self.subTest(command=command):
                self.assertEqual(found(sh(command)), [("system-write", True)], command)

    def test_a_comment_right_above_explains_the_write(self) -> None:
        text = sh("# The unit is read from here.\ninstall -m 0644 demo /etc/demo")
        self.assertEqual(found(text), [])

    def test_a_comment_of_several_lines_explains_the_write(self) -> None:
        text = sh("# Why:\n# because.\necho x > /etc/demo")
        self.assertEqual(found(text), [])

    def test_a_blank_line_or_a_command_between_the_comment_and_the_write(self) -> None:
        self.assertEqual(found(sh("# Why.\n\necho x > /etc/demo")), [("system-write", True)])
        self.assertEqual(found(sh("# Why.\necho a\necho x > /etc/demo")), [("system-write", True)])

    def test_one_comment_covers_a_run_of_writes(self) -> None:
        text = sh(
            "# The unit files.\ninstall demo /etc/a\ninstall demo /etc/b\ninstall demo /etc/c"
        )
        self.assertEqual(found(text), [])
        text = sh("# The unit files.\ninstall demo /etc/a\necho between\ninstall demo /etc/c")
        self.assertEqual(found(text), [("system-write", True)])

    def test_the_line_of_the_write_is_reported(self) -> None:
        text = sh("echo a\necho x > /etc/demo")
        self.assertIn("line 6:", str(cs.check_script("a.sh", text)[0]))

    def test_reads_and_other_places_are_fine(self) -> None:
        for command in (
            "cat /etc/os-release",
            "cp /etc/hosts /tmp/hosts",
            "cp /etc/hosts demo",
            "grep -q x /etc/demo && echo y",
            "echo x >/tmp/demo",
            "echo x >/dev/null 2>&1",
            'echo x > "$dir/etc/demo"',
            'echo x > "$etc/demo"',
            "echo x > /etcetera/demo",
            "echo x > /usrlocal/demo",
            "echo x > /var/etc/demo",
            'install demo "$dest"',
            "ls /usr/bin | wc -l",
            "echo /etc/demo > /tmp/list",
            "echo x | tee /tmp/demo",
            "sed s/a/b/ /etc/demo",
            "sed -n p /etc/demo",
            "sed -i '/etc/d' /tmp/demo",
            "sed -i -e '/usr/d' /tmp/demo",
            "rm -f /etc/demo",
            "echo 'x > /etc/demo'",
            "# echo x > /etc/demo",
            "cat <<EOF\necho x > /etc/demo\nEOF",
        ):
            with self.subTest(command=command):
                self.assertEqual(found(sh(command)), [], command)

    def test_kiwi_scripts_may_write_there(self) -> None:
        text = bash("echo x > /etc/demo\ncp a /usr/lib/b")
        self.assertEqual(found(text, "obs/images/Vilna/config.sh"), [])
        self.assertEqual(found(text, "obs/images/Vilna/disk.sh"), [])
        self.assertEqual(found(text, "other/config.sh"), [("system-write", True)] * 2)

    def test_the_writes_are_only_warnings(self) -> None:
        self.assertEqual(errors(sh("echo x > /etc/demo")), [])


class Reader(unittest.TestCase):
    """The small reader of the script: what is code and what is not."""

    def test_comments_are_not_code(self) -> None:
        text = sh("echo a # curl x | sh\n# eval $x\necho 'a # b' \"c # d\" e#f")
        self.assertEqual(found(text), [])

    def test_a_here_document_body_is_not_code(self) -> None:
        body = "eval $x\ncurl a | sh\n`date`\nrm -rf $y\n[[ x ]]\n"
        for opener in ("cat <<EOF", "cat <<'EOF'", 'cat <<"EOF"', "cat <<\\EOF", "cat 3<<EOF"):
            with self.subTest(opener=opener):
                self.assertEqual(found(sh(f"{opener}\n{body}EOF")), [], opener)

    def test_a_tab_indented_delimiter_ends_a_dash_here_document(self) -> None:
        body = "eval $x\n[[ x ]]\n"
        for opener in ("cat <<-EOF", "cat <<-'EOF'"):
            with self.subTest(opener=opener):
                self.assertEqual(found(sh(f"{opener}\n{body}\tEOF\necho done")), [], opener)
        # Without the dash the indented line is only text: the here-document never ends.
        self.assertEqual(errors(sh(f"cat <<EOF\n{body}\tEOF\necho done")), ["syntax"])

    def test_code_after_a_here_document_is_checked(self) -> None:
        text = sh("cat <<EOF\nhello $x\nEOF\neval $cmd")
        problems = cs.check_script("a.sh", text)
        self.assertEqual([(p.rule, p.line) for p in problems], [("eval", 8)])

    def test_two_here_documents_on_one_line(self) -> None:
        text = sh("cat <<A <<B\none\nA\ntwo\nB\neval $cmd")
        self.assertEqual([(p.rule, p.line) for p in cs.check_script("a.sh", text)], [("eval", 10)])

    def test_a_here_document_inside_a_command_substitution(self) -> None:
        text = sh("x=$(cat <<EOF\neval $y\nEOF\n)\neval $cmd")
        self.assertEqual([(p.rule, p.line) for p in cs.check_script("a.sh", text)], [("eval", 9)])

    def test_the_delimiter_must_be_alone_on_its_line(self) -> None:
        text = sh("cat <<EOF\nEOF later\neval $x\nEOF")
        self.assertEqual(errors(text), [])

    def test_a_multi_line_string_is_not_code(self) -> None:
        text = sh("dbus-run-session -- bash -c '\nset -u\n[[ x ]]\neval $x\n'\neval $cmd")
        self.assertEqual([(p.rule, p.line) for p in cs.check_script("a.sh", text)], [("eval", 10)])

    def test_expansions_inside_double_quotes_are_found(self) -> None:
        text = sh('echo "$(eval "$x")"')
        self.assertEqual(errors(text), ["eval"])

    def test_nested_quotes_in_a_command_substitution(self) -> None:
        text = sh('here=$(cd "$(dirname "$0")" && pwd)\neval "$here"')
        self.assertEqual([(p.rule, p.line) for p in cs.check_script("a.sh", text)], [("eval", 6)])

    def test_case_patterns_do_not_end_a_command_substitution(self) -> None:
        text = sh('x=$(case $1 in a|b) echo ab ;; *) echo other ;; esac)\neval "$x"')
        self.assertEqual([(p.rule, p.line) for p in cs.check_script("a.sh", text)], [("eval", 6)])

    def test_case_patterns_in_the_script(self) -> None:
        text = sh(
            'case ${1:-} in\n  s) echo s ;;\n  (x) echo x ;;\n  */*|.*) echo y ;;\nesac\neval "$1"'
        )
        self.assertEqual([(p.rule, p.line) for p in cs.check_script("a.sh", text)], [("eval", 10)])

    def test_subshells_groups_and_functions(self) -> None:
        text = sh('(cd /tmp && eval "$x")\n{ eval "$y"; }\nf() { eval "$z"; }\nf')
        self.assertEqual([p.line for p in cs.check_script("a.sh", text)], [5, 6, 7])

    def test_a_hash_inside_a_word_does_not_start_a_comment(self) -> None:
        text = sh('echo a#b; eval "$x"\necho $# ${#x} $((1)); eval "$y"')
        self.assertEqual(
            [(p.rule, p.line) for p in cs.check_script("a.sh", text)], [("eval", 5), ("eval", 6)]
        )

    def test_strict_mode_inside_a_substitution_is_not_strict_mode_of_the_script(self) -> None:
        head = "#!/bin/sh\n# SPDX-License-Identifier: GPL-3.0-or-later\n"
        for body in ("x=$(set -eu)\n", "x=$(case a in a) set -eu ;; esac)\n", "(set -eu)\n"):
            with self.subTest(body=body):
                self.assertEqual(errors(head + body + "echo hi\n"), ["strict-mode"], body)

    def test_strict_mode_in_a_group_or_after_a_function_is_found(self) -> None:
        head = "#!/bin/sh\n# SPDX-License-Identifier: GPL-3.0-or-later\n"
        self.assertEqual(errors(head + "f() { :; }\nset -eu\n"), [])
        self.assertEqual(errors(head + "if true; then set -eu; fi\n"), [])

    def test_operators_inside_double_brackets_are_not_redirects(self) -> None:
        text = bash("[[ $a < /etc/x || $b > /etc/y ]]\n[[ $x =~ ^(a|b)$ ]] && echo yes")
        self.assertEqual(found(text), [])

    def test_a_file_descriptor_in_front_of_a_redirect_is_not_an_argument(self) -> None:
        text = sh("cp demo /tmp/demo 2>/etc/log\ncp demo /usr/demo 2>&1")
        self.assertEqual(
            [(p.rule, p.line) for p in cs.check_script("a.sh", text)],
            [("system-write", 5), ("system-write", 6)],
        )

    def test_a_continued_line_is_one_command(self) -> None:
        text = sh("curl -s https://example.org/x \\\n  | sh")
        self.assertEqual(
            [(p.rule, p.line) for p in cs.check_script("a.sh", text)], [("curl-pipe", 5)]
        )

    def test_special_parameters_and_arithmetic(self) -> None:
        text = sh('echo "$#" "$$" "$?" "$!" "$-" "$@" "$*" $((1 + (2 * 3))) "${#x}"\n(( n = 1 ))')
        self.assertEqual(found(text), [])

    def test_a_lone_dollar_and_escapes(self) -> None:
        text = sh('echo "cost: 5$" \\$x "a\\"b" \'it\'"\'"\'s\'\neval "$x"')
        self.assertEqual([(p.rule, p.line) for p in cs.check_script("a.sh", text)], [("eval", 6)])

    def test_default_values_with_braces_and_quotes(self) -> None:
        text = sh('x=${y:-\'}\'}\nz=${w:-"}"}\neval "$x"')
        self.assertEqual([(p.rule, p.line) for p in cs.check_script("a.sh", text)], [("eval", 7)])

    def test_unterminated_things_are_errors_with_a_line(self) -> None:
        for command, message in (
            ("echo 'abc", "unterminated single quote"),
            ('echo "abc', "unterminated double quote"),
            ("cat <<EOF\nabc", "unterminated here-document"),
            ("x=$(echo abc", "unterminated $("),
            ("x=`echo abc", "unterminated backtick"),
            ("echo ${x", "unterminated {"),
        ):
            with self.subTest(command=command):
                problems = cs.check_script("a.sh", sh(command))
                self.assertEqual([p.rule for p in problems], ["syntax"])
                self.assertIn(message, problems[0].message)
                self.assertIn("line 5", str(problems[0]))

    def test_header_problems_are_still_reported_when_the_script_cannot_be_read(self) -> None:
        problems = cs.check_script("a.sh", "echo 'abc\n")
        self.assertEqual([p.rule for p in problems], ["shebang", "spdx", "syntax"])


class Legacy(unittest.TestCase):
    def test_a_legacy_problem_is_a_warning_only_for_its_file_and_rule(self) -> None:
        text = sh("curl -s https://example.org/x | sh")
        with mock.patch.object(cs, "LEGACY", frozenset({("old/a.sh", "curl-pipe")})):
            self.assertEqual(found(text, "old/a.sh"), [("curl-pipe", True)])
            self.assertEqual(found(text, "new/a.sh"), [("curl-pipe", False)])
            self.assertEqual(found(text.replace("curl", "wget"), "old/a.sh"), [("curl-pipe", True)])
            other = sh("eval $x")
            self.assertEqual(found(other, "old/a.sh"), [("eval", False)])

    def test_legacy_applies_to_syntax_errors_and_header_rules_too(self) -> None:
        with mock.patch.object(cs, "LEGACY", frozenset({("old/a.sh", "syntax")})):
            self.assertEqual(found(sh("echo 'x"), "old/a.sh"), [("syntax", True)])

    def test_every_legacy_entry_names_a_rule_that_exists(self) -> None:
        rules = {
            "shebang",
            "spdx",
            "crlf",
            "strict-mode",
            "curl-pipe",
            "eval",
            "rm-rf",
            "bashism",
            "backtick",
            "system-write",
            "syntax",
            "encoding",
        }
        for path, rule in cs.LEGACY:
            self.assertIn(rule, rules)
            self.assertTrue(path.endswith(".sh"), path)


class Messages(unittest.TestCase):
    def test_format_of_errors_and_warnings(self) -> None:
        error = cs.Problem("a.sh", "bad", line=3)
        warning = cs.Problem("a.sh", "meh", warning=True)
        self.assertEqual(str(error), "a.sh: line 3: bad")
        self.assertEqual(str(warning), "a.sh: warning: meh")


class Main(unittest.TestCase):
    def run_main(self, files: dict[str, str], args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in files.items():
                path = Path(tmp) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(text.encode("utf-8"))
            with (
                mock.patch.object(cs, "ROOT", Path(tmp)),
                mock.patch.object(cs, "tracked_scripts", return_value=sorted(files)),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = cs.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_exit_0_for_good_scripts(self) -> None:
        code, out, err = self.run_main({"a.sh": sh("echo hi")}, [])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "1 shell scripts checked: 0 errors, 0 warnings\n")

    def test_warnings_do_not_fail_the_check(self) -> None:
        code, out, err = self.run_main({"a.sh": sh("x=`date`")}, [])
        self.assertEqual(code, 0)
        self.assertIn("a.sh: warning: line 5: backticks", err)
        self.assertIn("0 errors, 1 warnings", out)

    def test_exit_1_for_errors(self) -> None:
        code, out, err = self.run_main({"a.sh": sh("eval $x")}, [])
        self.assertEqual(code, 1)
        self.assertIn("a.sh: line 5: eval with a variable", err)
        self.assertIn("1 errors, 0 warnings", out)

    def test_problems_are_listed_by_file_and_line(self) -> None:
        files = {"b.sh": sh("eval $x"), "a.sh": sh("echo a\neval $x\neval $y")}
        _, _, err = self.run_main(files, [])
        self.assertEqual(
            [line.split(":")[0:2] for line in err.splitlines()],
            [["a.sh", " line 6"], ["a.sh", " line 7"], ["b.sh", " line 5"]],
        )

    def test_the_kiwi_scripts_are_listed(self) -> None:
        script = "#!/bin/bash\n# SPDX-License-Identifier: GPL-3.0-or-later\necho hi\n"
        files = {"obs/images/V/config.sh": script, "obs/images/V/disk.sh": script, "a.sh": sh("")}
        code, out, err = self.run_main(files, [])
        self.assertEqual(code, 0)
        self.assertIn(
            "KIWI scripts (a missing strict mode is only a warning): "
            "obs/images/V/config.sh, obs/images/V/disk.sh",
            out,
        )
        self.assertEqual(err.count("warning: no strict mode"), 2)

    def test_no_kiwi_line_without_kiwi_scripts(self) -> None:
        _, out, _ = self.run_main({"a.sh": sh("")}, [])
        self.assertNotIn("KIWI", out)

    def test_only_the_named_script_is_checked(self) -> None:
        files = {"a.sh": sh("eval $x"), "b.sh": sh("echo b")}
        self.assertEqual(self.run_main(files, ["b.sh"])[0], 0)
        self.assertEqual(self.run_main(files, ["a.sh"])[0], 1)
        self.assertIn("1 shell scripts checked", self.run_main(files, ["b.sh"])[1])

    def test_a_backslash_path_is_accepted(self) -> None:
        files = {"d/a.sh": sh("echo a")}
        self.assertEqual(self.run_main(files, ["d\\a.sh"])[0], 0)

    def test_exit_2_for_wrong_calls(self) -> None:
        files = {"a.sh": sh("echo a")}
        self.assertEqual(self.run_main(files, ["--bogus"])[0], 2)
        self.assertEqual(self.run_main(files, ["nope.sh"])[0], 2)
        self.assertEqual(self.run_main(files, ["a.sh", "nope.sh"])[0], 2)
        self.assertEqual(self.run_main({}, [])[0], 2)

    def test_help_is_not_an_error(self) -> None:
        code, _, err = self.run_main({"a.sh": sh("echo a")}, ["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", err)

    def test_git_failure_is_exit_2(self) -> None:
        err = io.StringIO()
        failure = mock.patch.object(cs, "tracked_scripts", side_effect=OSError("no git"))
        with failure, contextlib.redirect_stderr(err):
            self.assertEqual(cs.main([]), 2)
        self.assertIn("cannot list files with git ls-files", err.getvalue())

    def test_unreadable_and_non_utf8_files_are_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "bad.sh").write_bytes(b"#!/bin/sh\n\xff\xfe\n")
            problems = cs.check_files(Path(tmp), ["bad.sh", "missing.sh"])
        self.assertEqual(
            [(p.path, p.rule) for p in problems], [("bad.sh", "encoding"), ("missing.sh", "")]
        )
        self.assertIn("cannot read the file", problems[1].message)

    def test_check_files_reads_scripts_from_a_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.sh").write_text(sh("eval $x"), encoding="utf-8")
            (Path(tmp) / "b.sh").write_text(sh("echo b"), encoding="utf-8")
            problems = cs.check_files(Path(tmp), ["a.sh", "b.sh"])
        self.assertEqual([(p.path, p.rule) for p in problems], [("a.sh", "eval")])


class RealRepo(unittest.TestCase):
    def test_tracked_scripts_have_no_errors(self) -> None:
        tracked = cs.tracked_scripts(ROOT)
        self.assertTrue(tracked, "the repo has tracked shell scripts")
        found_errors = [str(p) for p in cs.check_files(ROOT, tracked) if not p.warning]
        self.assertEqual(found_errors, [])


if __name__ == "__main__":
    unittest.main()
