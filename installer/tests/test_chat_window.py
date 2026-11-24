# SPDX-License-Identifier: GPL-3.0-or-later
"""The chat window on a display: it answers while the tools start, and a double click is one.

Needs GTK 4, libadwaita and a display (Xvfb is enough); skipped without them.
"""

import os
import threading
import time
from pathlib import Path

import pytest

# The first screen writes GNOME settings (keyboard layouts, the screen
# reader): in memory, never in the settings of whoever runs the tests.
os.environ["GSETTINGS_BACKEND"] = "memory"

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

    def answered(self, key):
        self.gone_to.append(f"answered {key}")

    def resume(self, entered=None):
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
    """The texts of the chat: the person's bubbles and Gloxi's (beside its owl)."""
    out, child = [], win.list.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Label) and child.has_css_class("vilna-bubble"):
            out.append(child.get_label())
        elif child.has_css_class("vilna-gloxi-row"):
            column = child.get_last_child()
            out.append(column.get_last_child().get_label())
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


def test_preloading_goes_on_after_a_module_that_fails():
    from vilna_installer import welcome as first

    seen = []

    def load(name):
        seen.append(name)
        if name == "b":
            raise ImportError("broken")

    first.preload(("a", "b", "c"), load=load)
    assert seen == ["a", "b", "c"]


def test_the_next_screen_opens_only_after_its_code_is_read(plan_path, monkeypatch):
    from vilna_installer import welcome as first

    woken = []
    monkeypatch.setattr(first.start, "MODEL_WAKE_DELAY_S", 0)
    monkeypatch.setattr(first.start, "wake_model", lambda: woken.append(True))
    app = first.InstallerApp(plan_path, True, "en")
    app.register(None)
    screen = first.Welcome(app, plan_path, "en")
    screen.preloaded = threading.Event()
    opened = []
    app.open_chat = opened.append
    screen._finish(assistant=True)
    assert spin(lambda: False, seconds=0.3) is False
    assert opened == []  # still reading: the screen stays and answers
    assert woken == []

    screen.is_active = lambda: True  # GNOME Shell shows it and gives it the keyboard
    threading.Thread(
        target=screen._preload_then_wake, kwargs={"load": lambda: None}, daemon=True
    ).start()
    assert spin(lambda: opened == [screen])
    assert spin(lambda: woken == [True])  # the model wakes after the code is read


def test_the_model_waits_until_the_shell_shows_the_first_screen(plan_path, monkeypatch):
    from vilna_installer import welcome as first

    woken = []
    monkeypatch.setattr(first.start, "MODEL_WAKE_DELAY_S", 0)
    monkeypatch.setattr(first.start, "wake_model", lambda: woken.append(True))
    app = first.InstallerApp(plan_path, True, "en")
    app.register(None)
    screen = first.Welcome(app, plan_path, "en")
    shown = False
    screen.is_active = lambda: shown
    screen._wake_when_shown()
    assert spin(lambda: False, seconds=0.3) is False
    assert woken == []  # mapped, but not on the screen yet

    shown = True
    screen.notify("is-active")
    screen.notify("is-active")  # focus comes and goes: one wake
    assert spin(lambda: woken == [True])
    assert spin(lambda: False, seconds=0.3) is False
    assert woken == [True]


def test_the_model_wakes_without_a_keyboard_too(plan_path, monkeypatch):
    from vilna_installer import welcome as first

    woken = []
    monkeypatch.setattr(first, "SHOWN_TIMEOUT_S", 0)
    monkeypatch.setattr(first.start, "MODEL_WAKE_DELAY_S", 0)
    monkeypatch.setattr(first.start, "wake_model", lambda: woken.append(True))
    app = first.InstallerApp(plan_path, True, "en")
    app.register(None)
    screen = first.Welcome(app, plan_path, "en")
    screen.is_active = lambda: False
    screen._wake_when_shown()
    assert spin(lambda: woken == [True])


def test_the_next_screen_opens_at_once_when_the_code_is_read(plan_path):
    from vilna_installer import welcome as first

    app = first.InstallerApp(plan_path, True, "en")
    app.register(None)
    screen = first.Welcome(app, plan_path, "en")
    screen.preloaded = threading.Event()
    screen.preloaded.set()
    opened = []
    app.open_wizard = opened.append
    screen._finish(assistant=False)
    assert opened == [screen]


def cards(win):
    out, child = [], win.list.get_first_child()
    while child is not None:
        if child.has_css_class("vilna-card"):
            out.append(child)
        child = child.get_next_sibling()
    return out


def buttons(widget):
    out, stack = [], [widget]
    while stack:
        w = stack.pop()
        if isinstance(w, Gtk.Button) and not isinstance(w, Gtk.Expander):
            out.append(w)
        child = w.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return out


def test_the_person_answers_about_crash_reports_with_their_own_button(plan_path):
    """ADR 0018 section 3: three equal buttons, none suggested; Gloxi does not answer."""
    talk = Talk(plan_path)
    win = window(plan_path, talk=talk)
    assert spin(lambda: "Hello" in bubbles(win))
    win._card("crash_reports", plans.load(plan_path))
    (card,) = cards(win)
    labels = [b.get_label() for b in buttons(card)]
    assert sorted(labels) == sorted(win._(title) for title, _hint in conv.CRASH_ANSWERS.values())
    assert not any(b.has_css_class("suggested-action") for b in buttons(card))
    win._on_crash_answer(None, "never", card)
    assert plans.load(plan_path)["privacy"]["crash_reports"] == "never"
    assert cards(win) == [] and "answered privacy" in talk.gone_to
    assert win._(conv.CRASH_NEVER) in bubbles(win)


@pytest.mark.parametrize(("demo", "expected"), [(None, ["uk"]), ("empty", [])])
def test_the_chosen_languages_keyboard_is_set_only_on_a_real_computer(
    plan_path, monkeypatch, demo, expected
):
    from vilna_installer import welcome as first

    followed = []
    monkeypatch.setattr(first, "follow_keyboard", followed.append)
    app = first.InstallerApp(plan_path, True, "uk", demo)
    app.register(None)
    screen = first.Welcome(app, plan_path, "uk")
    app.open_chat = lambda _w: None
    screen._finish(assistant=True)
    assert followed == expected


def test_gloxi_speaks_beside_its_owl_and_steps_show_progress(plan_path):
    win = window(plan_path)
    win._say("Hello")
    row = win.list.get_last_child()
    assert row.has_css_class("vilna-gloxi-row")
    owl = row.get_first_child()
    assert isinstance(owl, Gtk.Image)
    assert win.step_count.get_label() == "Step 1 of 7"
    assert win.heading.get_subtitle() == "Step 1 of 7"
    assert 0 <= win.progress.get_fraction() <= 1


def test_the_screen_reader_hears_gloxi_and_what_went_wrong(plan_path, monkeypatch):
    """Gloxi's reply appears where the focus is not: it was never said (item 34)."""
    from vilna_installer import a11y

    said = []
    monkeypatch.setattr(a11y, "announce", lambda _widget, text, **_kw: said.append(text))
    talk = Talk(plan_path)
    win = window(plan_path, talk=talk)
    assert spin(lambda: "Hello" in said)
    win.entry.set_text("erase it")
    win._on_send()
    assert win._(conv.THINKING) in said
    assert spin(lambda: "Erase it, then." in said)
    assert "erase it" not in said  # the person's own words are not read back
    win._card("user_password", plans.load(plan_path))
    (card,) = cards(win)
    first, second = [e for e in _all(card, Gtk.PasswordEntry)]
    first.set_text("short")
    second.set_text("short")
    buttons(card)[-1].emit("clicked")
    assert said[-1] == win._(conv.SHORT)


def _all(widget, kind):
    out = [widget] if isinstance(widget, kind) else []
    child = widget.get_first_child()
    while child is not None:
        out += _all(child, kind)
        child = child.get_next_sibling()
    return out


@pytest.mark.parametrize("code", ("uk", "es", "pl", "de", "fr", "it", "ro", "cs", "lt"))
def test_steps_panel_texts_are_translated(code):
    from vilna_installer import start

    catalog = start.catalog(code)
    assert [t for t in chat.TEXTS if t not in catalog] == []


def test_owls_ship_and_match_the_design_source():
    design = Path(__file__).resolve().parents[2] / "docs" / "design" / "owl-states"
    for state in ("waiting", "thinking", "error"):
        for dark in (False, True):
            shipped = chat.owl_file(state, dark)
            assert shipped.is_file()
            source = design / shipped.name
            if source.exists():
                assert shipped.read_bytes() == source.read_bytes()


def _labels(widget):
    return [w.get_label() for w in _all(widget, Gtk.Label)]


def test_password_card_without_a_user_name_is_not_cut_off(plan_path):
    """Build12.19 in a VM: the card read «Password for» with nothing after it."""
    win = window(plan_path, talk=Talk(plan_path))
    plan = plans.load(plan_path)
    plan["user"] = {}
    win._card("user_password", plan)
    (card,) = cards(win)
    labels = _labels(card)
    assert win._(conv.PASSWORD_ALONE) in labels
    assert not any(label.rstrip().endswith("for") for label in labels if label)


def test_the_greeting_puts_the_plan_where_it_is():
    """The plan panel is on the left; the greeting said «on the right»."""
    assert "on the left" in conv.INTRO
    assert "on the right" not in conv.INTRO
