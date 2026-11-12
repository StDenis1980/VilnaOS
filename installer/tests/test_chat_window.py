# SPDX-License-Identifier: GPL-3.0-or-later
"""The chat window on a display: it answers while the tools start, and a double click is one.

Needs GTK 4, libadwaita and a display (Xvfb is enough); skipped without them.
"""

import os
import threading
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

from vilna_mcp_installer import plan as plans  # noqa: E402

from vilna_installer import chat, start  # noqa: E402
from vilna_installer import conversation as conv  # noqa: E402

Adw.init()


class Ready:
    def available(self):
        return True


class Talk:
    """Only what the window uses; the turns themselves are test_conversation's."""

    def __init__(self, plan_path):
        self.plan_path = plan_path
        self.model = Ready()
        self.gone_to = []

    def _reply(self, text, step):
        return conv.Reply(text, conv.BY_KEY[step], plans.load(self.plan_path), [])

    def opening(self):
        return self._reply("Hello", "languages")

    def needs(self):
        return []

    def goto(self, key):
        self.gone_to.append(key)
        return conv.BY_KEY[key]

    def resume(self):
        return self._reply("What shall we do with the disk?", "disk")

    def send(self, text):
        return self._reply("Erase it, then.", "disk")


@pytest.fixture
def plan_path(tmp_path):
    path = tmp_path / "vilna-installer" / "plan.json"
    start.write_plan(start.start_plan("en", False, True), path)
    return path


def spin(until, seconds=5.0):
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.01)
    return until()


def bubbles(win):
    out, child = [], win.list.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Label):
            out.append(child.get_label())
        child = child.get_next_sibling()
    return out


def window(plan_path, **kw):
    app = Adw.Application()
    app.register(None)  # windows belong to a started application
    return chat.ChatWindow(app, kw.pop("talk", None), plan_path, demo=True, **kw)


def test_the_window_answers_and_says_gloxi_wakes_while_the_tools_start(plan_path):
    go = threading.Event()
    talk = Talk(plan_path)

    def starting():
        go.wait(10)  # the tools reading Python from a busy USB stick
        return talk

    began = time.monotonic()
    win = window(plan_path, starting=starting)
    assert time.monotonic() - began < 2  # not waiting for the tools
    waking = win._(conv.WAKING)
    assert bubbles(win) == [waking]
    go.set()
    assert spin(lambda: "Hello" in bubbles(win))
    assert waking not in bubbles(win)
    assert win.conversation is talk


def test_tools_that_do_not_start_leave_the_classic_wizard(plan_path):
    win = window(plan_path, starting=lambda: None)
    assert spin(lambda: win._(conv.NO_MODEL) in bubbles(win))
    assert win.conversation is None


def test_a_double_click_on_change_is_one_change(plan_path):
    talk = Talk(plan_path)
    win = window(plan_path, talk=talk)
    assert spin(lambda: "Hello" in bubbles(win))
    win._on_change(None, "disk")
    assert spin(lambda: not win.busy)  # the step's question needs no model: done at once
    win._on_change(None, "disk")
    spin(lambda: False, 0.3)
    assert talk.gone_to == ["disk"]
    assert bubbles(win).count("What shall we do with the disk?") == 1
    # After the person says something, «Change» works again.
    win.entry.set_text("Erase it")
    win._on_send()
    assert spin(lambda: not win.busy)
    win._on_change(None, "disk")
    assert spin(lambda: not win.busy)
    assert talk.gone_to == ["disk", "disk"]


def test_the_first_screen_stays_until_the_chat_is_on_the_screen(plan_path):
    from vilna_installer import welcome as first

    app = first.InstallerApp(plan_path, True, "en")
    app.register(None)
    screen = first.Welcome(app, plan_path, "en")
    opened = []
    app.open_chat = opened.append
    screen._finish(assistant=True)
    screen._finish(assistant=True)  # a second press while the chat comes up
    assert opened == [screen]
    assert not screen.get_sensitive()

    on_map = first._once_mapped(screen)
    assert screen in app.get_windows()  # nothing yet: the chat is not on the screen
    on_map(None)
    on_map(None)  # mapped again (unfullscreen, a workspace switch): nothing more
    assert screen not in app.get_windows()
