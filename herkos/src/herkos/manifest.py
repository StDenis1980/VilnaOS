# SPDX-License-Identifier: GPL-3.0-or-later
"""Tool manifests: what each tool operation is allowed to be (herkos/docs/manifests.md).

A manifest is a TOML file, one per tool::

    tool = "org.vilna.mcp.files"
    description = "Files in the folders the user allowed"

    [operations.read]
    level = "R1"
    summary = "Read a file"
    returns_external = true

    [operations.write]
    level = "R2"
    summary = "Change a file"
    undoable = true
    resource_args = ["path"]

Anything the manifest does not name is denied (permissions.md, principle 8).
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from herkos.risk import Level

FORBIDDEN = "forbidden"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
# A program on PATH (and, for the system Herkos, its systemd unit): no paths.
_PROGRAM = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_MODULE = re.compile(r"^[a-z_][a-z0-9_]{0,63}(\.[a-z_][a-z0-9_]{0,63}){0,3}$")
# What Herkos may pass a tool when it starts it, in this order:
# backup_dir -> --backup-dir DIR, roots -> --root DIR for each allowed folder.
LAUNCH_ARGS = ("backup_dir", "roots")
_LAUNCH_KEYS = {"program", "module", "args", "flatpak", "command", "fixed", "root_flags"}
# A program from Flatpak runs in the unit vilna-mcp-ext-<name>@<uid> (ADR 0017, step 2).
EXT_PREFIX = "vilna-mcp-ext-"
_APP_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z0-9_][A-Za-z0-9_-]*){2,}")
_FIXED = re.compile(r"(--?)?[a-z0-9][a-z0-9._-]{0,63}")
_ROOT_FLAG = re.compile(r"--[a-z0-9][a-z0-9-]{0,63}")
_MAX_FIXED = 16
_MAX_ROOT_FLAGS = 4
# The catalogue of manifests installed with the system (ADR 0017, stage 2):
# package herkos-manifests. HERKOS_MANIFEST_DIR replaces it (tests).
SYSTEM_MANIFEST_DIR = Path("/usr/share/herkos/manifests")
MAX_MANIFEST_FILES = 256
MAX_MANIFEST_BYTES = 64 * 1024
_OPERATION_KEYS = {
    "level",
    "summary",
    "undoable",
    "reversible",
    "returns_external",
    "egress",
    "resource_args",
    "command_arg",
    "command_key",
}
# The program's own commands, run through one generic operation such as
# "execute" (ADR 0017, step 7): an id ("text.insert") or a group ("home.*").
_COMMAND = re.compile(r"[A-Za-z0-9_-]{1,64}(\.[A-Za-z0-9_-]{1,64}){0,7}")
_GROUP_SUFFIX = ".*"
MAX_COMMANDS = 4096
# At most this many commands in one call (a batch); more is not read.
MAX_CALL_COMMANDS = 64


class ManifestError(Exception):
    """A manifest file is malformed. The tool stays unknown (and so denied)."""


@dataclass(frozen=True)
class Operation:
    name: str
    level: Level | None  # None: forbidden even with confirmation
    summary: str
    # The tool can take a backup before the change, so Mitos can undo it.
    undoable: bool = False
    # The change loses nothing, so it needs no backup to be undone: it only
    # adds (a new snapshot in an append-only place, files in a new folder) or
    # sets something that can be set back. Spares it the "cannot be undone"
    # raise to R3 (permissions.md 4.2; first used for backups, ADR 0007).
    reversible: bool = False
    # The result carries content Herkos did not write (files, mail, web):
    # after it, the session is tainted (permissions.md, section 6).
    returns_external: bool = False
    # Data leaves the device (network, cloud, another person).
    egress: bool = False
    # Arguments that name files: each must be one of the request's declared
    # resources, so the model cannot declare one path and touch another.
    resource_args: tuple[str, ...] = ()
    # The argument that names the program's command(s) this call runs: a
    # string, or a list of strings or of tables whose ``command_key`` names it.
    # Each command's level comes from the manifest's [commands].
    command_arg: str | None = None
    command_key: str | None = None

    @property
    def forbidden(self) -> bool:
        return self.level is None

    @property
    def changes_state(self) -> bool:
        return self.level is not None and self.level >= Level.R2


@dataclass(frozen=True)
class Flatpak:
    """``flatpak run --command=<command> <app> <fixed...> <root_flags DIR...>``."""

    app: str
    command: str
    fixed: tuple[str, ...] = ()  # arguments that never change, such as "mcp"
    root_flags: tuple[str, ...] = ("--root",)  # each flag gets each allowed folder


@dataclass(frozen=True)
class Launch:
    """How Herkos starts the tool (ADR 0017, 0.2): from the manifest, not from code."""

    program: str  # on PATH; the system Herkos starts the unit <program>@<uid>
    module: str | None = None  # Herkos-lite runs it with its own Python
    args: tuple[str, ...] = ()  # names from LAUNCH_ARGS
    flatpak: Flatpak | None = None  # a program from Flatpak (catalogue only)


@dataclass(frozen=True)
class Manifest:
    tool: str
    description: str
    operations: Mapping[str, Operation]
    launch: Launch | None = None
    # Level of each of the program's commands, by id or group ("home.*");
    # None: forbidden. A command it does not name is R4 (decision of 2026-11-05).
    commands: Mapping[str, Level | None] = field(default_factory=dict)

    def operation(self, name: str) -> Operation | None:
        return self.operations.get(name)

    def command_level(self, command: str) -> tuple[bool, Level | None]:
        """(known, level) of one command: the id itself, else its nearest group."""
        if command in self.commands:
            return True, self.commands[command]
        parts = command.split(".")
        for n in range(len(parts) - 1, 0, -1):
            group = ".".join(parts[:n]) + _GROUP_SUFFIX
            if group in self.commands:
                return True, self.commands[group]
        return False, None


def is_command(value: object) -> bool:
    """A command id a call may name (no groups: those are only for the manifest)."""
    return isinstance(value, str) and _COMMAND.fullmatch(value) is not None


def parse_manifest(data: Mapping[str, Any], source: str = "<manifest>") -> Manifest:
    tool = data.get("tool")
    if not isinstance(tool, str) or not _NAME.fullmatch(tool):
        raise ManifestError(f"{source}: 'tool' must be a reverse-DNS name")
    unknown = set(data) - {"tool", "description", "operations", "launch", "commands"}
    if unknown:
        raise ManifestError(f"{source}: unknown keys {sorted(unknown)}")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ManifestError(f"{source}: 'description' must be a string")
    ops_data = data.get("operations")
    if not isinstance(ops_data, dict) or not ops_data:
        raise ManifestError(f"{source}: at least one [operations.<name>] is required")

    operations: dict[str, Operation] = {}
    for name, op in ops_data.items():
        where = f"{source}: operations.{name}"
        if not _NAME.fullmatch(name):
            raise ManifestError(f"{where}: bad operation name")
        if not isinstance(op, dict):
            raise ManifestError(f"{where}: must be a table")
        unknown = set(op) - _OPERATION_KEYS
        if unknown:
            raise ManifestError(f"{where}: unknown keys {sorted(unknown)}")
        raw_level = op.get("level")
        if not isinstance(raw_level, str):
            raise ManifestError(f"{where}: 'level' is required (R0-R4 or 'forbidden')")
        if raw_level == FORBIDDEN:
            level = None
        else:
            try:
                level = Level.parse(raw_level)
            except ValueError as e:
                raise ManifestError(f"{where}: {e}") from None
        flags = {}
        for key in ("undoable", "reversible", "returns_external", "egress"):
            value = op.get(key, False)
            if not isinstance(value, bool):
                raise ManifestError(f"{where}: '{key}' must be true or false")
            flags[key] = value
        resource_args = op.get("resource_args", [])
        if not isinstance(resource_args, list) or not all(
            isinstance(a, str) and a for a in resource_args
        ):
            raise ManifestError(f"{where}: 'resource_args' must be a list of argument names")
        summary = op.get("summary", name)
        if not isinstance(summary, str):
            raise ManifestError(f"{where}: 'summary' must be a string")
        command_arg = op.get("command_arg")
        command_key = op.get("command_key")
        for key, value in (("command_arg", command_arg), ("command_key", command_key)):
            if value is not None and (not isinstance(value, str) or not _NAME.fullmatch(value)):
                raise ManifestError(f"{where}: '{key}' must be an argument name")
        if command_key is not None and command_arg is None:
            raise ManifestError(f"{where}: 'command_key' goes only with 'command_arg'")
        if command_arg is not None and "commands" not in data:
            raise ManifestError(f"{where}: 'command_arg' needs a [commands] table")
        operations[name] = Operation(
            name=name,
            level=level,
            summary=summary,
            resource_args=tuple(resource_args),
            command_arg=command_arg,
            command_key=command_key,
            **flags,
        )
    launch = _parse_launch(data.get("launch"), source) if "launch" in data else None
    commands = _parse_commands(data["commands"], source) if "commands" in data else {}
    return Manifest(
        tool=tool,
        description=description,
        operations=operations,
        launch=launch,
        commands=commands,
    )


def _parse_commands(data: Any, source: str) -> dict[str, Level | None]:
    where = f"{source}: commands"
    if not isinstance(data, dict) or not data:
        raise ManifestError(f"{where}: must be a table of command ids or groups")
    if len(data) > MAX_COMMANDS:
        raise ManifestError(f"{where}: more than {MAX_COMMANDS} entries")
    commands: dict[str, Level | None] = {}
    for key, raw in data.items():
        name = key.removesuffix(_GROUP_SUFFIX)
        if not _COMMAND.fullmatch(name):
            raise ManifestError(f"{where}: bad command id or group {key!r}")
        if not isinstance(raw, str):
            raise ManifestError(f"{where}.{key}: level must be R0-R4 or 'forbidden'")
        if raw == FORBIDDEN:
            commands[key] = None
            continue
        try:
            commands[key] = Level.parse(raw)
        except ValueError as e:
            raise ManifestError(f"{where}.{key}: {e}") from None
    return commands


def _parse_launch(data: Any, source: str) -> Launch:
    where = f"{source}: launch"
    if not isinstance(data, dict):
        raise ManifestError(f"{where}: must be a table")
    unknown = set(data) - _LAUNCH_KEYS
    if unknown:
        raise ManifestError(f"{where}: unknown keys {sorted(unknown)}")
    program = data.get("program")
    if not isinstance(program, str) or not _PROGRAM.fullmatch(program):
        raise ManifestError(f"{where}: 'program' must be a program name, not a path")
    module = data.get("module")
    if module is not None and (not isinstance(module, str) or not _MODULE.fullmatch(module)):
        raise ManifestError(f"{where}: 'module' must be a Python module name")
    args = data.get("args", [])
    if (
        not isinstance(args, list)
        or not all(isinstance(a, str) and a in LAUNCH_ARGS for a in args)
        or len(set(args)) != len(args)
    ):
        raise ManifestError(f"{where}: 'args' must be a list of {list(LAUNCH_ARGS)}")
    # Always in LAUNCH_ARGS order, so every Herkos builds the same command line.
    ordered = tuple(a for a in LAUNCH_ARGS if a in args)
    flatpak = _parse_flatpak(data, program, where)
    if flatpak is not None and "backup_dir" in ordered:
        raise ManifestError(f"{where}: a Flatpak program gets no 'backup_dir'")
    return Launch(program=program, module=module, args=ordered, flatpak=flatpak)


def _words(value: Any, limit: int, pattern: re.Pattern[str]) -> tuple[str, ...] | None:
    if not isinstance(value, list) or len(value) > limit:
        return None
    if not all(isinstance(w, str) and pattern.fullmatch(w) for w in value):
        return None
    return tuple(value)


def _parse_flatpak(data: dict[str, Any], program: str, where: str) -> Flatpak | None:
    if "flatpak" not in data:
        if any(k in data for k in ("command", "fixed", "root_flags")):
            raise ManifestError(
                f"{where}: 'command', 'fixed' and 'root_flags' go only with 'flatpak'"
            )
        return None
    app = data["flatpak"]
    if not isinstance(app, str) or len(app) > 255 or not _APP_ID.fullmatch(app):
        raise ManifestError(f"{where}: 'flatpak' must be a Flatpak application id")
    if not program.startswith(EXT_PREFIX) or program == EXT_PREFIX:
        raise ManifestError(f"{where}: a Flatpak program needs 'program' = \"{EXT_PREFIX}<name>\"")
    if "module" in data:
        raise ManifestError(f"{where}: a Flatpak program has no 'module'")
    command = data.get("command")
    if not isinstance(command, str) or not _PROGRAM.fullmatch(command):
        raise ManifestError(f"{where}: 'command' must be a program name, not a path")
    fixed = _words(data.get("fixed", []), _MAX_FIXED, _FIXED)
    if fixed is None:
        raise ManifestError(f"{where}: 'fixed' must be at most {_MAX_FIXED} plain words or options")
    flags = _words(data.get("root_flags", ["--root"]), _MAX_ROOT_FLAGS, _ROOT_FLAG)
    if not flags:
        raise ManifestError(f"{where}: 'root_flags' must be 1 to {_MAX_ROOT_FLAGS} long options")
    return Flatpak(app=app, command=command, fixed=fixed, root_flags=flags)


def load_manifest(path: Path) -> Manifest:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ManifestError(f"{path}: {e}") from None
    return parse_manifest(data, str(path))


class ManifestRegistry:
    """The manifests Herkos knows. A tool not in here is unknown."""

    def __init__(self, manifests: Iterable[Manifest] = ()) -> None:
        self._by_tool: dict[str, Manifest] = {}
        self._builtin: set[str] = set()
        for m in manifests:
            self.add(m)

    def add(self, manifest: Manifest) -> None:
        if manifest.tool in self._by_tool:
            raise ManifestError(f"duplicate manifest for {manifest.tool}")
        self._by_tool[manifest.tool] = manifest

    def get(self, tool: str) -> Manifest | None:
        return self._by_tool.get(tool)

    def __contains__(self, tool: str) -> bool:
        return tool in self._by_tool

    def __iter__(self):
        """The manifests, sorted by tool name."""
        return iter(sorted(self._by_tool.values(), key=lambda m: m.tool))

    @classmethod
    def builtin(cls) -> ManifestRegistry:
        """Manifests shipped with Herkos (``herkos/manifests/*.toml``)."""
        registry = cls()
        for entry in sorted(resources.files("herkos.manifests").iterdir(), key=lambda e: e.name):
            if entry.name.endswith(".toml"):
                data = tomllib.loads(entry.read_text(encoding="utf-8"))
                registry.add(parse_manifest(data, entry.name))
        registry._builtin = set(registry._by_tool)
        return registry

    def is_builtin(self, tool: str) -> bool:
        """Shipped with Herkos itself, not from the catalogue (no pins, ADR 0017)."""
        return tool in self._builtin

    @classmethod
    def installed(cls, directory: Path | None = None) -> ManifestRegistry:
        """The built-in manifests plus the system catalogue (ADR 0017, step 1).

        A catalogue file is read only if nobody but root or this Herkos can
        change it (``trusted``). A file that is not trusted, malformed, or
        names a tool Herkos already knows is skipped and reported: the tool
        stays unknown, and so denied. Built-in manifests always win.
        """
        registry = cls.builtin()
        if directory is None:
            env = os.environ.get("HERKOS_MANIFEST_DIR")
            directory = Path(env) if env else SYSTEM_MANIFEST_DIR
        for problem in registry.add_catalogue(directory):
            print(f"herkos: manifest catalogue: {problem}", file=sys.stderr)
        return registry

    def add_catalogue(self, directory: Path) -> list[str]:
        """Add the trusted ``*.toml`` files of ``directory``; return what was skipped."""
        try:
            st = os.lstat(directory)
        except FileNotFoundError:
            return []
        except OSError as e:
            return [f"{directory}: {e}"]
        if not stat.S_ISDIR(st.st_mode) or not trusted(st):
            return [f"{directory}: not a folder that only root or Herkos can change"]
        try:
            names = sorted(e.name for e in os.scandir(directory) if e.name.endswith(".toml"))
        except OSError as e:
            return [f"{directory}: {e}"]
        problems = []
        if len(names) > MAX_MANIFEST_FILES:
            problems.append(f"{directory}: more than {MAX_MANIFEST_FILES} manifests, rest skipped")
            names = names[:MAX_MANIFEST_FILES]
        for name in names:
            try:
                self.add(_read_trusted(directory / name))
            except ManifestError as e:
                problems.append(str(e))
        return problems


def trusted(st: os.stat_result) -> bool:
    """Owned by root or by this process, and nobody else may write to it."""
    return st.st_uid in (0, os.geteuid()) and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def _read_trusted(path: Path) -> Manifest:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise ManifestError(f"{path}: {e}") from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or not trusted(st):
            raise ManifestError(f"{path}: not a file that only root or Herkos can change")
        data = f.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError(f"{path}: larger than {MAX_MANIFEST_BYTES} bytes")
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ManifestError(f"{path}: {e}") from None
    return parse_manifest(parsed, str(path))
