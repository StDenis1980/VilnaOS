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
            "arguments": {"path": str(paths[0]) if paths else "/", "text": "secret contents"},
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
    # The same write is now R3: the permission no longer covers it. Lite asks
    # again, "once" only, so the answer "always" counts as a no.
    out = env.gate.handle(request(env, "write", env.docs / "a"), s)
    assert out.code == "denied_user" and out.level == Level.R3
    q = env.confirmer.questions[-1]
    assert q.choices == (Answer.ONCE,)


def test_a_tainted_write_shows_exactly_what_will_be_written(tmp_path):
    """Decision 2026-11-03: "find a note and change it" works in one conversation."""
    env = Env(tmp_path, Answer.ONCE)
    s = SessionState()
    assert env.gate.handle(request(env, "read", env.docs / "note.txt"), s).allowed
    out = env.gate.handle(request(env, "write", env.docs / "note.txt"), s)
    assert out.allowed and out.level == Level.R3
    q = env.confirmer.questions[-1]
    assert q.content == {"path": str(env.docs / "note.txt"), "text": "secret contents"}
    assert "untrusted content is in the conversation" in q.raises
    # Never a lasting permission: the next write asks again.
    before = len(env.confirmer.questions)
    assert env.gate.handle(request(env, "write", env.docs / "note.txt"), s).allowed
    assert len(env.confirmer.questions) == before + 1


def test_a_tainted_write_raised_for_more_than_taint_is_still_denied(tmp_path):
    env = Env(tmp_path, Answer.ONCE)
    s = SessionState()
    s.taint("org.vilna.mcp.files.read")
    out = env.gate.handle(request(env, "write", env.other / "x"), s)  # also out of bounds
    assert out.code == "denied_level"
    out = env.gate.handle(request(env, "delete", env.docs / "a"), s)  # R3 on its own
    assert out.code == "denied_level"


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
