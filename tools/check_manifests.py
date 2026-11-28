#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the links between the Herkos manifests and the rest of the repo.

``herkos/src/herkos/manifests/*.toml`` are checked one by one by ``manifest.py`` (its
syntax, the levels, the names). This tool checks what ``manifest.py`` cannot see: the
links of a manifest to the MCP server, the polkit policy and the systemd unit.

For every tracked manifest it looks for the server of the tool and reads the tools of that
server with ``ast``. Nothing of the server is imported or executed. The server is found

* by ``[launch] module`` (the folder of that Python package in the repo), or, if the
  manifest names no module,
* by ``[launch] program``: a tracked ``pyproject.toml`` with that name in
  ``[project.scripts]`` gives ``package.module:function``, and the tools are read from that
  module only (one package may hold several programs).

A tool of the server is a call ``_tool("name", description, {properties}, ...)`` (or
``tool(...)``) or a dict that has ``"name"`` and ``"inputSchema"``. The properties and the
annotations (``readOnlyHint=True`` or ``**READ``) are read when they are literals or module
constants of the same package; whatever cannot be read is skipped, never guessed.

Errors (exit code 1):

* an ``[operations.<x>]`` that is not forbidden has no tool ``<x>`` in the server, or a tool
  of the server has no ``[operations.<x>]`` (a ``forbidden`` operation needs no tool: the
  server may not have it at all);
* a ``resource_args`` name that is not a property of the ``inputSchema`` of the tool;
* ``destructiveHint=True`` on a tool whose level is lower than R3;
* a ``polkit`` action of an operation that no tracked ``*.policy`` declares;
* a manifest that cannot be read as TOML.

Warnings (``path: warning: ...``, they do not fail the check):

* no server found for the manifest (no ``[launch]``, a module or a program that is not in
  the repo, such as a tool that is Rust code of herkosd), or its code cannot be parsed;
* ``readOnlyHint=True`` on a tool whose level is not R1; level R1 with ``undoable = true``;
* no unit ``<program>@.service`` (or ``<program>.service``) among the tracked systemd units;
* known old violations (``LEGACY``).

Then a table "manifest | server | operations | levels" is printed. Only ``git ls-files`` is
run; nothing else is executed and nothing is written.

Run: python3 tools/check_manifests.py [MANIFEST.toml ...]
Without arguments every tracked manifest is checked. Paths are from the repo root.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import ast
import posixpath
import re
import subprocess
import sys
import tomllib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

MANIFEST_DIR = "herkos/src/herkos/manifests"
FORBIDDEN = "forbidden"  # the level of an operation Herkos never carries out
# The helper that builds a tool in the servers: mcp/*/server.py has _tool, mcp/desktop has tool.
TOOL_FUNCTIONS = ("_tool", "tool")
# Programs of the third-party catalogue run in their own units (manifest.py, EXT_PREFIX).
EXT_PREFIX = "vilna-mcp-ext-"
ACTION_ID = re.compile(r"<action\s+id\s*=\s*[\"']([^\"']+)[\"']")
LEVEL_NAME = re.compile(r"R[0-4]")
MAX_RESOLVE_DEPTH = 5

# Problems the repo already has today: (manifest path, rule, name of the operation or of
# the tool; "" for the whole manifest). They are warnings, so that the check can be switched
# on in CI; fix the manifest or the server, then delete the line.
LEGACY: frozenset[tuple[str, str, str]] = frozenset(
    {
        # Operations "for the person, not for Gloxi": the manifest lists them only to fix
        # their levels, and the server does not offer them (see the comment in the manifest).
        *(
            (f"{MANIFEST_DIR}/org.vilna.mcp.backup.toml", "operation-no-tool", name)
            for name in (
                "add_device",
                "block_device",
                "change_destination",
                "configure_destination",
                "forget",
                "reissue_repository",
                "restore_in_place",
                "rotate_keys",
            )
        ),
        # destructiveHint=True with R2: Herkos takes the higher of the hint (R3) and the
        # manifest (permissions.md 4.1), so the effective level is R3, but the manifest says R2.
        (f"{MANIFEST_DIR}/org.vilna.mcp.atspi.toml", "destructive-level", "activate"),
        (f"{MANIFEST_DIR}/org.vilna.mcp.mail.toml", "destructive-level", "restore"),
    }
)


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False
    rule: str = ""
    name: str = ""

    def __str__(self) -> str:
        kind = "warning: " if self.warning else ""
        return f"{self.path}: {kind}{self.message}"


@dataclass(frozen=True)
class Tool:
    """A tool of an MCP server as the source code says it (``None``: not readable)."""

    name: str
    properties: frozenset[str] | None = None
    hints: Mapping[str, object] = field(default_factory=dict)


@dataclass
class Server:
    where: str  # a folder or a .py file from the repo root
    tools: dict[str, Tool] = field(default_factory=dict)


@dataclass
class Row:
    manifest: str
    server: str
    operations: str
    levels: str


# --- reading the code of a server (ast only) ------------------------------------


def module_constants(tree: ast.Module) -> dict[str, ast.expr]:
    """The module-level ``NAME = value`` assignments."""
    found: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and isinstance(node.target, ast.Name)
        ):
            found[node.target.id] = node.value
    return found


def resolve_dict(
    node: ast.expr | None, consts: Mapping[str, ast.expr], depth: int = 0
) -> dict[str, ast.expr] | None:
    """A dict literal (or a constant that is one) as {key: value node}; None if unreadable."""
    if depth > MAX_RESOLVE_DEPTH:
        return None
    if isinstance(node, ast.Name) and node.id in consts:
        return resolve_dict(consts[node.id], consts, depth + 1)
    if not isinstance(node, ast.Dict):
        return None
    out: dict[str, ast.expr] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        if key is None:  # {**other}
            merged = resolve_dict(value, consts, depth + 1)
            if merged is None:
                return None
            out.update(merged)
        elif isinstance(key, ast.Constant) and isinstance(key.value, str):
            out[key.value] = value
        else:
            return None
    return out


def literal_hints(items: Mapping[str, ast.expr]) -> dict[str, object]:
    """The ``name: True/False`` pairs of an annotations dict; other values are dropped."""
    return {
        k: v.value for k, v in items.items() if isinstance(v, ast.Constant) and v.value is not None
    }


def property_names(props: ast.expr | None, consts: Mapping[str, ast.expr]) -> frozenset[str] | None:
    items = resolve_dict(props, consts)
    return None if items is None else frozenset(items)


def tool_from_call(call: ast.Call, consts: Mapping[str, ast.expr]) -> Tool | None:
    """``_tool("name", "description", {props}, [required], **annotations)``."""
    first = call.args[0] if call.args else None
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return None
    if len(call.args) < 2:  # the name and the description at least
        return None
    props = property_names(call.args[2], consts) if len(call.args) > 2 else None
    hints: dict[str, object] = {}
    for kw in call.keywords:
        if kw.arg is None:  # **READ
            items = resolve_dict(kw.value, consts)
            hints.update(literal_hints(items or {}))
        elif isinstance(kw.value, ast.Constant) and kw.value.value is not None:
            hints[kw.arg] = kw.value.value
    return Tool(first.value, props, hints)


def tool_from_dict(node: ast.Dict, consts: Mapping[str, ast.expr]) -> Tool | None:
    """``{"name": "x", "inputSchema": {"properties": {...}}, "annotations": {...}}``."""
    items = resolve_dict(node, consts)
    if items is None or "inputSchema" not in items:
        return None
    name = items.get("name")
    if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
        return None
    schema = resolve_dict(items["inputSchema"], consts)
    props = property_names(schema.get("properties"), consts) if schema else None
    hints = literal_hints(resolve_dict(items.get("annotations"), consts) or {})
    return Tool(name.value, props, hints)


def find_tools(tree: ast.Module, consts: Mapping[str, ast.expr]) -> list[Tool]:
    tools: list[Tool] = []
    for node in ast.walk(tree):
        tool: Tool | None = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in TOOL_FUNCTIONS:
                tool = tool_from_call(node, consts)
        elif isinstance(node, ast.Dict):
            tool = tool_from_dict(node, consts)
        if tool is not None:
            tools.append(tool)
    return tools


def read_server(where: str, sources: Mapping[str, str]) -> tuple[Server, list[str]]:
    """The tools of the server whose code is ``sources`` (path -> text), and notes (warnings)."""
    server = Server(where)
    notes: list[str] = []
    trees: dict[str, ast.Module] = {}
    for path, text in sorted(sources.items()):
        try:
            trees[path] = ast.parse(text, filename=path)
        except (SyntaxError, ValueError) as err:
            notes.append(f"cannot parse {path}: {err}")
        except (RecursionError, MemoryError) as err:  # nested too deep for the parser
            notes.append(f"cannot parse {path}: too complex ({type(err).__name__})")
    shared: dict[str, ast.expr] = {}
    for tree in trees.values():
        for name, value in module_constants(tree).items():
            shared.setdefault(name, value)
    for path, tree in trees.items():
        consts = {**shared, **module_constants(tree)}
        for tool in find_tools(tree, consts):
            if tool.name in server.tools:
                notes.append(f"the tool '{tool.name}' is defined twice (also in {path})")
                continue
            server.tools[tool.name] = tool
    return server, notes


# --- finding the server, the policy and the unit --------------------------------


def module_candidates(module: str, py_files: Sequence[str]) -> list[str]:
    """Where the module is: a package folder or a ``.py`` file (paths from the repo root)."""
    tail = "/".join(module.split("."))
    found: set[str] = set()
    for path in py_files:
        for ending, drop in ((f"{tail}/__init__.py", "/__init__.py"), (f"{tail}.py", "")):
            if path == ending or path.endswith("/" + ending):
                found.add(path.removesuffix(drop) if drop else path)
    return sorted(found)


def server_files(where: str, py_files: Sequence[str]) -> list[str]:
    """The ``.py`` files of the server: the file itself, or all files of the package folder."""
    if where.endswith(".py"):
        return [where]
    return sorted(p for p in py_files if p.startswith(where + "/") and "/tests/" not in p)


def script_modules(root: Path, tracked: Sequence[str]) -> dict[str, str]:
    """``program -> module`` from ``[project.scripts]`` of every tracked pyproject.toml."""
    out: dict[str, str] = {}
    for rel in sorted(p for p in tracked if posixpath.basename(p) == "pyproject.toml"):
        try:
            data = tomllib.loads((root / rel).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        scripts = data.get("project", {}).get("scripts", {})
        if not isinstance(scripts, dict):
            continue
        for program, target in scripts.items():
            if isinstance(target, str):
                out.setdefault(program, target.partition(":")[0].strip())
    return out


def policy_actions(root: Path, tracked: Sequence[str]) -> set[str]:
    """Every action id that a tracked ``*.policy`` file declares."""
    actions: set[str] = set()
    for rel in tracked:
        if rel.endswith(".policy"):
            try:
                text = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            actions.update(ACTION_ID.findall(text))
    return actions


def unit_names(program: str) -> tuple[str, str]:
    return f"{program}@.service", f"{program}.service"


# --- checking one manifest ------------------------------------------------------


def operation_level(op: Mapping[str, object]) -> str | None:
    level = op.get("level")
    if level == FORBIDDEN:
        return FORBIDDEN
    if isinstance(level, str) and LEVEL_NAME.fullmatch(level):
        return level
    return None


def check_server_link(
    path: str, operations: Mapping[str, Mapping[str, object]], server: Server
) -> list[Problem]:
    """The names of the operations against the names of the tools; resource_args."""
    problems: list[Problem] = []
    for name, op in sorted(operations.items()):
        if operation_level(op) == FORBIDDEN:
            continue
        if name not in server.tools:
            problems.append(
                Problem(
                    path,
                    f"operations.{name}: the server {server.where} has no tool '{name}'",
                    rule="operation-no-tool",
                    name=name,
                )
            )
    for name in sorted(server.tools):
        if name not in operations:
            problems.append(
                Problem(
                    path,
                    f"the tool '{name}' of the server {server.where} has no [operations.{name}]",
                    rule="tool-no-operation",
                    name=name,
                )
            )
    for name, op in sorted(operations.items()):
        tool = server.tools.get(name)
        if tool is None or tool.properties is None:
            continue
        args = op.get("resource_args")
        for arg in args if isinstance(args, list) else []:
            if isinstance(arg, str) and arg not in tool.properties:
                problems.append(
                    Problem(
                        path,
                        f"operations.{name}: resource_args '{arg}' is not a property of "
                        f"the tool '{name}' (it has: {', '.join(sorted(tool.properties)) or '-'})",
                        rule="resource-arg",
                        name=name,
                    )
                )
    return problems


def check_levels(
    path: str, operations: Mapping[str, Mapping[str, object]], server: Server | None
) -> list[Problem]:
    """The annotations of the tools and the flags of the operations against the level."""
    problems: list[Problem] = []
    for name, op in sorted(operations.items()):
        level = operation_level(op)
        if level is None or level == FORBIDDEN:
            continue
        tool = server.tools.get(name) if server else None
        hints = tool.hints if tool else {}
        if hints.get("readOnlyHint") is True and level != "R1":
            why = (
                "R0 is for neutral things: check that it reads nothing personal"
                if level == "R0"
                else "the tool is stricter than its own hint"
            )
            problems.append(
                Problem(
                    path,
                    f"operations.{name}: the tool says readOnlyHint=True but the level is "
                    f"{level}, not R1 ({why})",
                    warning=True,
                    rule="read-only-level",
                    name=name,
                )
            )
        if hints.get("destructiveHint") is True and level in ("R0", "R1", "R2"):
            problems.append(
                Problem(
                    path,
                    f"operations.{name}: the tool says destructiveHint=True but the level is "
                    f"{level}, lower than R3",
                    rule="destructive-level",
                    name=name,
                )
            )
        if level == "R1" and op.get("undoable") is True:
            problems.append(
                Problem(
                    path,
                    f"operations.{name}: level R1 with undoable = true (a read has nothing "
                    "to undo)",
                    warning=True,
                    rule="undoable-read",
                    name=name,
                )
            )
    return problems


def check_polkit(
    path: str, operations: Mapping[str, Mapping[str, object]], actions: set[str]
) -> list[Problem]:
    problems: list[Problem] = []
    for name, op in sorted(operations.items()):
        action = op.get("polkit")
        if isinstance(action, str) and action not in actions:
            problems.append(
                Problem(
                    path,
                    f"operations.{name}: the polkit action {action} is not declared in any "
                    "tracked .policy file",
                    rule="polkit-action",
                    name=name,
                )
            )
    return problems


def check_unit(path: str, program: str, units: set[str]) -> list[Problem]:
    if program.startswith(EXT_PREFIX) or any(n in units for n in unit_names(program)):
        return []
    return [
        Problem(
            path,
            f"launch.program '{program}' has no systemd unit {unit_names(program)[0]} "
            "among the tracked units",
            warning=True,
            rule="systemd-unit",
        )
    ]


@dataclass
class Repo:
    """What the checks need to know about the repo (paths are from its root)."""

    root: Path
    tracked: Sequence[str]
    py_files: list[str] = field(default_factory=list)
    scripts: dict[str, str] = field(default_factory=dict)
    actions: set[str] = field(default_factory=set)
    units: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, root: Path, tracked: Sequence[str]) -> Repo:
        return cls(
            root=root,
            tracked=tracked,
            py_files=sorted(p for p in tracked if p.endswith(".py")),
            scripts=script_modules(root, tracked),
            actions=policy_actions(root, tracked),
            units={posixpath.basename(p) for p in tracked if p.endswith(".service")},
        )


def locate_server(path: str, launch: object, repo: Repo) -> tuple[Server | None, list[Problem]]:
    """The server of a manifest, read with ast; a warning if there is none."""

    def missing(why: str) -> tuple[None, list[Problem]]:
        return None, [Problem(path, f"no server found: {why}", True, "server-not-found")]

    if not isinstance(launch, dict):
        return missing("the manifest has no [launch]")
    module = launch.get("module")
    program = launch.get("program")
    via = "launch.module"
    if not isinstance(module, str):
        module = repo.scripts.get(program) if isinstance(program, str) else None
        via = "launch.program in a pyproject.toml"
        if module is None:
            what = (
                f"program '{program}' is not in any [project.scripts]"
                if program
                else ("[launch] has neither module nor program")
            )
            return missing(what)
    found = module_candidates(module, repo.py_files)
    if not found:
        return missing(f"the module '{module}' ({via}) is not in the repo")
    problems: list[Problem] = []
    if len(found) > 1:
        problems.append(
            Problem(
                path,
                f"the module '{module}' is in several places ({', '.join(found)}); "
                f"the first one is used",
                True,
                "server-not-found",
            )
        )
    where = found[0]
    sources: dict[str, str] = {}
    for rel in server_files(where, repo.py_files):
        try:
            sources[rel] = (repo.root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            problems.append(Problem(path, f"cannot read {rel}: {err}", True, "server-not-found"))
    server, notes = read_server(where, sources)
    problems += [Problem(path, f"server {where}: {n}", True, "server-not-found") for n in notes]
    return server, problems


def levels_text(operations: Mapping[str, Mapping[str, object]]) -> str:
    counts = Counter(operation_level(op) or "?" for op in operations.values())
    return ", ".join(f"{level}×{n}" for level, n in sorted(counts.items())) or "-"


def check_manifest(path: str, data: Mapping[str, object], repo: Repo) -> tuple[list[Problem], Row]:
    raw_ops = data.get("operations")
    operations: dict[str, Mapping[str, object]] = (
        {k: v for k, v in raw_ops.items() if isinstance(v, dict)}
        if isinstance(raw_ops, dict)
        else {}
    )
    launch = data.get("launch")
    server, problems = locate_server(path, launch, repo)
    if server is not None:
        problems += check_server_link(path, operations, server)
    problems += check_levels(path, operations, server)
    problems += check_polkit(path, operations, repo.actions)
    program = launch.get("program") if isinstance(launch, dict) else None
    if isinstance(program, str):
        problems += check_unit(path, program, repo.units)
    tools = str(len(server.tools)) if server else "-"
    row = Row(
        manifest=posixpath.basename(path).removesuffix(".toml"),
        server=server.where if server else "-",
        operations=f"{len(operations)}/{tools}",
        levels=levels_text(operations),
    )
    return [downgrade(p, path) for p in problems], row


def downgrade(problem: Problem, path: str) -> Problem:
    """Turn a known old violation (see LEGACY) into a warning."""
    if (path, problem.rule, problem.name) in LEGACY or (path, problem.rule, "") in LEGACY:
        return Problem(problem.path, problem.message, True, problem.rule, problem.name)
    return problem


# --- checking the repo ----------------------------------------------------------


def manifest_paths(tracked: Sequence[str]) -> list[str]:
    return sorted(
        p for p in tracked if posixpath.dirname(p) == MANIFEST_DIR and p.endswith(".toml")
    )


def check_files(
    root: Path, tracked: Sequence[str], only: Sequence[str] = ()
) -> tuple[list[Problem], list[Row]]:
    """Check the manifests among ``tracked``; ``only`` limits which ones."""
    repo = Repo.load(root, tracked)
    problems: list[Problem] = []
    rows: list[Row] = []
    for rel in only or manifest_paths(tracked):
        try:
            data = tomllib.loads((root / rel).read_text(encoding="utf-8"))
        except OSError as err:
            problems.append(Problem(rel, f"cannot read the file: {err.strerror or err}"))
            continue
        except UnicodeDecodeError:
            problems.append(Problem(rel, "the file is not valid UTF-8"))
            continue
        except tomllib.TOMLDecodeError as err:
            problems.append(Problem(rel, f"not valid TOML: {err}"))
            continue
        found, row = check_manifest(rel, data, repo)
        problems += found
        rows.append(row)
    return problems, rows


def table(rows: Sequence[Row]) -> str:
    """A Markdown table: manifest | server | operations (manifest/server) | levels."""
    header = ["Manifest", "Server", "Operations (manifest/server)", "Levels"]
    lines = [header, ["---"] * len(header)]
    lines += [
        [r.manifest, f"`{r.server}`" if r.server != "-" else "-", r.operations, r.levels]
        for r in rows
    ]
    return "\n".join("| " + " | ".join(line) + " |" for line in lines)


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, text=True, errors="surrogateescape"
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    usage = "usage: python3 tools/check_manifests.py [MANIFEST.toml ...]"
    options = [a for a in args if a.startswith("-")]
    if any(o not in ("-h", "--help") for o in options):
        print(usage, file=sys.stderr)
        return 2
    if options:
        print(usage, file=sys.stderr)
        return 0
    try:
        tracked = tracked_files(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    only = [a.replace("\\", "/") for a in args]
    known = manifest_paths(tracked)
    unknown = [a for a in only if a not in known]
    if unknown:
        print(f"not a tracked Herkos manifest: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if not known:
        print(f"no tracked manifests in {MANIFEST_DIR}", file=sys.stderr)
        return 2
    problems, rows = check_files(ROOT, tracked, only)
    for problem in problems:
        print(problem, file=sys.stderr)
    print(table(rows))
    errors = sum(not p.warning for p in problems)
    print(f"{len(rows)} manifests checked: {errors} errors, {len(problems) - errors} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    # A file name that is not UTF-8 is printed escaped, not as a traceback.
    sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    sys.exit(main())
