#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Look for sources of non-reproducible builds in the build files of the repo.

A build is reproducible when the same commit gives the same bytes, whoever
builds it, wherever and whenever (the CRA wants an image that can be checked
again, and a Reproducible Builds check of an OBS result is only useful when
the sources are quiet). This tool reads the build files as text and as a
Python syntax tree. Nothing is run, imported or built.

Files checked (``git ls-files``):

* every ``*.spec`` under ``obs/packages``;
* the shell scripts of the KIWI images (``obs/images/*/config.sh``, ``disk.sh``);
* the build scripts in ``BUILD_SCRIPTS``: ``tools/build_site.py``,
  ``sounds/make_sounds.py``, ``obs/tools/obs_sync.py``,
  ``image/plymouth/make_theme.py``.

Any other ``.spec``, ``.sh`` or ``.py`` file can be given on the command line.

Python rules (rule name in brackets):

* [time] the current time (``time.time``, ``datetime.now``, ``date.today``,
  ``time.localtime()`` without an argument, ...) unless the file reads
  ``SOURCE_DATE_EPOCH``;
* [host] the name of the computer or the user (``socket.gethostname``,
  ``getpass.getuser``, ``platform.node``, ``os.getlogin``);
* [random] ``random``/``numpy.random`` functions of the shared generator
  without a ``seed(...)`` call in the file, a generator made without a seed
  (``random.Random()``, ``default_rng()``), ``secrets``, ``uuid.uuid1/4``,
  ``os.urandom``, and ``hash(...)`` of anything but a number (``str`` hashes
  change from one run to the next);
* [order] ``os.listdir``, ``os.scandir``, ``os.walk``, ``glob.glob``,
  ``Path.glob/rglob/iterdir`` whose result is not sorted, and a ``set``
  that is looped over (or turned into a list, or joined) without ``sorted``;
* [mtime] ``getmtime``/``st_mtime`` and the like, unless ``SOURCE_DATE_EPOCH``
  is read;
* [archive] archives that carry the build's own metadata: ``TarFile.add``
  without ``filter=``, ``ZipFile.write``, ``shutil.make_archive``, a gzip
  stream without ``mtime=`` (also ``tarfile.open(..., "w:gz")``);
* [path] a path of the building computer (a literal ``/home/...``,
  ``/root/...`` and so on, or a value made from ``__file__``, the current
  folder, the home or a temporary folder) written to a file or printed
  without ``relative_to``/``.name``.

Spec and shell rules:

* [spec-time] ``%(date)`` and other ``%(...)`` that run ``date``, ``hostname``,
  ``whoami``, ``uname``, ``git``, ... while rpm reads the spec (also in
  comments: rpm expands macros there too), and ``%{lua:}`` that reads the
  clock, a variable or a random number;
* [shell-time] ``date``, ``hostname``, ``whoami``, ``uname``, ``id``,
  ``uuidgen``, ``$USER``, ``$HOSTNAME``, ``$RANDOM``, ``/dev/urandom`` in a
  script (``date -d @$SOURCE_DATE_EPOCH`` is fine);
* [build-path] the build folder (``%{buildroot}``, ``%{_builddir}``,
  ``$RPM_BUILD_ROOT``, ``$PWD``, ``$(pwd)``, ...) copied into a file: inside
  a here-document, the text of ``echo``/``printf``, the replacement of a
  ``sed 's|a|b|'``, or ``--prefix=`` and the like;
* [abs-path] a literal ``/home/<user>``, ``/root``, ``/builddir``,
  ``/usr/src/packages`` in a script;
* [archive] ``tar`` that makes an archive without ``--sort=name``,
  ``--mtime=`` and ``--owner``/``--numeric-owner``, ``gzip`` without ``-n``,
  ``zip`` without ``-X``;
* [order] ``find``/``ls`` output used unsorted (a ``for`` loop over it,
  ``-printf`` or a pipe into a loop);
* [rust-path], [go-path] ``cargo build`` without ``--remap-path-prefix`` (or
  ``RUSTFLAGS`` with it) and ``go build`` without ``-trimpath``: the compiler
  writes the source paths into the program.

A line (or the line above it) with ``repro: ok`` is left alone; say why next
to it. Old findings of the repo are listed in ``LEGACY`` and shown as
warnings, a new finding is an error. Only ``git ls-files`` is run. Nothing is
written and nothing is read from the network.

Run: python3 tools/check_reproducible.py [FILE ...]
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

BUILD_SCRIPTS = (
    "tools/build_site.py",
    "sounds/make_sounds.py",
    "obs/tools/obs_sync.py",
    "image/plymouth/make_theme.py",
)
IMAGE_SCRIPT = re.compile(r"obs/images/[^/]+/(?:config|disk)\.sh")
SPEC_FILE = re.compile(r"obs/packages/[^/]+/[^/]+\.spec")
MARKER = "repro: ok"
EPOCH = "SOURCE_DATE_EPOCH"

# Old findings: (path, rule, the stripped text of the line). Shown as warnings.
LEGACY: frozenset[tuple[str, str, str]] = frozenset(
    {
        # The home of the live user inside the image, not a path of the builder (repro: ok).
        ("obs/images/Vilna-Live/config.sh", "abs-path", "home=/home/live"),
        # Real: the Rust programs keep the paths of the sources they were built from.
        (
            "obs/packages/herkos/herkos.spec",
            "rust-path",
            "cargo build --manifest-path rs/Cargo.toml --release --offline --locked"
            " -p herkosd -p vilna-netguard",
        ),
        # The order of these finds does not reach the package: the symlinks are fixed one
        # by one, and rpm sorts the payload whatever the order of the file list.
        (
            "obs/packages/mullvad-vpn/mullvad-vpn.spec",
            "order",
            "find %{buildroot} -type l | while read -r link; do",
        ),
        (
            "obs/packages/mullvad-vpn/mullvad-vpn.spec",
            "order",
            "find . -mindepth 1 \\( -type f -o -type l \\) -printf '\"/%%P\"\\n'",
        ),
        (
            "obs/packages/mullvad-vpn/mullvad-vpn.spec",
            "order",
            "find . -mindepth 1 -type d -printf '%%P\\n' \\",
        ),
        # The order does not matter: files are removed or copied one by one into a checkout.
        ("obs/tools/obs_sync.py", "order", "for old in co.iterdir():"),
        ("obs/tools/obs_sync.py", "order", "for new in (built / name).iterdir():"),
        # The paths go into a set.
        ("tools/build_site.py", "order", 'for path in public.rglob("*.html"):'),
    }
)


@dataclass(frozen=True)
class Problem:
    path: str
    line: int
    rule: str
    message: str
    text: str = ""
    warning: bool = False

    def __str__(self) -> str:
        flag = "warning: " if self.warning else ""
        return f"{self.path}: {flag}line {self.line}: {self.message} [{self.rule}]"


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

TIME_ALWAYS = frozenset(
    {
        "time.time",
        "time.time_ns",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.date.today",
    }
)
# Current time only without an argument (with one they convert a given time).
TIME_NO_ARGS = frozenset({"time.localtime", "time.gmtime", "time.ctime", "time.asctime"})
HOST_CALLS = frozenset(
    {
        "socket.gethostname",
        "socket.getfqdn",
        "platform.node",
        "getpass.getuser",
        "os.getlogin",
    }
)
RANDOM_KEEP = frozenset(
    {
        "random.Random",
        "random.seed",
        "random.getstate",
        "random.setstate",
        "random.SystemRandom",
    }
)
NUMPY_KEEP = frozenset(
    {
        "numpy.random.default_rng",
        "numpy.random.RandomState",
        "numpy.random.Generator",
        "numpy.random.SeedSequence",
        "numpy.random.seed",
        "numpy.random.PCG64",
        "numpy.random.MT19937",
        "numpy.random.Philox",
        "numpy.random.SFC64",
        "numpy.random.get_state",
        "numpy.random.set_state",
    }
)
ENTROPY_CALLS = frozenset(
    {"os.urandom", "uuid.uuid1", "uuid.uuid4", "random.SystemRandom", "os.getrandom"}
)
LISTING_FUNCS = frozenset(
    {"os.listdir", "os.scandir", "os.walk", "os.fwalk", "glob.glob", "glob.iglob"}
)
LISTING_METHODS = frozenset({"glob", "rglob", "iterdir"})
ORDER_SAFE = frozenset({"sorted", "len", "any", "all", "set", "frozenset", "sum", "max", "min"})
MTIME_CALLS = frozenset({"os.path.getmtime", "os.path.getctime", "os.path.getatime"})
MTIME_ATTRS = frozenset(
    {"st_mtime", "st_mtime_ns", "st_ctime", "st_ctime_ns", "st_atime", "st_atime_ns"}
)
PATH_SOURCES = frozenset(
    {
        "os.getcwd",
        "os.getcwdb",
        "pathlib.Path.cwd",
        "pathlib.Path.home",
        "os.path.expanduser",
        "os.path.abspath",
        "os.path.realpath",
        "tempfile.gettempdir",
        "tempfile.mkdtemp",
        "tempfile.TemporaryDirectory",
    }
)
PATH_SOURCE_METHODS = frozenset({"resolve", "absolute", "expanduser"})
# Calls whose result still holds the path they are given.
PATH_KEEP_FUNCS = frozenset(
    {
        "str",
        "repr",
        "format",
        "os.fspath",
        "os.path.join",
        "os.path.normpath",
        "os.path.dirname",
        "pathlib.Path",
        "pathlib.PurePath",
        "pathlib.PosixPath",
        "pathlib.PurePosixPath",
    }
)
PATH_KEEP_METHODS = frozenset(
    {
        "joinpath",
        "with_name",
        "with_suffix",
        "with_stem",
        "as_posix",
        "as_uri",
        "format",
        "join",
        "replace",
        "strip",
        "rstrip",
        "lstrip",
        "removeprefix",
        "removesuffix",
        "glob",
        "rglob",
        "iterdir",
    }
)
PATH_CLEAN_ATTRS = frozenset({"name", "stem", "suffix", "suffixes"})
PATH_CLEAN_FUNCS = frozenset({"os.path.relpath", "os.path.basename"})
WRITE_METHODS = frozenset({"write_text", "write", "writelines", "writestr", "write_bytes"})
WRITE_FUNCS = frozenset({"json.dump", "json.dumps"})
STRINGIFY_FUNCS = frozenset({"str", "repr", "os.fspath", "format"})
STRINGIFY_METHODS = frozenset({"as_posix", "as_uri", "__str__"})
# A string with these is a pattern that looks for a path, not a path.
REGEX_LIKE = re.compile(r"\(\?|\\[wWsS]|\[\^|\[\\w|\.\*")
BUILD_PATH_LITERAL = re.compile(
    r"(?<![\w/.%${}-])(?:/home/[A-Za-z_][\w.-]*|/root|/Users/[\w.-]+|/builddir"
    r"|/usr/src/packages|/\.build|[A-Za-z]:\\Users)(?![\w-])"
)


def chain(node: ast.AST) -> list[str] | None:
    """``a.b.c`` as ['a', 'b', 'c']; None when the base is not a plain name."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return parts[::-1]
    return None


def is_str(node: ast.AST) -> bool:
    return isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


class PythonChecker:
    def __init__(self, path: str, text: str) -> None:
        self.path = path
        self.lines = text.splitlines()
        self.tree = ast.parse(text, filename=path)
        self.problems: list[Problem] = []
        self.parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(self.tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent
        self.aliases = self.collect_aliases()
        self.mentions_epoch = any(
            isinstance(n, ast.Constant) and isinstance(n.value, str) and EPOCH in n.value
            for n in ast.walk(self.tree)
        )
        self.docstrings = self.collect_docstrings()
        self.seeded_random = False
        self.seeded_numpy = False
        self.set_names = self.collect_set_names()
        self.archive_names = self.collect_archive_names()

    # -- helpers ----------------------------------------------------------

    def report(self, node: ast.AST, rule: str, message: str) -> None:
        line = getattr(node, "lineno", 1)
        text = self.lines[line - 1].strip() if 0 < line <= len(self.lines) else ""
        self.problems.append(Problem(self.path, line, rule, message, text))

    def collect_aliases(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    if item.asname:
                        aliases[item.asname] = item.name
                    else:
                        aliases[item.name.split(".")[0]] = item.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for item in node.names:
                    aliases[item.asname or item.name] = f"{node.module}.{item.name}"
        return aliases

    def resolve(self, node: ast.AST) -> str | None:
        parts = chain(node)
        if parts is None:
            return None
        head = self.aliases.get(parts[0])
        return ".".join([head, *parts[1:]]) if head else ".".join(parts)

    def collect_docstrings(self) -> set[int]:
        found = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                first = node.body[0] if node.body else None
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    found.add(id(first.value))
        return found

    def scope_of(self, node: ast.AST) -> ast.AST:
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.Module):
                return node
        return self.tree

    def statement_of(self, node: ast.AST) -> ast.AST:
        while node in self.parents and not isinstance(node, ast.stmt):
            node = self.parents[node]
        return node

    # -- sets ---------------------------------------------------------------

    def is_set_expr(self, node: ast.AST, names: set[str] | None = None) -> bool:
        if isinstance(node, ast.Set | ast.SetComp):
            return True
        if isinstance(node, ast.Call):
            full = self.resolve(node.func)
            return full in ("set", "frozenset")
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, ast.BitOr | ast.BitAnd | ast.Sub | ast.BitXor
        ):
            return self.is_set_expr(node.left, names) or self.is_set_expr(node.right, names)
        if names is not None and isinstance(node, ast.Name):
            return node.id in names
        return False

    def collect_set_names(self) -> set[str]:
        """Names whose every assignment in the file is a set."""
        sets: dict[str, bool] = {}
        for node in ast.walk(self.tree):
            targets: list[ast.expr] = []
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            elif isinstance(node, ast.For | ast.comprehension | ast.AugAssign):
                targets, value = [node.target], None
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        is_set = value is not None and self.is_set_expr(value)
                        sets[name.id] = sets.get(name.id, True) and is_set
        return {name for name, ok in sets.items() if ok}

    def check_set_loops(self) -> None:
        for node in ast.walk(self.tree):
            iterated: ast.AST | None = None
            if isinstance(node, ast.For | ast.comprehension):
                iterated = node.iter
            elif isinstance(node, ast.Call):
                full = self.resolve(node.func)
                joined = isinstance(node.func, ast.Attribute) and node.func.attr == "join"
                if node.args and (full in ("list", "tuple", "enumerate", "iter") or joined):
                    iterated = node.args[0]
            if iterated is not None and self.is_set_expr(iterated, self.set_names):
                if not isinstance(node, ast.For) and self.ordered_context(node):
                    continue
                self.report(
                    node if not isinstance(node, ast.comprehension) else iterated,
                    "order",
                    "a set is used in its own order, which changes between runs; use sorted(...)",
                )

    # -- archives -----------------------------------------------------------

    def collect_archive_names(self) -> dict[str, str]:
        names: dict[str, str] = {}
        for node in ast.walk(self.tree):
            pairs: list[tuple[ast.AST, ast.AST]] = []
            if isinstance(node, ast.With):
                pairs = [(i.context_expr, i.optional_vars) for i in node.items if i.optional_vars]
            elif isinstance(node, ast.Assign):
                pairs = [(node.value, t) for t in node.targets]
            for value, target in pairs:
                if isinstance(value, ast.Call) and isinstance(target, ast.Name):
                    full = self.resolve(value.func)
                    if full in ("tarfile.open", "tarfile.TarFile"):
                        names[target.id] = "tar"
                    elif full == "zipfile.ZipFile":
                        names[target.id] = "zip"
        return names

    @staticmethod
    def keyword(call: ast.Call, name: str) -> ast.keyword | None:
        return next((k for k in call.keywords if k.arg == name), None)

    @staticmethod
    def mode_of(call: ast.Call, position: int, default: str = "r") -> str:
        arg: ast.AST | None = call.args[position] if len(call.args) > position else None
        for kw in call.keywords:
            if kw.arg == "mode":
                arg = kw.value
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        return default

    def check_archive(self, node: ast.Call, full: str | None) -> None:
        func = node.func
        if full == "shutil.make_archive":
            self.report(
                node,
                "archive",
                "shutil.make_archive keeps file times and owners; use tarfile",
            )
        elif full == "gzip.compress" and not self.keyword(node, "mtime"):
            self.report(node, "archive", "gzip.compress without mtime=0 writes the current time")
        elif full == "gzip.GzipFile" and not self.keyword(node, "mtime"):
            if any(c in self.mode_of(node, 1) for c in "wax"):
                self.report(node, "archive", "GzipFile without mtime=0 writes the current time")
        elif full == "gzip.open" and any(c in self.mode_of(node, 1) for c in "wax"):
            self.report(
                node,
                "archive",
                "gzip.open writes the current time; use GzipFile(mtime=0)",
            )
        elif full in ("tarfile.open", "tarfile.TarFile"):
            mode = self.mode_of(node, 1)
            if re.match(r"[wax][:|]gz", mode) and not self.keyword(node, "fileobj"):
                self.report(
                    node,
                    "archive",
                    "w:gz writes the current time into the gzip header; "
                    "wrap a GzipFile(mtime=0) as fileobj",
                )
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            kind = self.archive_names.get(func.value.id)
            if kind == "tar" and func.attr == "add" and not self.keyword(node, "filter"):
                self.report(
                    node,
                    "archive",
                    "TarFile.add keeps owners and times; pass filter= or addfile",
                )
            elif kind == "zip" and func.attr == "write":
                self.report(
                    node,
                    "archive",
                    "ZipFile.write keeps the file time; use writestr(ZipInfo)",
                )

    # -- order --------------------------------------------------------------

    def sorted_afterwards(self, node: ast.AST) -> bool:
        statement = self.statement_of(node)
        target: str | None = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            if isinstance(statement.targets[0], ast.Name):
                target = statement.targets[0].id
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            target = statement.target.id
        if target is None:
            return False
        for call in ast.walk(self.scope_of(node)):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "sort"
                and isinstance(func.value, ast.Name)
                and func.value.id == target
            ):
                return True
            if self.resolve(func) == "sorted" and any(
                isinstance(a, ast.Name) and a.id == target for a in call.args
            ):
                return True
        return False

    def ordered_context(self, node: ast.AST) -> bool:
        """The value is sorted (or only counted) before it matters."""
        ancestor = self.parents.get(node)
        while ancestor is not None and not isinstance(ancestor, ast.stmt):
            if isinstance(ancestor, ast.Call) and self.resolve(ancestor.func) in ORDER_SAFE:
                return True
            ancestor = self.parents.get(ancestor)
        return self.sorted_afterwards(node)

    def walk_sorted(self, node: ast.Call) -> bool:
        """``for root, dirs, files in os.walk(...)`` with dirs and files sorted."""
        parent = self.parents.get(node)
        if not isinstance(parent, ast.For) or not isinstance(parent.target, ast.Tuple):
            return False
        names = [e.id for e in parent.target.elts[1:] if isinstance(e, ast.Name)]
        names = [n for n in names if not n.startswith("_")]
        if not names:
            return False
        sorted_names = set()
        for call in (c for stmt in parent.body for c in ast.walk(stmt) if isinstance(c, ast.Call)):
            func = call.func
            if isinstance(func, ast.Attribute) and func.attr == "sort":
                if isinstance(func.value, ast.Name):
                    sorted_names.add(func.value.id)
            elif self.resolve(func) == "sorted":
                sorted_names.update(a.id for a in call.args if isinstance(a, ast.Name))
        return all(n in sorted_names for n in names)

    def check_listing(self, node: ast.Call, full: str | None) -> None:
        is_function = full in LISTING_FUNCS
        is_method = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in LISTING_METHODS
            and not (full or "").startswith("glob.")
        )
        if not (is_function or is_method):
            return
        if full in ("os.walk", "os.fwalk") and self.walk_sorted(node):
            return
        if self.ordered_context(node):
            return
        name = full if is_function else f".{node.func.attr}()"  # type: ignore[union-attr]
        self.report(
            node,
            "order",
            f"{name} lists in the order of the file system; wrap it in sorted(...)",
        )

    # -- random, time -----------------------------------------------------------

    def find_seeds(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and (node.args or node.keywords):
                full = self.resolve(node.func)
                if full == "random.seed":
                    self.seeded_random = True
                elif full == "numpy.random.seed":
                    self.seeded_numpy = True

    def check_call(self, node: ast.Call) -> None:
        full = self.resolve(node.func)
        has_args = bool(node.args or node.keywords)
        if full in TIME_ALWAYS or (full in TIME_NO_ARGS and not has_args):
            if not self.mentions_epoch:
                self.report(
                    node,
                    "time",
                    f"{full}() reads the current time; use {EPOCH} (or the commit time)",
                )
        elif full == "time.strftime" and len(node.args) < 2 and not self.mentions_epoch:
            self.report(node, "time", "time.strftime without a time uses the current time")
        if full in HOST_CALLS:
            self.report(node, "host", f"{full}() puts the builder's identity into the build")
        if full is not None:
            self.check_random(node, full, has_args)
        self.check_listing(node, full)
        self.check_archive(node, full)
        if full in MTIME_CALLS and not self.mentions_epoch:
            self.report(node, "mtime", f"{full}() depends on when the files were checked out")

    def check_random(self, node: ast.Call, full: str, has_args: bool) -> None:
        if full == "hash" and node.args and not isinstance(node.args[0], ast.Constant):
            self.report(
                node,
                "random",
                "hash() of text changes between runs (PYTHONHASHSEED); use hashlib",
            )
        elif full in ENTROPY_CALLS or full.startswith("secrets."):
            self.report(node, "random", f"{full}() gives different bytes every run")
        elif full == "random.seed" and not has_args:
            self.report(node, "random", "random.seed() without a value seeds from the system")
        elif full == "random.Random" and not has_args:
            self.report(node, "random", "random.Random() without a seed")
        elif full in ("numpy.random.default_rng", "numpy.random.RandomState") and not has_args:
            self.report(node, "random", f"{full}() without a seed")
        elif full.startswith("random.") and full.count(".") == 1 and full not in RANDOM_KEEP:
            if not self.seeded_random:
                self.report(node, "random", f"{full}() uses the shared generator, never seeded")
        elif (
            full.startswith("numpy.random.")
            and full.count(".") == 2
            and full not in NUMPY_KEEP
            and not self.seeded_numpy
        ):
            self.report(node, "random", f"{full}() uses the shared generator, never seeded")

    def check_attribute(self, node: ast.Attribute) -> None:
        if node.attr in MTIME_ATTRS and not self.mentions_epoch:
            self.report(
                node,
                "mtime",
                f".{node.attr} depends on when the files were checked out",
            )

    # -- build paths --------------------------------------------------------

    def check_literals(self) -> None:
        for node in ast.walk(self.tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in self.docstrings
                and not REGEX_LIKE.search(node.value)
                and BUILD_PATH_LITERAL.search(node.value)
            ):
                self.report(node, "path", "a path of the building computer in a string")

    def build_taint(self) -> dict[ast.AST, tuple[set[str], set[str], set[str]]]:
        """Per scope: (names bound there, tainted names, names holding tainted text)."""
        scopes: dict[ast.AST, tuple[set[str], set[str], set[str]]] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.Module):
                bound: set[str] = set()
                if not isinstance(node, ast.Module):
                    args = node.args
                    every = [*args.posonlyargs, *args.args, *args.kwonlyargs]
                    every += [a for a in (args.vararg, args.kwarg) if a]
                    bound.update(a.arg for a in every)
                scopes[node] = (bound, set(), set())
        for node in ast.walk(self.tree):
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr | ast.For):
                targets = [node.target]
            elif isinstance(node, ast.With):
                targets = [i.optional_vars for i in node.items if i.optional_vars]
            elif isinstance(node, ast.comprehension):
                targets = [node.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        scopes[self.scope_of(node)][0].add(name.id)
        return scopes

    def run_taint(self) -> None:
        scopes = self.build_taint()
        module = self.tree
        pairs: list[tuple[ast.AST, ast.AST, ast.AST]] = []  # (scope, target, value)
        for node in ast.walk(self.tree):
            scope = self.scope_of(node)
            if isinstance(node, ast.Assign):
                pairs += [(scope, t, node.value) for t in node.targets]
            elif (
                isinstance(node, ast.AnnAssign | ast.AugAssign) and getattr(node, "value", None)
            ) or isinstance(node, ast.NamedExpr):
                pairs.append((scope, node.target, node.value))
            elif isinstance(node, ast.With):
                pairs += [
                    (scope, i.optional_vars, i.context_expr) for i in node.items if i.optional_vars
                ]

        def lookup(scope: ast.AST, name: str, index: int) -> bool:
            if name in scopes[scope][0] or scope is module:
                return name in scopes[scope][index]
            return name in scopes[module][index]

        def tainted(expr: ast.AST, scope: ast.AST) -> bool:
            if isinstance(expr, ast.Name):
                return expr.id == "__file__" or lookup(scope, expr.id, 1)
            if isinstance(expr, ast.Call):
                full = self.resolve(expr.func)
                if full in PATH_SOURCES:
                    return True
                if full in PATH_CLEAN_FUNCS:
                    return False
                arguments = [*expr.args, *(k.value for k in expr.keywords)]
                if isinstance(expr.func, ast.Attribute):
                    attr = expr.func.attr
                    if attr in PATH_SOURCE_METHODS:
                        return True
                    if attr not in PATH_KEEP_METHODS:
                        return False
                    return any(tainted(c, scope) for c in [expr.func.value, *arguments])
                if full in PATH_KEEP_FUNCS:
                    return any(tainted(a, scope) for a in arguments)
                return False
            if isinstance(expr, ast.Attribute) and expr.attr in PATH_CLEAN_ATTRS:
                return False
            return any(tainted(c, scope) for c in ast.iter_child_nodes(expr))

        def stringifies(expr: ast.AST, scope: ast.AST) -> bool:
            for sub in ast.walk(expr):
                if isinstance(sub, ast.FormattedValue) and tainted(sub.value, scope):
                    return True
                if isinstance(sub, ast.Name) and lookup(scope, sub.id, 2):
                    return True
                if isinstance(sub, ast.Call):
                    full = self.resolve(sub.func)
                    arguments = [*sub.args, *(k.value for k in sub.keywords)]
                    if full in STRINGIFY_FUNCS and any(tainted(a, scope) for a in arguments):
                        return True
                    if isinstance(sub.func, ast.Attribute):
                        attr, base = sub.func.attr, sub.func.value
                        if attr in STRINGIFY_METHODS and tainted(base, scope):
                            return True
                        if (
                            attr == "format"
                            and is_str(base)
                            and any(tainted(a, scope) for a in arguments)
                        ):
                            return True
                if isinstance(sub, ast.BinOp):
                    one, other = sub.left, sub.right
                    if isinstance(sub.op, ast.Mod) and is_str(one) and tainted(other, scope):
                        return True
                    if isinstance(sub.op, ast.Add) and (
                        (is_str(one) and tainted(other, scope))
                        or (is_str(other) and tainted(one, scope))
                    ):
                        return True
            return False

        for _ in range(6):
            changed = False
            for scope, target, value in pairs:
                is_tainted = tainted(value, scope)
                is_text = stringifies(value, scope)
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        for flag, index in ((is_tainted, 1), (is_text, 2)):
                            if flag and name.id not in scopes[scope][index]:
                                scopes[scope][index].add(name.id)
                                changed = True
            if not changed:
                break

        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            full = self.resolve(node.func)
            is_sink = full in WRITE_FUNCS or (
                isinstance(node.func, ast.Attribute) and node.func.attr in WRITE_METHODS
            )
            if full == "print" and not self.keyword(node, "file"):
                is_sink = True
            if not is_sink:
                continue
            scope = self.scope_of(node)
            arguments = [*node.args, *(k.value for k in node.keywords)]
            if any(stringifies(a, scope) for a in arguments):
                self.report(
                    node,
                    "path",
                    "a path of the building computer is written out; make it relative "
                    "(relative_to) or drop it",
                )

    # -- run ---------------------------------------------------------------------

    def run(self) -> list[Problem]:
        self.find_seeds()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                self.check_call(node)
            elif isinstance(node, ast.Attribute):
                self.check_attribute(node)
        self.check_set_loops()
        self.check_literals()
        self.run_taint()
        return self.problems


def check_python(path: str, text: str) -> list[Problem]:
    try:
        return PythonChecker(path, text).run()
    except SyntaxError as err:
        return [Problem(path, err.lineno or 1, "syntax", f"cannot parse: {err.msg}")]


# ---------------------------------------------------------------------------
# Spec files and shell scripts
# ---------------------------------------------------------------------------

SHELL_SECTIONS = frozenset(
    {
        "%prep",
        "%generate_buildrequires",
        "%conf",
        "%build",
        "%install",
        "%check",
        "%clean",
        "%pre",
        "%post",
        "%preun",
        "%postun",
        "%pretrans",
        "%posttrans",
        "%preuninstall",
        "%verifyscript",
        "%triggerin",
        "%triggerun",
        "%triggerpostun",
        "%triggerprein",
    }
)
OTHER_SECTIONS = frozenset(
    {
        "%package",
        "%description",
        "%files",
        "%changelog",
        "%lang",
        "%sourcelist",
        "%patchlist",
    }
)
BUILD_PATH = re.compile(
    r"%\{?_?(?:buildroot|builddir|topdir|sourcedir|specdir|tmppath|rpmdir|srcrpmdir|buildrootdir)\}?"
    r"|\$\{?RPM_(?:BUILD_ROOT|BUILD_DIR|SOURCE_DIR)\}?"
    r"|\$\{?(?:PWD|OLDPWD)\}?|\$\(\s*pwd\s*\)|`\s*pwd\s*`"
)
SPEC_COMMANDS = r"date|hostname|whoami|logname|uname|uuidgen|uptime|id|git|svn|hg|shuf|hostid"
SPEC_SHELL = re.compile(rf"(?:(?<![\w/.-])|(?<=/bin/))(?:{SPEC_COMMANDS})(?![\w.-])")
LUA_CLOCK = re.compile(
    r"os\.(?:date|time|clock|getenv|tmpname)|math\.random|posix\.(?:getenv|uname|time|getpid|ctermid)"
)
CMD_START = r"(?:^|[;&|({`]|\$\(|\bthen\b|\bdo\b|\belse\b)\s*"
SHELL_CMD = re.compile(
    rf"{CMD_START}(?P<cmd>date|hostname|whoami|logname|uname|id|uuidgen|uptime|hostid)(?![\w.-])"
)
SHELL_SUBST = re.compile(
    r"(?:\$\(|`)\s*(?P<cmd>date|hostname|whoami|logname|uname|id|uuidgen|uptime|hostid)(?![\w.-])"
)
SHELL_VAR = re.compile(
    r"\$\{?(?:USER|LOGNAME|HOSTNAME|HOSTTYPE|RANDOM|SRANDOM|EPOCHSECONDS|EPOCHREALTIME|BASHPID)\b"
    r"|/dev/u?random"
)
DETERMINISTIC_DATE = re.compile(rf"{EPOCH}|(?:-d|--date)[ =]*[\"']?@")
HEREDOC = re.compile(r"<<-?\s*(?P<q>['\"]?)(?P<tag>\w+)(?P=q)")
SED_SUBST = re.compile(
    r"""['"]s(?P<d>[|/#,@:!])(?P<pat>(?:\\.|(?!(?P=d)).)*)(?P=d)(?P<rep>(?:\\.|(?!(?P=d)).)*)(?P=d)"""
)
OPTION_PATH = re.compile(
    r"(?:--(?:prefix|exec-prefix|bindir|sbindir|libdir|libexecdir|datadir|sysconfdir|"
    r"localstatedir|mandir|infodir)|-DCMAKE_\w*(?:PREFIX|DIR)\w*)=\S*?(?P<var>"
    + BUILD_PATH.pattern
    + ")"
)
SPEC_LITERAL = re.compile(
    r"(?<![\w/.%${}-])(?:/home/[A-Za-z_][\w.-]*|/root|/builddir|/usr/src/packages|/\.build)(?![\w-])"
)


def macro_bodies(line: str) -> Iterator[tuple[str, str]]:
    """The text of every ``%(...)`` and ``%{lua:...}`` of a line: (kind, body)."""
    for start in (m.start() for m in re.finditer(r"%(?=[({])", line)):
        opening = line[start + 1]
        closing = ")" if opening == "(" else "}"
        depth = 0
        for index in range(start + 1, len(line)):
            if line[index] == opening:
                depth += 1
            elif line[index] == closing:
                depth -= 1
                if depth == 0:
                    body = line[start + 2 : index]
                    if opening == "(":
                        yield "shell", body
                    elif body.startswith("lua:"):
                        yield "lua", body
                    break


@dataclass(frozen=True)
class Unit:
    line: int
    text: str
    kind: str  # "cmd", "heredoc" (unquoted tag) or "quoted-heredoc"


def shell_units(lines: Sequence[str], numbers: Sequence[int]) -> Iterator[Unit]:
    """Logical lines (a trailing backslash joins) and the bodies of here-documents."""
    pending: list[str] = []
    first = 0
    tag: str | None = None
    quoted = False
    for number, raw in zip(numbers, lines, strict=True):
        if tag is not None:
            if raw.strip() == tag:
                tag = None
            else:
                yield Unit(number, raw, "quoted-heredoc" if quoted else "heredoc")
            continue
        if not pending:
            first = number
        if raw.rstrip().endswith("\\"):
            pending.append(raw.rstrip()[:-1])
            continue
        pending.append(raw)
        text = " ".join(p.strip() for p in pending)
        pending = []
        yield Unit(first, text, "cmd")
        match = HEREDOC.search(text)
        if match:
            tag, quoted = match["tag"], bool(match["q"])
    if pending:
        yield Unit(first, " ".join(p.strip() for p in pending), "cmd")


def tar_problem(words: list[str]) -> str | None:
    """What a ``tar`` that makes an archive lacks, or None."""
    creating = False
    for index, word in enumerate(words):
        if word == "--create" or (
            word.startswith("-") and not word.startswith("--") and "c" in word
        ):
            creating = True
        elif index == 0 and not word.startswith("-") and re.fullmatch(r"[A-Za-z]+", word):
            creating = "c" in word
    if not creating:
        return None
    text = " ".join(words)
    missing = []
    if "--sort=name" not in text:
        missing.append("--sort=name")
    if "--mtime" not in text:
        missing.append("--mtime=")
    if "--owner" not in text and "--numeric-owner" not in text:
        missing.append("--owner=0 --group=0 or --numeric-owner")
    if missing:
        return "tar makes an archive without " + ", ".join(missing)
    return None


def option_words(command: str, name: str) -> list[list[str]]:
    """Words after each ``name`` that starts a command, up to the next separator."""
    found = []
    for match in re.finditer(rf"{CMD_START}{name}\s+(?P<rest>[^;&|)`]*)", command):
        found.append(match["rest"].split())
    return found


class SpecChecker:
    def __init__(self, path: str, text: str) -> None:
        self.path = path
        self.is_spec = path.endswith(".spec")
        self.lines = text.splitlines()
        self.problems: list[Problem] = []
        self.rustflags_remap = "remap-path-prefix" in text

    def report(self, number: int, rule: str, message: str) -> None:
        text = self.lines[number - 1].strip() if 0 < number <= len(self.lines) else ""
        self.problems.append(Problem(self.path, number, rule, message, text))

    def script_lines(self) -> tuple[list[int], list[str]]:
        """The lines of the shell sections (all of a shell script)."""
        numbers: list[int] = []
        found: list[str] = []
        active = not self.is_spec
        for number, line in enumerate(self.lines, 1):
            word = line.split(None, 1)[0] if line.strip() else ""
            if self.is_spec and word.startswith("%"):
                if word in SHELL_SECTIONS:
                    active = True
                    continue
                if word in OTHER_SECTIONS:
                    active = False
                    continue
            if active:
                numbers.append(number)
                found.append(line)
        return numbers, found

    def check_macros(self) -> None:
        for number, line in enumerate(self.lines, 1):
            for kind, body in macro_bodies(line):
                if kind == "shell":
                    if SPEC_SHELL.search(body) and not DETERMINISTIC_DATE.search(body):
                        self.report(
                            number,
                            "spec-time",
                            "rpm runs a command that gives a different answer on each build: "
                            f"%({body.strip()[:40]})",
                        )
                elif LUA_CLOCK.search(body) and EPOCH not in body:
                    self.report(
                        number,
                        "spec-time",
                        "a lua macro reads the clock, a variable or random",
                    )

    def check_units(self) -> None:
        numbers, lines = self.script_lines()
        for unit in shell_units(lines, numbers):
            self.check_unit(unit)

    def check_unit(self, unit: Unit) -> None:
        text, number = unit.text, unit.line
        if unit.kind == "cmd" and text.lstrip().startswith("#"):
            return
        if unit.kind == "cmd":
            self.check_shell_time(unit)
            self.check_commands(unit)
        elif unit.kind == "heredoc":
            for match in SHELL_SUBST.finditer(text):
                if match["cmd"] != "date" or not DETERMINISTIC_DATE.search(text):
                    self.report(
                        number,
                        "shell-time",
                        f"{match['cmd']} is expanded inside a here-document",
                    )
            if SHELL_VAR.search(text):
                self.report(number, "shell-time", "a variable of the builder is expanded here")
        if unit.kind != "cmd" and BUILD_PATH.search(text):
            self.report(number, "build-path", "the build folder is written into a file")
        if SPEC_LITERAL.search(text):
            self.report(number, "abs-path", "a path of the building computer")

    def check_shell_time(self, unit: Unit) -> None:
        text, number = unit.text, unit.line
        for match in [*SHELL_CMD.finditer(text), *SHELL_SUBST.finditer(text)]:
            cmd = match["cmd"]
            if cmd == "date" and DETERMINISTIC_DATE.search(text):
                continue
            self.report(number, "shell-time", f"{cmd} gives a different answer on each build")
        if SHELL_VAR.search(text):
            self.report(number, "shell-time", "a variable of the builder or a random value")

    def check_commands(self, unit: Unit) -> None:
        text, number = unit.text, unit.line
        if BUILD_PATH.search(text):
            self.check_embedded(unit)
        for words in option_words(text, "tar"):
            problem = tar_problem(words)
            if problem:
                self.report(number, "archive", problem)
        for words in option_words(text, "gzip"):
            flags = [w for w in words if w.startswith("-") and not w.startswith("--")]
            decompress = any(c in f for f in flags for c in "dlt") or "--decompress" in words
            if not decompress and not any("n" in f for f in flags) and "--no-name" not in words:
                self.report(number, "archive", "gzip without -n stores the file name and time")
        for words in option_words(text, "zip"):
            if not any(w.startswith("-") and "X" in w for w in words):
                self.report(number, "archive", "zip without -X stores owners and file times")
        self.check_order(unit)
        self.check_compilers(unit)

    def check_embedded(self, unit: Unit) -> None:
        text, number = unit.text, unit.line
        echo = re.search(rf"{CMD_START}(?:echo|printf)\b(?P<args>[^>|]*)", text)
        if echo and BUILD_PATH.search(echo["args"]):
            self.report(number, "build-path", "the build folder is written by echo/printf")
        for match in SED_SUBST.finditer(text):
            if BUILD_PATH.search(match["rep"]):
                self.report(number, "build-path", "sed puts the build folder into the files")
        if OPTION_PATH.search(text):
            self.report(number, "build-path", "an install option takes the build folder")

    def check_order(self, unit: Unit) -> None:
        text, number = unit.text, unit.line
        if re.search(r"\bsort\b", text):
            return
        for_loop = re.search(r"\bfor\s+\w+\s+in\s+(?:\$\(|`)\s*(?:ls|find)\b", text)
        listing = re.search(CMD_START + r"(?:ls|find)\b(?P<rest>.*)", text)
        if for_loop:
            self.report(number, "order", "a for loop over unsorted ls/find output")
        elif listing:
            rest = listing["rest"]
            if re.search(r"-print0?\b|-printf\b", rest) or re.search(
                r"\|\s*(?:while|xargs)\b", rest
            ):
                self.report(number, "order", "find/ls output is used in the order of the disk")

    def check_compilers(self, unit: Unit) -> None:
        text, number = unit.text, unit.line
        if (
            re.search(CMD_START + r"cargo\s+(?:build|install|rustc)\b", text)
            and "remap-path-prefix" not in text
            and not self.rustflags_remap
        ):
            self.report(
                number,
                "rust-path",
                "cargo build without --remap-path-prefix writes the source paths "
                "into the program (RUSTFLAGS)",
            )
        if re.search(CMD_START + r"go\s+(?:build|install)\b", text) and "-trimpath" not in text:
            self.report(number, "go-path", "go build without -trimpath writes the source paths")

    def run(self) -> list[Problem]:
        if self.is_spec:
            self.check_macros()
        self.check_units()
        return self.problems


def check_spec(path: str, text: str) -> list[Problem]:
    return SpecChecker(path, text).run()


# ---------------------------------------------------------------------------
# Files, markers, legacy
# ---------------------------------------------------------------------------


def check_text(path: str, text: str) -> list[Problem]:
    """All problems of one file, with the ``repro: ok`` lines and LEGACY applied."""
    found = check_python(path, text) if path.endswith(".py") else check_spec(path, text)
    lines = text.splitlines()
    result = []
    seen: set[tuple[int, str, str]] = set()
    for problem in sorted(found, key=lambda p: (p.line, p.rule, p.message)):
        if (problem.line, problem.rule, problem.message) in seen:
            continue
        seen.add((problem.line, problem.rule, problem.message))
        marked = [lines[i - 1] for i in (problem.line, problem.line - 1) if 0 < i <= len(lines)]
        if any(MARKER in line for line in marked):
            continue
        if (problem.path, problem.rule, problem.text) in LEGACY:
            problem = Problem(
                problem.path,
                problem.line,
                problem.rule,
                problem.message,
                problem.text,
                True,
            )
        result.append(problem)
    return result


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, text=True
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def default_files(tracked: Iterable[str]) -> list[str]:
    tracked_set = set(tracked)
    chosen = [p for p in tracked_set if SPEC_FILE.fullmatch(p) or IMAGE_SCRIPT.fullmatch(p)]
    chosen += [p for p in BUILD_SCRIPTS if p in tracked_set]
    return sorted(chosen)


def normalise(root: Path, arg: str) -> str | None:
    """A repo-relative path with slashes (None when the file is outside the repo).

    A relative name is looked up from the current folder, then from the root.
    """
    path = Path(arg)
    candidates = [path] if path.is_absolute() else [Path.cwd() / path, root / path]
    inside = []
    for candidate in candidates:
        try:
            inside.append(PurePosixPath(candidate.resolve().relative_to(root)).as_posix())
        except ValueError:
            continue
    existing = [rel for rel in inside if (root / rel).exists()]
    return (existing or inside or [None])[0]


def check_files(root: Path, files: Sequence[str]) -> tuple[list[Problem], list[str]]:
    """Problems and read errors (``path: message``) for repo-relative files."""
    problems: list[Problem] = []
    errors: list[str] = []
    for rel in files:
        if not rel.endswith((".py", ".spec", ".sh")):
            errors.append(f"{rel}: not a .py, .spec or .sh file")
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            errors.append(f"{rel}: cannot read: {err.strerror or err}")
            continue
        problems += check_text(rel, text)
    return problems, errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if any(a.startswith("-") for a in args):
        print("usage: python3 tools/check_reproducible.py [FILE ...]", file=sys.stderr)
        return 0 if args in (["-h"], ["--help"]) else 2
    if args:
        files = []
        for arg in args:
            rel = normalise(ROOT, arg)
            if rel is None:
                print(f"{arg}: not inside the repository", file=sys.stderr)
                return 2
            files.append(rel)
    else:
        try:
            files = default_files(tracked_files(ROOT))
        except (OSError, subprocess.CalledProcessError) as err:
            print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
            return 2
        if not files:
            print("no build files found", file=sys.stderr)
            return 2
    problems, errors = check_files(ROOT, files)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 2
    for problem in problems:
        print(problem, file=sys.stderr)
    failures = sum(not p.warning for p in problems)
    print(
        f"{len(files)} build files checked: {failures} errors, {len(problems) - failures} warnings"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
