# SPDX-License-Identifier: GPL-3.0-or-later
"""The Backups window (GTK 4 + libadwaita): turn backups on, see them, restore.

    vilna-backup-gui

The window is the person's own tool, as the terminal is: it may set up the
place and the keys (R3 in design.md 5), which Gloxi cannot. What it does not
do yet stays in the terminal: restoring over existing files, system secrets,
recovering on a new device (``vilna-backup restore --in-place``,
``vilna-backup secrets``, ``vilna-backup recover``).

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


class SetupPage(Gtk.Box):
    """What to keep, where, how often (design 10.16)."""

    def __init__(self, win: BackupWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.folder = ""
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

        where = Adw.PreferencesGroup(title=_("Where"))
        page.add(where)
        cloud = _radio_row(_("Vilna cloud"), _("In the EU · coming later"), None)
        cloud.set_sensitive(False)
        where.add(cloud)
        self.disk = _radio_row(_("External disk"), _("No folder chosen yet"), cloud.check)
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._choose_folder)
        self.disk.add_suffix(choose)
        where.add(self.disk)
        self.server = _radio_row(_("Your own server"), _("SFTP, S3 or REST"), cloud.check)
        where.add(self.server)
        self.address = Adw.EntryRow(title=_("Address, e.g. sftp:me@host:/backups"))
        self.login = Adw.EntryRow(title=_("Login (S3 and REST)"))
        self.password = Adw.PasswordEntryRow(title=_("Password (S3 and REST)"))
        for row in (self.address, self.login, self.password):
            where.add(row)
        self.disk.check.set_active(True)
        self.server.check.connect("toggled", lambda *_a: self._sync_server())
        self._sync_server()

        often = Adw.PreferencesGroup(title=_("How often"))
        page.add(often)
        self.schedule = Adw.ComboRow(
            title=_("Back up"),
            model=Gtk.StringList.new([view.schedule_title(s) for s in view.SCHEDULE_ORDER]),
        )
        often.add(self.schedule)

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
                self.folder = f.get_path()
                self.disk.set_subtitle(self.folder)
                self.disk.check.set_active(True)

        dialog.select_folder(self.win, None, picked)

    def chosen_preset(self) -> str:
        return next(p for p, row in self.presets.items() if row.check.get_active())

    def _next(self, *_a: Any) -> None:
        kind = "server" if self.server.check.get_active() else "disk"
        try:
            dest, creds = view.form_destination(
                kind,
                folder=self.folder,
                address=self.address.get_text(),
                login=self.login.get_text(),
                password=self.password.get_text(),
            )
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
            self.password.set_text("")
            self.win.show_sheet(key)

        _in_thread(work, done)

    def show_error(self, text: str) -> None:
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
        self.place = Adw.ActionRow(title=_("Where"))
        settings.add(self.place)

        self.list = Adw.PreferencesGroup(
            title=_("Backups"),
            description=_("Restore puts the files into a new folder; nothing is replaced."),
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
        self.now.set_sensitive(s.percent is None and status.get("enabled", False))
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

    def _restore(self, _button: Gtk.Button, snap_id: str, title: str) -> None:
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
        self.status = StatusPage(self)
        self.stack.add_named(self.setup, "setup")
        self.stack.add_named(self.sheet, "sheet")
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
