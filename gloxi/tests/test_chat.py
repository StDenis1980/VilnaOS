# SPDX-License-Identifier: GPL-3.0-or-later
import pytest
from fake_server import FakeLlamaServer
from helpers import ask, make_conversation

from gloxi.llm import ModelError


def test_history_is_sent_on_next_turn():
    server = FakeLlamaServer("відповідь")
    conv = make_conversation(server)
    assert ask(conv, "перше") == "відповідь"
    ask(conv, "друге")
    sent = server.requests[-1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[0]["content"] == "SYS"
    assert sent[1]["content"] == "перше"
    assert sent[-1]["content"] == "друге"


def test_failed_reply_is_not_kept_in_history():
    conv = make_conversation(FakeLlamaServer(status=500))
    with pytest.raises(ModelError):
        ask(conv, "привіт")
    assert conv.history == []


def test_history_is_trimmed_to_max_turns():
    server = FakeLlamaServer("ok")
    conv = make_conversation(server, max_turns=2)
    for i in range(5):
        ask(conv, f"q{i}")
    sent = server.requests[-1]["messages"]
    # system + 2 previous turns + the new question
    assert [m["content"] for m in sent] == ["SYS", "q2", "ok", "q3", "ok", "q4"]


def test_reset_clears_history():
    conv = make_conversation(FakeLlamaServer())
    ask(conv, "привіт")
    conv.reset()
    assert conv.messages() == [{"role": "system", "content": "SYS"}]
