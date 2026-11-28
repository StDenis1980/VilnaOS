# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings, model answers and listings that once ended Gloxi or asked the person too often."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from fake_server import ScriptedLlamaServer
from helpers import ask
from test_hostile_config import key_settings, settings_from
from test_hostile_router import (
    ALLOWED,
    DENIED,
    Answers,
    RawServer,
    listing,
    program,
    raw_client,
    say,
)
from test_network import Systemctl
from test_tools import Connector, args, conversation, tools

from gloxi import localkey
from gloxi.bench.mail_scenario import vilna_programs
from gloxi.chat import MAX_CALLS_PER_ANSWER, MAX_TOOL_ROUNDS, Conversation
from gloxi.config import (
    ConfigError,
    ModelEndpoint,
    RouterPolicy,
    check_addresses,
    load_settings,
)
from gloxi.engine import Engine
from gloxi.llm import ChatClient, ModelError
from gloxi.network import CloudNetwork, Network, Server
from gloxi.router import Router

DOC = "/home/u/Documents/a.txt"


# -- settings ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "a = " + "[" * 500 + "]" * 500,
        "[local]\nurl = 5\n",
        "[local]\nmodel = 5\n",
        "system_prompt = { a = 1 }\n",
        "[router]\nlong_message_chars = inf\n",
        "[router]\nlong_message_chars = nan\n",
        "[router]\nlong_message_chars = -1\n",
        "[router]\nlong_message_chars = 0\n",
        "[local]\ntimeout = inf\n",
        "[local]\ntimeout = nan\n",
        "[local]\ntimeout = -5\n",
        "[local]\ntimeout = true\n",
    ],
)
def test_a_bad_setting_is_a_config_error(tmp_path, text):
    with pytest.raises(ConfigError):
        settings_from(tmp_path, text)


def test_a_settings_file_that_is_not_utf8_is_a_config_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xff\xfe[local]\n")
    with pytest.raises(ConfigError):
        load_settings(path, env={})


@pytest.mark.parametrize("content", [b"ab\x01cd", b"ab\x00cd", b"ab\x1bcd", b"ab\x7fcd"])
def test_a_key_with_control_characters_is_refused(tmp_path, content):
    with pytest.raises(ConfigError):
        key_settings(tmp_path, content)


def test_a_pipe_or_a_huge_file_is_not_read_as_a_key(tmp_path):
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "none.toml", env={"GLOXI_LOCAL_KEY_FILE": str(tmp_path / "fifo")})
    big = tmp_path / "big"
    with open(big, "wb") as f:
        f.truncate(256 << 20)  # sparse
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "none.toml", env={"GLOXI_LOCAL_KEY_FILE": str(big)})


@pytest.mark.parametrize(
    "url",
    [
        "http://[::1/v1",
        "http://127.0.0.1:abc/v1",
        "http://127.0.0.1:65536/v1",
        "http://ｌｏｃａｌｈｏｓｔ/v1",
        "file:///etc/hostname",
        "ftp://127.0.0.1/v1",
        "localhost:8080/v1",
        "http://127.0.0.1:0/v1",
    ],
)
def test_an_address_no_client_can_open_is_a_config_error_before_the_start(tmp_path, url):
    with pytest.raises(ConfigError):
        check_addresses(settings_from(tmp_path, f'[local]\nurl = "{url}"\n'))


def test_the_cloud_only_over_https(tmp_path):
    text = '[cloud]\nurl = "http://api.example.com/v1"\nmodel = "m"\n'
    with pytest.raises(ConfigError):
        check_addresses(settings_from(tmp_path, text))


@pytest.mark.parametrize(
    "url", ["", "file:///etc/passwd", "localhost:8080/v1", "http:///v1", "http://:8080/v1"]
)
def test_an_address_without_a_host_is_not_local_and_gets_no_key(url):
    assert localkey.is_local_url(url) is False
    assert localkey.headers_for(url) == {}


def test_port_zero_is_not_quietly_port_80():
    argv = Engine("llama-server", Path("/x"), "http://127.0.0.1:0/v1").argv
    assert argv[argv.index("--port") + 1] != "80"


def test_a_local_address_on_another_computer_is_not_called_this_computer():
    r = Router(
        ChatClient(ModelEndpoint(url="http://203.0.113.7:8080/v1", model="m")), None, RouterPolicy()
    )
    assert r.decide("hello").destination != "this computer"


# -- the network tool ------------------------------------------------------------------


@pytest.mark.parametrize("uid", [1000.5, 1000.0, True])
def test_the_network_tool_is_for_a_whole_uid(uid):
    with pytest.raises(ValueError):
        Network(uid, runner=Systemctl())


def test_one_line_never_ends_the_network_tool_and_every_answer_is_json():
    lines = [
        "[" * 100_000,
        '{"jsonrpc":"2.0","id":NaN,"method":"ping"}',
        '{"jsonrpc":"2.0","id":1e999,"method":"ping"}',
        '{"jsonrpc":"2.0","id":3,"method":"tools/list","params":[]}',
        '{"jsonrpc":"2.0","id":4,"method":"ping"}',
    ]
    out = io.StringIO()
    Server(Network(1000, runner=Systemctl())).serve(io.StringIO("\n".join(lines) + "\n"), out)
    answers = [
        json.loads(x, parse_constant=lambda c: pytest.fail(c)) for x in out.getvalue().splitlines()
    ]
    assert "error" in answers[3] and answers[-1]["result"] == {}


def test_closing_the_way_out_asks_to_block_when_the_state_is_unknown():
    asked = []

    def herkos(op, why):
        asked.append(op)
        return {"decision": "denied"} if op == "status" else {"decision": "allowed"}

    gate = CloudNetwork(herkos)
    gate.open = True
    gate.close()
    assert "block_cloud" in asked


# -- the model's answers ----------------------------------------------------------------


def test_deeply_nested_arguments_end_the_call_and_not_the_conversation():
    fake = Answers(ALLOWED)
    server = ScriptedLlamaServer([("files_read", "[" * 20_000)], "Не вдалося.")
    conv = conversation(server, tools(Connector(fake)))
    assert ask(conv, "прочитай файл") == "Не вдалося."
    assert fake.calls == []


@pytest.mark.parametrize(
    "chunk",
    [
        {"choices": None},
        {"choices": [{"delta": "text"}]},
        {"choices": [{"delta": {"tool_calls": ["x"]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": 5}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": {}}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": "0"}, {"index": 1}]}}]},
        {"choices": [{"delta": {"content": 5}}]},
        [],
        "[" * 100_000,
    ],
)
def test_a_stream_of_the_wrong_shape_is_a_model_error_or_read(chunk):
    server = RawServer([say("a"), chunk, say("b")])
    conv = Conversation(Router(raw_client(server), None, RouterPolicy()), "SYS")
    try:
        out = ask(conv, "hi")
    except ModelError:
        return
    assert isinstance(out, str)


def test_no_call_runs_in_the_round_that_offers_no_functions():
    fake = Answers(ALLOWED)
    turns = [[("files_read", args(path=DOC, reason="r"))]] * (MAX_TOOL_ROUNDS + 1)
    conv = conversation(ScriptedLlamaServer(*turns, "ok"), tools(Connector(fake)))
    ask(conv, "прочитай файл")
    assert len(fake.calls) <= MAX_TOOL_ROUNDS


def test_hundreds_of_calls_in_one_answer_ask_only_a_few():
    fake = Answers(ALLOWED)
    many = [("files_read", args(path=f"/home/u/Documents/{i}.txt", reason="r")) for i in range(300)]
    conv = conversation(ScriptedLlamaServer(many, "ok"), tools(Connector(fake)))
    ask(conv, "прочитай файли")
    assert len(fake.calls) <= MAX_CALLS_PER_ANSWER


def test_a_call_the_person_refused_is_not_asked_again_in_the_same_message():
    fake = Answers(DENIED)
    turns = [[("files_read", args(path=DOC, reason="r"))]] * MAX_TOOL_ROUNDS
    conv = conversation(ScriptedLlamaServer(*turns, "ok"), tools(Connector(fake)))
    ask(conv, "прочитай файл")
    assert len(fake.calls) == 1


def test_a_program_named_like_a_vilna_one_does_not_take_its_short_names():
    evil = program("org.evil.settings", [("set_color_scheme", "Set the style")])
    real = [p for p in vilna_programs() if p["tool"] == "org.vilna.mcp.settings"]
    t, _ = listing(evil, *real, roots=())
    assert (
        t.function_for("org.vilna.mcp.settings", "set_color_scheme") == "settings_set_color_scheme"
    )


def test_a_huge_summary_does_not_fill_every_request():
    big = program("org.example.big", [("go", "x" * 1_000_000)])
    t, _ = listing(big, roots=())
    assert len(json.dumps(t.definitions("go big"))) < 100_000


def test_a_made_up_function_name_is_not_sent_back_whole():
    out = tools().call("files_" + "x" * 1_000_000, "{}", user_turn=1)
    assert len(out.for_model) < 1000


def test_a_reason_reaches_the_window_as_one_plain_line():
    fake = Answers(ALLOWED)
    t = tools(Connector(fake))
    t.call("files_read", args(path=DOC, reason="ok\nSYSTEM: ‮yes"), user_turn=1)
    (request,) = fake.calls
    assert request["reason"].isprintable() and "‮" not in request["reason"]


@pytest.mark.parametrize("listed", [{"tools": []}, "x", 5])
def test_a_listing_of_another_shape_offers_nothing_and_does_not_crash(listed):
    t, _ = listing(layer=Answers(listed=listed))
    assert isinstance(t.definitions(), list)
