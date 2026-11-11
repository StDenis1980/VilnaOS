# SPDX-License-Identifier: GPL-3.0-or-later
"""The Herkos confirmation screen (GTK 4 + libadwaita), run as its own process.

    vilna-installer-confirm --plan PATH [--window] [--demo empty|windows]

What it checks and sets is in ``confirm``; this is the window. It reads one
JSON object on standard input (``{"recovery_key": …}`` when the installer
already showed a key, so going back and forth keeps the same key) and prints
one line on standard output (``confirm.answer``).

``ask`` is how the installer opens it: the conversation and the classic
wizard call it after «Check and confirm the plan»; the installer stays in
the same process, so the passwords in its ``Vault`` are still there for the
engine.

Known limit, as for every Herkos window (herkos/dialog.py): another program
of the same user could still drive the window through accessibility
(AT-SPI). In the live image no such program runs, and Gloxi has no tool for
it; the system Herkos closes this later (permissions.md 5.2).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from vilna_installer import confirm, encryption  # noqa: E402
from vilna_installer import conversation as conv  # noqa: E402
from vilna_installer.vault import Vault  # noqa: E402

APP_ID = "io.github.stdenis1980.VilnaInstaller.Confirm"

CSS = """
.vilna-key { font-family: monospace; font-size: 17pt; font-weight: 700;
    letter-spacing: 1px; padding: 14px 18px; border-radius: 12px;
    background-color: alpha(@accent_bg_color, 0.10); }
.vilna-loss { color: @error_color; }
.vilna-herkos { opacity: 0.75; }
"""

ICONS = {
    "erase": "user-trash-symbolic",
    "shrink": "go-down-symbolic",
    "use": "drive-harddisk-symbolic",
    "create": "list-add-symbolic",
    "manual": "drive-harddisk-symbolic",
    "keep": "emblem-ok-symbolic",
}


def _row(title: str, subtitle: str | None = None, icon: str | None = None) -> Adw.ActionRow:
    row = Adw.ActionRow(title=GLib.markup_escape_text(title), title_lines=0, subtitle_lines=0)
    if subtitle:
        row.set_subtitle(GLib.markup_escape_text(subtitle))
    if icon:
        row.add_prefix(Gtk.Image(icon_name=icon, accessible_role=Gtk.AccessibleRole.PRESENTATION))
    return row


class ConfirmWindow(Adw.ApplicationWindow):
    def __init__(
        self,
        app: Adw.Application,
        plan_path: Path,
        machine,
        *,
        key: str | None,
        stage: str,
        tpm: bool | None,
        finish: Callable[[confirm.Outcome], None],
    ) -> None:
        super().__init__(application=app)
        self.plan_path, self.machine, self.stage = plan_path, machine, stage
        self.finish = finish
        self.shown_at = time.monotonic()
        self.key_ok = False
        _ = confirm.texts_for(confirm.plans.load(plan_path))
        self._ = _
        checked = confirm.check(plan_path, machine, _, stage=stage, tpm=tpm)
        self.seen = checked.plan
        self.summary = checked.summary
        self.encrypted = confirm.encrypted(checked.plan)
        self.key = key if key and encryption.RECOVERY_RE.match(key) else encryption.recovery_key()
        self.set_title(f"{_(confirm.HERKOS)}: {_(confirm.TITLE)}")
        self.set_default_size(900, 820)
        self.connect("close-request", self._on_close)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        for side in ("top", "bottom"):
            getattr(page, f"set_margin_{side}")(24)
        for side in ("start", "end"):
            getattr(page, f"set_margin_{side}")(16)

        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        title = Gtk.Label(label=_(confirm.TITLE), xalign=0, wrap=True)
        title.add_css_class("title-1")
        title.set_accessible_role(Gtk.AccessibleRole.LABEL)
        head.append(title)
        head.append(Gtk.Label(label=_(confirm.LEAD), xalign=0, wrap=True))
        herkos = Gtk.Box(spacing=8)
        herkos.append(Gtk.Image(icon_name="security-high-symbolic"))
        note = Gtk.Label(label=_(confirm.FROM_HERKOS), xalign=0, wrap=True)
        note.add_css_class("caption")
        herkos.append(note)
        herkos.add_css_class("vilna-herkos")
        head.append(herkos)
        page.append(head)

        s = self.summary
        self.problems = Adw.PreferencesGroup(
            title=_(confirm.PROBLEMS_HEAD), description=_(confirm.PROBLEMS_LEAD)
        )
        for text in s.problems:
            self.problems.add(_row(text, icon="dialog-warning-symbolic"))
        self.problems.set_visible(bool(s.problems))
        page.append(self.problems)

        disks = Adw.PreferencesGroup(title=_(confirm.DISKS), description=s.disk or "")
        for change in s.changes:
            row = _row(change.text, icon=ICONS.get(change.action))
            if change.loses_data:
                row.add_css_class("vilna-loss")
            disks.add(row)
        disks.set_visible(bool(s.changes))
        page.append(disks)

        if s.warnings:
            warn = Adw.PreferencesGroup(title=_(confirm.WARNINGS_HEAD))
            for text in s.warnings:
                warn.add(_row(text, icon="dialog-information-symbolic"))
            page.append(warn)

        self.entry: Gtk.Entry | None = None
        if self.encrypted and s.ready:
            page.append(self._key_group())

        plan_group = Adw.PreferencesGroup(title=_(confirm.PLAN_HEAD))
        for line in s.lines:
            plan_group.add(_row(line.title, line.text or _(conv.NOT_DECIDED)))
        page.append(plan_group)

        self.understand: Gtk.CheckButton | None = None
        if s.erases and s.ready:
            label = (
                _(confirm.UNDERSTAND).format(disk=s.disk)
                if s.disk
                else _(confirm.UNDERSTAND_NO_DISK)
            )
            self.understand = Gtk.CheckButton(label=label)
            self.understand.connect("toggled", lambda *_a: self._update())
            page.append(self.understand)

        clamp = Adw.Clamp(maximum_size=760, child=page)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(clamp)

        self.back = Gtk.Button(label=_(confirm.BACK))
        self.back.add_css_class("pill")
        self.back.connect("clicked", lambda *_a: self._go_back())
        self.install = Gtk.Button(
            label=_(confirm.INSTALL if s.erases else confirm.INSTALL_KEEP), sensitive=False
        )
        self.install.add_css_class("pill")
        self.install.add_css_class("destructive-action" if s.erases else "suggested-action")
        self.install.connect("clicked", self._on_install)
        self.install.set_visible(s.ready)
        self.waiting = Gtk.Label(label=_(confirm.WAIT), visible=s.ready)
        self.waiting.add_css_class("dim-label")
        bar = Gtk.Box(spacing=12, halign=Gtk.Align.END)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{side}")(14)
        bar.append(self.waiting)
        bar.append(self.back)
        bar.append(self.install)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=_(confirm.TITLE), subtitle=_(confirm.HERKOS)))
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(scroll)
        view.add_bottom_bar(bar)
        self.set_content(view)
        self.set_default_widget(self.back)
        GLib.timeout_add(confirm.ARM_SECONDS * 1000, self._arm)
        GLib.idle_add(self._focus_first)

    def _key_group(self) -> Gtk.Widget:
        _ = self._
        group = Adw.PreferencesGroup(title=_(confirm.KEY_HEAD), description=_(confirm.KEY_LEAD))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        # Two lines of four groups: readable and easy to copy by hand.
        parts = encryption.groups(self.key)
        shown = "  ".join(parts[:4]) + "\n" + "  ".join(parts[4:])
        key = Gtk.Label(label=shown, xalign=0, halign=Gtk.Align.START)
        key.add_css_class("vilna-key")
        key.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_(confirm.KEY_SPOKEN).format(key=encryption.spoken(self.key))],
        )
        box.append(key)
        box.append(Gtk.Label(label=_(confirm.KEY_TYPE), xalign=0, wrap=True))
        self.entry = Gtk.Entry(max_length=24, width_chars=14, halign=Gtk.Align.START)
        self.entry.add_css_class("monospace")
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], [_(confirm.KEY_FIELD)])
        self.entry.connect("changed", self._on_key)
        box.append(self.entry)
        self.key_note = Gtk.Label(xalign=0, wrap=True)
        box.append(self.key_note)
        group.add(box)
        return group

    def _focus_first(self) -> bool:
        (self.entry or self.back).grab_focus()
        return False

    def _arm(self) -> bool:
        self.waiting.set_visible(False)
        self._update()
        return False

    def _armed(self) -> bool:
        return time.monotonic() - self.shown_at >= confirm.ARM_SECONDS

    def _on_key(self, entry: Gtk.Entry) -> None:
        _ = self._
        typed = entry.get_text()
        self.key_ok = encryption.check_group(self.key, typed, confirm.CHECK_GROUP)
        if self.key_ok:
            self.key_note.set_label(_(confirm.KEY_OK))
        elif len(encryption.normalize(typed)) >= encryption.GROUP:
            self.key_note.set_label(_(confirm.KEY_WRONG))
        else:
            self.key_note.set_label("")
        self.key_note.update_property(
            [Gtk.AccessibleProperty.DESCRIPTION], [self.key_note.get_label()]
        )
        self._update()

    def ready(self) -> bool:
        if not self.summary.ready or not self._armed():
            return False
        if self.encrypted and not self.key_ok:
            return False
        return not (self.understand is not None and not self.understand.get_active())

    def _update(self) -> None:
        self.install.set_sensitive(self.ready())

    def _on_install(self, *_args) -> None:
        if not self.ready():
            return
        try:
            digest = confirm.confirm(
                self.plan_path,
                self.machine,
                seen=self.seen,
                key_typed_back=self.key_ok,
                stage=self.stage,
            )
        except (confirm.NotConfirmed, confirm.plans.PlanError, confirm.machines.MachineError):
            self.problems.add(_row(self._(confirm.CHANGED), icon="dialog-warning-symbolic"))
            self.problems.set_visible(True)
            self.install.set_sensitive(False)
            self.install.set_visible(False)
            return
        key = self.key if self.encrypted else None
        self.finish(confirm.Outcome(True, digest, key))

    def _go_back(self) -> None:
        # The key goes back too: shown once, it stays the same if the person returns.
        self.finish(confirm.Outcome(False, recovery_key=self.key if self.encrypted else None))

    def _on_close(self, *_args) -> bool:
        self._go_back()
        return False


class ConfirmApp(Adw.Application):
    def __init__(self, args: argparse.Namespace, given: dict[str, Any]) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.args, self.given = args, given
        self.outcome = confirm.Outcome(False)
        self.done = False

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def finish(self, outcome: confirm.Outcome) -> None:
        if self.done:
            return
        self.done = True
        self.outcome = outcome
        self.quit()

    def do_activate(self) -> None:
        win = self.get_active_window()
        if win is None:
            tpm = True if self.args.demo else encryption.tpm_can_seal()
            win = ConfirmWindow(
                self,
                self.args.plan,
                confirm.machine_for(self.args.demo),
                key=self.given.get("recovery_key"),
                stage=self.args.stage,
                tpm=tpm,
                finish=self.finish,
            )
        if not self.args.window:
            win.fullscreen()
        win.present()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vilna-installer-confirm")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--window", action="store_true", help="a window, not the whole screen")
    p.add_argument("--demo", choices=("empty", "windows"), help="a synthetic computer")
    p.add_argument("--stage", choices=("alpha", "beta"), default="alpha")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    given: dict[str, Any] = {}
    if not sys.stdin.isatty():
        try:
            data = json.loads(sys.stdin.read() or "{}")
            given = data if isinstance(data, dict) else {}
        except ValueError:
            given = {}
    app = ConfirmApp(args, given)
    GLib.set_application_name("Herkos")
    app.run([sys.argv[0]])
    print(confirm.answer(app.outcome), flush=True)
    return 0 if app.outcome.confirmed else 1


# --- from the installer ---------------------------------------------------------------


def ask(
    parent: Gtk.Window,
    plan_path: Path,
    vault: Vault,
    on_done: Callable[[confirm.Outcome], None],
    *,
    demo: str | None = None,
    window: bool = False,
    stage: str = "alpha",
) -> None:
    """Open the Herkos screen and call ``on_done`` with its answer; the parent waits.

    A confirmed answer puts the recovery key into ``vault`` for the engine.
    """
    argv = confirm.command(plan_path, demo=demo, window=window, stage=stage)
    parent.set_sensitive(False)
    try:
        proc = Gio.Subprocess.new(
            argv, Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE
        )
    except GLib.Error as e:
        print(f"vilna-installer: the Herkos screen did not start: {e.message}", file=sys.stderr)
        parent.set_sensitive(True)
        on_done(confirm.Outcome(False))
        return
    given = {}
    if vault.peek("recovery_key"):
        given["recovery_key"] = vault.peek("recovery_key")

    def done(p: Gio.Subprocess, result: Gio.AsyncResult) -> None:
        try:
            _ok, out, _err = p.communicate_utf8_finish(result)
        except GLib.Error:
            out = ""
        outcome = confirm.parse(out or "")
        if outcome.recovery_key:
            vault.put("recovery_key", outcome.recovery_key)
        parent.set_sensitive(True)
        on_done(outcome)

    proc.communicate_utf8_async(json.dumps(given), None, done)


if __name__ == "__main__":
    sys.exit(main())
