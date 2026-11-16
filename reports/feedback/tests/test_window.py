# SPDX-License-Identifier: GPL-3.0-or-later
"""The window on a display, against a fake vilna-reportd.

Needs GTK 4, libadwaita and a display (Xvfb is enough); skipped without them.
"""

import os
import sys
import time

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk
except (ImportError, ValueError):
    pytest.skip("GTK 4 or libadwaita is missing", allow_module_level=True)
if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) or not Gtk.init_check():
    pytest.skip("no display", allow_module_level=True)

from vilna_feedback import texts as t  # noqa: E402
from vilna_feedback import window  # noqa: E402

Adw.init()

REPORTS = [
    {"id": "r1", "state": "waiting", "program": "nautilus", "what": "Files"},
    {"id": "r2", "state": "sent", "status": "known", "program": "gloxi", "what": "Gloxi"},
]


@pytest.fixture(autouse=True)
def no_error_in_callbacks(monkeypatch):
    """PyGObject only prints an exception raised in a signal handler; fail the test on it."""
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: errors.append(exc))
    yield
    assert not errors, errors[0][1]


def spin(until, seconds=5.0):
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.01)
    return until()


@pytest.fixture
def answering(fake):
    sent = []

    def feedback(request):
        sent.append(request["feedback"])
        return {"id": "f-1"}

    fake.answers = {
        "system": {"system": "Vilna 0.1 · 16 GB"},
        "status": {"consent": "ask"},
        "list": {"reports": REPORTS},
        "preview": lambda r: {"feedback": {**r["feedback"], "id": "preview"}},
        "feedback": feedback,
        "discard": {"discarded": True},
        "send": {"id": "r1", "state": "sent"},
    }
    fake.sent = sent
    return fake


def make(reportd, code="uk"):
    app = Adw.Application()
    app.register(None)
    return window.FeedbackWindow(app, reportd, t.Texts(code))


def test_the_window_speaks_the_persons_language(answering, reportd):
    win = make(reportd)
    assert win.get_title() == "Відгук про Vilna"
    assert win.send.get_label() == "Надіслати"
    assert spin(lambda: win.system == "Vilna 0.1 · 16 GB")
    assert win.add_system.get_subtitle() == "Vilna 0.1 · 16 GB"


def test_nothing_optional_is_chosen_in_advance(answering, reportd):
    win = make(reportd)
    assert not win.add_system.get_active()
    assert not win.reply.get_active()
    assert not win.publish.get_active()
    assert not win.email.get_visible()
    win.reply.set_active(True)
    assert win.email.get_visible()


def test_an_empty_text_is_not_sent(answering, reportd):
    win = make(reportd)
    win._on_send()
    assert win.problem.get_visible()
    assert win.problem.get_label() == "Напишіть, що сталося або що ви пропонуєте."
    assert answering.sent == []


def test_the_preview_is_what_the_service_would_send(answering, reportd):
    win = make(reportd)
    win.text.get_buffer().set_text("Панель ховається")
    win.preview.set_expanded(True)
    shown = win.preview_text.get_label()
    assert '"text": "Панель ховається"' in shown
    assert '"id": "preview"' in shown  # it came from the service's own check


def test_send_sends_exactly_the_form(answering, reportd):
    win = make(reportd)
    assert spin(lambda: bool(win.system))
    win.kind_checks["idea"].set_active(True)
    win.text.get_buffer().set_text("Темна тема для входу")
    win.add_system.set_active(True)
    win._on_send()
    assert spin(lambda: answering.sent)
    (body,) = answering.sent
    assert body == {
        "kind": "idea",
        "channel": "window",
        "text": "Темна тема для входу",
        "locale": "uk",
        "program": "vilna-feedback",
        "may_publish": False,
        "system": "Vilna 0.1 · 16 GB",
    }
    assert spin(lambda: win.text.get_buffer().get_char_count() == 0)


def test_without_the_service_the_text_stays(tmp_path):
    from vilna_feedback.client import Reportd

    win = make(Reportd(tmp_path / "missing", timeout=1))
    assert spin(lambda: win.problem.get_visible())
    win.text.get_buffer().set_text("Не губіть мій текст")
    win._on_send()
    assert spin(lambda: win.send.get_sensitive())
    assert win.problem.get_visible()
    assert win.text.get_buffer().get_char_count() > 0


def rows(widget):
    found = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Adw.ActionRow) and not isinstance(child, Adw.ComboRow):
            found.append(child)
        found.extend(rows(child))
        child = child.get_next_sibling()
    return found


def test_crash_reports_list_each_report_with_its_state(answering, reportd):
    win = make(reportd)
    win.show_reports()
    assert spin(lambda: len(rows(win.reports_page)) >= 2)
    found = {r.get_title(): r.get_subtitle() for r in rows(win.reports_page)}
    assert found["Files"] == "Не надіслано"
    assert found["Gloxi"] == "Надіслано · Ми вже знаємо про цей збій"


def test_discard_asks_the_service_and_refreshes(answering, reportd):
    win = make(reportd)
    win.discard_report("r1")
    assert spin(lambda: {"op": "discard", "id": "r1"} in answering.requests)
    assert spin(lambda: any(r["op"] == "list" for r in answering.requests))


def test_notifications_only_for_ask_and_once(answering, reportd, tmp_path):
    app = window.FeedbackApp(reportd)
    app.notify_file = tmp_path / "notified.json"
    shown = []
    app.send_notification = lambda key, note: shown.append(key)
    app._notify(("ask", REPORTS))
    assert shown == ["crash-r1"]
    app._notify(("ask", REPORTS))
    assert shown == ["crash-r1"]
    app._notify(("auto", [{"id": "r3", "state": "waiting"}]))
    assert shown == ["crash-r1"]
