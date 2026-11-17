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

from vilna_installer import chat, confirm, start, welcome, wizard_window  # noqa: E402
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
    return wizard_window.WizardWindow(app, plan_path, DemoMachine(kind), demo=kind)


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


def fill(win):
    w = win.wizard
    w.set_languages(["uk_UA.UTF-8"], None)
    w.set_network("offline")
    w.set_disk(w.disks()[0]["id"], "replace")
    w.set_encryption("tpm2")
    w.set_user("Олена Демо", "olena", "olena-vilna")
    w.mark_entered("user")
    w.set_time(*w.suggested_time())
    assert w.set_crash_reports("ask") is None
    assert w.set_vpn("skip") is None


def test_the_wizard_ends_on_the_herkos_screen(plan_path, monkeypatch):
    asked = []

    def ask(parent, path, vault, on_done, **kw):
        asked.append((path, vault, kw))
        on_done(confirm.Outcome(False))

    monkeypatch.setattr(wizard_window.confirm_window, "ask", ask)
    win = window(plan_path)
    app = win.get_application()
    app.exit_code = 1
    fill(win)
    win._finish()
    assert len(asked) == 1
    path, vault, kw = asked[0]
    assert path == plan_path and vault is win.secrets
    assert kw["demo"] == "empty" and kw["stage"] == "alpha"
    # Not confirmed: the person is back on the summary, nothing ends.
    assert win.key == "summary" and app.exit_code == 1
    # Confirmed: step 13 starts in this same process, with the same vault.
    started = []
    monkeypatch.setattr(
        wizard_window.install_window, "start", lambda *a, **kw: started.append((a, kw))
    )
    win._confirm_done(confirm.Outcome(True, "abc", "1234-5678"))
    ((args, kw),) = started
    assert args[1] == plan_path and args[3] is win.secrets and kw["demo"] is True
    assert app.exit_code == 1  # still installing
    kw["on_end"](True)
    assert app.exit_code == wizard_window.EXIT_CONFIRM


def test_crash_reports_have_no_answer_until_the_person_gives_one(plan_path):
    """ADR 0018 section 3: three equal answers, none chosen in advance."""
    win = window(plan_path)
    win.go("privacy", strict=False)
    assert set(win.crash_checks) == {"auto", "ask", "never"}
    assert not any(check.get_active() for check in win.crash_checks.values())
    win.step(+1)
    assert win.key == "privacy"
    assert win.error.get_label() == "Оберіть відповідь про звіти про збої."
    assert "crash_reports" not in plans.load(plan_path)["privacy"]
    win.crash_checks["never"].set_active(True)
    win.step(+1)
    assert win.key == "vpn"
    assert plans.load(plan_path)["privacy"]["crash_reports"] == "never"
    assert win.wizard.done("privacy")
    # Back on the step, the answer given is shown.
    win.go("privacy", strict=False)
    assert win.crash_checks["never"].get_active()


def test_the_vpn_step_has_no_choice_until_the_person_makes_one(plan_path):
    """ADR 0027 section 2: Proton, a free Proton account, Mullvad or skip; none in advance."""
    win = window(plan_path)
    win.go("vpn", strict=False)
    assert win.title.get_label() == "VPN"
    assert list(win.vpn_checks) == ["proton", "proton_signup", "mullvad", "skip"]
    assert not any(check.get_active() for check in win.vpn_checks.values())
    win.step(+1)
    assert win.key == "vpn"
    assert win.error.get_label() == "Оберіть відповідь про VPN."
    assert "vpn" not in plans.load(plan_path)["privacy"]
    # One keyboard stop per choice, on the radio button (Orca, VM test 17.11).
    for check in win.vpn_checks.values():
        assert (
            check.get_focusable()
            and not check.get_parent().get_ancestor(Adw.ActionRow).get_focusable()
        )
    win.vpn_checks["mullvad"].set_active(True)
    win.step(+1)
    assert win.key == "summary"
    assert plans.load(plan_path)["privacy"]["vpn"] == "mullvad"
    win.go("vpn", strict=False)
    assert win.vpn_checks["mullvad"].get_active()
