# SPDX-License-Identifier: GPL-3.0-or-later
"""The classic wizard on a display: steps, errors, the partition editor, «Without the assistant».

Needs GTK 4, libadwaita and a display (Xvfb is enough); skipped without them.
"""

import os
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
from vilna_mcp_installer.machine import DemoMachine  # noqa: E402

from vilna_installer import chat, start, welcome, wizard_window  # noqa: E402
from vilna_installer import wizard as wiz  # noqa: E402

Adw.init()


def spin(until, seconds=5.0):
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.01)
    return until()


@pytest.fixture
def plan_path(tmp_path):
    path = tmp_path / "vilna-installer" / "plan.json"
    start.write_plan(start.start_plan("uk", False, False), path)
    return path


def window(plan_path, kind="empty"):
    app = Adw.Application()
    app.register(None)
    return wizard_window.WizardWindow(app, plan_path, DemoMachine(kind), demo=True)


def test_it_opens_at_the_first_undecided_step(plan_path):
    win = window(plan_path)
    assert win.key == "languages"
    assert win.title.get_label() == "Мови й клавіатура"
    assert not win.back.get_sensitive()


def test_next_saves_the_step_and_an_error_keeps_the_person_there(plan_path):
    win = window(plan_path)
    win.step(+1)  # the UI language is chosen already
    assert win.key == "network"
    assert plans.load(plan_path)["locale"]["keyboards"] == ["ua", "us"]
    win.step(+1)  # «Without a network» is chosen by default
    assert win.key == "disk"
    assert plans.load(plan_path)["network"] == {"mode": "offline"}
    win.step(+1)  # the only disk is chosen, but not what to do with it
    assert win.key == "disk"
    assert win.error.get_visible()
    assert win.error.get_label() == "Виберіть один із варіантів."
    win.step(-1)
    assert win.key == "network" and not win.error.get_visible()


def test_the_summary_sends_the_person_back_to_what_is_missing(plan_path):
    win = window(plan_path)
    win.go("summary", strict=False)
    assert not win.next.get_sensitive()
    assert win.next.get_label() == "Перевірити й підтвердити план"


def test_the_partition_editor_saves_a_manual_layout(plan_path):
    win = window(plan_path, "windows")
    w = win.wizard
    w.set_disk(w.disks()[0]["id"], "manual")
    saved = []
    editor = wizard_window.PartitionEditor(w, win._, lambda: saved.append(True))
    editor.present(win)
    editor._on_save(None)  # nothing chosen yet
    assert editor.error.get_visible() and not saved
    editor._on_suggest(None)
    editor._on_save(None)
    assert saved == [True]
    parts = plans.load(plan_path)["disk"]["partitions"]
    assert parts[0] == {"use": "efi", "partition": "p-efi", "format": False}


def test_text_from_the_disk_is_never_markup():
    row = wizard_window.plain_row(title="<b>SYSTEM</b> & co")
    assert not row.get_use_markup()


def test_without_the_assistant_opens_the_wizard_in_the_same_program(plan_path):
    app = welcome.InstallerApp(plan_path, True, "uk", demo="empty")
    app.register(None)
    first = welcome.Welcome(app, plan_path, "uk")
    first._finish(assistant=False)
    wins = [w for w in app.get_windows() if isinstance(w, wizard_window.WizardWindow)]
    assert len(wins) == 1
    assert plans.load(plan_path)["assistant"] is False
    assert app.exit_code == 1  # nothing confirmed yet; no more «status 3»


def test_the_chat_hands_its_secrets_to_the_wizard(plan_path):
    app = welcome.InstallerApp(plan_path, True, "uk", demo="empty")
    app.register(None)
    talk = chat.ChatWindow(app, None, plan_path, demo=True, demo_kind="empty")
    talk.secrets.put("network", "demo-password")
    talk._without()
    wins = [w for w in app.get_windows() if isinstance(w, wizard_window.WizardWindow)]
    assert wins and wins[0].secrets.take("network") == "demo-password"


def test_every_step_shows(plan_path):
    win = window(plan_path, "windows")
    for key in wiz.STEPS:
        win.go(key, strict=False)
        assert win.key == key
        assert win.holder.get_first_child() is not None
