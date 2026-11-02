# SPDX-License-Identifier: Apache-2.0
import io
import json
import subprocess

import pytest

from vilna_mcp_backup.server import Server, ToolFailure, check_arguments, to_command


class FakeProgram:
    def __init__(self, answer=None, code=0):
        self.calls = []
        self.answer = answer if answer is not None else {"ok": True}
        self.code = code

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.code, json.dumps(self.answer) + "\n", "")


def call(server, name, args=None):
    return server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args or {}},
        }
    )["result"]


def test_tools_list_offers_only_r0_to_r2():
    server = Server(["vilna-backup"], FakeProgram())
    tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {
        "status",
        "list_snapshots",
        "overview",
        "run_now",
        "set_schedule",
        "set_paths",
        "restore",
    }
    for t in tools:
        assert t["annotations"].get("destructiveHint") is not True
        assert t["annotations"].get("openWorldHint") is not True


@pytest.mark.parametrize(
    ("name", "args", "cmd"),
    [
        ("status", {}, ["status"]),
        ("run_now", {}, ["start"]),
        ("set_schedule", {"schedule": "weekly"}, ["schedule", "weekly"]),
        (
            "set_paths",
            {
                "preset": "settings",
                "add": ["/home/a/Music"],
                "gloxi_history": False,
                "limit_gb": 50,
            },
            [
                "set",
                "--preset",
                "settings",
                "--add",
                "/home/a/Music",
                "--history",
                "no",
                "--limit-gb",
                "50",
            ],
        ),
        (
            "restore",
            {"target": "/home/a/Documents/R", "path": "Documents/x.odt"},
            ["restore", "--target", "/home/a/Documents/R", "--path", "Documents/x.odt"],
        ),
    ],
)
def test_commands(name, args, cmd):
    prog = FakeProgram()
    out = call(Server(["vb"], prog), name, args)
    assert out["isError"] is False
    assert prog.calls == [["vb", "--json", *cmd]]


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("set_schedule", {"schedule": "every minute"}),
        ("set_paths", {"add": ["Music"]}),
        ("set_paths", {"limit_gb": True}),
        ("set_paths", {"limit_gb": -1}),
        ("set_paths", {"system_secrets": True}),  # not offered to Gloxi at all
        ("restore", {}),
        ("restore", {"target": "--in-place"}),
        ("restore_in_place", {}),
        ("configure_destination", {"place": "/media/usb"}),
    ],
)
def test_refused(name, args):
    prog = FakeProgram()
    out = call(Server(["vb"], prog), name, args)
    assert out["isError"] is True
    assert prog.calls == []


def test_errors_from_the_program_are_passed_on():
    out = call(Server(["vb"], FakeProgram({"error": "no place for backups"}, code=1)), "run_now")
    assert out["isError"] and "no place" in out["content"][0]["text"]


def test_missing_program():
    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    out = call(Server(["vb"], missing), "status")
    assert out["isError"] and "not installed" in out["content"][0]["text"]


def test_check_arguments_types():
    with pytest.raises(ToolFailure):
        check_arguments("set_paths", {"gloxi_history": "yes"})
    assert to_command("list_snapshots", {}) == ["snapshots"]


def test_serve_over_stdio():
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]
    out = io.StringIO()
    Server(["vb"], FakeProgram()).serve(io.StringIO("\n".join(json.dumps(x) for x in lines)), out)
    answers = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [a["id"] for a in answers] == [1, 2]
    assert answers[0]["result"]["serverInfo"]["name"] == "vilna-mcp-backup"
