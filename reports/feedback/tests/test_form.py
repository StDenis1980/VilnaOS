# SPDX-License-Identifier: GPL-3.0-or-later
"""The form, the preview and «Ask me every time» without GTK."""

import json

from vilna_feedback import form
from vilna_feedback import texts as t


def test_nothing_optional_goes_unless_the_person_chose_it():
    f = form.Form(text="  The dock hides  ", system="Vilna 0.1", contact="a@b.ua", locale="uk")
    body = f.body()
    assert body == {
        "kind": "bug",
        "channel": "window",
        "text": "The dock hides",
        "locale": "uk",
        "program": "vilna-feedback",
        "may_publish": False,
    }
    f.add_system = True
    f.reply = True
    f.may_publish = True
    body = f.body()
    assert body["system"] == "Vilna 0.1"
    assert body["contact"] == "a@b.ua"
    assert body["may_publish"] is True


def test_problems_are_shown_before_sending():
    assert form.Form(text="   ").problem() == t.EMPTY_TEXT
    assert form.Form(text="x" * (form.MAX_TEXT + 1)).problem() == t.TOO_LONG
    assert form.Form(text="hi", reply=True, contact="not an address").problem() == t.BAD_EMAIL
    assert form.Form(text="hi", reply=True, contact="olena@vilnaos.com").problem() is None
    # An address typed and then switched off is neither checked nor sent.
    off = form.Form(text="hi", reply=False, contact="nonsense")
    assert off.problem() is None and "contact" not in off.body()


def test_an_unknown_kind_is_other():
    assert form.Form(kind="spam", text="x").body()["kind"] == "other"


def test_the_preview_is_the_request():
    body = form.Form(text="Привіт", locale="uk").body()
    shown = form.preview(body)
    assert json.loads(shown) == body
    assert "Привіт" in shown  # readable, not \u escapes


def test_status_text():
    _ = t.Texts("en")
    assert form.status_text({"state": "waiting"}, _) == "Not sent"
    assert (
        form.status_text({"state": "sent", "status": "fixed", "fixed_in": "0.2"}, _)
        == "Sent · Fixed in 0.2"
    )
    assert form.status_text({"state": "sent", "status": "known"}, _) == (
        "Sent · We already know about this crash"
    )
    assert form.status_text({"state": "odd"}, _) == "Not sent"


REPORTS = [
    {"id": "a", "state": "waiting"},
    {"id": "b", "state": "sent"},
    {"id": "c", "state": "waiting"},
]


def test_only_the_answer_ask_notifies_and_each_report_once():
    assert form.to_notify("auto", REPORTS, set()) == []
    assert form.to_notify("never", REPORTS, set()) == []
    assert form.to_notify(None, REPORTS, set()) == []
    assert [r["id"] for r in form.to_notify("ask", REPORTS, set())] == ["a", "c"]
    assert [r["id"] for r in form.to_notify("ask", REPORTS, {"a"})] == ["c"]


def test_the_notified_file_forgets_gone_reports(tmp_path):
    path = tmp_path / "state" / "notified.json"
    assert form.load_notified(path) == set()
    form.save_notified(path, {"a", "gone"})
    assert form.load_notified(path) == {"a", "gone"}
    assert form.still_waiting(REPORTS, form.load_notified(path)) == {"a"}
    path.write_text("{broken", encoding="utf-8")
    assert form.load_notified(path) == set()


def test_state_dir_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert form.state_dir() == tmp_path / "vilna-feedback"
    monkeypatch.setenv("XDG_STATE_HOME", "relative")
    assert form.state_dir().name == "vilna-feedback"
    assert form.state_dir().is_absolute()
