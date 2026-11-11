# SPDX-License-Identifier: GPL-3.0-or-later
"""The Herkos screen on a display: nothing is confirmed until the person did all of it.

Needs GTK 4, libadwaita and a display (Xvfb is enough); skipped without them.
"""

import json
import os
import subprocess
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

from test_confirm import full_plan  # noqa: E402
from vilna_mcp_installer import machine as machines  # noqa: E402
from vilna_mcp_installer import plan as plans  # noqa: E402

from vilna_installer import confirm, confirm_window, encryption  # noqa: E402
from vilna_installer.vault import Vault  # noqa: E402

Adw.init()


def spin(until, seconds=5.0):
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.01)
    return until()


def screen(path, kind="windows", tpm=True):
    app = Adw.Application()
    app.register(None)
    got = []
    key = encryption.recovery_key()
    win = confirm_window.ConfirmWindow(
        app,
        path,
        machines.DemoMachine(kind),
        key=key,
        stage="alpha",
        tpm=tpm,
        finish=got.append,
    )
    return win, got, key


def test_the_install_button_waits_for_the_key_the_tick_and_the_time(tmp_path):
    path = tmp_path / "plan.json"
    full_plan(path)
    win, got, key = screen(path)
    assert win.install.get_visible() and not win.install.get_sensitive()
    assert win.get_default_widget() is win.back  # Enter means «back»
    win.entry.set_text(encryption.groups(key)[7])
    win.understand.set_active(True)
    assert not win.install.get_sensitive()  # not before ARM_SECONDS
    assert spin(win.install.get_sensitive, confirm.ARM_SECONDS + 2)
    win.understand.set_active(False)
    assert not win.install.get_sensitive()
    win.understand.set_active(True)
    win.entry.set_text(encryption.groups(key)[6])
    assert not win.install.get_sensitive()
    assert win.key_note.get_label() == win._(confirm.KEY_WRONG)
    win.entry.set_text(encryption.groups(key)[7].upper())
    win.install.emit("clicked")
    assert got == [confirm.Outcome(True, confirm.sha256(path), key)]
    plan = plans.load(path)
    assert plan["confirmed_by_user"] is True
    assert plan["disk"]["encryption"]["recovery_key_acknowledged"] is True


def test_a_plan_that_cannot_be_installed_shows_only_back(tmp_path):
    path = tmp_path / "plan.json"
    full_plan(path)
    win, got, key = screen(path, tpm=False)
    assert win.problems.get_visible()
    assert not win.install.get_visible() and win.entry is None and win.understand is None
    win.back.emit("clicked")
    assert got == [confirm.Outcome(False, recovery_key=key)]
    assert plans.load(path)["confirmed_by_user"] is False


def test_the_key_is_read_letter_by_letter():
    key = encryption.recovery_key()
    spoken = encryption.spoken(key)
    assert len(spoken.split(". ")) == 8 and spoken.count(" ") == 8 * 7 + 7


def test_the_installer_gets_the_answer_and_keeps_the_key(tmp_path):
    """The screen as its own process: closed at once, it answers «back» with its key."""
    path = tmp_path / "plan.json"
    full_plan(path)
    vault = Vault()
    vault.put("recovery_key", encryption.recovery_key())
    argv = confirm.command(path, demo="windows", window=True)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
        text=True,
    )  # fmt: skip
    proc.stdin.write(json.dumps({"recovery_key": vault.peek("recovery_key")}))
    proc.stdin.close()
    time.sleep(3)
    assert proc.poll() is None  # the window is up, waiting for the person
    proc.terminate()  # like closing the window: the answer must still be «back»
    out = proc.stdout.read()
    proc.wait(10)
    outcome = confirm.parse(out)
    assert not outcome.confirmed
    assert plans.load(path)["confirmed_by_user"] is False
