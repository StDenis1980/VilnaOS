# SPDX-License-Identifier: Apache-2.0
import io
import json
import subprocess
import sys

from vilna_mcp_mail.mail import Mailbox
from vilna_mcp_mail.server import DEMO_ACCOUNT, TOOLS, Server, demo_store


def rpc(server, method, params=None, id_=1):
    return server.handle({"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})


def call(server, name, **args):
    return rpc(server, "tools/call", {"name": name, "arguments": args})["result"]


def test_lists_every_operation():
    s = Server(Mailbox(demo_store()))
    names = [t["name"] for t in rpc(s, "tools/list")["result"]["tools"]]
    assert names == [
        "list_accounts",
        "list_folders",
        "list_messages",
        "search_messages",
        "read_message",
        "save_draft",
        "send",
        "restore",
    ]
    send = next(t for t in TOOLS if t["name"] == "send")
    assert send["inputSchema"]["required"] == ["account", "to", "subject", "text"]
    assert send["annotations"]["openWorldHint"] is True
    assert rpc(s, "initialize", {"protocolVersion": "2025-03-26"})["result"]["protocolVersion"] == (
        "2025-03-26"
    )


def test_from_is_the_sender_filter():
    s = Server(Mailbox(demo_store()))
    out = call(s, "search_messages", **{"from": "marta"})
    assert out["isError"] is False
    assert [m["subject"] for m in out["structuredContent"]["messages"]] == ["Lunch on Friday?"]


def test_bad_arguments_are_tool_errors():
    s = Server(Mailbox(demo_store()))
    for name, args in [
        ("read_message", {}),
        ("read_message", {"uid": "1"}),
        ("read_message", {"uid": True}),
        ("list_messages", {"limit": 1000}),
        ("send", {"account": DEMO_ACCOUNT, "to": "a@example.com", "subject": "", "text": ""}),
        (
            "send",
            {
                "account": DEMO_ACCOUNT,
                "to": ["a@example.com"],
                "subject": "",
                "text": "",
                "attachments": ["/etc/passwd"],
            },
        ),
        ("nope", {}),
    ]:
        out = call(s, name, **args)
        assert out["isError"] is True, (name, args)
    assert s.mailbox.store.outbox == []


def test_unexpected_failures_say_nothing_private():
    class Boom:
        def accounts(self):
            raise RuntimeError("password=hunter2")

    out = call(Server(Mailbox(Boom())), "list_accounts")
    assert out["isError"] is True and "hunter2" not in json.dumps(out)


def test_stdio_round_trip():
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_messages", "arguments": {"limit": 2}},
        },
    ]
    out = subprocess.run(
        [sys.executable, "-m", "vilna_mcp_mail", "--demo"],
        input="\n".join(json.dumps(m) for m in lines) + "\nnot json\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.splitlines()
    answers = [json.loads(o) for o in out]
    assert [a.get("id") for a in answers] == [1, 2, None]
    assert len(answers[1]["result"]["structuredContent"]["messages"]) == 2
    assert answers[2]["error"]["code"] == -32700


def test_serve_skips_blank_lines():
    s = Server(Mailbox(demo_store()))
    out = io.StringIO()
    s.serve(io.StringIO('\n{"jsonrpc":"2.0","id":3,"method":"ping"}\n'), out)
    assert json.loads(out.getvalue())["result"] == {}
