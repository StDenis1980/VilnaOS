# SPDX-License-Identifier: GPL-3.0-or-later
"""`gloxi-gtk --ask TEXT`: the question from the Gloxi widget on the Vilna desktop."""

from __future__ import annotations

import pytest

window = pytest.importorskip("gloxi.gui.window")
from gi.repository import GLib  # noqa: E402


class FakeCommandLine:
    def __init__(self, **options):
        self._dict = GLib.VariantDict.new(None)
        for key, value in options.items():
            self._dict.insert_value(key, value)

    def get_options_dict(self):
        return self._dict


class FakeApp:
    def __init__(self):
        self.calls = []

    def ask(self, text):
        self.calls.append(("ask", text))

    def activate(self):
        self.calls.append(("activate",))

    def toggle(self):
        self.calls.append(("toggle",))

    def talk_about_vpn(self):
        self.calls.append(("vpn",))


def test_ask_passes_the_question():
    app = FakeApp()
    line = FakeCommandLine(ask=GLib.Variant("s", "Що сьогодні?"))
    assert window.GloxiApp.do_command_line(app, line) == 0
    assert app.calls == [("ask", "Що сьогодні?")]


def test_no_option_just_shows_gloxi():
    app = FakeApp()
    assert window.GloxiApp.do_command_line(app, FakeCommandLine()) == 0
    assert app.calls == [("activate",)]


class FakeController:
    def __init__(self):
        self.sent = []

    def submit(self, text):
        self.sent.append(text)
        return True


def test_ask_submits_trimmed_and_cut(monkeypatch):
    controller = FakeController()

    class Window:
        pass

    win = Window()
    win.controller = controller
    monkeypatch.setattr(window, "ChatWindow", Window)
    app = FakeApp()
    app.get_active_window = lambda: win
    window.GloxiApp.ask(app, "  " + "я" * (window.MAX_ASK + 50) + "  ")
    window.GloxiApp.ask(app, "   ")
    assert controller.sent == ["я" * window.MAX_ASK]
    assert app.calls == [("activate",), ("activate",)]
