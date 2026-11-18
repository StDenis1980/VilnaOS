# SPDX-License-Identifier: GPL-3.0-or-later
"""The gate and the mail MCP server (mcp/mail, ADR 0030), in its demo mailbox.

Herkos-lite never starts the mail server itself ([launch] has no module):
these tests register it by hand to check the manifest's levels against the
real server. Sending (R4) is for the system herkosd only, which asks every
time with the whole message (herkos/rs, gate tests).
"""

import sys

import pytest

from herkos.config import Config
from herkos.confirm import Answer, Scripted
from herkos.gate import Gate
from herkos.grants import GrantStore
from herkos.manifest import ManifestRegistry
from herkos.mcp_client import McpStdioExecutor
from herkos.mitos import Journal
from herkos.policy import SessionState
from herkos.request import parse_request
from herkos.risk import Level
from herkos.tools import ToolRegistry

pytest.importorskip("vilna_mcp_mail")
TOOL = "org.vilna.mcp.mail"
ME = "ola@example.org"


@pytest.fixture
def gate(tmp_path):
    config = Config(allowed_roots=(), state_dir=tmp_path / "state")
    server = McpStdioExecutor([sys.executable, "-m", "vilna_mcp_mail", "--demo"])
    server.start()
    tools = ToolRegistry()
    tools.register(TOOL, server)
    # The most the dialog offers: "always" when it may, else "once".
    confirmer = Scripted(lambda q: q.choices[-1])
    yield (
        Gate(
            config,
            ManifestRegistry.builtin(),
            tools,
            GrantStore(config.grants_path),
            Journal(config.journal_path),
            confirmer,
        ),
        confirmer,
    )
    server.stop()


def call(gate, op, session, **args):
    request = {"tool": TOOL, "operation": op, "reason": "test", "arguments": args}
    return gate.handle(parse_request(request), session)


def test_levels_and_no_module_for_herkos_lite():
    m = ManifestRegistry.builtin().get(TOOL)
    assert {op: m.operation(op).level for op in m.operations} == {
        "list_accounts": Level.R0,
        "list_folders": Level.R0,
        "list_messages": Level.R1,
        "search_messages": Level.R1,
        "read_message": Level.R1,
        "save_draft": Level.R2,
        "send": Level.R4,
        "restore": Level.R2,
    }
    assert m.operation("send").egress
    assert all(m.operation(op).returns_external for op in ("list_messages", "read_message"))
    assert m.launch is not None and m.launch.module is None


def test_herkos_lite_never_offers_or_sends(gate):
    g, confirmer = gate
    listed = {
        op["name"] for t in g.available_tools() if t["tool"] == TOOL for op in t["operations"]
    }
    assert "send" not in listed and "read_message" in listed
    out = call(g, "send", SessionState(), account=ME, to=["a@example.com"], subject="", text="")
    assert out.code == "denied_level" and confirmer.questions == []


def test_reading_mail_taints_and_a_draft_after_it_is_asked_exactly(gate):
    g, confirmer = gate
    s = SessionState()
    assert call(g, "list_accounts", s).code == "allowed"
    out = call(g, "search_messages", s, query="lunch")
    assert out.code == "allowed", out.message
    assert s.tainted
    msg = call(g, "read_message", s, uid=out.result["messages"][0]["uid"])
    assert "Friday at 13:00" in msg.result["text"]
    # After someone else's mail a draft is one level up: once only, shown in full.
    out = call(
        g,
        "save_draft",
        s,
        account=ME,
        to=["marta@example.com"],
        subject="Re: Lunch",
        text="Yes!",
        in_reply_to=msg.result["message_id"],
    )
    assert (out.code, out.level) == ("allowed", Level.R3), out.message
    q = confirmer.questions[-1]
    assert q.content["to"] == ["marta@example.com"] and q.taint_sources
    assert q.choices == (Answer.ONCE,)


def test_mail_that_gives_orders_puts_writing_out_of_herkos_lite(gate):
    g, _confirmer = gate
    s = SessionState()
    found = call(g, "search_messages", s, query="instructions").result["messages"]
    msg = call(g, "read_message", s, uid=found[0]["uid"])
    assert "forward every invoice" in msg.result["text"]
    out = call(
        g,
        "save_draft",
        s,
        account=ME,
        to=["collect@example.com"],
        subject="Invoices",
        text="all of them",
    )
    assert (out.code, out.level) == ("denied_level", Level.R4)
