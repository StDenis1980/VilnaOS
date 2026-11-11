# SPDX-License-Identifier: GPL-3.0-or-later
import pytest
from fake_server import FakeLlamaServer
from helpers import ask, make_conversation

from gloxi.chat import UKRAINIAN_HINT
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
    assert sent[-1]["content"] == f"друге\n\n{UKRAINIAN_HINT}"


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


# -- the answer language (Build14.6: a Ukrainian question got a Russian answer) ----


@pytest.mark.parametrize(
    ("text", "ukrainian"),
    [
        ("Покажи файли в Документах", True),  # no і, ї, є or ґ, still Ukrainian
        ("Що в моїх документах?", True),
        ("Покажи файл report.pdf", True),
        ("Покажи файлы в Документах", False),  # ы: Russian
        ("Это мой ответ", False),
        ("Show my documents", False),
        ("Pokaż pliki", False),
        ("", False),
        ("123 ?!", False),
    ],
)
def test_ukrainian_is_told_apart_from_russian(text, ukrainian):
    from gloxi.chat import is_ukrainian

    assert is_ukrainian(text) is ukrainian


def test_a_ukrainian_message_reminds_the_local_model_of_the_language():
    server = FakeLlamaServer("ok")
    conv = make_conversation(server)
    ask(conv, "Покажи файли в Документах")
    sent = server.requests[-1]["messages"]
    # At the end of this request only: the cached start and the history stay as they were.
    assert sent[-1]["content"] == f"Покажи файли в Документах\n\n{UKRAINIAN_HINT}"
    assert sent[0]["content"] == "SYS"
    assert conv.history[0]["content"] == "Покажи файли в Документах"


@pytest.mark.parametrize("text", ["Show my documents", "Покажи файлы"])
def test_other_languages_get_no_reminder(text):
    server = FakeLlamaServer("ok")
    conv = make_conversation(server)
    ask(conv, text)
    assert server.requests[-1]["messages"][-1]["content"] == text


def test_the_cloud_model_gets_no_reminder():
    from gloxi.config import RouterPolicy

    local, cloud = FakeLlamaServer("local"), FakeLlamaServer("cloud")
    conv = make_conversation(local, cloud=cloud, policy=RouterPolicy(cloud_allowed=True))
    ask(conv, "Покажи файли", force="cloud")
    assert cloud.requests[-1]["messages"][-1]["content"] == "Покажи файли"


def test_the_default_prompt_asks_for_the_language_of_the_message():
    from gloxi.config import DEFAULT_SYSTEM_PROMPT

    assert "latest message" in DEFAULT_SYSTEM_PROMPT
    assert "never in Russian" in DEFAULT_SYSTEM_PROMPT


# -- reading the prompt in advance (Build14.6: 2.5 minutes before the first answer) --


def test_warm_has_the_model_read_the_start_of_the_first_request():
    server = FakeLlamaServer("ok")
    conv = make_conversation(server)
    assert conv.warm() is True
    warm = server.requests[0]
    assert warm["stream"] is False
    assert warm["max_tokens"] == 1 and warm["cache_prompt"] is True
    assert warm["messages"] == [{"role": "system", "content": "SYS"}]
    ask(conv, "привіт")
    assert server.requests[1]["messages"][0] == warm["messages"][0]


def test_warm_is_only_for_a_new_conversation_and_never_fails():
    server = FakeLlamaServer("ok")
    conv = make_conversation(server)
    ask(conv, "привіт")
    assert conv.warm() is False  # the conversation started: its own requests keep the cache
    assert len(server.requests) == 1
    assert make_conversation(FakeLlamaServer(status=503)).warm() is False


def test_warm_does_not_wake_the_cloud():
    from gloxi.config import RouterPolicy

    local, cloud = FakeLlamaServer("local"), FakeLlamaServer("cloud")
    policy = RouterPolicy(cloud_allowed=True, mode="cloud")
    conv = make_conversation(local, cloud=cloud, policy=policy)
    assert conv.warm() is False
    assert local.requests == [] and cloud.requests == []
