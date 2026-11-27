#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the tracked GitHub Actions workflows for the usual supply-chain and secret mistakes.

The workflows (``.github/workflows/*.yml``) and the composite actions of the repo
(``.github/actions/*/action.yml``) are read with the small YAML reader of
``tools/check_ci_coverage.py`` (imported, no PyYAML). Nothing is executed. The checks:

* ``pin``: every ``uses:`` (a step, or a job that calls a reusable workflow) is pinned to a
  version tag (``v4``, ``v1.2.3``, ``gloxi-v1``) or to a full commit SHA (40 or 64 hex digits).
  A branch (``main``, ``release/v1``), a short SHA, a missing ``@ref`` and an expression in
  ``uses`` are errors. ``./local`` actions are fine. ``docker://image`` needs a digest or a tag
  other than ``latest``. A version tag is a mutable pointer too; only a SHA is immutable, but
  a tag is what the repo policy asks for;
* ``permissions``: the workflow has ``permissions:`` at its top level (``{}`` and ``read-all``
  are fine); an empty value and ``write-all`` (at the top or in a job) are errors;
* ``pr-target``: a workflow triggered by ``pull_request_target`` runs with the secrets and a
  write token of the base repo, so it must not take code from the pull request: an
  ``actions/checkout`` whose ``ref`` or ``repository`` points at the pull request (``head``,
  ``merge_commit_sha``, ``head_ref``, ``refs/pull/..``, also through an ``env`` variable), a
  ``gh pr checkout``, or a ``git checkout/fetch/pull/merge ..`` of the pull request head;
* ``secret-in-run``: no ``${{ }}`` expression of a ``run:`` reads ``secrets`` or
  ``github.token`` (the value would be pasted into the shell command line and its log);
  pass it through ``env:`` instead. Text inside quotes of an expression does not count;
* ``persist-credentials``: an ``actions/checkout`` has ``persist-credentials: false``
  unless a later step of the same job pushes (``git push`` in a ``run``, also through
  ``sudo``, ``env``, ``bash -c`` or ``git -C dir``; or an action known to push). An explicit
  ``true`` with no push after it is a warning (the push may hide in a script this tool does not
  read); the value may only be written ``false``, ``False`` or ``FALSE`` (what the action reads).

A workflow that cannot be read is an error (``yaml``). Errors make the exit code 1. Warnings
(``path: warning: ...``) are listed but do not fail the check. Problems the repo already has
are warnings (see ``LEGACY``). Only ``git ls-files`` is run; nothing else is executed, nothing is
read from the network and nothing is written.

Run: python3 tools/check_actions.py [FILE ...]
FILE is a path from the repo root; without it every tracked workflow and action is checked.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import posixpath
import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

import check_ci_coverage as cc  # noqa: E402  (a neighbour in tools/, as check_obs imports check_spec)

WORKFLOW_DIR = ".github/workflows"
ACTION_DIR = ".github/actions"

# Problems the repo already has today: (path from the repo root, rule) -> how many. They are
# warnings, so that the check can be switched on in CI. A problem over the number is an
# error (a new one in the same file); fewer problems than the number is a warning (lower the
# number). Fix the workflow, then lower or delete the line.
LEGACY: dict[tuple[str, str], int] = {
    # actions/checkout without persist-credentials: false in jobs that never push.
    (".github/workflows/ci.yml", "persist-credentials"): 19,
    (".github/workflows/claude-desktop.yml", "persist-credentials"): 2,
    (".github/workflows/cursor-apps.yml", "persist-credentials"): 2,
    (".github/workflows/devin-desktop.yml", "persist-credentials"): 2,
    (".github/workflows/dns.yml", "persist-credentials"): 1,
    (".github/workflows/flatpak.yml", "persist-credentials"): 3,
    (".github/workflows/obs.yml", "persist-credentials"): 2,
    (".github/workflows/site-watch.yml", "persist-credentials"): 2,
}

SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
SHORT_SHA = re.compile(r"(?=[0-9]*[a-fA-F])[0-9a-fA-F]{7,39}")
# v4, 1.2.3, v1.2.3-rc.1, 2024.05.1, gloxi-v1.2, release_3, refs/tags/v1
TAG = re.compile(
    r"(?:refs/tags/)?(?:[A-Za-z][A-Za-z0-9]*[-_])?[vV]?\d+(?:[._-]\d+)*"
    r"(?:[-+.][0-9A-Za-z][0-9A-Za-z.+-]*)?"
)
BRANCH_NAMES = cc.wordset("main master develop dev trunk head latest stable nightly edge")
DOCKER_DIGEST = re.compile(r"sha256:[0-9a-fA-F]{64}")
CHECKOUT = re.compile(r"^actions/checkout(?:@|$)", re.IGNORECASE)
GH_PR_CHECKOUT = ("pr", "checkout")
GH_VALUE_OPTIONS = frozenset({"-R", "--repo", "--hostname"})
FALSE_VALUES = frozenset({"false", "False", "FALSE"})
# Actions whose job is to push to the repo: they need the credentials of the checkout.
PUSH_ACTIONS = cc.wordset("""
    stefanzweifel/git-auto-commit-action peter-evans/create-pull-request
    ad-m/github-push-action endbug/add-and-commit jamesives/github-pages-deploy-action
    peaceiris/actions-gh-pages actions-js/push crazy-max/ghaction-github-pages
    devops-infra/action-commit-push cpina/github-action-push-to-another-repository""")
GIT_VALUE_OPTIONS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
)
GIT_PR_COMMANDS = cc.wordset("checkout fetch pull switch merge cherry-pick clone reset restore")
SHELLS = cc.SHELLS

BRACKET = re.compile(r"\[\s*'([^']*)'\s*\]")
STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
SECRET_CONTEXT = re.compile(r"(?<![\w.-])secrets(?![\w-])")
GITHUB_TOKEN = re.compile(r"(?<![\w.-])github\.token(?![\w-])")
# Text of the pull request: its head, the merge commit, ``refs/pull/N/..``.
PR_CODE = re.compile(
    r"github\.event\.pull_request\.(?:head|merge_commit_sha)|github\.head_ref"
    r"|refs/pull/|(?<![\w.-])pull/\d+/(?:head|merge)"
)
STEP_OUTPUT_LIMIT = 50


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False
    rule: str = ""

    def __str__(self) -> str:
        kind = "warning: " if self.warning else ""
        return f"{self.path}: {kind}{self.message}"


@dataclass(frozen=True)
class Where:
    """Where a problem is: file, job and step (``step`` is None for the job itself)."""

    path: str
    job: str = ""
    step: str | None = None
    line: int = 0

    def say(self, message: str) -> str:
        parts = []
        if self.job:
            parts.append(f'job "{self.job}"')
        if self.step is not None:
            parts.append(f'step "{self.step}"')
        text = " ".join(parts)
        if self.line:
            text += f" (line {self.line})" if text else f"line {self.line}"
        return f"{text}: {message}" if text else message


def found(where: Where, rule: str, message: str, warning: bool = False) -> Problem:
    return Problem(where.path, where.say(message), warning, rule)


# ---------------------------------------------------------------------------
# Expressions and shell lines
# ---------------------------------------------------------------------------


def normal(text: str) -> str:
    """Lower case, ``a['b']`` as ``a.b``, no space around a dot: easy to match."""
    text = BRACKET.sub(lambda m: "." + m.group(1), text.lower())
    return re.sub(r"\s*\.\s*", ".", text)


def expressions(text: str) -> list[str]:
    """The bodies of the ``${{ }}`` expressions of a text; ``}}`` inside quotes does not end one."""
    bodies = []
    start = text.find("${{")
    while start != -1:
        i = start + 3
        quoted = False
        while i < len(text):
            if text[i] == "'":
                quoted = not quoted  # a doubled quote closes and opens again: still right
            elif not quoted and text.startswith("}}", i):
                break
            i += 1
        bodies.append(text[start + 3 : i])
        start = text.find("${{", i + 2)
    return bodies


def reads_secret(expression: str) -> bool:
    """True when the body of a ``${{ }}`` reads the secrets or the token of the workflow."""
    plain = STRING_LITERAL.sub("''", normal(expression))
    return bool(SECRET_CONTEXT.search(plain) or GITHUB_TOKEN.search(plain))


def shorten(text: str) -> str:
    one = " ".join(text.split())
    return one if len(one) <= STEP_OUTPUT_LIMIT else one[: STEP_OUTPUT_LIMIT - 3] + "..."


def commands(script: str, depth: int = 0) -> Iterator[list[str]]:
    """The commands of a ``run`` script (``bash -c '...'`` is read too); nothing is executed."""
    for line in cc.logical_lines(script):
        for kind, words in cc.shell_events(line):
            if kind != "cmd":
                continue
            words = cc.unwrap(words)
            if not words:
                continue
            yield words
            name = posixpath.basename(words[0])
            if name in SHELLS and depth < 3:
                for i, word in enumerate(words[1:-1], start=1):
                    if word.startswith("-") and not word.startswith("--") and "c" in word:
                        yield from commands(words[i + 1], depth + 1)
                        break


def git_subcommand(words: list[str]) -> tuple[str, list[str]] | None:
    """``("push", rest)`` for ``git -C dir push origin``; None when it is not git."""
    if posixpath.basename(words[0]) != "git":
        return None
    index = cc.skip_options(words, 1, GIT_VALUE_OPTIONS)
    if index >= len(words):
        return None
    return words[index], words[index + 1 :]


def gh_pr_checkout(words: list[str]) -> bool:
    """True for ``gh pr checkout`` (also ``gh --repo o/r pr checkout``)."""
    index = cc.skip_options(words, 1, GH_VALUE_OPTIONS)
    return tuple(words[index : index + 2]) == GH_PR_CHECKOUT


def pushes(step: cc.Map) -> bool:
    """True when the step can push to the repo (``git push``, or an action that does)."""
    uses = cc.text_of(step.get("uses")) or ""
    if uses.split("@", 1)[0].lower() in PUSH_ACTIONS:
        return True
    run = cc.text_of(step.get("run"))
    if run is None:
        return False
    return any((git_subcommand(w) or ("",))[0] == "push" for w in commands(run))


# ---------------------------------------------------------------------------
# Reading a file
# ---------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    node: cc.Map
    steps: list[cc.Map]
    env: dict[str, str]


@dataclass
class Document:
    path: str
    root: cc.Map
    composite: bool
    jobs: list[Job]
    env: dict[str, str]


def env_of(node: object) -> dict[str, str]:
    env = cc.as_map(cc.as_map(node).get("env"))
    return {str(k): v for k, v in ((k, cc.text_of(x)) for k, x in env.items()) if v is not None}


def steps_of(node: object) -> list[cc.Map]:
    steps = cc.as_map(node).get("steps")
    return [cc.as_map(s) for s in steps] if isinstance(steps, cc.Seq) else []


def read_document(path: str, text: str) -> Document:
    """The jobs and steps of a workflow or of a composite action (raises ``YamlError``)."""
    root = cc.as_map(cc.parse_yaml(text))
    if posixpath.basename(path).startswith("action."):
        runs = cc.as_map(root.get("runs"))
        jobs = [Job("", runs, steps_of(runs), {})] if runs.get("using") == "composite" else []
        return Document(path, root, True, jobs, {})
    jobs = [
        Job(str(job_id), cc.as_map(node), steps_of(node), env_of(node))
        for job_id, node in cc.as_map(root.get("jobs")).items()
    ]
    return Document(path, root, False, jobs, env_of(root))


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def pin_problem(uses: str) -> str | None:
    """Why ``uses`` is not pinned; None when it is."""
    if "${{" in uses:
        return "uses has an expression, so its version cannot be checked"
    if uses.startswith("./"):
        return None
    if uses.startswith("docker://"):
        image = uses[len("docker://") :]
        if "@" in image:
            if DOCKER_DIGEST.fullmatch(image.split("@", 1)[1]):
                return None
            return f"{uses} has a digest that is not sha256 and 64 hex digits"
        tag = image.rsplit("/", 1)[-1].partition(":")[2]
        if tag in ("", "latest"):
            return f"{uses} has no tag other than latest and no digest"
        return None
    name, at, ref = uses.rpartition("@")
    if not at:
        return f"{uses} has no @ref: pin it to a version tag or a full commit SHA"
    if "/" not in name or not all(name.split("/")[:2]):
        return f"{uses} is not written owner/repo@ref"
    if SHA.fullmatch(ref):
        return None
    if SHORT_SHA.fullmatch(ref):
        return f"{uses} is pinned to a short SHA: use the full 40-digit commit SHA"
    if TAG.fullmatch(ref):
        return None
    if ref.lower() in BRANCH_NAMES or "/" in ref:
        return f"{uses} is pinned to a branch ({ref}): use a version tag or a full commit SHA"
    return f"{uses} is not pinned to a version tag or a full commit SHA"


def check_pins(doc: Document) -> list[Problem]:
    problems = []
    for job in doc.jobs:
        top = cc.text_of(job.node.get("uses"))
        if top is not None and not doc.composite:
            why = pin_problem(top)
            if why:
                problems.append(found(Where(doc.path, job.id, None, job.node.line), "pin", why))
        for index, step in enumerate(job.steps, start=1):
            uses = cc.text_of(step.get("uses"))
            why = pin_problem(uses) if uses is not None else None
            if why:
                where = Where(doc.path, job.id, cc.step_label(step, index), step.line)
                problems.append(found(where, "pin", why))
    return problems


def check_permissions(doc: Document) -> list[Problem]:
    if doc.composite:
        return []
    problems = []
    where = Where(doc.path)
    if "permissions" not in doc.root:
        problems.append(
            found(
                where,
                "permissions",
                "the workflow has no top-level permissions: (add `permissions: contents: read`)",
            )
        )
    for owner, node in [("", doc.root)] + [(j.id, j.node) for j in doc.jobs]:
        if "permissions" not in node:
            continue
        value = node["permissions"]
        here = Where(doc.path, owner)
        text = cc.text_of(value)
        if text is not None and text.strip() == "write-all":
            problems.append(found(here, "permissions", "permissions: write-all gives every scope"))
        elif (text is not None and text.strip() != "read-all") or isinstance(value, cc.Seq):
            problems.append(
                found(
                    here,
                    "permissions",
                    "permissions: must be read-all, write-all or a map (`{}` for none)",
                )
            )
    return problems


def tainted_names(*envs: dict[str, str]) -> set[str]:
    """Names of ``env`` variables whose value comes from the pull request."""
    return {name for env in envs for name, value in env.items() if PR_CODE.search(normal(value))}


def takes_pr_code(text: str, tainted: set[str]) -> bool:
    plain = normal(text)
    if PR_CODE.search(plain):
        return True
    return any(
        re.search(rf"(?<![\w.-])env\.{re.escape(n.lower())}(?![\w-])", plain)
        or re.search(rf"\$\{{?{re.escape(n)}(?![A-Za-z0-9_])", text)
        for n in tainted
    )


def check_pr_target(doc: Document) -> list[Problem]:
    if doc.composite or "pull_request_target" not in cc.trigger_names(doc.root.get("on")):
        return []
    problems = []
    for job in doc.jobs:
        for index, step in enumerate(job.steps, start=1):
            where = Where(doc.path, job.id, cc.step_label(step, index), step.line)
            tainted = tainted_names(doc.env, job.env, env_of(step))
            uses = cc.text_of(step.get("uses")) or ""
            if CHECKOUT.match(uses):
                inputs = cc.as_map(step.get("with"))
                for key in ("ref", "repository"):
                    value = cc.text_of(inputs.get(key))
                    if value is not None and takes_pr_code(value, tainted):
                        problems.append(
                            found(
                                where,
                                "pr-target",
                                f"pull_request_target checks out code of the pull request "
                                f"(with: {key}): it would run with the secrets of the base repo",
                            )
                        )
            run = cc.text_of(step.get("run"))
            for words in commands(run) if run is not None else ():
                git = git_subcommand(words)
                if posixpath.basename(words[0]) == "gh" and gh_pr_checkout(words):
                    why = "gh pr checkout"
                elif git and git[0] in GIT_PR_COMMANDS and takes_pr_code(" ".join(git[1]), tainted):
                    why = f"git {git[0]}"
                else:
                    continue
                problems.append(
                    found(
                        where,
                        "pr-target",
                        f"pull_request_target gets code of the pull request with `{why}`: "
                        "it would run with the secrets of the base repo",
                    )
                )
    return problems


def secret_names(*envs: dict[str, str]) -> set[str]:
    """Names of ``env`` variables whose value is a secret (``env.NAME`` in a run pastes it)."""
    return {
        name
        for env in envs
        for name, value in env.items()
        if any(reads_secret(body) for body in expressions(value))
    }


def reads_env(expression: str, names: set[str]) -> bool:
    plain = STRING_LITERAL.sub("''", normal(expression))
    return any(re.search(rf"(?<![\w.-])env\.{re.escape(n.lower())}(?![\w-])", plain) for n in names)


def check_secrets(doc: Document) -> list[Problem]:
    problems = []
    for job in doc.jobs:
        for index, step in enumerate(job.steps, start=1):
            run = cc.text_of(step.get("run"))
            if run is None:
                continue
            names = secret_names(doc.env, job.env, env_of(step))
            for body in expressions(run):
                if reads_secret(body) or reads_env(body, names):
                    where = Where(doc.path, job.id, cc.step_label(step, index), step.line)
                    problems.append(
                        found(
                            where,
                            "secret-in-run",
                            f"run: has `${{{{ {shorten(body)} }}}}`: the secret lands in the "
                            "command line; pass it through env: and read the variable",
                        )
                    )
    return problems


def check_checkout(doc: Document) -> list[Problem]:
    problems = []
    for job in doc.jobs:
        for index, step in enumerate(job.steps, start=1):
            if not CHECKOUT.match(cc.text_of(step.get("uses")) or ""):
                continue
            where = Where(doc.path, job.id, cc.step_label(step, index), step.line)
            value = cc.text_of(cc.as_map(step.get("with")).get("persist-credentials"))
            if value in FALSE_VALUES:
                continue
            if any(pushes(later) for later in job.steps[index:]):
                continue
            if value is None:
                problems.append(
                    found(
                        where,
                        "persist-credentials",
                        "actions/checkout keeps the token in .git/config but no later step "
                        "pushes: add `persist-credentials: false`",
                    )
                )
            elif value.strip().lower() == "true":
                problems.append(
                    found(
                        where,
                        "persist-credentials-true",
                        "persist-credentials: true but no later step of the job pushes (a push "
                        "inside a script is not seen); use false if nothing pushes",
                        warning=True,
                    )
                )
            else:
                problems.append(
                    found(
                        where,
                        "persist-credentials",
                        f"persist-credentials: {shorten(value)} is not false (write false, "
                        "False or FALSE) and no later step pushes",
                    )
                )
    return problems


RULES = (check_pins, check_permissions, check_pr_target, check_secrets, check_checkout)


# ---------------------------------------------------------------------------
# The whole repo
# ---------------------------------------------------------------------------


def is_target(path: str) -> bool:
    folder, name = posixpath.split(path)
    if not name.endswith((".yml", ".yaml")):
        return False
    if folder == WORKFLOW_DIR:
        return True
    return (
        folder.startswith(ACTION_DIR + "/")
        and folder.count("/") == ACTION_DIR.count("/") + 1
        and name.rsplit(".", 1)[0] == "action"
    )


def apply_legacy(problems: list[Problem], complete: bool) -> list[Problem]:
    """Known old problems become warnings; a problem over the LEGACY number stays an error."""
    seen: dict[tuple[str, str], int] = {}
    result = []
    for item in problems:
        key = (item.path, item.rule)
        seen[key] = seen.get(key, 0) + 1
        allowed = LEGACY.get(key, 0)
        if item.warning:
            result.append(item)
        elif seen[key] > allowed:
            suffix = f" (LEGACY allows only {allowed} {item.rule} problem(s) in this file)"
            result.append(
                Problem(item.path, item.message + (suffix if allowed else ""), False, item.rule)
            )
        else:
            result.append(Problem(item.path, item.message, True, item.rule))
    if complete:
        for (path, rule), allowed in sorted(LEGACY.items()):
            count = seen.get((path, rule), 0)
            if count < allowed:
                result.append(
                    Problem(
                        path,
                        f"LEGACY lists {allowed} {rule} problem(s) but only {count} found: "
                        "lower the number or delete the line",
                        True,
                        "legacy",
                    )
                )
    return result


def check(
    root: Path, tracked: Sequence[str], only: Sequence[str] | None = None
) -> tuple[list[Problem], dict[str, int]]:
    paths = sorted(p for p in tracked if is_target(p)) if only is None else sorted(only)
    problems: list[Problem] = []
    stats = {"files": 0, "jobs": 0, "steps": 0}
    for path in paths:
        try:
            doc = read_document(path, (root / path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, cc.YamlError) as err:
            problems.append(Problem(path, f"cannot read the workflow: {err}", rule="yaml"))
            continue
        stats["files"] += 1
        stats["jobs"] += len(doc.jobs)
        stats["steps"] += sum(len(j.steps) for j in doc.jobs)
        for rule in RULES:
            problems += rule(doc)
    problems = apply_legacy(problems, complete=only is None)
    problems.sort(key=lambda p: (p.warning, p.path, p.message))
    return problems, stats


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=root, check=True, capture_output=True, text=True
    ).stdout
    return out.splitlines()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args in (["-h"], ["--help"]):
        print("usage: python3 tools/check_actions.py [FILE ...]")
        return 0
    if any(a.startswith("-") for a in args):
        print("usage: python3 tools/check_actions.py [FILE ...]", file=sys.stderr)
        return 2
    only = [posixpath.normpath(a) for a in args] or None
    if only:
        for path in only:
            if not (ROOT / path).is_file():
                print(f"{path}: no such file", file=sys.stderr)
                return 2
    try:
        tracked = tracked_files(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    problems, stats = check(ROOT, tracked, only)
    for item in problems:
        print(item, file=sys.stderr)
    errors = sum(not p.warning for p in problems)
    print(
        f"{stats['files']} workflows and actions ({stats['jobs']} jobs, {stats['steps']} steps) "
        f"checked: {errors} errors, {len(problems) - errors} warnings"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
