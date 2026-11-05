# SPDX-License-Identifier: GPL-3.0-or-later
"""/feedback (ADR 0018): the talk, the draft, the form address, local crash reports."""

import io
import sys
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fake_server import FakeLlamaServer
from helpers import make_conversation

from gloxi import cli, crash
from gloxi.feedback import FORM_URL, FeedbackTalk, wants_feedback

SYSTEM = ["Gloxi: 0.1.0", "System: Test OS, GNOME", "RAM: 16.0 GiB"]


def talk(crash_report=None):
    return FeedbackTalk(system_lines=lambda: list(SYSTEM), latest_crash=lambda: crash_report)


def form(url):
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == FORM_URL
    return {k: v[0] for k, v in parse_qs(parts.query).items()}


def test_bug_report_fills_the_public_form():
    t = talk()
    assert len(t.start().choices) == 3
    t.answer("1")
    t.answer("I asked for my notes and Gloxi froze")
    t.answer("A list of files")
    shown = t.answer("1")  # add the computer
    assert "System: Test OS, GNOME" in shown.text
    assert "public" in shown.text
    assert t.url is None  # nothing opened before the person says so
    done = t.answer("1")
    assert done.done
    fields = form(t.url)
    assert fields["template"] == "gloxi-feedback.yml"
    assert fields["title"].startswith("[bug] I asked for my notes")
    assert "Gloxi froze" in fields["failed"] and "A list of files" in fields["failed"]
    assert fields["system"] == "Test OS, GNOME"
    assert "RAM: 16.0 GiB" in fields["check"]


def test_suggestion_without_computer_details():
    t = talk()
    t.start()
    t.answer("2")
    t.answer("A dark theme for the Herkos window")
    t.answer("2")  # no computer details
    t.answer("1")
    fields = form(t.url)
    assert fields["idea"] == "A dark theme for the Herkos window"
    assert "check" not in fields and "system" not in fields and "failed" not in fields


def test_personal_data_is_hidden_before_it_is_shown():
    t = talk()
    t.start("Gloxi sent nothing to olena@example.com, call me at +380 67 123 45 67")
    t.answer("")
    shown = t.answer("2")
    assert "olena@example.com" not in shown.text
    assert "123 45 67" not in shown.text
    assert "e-mail address" in shown.text and "phone number" in shown.text
    t.answer("1")
    assert "olena%40example.com" not in t.url and "olena@example.com" not in t.url


def test_cancel_and_never_mind_open_nothing():
    t = talk()
    t.start()
    assert t.answer("3").done and t.url is None
    t.start("It crashed")
    t.answer("")
    t.answer("2")
    assert t.answer("2").done and t.url is None
    t.start()
    assert t.cancel().done and not t.active


def test_security_problem_goes_to_the_private_channel():
    t = talk()
    end = t.start("I found a way to bypass Herkos")
    assert end.done and "security/advisories" in end.text and t.url is None


def test_unclear_answers_are_asked_again():
    t = talk()
    t.start()
    assert "1, 2 or 3" in t.answer("maybe").text
    t.answer("2")
    t.answer("x")
    assert "1 or 2" in t.answer("perhaps").text


def test_long_text_is_cut_so_the_address_stays_short():
    t = talk()
    t.start("word " * 2000)
    t.answer("")
    t.answer("2")
    t.answer("1")
    assert len(t.url) < 4000


def test_recent_crash_is_offered_and_shown():
    report = crash.Crash(path=None, time=0, text="Gloxi 0.1.0 crashed\nError: ValueError\n")
    t = talk(report)
    t.start("It closed")
    t.answer("")
    offer = t.answer("2")
    assert "Error: ValueError" in offer.text and offer.choices
    t.answer("1")
    t.answer("1")
    assert "Error: ValueError" in form(t.url)["check"]


@pytest.mark.parametrize(
    "text",
    [
        "Повідом про помилку",
        "повідомити про проблему",
        "Report a bug",
        "I have a suggestion",
        "Quiero reportar un error",
        "zgłoś błąd",
        "Fehler melden",
    ],
)
def test_phrases_start_feedback(text):
    if text.startswith("Quiero"):
        assert not wants_feedback(text)  # only at the start of the message
    else:
        assert wants_feedback(text)


@pytest.mark.parametrize("text", ["Як справи?", "Знайди мій звіт про помилку", "report " * 40])
def test_other_messages_go_to_the_model(text):
    assert not wants_feedback(text)


def test_cli_feedback_opens_the_browser_after_yes():
    server = FakeLlamaServer()
    opened = []
    answers = iter(["/feedback Gloxi froze", "", "2", "1", "/exit"])
    out = io.StringIO()
    cli.repl(
        make_conversation(server),
        read=lambda _prompt: next(answers),
        out=out,
        feedback=talk(),
        open_url=opened.append,
    )
    assert len(opened) == 1 and "Gloxi+froze" in opened[0]
    assert server.requests == []
    assert "/feedback" in out.getvalue()  # in the help line


def _boom():
    raise ValueError("secret text from the person")


def test_crash_report_keeps_only_our_frames(tmp_path):
    try:
        _boom()
    except ValueError:
        exc_type, _exc, tb = sys.exc_info()
    text = crash.report_text(exc_type, tb, when=0)
    assert "Error: ValueError" in text
    assert "secret text" not in text
    assert str(tmp_path.parent) not in text
    path = crash.save(text, tmp_path, when=time.time())
    found = crash.latest(tmp_path)
    assert found is not None and found.path == path and "ValueError" in found.text
    assert crash.latest(tmp_path, now=time.time() + crash.RECENT + 10) is None


def test_crash_reports_are_capped(tmp_path):
    for i in range(crash.MAX_REPORTS + 5):
        crash.save(f"report {i}\n", tmp_path, when=1_000_000 + i)
    assert len(list(tmp_path.glob("crash-*.txt"))) == crash.MAX_REPORTS


def test_crash_save_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert crash.save("x", blocker / "sub") is None
