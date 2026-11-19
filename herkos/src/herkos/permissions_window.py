# SPDX-License-Identifier: GPL-3.0-or-later
"""The «Дозволи» window (GTK 4 + libadwaita): what Gloxi did, and what it may do.

    herkos-permissions [--state-dir DIR]

Two pages (design 10.9 and permissions.md 5.3, 7.1):

- **Journal**: Gloxi's actions in plain language, by day, with a filter by
  kind. A change that can be undone has «Скасувати»; the undo is a new
  action through Herkos, so Herkos asks first, in its own window.
- **Permissions**: what the person allowed, lasting or for this session,
  each with «Відкликати».

The window is the person's own: it talks to the system Herkos through the
person's connection, which Gloxi does not have (``permissions_view``).
``--state-dir`` reads a Herkos-lite folder instead (tests, screenshots).
"""

from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from herkos import permissions_view as view  # noqa: E402
from herkos.i18n import _  # noqa: E402
from herkos.manifest import ManifestRegistry  # noqa: E402

APP_ID = "io.github.stdenis1980.VilnaPermissions"
REFRESH_SECONDS = 5

CSS = """
.vilna-risk { font-weight: 800; font-size: 9pt; padding: 2px 9px; border-radius: 999px; }
.vilna-r0 { background-color: alpha(@view_fg_color, 0.10); color: alpha(@view_fg_color, 0.7); }
.vilna-r1 { background-color: alpha(@accent_bg_color, 0.14); color: @accent_color; }
.vilna-r2 { background-color: alpha(@warning_bg_color, 0.18); color: @warning_color; }
.vilna-r3 { background-color: alpha(@error_bg_color, 0.14); color: @error_color; }
.vilna-r4 { background-color: @error_bg_color; color: @error_fg_color; }
"""


def _in_thread(work: Callable[[], Any], done: Callable[[Any, Exception | None], None]) -> None:
    def run() -> None:
        try:
            result, error = work(), None
        except (view.HerkosUnreachable, OSError, ValueError) as e:
            result, error = None, e
        GLib.idle_add(lambda: done(result, error) and False)

    threading.Thread(target=run, daemon=True).start()


def _risk(level: str | None) -> Gtk.Label:
    label = Gtk.Label(label=level or "—", valign=Gtk.Align.CENTER)
    label.add_css_class("vilna-risk")
    if level and len(level) == 2 and level[0] == "R" and level[1] in "01234":
        label.add_css_class(f"vilna-r{level[1]}")
    return label


def _clear(box: Gtk.Box) -> None:
    while (child := box.get_first_child()) is not None:
        box.remove(child)


class JournalPage(Gtk.Box):
    def __init__(self, window: PermissionsWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.category = "all"
        self.actions: list[view.Action] = []

        filters = Gtk.Box(spacing=0, halign=Gtk.Align.CENTER, margin_top=12)
        filters.add_css_class("linked")
        first = None
        for cat in view.CATEGORIES:
            b = Gtk.ToggleButton(label=view.category_title(cat), active=cat == "all")
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.connect("toggled", self._on_filter, cat)
            filters.append(b)
        self.append(filters)

        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp = Adw.Clamp(maximum_size=720, child=self.list, margin_top=12, margin_bottom=12)
        clamp.set_margin_start(12)
        clamp.set_margin_end(12)
        scroll = Gtk.ScrolledWindow(vexpand=True, child=clamp)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.append(scroll)

        foot = Gtk.Label(
            label=_("Changes can be undone for 30 days. The journal cannot be altered."),
            wrap=True,
            margin_top=8,
            margin_bottom=10,
        )
        foot.add_css_class("dim-label")
        foot.add_css_class("caption")
        self.append(foot)

    def _on_filter(self, button: Gtk.ToggleButton, cat: str) -> None:
        if button.get_active():
            self.category = cat
            self.show(self.actions)

    def show(self, actions: list[view.Action]) -> None:
        self.actions = actions
        _clear(self.list)
        days = view.by_day(actions, self.category)
        if not days:
            self.list.append(
                Adw.StatusPage(
                    icon_name="document-open-recent-symbolic",
                    title=_("Nothing here yet"),
                    description=_("When Gloxi does something for you, it shows up here."),
                    vexpand=True,
                )
            )
            return
        now = datetime.now().astimezone()
        today = now.date()
        for day, items in days:
            group = Adw.PreferencesGroup(title=view.day_title(day, today))
            for a in items:
                group.add(self._row(a, now))
            self.list.append(group)

    def _row(self, a: view.Action, now: datetime) -> Adw.ExpanderRow:
        row = Adw.ExpanderRow(
            title=GLib.markup_escape_text(view.title(a, self.window.registry)),
            subtitle=f"{view.time_text(a.when)} · {view.outcome_text(a)}",
        )
        row.add_prefix(_risk(a.level))
        if a.can_undo(now):
            undo = Gtk.Button(label=_("Undo"), valign=Gtk.Align.CENTER)
            undo.connect("clicked", lambda _b, aid=a.action_id: self.window.undo(aid))
            row.add_suffix(undo)
        if a.reason:
            row.add_row(
                Adw.ActionRow(title=_("Gloxi's reason"), subtitle=GLib.markup_escape_text(a.reason))
            )
        if a.resources:
            text = "\n".join(a.resources[:10])
            if a.resource_count > 10:
                text += "\n" + _("… and {n} more").format(n=a.resource_count - 10)
            row.add_row(Adw.ActionRow(title=_("What"), subtitle=GLib.markup_escape_text(text)))
        risk = _("Risk level {level}").format(level=a.level) if a.level else _("Not assessed")
        row.add_row(Adw.ActionRow(title=risk, subtitle=view.outcome_text(a)))
        return row


class PermissionsPage(Gtk.Box):
    def __init__(self, window: PermissionsWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.page = Adw.PreferencesPage()
        self.append(self.page)
        self.groups: list[Adw.PreferencesGroup] = []

    def show(self, permissions: list[view.Permission]) -> None:
        for g in self.groups:
            self.page.remove(g)
        self.groups = []
        intro = Adw.PreferencesGroup(
            description=_(
                "Gloxi asks before it changes anything. Here is what you allowed "
                "without asking again; take back anything you no longer want."
            )
        )
        self._add(intro)
        lasting = [p for p in permissions if p.duration == "permanent"]
        session = [p for p in permissions if p.duration != "permanent"]
        if not permissions:
            intro.add(
                Adw.ActionRow(
                    title=_("No permissions"),
                    subtitle=_("Gloxi will ask you each time."),
                )
            )
        for title, items in ((_("Always"), lasting), (_("Until the session ends"), session)):
            if not items:
                continue
            group = Adw.PreferencesGroup(title=title)
            for p in items:
                row = Adw.ActionRow(
                    title=GLib.markup_escape_text(view.permission_title(p, self.window.registry)),
                    subtitle=GLib.markup_escape_text(view.permission_subtitle(p)),
                )
                row.add_prefix(_risk(p.level))
                revoke = Gtk.Button(label=_("Revoke"), valign=Gtk.Align.CENTER)
                revoke.add_css_class("flat")
                revoke.add_css_class("error")
                revoke.connect("clicked", lambda _b, perm=p: self.window.revoke(perm))
                row.add_suffix(revoke)
                group.add(row)
            self._add(group)

    def _add(self, group: Adw.PreferencesGroup) -> None:
        self.page.add(group)
        self.groups.append(group)


class PermissionsWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, herkos: Any) -> None:
        super().__init__(application=app, title=_("Permissions"))
        self.set_default_size(720, 780)
        self.herkos = herkos
        self.registry = ManifestRegistry.installed()
        self.busy = False

        self.stack = Adw.ViewStack()
        self.journal = JournalPage(self)
        self.perms = PermissionsPage(self)
        self.stack.add_titled_with_icon(
            self.journal, "journal", _("Journal"), "document-open-recent-symbolic"
        )
        self.stack.add_titled_with_icon(
            self.perms, "permissions", _("Permissions"), "security-high-symbolic"
        )

        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        )
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh"))
        refresh.connect("clicked", lambda _b: self.refresh())
        header.pack_end(refresh)

        self.banner = Adw.Banner()
        self.toasts = Adw.ToastOverlay(child=self.stack)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.banner)
        body.append(self.toasts)
        self.toasts.set_vexpand(True)

        toolbar = Adw.ToolbarView(content=body)
        toolbar.add_top_bar(header)
        self.set_content(toolbar)

        self.refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)

    def _tick(self) -> bool:
        if self.is_active():
            self.refresh()
        return True

    def refresh(self) -> None:
        if self.busy:
            return
        self.busy = True

        def work() -> tuple[list[view.Action], list[view.Permission]]:
            return view.actions(self.herkos.records()), self.herkos.permissions()

        def done(result: Any, error: Exception | None) -> None:
            self.busy = False
            if error is not None:
                self.banner.set_title(_("Herkos is not answering: {why}").format(why=error))
                self.banner.set_revealed(True)
                return
            self.banner.set_revealed(False)
            actions, permissions = result
            self.journal.show(actions)
            self.perms.show(permissions)

        _in_thread(work, done)

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=5))

    def undo(self, action_id: str) -> None:
        # Herkos asks in its own window; this one only waits for the answer.
        def done(result: Any, error: Exception | None) -> None:
            self.toast(str(error) if error else view.undo_message(result))
            self.refresh()

        _in_thread(lambda: self.herkos.undo(action_id), done)

    def revoke(self, p: view.Permission) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Revoke this permission?"),
            body=_("Gloxi will ask you again before it does this: {what}.").format(
                what=view.permission_title(p, self.registry)
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("revoke", _("Revoke"))
        dialog.set_response_appearance("revoke", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def answered(_d: Adw.AlertDialog, response: str) -> None:
            if response != "revoke":
                return

            def done(result: Any, error: Exception | None) -> None:
                if error:
                    self.toast(str(error))
                elif result:
                    self.toast(_("Permission revoked."))
                self.refresh()

            _in_thread(lambda: self.herkos.revoke(p.grant_id), done)

        dialog.connect("response", answered)
        dialog.present(self)


class PermissionsApp(Adw.Application):
    def __init__(self, herkos_for: Callable[[], Any]) -> None:
        super().__init__(application_id=APP_ID)
        self.herkos_for = herkos_for

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def do_activate(self) -> None:
        win = self.get_active_window() or PermissionsWindow(self, self.herkos_for())
        win.present()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="herkos-permissions")
    parser.add_argument("--state-dir", type=Path, help="a Herkos-lite state folder")
    args, rest = parser.parse_known_args(sys.argv[1:] if argv is None else argv)

    def herkos_for() -> Any:
        if args.state_dir is not None:
            return view.LiteHerkos(state_dir=args.state_dir)
        return view.find_herkos()

    return PermissionsApp(herkos_for).run([sys.argv[0], *rest])


if __name__ == "__main__":
    sys.exit(main())
