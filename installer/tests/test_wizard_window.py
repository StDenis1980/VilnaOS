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
    from gi.repository import Adw, Gdk, GLib, Gtk
except (ImportError, ValueError):
    pytest.skip("GTK 4 or libadwaita is missing", allow_module_level=True)
if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) or not Gtk.init_check():
    pytest.skip("no display", allow_module_level=True)

from vilna_mcp_installer import plan as plans  # noqa: E402
from vilna_mcp_installer.machine import DemoMachine  # noqa: E402

from vilna_installer import a11y, chat, confirm, start, welcome, wizard_window  # noqa: E402
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


def _all(widget, kind):
    out = [widget] if isinstance(widget, kind) else []
    child = widget.get_first_child()
    while child is not None:
        out += _all(child, kind)
        child = child.get_next_sibling()
    return out


def test_tpm_with_a_pin_keeps_the_pin_in_the_vault_only(plan_path):
    win = window(plan_path)
    win.wizard.tpm = lambda: True
    win.go("encryption", strict=False)
    checks = [c for c in _all(win, Gtk.CheckButton) if c.get_parent() is not None]
    labels = [c.get_ancestor(Adw.ActionRow).get_title() for c in checks]
    pin = checks[labels.index("Зашифровано, з PIN під час кожного запуску")]
    pin.set_active(True)
    rows = [r for r in _all(win, Adw.PasswordEntryRow) if r.get_visible()]
    assert [r.get_title() for r in rows] == ["PIN запуску (від 6 до 20 цифр)", "Повторіть пароль"]
    rows[0].set_text("12ab")
    rows[1].set_text("12ab")
    win.step(+1)
    assert win.key == "encryption"
    assert win.error.get_label() == "PIN — це від 6 до 20 цифр, нічого іншого."
    rows[0].set_text("246810")
    rows[1].set_text("246810")
    win.step(+1)
    assert win.key != "encryption"
    enc = plans.load(plan_path)["disk"]["encryption"]
    assert enc == {"method": "tpm2-pin", "pin": "entered"}
    assert "246810" not in plan_path.read_text()
    assert win.secrets.take("pin") == "246810"


def _press(check, keyval):
    """What the key controller of a choice gets for an arrow key."""
    return a11y._arrow(None, keyval, 0, Gdk.ModifierType(0), check)


def test_the_arrows_reach_every_choice_after_one_is_made(plan_path):
    """With one way chosen, GTK left the others out of Tab and the arrows (item 34)."""
    win = window(plan_path)
    win.wizard.tpm = lambda: True
    win.go("encryption", strict=False)
    win.present()
    assert spin(lambda: win.get_mapped())
    checks = [c for c in _all(win, Gtk.CheckButton) if c.get_parent() is not None]
    assert all(c.get_accessible_role() == Gtk.AccessibleRole.RADIO for c in checks)
    first = next(c for c in checks if c.get_active())
    spin(lambda: False, 0.3)
    reached, here = [], first
    for _ in range(len(checks)):
        assert _press(here, Gdk.KEY_Down)
        here = win.get_focus()
        reached.append(here)
    assert set(reached) == set(checks)  # every way, and round again to the first
    assert here is first and first.get_active()  # moving chose nothing
    assert _press(first, Gdk.KEY_Up) and win.get_focus() is checks[-1]
    assert not _press(first, Gdk.KEY_Tab)  # Tab stays GTK's own


def test_tab_reaches_every_choice_after_one_is_made(plan_path):
    """In the live image (Build12.19) the arrows went past them to «Back»: Tab is the way left."""
    win = window(plan_path)
    win.wizard.tpm = lambda: True
    win.go("encryption", strict=False)
    win.present()
    assert spin(lambda: win.get_mapped())
    spin(lambda: False, 0.3)
    checks = [c for c in _all(win, Gtk.CheckButton) if c.get_mapped()]
    assert len(checks) == 4
    first = next(c for c in checks if c.get_active())
    first.grab_focus()
    reached = []
    for _ in range(12):
        win.emit("move-focus", Gtk.DirectionType.TAB_FORWARD)
        spin(lambda: False, 0.05)
        reached.append(win.get_focus())
    assert set(checks) - {first} <= set(reached)
    assert first.get_active()  # Tab chose nothing


def test_tab_goes_past_the_keyboard_layout_hint(plan_path):
    """Without a TPM the passphrase is chosen; Tab stopped at the hint under it (VM, Build12.26)."""
    win = window(plan_path)
    win.wizard.tpm = lambda: False
    win.go("encryption", strict=False)
    win.present()
    assert spin(lambda: win.get_mapped())
    spin(lambda: False, 0.3)
    checks = [c for c in _all(win, Gtk.CheckButton) if c.get_mapped()]
    chosen = next(c for c in checks if c.get_active())
    last = checks[-1]
    assert last is not chosen and last.is_sensitive()  # «Not encrypted», under the hint
    chosen.grab_focus()
    reached = []
    for _ in range(8):
        win.emit("move-focus", Gtk.DirectionType.TAB_FORWARD)
        spin(lambda: False, 0.05)
        reached.append(win.get_focus())
    assert last in reached
    last.grab_focus()
    win.emit("move-focus", Gtk.DirectionType.TAB_BACKWARD)
    spin(lambda: False, 0.05)
    assert isinstance(win.get_focus().get_ancestor(Adw.PasswordEntryRow), Adw.PasswordEntryRow)


def test_a_list_does_not_stop_at_the_hint():
    """libadwaita 1.5 put the hint under the list; newer ones keep it in, where Tab stopped."""
    win = Gtk.Window()
    rows = Gtk.ListBox()
    entry = Adw.PasswordEntryRow(title="Passphrase")
    check = a11y.radio(wizard_window.radio_group(), label="Not encrypted")
    after = Adw.ActionRow(title="Not encrypted")
    after.add_prefix(check)
    after.set_focusable(False)
    for row in (entry, wizard_window.hint_row(start.Texts("en"), entry), after):
        rows.append(row)
    win.set_child(rows)
    win.present()
    assert spin(lambda: check.get_mapped())
    entry.grab_focus()
    reached = []
    for _ in range(4):
        win.emit("move-focus", Gtk.DirectionType.TAB_FORWARD)
        spin(lambda: False, 0.05)
        reached.append(win.get_focus())
    assert check in reached
    check.grab_focus()
    win.emit("move-focus", Gtk.DirectionType.TAB_BACKWARD)
    spin(lambda: False, 0.05)
    assert win.get_focus().get_ancestor(Adw.PasswordEntryRow) is entry


def test_a_wifi_network_can_be_chosen_after_without_a_network(plan_path):
    win = window(plan_path)
    win.go("network", strict=False)
    win.present()
    radios = lambda: [c for c in _all(win, Gtk.CheckButton) if c.get_parent() is not None]  # noqa: E731
    assert spin(lambda: len(radios()) > 1)
    offline, *networks = radios()
    assert offline.get_active()
    networks[0].set_active(True)
    assert not offline.get_active()
    offline.set_active(True)
    assert not any(n.get_active() for n in networks)
    win.step(+1)
    assert plans.load(plan_path)["network"] == {"mode": "offline"}
