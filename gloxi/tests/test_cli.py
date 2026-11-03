# SPDX-License-Identifier: GPL-3.0-or-later
import io

from fake_server import FakeLlamaServer
from helpers import make_conversation

from gloxi.cli import repl, split_command
from gloxi.config import RouterPolicy


def run(lines, server, **kwargs):
    inputs = iter(lines)

    def read(_prompt):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError from None

    out = io.StringIO()
    conv = make_conversation(server, **kwargs)
    code = repl(conv, read=read, out=out)
    return code, out.getvalue(), conv


def test_repl_answers_and_exits():
    code, out, conv = run(["привіт", "/exit"], FakeLlamaServer("Привіт! Я Gloxi."))
    assert code == 0
    assert "[local] Привіт! Я Gloxi." in out
    assert "Cloud: off" in out
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


def test_repl_cloud_command_and_unmasked_warning():
    cloud = FakeLlamaServer("з хмари")
    code, out, _conv = run(
        ["/cloud поясни", "/exit"],
        FakeLlamaServer(),
        cloud=cloud,
        policy=RouterPolicy(cloud_allowed=True),
    )
    assert code == 0
    assert "NOT masked" in out
    assert "[cloud: api.example.eu, not masked] з хмари" in out


def test_repl_cloud_command_refused_when_off(capsys):
    cloud = FakeLlamaServer()
    run(["/cloud поясни", "/exit"], FakeLlamaServer(), cloud=cloud)
    assert "turned off" in capsys.readouterr().err
    assert cloud.requests == []


def test_split_command():
    assert split_command("/cloud hi there") == ("cloud", "hi there")
    assert split_command("/local") == ("local", "")
    assert split_command("/cloudy day") == (None, "/cloudy day")
    assert split_command("hello") == (None, "hello")


def test_repl_cloud_answer_is_masked_and_restored():
    from gloxi.privacy import guard_factory

    on = RouterPolicy(cloud_allowed=True)
    cloud = FakeLlamaServer("Надіслано на [EMAIL_1]")
    code, out, _conv = run(
        ["/cloud напиши на olena@example.com", "/exit"],
        FakeLlamaServer(),
        cloud=cloud,
        policy=on,
        guard_factory=guard_factory(on),
    )
    assert code == 0
    assert "NOT masked" not in out
    assert "[cloud: api.example.eu, masked] Надіслано на olena@example.com" in out
    assert "olena@example.com" not in str(cloud.requests)


def test_repl_shows_tool_calls():
    import json

    from fake_server import ScriptedLlamaServer
    from helpers import LOCAL
    from test_tools import Connector, tools

    from gloxi.chat import Conversation
    from gloxi.llm import ChatClient
    from gloxi.router import Router

    call = [("files_list", json.dumps({"path": "/home/u/Documents", "reason": "r"}))]
    server = ScriptedLlamaServer(call, "Там два файли.")
    router = Router(ChatClient(LOCAL, transport=server.transport()), None, RouterPolicy())
    conv = Conversation(router, "SYS", tools=tools(Connector()))
    out = io.StringIO()
    inputs = iter(["що в документах?"])
    repl(conv, read=lambda _p: next(inputs, None) or (_ for _ in ()).throw(EOFError()), out=out)
    assert "[files: list /home/u/Documents (done)]" in out.getvalue()
    assert "Там два файли." in out.getvalue()


def test_closing_the_terminal_still_stops_the_children(tmp_path):
    """SIGHUP/SIGTERM leave through the ExitStack, which stops llama-server and Herkos."""
    import signal
    import subprocess
    import sys

    stopped = tmp_path / "stopped"
    script = f"""
import signal, sys, time
from contextlib import ExitStack
from gloxi import cli
for s in (signal.SIGTERM, signal.SIGHUP):
    signal.signal(s, cli._leave)
with ExitStack() as stack:
    stack.callback(lambda: open({str(stopped)!r}, "w").write("yes"))
    print("ready", flush=True)
    input()
"""
    for signum in (signal.SIGHUP, signal.SIGTERM):
        stopped.unlink(missing_ok=True)
        proc = subprocess.Popen(
            [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        assert proc.stdout.readline().strip() == b"ready"
        proc.send_signal(signum)
        assert proc.wait(timeout=10) == 128 + signum
        assert stopped.read_text() == "yes"
        proc.stdin.close()
        proc.stdout.close()
