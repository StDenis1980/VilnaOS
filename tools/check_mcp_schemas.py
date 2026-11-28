#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the ``inputSchema`` of every MCP tool of the repo, with ``ast`` only.

The MCP servers under ``mcp/*/src/`` (``server.py``, and for ``mcp/desktop`` the
modules that list tools) declare their tools in a module-level ``TOOLS`` list.
The schema is what stands between the model's arguments and the tool, so this
check reads it without importing or running any server code: the source is parsed
with ``ast`` and a small, safe evaluator works out the dictionaries (literals,
``**`` unpacking, f-strings, ``list(...)``, simple arithmetic, names of the same
module and of sibling modules of the package, and calls of helpers such as
``_tool(...)`` that are a single ``return``). Whatever it cannot work out is
reported as a warning and is not checked.

Every tool schema is checked as JSON Schema (a subset of draft 2020-12).
Errors, which make the exit code 1:

* the ``TOOLS`` entry is a dictionary with a valid, unique ``name`` and an
  ``inputSchema`` object; ``annotations`` hints are booleans and do not contradict
  each other (``readOnlyHint`` together with ``destructiveHint``);
* ``type`` is ``object`` and ``properties`` is an object; ``required`` is a list of
  unique names that are all in ``properties``; ``additionalProperties`` is ``false``;
* every property schema is a valid schema: known ``type``, a non-empty ``enum`` without
  duplicates whose members have the declared type, ``minimum`` <= ``maximum``,
  ``minLength`` <= ``maxLength``, ``minItems`` <= ``maxItems``, limits that are
  finite numbers or non-negative integers.

Warnings (``path: warning: ...``) are the limits for hostile input from a model.
They are listed but do not fail the check (``--strict`` makes them fail):

* a string without ``maxLength`` (an ``enum`` or ``const`` bounds itself);
* an array without ``maxItems``, or without ``items``;
* an integer or a number without a lower or an upper bound;
* a nested object that is free-form (no ``properties``) or lacks
  ``additionalProperties: false``;
* a property without ``type``, an unknown keyword (a typo would drop a limit
  silently), a ``$ref`` (not followed), a tool without a description;
* a tool, a file or a value the ``ast`` reader cannot work out.

Only ``git ls-files`` is run (to find the files). Nothing else is executed, no
server code is imported, nothing is read from the network and nothing is written.

Run: python3 tools/check_mcp_schemas.py [--strict] [FILE.py ...]
Without arguments every tracked ``mcp/*/src/**/*.py`` that lists tools is checked.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import ast
import math
import re
import subprocess
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_STEPS = 300_000  # work of the evaluator for one file
MAX_DEPTH = 60
NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")
TYPES = frozenset({"string", "number", "integer", "boolean", "array", "object", "null"})
# The keywords of draft 2020-12 (core, applicator, validation, annotations); "x-..." is free.
KEYWORDS = frozenset(
    {
        "$schema", "$id", "$ref", "$defs", "$comment", "$anchor", "$dynamicRef",
        "$dynamicAnchor", "$vocabulary", "allOf", "anyOf", "oneOf", "not", "if", "then",
        "else", "dependentSchemas", "prefixItems", "items", "contains", "properties",
        "patternProperties", "additionalProperties", "propertyNames", "unevaluatedItems",
        "unevaluatedProperties", "type", "enum", "const", "multipleOf", "maximum",
        "exclusiveMaximum", "minimum", "exclusiveMinimum", "maxLength", "minLength",
        "pattern", "maxItems", "minItems", "uniqueItems", "maxContains", "minContains",
        "maxProperties", "minProperties", "required", "dependentRequired", "title",
        "description", "default", "deprecated", "readOnly", "writeOnly", "examples",
        "format", "contentEncoding", "contentMediaType", "contentSchema",
    }
)  # fmt: skip
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
COMBINATORS = ("anyOf", "oneOf", "allOf")


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False

    def __str__(self) -> str:
        return f"{self.path}: {'warning: ' if self.warning else ''}{self.message}"


class _Unknown:
    """The value of something the evaluator cannot work out."""

    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN: Any = _Unknown()


class SourceError(Exception):
    """A file that cannot be read or parsed."""


class TooComplex(Exception):
    """The evaluator spent its budget on one file."""


@dataclass
class ModuleRef:
    module: Module


@dataclass
class Module:
    path: Path
    tree: ast.Module
    src: Path | None  # the ``src`` folder that holds the package
    assigns: dict[str, ast.expr] = field(default_factory=dict)
    funcs: dict[str, ast.FunctionDef] = field(default_factory=dict)
    imports: dict[str, tuple[str, int, str | None]] = field(default_factory=dict)
    cache: dict[str, Any] = field(default_factory=dict)
    busy: set[str] = field(default_factory=set)


def src_folder(path: Path) -> Path | None:
    for parent in path.resolve().parents:
        if parent.name == "src":
            return parent
    return None


def parse_module(path: Path) -> Module:
    """Read and parse one source file (nothing of it is run)."""
    try:
        if not path.exists():
            raise SourceError("cannot read the file: no such file")
        if path.is_symlink() or not path.is_file():
            raise SourceError("cannot read the file: not a regular file")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SourceError(f"larger than {MAX_FILE_BYTES} bytes")
        text = path.read_bytes().decode("utf-8-sig")
        tree = ast.parse(text, filename=str(path))
    except SourceError:
        raise
    except OSError as err:
        raise SourceError(f"cannot read the file: {err.strerror or err}") from err
    except UnicodeDecodeError as err:
        raise SourceError("cannot read the file: not UTF-8 text") from err
    except SyntaxError as err:
        raise SourceError(f"cannot parse the file: {err.msg} (line {err.lineno})") from err
    except (ValueError, RecursionError, MemoryError) as err:
        raise SourceError(f"cannot parse the file: {type(err).__name__}") from err
    module = Module(path, tree, src_folder(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                module.assigns[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                module.assigns[node.target.id] = node.value
        elif isinstance(node, ast.FunctionDef):
            module.funcs[node.name] = node
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                module.imports[alias.asname or alias.name] = (
                    node.module or "",
                    node.level,
                    alias.name,
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    module.imports[alias.asname] = (alias.name, 0, None)
    return module


class Loader:
    """Loads and caches the modules of one run."""

    def __init__(self) -> None:
        self.modules: dict[Path, Module | None] = {}

    def load(self, path: Path) -> Module:
        key = path.resolve()
        if key not in self.modules:
            self.modules[key] = None
            self.modules[key] = parse_module(path)
        module = self.modules[key]
        if module is None:
            raise SourceError("import cycle")
        return module

    def find(self, origin: Module, dotted: str, level: int) -> Module | None:
        """The module ``dotted`` as seen from ``origin`` (None when it is not in the repo)."""
        if level > 0:
            base = origin.path.resolve().parent
            for _ in range(level - 1):
                base = base.parent
        elif origin.src is not None:
            base = origin.src
        else:
            return None
        parts = [p for p in dotted.split(".") if p]
        if not all(p.isidentifier() for p in parts):
            return None
        folder = base.joinpath(*parts)
        for candidate in (folder.with_suffix(".py"), folder / "__init__.py"):
            if candidate.is_file() and not candidate.is_symlink():
                try:
                    return self.load(candidate)
                except SourceError:
                    return None
        return None


class Evaluator:
    """A safe, partial evaluator of the constants a tool list is made of."""

    def __init__(self, loader: Loader) -> None:
        self.loader = loader
        self.steps = 0

    def lookup(self, module: Module, name: str, depth: int) -> Any:
        if name in module.cache:
            return module.cache[name]
        if name in module.busy:
            return UNKNOWN
        module.busy.add(name)
        try:
            value = self._lookup(module, name, depth)
        finally:
            module.busy.discard(name)
        module.cache[name] = value
        return value

    def _lookup(self, module: Module, name: str, depth: int) -> Any:
        if name in module.assigns:
            return self.ev(module.assigns[name], module, None, depth + 1)
        if name in module.imports:
            dotted, level, orig = module.imports[name]
            target = self.loader.find(module, dotted, level)
            if orig is None:
                return ModuleRef(target) if target else UNKNOWN
            if target is not None and (orig in target.assigns or orig in target.imports):
                return self.lookup(target, orig, depth + 1)
            sub = self.loader.find(module, f"{dotted}.{orig}" if dotted else orig, level)
            return ModuleRef(sub) if sub else UNKNOWN
        return UNKNOWN

    def ev(self, node: ast.AST, module: Module, local: dict | None, depth: int = 0) -> Any:
        self.steps += 1
        if self.steps > MAX_STEPS or depth > MAX_DEPTH:
            raise TooComplex
        try:
            return self._ev(node, module, local, depth)
        except (
            TypeError,
            ValueError,
            KeyError,
            IndexError,
            ZeroDivisionError,
            OverflowError,
        ):
            return UNKNOWN

    def _ev(self, node: ast.AST, module: Module, local: dict | None, depth: int) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if local is not None and node.id in local:
                return local[node.id]
            return self.lookup(module, node.id, depth)
        if isinstance(node, ast.Attribute):
            base = self.ev(node.value, module, local, depth + 1)
            if isinstance(base, ModuleRef):
                return self.lookup(base.module, node.attr, depth + 1)
            return UNKNOWN
        if isinstance(node, ast.Dict):
            return self._dict(node, module, local, depth)
        if isinstance(node, ast.List | ast.Tuple | ast.Set):
            items = self._sequence(node.elts, module, local, depth)
            if items is UNKNOWN:
                return UNKNOWN
            if isinstance(node, ast.Tuple):
                return tuple(items)
            return list(items) if isinstance(node, ast.List) else UNKNOWN
        if isinstance(node, ast.JoinedStr):
            parts = []
            for part in node.values:
                if isinstance(part, ast.Constant):
                    parts.append(str(part.value))
                else:
                    parts.append("?")
            return "".join(parts)
        if isinstance(node, ast.BinOp):
            return self._binop(node, module, local, depth)
        if isinstance(node, ast.UnaryOp):
            value = self.ev(node.operand, module, local, depth + 1)
            if value is UNKNOWN:
                return UNKNOWN
            if isinstance(node.op, ast.USub) and _is_num(value):
                return -value
            if isinstance(node.op, ast.UAdd) and _is_num(value):
                return +value
            if isinstance(node.op, ast.Not):
                return not value
            return UNKNOWN
        if isinstance(node, ast.Subscript):
            base = self.ev(node.value, module, local, depth + 1)
            index = self.ev(node.slice, module, local, depth + 1)
            if base is UNKNOWN or index is UNKNOWN:
                return UNKNOWN
            if isinstance(base, list | tuple | dict | str) and not isinstance(index, slice):
                return base[index]
            return UNKNOWN
        if isinstance(node, ast.Call):
            return self._call(node, module, local, depth)
        return UNKNOWN

    def _sequence(
        self, elts: Sequence[ast.expr], module: Module, local: dict | None, depth: int
    ) -> Any:
        items: list[Any] = []
        for elt in elts:
            if isinstance(elt, ast.Starred):
                inner = self.ev(elt.value, module, local, depth + 1)
                if not isinstance(inner, list | tuple):
                    return UNKNOWN
                items.extend(inner)
            else:
                items.append(self.ev(elt, module, local, depth + 1))
        return items

    def _dict(self, node: ast.Dict, module: Module, local: dict | None, depth: int) -> Any:
        out: dict[Any, Any] = {}
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:
                inner = self.ev(value, module, local, depth + 1)
                if not isinstance(inner, dict):
                    return UNKNOWN
                out.update(inner)
                continue
            name = self.ev(key, module, local, depth + 1)
            if not isinstance(name, str | int | float | bool | tuple) or name is UNKNOWN:
                return UNKNOWN
            out[name] = self.ev(value, module, local, depth + 1)
        return out

    def _binop(self, node: ast.BinOp, module: Module, local: dict | None, depth: int) -> Any:
        left = self.ev(node.left, module, local, depth + 1)
        right = self.ev(node.right, module, local, depth + 1)
        if left is UNKNOWN or right is UNKNOWN:
            return UNKNOWN
        op = node.op
        if (
            isinstance(op, ast.Add)
            and type(left) is type(right)
            and (isinstance(left, str | list | tuple) or _is_num(left))
        ):
            return left + right
        if not (_is_num(left) and _is_num(right)):
            return UNKNOWN
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            return left * right
        if isinstance(op, ast.Div):
            return left / right
        if isinstance(op, ast.FloorDiv):
            return left // right
        if isinstance(op, ast.Mod):
            return left % right
        if isinstance(op, ast.Pow) and abs(left) <= 10**6 and 0 <= right <= 64:
            return left**right
        if isinstance(op, ast.LShift) and isinstance(left, int) and 0 <= right <= 64:
            return left << right
        return UNKNOWN

    def _call(self, node: ast.Call, module: Module, local: dict | None, depth: int) -> Any:
        if not isinstance(node.func, ast.Name):
            return UNKNOWN
        name = node.func.id
        if local is None or name not in local:
            found = self._function(module, name)
            if found is not None:
                return self._inline(found[0], node, module, found[1], local, depth)
        if name in ("list", "tuple") and len(node.args) == 1 and not node.keywords:
            arg = self.ev(node.args[0], module, local, depth + 1)
            if isinstance(arg, list | tuple | dict | str):
                return list(arg) if name == "list" else tuple(arg)
        return UNKNOWN

    def _function(self, module: Module, name: str) -> tuple[ast.FunctionDef, Module] | None:
        """The helper ``name`` of this module, or imported from a module of the repo."""
        if name in module.funcs:
            return module.funcs[name], module
        if name in module.imports:
            dotted, level, orig = module.imports[name]
            target = self.loader.find(module, dotted, level)
            if target is not None and orig in target.funcs:
                return target.funcs[orig], target
        return None

    def _inline(
        self,
        func: ast.FunctionDef,
        call: ast.Call,
        module: Module,
        home: Module,
        local: dict | None,
        depth: int,
    ) -> Any:
        """Call a helper whose body is one ``return`` by evaluating that expression.

        The arguments are read in ``module`` (the caller), the body in ``home``.
        """
        body = list(func.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]  # the docstring
        if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
            return UNKNOWN
        if any(isinstance(a, ast.Starred) for a in call.args):
            return UNKNOWN
        positional = [self.ev(a, module, local, depth + 1) for a in call.args]
        named: dict[str, Any] = {}
        for kw in call.keywords:
            if kw.arg is None:
                inner = self.ev(kw.value, module, local, depth + 1)
                if not isinstance(inner, dict) or not all(isinstance(k, str) for k in inner):
                    return UNKNOWN
                named.update(inner)
            else:
                named[kw.arg] = self.ev(kw.value, module, local, depth + 1)
        spec = func.args
        params = [a.arg for a in spec.posonlyargs + spec.args]
        if len(positional) > len(params) and spec.vararg is None:
            return UNKNOWN
        scope: dict[str, Any] = {}
        defaults = dict(zip(reversed(params), reversed(spec.defaults), strict=False))
        for i, param in enumerate(params):
            if i < len(positional):
                scope[param] = positional[i]
            elif param in named:
                scope[param] = named.pop(param)
            elif param in defaults:
                scope[param] = self.ev(defaults[param], home, None, depth + 1)
            else:
                return UNKNOWN
        for arg, default in zip(spec.kwonlyargs, spec.kw_defaults, strict=True):
            if arg.arg in named:
                scope[arg.arg] = named.pop(arg.arg)
            elif default is not None:
                scope[arg.arg] = self.ev(default, home, None, depth + 1)
            else:
                return UNKNOWN
        if spec.vararg is not None:
            scope[spec.vararg.arg] = tuple(positional[len(params) :])
        if named:
            if spec.kwarg is None:
                return UNKNOWN
            scope[spec.kwarg.arg] = named
        return self.ev(body[0].value, home, scope, depth + 1)


def _is_num(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


# --- the schema rules ---------------------------------------------------------


class Report:
    """The findings about one tool."""

    def __init__(self, path: str, label: str) -> None:
        self.path = path
        self.label = label
        self.problems: list[Problem] = []

    def add(self, warning: bool, where: str, text: str) -> None:
        self.problems.append(Problem(self.path, f"{self.label}: {where}: {text}", warning))

    def error(self, where: str, text: str) -> None:
        self.add(False, where, text)

    def warn(self, where: str, text: str) -> None:
        self.add(True, where, text)


def _limit(report: Report, where: str, schema: dict, key: str, integer: bool) -> Any:
    """The value of a limit keyword, checked; None when it is absent or unreadable."""
    if key not in schema:
        return None
    value = schema[key]
    if value is UNKNOWN:
        return None
    if integer:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            report.error(where, f"{key} must be a non-negative integer, not {value!r}")
            return None
    elif not _is_num(value) or (isinstance(value, float) and not math.isfinite(value)):
        report.error(where, f"{key} must be a finite number, not {value!r}")
        return None
    return value


def _check_enum(report: Report, where: str, schema: dict, types: list[str]) -> bool:
    """Check ``enum``; True when it bounds the value."""
    if "enum" not in schema:
        return "const" in schema
    members = schema["enum"]
    if members is UNKNOWN:
        return True
    if not isinstance(members, list | tuple) or not members:
        report.error(where, "enum must be a non-empty list")
        return False
    seen: set[str] = set()
    for member in members:
        key = f"{type(member).__name__}:{member!r}"
        if key in seen:
            report.error(where, f"enum lists {member!r} twice")
        seen.add(key)
    if types:
        for member in members:
            if member is not UNKNOWN and not any(_matches(member, t) for t in types):
                report.error(where, f"enum member {member!r} is not of type {'/'.join(types)}")
    return True


def _matches(value: Any, type_name: str) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, float) and value.is_integer()
        )
    if type_name == "number":
        return _is_num(value)
    if type_name == "null":
        return value is None
    if type_name == "array":
        return isinstance(value, list | tuple)
    return isinstance(value, dict)


def _types_of(report: Report, where: str, schema: dict) -> list[str] | None:
    """The declared types as a list; None when ``type`` is missing."""
    declared = schema.get("type")
    if "type" not in schema or declared is UNKNOWN:
        return None
    names = [declared] if isinstance(declared, str) else declared
    if not isinstance(names, list | tuple) or not names:
        report.error(where, f"type must be a name or a list of names, not {declared!r}")
        return []
    good = []
    for name in names:
        if not isinstance(name, str) or name not in TYPES:
            report.error(where, f"unknown type {name!r}")
        else:
            good.append(name)
    return good


def check_schema(report: Report, where: str, schema: Any, root: bool = False) -> None:
    """Check one schema and, below it, its properties, items and branches."""
    if schema is UNKNOWN:
        report.warn(where, "cannot be read with ast: not checked")
        return
    if schema is True:
        report.warn(where, "accepts any value")
        return
    if schema is False:
        return
    if not isinstance(schema, dict):
        report.error(where, f"a schema must be an object, not {schema!r}")
        return
    for key in schema:
        if not isinstance(key, str) or (key not in KEYWORDS and not key.startswith("x-")):
            report.warn(where, f"unknown keyword {key!r}")
    types = _types_of(report, where, schema)
    bounded = _check_enum(report, where, schema, types or [])
    if "$ref" in schema:
        report.warn(where, "$ref is not followed: not checked")
    typed_otherwise = bounded or any(k in schema for k in (*COMBINATORS, "not", "$ref"))
    if types is None and not root and not typed_otherwise:
        report.warn(where, "no type: any value is accepted")
    types = types or []
    if "string" in types:
        _check_string(report, where, schema, bounded)
    if "integer" in types or "number" in types:
        _check_number(report, where, schema, bounded)
    if "array" in types:
        _check_array(report, where, schema)
    if "object" in types:
        _check_object(report, where, schema, root)
    for key in COMBINATORS:
        if key not in schema:
            continue
        branches = schema[key]
        if branches is UNKNOWN:
            continue
        if not isinstance(branches, list | tuple) or not branches:
            report.error(where, f"{key} must be a non-empty list of schemas")
            continue
        for i, branch in enumerate(branches):
            check_schema(report, f"{where}.{key}[{i}]", branch)


def _check_string(report: Report, where: str, schema: dict, bounded: bool) -> None:
    longest = _limit(report, where, schema, "maxLength", True)
    shortest = _limit(report, where, schema, "minLength", True)
    if longest is not None and shortest is not None and shortest > longest:
        report.error(where, f"minLength {shortest} is more than maxLength {longest}")
    if "maxLength" not in schema and not bounded:
        report.warn(where, "string without maxLength")


def _check_number(report: Report, where: str, schema: dict, bounded: bool) -> None:
    low = _limit(report, where, schema, "minimum", False)
    high = _limit(report, where, schema, "maximum", False)
    _limit(report, where, schema, "exclusiveMinimum", False)
    _limit(report, where, schema, "exclusiveMaximum", False)
    if low is not None and high is not None and low > high:
        report.error(where, f"minimum {low} is more than maximum {high}")
    if bounded:
        return
    has_low = "minimum" in schema or "exclusiveMinimum" in schema
    has_high = "maximum" in schema or "exclusiveMaximum" in schema
    if not has_low and not has_high:
        report.warn(where, f"{schema.get('type')} without minimum and maximum")
    elif not has_low:
        report.warn(where, f"{schema.get('type')} without minimum")
    elif not has_high:
        report.warn(where, f"{schema.get('type')} without maximum")


def _check_array(report: Report, where: str, schema: dict) -> None:
    most = _limit(report, where, schema, "maxItems", True)
    least = _limit(report, where, schema, "minItems", True)
    if most is not None and least is not None and least > most:
        report.error(where, f"minItems {least} is more than maxItems {most}")
    if "maxItems" not in schema:
        report.warn(where, "array without maxItems")
    if "items" in schema:
        check_schema(report, f"{where}[]", schema["items"])
    elif "prefixItems" not in schema:
        report.warn(where, "array without items: its elements are not checked")
    prefix = schema.get("prefixItems")
    if isinstance(prefix, list | tuple):
        for i, item in enumerate(prefix):
            check_schema(report, f"{where}[{i}]", item)


def _check_object(report: Report, where: str, schema: dict, root: bool) -> None:
    props = schema.get("properties", UNKNOWN if "properties" in schema else None)
    flag = report.error if root else report.warn
    if props is None:
        flag(where, "no properties: any argument names are accepted")
    elif props is not UNKNOWN and not isinstance(props, dict):
        report.error(where, f"properties must be an object, not {props!r}")
        props = None
    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if extra is not False and extra is not UNKNOWN:
            flag(where, "additionalProperties is not false: unknown names are accepted")
    elif root or props is not None:
        flag(where, "additionalProperties is not false: unknown names are accepted")
    if isinstance(props, dict):
        for name, sub in props.items():
            if not isinstance(name, str):
                report.error(where, f"property name {name!r} is not a string")
                continue
            check_schema(report, f"{where}.properties.{name}".removeprefix("inputSchema."), sub)
    _check_required(report, where, schema, props)


def _check_required(report: Report, where: str, schema: dict, props: Any) -> None:
    if "required" not in schema:
        return
    required = schema["required"]
    if required is UNKNOWN:
        return
    if not isinstance(required, list | tuple):
        report.error(where, f"required must be a list of names, not {required!r}")
        return
    seen: set[str] = set()
    for name in required:
        if not isinstance(name, str):
            report.error(where, f"required has {name!r}, which is not a name")
        elif name in seen:
            report.error(where, f"required lists {name!r} twice")
        elif isinstance(props, dict) and name not in props:
            report.error(where, f"required {name!r} is not in properties")
        if isinstance(name, str):
            seen.add(name)


def check_tool(path: str, tool: Any, line: int, seen: set[str]) -> list[Problem]:
    """All findings about one entry of ``TOOLS``."""
    if tool is UNKNOWN:
        return [Problem(path, f"tool at line {line}: cannot be read with ast: not checked", True)]
    if not isinstance(tool, dict):
        return [Problem(path, f"tool at line {line}: is not a dictionary")]
    name = tool.get("name", UNKNOWN)
    if name is UNKNOWN or "name" not in tool:
        return [Problem(path, f"tool at line {line}: has no readable name", "name" in tool)]
    report = Report(path, f'tool "{name}" (line {line})')
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        report.error("name", f"{name!r} is not 1-128 characters of A-Z a-z 0-9 _ - .")
    elif name in seen:
        report.error("name", "the name is used by another tool of the same list")
    seen.add(str(name))
    description = tool.get("description", UNKNOWN if "description" in tool else None)
    if description is None or (isinstance(description, str) and not description.strip()):
        report.warn("description", "the tool has no description")
    check_annotations(report, tool)
    if "inputSchema" not in tool:
        report.error("inputSchema", "the tool has no inputSchema")
    else:
        check_root(report, tool["inputSchema"])
    return report.problems


def check_annotations(report: Report, tool: dict) -> None:
    notes = tool.get("annotations", {})
    if notes is UNKNOWN:
        return
    if not isinstance(notes, dict):
        report.error("annotations", "must be an object")
        return
    for hint in HINTS:
        value = notes.get(hint)
        if hint in notes and value is not UNKNOWN and not isinstance(value, bool):
            report.error("annotations", f"{hint} must be true or false, not {value!r}")
    if notes.get("readOnlyHint") is True and notes.get("destructiveHint") is True:
        report.error("annotations", "readOnlyHint and destructiveHint are both true")


def check_root(report: Report, schema: Any) -> None:
    """The rules for the top of an ``inputSchema``, then the generic ones."""
    where = "inputSchema"
    if schema is UNKNOWN or not isinstance(schema, dict):
        check_schema(report, where, schema, True)
        return
    if "type" not in schema:
        report.error(where, "type is missing: it must be 'object'")
    elif schema["type"] is not UNKNOWN and schema["type"] != "object":
        report.error(where, f"type must be 'object', not {schema['type']!r}")
    schema = {**schema, "type": "object"}  # so that the rest is still checked
    check_schema(report, where, schema, True)


# --- finding and reading the files ---------------------------------------------


def tool_nodes(module: Module) -> tuple[ast.expr, list[ast.expr] | None] | None:
    """The ``TOOLS`` expression of a module and its elements when it is a literal list."""
    node = module.assigns.get("TOOLS")
    if node is None:
        return None
    if isinstance(node, ast.List | ast.Tuple) and not any(
        isinstance(e, ast.Starred) for e in node.elts
    ):
        return node, list(node.elts)
    return node, None


def check_module(
    loader: Loader, evaluator: Evaluator, rel: str, module: Module
) -> tuple[int, list[Problem]]:
    """Check the ``TOOLS`` of one module; returns (number of tools, problems), -1 if none."""
    found = tool_nodes(module)
    if found is None:
        return -1, []
    node, elements = found
    problems: list[Problem] = []
    seen: set[str] = set()
    if elements is None:
        value = evaluator.ev(node, module, None)
        if not isinstance(value, list | tuple):
            return 0, [Problem(rel, f"TOOLS (line {node.lineno}): not a list ast can read", True)]
        entries = [(tool, node.lineno) for tool in value]
    else:
        entries = [(evaluator.ev(e, module, None), e.lineno) for e in elements]
    for tool, line in entries:
        problems += check_tool(rel, tool, line, seen)
    return len(entries), problems


def check_files(root: Path, paths: Sequence[str]) -> tuple[int, int, list[Problem]]:
    """Check the files; returns (tools, servers with tools, problems)."""
    problems: list[Problem] = []
    tools = servers = 0
    loader = Loader()
    projects: dict[str, int] = {}
    for rel in sorted(set(paths)):
        path = root / rel
        parts = Path(rel).parts
        project = "/".join(parts[:2]) if len(parts) > 1 and parts[0] == "mcp" else ""
        projects.setdefault(project, 0)
        if path.is_symlink():
            problems.append(Problem(rel, "a symbolic link is not followed: not checked", True))
            continue
        try:
            module = loader.load(path)
        except SourceError as err:
            problems.append(Problem(rel, str(err)))
            continue
        evaluator = Evaluator(loader)
        try:
            count, found = check_module(loader, evaluator, rel, module)
        except TooComplex:
            problems.append(Problem(rel, "too complex for ast to read: not checked", True))
            continue
        if count < 0:
            if path.name == "server.py":
                problems.append(Problem(rel, "a server.py with no TOOLS list: not checked", True))
            continue
        tools += count
        servers += 1
        projects[project] += 1
        problems += found
    for project, count in projects.items():
        if project and count == 0 and not any(p.path.startswith(project + "/") for p in problems):
            problems.append(Problem(project, "no TOOLS list found in the checked files", True))
    return tools, servers, problems


def tracked_sources(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "mcp"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    found = []
    for name in out.split("\0"):
        parts = Path(name).parts
        if len(parts) >= 4 and parts[2] == "src" and name.endswith(".py"):
            found.append(name)
    return sorted(found)


def lists_tools(root: Path, rel: str) -> bool:
    """A cheap look before parsing: only server.py and files that name TOOLS are read."""
    if rel.endswith("/server.py"):
        return True
    try:
        with (root / rel).open("rb") as handle:
            return b"TOOLS" in handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return True


def main(argv: list[str] | None = None) -> int:
    try:
        args = sys.argv[1:] if argv is None else argv
        usage = "usage: python3 tools/check_mcp_schemas.py [--strict] [FILE.py ...]"
        strict = "--strict" in args
        files = [a for a in args if a != "--strict"]
        if any(a.startswith("-") for a in files):
            print(usage, file=sys.stderr)
            return 0 if files in (["-h"], ["--help"]) else 2
        if any(not a.endswith(".py") for a in files):
            print(f"every file must be a .py file\n{usage}", file=sys.stderr)
            return 2
        if files:
            base = Path.cwd()
            paths = files
        else:
            base = ROOT
            try:
                paths = [p for p in tracked_sources(ROOT) if lists_tools(ROOT, p)]
            except (OSError, subprocess.CalledProcessError) as err:
                print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
                return 2
            if not paths:
                print("no tracked mcp/*/src/**/*.py files found", file=sys.stderr)
                return 2
        tools, servers, problems = check_files(base, paths)
        for problem in problems:
            print(problem, file=sys.stderr)
        errors = sum(not p.warning or strict for p in problems)
        warnings = len(problems) - errors
        print(f"{tools} tools in {servers} files checked: {errors} errors, {warnings} warnings")
        return 1 if errors else 0
    except (OSError, ValueError) as err:  # no traceback for a bad name, argument or bytes
        print(f"{os.path.basename(__file__)}: cannot check: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
