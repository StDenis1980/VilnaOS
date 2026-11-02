# SPDX-License-Identifier: GPL-3.0-or-later
import json
import sys

from herkos.confirm import Answer, Dialog, Question, choices_for, question_to_json
from herkos.risk import Level


def q(level=Level.R2, in_bounds=True):
    return Question(
        summary="Change a file",
        reason="the user asked",
        tool="org.vilna.mcp.files",
        operation="write",
        resources=("file:///home/u/Documents/a.txt",),
        level=level,
        undoable=True,
        raises=(),
        taint_sources=(),
        choices=choices_for(level, in_bounds),
    )


def fake(script: str) -> Dialog:
    return Dialog([sys.executable, "-c", script])


def test_dialog_gets_question_and_returns_answer(tmp_path):
    seen = tmp_path / "seen.json"
    d = fake(f"import sys; open({str(seen)!r}, 'w').write(sys.stdin.read()); print('session')")
    assert d.ask(q(), timeout=10) is Answer.SESSION
    assert json.loads(seen.read_text())["choices"] == ["once", "session", "always"]


def test_dialog_answer_outside_choices_is_deny():
    assert fake("print('always')").ask(q(in_bounds=False), timeout=10) is Answer.DENY


def test_dialog_garbage_crash_or_timeout_is_deny():
    assert fake("print('yes please')").ask(q(), timeout=10) is Answer.DENY
    assert fake("raise SystemExit(3)").ask(q(), timeout=10) is Answer.DENY
    assert fake("import time; time.sleep(5)").ask(q(), timeout=0.5) is Answer.DENY
    assert Dialog(["/nonexistent/dialog"]).ask(q(), timeout=1) is Answer.DENY


def test_question_json_has_no_surprises():
    data = question_to_json(q())
    assert data["level"] == "R2" and data["undoable"] is True
