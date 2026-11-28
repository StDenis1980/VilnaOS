#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_actions.py (pins, permissions, pull_request_target, secrets, checkout).

Run: python3 tools/test_check_actions.py
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_actions as ca  # noqa: E402  (after sys.path.insert above)

REAL_LEGACY = dict(ca.LEGACY)  # the table of the repo, before a test replaces it
CHECKOUT_WITH = "      - uses: actions/checkout@v4\n        with:\n          persist-credentials: "
SHA = "933e9e8ca36e85046ebca8e211f0785c6becac40"
PERMS = "permissions:\n  contents: read\n"
CHECKOUT = (
    "      - uses: actions/checkout@v4\n        with:\n          persist-credentials: false\n"
)


def workflow(
    steps: str = CHECKOUT,
    on: str = "on:\n  push:\n  pull_request:\n",
    perms: str = PERMS,
    job: str = "build",
    job_extra: str = "",
    top_extra: str = "",
) -> str:
    """A workflow with one job; ``steps`` is the YAML of the steps (already indented)."""
    return (
        f"name: T\n{on}{perms}{top_extra}jobs:\n  {job}:\n    runs-on: ubuntu-latest\n"
        f"{job_extra}    steps:\n{steps}"
    )


def run_step(script: str, extra: str = "") -> str:
    return (
        "      - name: s\n" + extra + "        run: |\n" + textwrap.indent(script, " " * 10) + "\n"
    )


def steps_of(*items: str) -> str:
    return "".join(items)


class Repo(unittest.TestCase):
    """A repo in a temporary folder; every file in it counts as tracked."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = mock.patch.object(ca, "LEGACY", {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def run_check(self, text: str, path: str = ".github/workflows/t.yml") -> list[ca.Problem]:
        self.write(path, text)
        return ca.check(self.root, [path])[0]

    def errors(self, text: str, rule: str | None = None) -> list[str]:
        found = self.run_check(text)
        return [str(p) for p in found if not p.warning and (rule is None or p.rule == rule)]

    def warnings(self, text: str) -> list[str]:
        return [str(p) for p in self.run_check(text) if p.warning]

    def assertClean(self, text: str) -> None:  # noqa: N802  (unittest style)
        self.assertEqual([str(p) for p in self.run_check(text)], [])


class Pins(Repo):
    def uses(self, value: str, rule: str | None = "pin") -> list[str]:
        steps = f"      - uses: {value}\n"
        return self.errors(workflow(CHECKOUT + steps), rule)

    def test_version_tags_and_full_shas_are_pinned(self) -> None:
        for value in (
            "actions/checkout@v4",
            "actions/checkout@v4.1.7",
            "astral-sh/setup-uv@6",
            "owner/repo@1.2.3",
            "owner/repo@v1.2.3-rc.1",
            "owner/repo@gloxi-v1.2",
            "owner/repo@refs/tags/v2",
            "owner/repo/sub/dir@v2",
            f"flathub-infra/flatpak-github-actions/flatpak-builder@{SHA}",
            f"owner/repo@{SHA.upper()}",
            "owner/repo@" + "a" * 64,
        ):
            with self.subTest(value=value):
                self.assertEqual(self.uses(value), [])

    def test_branches_are_errors(self) -> None:
        for ref in ("main", "master", "develop", "HEAD", "release/v1", "latest", "Main"):
            with self.subTest(ref=ref):
                got = self.uses(f"owner/repo@{ref}")
                self.assertEqual(len(got), 1)
                self.assertIn(f"owner/repo@{ref} is pinned to a branch", got[0])
                self.assertTrue(got[0].startswith(".github/workflows/t.yml: job"))

    def test_other_refs_are_errors_too(self) -> None:
        got = self.uses("owner/repo@feature-x")
        self.assertEqual(len(got), 1)
        self.assertIn("is not pinned to a version tag or a full commit SHA", got[0])

    def test_missing_short_and_odd_refs_are_errors(self) -> None:
        self.assertIn("no @ref", self.uses("owner/repo")[0])
        self.assertIn("short SHA", self.uses("owner/repo@933e9e8")[0])
        self.assertIn("short SHA", self.uses("owner/repo@" + SHA[:39])[0])
        self.assertEqual(len(self.uses("owner/repo@" + SHA + "0")), 1)  # 41 digits
        self.assertIn("owner/repo@ref", self.uses("repo@v1")[0])
        self.assertIn("owner/repo@ref", self.uses("/repo@v1")[0])
        self.assertEqual(len(self.uses("owner/repo@master2")), 1)

    def test_a_date_made_of_digits_is_a_tag_not_a_short_sha(self) -> None:
        self.assertEqual(self.uses("owner/repo@20240501"), [])

    def test_uses_with_an_expression_is_an_error(self) -> None:
        got = self.uses("owner/repo@${{ inputs.ref }}")
        self.assertEqual(len(got), 1)
        self.assertIn("expression", got[0])

    def test_local_actions_are_fine(self) -> None:
        self.assertEqual(self.uses("./.github/actions/x"), [])
        self.assertEqual(len(self.uses("../elsewhere")), 1)

    def test_docker_images(self) -> None:
        self.assertEqual(self.uses("docker://alpine:3.20"), [])
        self.assertEqual(self.uses("docker://ghcr.io/o/i:v1"), [])
        self.assertEqual(self.uses("docker://alpine@sha256:" + "a" * 64), [])
        self.assertEqual(self.uses("docker://localhost:5000/img:1.0"), [])
        self.assertEqual(len(self.uses("docker://alpine")), 1)
        self.assertEqual(len(self.uses("docker://alpine:latest")), 1)
        self.assertEqual(len(self.uses("docker://localhost:5000/img")), 1)
        self.assertEqual(len(self.uses("docker://alpine@sha256:abc")), 1)

    def test_a_reusable_workflow_call_is_checked(self) -> None:
        for ref, count in (("main", 1), ("v2", 0), (SHA, 0)):
            text = workflow("", job_extra=f"    uses: o/r/.github/workflows/x.yml@{ref}\n")
            text = text.replace("    runs-on: ubuntu-latest\n", "").replace("    steps:\n", "")
            with self.subTest(ref=ref):
                self.assertEqual(len(self.errors(text, "pin")), count)

    def test_quoted_and_commented_values(self) -> None:
        self.assertEqual(self.uses('"owner/repo@main"  # comment'), self.uses("owner/repo@main"))
        self.assertEqual(self.uses("'owner/repo@v1'  # comment"), [])

    def test_the_message_names_job_step_and_line(self) -> None:
        text = workflow(CHECKOUT + "      - name: Cache\n        uses: o/r@main\n", job="lint")
        (message,) = self.errors(text, "pin")
        self.assertTrue(message.startswith('.github/workflows/t.yml: job "lint" step "Cache"'))
        self.assertIn("(line ", message)

    def test_several_problems_are_all_reported(self) -> None:
        steps = CHECKOUT + "      - uses: a/b@main\n      - uses: c/d\n      - uses: e/f@v1\n"
        self.assertEqual(len(self.errors(workflow(steps), "pin")), 2)


class Permissions(Repo):
    def test_a_workflow_without_permissions_is_an_error(self) -> None:
        (message,) = self.errors(workflow(perms=""))
        self.assertEqual(
            message,
            ".github/workflows/t.yml: the workflow has no top-level permissions: "
            "(add `permissions: contents: read`)",
        )

    def test_good_permissions(self) -> None:
        for perms in (
            PERMS,
            "permissions: {}\n",
            "permissions: read-all\n",
            "permissions:\n  contents: read\n  pull-requests: write\n",
        ):
            with self.subTest(perms=perms):
                self.assertEqual(self.errors(workflow(perms=perms)), [])

    def test_empty_write_all_and_odd_values_are_errors(self) -> None:
        self.assertIn(
            "gives every scope", self.errors(workflow(perms="permissions: write-all\n"))[0]
        )
        for perms in (
            "permissions: write-all\n",
            "permissions:\n",
            "permissions: write\n",
            "permissions:\n  - contents\n",
        ):
            with self.subTest(perms=perms):
                got = self.errors(workflow(perms=perms), "permissions")
                self.assertEqual(len(got), 1)

    def test_a_job_with_write_all_is_an_error(self) -> None:
        text = workflow(job_extra="    permissions: write-all\n")
        (message,) = self.errors(text)
        self.assertIn('job "build"', message)
        self.assertIn("write-all", message)

    def test_a_job_may_widen_one_scope_and_may_not_be_empty(self) -> None:
        self.assertEqual(
            self.errors(workflow(job_extra="    permissions:\n      id-token: write\n")), []
        )
        self.assertEqual(len(self.errors(workflow(job_extra="    permissions:\n"))), 1)

    def test_a_composite_action_needs_no_permissions(self) -> None:
        text = (
            "name: A\nruns:\n  using: composite\n  steps:\n    - run: echo hi\n      shell: bash\n"
        )
        found = self.run_check(text, ".github/actions/x/action.yml")
        self.assertEqual(found, [])


class PullRequestTarget(Repo):
    ON = "on:\n  pull_request_target:\n    types: [opened]\n"

    def checkout(self, with_lines: str) -> str:
        return (
            "      - uses: actions/checkout@v4\n        with:\n"
            + with_lines
            + "          persist-credentials: false\n"
        )

    def errors_for(self, steps: str, on: str = ON, **kwargs: str) -> list[str]:
        return self.errors(workflow(steps, on=on, **kwargs), "pr-target")

    def test_checkout_of_the_pull_request_head_is_an_error(self) -> None:
        for ref in (
            "${{ github.event.pull_request.head.sha }}",
            "${{ github.event.pull_request.head.ref }}",
            "${{ github.event.pull_request.merge_commit_sha }}",
            "${{ github.head_ref }}",
            "refs/pull/${{ github.event.number }}/merge",
            "refs/pull/7/head",
            "${{ github['event']['pull_request']['head']['sha'] }}",
            "${{ GitHub.Event.Pull_Request.Head.SHA }}",
            "${{ github . event . pull_request . head . sha }}",
            "${{ format('{0}', github.head_ref) }}",
        ):
            with self.subTest(ref=ref):
                got = self.errors_for(self.checkout(f"          ref: {ref}\n"))
                self.assertEqual(len(got), 1)
                self.assertIn("with: ref", got[0])

    def test_checkout_of_the_head_repository_is_an_error(self) -> None:
        got = self.errors_for(
            self.checkout(
                "          repository: ${{ github.event.pull_request.head.repo.full_name }}\n"
            )
        )
        self.assertEqual(len(got), 1)
        self.assertIn("with: repository", got[0])

    def test_checkout_of_the_base_is_fine(self) -> None:
        for lines in (
            "",
            "          ref: main\n",
            "          ref: ${{ github.event.pull_request.base.sha }}\n",
            "          ref: ${{ github.sha }}\n",
            "          repository: ${{ github.repository }}\n",
        ):
            with self.subTest(lines=lines):
                self.assertEqual(self.errors_for(self.checkout(lines)), [])

    def test_the_same_checkout_is_fine_for_a_plain_pull_request(self) -> None:
        text = workflow(self.checkout("          ref: ${{ github.event.pull_request.head.sha }}\n"))
        self.assertEqual(self.errors(text, "pr-target"), [])

    def test_the_trigger_may_be_written_in_any_form(self) -> None:
        steps = self.checkout("          ref: ${{ github.head_ref }}\n")
        for on in (
            "on: pull_request_target\n",
            "on: [push, pull_request_target]\n",
            "on:\n  - pull_request_target\n",
            "on:\n  push:\n  pull_request_target:\n",
        ):
            with self.subTest(on=on):
                self.assertEqual(len(self.errors_for(steps, on=on)), 1)

    def test_a_variable_that_carries_the_pull_request_head(self) -> None:
        env = "    env:\n      HEAD: ${{ github.event.pull_request.head.sha }}\n"
        steps = self.checkout("          ref: ${{ env.HEAD }}\n")
        self.assertEqual(len(self.errors_for(steps, job_extra=env)), 1)
        steps = run_step('git fetch origin "$HEAD"\ngit checkout ${HEAD}')
        self.assertEqual(len(self.errors_for(steps, job_extra=env)), 2)
        top = "env:\n  HEAD: ${{ github.event.pull_request.head.sha }}\n"
        self.assertEqual(len(self.errors_for(steps, top_extra=top)), 2)
        other = "env:\n  OTHER: ${{ github.event.pull_request.head.sha }}\n"
        self.assertEqual(len(self.errors_for(steps, top_extra=other)), 0)
        step_env = "        env:\n          HEAD: ${{ github.head_ref }}\n"
        self.assertEqual(len(self.errors_for(run_step("git checkout $HEAD", step_env))), 1)

    def test_a_harmless_variable_is_fine(self) -> None:
        env = "    env:\n      BASE: ${{ github.event.pull_request.base.sha }}\n"
        steps = run_step("git checkout $BASE")
        self.assertEqual(self.errors_for(steps, job_extra=env), [])

    def test_gh_pr_checkout_is_an_error(self) -> None:
        for script in (
            "gh pr checkout ${{ github.event.number }}",
            "sudo gh pr checkout 7",
            "gh --repo o/r pr checkout 7",
            "gh -R o/r pr checkout 7",
            "gh pr checkout 7 --detach",
        ):
            with self.subTest(script=script):
                self.assertEqual(len(self.errors_for(run_step(script))), 1)

    def test_git_commands_that_take_the_pull_request(self) -> None:
        for script in (
            "git fetch origin refs/pull/7/head",
            "git fetch origin pull/7/head:pr",
            "git checkout ${{ github.event.pull_request.head.sha }}",
            "git -C src merge ${{ github.head_ref }}",
            "git pull origin ${{ github.event.pull_request.head.ref }}",
            "bash -c 'git checkout ${{ github.head_ref }}'",
            "if true; then git switch ${{ github.head_ref }}; fi",
        ):
            with self.subTest(script=script):
                self.assertEqual(len(self.errors_for(run_step(script))), 1)

    def test_other_git_and_gh_commands_are_fine(self) -> None:
        for script in (
            "git fetch origin main",
            "git checkout main",
            "git log --oneline",
            "echo git checkout ${{ github.head_ref }}",
            "gh pr view 7",
            "gh pr comment 7 --body hi",
            "git status",
        ):
            with self.subTest(script=script):
                self.assertEqual(self.errors_for(run_step(script)), [])

    def test_a_workflow_without_the_trigger_is_not_checked(self) -> None:
        self.assertEqual(
            self.errors(workflow(run_step("gh pr checkout 7")), "pr-target"),
            [],
        )


class SecretsInRun(Repo):
    def in_run(self, script: str) -> list[str]:
        return self.errors(workflow(CHECKOUT + run_step(script)), "secret-in-run")

    def test_secrets_in_a_command_are_errors(self) -> None:
        for script in (
            "curl -H 'Authorization: ${{ secrets.API }}' https://example.org",
            'echo "${{ secrets.DESEC_TOKEN }}" | tool',
            "tool --key ${{secrets.KEY}}",
            "tool ${{ secrets['KEY'] }}",
            "tool ${{ secrets[ 'KEY' ] }}",
            "tool ${{ SECRETS.KEY }}",
            "tool ${{ Secrets . KEY }}",
            "tool ${{ toJSON(secrets) }}",
            "tool ${{ secrets }}",
            "tool ${{ github.token }}",
            "tool ${{ github['token'] }}",
            "tool ${{ GITHUB.TOKEN }}",
            "tool ${{ format('{0}-x', secrets.A) }}",
            "tool ${{ inputs.x || secrets.A }}",
            "tool ${{ startsWith(github.ref, 'refs/tags/') && secrets.A || '' }}",
            "line one\nline two\ntool ${{ secrets.A }}",
        ):
            with self.subTest(script=script):
                got = self.in_run(script)
                self.assertEqual(len(got), 1)
                self.assertIn("pass it through env:", got[0])

    def test_closing_braces_inside_a_quoted_string_do_not_hide_the_secret(self) -> None:
        for script in (
            "tool ${{ format('}}', secrets.A) }}",
            "tool ${{ format('it''s }}', secrets.A) }}",
            "tool ${{ '}}' && secrets.A }}",
        ):
            with self.subTest(script=script):
                self.assertEqual(len(self.in_run(script)), 1)
        self.assertEqual(self.in_run("tool ${{ format('}}', github.sha) }} ${{ vars.A }}"), [])

    def test_a_variable_that_holds_a_secret_is_a_secret_too(self) -> None:
        env = "        env:\n          T: ${{ secrets.A }}\n"
        got = self.errors(workflow(CHECKOUT + run_step("tool ${{ env.T }}", env)), "secret-in-run")
        self.assertEqual(len(got), 1)
        got = self.errors(workflow(CHECKOUT + run_step("tool ${{ env['T'] }}", env)))
        self.assertEqual(len(got), 1)
        self.assertEqual(self.in_run('tool "$T"'), [])
        self.assertEqual(self.errors(workflow(CHECKOUT + run_step("tool ${{ env.U }}", env))), [])
        top = "env:\n  T: ${{ github.token }}\n"
        text = workflow(CHECKOUT + run_step("tool ${{ env.T }}"), top_extra=top)
        self.assertEqual(len(self.errors(text, "secret-in-run")), 1)
        job = "    env:\n      T: ${{ secrets.A }}\n"
        text = workflow(CHECKOUT + run_step("tool ${{ env.T }}"), job_extra=job)
        self.assertEqual(len(self.errors(text, "secret-in-run")), 1)
        plain = "        env:\n          T: ${{ vars.A }}\n"
        self.assertEqual(self.errors(workflow(CHECKOUT + run_step("tool ${{ env.T }}", plain))), [])

    def test_two_expressions_are_two_errors(self) -> None:
        self.assertEqual(len(self.in_run("tool ${{ secrets.A }} ${{ secrets.B }}")), 2)

    def test_other_contexts_and_values_are_fine(self) -> None:
        for script in (
            "tool ${{ vars.OBS_USER }}",
            "tool ${{ github.sha }}",
            "tool ${{ inputs.secrets }}",
            "tool ${{ steps.a.outputs.secrets }}",
            "tool ${{ steps.a.outputs.token }}",
            "tool ${{ needs.a.outputs.github.token }}",
            "tool ${{ matrix.secrets }}",
            "tool ${{ 'secrets.A' }}",
            "tool ${{ format('github.token {0}', github.sha) }}",
            "tool ${{ vars.secrets_x }}",
            "tool ${{ secrets-x }}",
            "echo secrets.A github.token",
            'echo "$SECRET" "$GITHUB_TOKEN"',
        ):
            with self.subTest(script=script):
                self.assertEqual(self.in_run(script), [])

    def test_secrets_in_env_and_with_are_the_right_way(self) -> None:
        steps = (
            "      - name: s\n        env:\n          TOKEN: ${{ secrets.T }}\n"
            '          GH_TOKEN: ${{ github.token }}\n        run: tool "$TOKEN"\n'
            "      - uses: o/r@v1\n        with:\n          token: ${{ secrets.T }}\n"
        )
        self.assertEqual(self.errors(workflow(CHECKOUT + steps), "secret-in-run"), [])

    def test_the_message_does_not_repeat_the_whole_run(self) -> None:
        long = "x" * 200
        (message,) = self.in_run(f"echo {long}\ntool ${{{{ secrets.A }}}} {long}")
        self.assertNotIn(long, message)
        self.assertIn("secrets.A", message)
        self.assertIn('step "s"', message)

    def test_a_long_expression_is_cut_in_the_message(self) -> None:
        long = " || ".join(f"inputs.name{i}" for i in range(20))
        (message,) = self.in_run(f"tool ${{{{ secrets.A || {long} }}}}")
        self.assertNotIn("inputs.name19", message)
        self.assertIn("secrets.A || inputs.name0", message)
        self.assertIn("...", message)

    def test_quoted_run_and_other_block_styles(self) -> None:
        for step in (
            '      - run: "tool ${{ secrets.A }}"\n',
            "      - run: 'tool ${{ secrets.A }}'\n",
            "      - run: tool ${{ secrets.A }}\n",
            "      - run: >\n          tool\n          ${{ secrets.A }}\n",
        ):
            with self.subTest(step=step):
                self.assertEqual(len(self.errors(workflow(CHECKOUT + step), "secret-in-run")), 1)


class Checkout(Repo):
    def problems(self, steps: str, **kwargs: str) -> list[str]:
        return self.errors(workflow(steps, **kwargs), "persist-credentials")

    def test_false_is_fine_in_every_accepted_spelling(self) -> None:
        for value in ("false", "False", "FALSE", "'false'", '"false"'):
            steps = f"{CHECKOUT_WITH}{value}\n"
            with self.subTest(value=value):
                self.assertEqual(self.problems(steps), [])
                self.assertEqual(self.warnings(workflow(steps)), [])

    def test_missing_is_an_error_without_a_push(self) -> None:
        (message,) = self.problems("      - uses: actions/checkout@v4\n")
        self.assertIn("persist-credentials: false", message)
        self.assertIn('job "build"', message)

    def test_odd_values_are_errors(self) -> None:
        for value in ("no", "0", "off", "${{ false }}", "${{ inputs.x }}", "''"):
            steps = f"{CHECKOUT_WITH}{value}\n"
            with self.subTest(value=value):
                self.assertEqual(len(self.problems(steps)), 1)

    def test_explicit_true_without_a_push_is_a_warning(self) -> None:
        steps = f"{CHECKOUT_WITH}true\n"
        self.assertEqual(self.problems(steps), [])
        (warning,) = self.warnings(workflow(steps))
        self.assertIn("warning: ", warning)
        self.assertIn("persist-credentials: true", warning)

    def test_a_later_push_allows_the_default_and_true(self) -> None:
        for script in (
            "git push",
            "git push origin HEAD:main",
            "git -C out push",
            "git -c user.name=x push",
            "git --git-dir=.git push",
            "sudo git push",
            "env A=1 git push",
            "FOO=1 git push",
            "git commit -m x && git push",
            "git add -A\ngit commit -m x\ngit push",
            'if [ -n "$X" ]; then git push; fi',
            "bash -c 'git push'",
            'sh -ec "git push origin main"',
            "timeout 60 git push",
            "git commit -m x \\\n  && git push",
            "/usr/bin/git push",
        ):
            with self.subTest(script=script):
                for value in ("", "true"):
                    extra = (
                        ""
                        if not value
                        else f"        with:\n          persist-credentials: {value}\n"
                    )
                    steps = "      - uses: actions/checkout@v4\n" + extra + run_step(script)
                    self.assertEqual(self.run_check(workflow(steps)), [])

    def test_a_push_some_steps_later_counts(self) -> None:
        steps = (
            "      - uses: actions/checkout@v4\n"
            + run_step("make")
            + run_step("make test")
            + run_step("git push")
        )
        self.assertEqual(self.problems(steps), [])

    def test_a_known_pushing_action_counts(self) -> None:
        for action in (
            "Stefanzweifel/Git-Auto-Commit-Action@v5",
            "stefanzweifel/git-auto-commit-action@v5",
            "peter-evans/create-pull-request@v7",
            "ad-m/github-push-action@master",
        ):
            steps = "      - uses: actions/checkout@v4\n" + f"      - uses: {action}\n"
            with self.subTest(action=action):
                self.assertEqual(self.problems(steps), [])

    def test_text_that_only_looks_like_a_push_does_not_count(self) -> None:
        for script in (
            'echo "git push"',
            "# git push",
            "git pull",
            "git log --oneline # push",
            "gh release create v1",
            "git config push.default simple",
            "git remote add push-mirror https://example.org/r.git",
            "echo git push",
            "cat <<EOF\ngit push\nEOF",
            "grep push file",
        ):
            steps = "      - uses: actions/checkout@v4\n" + run_step(script)
            with self.subTest(script=script):
                self.assertEqual(len(self.problems(steps)), 1)

    def test_a_push_before_the_checkout_does_not_count(self) -> None:
        steps = run_step("git push") + "      - uses: actions/checkout@v4\n"
        self.assertEqual(len(self.problems(steps)), 1)

    def test_a_push_in_another_job_does_not_count(self) -> None:
        text = (
            workflow("      - uses: actions/checkout@v4\n")
            + "  other:\n    runs-on: ubuntu-latest\n    steps:\n"
            + run_step("git push")
        )
        self.assertEqual(len(self.errors(text, "persist-credentials")), 1)

    def test_every_checkout_is_judged_on_its_own(self) -> None:
        steps = (
            "      - uses: actions/checkout@v4\n"
            + run_step("git push")
            + "      - uses: actions/checkout@v4\n"
            + "        with:\n          path: other\n"
        )
        self.assertEqual(len(self.problems(steps)), 1)

    def test_other_checkouts_are_not_the_official_action(self) -> None:
        for uses in ("my/checkout@v4", "actions/checkout-extra@v4", "actions/cache@v4"):
            self.assertEqual(self.problems(f"      - uses: {uses}\n"), [])
        for uses in ("actions/checkout@v4", "actions/checkout@" + SHA, "Actions/Checkout@v4"):
            self.assertEqual(len(self.problems(f"      - uses: {uses}\n")), 1)

    def test_a_composite_action_is_checked_too(self) -> None:
        text = (
            "name: A\nruns:\n  using: composite\n  steps:\n"
            "    - uses: actions/checkout@v4\n    - uses: o/r@main\n"
            "    - run: echo ${{ secrets.X }}\n      shell: bash\n"
        )
        got = self.run_check(text, ".github/actions/x/action.yml")
        self.assertEqual(
            sorted(p.rule for p in got), ["persist-credentials", "pin", "secret-in-run"]
        )


class Whole(Repo):
    def test_a_clean_workflow_has_no_problems(self) -> None:
        steps = (
            CHECKOUT
            + f"      - uses: o/r@{SHA}\n"
            + "      - name: Publish\n        env:\n          TOKEN: ${{ secrets.T }}\n"
            + '        run: tool "$TOKEN"\n'
        )
        self.assertClean(workflow(steps))

    def test_only_workflows_and_composite_actions_are_read(self) -> None:
        bad = workflow("      - uses: o/r@main\n", perms="")
        for path in (
            ".github/ISSUE_TEMPLATE/x.yml",
            ".github/workflows/sub/x.yml",
            ".github/workflows/x.txt",
            ".github/actions/x/other.yml",
            ".github/actions/action.yml",
            ".github/actions/a/b/action.yml",
            "docs/.github/workflows/x.yml",
            "x.yml",
        ):
            self.write(path, bad)
            with self.subTest(path=path):
                self.assertEqual(ca.check(self.root, [path])[1]["files"], 0)
        for path in (
            ".github/workflows/x.yml",
            ".github/workflows/x.yaml",
            ".github/actions/a/action.yml",
        ):
            self.write(path, bad)
            with self.subTest(path=path):
                self.assertEqual(ca.check(self.root, [path])[1]["files"], 1)

    def test_an_unreadable_workflow_is_an_error(self) -> None:
        self.write(".github/workflows/a.yml", "jobs:\n\tbuild: {\n")
        self.write(".github/workflows/b.yml", "name: [unclosed\n")
        problems, stats = ca.check(
            self.root,
            [".github/workflows/a.yml", ".github/workflows/b.yml", ".github/workflows/c.yml"],
        )
        self.assertEqual(stats["files"], 0)
        self.assertEqual([p.rule for p in problems], ["yaml"] * 3)
        self.assertTrue(all(not p.warning for p in problems))
        self.assertIn("cannot read the workflow", str(problems[0]))

    def test_a_workflow_with_a_binary_byte_is_an_error_not_a_crash(self) -> None:
        (self.root / ".github/workflows").mkdir(parents=True)
        (self.root / ".github/workflows/a.yml").write_bytes(b"name: \xff\xfe\n")
        problems, _ = ca.check(self.root, [".github/workflows/a.yml"])
        self.assertEqual([p.rule for p in problems], ["yaml"])

    def test_errors_come_before_warnings_and_both_are_sorted(self) -> None:
        steps = f"{CHECKOUT_WITH}true\n" + "      - uses: z/z@main\n      - uses: a/a@main\n"
        problems = self.run_check(workflow(steps))
        self.assertEqual([p.warning for p in problems], [False, False, True])
        self.assertIn("a/a@main", problems[0].message)
        self.assertIn("z/z@main", problems[1].message)

    def test_the_output_is_sorted_and_stable(self) -> None:
        self.write(".github/workflows/b.yml", workflow("      - uses: o/r@main\n", perms=""))
        self.write(".github/workflows/a.yml", workflow("      - uses: o/r\n", perms=""))
        tracked = [".github/workflows/b.yml", ".github/workflows/a.yml"]
        first = [str(p) for p in ca.check(self.root, tracked)[0]]
        second = [str(p) for p in ca.check(self.root, tracked[::-1])[0]]
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))
        self.assertEqual(len(first), 4)

    def test_nothing_is_written(self) -> None:
        self.write(".github/workflows/a.yml", workflow("      - uses: o/r@main\n"))
        before = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))
        text_before = (self.root / ".github/workflows/a.yml").read_text(encoding="utf-8")
        ca.check(self.root, [".github/workflows/a.yml"])
        after = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual(
            (self.root / ".github/workflows/a.yml").read_text(encoding="utf-8"), text_before
        )

    def test_stats(self) -> None:
        self.write(".github/workflows/a.yml", workflow(CHECKOUT + run_step("echo")))
        self.assertEqual(
            ca.check(self.root, [".github/workflows/a.yml"])[1], {"files": 1, "jobs": 1, "steps": 2}
        )


class Legacy(Repo):
    PATH = ".github/workflows/t.yml"
    KEY = (PATH, "persist-credentials")

    def check(self, text: str, legacy: dict[tuple[str, str], int]) -> list[ca.Problem]:
        self.write(self.PATH, text)
        with mock.patch.object(ca, "LEGACY", legacy):
            return ca.check(self.root, [self.PATH])[0]

    def two_jobs(self) -> str:
        return workflow("      - uses: actions/checkout@v4\n", job="one") + (
            "  two:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
        )

    def test_listed_problems_are_warnings(self) -> None:
        problems = self.check(self.two_jobs(), {self.KEY: 2})
        self.assertEqual([p.warning for p in problems], [True, True])
        self.assertTrue(all(str(p).startswith(f"{self.PATH}: warning: ") for p in problems))

    def test_another_rule_or_file_stays_an_error(self) -> None:
        text = workflow("      - uses: actions/checkout@v4\n      - uses: o/r@main\n")
        problems = self.check(text, {self.KEY: 1, (self.PATH, "pin"): 0})
        self.assertEqual(
            {p.rule: p.warning for p in problems}, {"persist-credentials": True, "pin": False}
        )
        problems = self.check(self.two_jobs(), {("other.yml", "persist-credentials"): 2})
        self.assertEqual([p.warning for p in problems if p.rule != "legacy"], [False, False])

    def test_a_new_problem_in_a_listed_file_is_an_error(self) -> None:
        problems = self.check(self.two_jobs(), {self.KEY: 1})
        self.assertEqual(sorted(p.warning for p in problems), [False, True])
        error = next(p for p in problems if not p.warning)
        self.assertIn(
            "LEGACY allows only 1 persist-credentials problem(s) in this file", str(error)
        )

    def test_without_a_legacy_line_there_is_no_legacy_note(self) -> None:
        (error,) = self.check(self.two_jobs(), {})[:1]
        self.assertNotIn("LEGACY", str(error))

    def test_a_listed_problem_that_is_gone_is_a_warning(self) -> None:
        problems = self.check(workflow("      - uses: actions/checkout@v4\n"), {self.KEY: 2})
        self.assertTrue(all(p.warning for p in problems))
        stale = [str(p) for p in problems if p.rule == "legacy"]
        self.assertEqual(len(stale), 1)
        self.assertIn("lower the number or delete the line", stale[0])
        self.assertIn("lists 2 persist-credentials problem(s) but only 1 found", stale[0])

    def test_a_fixed_file_leaves_a_stale_warning_and_no_error(self) -> None:
        problems = self.check(workflow(CHECKOUT), {self.KEY: 1})
        self.assertEqual([(p.rule, p.warning) for p in problems], [("legacy", True)])

    def test_checking_one_file_does_not_report_stale_lines(self) -> None:
        self.write(self.PATH, workflow(CHECKOUT))
        with mock.patch.object(ca, "LEGACY", {("other.yml", "pin"): 3}):
            self.assertEqual(ca.check(self.root, [], only=[self.PATH])[0], [])

    def test_a_warning_is_not_counted_against_the_number(self) -> None:
        steps = f"{CHECKOUT_WITH}true\n" + "      - uses: actions/checkout@v4\n"
        problems = self.check(workflow(steps), {self.KEY: 1})
        self.assertEqual(
            sorted(p.rule for p in problems), ["persist-credentials", "persist-credentials-true"]
        )
        self.assertTrue(all(p.warning for p in problems))

    def test_the_real_table_matches_the_real_repo(self) -> None:
        tracked = ca.tracked_files(ROOT)
        with mock.patch.object(ca, "LEGACY", REAL_LEGACY):
            problems, stats = ca.check(ROOT, tracked)
        self.assertEqual([str(p) for p in problems if not p.warning], [])
        self.assertEqual([p for p in problems if p.rule == "legacy"], [])
        self.assertGreater(stats["files"], 0)
        self.assertTrue(all(p.warning for p in problems))


class Main(Repo):
    def call(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ca.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_help_and_wrong_calls(self) -> None:
        self.assertEqual(self.call("--help")[0], 0)
        self.assertEqual(self.call("-h")[0], 0)
        code, _, err = self.call("--nope")
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)
        code, _, err = self.call(".github/workflows/does-not-exist.yml")
        self.assertEqual(code, 2)
        self.assertIn("does-not-exist.yml: no such file", err)

    def test_the_repo_exits_zero_and_says_how_many(self) -> None:
        with mock.patch.object(ca, "LEGACY", REAL_LEGACY):
            code, out, err = self.call()
        self.assertEqual(code, 0)
        self.assertIn("0 errors", out)
        self.assertNotIn("\n\n", out)
        for line in err.splitlines():
            self.assertTrue(line.startswith(".github/"), line)
            self.assertIn(": warning: ", line)

    def test_one_file_with_an_error_exits_one(self) -> None:
        with mock.patch.object(ca, "ROOT", self.root):
            self.write(".github/workflows/x.yml", workflow("      - uses: o/r@main\n"))
            with mock.patch.object(ca, "tracked_files", return_value=[".github/workflows/x.yml"]):
                code, out, err = self.call(".github/workflows/x.yml")
                code_all, _, _ = self.call()
        self.assertEqual((code, code_all), (1, 1))
        self.assertIn(".github/workflows/x.yml: job", err)
        self.assertIn("1 errors", out)

    def test_warnings_alone_exit_zero(self) -> None:
        steps = f"{CHECKOUT_WITH}true\n"
        with mock.patch.object(ca, "ROOT", self.root):
            self.write(".github/workflows/x.yml", workflow(steps))
            with mock.patch.object(ca, "tracked_files", return_value=[".github/workflows/x.yml"]):
                code, out, err = self.call()
        self.assertEqual(code, 0)
        self.assertIn("1 warnings", out)
        self.assertIn(": warning: ", err)

    def test_git_failure_exits_two(self) -> None:
        error = subprocess.CalledProcessError(128, ["git", "ls-files"])
        with mock.patch.object(ca, "tracked_files", side_effect=error):
            code, _, err = self.call()
        self.assertEqual(code, 2)
        self.assertIn("cannot list files", err)

    def test_only_git_ls_files_is_run(self) -> None:
        calls = []
        real = subprocess.run

        def spy(cmd: list[str], *args: object, **kwargs: object) -> object:
            calls.append(cmd)
            return real(cmd, *args, **kwargs)  # type: ignore[call-overload]

        with mock.patch.object(subprocess, "run", spy):
            self.call()
        self.assertEqual(calls, [["git", "ls-files"]])


if __name__ == "__main__":
    unittest.main()
