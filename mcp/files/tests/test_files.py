# SPDX-License-Identifier: Apache-2.0
import io
import json

import pytest

from vilna_mcp_files.files import Files, FilesError
from vilna_mcp_files.server import Server


@pytest.fixture
def files(tmp_path):
    docs = tmp_path / "Documents"
    docs.mkdir()
    return Files([docs], tmp_path / "backups")


def docs(files):
    return files.roots[0]


def test_create_read_write_restore(files):
    p = str(docs(files) / "zvit.txt")
    created = files.create(p, "дата: 1 листопада")
    assert files.read(p)["text"] == "дата: 1 листопада"
    changed = files.write(p, "дата: 2 листопада")
    assert files.read(p)["text"] == "дата: 2 листопада"
    files.restore(p, changed["backup_id"])
    assert files.read(p)["text"] == "дата: 1 листопада"
    files.restore(p, created["backup_id"])  # undo the creation: the file goes away
    assert not (docs(files) / "zvit.txt").exists()


def test_delete_and_restore(files):
    p = docs(files) / "a.txt"
    p.write_text("keep me")
    out = files.delete(str(p))
    assert not p.exists()
    files.restore(str(p), out["backup_id"])
    assert p.read_text() == "keep me"


def test_outside_roots_is_refused(files, tmp_path):
    (tmp_path / "secret").write_text("x")
    with pytest.raises(FilesError):
        files.read(str(tmp_path / "secret"))
    with pytest.raises(FilesError):
        files.read(f"{docs(files)}/../secret")
    (docs(files) / "link").symlink_to(tmp_path / "secret")
    with pytest.raises(FilesError):
        files.read(str(docs(files) / "link"))
    with pytest.raises(FilesError):
        files.read("relative.txt")


def test_create_does_not_overwrite(files):
    p = docs(files) / "a.txt"
    p.write_text("x")
    with pytest.raises(FilesError):
        files.create(str(p), "y")


def test_list_and_search(files):
    d = docs(files)
    (d / "sub").mkdir()
    (d / "sub" / "lyst.txt").write_text("зустріч у вівторок")
    (d / "Zvit.txt").write_text("nothing")
    names = [e["name"] for e in files.list(str(d))["entries"]]
    assert names == ["Zvit.txt", "sub"]
    hits = files.search(str(d), "ВІВТОРОК")["matches"]
    assert hits == [{"path": str(d / "sub" / "lyst.txt"), "match": "text"}]
    assert files.search(str(d), "zvit")["matches"][0]["match"] == "name"


def test_bad_backup_id(files):
    with pytest.raises(FilesError):
        files.restore(str(docs(files) / "a"), "../../etc")
    with pytest.raises(FilesError):
        files.restore(str(docs(files) / "a"), "123")


def test_restore_is_bound_to_its_file_and_undoable(files):
    a, b = docs(files) / "a.txt", docs(files) / "b.txt"
    a.write_text("a1")
    b.write_text("b1")
    out = files.write(str(a), "a2")
    with pytest.raises(FilesError):
        files.restore(str(b), out["backup_id"])
    undo = files.restore(str(a), out["backup_id"])
    assert a.read_text() == "a1"
    files.restore(str(a), undo["backup_id"])
    assert a.read_text() == "a2"


def rpc(server, *msgs):
    out = io.StringIO()
    server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_mcp_session(files):
    s = Server(files)
    p = str(docs(files) / "n.txt")
    r = rpc(
        s,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "create", "arguments": {"path": p, "text": "hi"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "read", "arguments": {"path": p, "extra": "x"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "chmod", "arguments": {}},
        },
    )
    assert [x["id"] for x in r] == [1, 2, 3, 4, 5]
    assert r[0]["result"]["protocolVersion"] == "2025-06-18"
    tools = {t["name"]: t for t in r[1]["result"]["tools"]}
    assert tools["read"]["annotations"]["readOnlyHint"] is True
    assert tools["delete"]["annotations"]["destructiveHint"] is True
    assert r[2]["result"]["isError"] is False
    assert r[2]["result"]["structuredContent"]["backup_id"]
    assert r[3]["result"]["isError"] is True
    assert r[4]["result"]["isError"] is True


def test_backups_expire_after_30_days(tmp_path):
    now = [1_000_000.0]
    d = tmp_path / "Documents"
    d.mkdir()
    files = Files([d], tmp_path / "backups", clock=lambda: now[0])
    a = d / "a.txt"
    a.write_text("v1")
    old = files.write(str(a), "v2")["backup_id"]
    now[0] += 29 * 86400
    files.restore(str(a), old)  # still there after 29 days
    assert a.read_text() == "v1"
    now[0] += 2 * 86400
    assert files.prune() >= 1
    with pytest.raises(FilesError, match="30 days"):
        files.restore(str(a), old)
