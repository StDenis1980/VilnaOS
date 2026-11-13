# SPDX-License-Identifier: GPL-3.0-or-later
"""The Backups window (GTK 4 + libadwaita): turn backups on, see them, restore.

    vilna-backup-gui

The window is the person's own tool, as the terminal is: it may do what
Gloxi cannot (R3 in design.md 5): set up the place and the keys, connect a
new device to old backups with the recovery key, restore over existing files
after showing which ones change, and keep system secrets in backups.

Every call to :class:`Backup` that may be slow (restic, sizes) runs in a
thread; results come back to GTK through ``GLib.idle_add``.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from vilna_backup import view  # noqa: E402
from vilna_backup.i18n import _  # noqa: E402
from vilna_backup.restic import ResticError  # noqa: E402
from vilna_backup.service import Backup, BackupError  # noqa: E402

APP_ID = "io.github.stdenis1980.VilnaBackup"
POLL_SECONDS = 2
EXPECT_RUN_SECONDS = 20

CSS = """
.vilna-key {
  font-family: monospace;
  font-size: 20pt;
  font-weight: bold;
  letter-spacing: 2px;
  padding: 18px 24px;
  border-radius: 12px;
  background-color: alpha(@accent_bg_color, 0.08);
  border: 1px dashed alpha(@accent_bg_color, 0.5);
}
.vilna-safe { color: @success_color; font-weight: bold; }
"""


def _in_thread(work: Callable[[], Any], done: Callable[[Any, Exception | None], None]) -> None:
    def run() -> None:
        try:
            result, error = work(), None
        except (BackupError, ResticError, OSError, ValueError) as e:
            result, error = None, e
        GLib.idle_add(lambda: done(result, error) and False)

    threading.Thread(target=run, daemon=True).start()


def _radio_row(title: str, subtitle: str, group: Gtk.CheckButton | None) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
    if group is not None:
        check.set_group(group)
    row.add_prefix(check)
    row.set_activatable_widget(check)
    row.check = check  # type: ignore[attr-defined]
    return row


class PlaceGroup(Adw.PreferencesGroup):
    """Where the backups are: a folder on an external disk or one's own server."""

    def __init__(self, win: Gtk.Window, title: str, *, cloud: bool) -> None:
        super().__init__(title=title)
        self.win = win
        self.folder = ""
        leader = None
        if cloud:
            row = _radio_row(_("Vilna cloud"), _("In the EU · coming later"), None)
            row.set_sensitive(False)
            self.add(row)
            leader = row.check
        self.disk = _radio_row(_("External disk"), _("No folder chosen yet"), leader)
        leader = leader or self.disk.check
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._choose_folder)
        self.disk.add_suffix(choose)
        self.add(self.disk)
        self.server = _radio_row(_("Your own server"), _("SFTP, S3 or REST"), leader)
        self.add(self.server)
        self.address = Adw.EntryRow(title=_("Address, e.g. sftp:me@host:/backups"))
        self.login = Adw.EntryRow(title=_("Login (S3 and REST)"))
        self.password = Adw.PasswordEntryRow(title=_("Password (S3 and REST)"))
        for row in (self.address, self.login, self.password):
            self.add(row)
        self.disk.check.set_active(True)
        self.server.check.connect("toggled", lambda *_a: self._sync_server())
        self._sync_server()

    def _sync_server(self) -> None:
        on = self.server.check.get_active()
        for row in (self.address, self.login, self.password):
            row.set_visible(on)

    def _choose_folder(self, *_a: Any) -> None:
        dialog = Gtk.FileDialog(title=_("Folder for backups on the external disk"))
        media = Path("/run/media") / os.environ.get("USER", "")
        if media.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(media)))

        def picked(d: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                f = d.select_folder_finish(result)
            except GLib.Error:
                return
            if f and f.get_path():
                self.set_folder(f.get_path())

        dialog.select_folder(self.win, None, picked)

    def set_folder(self, folder: str) -> None:
        self.folder = folder
        self.disk.set_subtitle(folder)
        self.disk.check.set_active(True)

    def destination(self) -> tuple[Any, dict[str, str]]:
        """The place and its login, or :class:`view.FormError` with what to fix."""
        kind = "server" if self.server.check.get_active() else "disk"
        return view.form_destination(
            kind,
            folder=self.folder,
            address=self.address.get_text(),
            login=self.login.get_text(),
            password=self.password.get_text(),
        )

    def forget_password(self) -> None:
        self.password.set_text("")


class SetupPage(Gtk.Box):
    """What to keep, where, how often (design 10.16)."""

    def __init__(self, win: BackupWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        page = Adw.PreferencesPage(vexpand=True)
        self.append(page)

        what = Adw.PreferencesGroup(title=_("What to keep in backups?"))
        page.add(what)
        self.presets: dict[str, Adw.ActionRow] = {}
        first = None
        for preset in view.WINDOW_PRESETS:
            row = _radio_row(view.preset_title(preset), view.preset_subtitle(preset, None), first)
            first = first or row.check
            self.presets[preset] = row
            what.add(row)

        self.place = PlaceGroup(win, _("Where"), cloud=True)
        page.add(self.place)

        often = Adw.PreferencesGroup(title=_("How often"))
        page.add(often)
        self.schedule = Adw.ComboRow(
            title=_("Back up"),
            model=Gtk.StringList.new([view.schedule_title(s) for s in view.SCHEDULE_ORDER]),
        )
        often.add(self.schedule)

        before = Adw.PreferencesGroup(title=_("Already have backups?"))
        page.add(before)
        recover = Adw.ActionRow(
            title=_("Restore from backups made before"),
            subtitle=_("On a new computer, with your recovery sheet"),
        )
        recover_button = Gtk.Button(label=_("Restore…"), valign=Gtk.Align.CENTER)
        recover_button.connect("clicked", lambda *_a: win.show_recover())
        recover.add_suffix(recover_button)
        recover.set_activatable_widget(recover_button)
        before.add(recover)

        note = Gtk.Label(
            label=_(
                "Backups are encrypted on this computer. "
                "Without the key nobody can read them, not even us."
            ),
            wrap=True,
            xalign=0,
            margin_top=12,
            margin_start=24,
            margin_end=24,
        )
        note.add_css_class("vilna-safe")

        self.append(note)
        self.error = Gtk.Label(wrap=True, xalign=0, visible=False, margin_start=24, margin_end=24)
        self.error.add_css_class("error")
        self.append(self.error)
        buttons = Gtk.Box(
            spacing=12,
            halign=Gtk.Align.END,
            margin_top=12,
            margin_bottom=18,
            margin_start=24,
            margin_end=24,
        )
        later = Gtk.Button(label=_("Later"))
        later.connect("clicked", lambda *_a: win.close())
        self.next = Gtk.Button(label=_("Next"))
        self.next.add_css_class("suggested-action")
        self.next.connect("clicked", self._next)
        buttons.append(later)
        buttons.append(self.next)
        self.append(buttons)

    def load(self, settings_preset: str, schedule: str) -> None:
        preset = settings_preset if settings_preset in self.presets else "documents"
        self.presets[preset].check.set_active(True)
        if schedule in view.SCHEDULE_ORDER:
            self.schedule.set_selected(view.SCHEDULE_ORDER.index(schedule))
        _in_thread(self.win.backup.overview, self._sizes)

    def _sizes(self, data: Any, error: Exception | None) -> None:
        if error or not data:
            return
        sizes = view.preset_sizes(data.get("groups", []))
        for preset, row in self.presets.items():
            row.set_subtitle(view.preset_subtitle(preset, sizes))

    def chosen_preset(self) -> str:
        return next(p for p, row in self.presets.items() if row.check.get_active())

    def _next(self, *_a: Any) -> None:
        try:
            dest, creds = self.place.destination()
        except view.FormError as e:
            self.show_error(str(e))
            return
        self.show_error("")
        preset = self.chosen_preset()
        schedule = view.SCHEDULE_ORDER[self.schedule.get_selected()]
        self.next.set_sensitive(False)
        backup = self.win.backup

        def work() -> str:
            backup.set_paths(preset=preset, by_assistant=False)
            s = backup.settings
            backup.save(s.with_changes(schedule=schedule))
            return backup.set_up(dest, creds)

        def done(key: Any, error: Exception | None) -> None:
            self.next.set_sensitive(True)
            if error:
                self.show_error(_("Could not set up the backups: {why}").format(why=error))
                return
            self.place.forget_password()
            self.win.show_sheet(key)

        _in_thread(work, done)

    def show_error(self, text: str) -> None:
        self.error.set_label(text)
        self.error.set_visible(bool(text))


class RecoverPage(Gtk.Box):
    """A new computer: open the old backups with the key from the recovery sheet."""

    def __init__(self, win: BackupWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        page = Adw.PreferencesPage(vexpand=True)
        self.append(page)
        intro = Adw.PreferencesGroup(
            description=_(
                "Connect this computer to the backups you made before. "
                "You need the place of the backups and the key from your recovery sheet. "
                "Nothing on this computer changes until you choose what to bring back."
            )
        )
        page.add(intro)
        self.place = PlaceGroup(win, _("Where are your backups?"), cloud=False)
        page.add(self.place)
        key = Adw.PreferencesGroup(title=_("Recovery sheet"))
        page.add(key)
        self.entry = Adw.EntryRow(title=_("Type the key from your sheet"))
        self.entry.connect("changed", lambda *_a: self._sync())
        self.entry.connect("entry-activated", self._connect)
        key.add(self.entry)

        self.error = Gtk.Label(wrap=True, xalign=0, visible=False, margin_start=24, margin_end=24)
        self.error.add_css_class("error")
        self.append(self.error)
        buttons = Gtk.Box(
            spacing=12,
            halign=Gtk.Align.END,
            margin_top=12,
            margin_bottom=18,
            margin_start=24,
            margin_end=24,
        )
        back = Gtk.Button(label=_("Back"))
        back.connect("clicked", lambda *_a: win.show_setup())
        self.ok = Gtk.Button(label=_("Connect"))
        self.ok.add_css_class("suggested-action")
        self.ok.connect("clicked", self._connect)
        buttons.append(back)
        buttons.append(self.ok)
        self.append(buttons)

    def show(self) -> None:
        self.entry.set_text("")
        self.error.set_visible(False)
        self._sync()

    def _sync(self) -> None:
        self.ok.set_sensitive(view.key_looks_complete(self.entry.get_text()))

    def _connect(self, *_a: Any) -> None:
        if not self.ok.get_sensitive():
            return
        try:
            dest, creds = self.place.destination()
        except view.FormError as e:
            self._error(str(e))
            return
        self._error("")
        typed = self.entry.get_text()
        backup = self.win.backup
        self.ok.set_sensitive(False)

        def done(_result: Any, error: Exception | None) -> None:
            self._sync()
            if error:
                self._error(_("Could not open the backups: {why}").format(why=error))
                return
            self.entry.set_text("")
            self.place.forget_password()
            self.win.after_recover()

        _in_thread(lambda: backup.recover(dest, typed, creds), done)

    def _error(self, text: str) -> None:
        self.error.set_label(text)
        self.error.set_visible(bool(text))


class SheetPage(Gtk.Box):
    """The recovery sheet: shown once, typed back before backups start (design.md 3a)."""

    def __init__(self, win: BackupWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        clamp = Adw.Clamp(maximum_size=520, vexpand=True)
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            margin_top=24,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
            valign=Gtk.Align.CENTER,
        )
        clamp.set_child(box)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(clamp)
        self.append(scroll)

        title = Gtk.Label(label=_("Recovery sheet"), xalign=0)
        title.add_css_class("title-1")
        box.append(title)
        self.intro = Gtk.Label(wrap=True, xalign=0)
        box.append(self.intro)
        self.key = Gtk.Label(selectable=False, justify=Gtk.Justification.CENTER)
        self.key.add_css_class("vilna-key")
        box.append(self.key)
        self.hint = Gtk.Label(wrap=True, xalign=0)
        self.hint.add_css_class("dim-label")
        box.append(self.hint)

        group = Adw.PreferencesGroup(margin_top=6)
        self.entry = Adw.EntryRow(title=_("Type the key from your sheet"))
        self.entry.connect("changed", lambda *_a: self._sync())
        self.entry.connect("entry-activated", self._confirm)
        group.add(self.entry)
        box.append(group)
        self.error = Gtk.Label(wrap=True, xalign=0, visible=False)
        self.error.add_css_class("error")
        box.append(self.error)

        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.END)
        self.again = Gtk.Button(label=_("Start again with another place"))
        self.again.add_css_class("flat")
        self.again.connect("clicked", lambda *_a: win.show_setup())
        self.ok = Gtk.Button(label=_("Confirm and turn on"))
        self.ok.add_css_class("suggested-action")
        self.ok.connect("clicked", self._confirm)
        buttons.append(self.again)
        buttons.append(self.ok)
        box.append(buttons)

    def show(self, recovery_key: str | None) -> None:
        self.entry.set_text("")
        self.error.set_visible(False)
        if recovery_key:
            self.intro.set_label(
                _(
                    "Write this key down on paper and keep it away from the computer. "
                    "If this computer is lost, only this key opens your backups. "
                    "We do not know it and cannot send it again."
                )
            )
            self.key.set_label(view.key_for_reading(recovery_key))
            self.key.set_visible(True)
            self.hint.set_label(
                _("The key is shown only now. The letters I, L, O and U are never used.")
            )
            self.again.set_visible(False)
        else:
            self.intro.set_label(
                _(
                    "Backups stay off until you type the recovery key back from your sheet. "
                    "It was shown once, when the place was set up."
                )
            )
            self.key.set_visible(False)
            self.hint.set_label(
                _("No sheet? Start again with another place: a new key will be made.")
            )
            self.again.set_visible(True)
        self._sync()

    def _sync(self) -> None:
        self.ok.set_sensitive(view.key_looks_complete(self.entry.get_text()))

    def _confirm(self, *_a: Any) -> None:
        if not self.ok.get_sensitive():
            return
        typed = self.entry.get_text()
        backup = self.win.backup
        self.ok.set_sensitive(False)

        def work() -> dict:
            backup.confirm_recovery(typed)
            backup.set_schedule(backup.settings.schedule)
            return backup.start()

        def done(_result: Any, error: Exception | None) -> None:
            self._sync()
            if error:
                self.error.set_label(_("This is not the key: {why}").format(why=error))
                self.error.set_visible(True)
                return
            self.entry.set_text("")
            self.win.expect_run()
            self.win.show_status(_("Backups are on. The first backup has started."))

        _in_thread(work, done)


class StatusPage(Gtk.Box):
    """Backups are on: state, back up now, settings, the list of backups."""

    def __init__(self, win: BackupWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self._loading = False
        self.enabled = False
        page = Adw.PreferencesPage(vexpand=True)
        self.append(page)

        state = Adw.PreferencesGroup()
        page.add(state)
        self.state = Adw.ActionRow()
        self.icon = Gtk.Image(icon_size=Gtk.IconSize.LARGE)
        self.state.add_prefix(self.icon)
        self.now = Gtk.Button(label=_("Back up now"), valign=Gtk.Align.CENTER)
        self.now.add_css_class("suggested-action")
        self.now.add_css_class("pill")
        self.now.connect("clicked", self._back_up_now)
        self.state.add_suffix(self.now)
        state.add(self.state)
        self.progress = Gtk.ProgressBar(visible=False, margin_top=10)
        state.add(self.progress)

        settings = Adw.PreferencesGroup(title=_("Settings"))
        page.add(settings)
        self.preset = Adw.ComboRow(
            title=_("What to keep"),
            use_subtitle=True,
            model=Gtk.StringList.new([view.preset_title(p) for p in view.WINDOW_PRESETS]),
        )
        self.preset.connect("notify::selected", self._preset_changed)
        settings.add(self.preset)
        self.schedule = Adw.ComboRow(
            title=_("How often"),
            model=Gtk.StringList.new([view.schedule_title(s) for s in view.SCHEDULE_ORDER]),
        )
        self.schedule.connect("notify::selected", self._schedule_changed)
        settings.add(self.schedule)
        self.secrets = Adw.SwitchRow(
            title=_("System secrets"),
            subtitle=_("Saved passwords, online accounts, SSH and GPG keys"),
        )
        self.secrets.connect("notify::active", self._secrets_changed)
        settings.add(self.secrets)
        self.place = Adw.ActionRow(title=_("Where"))
        settings.add(self.place)

        self.list = Adw.PreferencesGroup(
            title=_("Backups"),
            description=_(
                "Restore into a new folder, or over your files after seeing which ones change."
            ),
        )
        page.add(self.list)
        self.rows: list[Gtk.Widget] = []

        safe = Adw.PreferencesGroup()
        note = Gtk.Label(
            label=_(
                "Encrypted on this computer. Without the key nobody can read them, not even us."
            ),
            wrap=True,
            xalign=0,
        )
        note.add_css_class("vilna-safe")
        safe.add(note)
        page.add(safe)

    # -- state -------------------------------------------------------------

    def refresh(self) -> None:
        _in_thread(self.win.backup.status, self._got_status)
        _in_thread(self.win.backup.snapshots, self._got_snapshots)

    def _got_status(self, status: Any, error: Exception | None) -> None:
        if error or not status:
            self.state.set_title(_("Cannot read the state of backups"))
            self.state.set_subtitle(str(error or ""))
            return
        s = view.summary(status)
        self.state.set_title(s.title)
        self.state.set_subtitle(s.detail)
        self.icon.set_from_icon_name("emblem-ok-symbolic" if s.ok else "dialog-warning-symbolic")
        self.enabled = bool(status.get("enabled"))
        self.now.set_label(_("Back up now") if self.enabled else _("Turn on"))
        self.now.set_sensitive(s.percent is None)
        self.progress.set_visible(s.percent is not None)
        if s.percent is not None:
            self.progress.set_fraction(min(max(s.percent / 100, 0), 1))
        self._loading = True
        preset = status.get("preset", "documents")
        model = self.preset.get_model()
        if preset == "custom" and model.get_n_items() == len(view.WINDOW_PRESETS):
            model.append(view.preset_title("custom"))
        presets = [*view.WINDOW_PRESETS, "custom"]
        self.preset.set_selected(presets.index(preset) if preset in presets else 1)
        schedule = status.get("schedule", "daily")
        if schedule in view.SCHEDULE_ORDER:
            self.schedule.set_selected(view.SCHEDULE_ORDER.index(schedule))
        self.secrets.set_active(bool(status.get("system_secrets")))
        self._loading = False
        self.place.set_subtitle(view.place_text(status.get("destination")))
        self.win.poll(running=s.percent is not None or self.win.expecting_run())

    def _got_snapshots(self, snaps: Any, error: Exception | None) -> None:
        for row in self.rows:
            self.list.remove(row)
        self.rows = []
        if error:
            row = Adw.ActionRow(title=_("Cannot list the backups"), subtitle=str(error))
        elif not snaps:
            row = Adw.ActionRow(title=_("No backups yet"))
        else:
            row = None
        if row is not None:
            self.list.add(row)
            self.rows.append(row)
            return
        for snap_id, title, detail in view.snapshot_rows(snaps)[:30]:
            row = Adw.ActionRow(title=title, subtitle=detail)
            restore = Gtk.Button(label=_("Restore…"), valign=Gtk.Align.CENTER)
            restore.connect("clicked", self._restore, snap_id, title)
            row.add_suffix(restore)
            self.list.add(row)
            self.rows.append(row)

    # -- actions -----------------------------------------------------------

    def _back_up_now(self, *_a: Any) -> None:
        self.now.set_sensitive(False)
        backup = self.win.backup
        if not self.enabled:

            def turn_on() -> dict:
                backup.turn_on()
                backup.set_schedule(backup.settings.schedule)
                return backup.start()

            def on(_result: Any, error: Exception | None) -> None:
                if error:
                    self.win.toast(_("Could not turn on: {why}").format(why=error))
                else:
                    self.win.expect_run()
                    self.win.toast(_("Backups are on. The first backup has started."))
                self.refresh()

            _in_thread(turn_on, on)
            return

        def done(result: Any, error: Exception | None) -> None:
            if error:
                self.win.toast(_("Could not start: {why}").format(why=error))
            elif result and not result.get("started"):
                self.win.toast(_("A backup is already running."))
            else:
                self.win.expect_run()
            self.refresh()

        _in_thread(self.win.backup.start, done)

    def _preset_changed(self, *_a: Any) -> None:
        if self._loading:
            return
        presets = [*view.WINDOW_PRESETS, "custom"]
        preset = presets[self.preset.get_selected()]
        if preset == "custom":
            return
        _in_thread(
            lambda: self.win.backup.set_paths(preset=preset, by_assistant=False),
            lambda _r, e: self.win.toast(str(e)) if e else None,
        )

    def _schedule_changed(self, *_a: Any) -> None:
        if self._loading:
            return
        schedule = view.SCHEDULE_ORDER[self.schedule.get_selected()]
        _in_thread(
            lambda: self.win.backup.set_schedule(schedule),
            lambda _r, e: self.win.toast(str(e)) if e else None,
        )

    def _secrets_changed(self, *_a: Any) -> None:
        if self._loading:
            return
        on = self.secrets.get_active()

        def save(value: bool) -> None:
            _in_thread(
                lambda: self.win.backup.set_paths(system_secrets=value, by_assistant=False),
                lambda _r, e: (self.win.toast(str(e)), self.refresh()) if e else None,
            )

        if not on:
            save(False)
            return
        dialog = Adw.AlertDialog(
            heading=_("Keep system secrets in backups?"),
            body=_(
                "Saved passwords from the key ring, online accounts, and SSH and GPG keys "
                "go into the backups, encrypted like everything else. Whoever has your "
                "recovery key can open them, so keep the sheet safe."
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("on", _("Keep them"))
        dialog.set_response_appearance("on", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")

        def answered(_d: Adw.AlertDialog, response: str) -> None:
            if response == "on":
                save(True)
            else:
                self._loading = True
                self.secrets.set_active(False)
                self._loading = False

        dialog.connect("response", answered)
        dialog.present(self.win)

    def _restore(self, _button: Gtk.Button, snap_id: str, title: str) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Restore the backup made {title}?").format(title=title.lower()),
            body=_(
                "Into a new folder, nothing on this computer is replaced. "
                "Over your files, files changed since the backup get their old version back; "
                "you see the list first."
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("over", _("Over my files…"))
        dialog.add_response("folder", _("Into a new folder…"))
        dialog.set_response_appearance("folder", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("folder")
        dialog.set_close_response("cancel")

        def answered(_d: Adw.AlertDialog, response: str) -> None:
            if response == "folder":
                self._restore_to_folder(snap_id, title)
            elif response == "over":
                self.restore_over(snap_id)

        dialog.connect("response", answered)
        dialog.present(self.win)

    def restore_over(self, snap_id: str) -> None:
        """Show which files would change, then restore over them if the person agrees."""
        backup = self.win.backup
        self.win.toast(_("Checking which files would change…"))

        def previewed(preview: Any, error: Exception | None) -> None:
            if error:
                self.win.toast(_("Could not restore: {why}").format(why=error))
                return
            self._confirm_over(preview)

        _in_thread(lambda: backup.preview_in_place(snapshot=snap_id), previewed)

    def _confirm_over(self, preview: dict[str, Any]) -> None:
        replaced, new = preview["replaced"], preview["new"]
        home = str(self.win.backup.layout.home)
        dialog = Adw.AlertDialog(
            heading=view.in_place_heading(len(replaced), new),
            body=view.in_place_body(len(replaced), new),
        )
        if replaced:
            text = Gtk.Label(
                label=view.replaced_for_reading(replaced, home),
                xalign=0,
                yalign=0,
                wrap=True,
            )
            text.add_css_class("monospace")
            scroll = Gtk.ScrolledWindow(
                hscrollbar_policy=Gtk.PolicyType.NEVER,
                propagate_natural_height=True,
                max_content_height=220,
            )
            scroll.set_child(text)
            dialog.set_extra_child(scroll)
        dialog.add_response("cancel", _("Cancel"))
        dialog.set_close_response("cancel")
        if replaced or new:
            dialog.add_response("restore", _("Replace files") if replaced else _("Bring back"))
            dialog.set_response_appearance(
                "restore",
                Adw.ResponseAppearance.DESTRUCTIVE
                if replaced
                else Adw.ResponseAppearance.SUGGESTED,
            )
        shown = set(replaced)

        def answered(_d: Adw.AlertDialog, response: str) -> None:
            if response != "restore":
                return
            self.win.toast(_("Restoring…"))
            _in_thread(
                lambda: self.win.backup.restore_in_place(
                    snapshot=preview["snapshot"],
                    # Only the files the person saw: if more would change now, stop.
                    confirm=lambda now, _new: set(now) <= shown,
                ),
                lambda result, error: self.win.toast(
                    _("Could not restore: {why}").format(why=error)
                    if error
                    else view.in_place_done(result)
                ),
            )

        dialog.connect("response", answered)
        dialog.present(self.win)

    def _restore_to_folder(self, snap_id: str, title: str) -> None:
        dialog = Gtk.FileDialog(title=_("Where to put the restored files"))
        dialog.set_initial_folder(Gio.File.new_for_path(str(self.win.backup.layout.home)))

        def picked(d: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                f = d.select_folder_finish(result)
            except GLib.Error:
                return
            if not f or not f.get_path():
                return
            stamp = time.strftime("%Y-%m-%d %H-%M")
            target = Path(f.get_path()) / _("Restored from backup {stamp}").format(stamp=stamp)
            self.win.toast(_("Restoring {title}…").format(title=title))

            def done(result: Any, error: Exception | None) -> None:
                if error:
                    self.win.toast(_("Could not restore: {why}").format(why=error))
                    return
                self.win.toast(
                    _("Restored {n} files into {folder}").format(
                        n=result.get("files", 0), folder=result.get("target", "")
                    )
                )

            _in_thread(
                lambda: self.win.backup.restore_to_new_folder(str(target), snapshot=snap_id),
                done,
            )

        dialog.select_folder(self.win, None, picked)


class BackupWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, backup: Backup) -> None:
        super().__init__(application=app, title=_("Backups"), default_width=600, default_height=780)
        self.set_size_request(360, 480)
        self.backup = backup
        self._poll_id = 0
        # A backup just started may not have written its progress yet:
        # keep reading the state for a while anyway.
        self._expect_until = 0.0
        self.toasts = Adw.ToastOverlay()
        view_ = Adw.ToolbarView()
        view_.add_top_bar(Adw.HeaderBar())
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.setup = SetupPage(self)
        self.sheet = SheetPage(self)
        self.recover = RecoverPage(self)
        self.status = StatusPage(self)
        self.stack.add_named(self.setup, "setup")
        self.stack.add_named(self.sheet, "sheet")
        self.stack.add_named(self.recover, "recover")
        self.stack.add_named(self.status, "status")
        self.toasts.set_child(self.stack)
        view_.set_content(self.toasts)
        self.set_content(view_)
        self.connect("close-request", self._closing)
        self.start()

    def start(self) -> None:
        try:
            s = self.backup.settings
        except (BackupError, ValueError, OSError) as e:
            self.show_setup()
            self.setup.show_error(str(e))
            return
        if s.destination is None:
            self.setup.load(s.preset, s.schedule)
            self.show_setup()
        elif not s.recovery_confirmed:
            self.show_sheet(None)
        else:
            self.show_status()

    def show_setup(self) -> None:
        s = self.backup.settings
        self.setup.load(s.preset, s.schedule)
        self.stack.set_visible_child_name("setup")
        self.set_title(_("Turn on backups"))

    def show_sheet(self, recovery_key: str | None) -> None:
        self.sheet.show(recovery_key)
        self.stack.set_visible_child_name("sheet")
        self.set_title(_("Recovery sheet"))

    def show_recover(self) -> None:
        self.recover.show()
        self.stack.set_visible_child_name("recover")
        self.set_title(_("Restore from backups"))

    def after_recover(self) -> None:
        """Connected to the old backups: offer to bring the files back right away."""
        self.show_status(_("This computer can now use your backups."))
        dialog = Adw.AlertDialog(
            heading=_("Bring your files back now?"),
            body=_(
                "Files from the latest backup come back into your home folder. "
                "First you see which files would change. "
                "Then turn backups on to keep this computer safe too."
            ),
        )
        dialog.add_response("later", _("Later"))
        dialog.add_response("restore", _("Bring them back…"))
        dialog.set_response_appearance("restore", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("later")
        dialog.connect(
            "response", lambda _d, r: self.status.restore_over("latest") if r == "restore" else None
        )
        dialog.present(self)

    def show_status(self, message: str | None = None) -> None:
        self.stack.set_visible_child_name("status")
        self.set_title(_("Backups"))
        self.status.refresh()
        if message:
            self.toast(message)

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=6))

    def expect_run(self) -> None:
        self._expect_until = time.monotonic() + EXPECT_RUN_SECONDS

    def expecting_run(self) -> bool:
        return time.monotonic() < self._expect_until

    def poll(self, *, running: bool) -> None:
        """While a backup runs, read the state every few seconds."""
        if running and not self._poll_id:
            self._poll_id = GLib.timeout_add_seconds(POLL_SECONDS, self._tick)
        elif not running and self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0
            self.status.refresh()

    def _tick(self) -> bool:
        self.status.refresh()
        return True

    def _closing(self, *_a: Any) -> bool:
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0
        return False


class BackupApp(Adw.Application):
    def __init__(self, backup_factory: Callable[[], Backup] = Backup.from_env) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.backup_factory = backup_factory

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)  # GTK 4.12+
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def do_activate(self) -> None:
        win = self.get_active_window()
        if win is None:
            win = BackupWindow(self, self.backup_factory())
        win.present()


def main(argv: list[str] | None = None) -> int:
    app = BackupApp()
    return app.run(sys.argv if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
