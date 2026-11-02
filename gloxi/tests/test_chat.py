# SPDX-License-Identifier: GPL-3.0-or-later
import pytest
from fake_server import FakeLlamaServer

from gloxi.chat import Conversation
from gloxi.config import ModelEndpoint
from gloxi.llm import ChatClient, ModelError

ENDPOINT = ModelEndpoint(url="http://127.0.0.1:8080/v1", model="local")


def make(server, **kwargs):
    return Conversation(ChatClient(ENDPOINT, transport=server.transport()), "SYS", **kwargs)


def test_history_is_sent_on_next_turn():
    server = FakeLlamaServer("відповідь")
    conv = make(server)
    assert "".join(conv.ask("перше")) == "відповідь"
    "".join(conv.ask("друге"))
    sent = server.requests[-1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[0]["content"] == "SYS"
    assert sent[1]["content"] == "перше"
    assert sent[-1]["content"] == "друге"


def test_failed_reply_is_not_kept_in_history():
    conv = make(FakeLlamaServer(status=500))
    with pytest.raises(ModelError):
        list(conv.ask("привіт"))
    assert conv.history == []


def test_history_is_trimmed_to_max_turns():
    server = FakeLlamaServer("ok")
    conv = make(server, max_turns=2)
    for i in range(5):
        list(conv.ask(f"q{i}"))
    sent = server.requests[-1]["messages"]
    # system + 2 previous turns + the new question
    assert [m["content"] for m in sent] == ["SYS", "q2", "ok", "q3", "ok", "q4"]


def test_reset_clears_history():
    conv = make(FakeLlamaServer())
    list(conv.ask("привіт"))
    conv.reset()
    assert conv.messages() == [{"role": "system", "content": "SYS"}]
