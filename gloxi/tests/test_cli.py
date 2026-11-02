# SPDX-License-Identifier: GPL-3.0-or-later
import io

from fake_server import FakeLlamaServer

from gloxi.chat import Conversation
from gloxi.cli import repl
from gloxi.config import ModelEndpoint
from gloxi.llm import ChatClient

ENDPOINT = ModelEndpoint(url="http://127.0.0.1:8080/v1", model="local")


def run(lines, server):
    inputs = iter(lines)

    def read(_prompt):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError from None

    out = io.StringIO()
    conv = Conversation(ChatClient(ENDPOINT, transport=server.transport()), "SYS")
    code = repl(conv, read=read, out=out)
    return code, out.getvalue(), conv


def test_repl_answers_and_exits():
    code, out, conv = run(["привіт", "/exit"], FakeLlamaServer("Привіт! Я Gloxi."))
    assert code == 0
    assert "Привіт! Я Gloxi." in out
    assert len(conv.history) == 2


def test_repl_reset_and_eof():
    code, out, conv = run(["привіт", "/reset"], FakeLlamaServer())
    assert code == 0
    assert "Conversation cleared." in out
    assert conv.history == []


def test_repl_survives_model_error(capsys):
    code, _out, conv = run(["привіт", "/exit"], FakeLlamaServer(status=500))
    assert code == 0
    assert "HTTP 500" in capsys.readouterr().err
    assert conv.history == []
