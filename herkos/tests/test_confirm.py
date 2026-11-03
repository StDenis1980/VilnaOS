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


def test_dialog_shows_r4_content_exactly():
    from herkos.dialog import MAX_CONTENT_LINES, content_text

    assert content_text({"level": "R3"}) is None
    assert content_text({"content": None}) is None
    shown = content_text({"content": {"path": "/home/ola/Звіт.odt"}})
    assert "/home/ola/Звіт.odt" in shown
    long = content_text({"content": {"lines": list(range(100))}})
    assert long.splitlines()[-1] == "…"
    assert len(long.splitlines()) == MAX_CONTENT_LINES + 1


def test_terminal_asks_on_a_handed_over_terminal():
    """Gloxi starts Herkos-lite in a new session and hands over its own terminal."""
    import os
    import pty
    import select

    from herkos.confirm import Terminal, default_confirmer

    parent, child = pty.openpty()
    try:
        os.write(parent, b"o\n")
        assert Terminal(fd=child).ask(q(), timeout=5) is Answer.ONCE
        shown = b""
        while select.select([parent], [], [], 0.2)[0]:
            shown += os.read(parent, 65536)
        shown = shown.decode()
        assert "Change a file" in shown and "Allowed" in shown
        assert isinstance(default_confirmer(child), (Terminal, Dialog))
    finally:
        os.close(parent)
        os.close(child)


def test_a_handed_over_non_terminal_is_deny(tmp_path):
    from herkos.confirm import Terminal

    with open(tmp_path / "plain", "w+") as f:
        assert Terminal(fd=f.fileno()).ask(q(), timeout=1) is Answer.DENY


def test_dialog_id_fits_the_flatpak_sandbox():
    """A Flatpak may own only names under its own id; the dialog must not need more."""
    from herkos.dialog import DIALOG_ID, application_id

    assert application_id({}) == DIALOG_ID
    gloxi = "io.github.stdenis1980.Gloxi"
    assert application_id({"FLATPAK_ID": gloxi}).startswith(gloxi + ".")
