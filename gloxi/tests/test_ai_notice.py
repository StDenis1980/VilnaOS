# SPDX-License-Identifier: GPL-3.0-or-later
"""Gloxi says it is an AI and marks its model's answers (AI Act Art. 50, ADR 0033)."""

from __future__ import annotations

import gettext

import pytest
from fake_server import FakeLlamaServer
from helpers import make_conversation
from test_cli import run
from test_gui_controller import RecordingView, sync

from gloxi import ai_notice
from gloxi.gui.controller import ChatController
from gloxi.i18n import DOMAIN, LOCALE_DIR


class MarkingView(RecordingView):
    def mark_answer(self, mark):
        self.events.append(("mark", mark))


def pieces(view):
    return [e[1] for e in view.events if e[0] == "piece"]


def test_the_first_answer_of_each_conversation_says_gloxi_is_an_ai():
    view = MarkingView()
    conv = make_conversation(FakeLlamaServer("Так."))
    ctl = ChatController(conv, view, runner=sync)

    ctl.submit("привіт")
    assert pieces(view)[0].startswith(ai_notice.notice())
    view.events.clear()
    ctl.submit("ще")
    assert ai_notice.notice() not in "".join(pieces(view))

    ctl.reset()
    view.events.clear()
    ctl.submit("знову")
    assert pieces(view)[0].startswith(ai_notice.notice())
    # Shown and read aloud, never sent to the model as something it said.
    assert all(ai_notice.notice() not in (m["content"] or "") for m in conv.history)


def test_a_first_run_talk_is_the_first_message_and_says_it_too(tmp_path):
    from test_backup_intro import FakeHerkos

    from gloxi.backup_intro import BackupIntro

    view = MarkingView()
    intro = BackupIntro(FakeHerkos(), path=tmp_path / "intro.json")
    ctl = ChatController(make_conversation(FakeLlamaServer("Так.")), view, runner=sync, intro=intro)
    assert ctl.offer_intro()
    assert pieces(view)[0].startswith(ai_notice.notice())
    # Its words are fixed, not the model's: no mark.
    assert "mark" not in view.kinds()


def test_model_answers_carry_a_machine_readable_mark():
    view = MarkingView()
    ctl = ChatController(make_conversation(FakeLlamaServer("Так.")), view, runner=sync)
    ctl.submit("привіт")
    marks = [e[1] for e in view.events if e[0] == "mark"]
    assert len(marks) == 1
    assert marks[0]["ai_generated"] is True
    assert marks[0]["model_location"] == "local"
    assert view.kinds().index("mark") < view.kinds().index("piece")


def test_the_terminal_says_it_at_the_start_and_after_reset():
    _code, out, _conv = run(["привіт", "/reset"], FakeLlamaServer("Так."))
    assert out.count(ai_notice.notice()) == 2
    assert out.index(ai_notice.notice()) < out.index("Так.")


LANGUAGES = ("uk", "es", "pl", "de", "fr", "it", "ro", "cs", "lt")
LANGUAGES += ("ca", "ca@valencia", "gl", "eu", "pt")


@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_notice_is_said_in_every_main_language(lang):
    t = gettext.translation(DOMAIN, str(LOCALE_DIR), languages=[lang])
    said = t.gettext(ai_notice.NOTICE)
    assert said != ai_notice.NOTICE
    assert "Gloxi" in said
