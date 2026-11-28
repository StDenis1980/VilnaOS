# SPDX-License-Identifier: Apache-2.0
"""Names, files and answers that once stopped the server or let a change land elsewhere."""

from __future__ import annotations

import io
import json
import os
import threading

import pytest

from vilna_mcp_files.files import MAX_READ_BYTES, Files, FilesError
from vilna_mcp_files.server import Server


@pytest.fixture
def files(tmp_path):
    docs = tmp_path / "Documents"
    docs.mkdir()
    return Files([docs], tmp_path / "backups")


def root(files):
    return files.roots[0]


def backups(files):
    return list(files.backup_dir.iterdir()) if files.backup_dir.is_dir() else []


def test_a_lone_surrogate_in_a_path_or_text_is_refused_and_leaves_no_copy(files):
    with pytest.raises(FilesError):
        files.read(str(root(files) / "\ud800"))
    (root(files) / "a.txt").write_text("a", encoding="utf-8")
    with pytest.raises(FilesError):
        files.write(str(root(files) / "a.txt"), "\ud800")
    with pytest.raises(FilesError):
        files.create(str(root(files) / "b.txt"), "\ud800")
    assert backups(files) == []


def test_a_name_that_is_not_utf8_comes_back_as_one_answer(files):
    os.close(os.open(os.fsencode(root(files)) + b"/caf\xe9.txt", os.O_CREAT | os.O_WRONLY))
    server = Server(files)
    out = io.StringIO()
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "list", "arguments": {"path": str(root(files))}},
    }
    server.serve(io.StringIO(json.dumps(msg) + "\n"), out)
    out.getvalue().encode("utf-8")  # writable as strict UTF-8
    (answer,) = [json.loads(x) for x in out.getvalue().splitlines()]
    names = [e["name"] for e in answer["result"]["structuredContent"]["entries"]]
    # The name read back can be used again.
    assert files.resolve(str(root(files) / names[0])).name == names[0]


def test_search_skips_a_fifo_instead_of_waiting_for_ever(files):
    os.mkfifo(root(files) / "pipe")
    (root(files) / "note.txt").write_text("needle", encoding="utf-8")
    result: dict = {}
    t = threading.Thread(target=lambda: result.update(files.search(str(root(files)), "needle")))
    t.daemon = True
    t.start()
    t.join(10)
    assert not t.is_alive()
    assert [m["path"] for m in result["matches"]] == [str(root(files) / "note.txt")]


def test_read_refuses_a_fifo(files):
    os.mkfifo(root(files) / "pipe")
    with pytest.raises(FilesError):
        files.read(str(root(files) / "pipe"))


def test_read_takes_only_the_start_of_a_huge_file_and_keeps_whole_letters(files):
    big = root(files) / "big.txt"
    with open(big, "wb") as f:
        f.write(b"a" + "ї".encode() * MAX_READ_BYTES)  # the cut falls inside a letter
        f.truncate(4 << 30)  # sparse: 4 GiB on paper
    out = files.read(str(big))
    assert out["truncated"] is True
    assert out["text"].startswith("aї") and len(out["text"].encode()) <= MAX_READ_BYTES


def test_a_damaged_undo_copy_is_an_error_not_a_crash(files):
    f = root(files) / "a.txt"
    f.write_text("a", encoding="utf-8")
    backup = files.write(str(f), "b")["backup_id"]
    (files.backup_dir / backup / "meta.json").write_text("[]", encoding="utf-8")
    with pytest.raises(FilesError):
        files.restore(str(f), backup)


def test_the_undo_copies_are_not_reachable_through_the_tool(tmp_path):
    home = tmp_path / "home"
    (home / ".local/state/herkos/backups").mkdir(parents=True)
    files = Files([home], home / ".local/state/herkos/backups")
    (home / "a.txt").write_text("a", encoding="utf-8")
    backup = files.write(str(home / "a.txt"), "b")["backup_id"]
    with pytest.raises(FilesError):
        files.write(str(files.backup_dir / backup / "meta.json"), "{}")


def test_a_long_name_can_be_written(files):
    name = "я" * 120 + ".t"  # 242 bytes: the old temporary name did not fit
    files.create(str(root(files) / name), "x")
    files.write(str(root(files) / name), "y")
    assert (root(files) / name).read_text(encoding="utf-8") == "y"


def test_search_says_when_a_folder_could_not_be_read(files):
    if os.getuid() == 0:
        pytest.skip("root reads every folder")
    locked = root(files) / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        assert files.search(str(root(files)), "x").get("unreadable_folders") == 1
    finally:
        locked.chmod(0o700)


def test_read_only_files_are_not_overwritten_or_deleted(files):
    if os.getuid() == 0:
        pytest.skip("root may write anything")
    f = root(files) / "ro.txt"
    f.write_text("keep", encoding="utf-8")
    f.chmod(0o444)
    with pytest.raises(FilesError):
        files.write(str(f), "x")
    with pytest.raises(FilesError):
        files.delete(str(f))
    assert f.read_text(encoding="utf-8") == "keep"


def test_delete_never_goes_through_a_link(files):
    target = root(files) / "target.txt"
    target.write_text("keep", encoding="utf-8")
    (root(files) / "link").symlink_to(target)
    with pytest.raises(FilesError):
        files.delete(str(root(files) / "link"))
    assert target.read_text(encoding="utf-8") == "keep"


def test_a_snapshot_link_never_brings_outside_files_into_the_home(tmp_path):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_text("other people's", encoding="utf-8")
    snap = home / ".snapshots" / "1" / "snapshot"
    snap.mkdir(parents=True)
    (snap / "Documents").symlink_to(outside)
    files = Files([home / "Documents"], tmp_path / "backups", home=home)
    out = files.restore_snapshot(1, [str(home / "Documents" / "passwd")])
    assert out["restored"] == []
    assert not (home / "Documents" / "passwd").exists()


def test_a_snapshot_number_below_the_schemas_minimum_is_refused():
    from vilna_mcp_files.server import check_arguments

    with pytest.raises(FilesError):
        check_arguments("restore_snapshot", {"pre": -5, "paths": ["/a"]})
