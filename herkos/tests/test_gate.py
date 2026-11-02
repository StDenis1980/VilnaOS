# SPDX-License-Identifier: GPL-3.0-or-later
import json

from conftest import Env

from herkos.confirm import Answer
from herkos.mitos import verify
from herkos.policy import SessionState
from herkos.request import parse_request
from herkos.risk import Level


def request(env: Env, op: str, *paths, tool="org.vilna.mcp.files"):
    return parse_request(
        {
            "tool": tool,
            "operation": op,
            "reason": "the user asked",
            "resources": [env.uri(p) for p in paths],
            "arguments": {"text": "secret contents"},
        }
    )


def records(env: Env):
    return [json.loads(x) for x in env.config.journal_path.read_text().splitlines()]


def test_read_asks_once_then_uses_permission(tmp_path):
    env = Env(tmp_path, Answer.ALWAYS)
    s = SessionState()
    first = env.gate.handle(request(env, "read", env.docs / "a.txt"), s)
    assert first.allowed and first.code == "allowed"
    assert len(env.confirmer.questions) == 1
    second = env.gate.handle(request(env, "read", env.docs / "b.txt"), s)
    assert second.allowed and "permission" in second.message
    assert len(env.confirmer.questions) == 1
    assert len(env.files.calls) == 2


def test_user_says_no(env):
    out = env.gate.handle(request(env, "read", env.docs / "a.txt"), SessionState())
    assert not out.allowed and out.code == "denied_user"
    assert env.files.calls == []


def test_unknown_tool_and_operation_are_denied(env):
    s = SessionState()
    out = env.gate.handle(request(env, "read", tool="org.example.none"), s)
    assert out.code == "denied_unknown"
    out = env.gate.handle(request(env, "chmod", env.docs / "a"), s)
    assert out.code == "denied_unknown"
    assert env.confirmer.questions == []


def test_r3_is_denied_in_lite_without_asking(tmp_path):
    env = Env(tmp_path, Answer.ONCE)
    out = env.gate.handle(request(env, "delete", env.docs / "a"), SessionState())
    assert out.code == "denied_level" and out.level == Level.R3
    assert env.confirmer.questions == []


def test_taint_from_a_read_raises_the_next_write(tmp_path):
    env = Env(tmp_path, Answer.ALWAYS)
    s = SessionState()
    assert env.gate.handle(request(env, "write", env.docs / "a"), s).allowed  # R2, clean
    assert env.gate.handle(request(env, "read", env.docs / "mail.eml"), s).allowed
    assert s.tainted
    # The same write is now R3: the permission no longer covers it, and lite denies.
    out = env.gate.handle(request(env, "write", env.docs / "a"), s)
    assert out.code == "denied_level" and "untrusted" in out.message


def test_out_of_bounds_read_never_gets_a_lasting_permission(tmp_path):
    env = Env(tmp_path, Answer.SESSION)
    s = SessionState()
    out = env.gate.handle(request(env, "read", env.other / "x"), s)
    assert out.allowed and out.level == Level.R2
    q = env.confirmer.questions[0]
    assert Answer.ALWAYS not in q.choices
    # Session permission was given with no roots, so it covers nothing outside.
    env.gate.handle(request(env, "read", env.other / "y"), s)
    assert len(env.confirmer.questions) == 2


def test_answer_outside_choices_is_deny(tmp_path):
    env = Env(tmp_path, Answer.ALWAYS)
    out = env.gate.handle(request(env, "read", env.other / "x"), SessionState())
    assert out.code == "denied_user"


def test_crashing_dialog_is_deny(tmp_path):
    def boom(q):
        raise RuntimeError("no display")

    env = Env(tmp_path, boom)
    out = env.gate.handle(request(env, "read", env.docs / "a"), SessionState())
    assert out.code == "denied_user"


def test_tool_error_is_reported(tmp_path):
    env = Env(tmp_path, Answer.ONCE)
    env.files.fail = True
    out = env.gate.handle(request(env, "write", env.docs / "a"), SessionState())
    assert out.code == "tool_error" and out.message == "disk full"
    assert out.to_json()["decision"] == "allowed" and "result" not in out.to_json()


def test_journal_records_every_step_without_contents(tmp_path):
    env = Env(tmp_path, Answer.ALWAYS)
    env.gate.handle(request(env, "read", env.docs / "a"), SessionState())
    kinds = [r["kind"] for r in records(env)]
    assert kinds == ["request", "grant", "decision", "result"]
    text = env.config.journal_path.read_text()
    assert "secret contents" not in text
    assert len({r["action_id"] for r in records(env)}) == 1
    assert verify(env.config.journal_path).ok


def test_no_journal_no_action(tmp_path):
    env = Env(tmp_path, Answer.ONCE)
    env.journal.append("start", {})
    env.config.journal_path.write_text("tampered\n")  # the running journal notices
    out = env.gate.handle(request(env, "write", env.docs / "a"), SessionState())
    assert out.code == "denied_journal"
    assert env.files.calls == []


def test_tool_annotations_raise_level(tmp_path):
    env = Env(tmp_path, Answer.ONCE)
    env.files._annotations = {"destructiveHint": True}
    out = env.gate.handle(request(env, "read", env.docs / "a"), SessionState())
    assert out.code == "denied_level"
