# SPDX-License-Identifier: Apache-2.0
"""Hostile paths for the files MCP server: nothing outside the allowed folders may be touched.

Every test builds its own tree in a temporary folder (pytest ``tmp_path``) and attacks the
border of the allowed folder through the same entry the model uses (``Server.call``, that is
the ``tools/call`` of the protocol) or through ``Files`` directly. A refused call must say so
(``isError``), must not leak the content of the file it was aimed at, and must leave the whole
tree exactly as it was.

Groups:

* lexical escapes: ``..``, absolute and relative paths, a sibling folder with the same prefix,
  NUL bytes, values that are not strings, ``~``, ``$HOME``, URLs and Windows paths;
* symbolic links out of the roots (files, folders, chains, loops, dangling), ``..`` after a
  link, and the root itself being a link that is re-pointed later;
* a path that changes between two calls (a folder or file turned into a link, a backup that
  points elsewhere);
* odd but legal names (newline, quotes, RTL mark, emoji, backslashes, percent codes), NFC and
  NFD, look-alike dots and slashes, very long names and paths;
* folders where files are expected and the other way round, FIFOs, sockets, devices;
* files without read or write permission;
* size limits of reading, writing, listing and searching;
* the ``.snapshots`` folders, snapshot numbers and the ``restore`` backup ids;
* protocol level: unknown arguments, wrong types, the server surviving bad lines.

Only behaviour that the server has and promises is asserted; no test is skipped for a reason
other than the machine (running as root, a file system that merges NFC and NFD names).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import socket
import stat
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from vilna_mcp_files import files as files_module
from vilna_mcp_files.files import Files, FilesError
from vilna_mcp_files.server import Server, check_arguments

SECRET = "TOP-SECRET-OUTSIDE"
IS_ROOT = os.geteuid() == 0


# -- helpers -----------------------------------------------------------------


@dataclass
class Env:
    tmp: Path
    home: Path
    root: Path
    outside: Path
    backups: Path
    files: Files
    server: Server
    backup_id: str

    @property
    def secret(self) -> Path:
        return self.outside / "secret.txt"

    def call(self, tool: str, **args) -> dict:
        return self.server.call(tool, args)

    def fmt(self, template: str) -> str:
        for key, value in (("root", self.root), ("outside", self.outside), ("home", self.home)):
            template = template.replace("{" + key + "}", str(value))
        return template


@pytest.fixture
def env(tmp_path: Path) -> Env:
    home = tmp_path / "home"
    root = home / "Documents"
    root.mkdir(parents=True)
    (root / "sub").mkdir()
    (root / "sub" / "inner.txt").write_text("inner", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    (home / "Documents-evil").mkdir()
    (home / "Documents-evil" / "secret.txt").write_text(SECRET, encoding="utf-8")
    # snapper-like snapshot 1 of the home, so a refused restore_snapshot is refused for its path
    shutil.copytree(home, home / ".snapshots" / "1" / "snapshot", symlinks=True)
    backups = tmp_path / "backups"
    files = Files([root], backups, home=home)
    seed = root / "seed.txt"
    seed.write_text("one", encoding="utf-8")
    files.write(str(seed), "two")
    backup_id = files.write(str(seed), "three")["backup_id"]
    return Env(tmp_path, home, root, outside, backups, files, Server(files), backup_id)


def text_of(result: dict) -> str:
    return result["content"][0]["text"]


def state(top: Path, skip: tuple[Path, ...] = ()) -> dict[str, tuple]:
    """Everything below ``top`` (without following links): kind, mode and content."""
    seen: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if here / d not in skip]
        for name in [*dirnames, *filenames]:
            p = here / name
            st = p.lstat()
            rel = str(p.relative_to(top))
            if stat.S_ISLNK(st.st_mode):
                seen[rel] = ("link", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                seen[rel] = ("dir", stat.S_IMODE(st.st_mode))
            elif stat.S_ISREG(st.st_mode):
                try:
                    content = p.read_bytes()
                except PermissionError:
                    content = b"<unreadable>"
                seen[rel] = ("file", stat.S_IMODE(st.st_mode), content)
            else:
                seen[rel] = ("special", stat.S_IFMT(st.st_mode))
    return seen


def world(env: Env) -> dict[str, tuple]:
    return state(env.tmp, skip=(env.backups,))


def args_for(env: Env, tool: str, path: str) -> dict:
    if tool == "search":
        return {"path": path, "query": "SECRET"}
    if tool in ("create", "write"):
        return {"path": path, "text": "planted"}
    if tool == "restore":
        return {"path": path, "backup_id": env.backup_id}
    if tool == "restore_snapshot":
        return {"pre": 1, "paths": [path]}
    return {"path": path}


PATH_TOOLS = ["list", "read", "search", "create", "write", "delete", "restore", "restore_snapshot"]


def assert_refused_everywhere(env: Env, path: str, tools: list[str] | None = None) -> None:
    for tool in tools or PATH_TOOLS:
        before = world(env)
        result = env.call(tool, **args_for(env, tool, path))
        assert result["isError"] is True, (tool, path, text_of(result))
        assert SECRET not in text_of(result), (tool, path)
        assert world(env) == before, (tool, path)


class Hang(BaseException):
    """Not an Exception, so that no ``except OSError`` of the code under test can swallow it."""


@contextmanager
def deadline(seconds: float = 10.0) -> Iterator[None]:
    """Fail with Hang instead of hanging the test run."""

    def on_alarm(signum, frame):
        raise Hang(f"no answer within {seconds} s")

    old = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def link(env: Env, name: str, target: str | Path) -> Path:
    p = env.root / name
    os.symlink(target, p)
    return p


# -- lexical escapes ---------------------------------------------------------

LEXICAL = {
    "dotdot-to-outside": "{root}/../../outside/secret.txt",
    "dotdot-through-missing-folder": "{root}/missing/../../../outside/secret.txt",
    "dotdot-through-existing-folder": "{root}/sub/../../../outside/secret.txt",
    "dot-and-dotdot-mixed": "{root}/./sub/./../../../outside/./secret.txt",
    "many-dotdot": "{root}/" + "../" * 40 + "etc/passwd",
    "parent-of-root": "{root}/..",
    "parent-of-root-slash": "{root}/../",
    "home-folder": "{home}",
    "absolute-outside": "{outside}/secret.txt",
    "outside-folder": "{outside}",
    "double-slash-outside": "/{outside}/secret.txt",
    "triple-slash-outside": "//{outside}/secret.txt",
    "etc-passwd": "/etc/passwd",
    "proc-environ": "/proc/self/environ",
    "filesystem-root": "/",
    "sibling-with-same-prefix": "{home}/Documents-evil/secret.txt",
    "sibling-prefix-folder": "{home}/Documents-evil",
    "root-name-glued-to-dotdot": "{root}..",
    "relative-name": "secret.txt",
    "relative-dot": "./secret.txt",
    "relative-dotdot": "../outside/secret.txt",
    "dotdot-alone": "..",
    "dot-alone": ".",
    "tilde": "~/Documents/a.txt",
    "tilde-alone": "~",
    "env-variable": "$HOME/Documents/a.txt",
    "braced-env-variable": "${HOME}/Documents/a.txt",
    "file-url": "file:///etc/passwd",
    "windows-drive": "C:\\Windows\\win.ini",
    "windows-unc": "\\\\server\\share\\x",
    "space-only": " ",
    "relative-with-root-name": "Documents/seed.txt",
}


@pytest.mark.parametrize("template", list(LEXICAL.values()), ids=list(LEXICAL))
def test_lexical_escapes_are_refused_by_every_tool(env: Env, template: str) -> None:
    assert_refused_everywhere(env, env.fmt(template))


@pytest.mark.parametrize("tool", PATH_TOOLS)
def test_empty_path_is_refused(env: Env, tool: str) -> None:
    assert_refused_everywhere(env, "", [tool])


def test_double_slash_inside_the_root_never_reaches_outside(env: Env) -> None:
    inside = env.call("read", path="/" + str(env.root / "seed.txt"))
    # either served (it is inside the root) or refused; never anything else
    if not inside["isError"]:
        assert json.loads(text_of(inside))["text"] == "three"
    out = env.call("read", path="/" + str(env.secret))
    assert out["isError"] is True


NUL_PATHS = {
    "nul-at-end": "{root}/seed.txt\x00",
    "nul-then-dotdot": "{root}/seed.txt\x00/../../../outside/secret.txt",
    "nul-in-the-middle": "{root}/see\x00d.txt",
    "nul-before-outside-name": "{root}/\x00{outside}/secret.txt",
    "only-nul": "\x00",
    "nul-then-absolute": "\x00/etc/passwd",
}


@pytest.mark.parametrize("template", list(NUL_PATHS.values()), ids=list(NUL_PATHS))
def test_nul_in_a_path_is_refused(env: Env, template: str) -> None:
    assert_refused_everywhere(env, env.fmt(template))


@pytest.mark.parametrize("bad", [None, 5, 1.5, True, [], ["/etc/passwd"], {}, {"path": "/x"}])
def test_path_that_is_not_a_string_is_refused(env: Env, bad: object) -> None:
    for tool in ("list", "read", "delete"):
        assert env.call(tool, path=bad)["isError"] is True
    for tool in ("create", "write"):
        assert env.call(tool, path=bad, text="x")["isError"] is True
    assert env.call("search", path=bad, query="x")["isError"] is True
    assert env.call("restore", path=bad, backup_id=env.backup_id)["isError"] is True
    assert env.call("restore_snapshot", pre=1, paths=[bad])["isError"] is True


def test_paths_that_are_not_strings_are_refused_by_files_itself(env: Env) -> None:
    for bad in (None, 7, b"/etc/passwd", Path("/etc/passwd"), ["/etc/passwd"]):
        for method in (env.files.list, env.files.read, env.files.delete):
            with pytest.raises(FilesError):
                method(bad)  # type: ignore[arg-type]


def test_files_refuses_relative_and_outside_paths_directly(env: Env) -> None:
    for bad in ("seed.txt", "./seed.txt", "../x", f"{env.root}/../outside/secret.txt"):
        with pytest.raises(FilesError):
            env.files.read(bad)
    with pytest.raises(FilesError):
        env.files.create(f"{env.root}/../outside/new.txt", "x")
    assert not (env.outside / "new.txt").exists()


def test_percent_encoded_and_url_forms_are_only_names(env: Env) -> None:
    for name in ("%2e%2e", "%2E%2E%2Foutside", "..%2f..%2foutside", "%00", "%252e%252e"):
        path = f"{env.root}/{name}/secret.txt"
        assert_refused_everywhere(env, path, ["read", "list", "delete", "write", "search"])
    assert env.secret.read_text(encoding="utf-8") == SECRET


def test_backslashes_are_part_of_a_name_not_a_separator(env: Env) -> None:
    name = "..\\..\\outside\\planted.txt"
    made = env.call("create", path=f"{env.root}/{name}", text="x")
    assert made["isError"] is False
    assert (env.root / name).read_text(encoding="utf-8") == "x"
    assert not (env.outside / "planted.txt").exists()
    assert not (env.tmp / "planted.txt").exists()


def test_a_path_with_dotdot_after_a_newline_still_collapses_as_a_path(env: Env) -> None:
    assert_refused_everywhere(env, f"{env.root}/a\n/../../../outside/secret.txt")
    assert_refused_everywhere(env, f"{env.root}/..\n/../../outside/secret.txt")


RELATIVE_INSIDE = [
    "seed.txt",
    "./seed.txt",
    "sub/inner.txt",
    "sub/../seed.txt",
    "../Documents/seed.txt",
]


@pytest.mark.parametrize("relative", RELATIVE_INSIDE)
def test_a_relative_path_is_refused_even_when_it_would_land_inside_the_root(
    env: Env, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    monkeypatch.chdir(env.root)
    assert (env.root / "seed.txt").exists()
    assert_refused_everywhere(env, relative)
    with pytest.raises(FilesError):
        env.files.read(relative)


# -- symbolic links ----------------------------------------------------------


def _scenario_targets(env: Env) -> dict[str, Callable[[], tuple[str, list[str]]]]:
    """name -> function that builds the link and returns (link path, extra paths below it)."""

    def simple(target: str | Path, below: list[str] | None = None):
        def build() -> tuple[str, list[str]]:
            p = link(env, "ln", target)
            return str(p), [str(p / b) for b in below or []]

        return build

    def chain() -> tuple[str, list[str]]:
        link(env, "l3", env.secret)
        link(env, "l2", env.root / "l3")
        p = link(env, "l1", env.root / "l2")
        return str(p), []

    def long_chain() -> tuple[str, list[str]]:
        previous: str | Path = env.secret
        for i in range(45):
            previous = link(env, f"c{i}", previous)
        return str(previous), []

    def dir_chain() -> tuple[str, list[str]]:
        link(env, "d2", env.outside)
        p = link(env, "d1", env.root / "d2")
        return str(p), [str(p / "secret.txt"), str(p / "new.txt")]

    return {
        "file-absolute": simple(env.secret),
        "file-relative": simple("../../outside/secret.txt"),
        "file-relative-deep": simple("sub/../../../outside/secret.txt"),
        "folder-absolute": simple(env.outside, ["secret.txt", "new.txt", "."]),
        "folder-relative": simple("../../outside", ["secret.txt", "new.txt"]),
        "parent-of-the-tree": simple(
            env.tmp, ["outside/secret.txt", "home/Documents-evil/secret.txt"]
        ),
        "filesystem-root": simple("/", ["etc/passwd", "tmp"]),
        "etc-passwd": simple("/etc/passwd"),
        "dev-zero": simple("/dev/zero"),
        "dev-null": simple("/dev/null"),
        "dangling-outside": simple(env.outside / "not-there-yet.txt"),
        "dangling-in-outside-folder": simple(env.outside / "no" / "such" / "file.txt"),
        "link-to-the-sibling": simple(env.home / "Documents-evil", ["secret.txt"]),
        "chain-to-outside": chain,
        "chain-of-45-to-outside": long_chain,
        "folder-chain": dir_chain,
    }


SCENARIOS = [
    "file-absolute",
    "file-relative",
    "file-relative-deep",
    "folder-absolute",
    "folder-relative",
    "parent-of-the-tree",
    "filesystem-root",
    "etc-passwd",
    "dev-zero",
    "dev-null",
    "dangling-outside",
    "dangling-in-outside-folder",
    "link-to-the-sibling",
    "chain-to-outside",
    "chain-of-45-to-outside",
    "folder-chain",
]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_links_out_of_the_root_are_refused_by_every_tool(env: Env, scenario: str) -> None:
    path, below = _scenario_targets(env)[scenario]()
    with deadline():
        for p in [path, *below]:
            assert_refused_everywhere(env, p)


def test_search_does_not_follow_or_report_links_out(env: Env) -> None:
    link(env, "file-link", env.secret)
    link(env, "folder-link", env.outside)
    link(env, "link-to-parent", env.tmp)
    link(env, "relative-folder-link", "../../outside")
    (env.root / "sub" / "deep-link").symlink_to(env.outside)
    (env.root / "real.txt").write_text(f"mentions {SECRET}", encoding="utf-8")
    result = env.call("search", path=str(env.root), query="TOP-SECRET")
    assert result["isError"] is False
    hits = [m["path"] for m in json.loads(text_of(result))["matches"]]
    assert hits == [str(env.root / "real.txt")]


def test_search_by_name_does_not_report_a_link_out_either(env: Env) -> None:
    link(env, "secret-link.txt", env.secret)
    link(env, "secret-folder", env.outside)
    result = env.call("search", path=str(env.root), query="secret")
    assert json.loads(text_of(result))["matches"] == []


def test_list_shows_a_link_as_a_name_and_never_as_a_folder(env: Env) -> None:
    link(env, "out-folder", env.outside)
    link(env, "out-file", env.secret)
    listing = json.loads(text_of(env.call("list", path=str(env.root))))
    entries = {e["name"]: e for e in listing["entries"]}
    assert entries["out-folder"]["type"] != "folder"
    assert entries["out-file"]["type"] != "folder"
    assert SECRET not in json.dumps(entries)
    assert entries["sub"]["type"] == "folder"


def test_self_loop_and_two_link_loop_are_refused_without_hanging(env: Env) -> None:
    link(env, "self", env.root / "self")
    link(env, "ping", env.root / "pong")
    link(env, "pong", env.root / "ping")
    with deadline():
        for name in ("self", "ping", "pong", "self/x", "ping/x"):
            for tool in ("read", "write", "delete", "list", "search"):
                result = env.call(tool, **args_for(env, tool, f"{env.root}/{name}"))
                assert result["isError"] is True, (tool, name)
        for name in ("self", "ping", "pong"):
            assert env.call("create", path=f"{env.root}/{name}", text="x")["isError"] is True
        assert env.call("create", path=f"{env.root}/self/x", text="x")["isError"] is True
    assert os.path.islink(env.root / "self")


def test_link_inside_the_root_works_and_stays_a_link_after_write(env: Env) -> None:
    target = env.root / "sub" / "inner.txt"
    ln = link(env, "to-inner", target)
    assert json.loads(text_of(env.call("read", path=str(ln))))["text"] == "inner"
    assert env.call("write", path=str(ln), text="changed")["isError"] is False
    assert ln.is_symlink()
    assert target.read_text(encoding="utf-8") == "changed"
    assert not (env.outside / "inner.txt").exists()


def test_link_to_a_folder_inside_the_root_lets_create_land_inside(env: Env) -> None:
    link(env, "to-sub", env.root / "sub")
    made = env.call("create", path=f"{env.root}/to-sub/new.txt", text="hello")
    assert made["isError"] is False
    assert (env.root / "sub" / "new.txt").read_text(encoding="utf-8") == "hello"
    assert json.loads(text_of(made))["path"] == str(env.root / "sub" / "new.txt")


def test_dotdot_after_a_link_means_the_parent_of_the_target(env: Env) -> None:
    deep = env.root / "a" / "b"
    deep.mkdir(parents=True)
    link(env, "inside-link", deep)
    ok = env.call("create", path=f"{env.root}/inside-link/../made.txt", text="x")
    assert ok["isError"] is False
    assert (env.root / "a" / "made.txt").read_text(encoding="utf-8") == "x"
    assert not (env.root / "made.txt").exists()
    (env.outside / "deep").mkdir()
    link(env, "outside-link", env.outside / "deep")
    assert_refused_everywhere(env, f"{env.root}/outside-link/../secret.txt")
    assert_refused_everywhere(env, f"{env.root}/outside-link/../new.txt")


def test_link_into_a_second_root_is_allowed_and_into_a_gap_is_not(tmp_path: Path) -> None:
    a, b, gap = tmp_path / "A", tmp_path / "B", tmp_path / "gap"
    for d in (a, b, gap):
        d.mkdir()
    (b / "f.txt").write_text("in B", encoding="utf-8")
    (gap / "f.txt").write_text("in the gap", encoding="utf-8")
    os.symlink(b / "f.txt", a / "to-b")
    os.symlink(gap / "f.txt", a / "to-gap")
    server = Server(Files([a, b], tmp_path / "bk", home=tmp_path))
    ok = server.call("read", {"path": str(a / "to-b")})
    assert json.loads(text_of(ok))["text"] == "in B"
    assert server.call("read", {"path": str(a / "to-gap")})["isError"] is True
    assert server.call("read", {"path": str(gap / "f.txt")})["isError"] is True


def test_root_that_is_a_link_is_resolved_once_and_stays_pinned(tmp_path: Path) -> None:
    real, other = tmp_path / "real", tmp_path / "other"
    real.mkdir()
    other.mkdir()
    (real / "f.txt").write_text("real", encoding="utf-8")
    (other / "f.txt").write_text("other", encoding="utf-8")
    door = tmp_path / "door"
    os.symlink(real, door)
    server = Server(Files([door], tmp_path / "bk", home=tmp_path))
    assert not server.call("read", {"path": str(door / "f.txt")})["isError"]
    assert not server.call("read", {"path": str(real / "f.txt")})["isError"]
    door.unlink()
    os.symlink(other, door)  # somebody re-points the link after start-up
    assert server.call("read", {"path": str(door / "f.txt")})["isError"] is True
    assert server.call("write", {"path": str(door / "f.txt"), "text": "x"})["isError"] is True
    assert (other / "f.txt").read_text(encoding="utf-8") == "other"
    assert server.call("read", {"path": str(real / "f.txt")})["isError"] is False


def test_root_with_trailing_slash_and_dotdot_is_normalised(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "f.txt").write_text("x", encoding="utf-8")
    roots = [Path(f"{real}/"), Path(f"{tmp_path}/real/../real")]
    files = Files(roots, tmp_path / "bk", home=tmp_path)
    assert files.roots == (real, real)
    assert Server(files).call("read", {"path": str(tmp_path / "other" / "f.txt")})["isError"]


def test_no_roots_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FilesError):
        Files([], tmp_path / "bk", home=tmp_path)


def test_root_itself_cannot_be_replaced_or_deleted(env: Env) -> None:
    for tool in ("delete", "write"):
        assert env.call(tool, **args_for(env, tool, str(env.root)))["isError"] is True
    assert env.call("create", path=str(env.root), text="x")["isError"] is True
    assert env.root.is_dir() and (env.root / "seed.txt").exists()
    assert env.call("read", path=str(env.root))["isError"] is True


# -- a path that changes between two calls -----------------------------------


def test_folder_turned_into_a_link_between_calls_is_refused(env: Env) -> None:
    (env.root / "box").mkdir()
    made = env.call("create", path=f"{env.root}/box/a.txt", text="mine")
    assert made["isError"] is False
    (env.root / "box" / "a.txt").rename(env.outside / "a.txt")
    (env.root / "box").rmdir()
    os.symlink(env.outside, env.root / "box")
    (env.outside / "a.txt").write_text(SECRET, encoding="utf-8")
    assert_refused_everywhere(env, f"{env.root}/box/a.txt")
    assert_refused_everywhere(env, f"{env.root}/box/new.txt")


def test_file_turned_into_a_link_out_between_calls_is_refused(env: Env) -> None:
    path = env.root / "doc.txt"
    path.write_text("mine", encoding="utf-8")
    assert env.call("read", path=str(path))["isError"] is False
    path.unlink()
    os.symlink(env.secret, path)
    assert_refused_everywhere(env, str(path))
    assert env.secret.read_text(encoding="utf-8") == SECRET


def test_restore_after_the_file_became_a_link_out_is_refused(env: Env) -> None:
    path = env.root / "doc.txt"
    created = env.files.create(str(path), "v1")
    changed = env.files.write(str(path), "v2")
    path.unlink()
    os.symlink(env.secret, path)
    for backup_id in (created["backup_id"], changed["backup_id"]):
        before = world(env)
        result = env.call("restore", path=str(path), backup_id=backup_id)
        assert result["isError"] is True
        assert world(env) == before
    assert env.secret.read_text(encoding="utf-8") == SECRET


def test_restore_after_the_folder_became_a_link_out_is_refused(env: Env) -> None:
    (env.root / "box").mkdir()
    path = env.root / "box" / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    deleted = env.files.delete(str(path))
    (env.root / "box").rmdir()
    os.symlink(env.outside, env.root / "box")
    before = world(env)
    result = env.call("restore", path=str(path), backup_id=deleted["backup_id"])
    assert result["isError"] is True
    assert world(env) == before
    assert not (env.outside / "doc.txt").exists()


def test_a_backup_whose_record_points_outside_is_not_restored(env: Env) -> None:
    path = env.root / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    backup_id = env.files.write(str(path), "v2")["backup_id"]
    meta_path = env.backups / backup_id / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for forged in (str(env.secret), "../../outside/secret.txt", "doc.txt", str(env.home), "/"):
        meta["path"] = forged
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        before = world(env)
        for target in (str(path), forged):
            assert env.call("restore", path=target, backup_id=backup_id)["isError"] is True
        assert world(env) == before
    assert env.secret.read_text(encoding="utf-8") == SECRET


def test_a_backup_of_one_file_cannot_be_put_on_another(env: Env) -> None:
    a, b = env.root / "a.txt", env.root / "b.txt"
    a.write_text("a1", encoding="utf-8")
    b.write_text("b1", encoding="utf-8")
    backup_id = env.files.write(str(a), "a2")["backup_id"]
    before = world(env)
    assert env.call("restore", path=str(b), backup_id=backup_id)["isError"] is True
    assert env.call("restore", path=str(env.secret), backup_id=backup_id)["isError"] is True
    assert world(env) == before


def test_a_file_created_after_the_check_is_not_overwritten_by_create(env: Env) -> None:
    path = env.root / "late.txt"
    path.write_text("somebody was faster", encoding="utf-8")
    assert env.call("create", path=str(path), text="mine")["isError"] is True
    assert path.read_text(encoding="utf-8") == "somebody was faster"
    link(env, "late-link", env.secret)
    assert env.call("create", path=f"{env.root}/late-link", text="mine")["isError"] is True
    assert env.secret.read_text(encoding="utf-8") == SECRET


# -- odd but legal names -----------------------------------------------------

ODD_NAMES = [
    "a\nb.txt",
    "line\r\nbreak",
    "tab\there",
    "quote'single",
    'quote"double',
    "-rf",
    "--help",
    "...",
    "a..b",
    ".hidden",
    "trailing space ",
    " leading space",
    "back\\slash",
    "..\\..\\evil",
    "\u202etxt.exe",
    "\u200bzero-width",
    "файл з пробілом.txt",
    "😀.txt",
    "*",
    "?",
    "[a-z]",
    "$(id)",
    "`id`",
    ";rm -rf ~",
    "|pipe",
    "%2e%2e",
    "a" * 200,
    "я" * 90,
]


@pytest.mark.parametrize("name", ODD_NAMES, ids=[repr(n)[:30] for n in ODD_NAMES])
def test_odd_names_stay_inside_the_root(env: Env, name: str) -> None:
    path = f"{env.root}/{name}"
    before = state(env.tmp, skip=(env.backups, env.root))
    made = env.call("create", path=path, text="body")
    assert made["isError"] is False, text_of(made)
    assert json.loads(text_of(made))["path"] == str(env.root / name)
    assert name in os.listdir(env.root)
    assert json.loads(text_of(env.call("read", path=path)))["text"] == "body"
    assert env.call("write", path=path, text="body2")["isError"] is False
    listing = json.loads(text_of(env.call("list", path=str(env.root))))
    assert name in [e["name"] for e in listing["entries"]]
    deleted = env.call("delete", path=path)
    assert deleted["isError"] is False
    assert name not in os.listdir(env.root)
    backup_id = json.loads(text_of(deleted))["backup_id"]
    assert env.call("restore", path=path, backup_id=backup_id)["isError"] is False
    assert (env.root / name).read_text(encoding="utf-8") == "body2"
    assert state(env.tmp, skip=(env.backups, env.root)) == before


def test_odd_names_are_found_by_search_by_name(env: Env) -> None:
    for name in ("a\nb.txt", "\u202etxt.exe", "😀.txt", "back\\slash", "я-файл"):
        (env.root / name).write_bytes(b"")
    for query, name in (("a\nb", "a\nb.txt"), ("\u202e", "\u202etxt.exe"), ("😀", "😀.txt")):
        out = json.loads(text_of(env.call("search", path=str(env.root), query=query)))
        assert [m["path"] for m in out["matches"]] == [str(env.root / name)]
    out = json.loads(text_of(env.call("search", path=str(env.root), query="Я-ФАЙЛ")))
    assert [m["path"] for m in out["matches"]] == [str(env.root / "я-файл")]


def test_name_with_a_newline_cannot_hide_a_second_path(env: Env) -> None:
    path = f"{env.root}/ok.txt\n{env.secret}"
    assert env.call("read", path=path)["isError"] is True
    made = env.call("create", path=path, text="x")
    assert made["isError"] is True  # the folder part "…/ok.txt\n/tmp/…" does not exist
    assert env.secret.read_text(encoding="utf-8") == SECRET


def test_lookalike_dots_and_slashes_are_ordinary_names(env: Env) -> None:
    for name in ("\uff0e\uff0e", "\u2025", "\u2026", "\u2215", "\uff0f", "\u29f8", "\u2216"):
        path = f"{env.root}/{name}/secret.txt"
        assert_refused_everywhere(env, path, ["read", "write", "delete", "list"])
        flat = f"{env.root}/x{name}y.txt"
        assert env.call("create", path=flat, text="x")["isError"] is False
        assert (env.root / f"x{name}y.txt").exists()
    assert not (env.outside / "y.txt").exists()
    assert env.secret.read_text(encoding="utf-8") == SECRET


def test_nfc_and_nfd_names_are_different_files_and_never_mixed_up(env: Env) -> None:
    nfc = unicodedata.normalize("NFC", "caf\u00e9-\u0439.txt")
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    folder = env.root / "names"
    folder.mkdir()
    (folder / nfc).write_text("nfc", encoding="utf-8")
    (folder / nfd).write_text("nfd", encoding="utf-8")
    if len(os.listdir(folder)) != 2:
        pytest.skip("this file system merges NFC and NFD names")
    assert json.loads(text_of(env.call("read", path=f"{folder}/{nfc}")))["text"] == "nfc"
    assert json.loads(text_of(env.call("read", path=f"{folder}/{nfd}")))["text"] == "nfd"
    assert env.call("write", path=f"{folder}/{nfd}", text="nfd2")["isError"] is False
    assert (folder / nfc).read_text(encoding="utf-8") == "nfc"
    assert env.call("delete", path=f"{folder}/{nfc}")["isError"] is False
    assert (folder / nfd).read_text(encoding="utf-8") == "nfd2"


def test_a_root_spelled_in_the_other_normal_form_is_a_different_place(tmp_path: Path) -> None:
    nfc = unicodedata.normalize("NFC", "Документи-\u00e9")
    nfd = unicodedata.normalize("NFD", nfc)
    allowed, twin = tmp_path / nfc, tmp_path / nfd
    allowed.mkdir()
    twin.mkdir(exist_ok=True)
    if len(os.listdir(tmp_path)) != 2:
        pytest.skip("this file system merges NFC and NFD names")
    (allowed / "f.txt").write_text("allowed", encoding="utf-8")
    (twin / "f.txt").write_text("twin", encoding="utf-8")
    server = Server(Files([allowed], tmp_path / "bk", home=tmp_path))
    ok = server.call("read", {"path": str(allowed / "f.txt")})
    assert json.loads(text_of(ok))["text"] == "allowed"
    twin_read = server.call("read", {"path": str(twin / "f.txt")})
    assert twin_read["isError"] is True and "twin" not in text_of(twin_read)
    assert server.call("write", {"path": str(twin / "f.txt"), "text": "x"})["isError"] is True
    assert (twin / "f.txt").read_text(encoding="utf-8") == "twin"


def test_case_variants_of_the_root_are_not_the_root_on_a_case_sensitive_system(env: Env) -> None:
    variant = str(env.root).replace("Documents", "documents")
    if os.path.exists(variant):
        pytest.skip("this file system ignores case")
    result = env.call("create", path=f"{variant}/x.txt", text="x")
    assert result["isError"] is True
    assert not os.path.exists(variant)


def test_a_name_of_255_bytes_can_be_read_and_listed_and_256_is_a_clean_error(env: Env) -> None:
    for name in ("n" * 255, "\u044f" * 127 + "n"):
        (env.root / name).write_text("long", encoding="utf-8")
        assert json.loads(text_of(env.call("read", path=f"{env.root}/{name}")))["text"] == "long"
        listing = json.loads(text_of(env.call("list", path=str(env.root))))
        assert name in [e["name"] for e in listing["entries"]]
        found = json.loads(text_of(env.call("search", path=str(env.root), query=name)))
        assert [m["path"] for m in found["matches"]] == [str(env.root / name)]
    before = world(env)
    for name in ("n" * 256, "\u044f" * 128, "n" * 5000):
        for tool in ("create", "read", "write", "delete", "list", "search"):
            result = env.call(tool, **args_for(env, tool, f"{env.root}/{name}"))
            assert result["isError"] is True, (tool, len(name))
    assert world(env) == before


def test_very_long_paths_are_refused_without_a_crash(env: Env) -> None:
    deep_missing = f"{env.root}/" + "/".join(["a"] * 3000)
    many_dotdot = f"{env.root}/" + "/".join([".."] * 5000) + "/etc/passwd"
    huge = f"{env.root}/" + "x" * 1_000_000
    for path in (deep_missing, many_dotdot, huge):
        for tool in ("read", "write", "create", "delete", "list", "search"):
            result = env.call(tool, **args_for(env, tool, path))
            assert result["isError"] is True, (tool, len(path))


def test_a_file_200_folders_deep_is_reachable_and_dotdot_cannot_climb_out(env: Env) -> None:
    deep = env.root
    for _ in range(200):
        deep = deep / "d"
    deep.mkdir(parents=True)
    path = str(deep / "f.txt")
    assert env.call("create", path=path, text="deep")["isError"] is False
    assert json.loads(text_of(env.call("read", path=path)))["text"] == "deep"
    escape = str(deep) + "/.." * 205 + "/../../outside/secret.txt"
    assert_refused_everywhere(env, escape, ["read", "write", "delete"])


# -- folders, files and special files ---------------------------------------


def test_a_folder_is_not_read_written_or_deleted_as_a_file(env: Env) -> None:
    folder = str(env.root / "sub")
    before = world(env)
    for tool in ("read", "write", "delete"):
        assert env.call(tool, **args_for(env, tool, folder))["isError"] is True
    assert env.call("create", path=folder, text="x")["isError"] is True
    assert env.call("restore", path=folder, backup_id=env.backup_id)["isError"] is True
    assert world(env) == before
    assert (env.root / "sub" / "inner.txt").read_text(encoding="utf-8") == "inner"


def test_a_file_is_not_listed_or_searched_as_a_folder(env: Env) -> None:
    path = str(env.root / "seed.txt")
    for tool in ("list", "search"):
        assert env.call(tool, **args_for(env, tool, path))["isError"] is True
    below = f"{path}/child.txt"
    for tool in ("read", "write", "delete", "create", "list"):
        assert env.call(tool, **args_for(env, tool, below))["isError"] is True
    assert (env.root / "seed.txt").read_text(encoding="utf-8") == "three"


def test_create_does_not_make_missing_folders(env: Env) -> None:
    before = world(env)
    for path in (f"{env.root}/new-folder/a.txt", f"{env.root}/a/b/c/d.txt"):
        assert env.call("create", path=path, text="x")["isError"] is True
    assert world(env) == before


def test_a_missing_file_is_a_clean_error_for_every_tool(env: Env) -> None:
    missing = f"{env.root}/missing.txt"
    before = world(env)
    for tool in ("read", "write", "delete", "list", "search"):
        assert env.call(tool, **args_for(env, tool, missing))["isError"] is True
    assert env.call("restore", path=missing, backup_id="0" * 20)["isError"] is True
    assert world(env) == before


def test_fifo_is_refused_at_once_and_stays(env: Env) -> None:
    fifo = env.root / "pipe"
    os.mkfifo(fifo)
    with deadline():
        for tool in ("read", "write", "delete", "list", "search"):
            assert env.call(tool, **args_for(env, tool, str(fifo)))["isError"] is True, tool
        assert env.call("create", path=str(fifo), text="x")["isError"] is True
        assert env.call("restore", path=str(fifo), backup_id=env.backup_id)["isError"] is True
    assert stat.S_ISFIFO(fifo.lstat().st_mode)


def test_list_shows_a_fifo_without_opening_it(env: Env) -> None:
    os.mkfifo(env.root / "pipe")
    with deadline():
        result = env.call("list", path=str(env.root))
    names = [e["name"] for e in json.loads(text_of(result))["entries"]]
    assert "pipe" in names


def test_link_to_a_fifo_inside_the_root_is_refused_at_once(env: Env) -> None:
    os.mkfifo(env.root / "pipe")
    link(env, "to-pipe", env.root / "pipe")
    with deadline():
        for tool in ("read", "write", "delete"):
            assert env.call(tool, **args_for(env, tool, f"{env.root}/to-pipe"))["isError"] is True


def test_fifo_outside_the_root_is_refused_at_once(env: Env) -> None:
    os.mkfifo(env.outside / "pipe")
    link(env, "to-outside-pipe", env.outside / "pipe")
    with deadline():
        assert_refused_everywhere(env, str(env.outside / "pipe"), ["read", "write", "delete"])
        assert_refused_everywhere(env, f"{env.root}/to-outside-pipe", ["read", "write", "delete"])


def test_unix_socket_is_refused(env: Env) -> None:
    path = env.root / "s.sock"
    server = socket.socket(socket.AF_UNIX)
    try:
        try:
            server.bind(str(path))
        except OSError:
            pytest.skip("the temporary folder path is too long for a unix socket")
        with deadline():
            for tool in ("read", "write", "delete"):
                assert env.call(tool, **args_for(env, tool, str(path)))["isError"] is True
            assert env.call("create", path=str(path), text="x")["isError"] is True
        assert stat.S_ISSOCK(path.lstat().st_mode)
    finally:
        server.close()


def test_devices_are_refused(env: Env) -> None:
    with deadline():
        for dev in ("/dev/zero", "/dev/null", "/dev/random", "/dev/stdin", "/dev/tty"):
            for tool in ("read", "write", "delete", "list"):
                assert env.call(tool, **args_for(env, tool, dev))["isError"] is True


# -- permissions -------------------------------------------------------------


@pytest.fixture
def restore_modes() -> Iterator[list[Path]]:
    """Give back the mode of what a test locked, so pytest can clean the folder up."""
    locked: list[Path] = []
    yield locked
    for p in locked:
        if p.exists():
            p.chmod(0o700)


@pytest.mark.skipif(IS_ROOT, reason="root reads files whatever their mode")
def test_a_file_without_read_permission_is_a_clean_error(env: Env, restore_modes: list) -> None:
    secret = env.root / "private.txt"
    secret.write_text("hidden", encoding="utf-8")
    secret.chmod(0)
    restore_modes.append(secret)
    for tool in ("read", "write", "delete"):
        before = world(env)
        result = env.call(tool, **args_for(env, tool, str(secret)))
        assert result["isError"] is True, tool
        assert "hidden" not in text_of(result)
        assert world(env) == before


@pytest.mark.skipif(IS_ROOT, reason="root reads files whatever their mode")
def test_search_skips_what_it_cannot_read_and_goes_on(env: Env, restore_modes: list) -> None:
    (env.root / "a-unreadable.txt").write_text("needle", encoding="utf-8")
    (env.root / "a-unreadable.txt").chmod(0)
    (env.root / "locked").mkdir()
    (env.root / "locked" / "needle.txt").write_text("needle", encoding="utf-8")
    (env.root / "locked").chmod(0)
    restore_modes += [env.root / "a-unreadable.txt", env.root / "locked"]
    (env.root / "z-readable.txt").write_text("needle", encoding="utf-8")
    result = env.call("search", path=str(env.root), query="needle")
    assert result["isError"] is False
    hits = [m["path"] for m in json.loads(text_of(result))["matches"]]
    assert str(env.root / "z-readable.txt") in hits
    assert str(env.root / "a-unreadable.txt") not in hits


@pytest.mark.skipif(IS_ROOT, reason="root lists folders whatever their mode")
def test_a_folder_without_permission_is_a_clean_error(env: Env, restore_modes: list) -> None:
    locked = env.root / "locked"
    locked.mkdir()
    (locked / "f.txt").write_text("x", encoding="utf-8")
    locked.chmod(0)
    restore_modes.append(locked)
    for tool in ("list", "read", "write", "delete"):
        path = str(locked if tool == "list" else locked / "f.txt")
        assert env.call(tool, **args_for(env, tool, path))["isError"] is True, tool
    found = env.call("search", path=str(locked), query="x")
    if not found["isError"]:
        assert json.loads(text_of(found))["matches"] == []
    assert env.call("create", path=f"{locked}/n.txt", text="x")["isError"] is True


@pytest.mark.skipif(IS_ROOT, reason="root writes into read-only folders")
def test_a_read_only_folder_is_left_without_leftovers(env: Env, restore_modes: list) -> None:
    ro = env.root / "ro"
    ro.mkdir()
    (ro / "f.txt").write_text("keep", encoding="utf-8")
    ro.chmod(0o500)
    restore_modes.append(ro)
    for tool in ("create", "write", "delete"):
        path = str(ro / ("new.txt" if tool == "create" else "f.txt"))
        assert env.call(tool, **args_for(env, tool, path))["isError"] is True, tool
    assert sorted(os.listdir(ro)) == ["f.txt"]
    assert (ro / "f.txt").read_text(encoding="utf-8") == "keep"


def test_the_mode_of_a_file_is_kept_on_write_and_new_files_are_private(env: Env) -> None:
    path = env.root / "mode.txt"
    path.write_text("x", encoding="utf-8")
    path.chmod(0o640)
    assert env.call("write", path=str(path), text="y")["isError"] is False
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    fresh = env.root / "fresh.txt"
    assert env.call("create", path=str(fresh), text="y")["isError"] is False
    assert stat.S_IMODE(fresh.stat().st_mode) & 0o077 == 0


def test_backups_are_private_even_with_a_loose_umask(env: Env) -> None:
    old = os.umask(0)
    try:
        assert env.call("write", path=str(env.root / "seed.txt"), text="again")["isError"] is False
    finally:
        os.umask(old)
    assert stat.S_IMODE(env.backups.stat().st_mode) == 0o700
    for folder in env.backups.iterdir():
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700


# -- sizes -------------------------------------------------------------------


def test_read_limit_is_exactly_one_mebibyte(env: Env) -> None:
    limit = files_module.MAX_READ_BYTES
    exact = env.root / "exact.txt"
    exact.write_text("a" * limit, encoding="utf-8")
    out = json.loads(text_of(env.call("read", path=str(exact))))
    assert out["truncated"] is False and len(out["text"]) == limit
    over = env.root / "over.txt"
    over.write_text("a" * (limit + 1), encoding="utf-8")
    out = json.loads(text_of(env.call("read", path=str(over))))
    assert out["truncated"] is True and out["text"] == "a" * limit


def test_reading_a_sparse_file_of_64_mebibytes_returns_one_mebibyte(env: Env) -> None:
    big = env.root / "sparse.bin"
    with open(big, "wb") as f:
        f.truncate(64 * 1024 * 1024)
    out = json.loads(text_of(env.call("read", path=str(big))))
    assert out["truncated"] is True
    assert len(out["text"]) == files_module.MAX_READ_BYTES
    assert set(out["text"]) == {"\x00"}


def test_binary_and_invalid_utf8_files_are_a_clean_error(env: Env) -> None:
    for name, data in (
        ("jpeg.bin", b"\xff\xd8\xff\xe0\x00\x10JFIF"),
        ("latin1.txt", "café".encode("latin-1")),
        ("overlong.txt", b"\xc0\xaf"),
        ("lone-continuation.txt", b"\x80"),
        ("utf16.txt", "hello".encode("utf-16")),
    ):
        (env.root / name).write_bytes(data)
        assert env.call("read", path=str(env.root / name))["isError"] is True, name
        assert (env.root / name).read_bytes() == data


def test_a_file_that_is_valid_utf8_with_nul_bytes_is_returned_as_it_is(env: Env) -> None:
    (env.root / "nul.txt").write_bytes(b"a\x00b")
    out = json.loads(text_of(env.call("read", path=str(env.root / "nul.txt"))))
    assert out["text"] == "a\x00b"


def test_write_limit_counts_bytes_not_characters(env: Env) -> None:
    limit = files_module.MAX_WRITE_BYTES
    path = env.root / "limit.txt"
    path.write_text("old", encoding="utf-8")
    ok = "\u044f" * (limit // 2)  # two bytes each: exactly the limit
    assert env.call("write", path=str(path), text=ok)["isError"] is False
    assert path.stat().st_size == limit
    for too_big in (ok + "x", ok + "\u044f", "a" * (limit + 1), "\U0001f600" * (limit // 4 + 1)):
        before = state(env.root)
        assert env.call("write", path=str(path), text=too_big)["isError"] is True
        assert state(env.root) == before
    assert path.read_bytes() == ok.encode("utf-8")


def test_create_over_the_limit_makes_no_file_and_no_leftover(env: Env) -> None:
    before = sorted(os.listdir(env.root))
    big = "a" * (files_module.MAX_WRITE_BYTES + 1)
    assert env.call("create", path=str(env.root / "new.txt"), text=big)["isError"] is True
    assert sorted(os.listdir(env.root)) == before


def test_empty_text_is_a_valid_file(env: Env) -> None:
    path = env.root / "empty.txt"
    assert env.call("create", path=str(path), text="")["isError"] is False
    assert path.read_bytes() == b""
    assert json.loads(text_of(env.call("read", path=str(path))))["text"] == ""


@pytest.mark.parametrize("bad", [None, 5, 1.5, True, [], ["x"], {}, {"a": 1}])
def test_text_that_is_not_a_string_is_refused_and_the_file_untouched(env: Env, bad) -> None:
    before = world(env)
    assert env.call("write", path=str(env.root / "seed.txt"), text=bad)["isError"] is True
    assert env.call("create", path=f"{env.root}/other.txt", text=bad)["isError"] is True
    assert world(env) == before


def test_text_with_unusual_characters_is_stored_byte_for_byte(env: Env) -> None:
    text = "ї\u0000\r\n\t\u202e\u200b\U0001f600\u2028\u2029 \ufeff end"
    path = f"{env.root}/weird.txt"
    assert env.call("create", path=path, text=text)["isError"] is False
    assert (env.root / "weird.txt").read_bytes() == text.encode("utf-8")
    assert json.loads(text_of(env.call("read", path=path)))["text"] == text


def test_list_stops_at_a_thousand_entries_in_a_stable_order(env: Env) -> None:
    folder = env.root / "many"
    folder.mkdir()
    for i in range(1200):
        (folder / f"f{i:04d}").write_bytes(b"")
    out = json.loads(text_of(env.call("list", path=str(folder))))
    names = [e["name"] for e in out["entries"]]
    assert len(names) == files_module.MAX_LIST and out["truncated"] is True
    assert names == [f"f{i:04d}" for i in range(1000)]


def test_a_short_listing_is_not_marked_truncated(env: Env) -> None:
    out = json.loads(text_of(env.call("list", path=str(env.root))))
    assert out["truncated"] is False
    assert {e["name"] for e in out["entries"]} >= {"seed.txt", "sub"}


def test_search_stops_at_two_hundred_matches(env: Env) -> None:
    folder = env.root / "hits"
    folder.mkdir()
    for i in range(260):
        (folder / f"n{i:03d}.txt").write_text("needle", encoding="utf-8")
    out = json.loads(text_of(env.call("search", path=str(folder), query="needle")))
    assert len(out["matches"]) == files_module.MAX_SEARCH_RESULTS and out["truncated"] is True
    assert len({m["path"] for m in out["matches"]}) == files_module.MAX_SEARCH_RESULTS


def test_search_reads_the_text_of_files_up_to_two_mebibytes_only(env: Env) -> None:
    limit = files_module.MAX_SEARCH_FILE_BYTES
    (env.root / "at-limit.txt").write_text("needle" + "x" * (limit - 6), encoding="utf-8")
    (env.root / "over-limit.txt").write_text("needle" + "x" * (limit - 5), encoding="utf-8")
    (env.root / "needle-in-name-over-limit.bin").write_bytes(b"x" * (limit + 1))
    found = json.loads(text_of(env.call("search", path=str(env.root), query="needle")))
    hits = {Path(m["path"]).name: m["match"] for m in found["matches"]}
    assert hits == {"at-limit.txt": "text", "needle-in-name-over-limit.bin": "name"}


def test_search_does_not_enter_hidden_folders(env: Env) -> None:
    (env.root / ".git").mkdir()
    (env.root / ".git" / "config").write_text("needle", encoding="utf-8")
    (env.root / ".snapshots").mkdir()
    (env.root / ".snapshots" / "x").write_text("needle", encoding="utf-8")
    out = json.loads(text_of(env.call("search", path=str(env.root), query="needle")))
    assert out["matches"] == []


@pytest.mark.parametrize("query", ["", " ", "\t\n", None, 5, ["x"], {"q": 1}])
def test_search_needs_a_real_query(env: Env, query) -> None:
    assert env.call("search", path=str(env.root), query=query)["isError"] is True


def test_search_with_odd_queries_never_leaves_the_root(env: Env) -> None:
    for query in ("\x00", "../outside", "/etc/passwd", "*", ".*", "ß", "İ", "x" * 100_000):
        result = env.call("search", path=str(env.root), query=query)
        assert result["isError"] is False
        for m in json.loads(text_of(result))["matches"]:
            assert Path(m["path"]).is_relative_to(env.root)


# -- snapshot folders, snapshot numbers, backup ids -------------------------


def _whole_home(env: Env) -> Server:
    return Server(Files([env.home], env.backups, home=env.home))


def test_snapshot_folders_are_off_limits_to_every_tool(env: Env) -> None:
    snap = env.home / ".snapshots"
    server = _whole_home(env)
    inner = snap / "1" / "snapshot" / "Documents" / "sub" / "inner.txt"
    for path in (snap, snap / "1", inner):
        for tool in ("list", "read", "search", "write", "delete", "create"):
            result = server.call(tool, args_for(env, tool, str(path)))
            assert result["isError"] is True, (tool, path)
    assert inner.read_text(encoding="utf-8") == "inner"


def test_link_to_a_snapshot_is_off_limits(env: Env) -> None:
    inner = env.home / ".snapshots" / "1" / "snapshot" / "Documents" / "sub" / "inner.txt"
    link(env, "to-snapshot-file", inner)
    link(env, "to-snapshot-folder", env.home / ".snapshots")
    server = _whole_home(env)
    for name in ("to-snapshot-file", "to-snapshot-folder", "to-snapshot-folder/1"):
        for tool in ("read", "write", "delete", "list"):
            result = server.call(tool, args_for(env, tool, f"{env.root}/{name}"))
            assert result["isError"] is True, (tool, name)
    assert inner.read_text(encoding="utf-8") == "inner"


def test_a_folder_named_snapshots_inside_a_root_is_refused_too(env: Env) -> None:
    (env.root / ".snapshots").mkdir()
    (env.root / ".snapshots" / "f.txt").write_text("x", encoding="utf-8")
    for tool in ("read", "write", "delete"):
        path = f"{env.root}/.snapshots/f.txt"
        assert env.call(tool, **args_for(env, tool, path))["isError"] is True
    assert env.call("create", path=f"{env.root}/.snapshots/n.txt", text="x")["isError"] is True


SNAPSHOT_NUMBERS = [0, -1, -(2**63), 2, 99, 2**63, 2**200, True, False]


@pytest.mark.parametrize("number", SNAPSHOT_NUMBERS, ids=[repr(n)[:14] for n in SNAPSHOT_NUMBERS])
def test_bad_snapshot_numbers_are_refused(env: Env, number: int) -> None:
    path = str(env.root / "seed.txt")
    before = world(env)
    assert env.call("restore_snapshot", pre=number, paths=[path])["isError"] is True
    assert env.call("restore_snapshot", pre=1, post=number, paths=[path])["isError"] is True
    assert world(env) == before


@pytest.mark.parametrize("number", ["1", "../1", "1/../1", 1.0, 1.5, None, [1], {"n": 1}, "1\x00"])
def test_snapshot_numbers_of_the_wrong_type_are_refused(env: Env, number) -> None:
    path = str(env.root / "seed.txt")
    assert env.call("restore_snapshot", pre=number, paths=[path])["isError"] is True
    assert env.call("restore_snapshot", pre=1, post=number, paths=[path])["isError"] is True


@pytest.mark.parametrize(
    "paths",
    [None, "x", [], [[]], [5], [None], {"a": "b"}, [""], ["relative.txt"], ["\x00"]],
    ids=["none", "string", "empty", "nested", "int", "null", "dict", "empty-name", "rel", "nul"],
)
def test_bad_snapshot_path_lists_are_refused(env: Env, paths) -> None:
    before = world(env)
    assert env.call("restore_snapshot", pre=1, paths=paths)["isError"] is True
    assert world(env) == before


def test_snapshot_restore_takes_at_most_a_thousand_paths(env: Env) -> None:
    path = str(env.root / "seed.txt")
    before = world(env)
    assert env.call("restore_snapshot", pre=1, paths=[path] * 1001)["isError"] is True
    assert world(env) == before


def test_one_bad_path_stops_the_whole_snapshot_restore(env: Env) -> None:
    snap_doc = env.home / ".snapshots" / "1" / "snapshot" / "Documents" / "good.txt"
    snap_doc.write_text("from the snapshot", encoding="utf-8")
    good = env.root / "good.txt"
    good.write_text("changed later", encoding="utf-8")
    for bad in (str(env.secret), f"{env.root}/../outside/x", str(env.home / "Documents-evil")):
        before = world(env)
        result = env.call("restore_snapshot", pre=1, paths=[str(good), bad])
        assert result["isError"] is True
        assert world(env) == before
    assert good.read_text(encoding="utf-8") == "changed later"


def test_snapshot_restore_refuses_a_root_outside_the_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".snapshots" / "1" / "snapshot").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "f.txt").write_text("keep", encoding="utf-8")
    server = Server(Files([elsewhere], tmp_path / "bk", home=home))
    result = server.call("restore_snapshot", {"pre": 1, "paths": [str(elsewhere / "f.txt")]})
    assert result["isError"] is True
    assert (elsewhere / "f.txt").read_text(encoding="utf-8") == "keep"


def test_snapshot_restore_does_not_follow_a_live_link_out(env: Env) -> None:
    snap_root = env.home / ".snapshots" / "1" / "snapshot" / "Documents"
    (snap_root / "doc.txt").write_text("from the snapshot", encoding="utf-8")
    link(env, "doc.txt", env.secret)
    before = world(env)
    result = env.call("restore_snapshot", pre=1, paths=[f"{env.root}/doc.txt"])
    assert result["isError"] is True
    assert world(env) == before
    assert env.secret.read_text(encoding="utf-8") == SECRET


def test_snapshot_restore_never_restores_a_link_from_the_snapshot(env: Env) -> None:
    snap_root = env.home / ".snapshots" / "1" / "snapshot" / "Documents"
    os.symlink(env.secret, snap_root / "was-a-link")
    result = env.call("restore_snapshot", pre=1, paths=[f"{env.root}/was-a-link"])
    assert result["isError"] is False
    assert json.loads(text_of(result))["restored"] == []
    assert not (env.root / "was-a-link").exists()
    assert not (env.root / "was-a-link").is_symlink()


BACKUP_IDS = [
    "",
    ".",
    "..",
    "../..",
    "../../etc",
    "a/b",
    "a b",
    "a\x00",
    "a\n",
    "meta",
    "content",
    "%2e%2e",
    "\u0430\u0431\u0432",  # Cyrillic letters count as alphanumeric
    "\u0661\u0662\u0663",  # Arabic-Indic digits too
    "\uff11\uff12",  # fullwidth digits
    "x" * 5000,
    "0" * 20,
]


@pytest.mark.parametrize("backup_id", BACKUP_IDS, ids=[repr(b)[:16] for b in BACKUP_IDS])
def test_odd_backup_ids_are_refused(env: Env, backup_id: str) -> None:
    before = world(env)
    path = str(env.root / "seed.txt")
    assert env.call("restore", path=path, backup_id=backup_id)["isError"] is True
    assert world(env) == before
    assert (env.root / "seed.txt").read_text(encoding="utf-8") == "three"


@pytest.mark.parametrize("backup_id", [None, 5, 1.5, True, [], ["x"], {}])
def test_backup_ids_of_the_wrong_type_are_refused(env: Env, backup_id) -> None:
    path = str(env.root / "seed.txt")
    assert env.call("restore", path=path, backup_id=backup_id)["isError"] is True


def test_restore_puts_back_exactly_what_was_there(env: Env) -> None:
    path = env.root / "doc.txt"
    original = "перший\n\u0000\ufeff"
    env.files.create(str(path), original)
    changed = env.files.write(str(path), "second")
    assert env.call("restore", path=str(path), backup_id=changed["backup_id"])["isError"] is False
    assert path.read_bytes() == original.encode("utf-8")


# -- protocol level ----------------------------------------------------------


@pytest.mark.parametrize("tool", PATH_TOOLS)
def test_unknown_arguments_are_refused(env: Env, tool: str) -> None:
    good = args_for(env, tool, str(env.root / "seed.txt"))
    before = world(env)
    for extra in ({"root": "/"}, {"follow": True}, {"backup_dir": str(env.outside)}, {"": 1}):
        assert env.call(tool, **good, **extra)["isError"] is True
    assert world(env) == before


@pytest.mark.parametrize("tool", PATH_TOOLS)
def test_missing_required_arguments_are_refused(env: Env, tool: str) -> None:
    good = args_for(env, tool, str(env.root / "seed.txt"))
    for key in good:
        partial = {k: v for k, v in good.items() if k != key}
        assert env.server.call(tool, partial)["isError"] is True


@pytest.mark.parametrize("arguments", [None, [], "x", 5, [["path", "/x"]], True])
def test_arguments_that_are_not_an_object_are_refused(env: Env, arguments) -> None:
    for tool in PATH_TOOLS:
        assert env.server.call(tool, arguments)["isError"] is True


@pytest.mark.parametrize("name", ["", "READ", "Read", "read ", "../read", "list\x00", 5])
def test_unknown_tool_names_are_refused(env: Env, name) -> None:
    before = world(env)
    assert env.server.call(name, {"path": str(env.root)})["isError"] is True
    assert world(env) == before


def _request(id_: int, tool: str, arguments: dict) -> str:
    message = {
        "jsonrpc": "2.0",
        "id": id_,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    return json.dumps(message)


def test_the_server_survives_bad_lines_and_keeps_answering(env: Env) -> None:
    lines = [
        "not json",
        "[]",
        "5",
        "null",
        '{"jsonrpc":"1.0","id":1}',
        '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"read"}}',
        '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"read","arguments":5}}',
        '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"arguments":{}}}',
        '{"jsonrpc":"2.0","id":5,"method":"tools/call"}',
        '{"jsonrpc":"2.0","id":6,"method":"nope"}',
        _request(7, "read", {"path": f"{env.root}/a\x00b"}),
        _request(9, "read", {"path": str(env.secret)}),
        "",
        "   ",
        _request(10, "read", {"path": str(env.root / "seed.txt")}),
    ]
    out = io.StringIO()
    env.server.serve(io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert SECRET not in out.getvalue()
    by_id = {r["id"]: r for r in replies}
    assert by_id[7]["result"]["isError"] is True
    assert by_id[9]["result"]["isError"] is True
    assert by_id[10]["result"]["isError"] is False
    assert json.loads(by_id[10]["result"]["content"][0]["text"])["text"] == "three"


def test_a_path_written_with_json_escapes_is_checked_after_decoding(env: Env) -> None:
    escaped = str(env.root / "..").replace("/", "\\u002f") + "\\u002foutside\\u002fsecret.txt"
    line = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
        '{"name":"read","arguments":{"path":"' + escaped + '"}}}'
    )
    out = io.StringIO()
    env.server.serve(io.StringIO(line + "\n"), out)
    reply = json.loads(out.getvalue())
    assert reply["result"]["isError"] is True
    assert SECRET not in out.getvalue()


def test_the_reply_to_a_listing_is_one_json_line_whatever_the_names(env: Env) -> None:
    odd = {"a\nb", 'q"uote', "back\\slash", "\u2028", "\U0001f600", "\u0007bell"}
    for name in odd:
        (env.root / name).write_bytes(b"")
    out = io.StringIO()
    env.server.serve(io.StringIO(_request(1, "list", {"path": str(env.root)}) + "\n"), out)
    assert out.getvalue().count("\n") == 1  # one message per line, whatever the names
    names = {
        e["name"] for e in json.loads(out.getvalue())["result"]["structuredContent"]["entries"]
    }
    assert odd <= names


def test_an_error_names_the_path_that_was_given_and_not_the_content(env: Env) -> None:
    result = env.call("read", path=str(env.secret))
    assert SECRET not in text_of(result)
    assert str(env.secret) in text_of(result)


def test_prune_leaves_alone_what_is_not_a_backup(env: Env) -> None:
    stray = env.backups / "not-ours"
    stray.mkdir()
    (stray / "meta.json").write_text("{not json", encoding="utf-8")
    (env.backups / "loose-file").write_text("x", encoding="utf-8")
    os.symlink(env.outside, env.backups / "link-out")
    far_future = 10**12
    Files([env.root], env.backups, keep_days=1, clock=lambda: far_future, home=env.home)
    assert stray.exists() and (env.backups / "loose-file").exists()
    assert env.secret.read_text(encoding="utf-8") == SECRET


def test_a_backup_id_cannot_point_at_a_forged_backup_folder_elsewhere(env: Env) -> None:
    forged = env.tmp / "forged"
    forged.mkdir()
    (forged / "content").write_text("FORGED CONTENT", encoding="utf-8")
    meta = {"path": str(env.root / "seed.txt"), "existed": True, "time": 1e12}
    (forged / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    inside_backups = env.backups / "real"
    inside_backups.mkdir()
    for backup_id in ("../forged", "real/../../forged", str(forged), "./../forged", "..%2fforged"):
        before = world(env)
        result = env.call("restore", path=str(env.root / "seed.txt"), backup_id=backup_id)
        assert result["isError"] is True, backup_id
        assert world(env) == before
    assert (env.root / "seed.txt").read_text(encoding="utf-8") == "three"


def test_an_existing_backup_folder_with_loose_rights_is_tightened(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "f.txt").write_text("x", encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir()
    backups.chmod(0o755)
    Files([root], backups, home=tmp_path).write(str(root / "f.txt"), "y")
    assert stat.S_IMODE(backups.stat().st_mode) == 0o700


def test_booleans_are_not_numbers_at_either_layer(env: Env) -> None:
    path = str(env.root / "seed.txt")
    for value in (True, False):
        with pytest.raises(FilesError):
            check_arguments("restore_snapshot", {"pre": value, "paths": [path]})
        with pytest.raises(FilesError):
            check_arguments("restore_snapshot", {"pre": 1, "post": value, "paths": [path]})
        with pytest.raises(FilesError):
            env.files.restore_snapshot(value, [path])  # type: ignore[arg-type]
        with pytest.raises(FilesError):
            env.files.restore_snapshot(1, [path], post=value)  # type: ignore[arg-type]
    assert env.files.restore_snapshot(1, [path])["snapshot"] == 1


def test_files_itself_refuses_a_text_that_is_not_a_string(env: Env) -> None:
    for bad in (None, 5, b"bytes", ["x"]):
        with pytest.raises(FilesError):
            env.files.write(str(env.root / "seed.txt"), bad)  # type: ignore[arg-type]
        with pytest.raises(FilesError):
            env.files.create(str(env.root / "n.txt"), bad)  # type: ignore[arg-type]
    assert not (env.root / "n.txt").exists()
