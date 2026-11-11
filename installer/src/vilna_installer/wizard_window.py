# SPDX-License-Identifier: GPL-3.0-or-later
"""The classic wizard on the screen (GTK 4 + libadwaita), without the assistant.

    vilna-installer-wizard [--plan PATH] [--window] [--demo empty|windows]

The steps on the left, one step on the right, «Back» and «Next» below.
It writes the same plan as the conversation (``wizard.Wizard``), needs no
model, and works with a keyboard and a screen reader alone: every control
has an accessible name, a new step and every error are announced.

Text that came from outside (disk models, partition labels, Wi-Fi names)
is shown as plain text, never as markup.

Exit status: 0 when the person pressed «Check and confirm the plan» (the
Herkos confirmation screen comes next), 1 when the window was closed.
"""

from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402
from vilna_mcp_installer import machine as machines  # noqa: E402
from vilna_mcp_installer import plan as plans  # noqa: E402

from vilna_installer import chat, confirm_window, start  # noqa: E402
from vilna_installer import conversation as conv  # noqa: E402
from vilna_installer import wizard as wiz  # noqa: E402
from vilna_installer.vault import Vault  # noqa: E402

APP_ID = "io.github.stdenis1980.VilnaInstaller.Wizard"
EXIT_CONFIRM, EXIT_CLOSED = 0, 1
USES = (None, "efi", "system", "home", "swap")

CSS = """
.vilna-steps { padding: 12px; background-color: @window_bg_color; }
.vilna-step { padding: 8px 12px; border-radius: 10px; }
.vilna-step-current { background-color: alpha(@accent_bg_color, 0.15); font-weight: 700; }
.vilna-step-title { font-weight: 800; font-size: 17pt; }
.vilna-error { color: @error_color; font-weight: 700; }
.vilna-go { background-color: #F5C24A; color: #13213A; font-weight: 800; padding: 10px 22px; }
"""


def _once(fn: Callable[[], Any]) -> Callable[[], bool]:
    def run() -> bool:
        fn()
        return False

    return run


def announce(widget: Gtk.Widget, text: str) -> None:
    """Say it to the screen reader (GTK 4.14+); the text is on the screen as well."""
    if hasattr(widget, "announce"):
        widget.announce(text, Gtk.AccessibleAnnouncementPriority.MEDIUM)


def plain_row(cls=Adw.ActionRow, **kwargs: Any) -> Adw.PreferencesRow:
    """A row whose title and subtitle are never read as Pango markup."""
    row = cls(**kwargs)
    row.set_use_markup(False)
    return row


def radio_group() -> Gtk.CheckButton:
    """A group leader never shown: a set of one choice still looks and reads as radio."""
    return Gtk.CheckButton()


def radio_row(
    title: str, subtitle: str | None, group: Gtk.CheckButton
) -> tuple[Adw.ActionRow, Gtk.CheckButton]:
    row = plain_row(title=title)
    if subtitle:
        row.set_subtitle(subtitle)
    check = Gtk.CheckButton(valign=Gtk.Align.CENTER, group=group)
    check.leader = group  # the leader is in no window: keep it alive with the group
    check.update_property([Gtk.AccessibleProperty.LABEL], [title])
    if subtitle:
        check.update_property([Gtk.AccessibleProperty.DESCRIPTION], [subtitle])
    row.add_prefix(check)
    row.set_activatable_widget(check)
    return row, check


def password_rows(title: str, repeat: str) -> tuple[Adw.PasswordEntryRow, Adw.PasswordEntryRow]:
    first = Adw.PasswordEntryRow(title=title)
    second = Adw.PasswordEntryRow(title=repeat)
    return first, second


def _group(title: str = "", description: str = "") -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup(title=title)
    if description:
        group.set_description(description)
    return group


class PartitionEditor(Adw.Dialog):
    """Manual layout: what each partition of the disk is for, and new partitions."""

    def __init__(
        self,
        wizard: wiz.Wizard,
        texts: start.Texts,
        on_saved: Callable[[], None],
    ) -> None:
        _ = texts
        super().__init__(title=_(wiz.EDITOR_TITLE), content_width=640, content_height=640)
        self.wizard, self._, self.on_saved = wizard, texts, on_saved
        self.disk = wizard.target() or {}
        self.rows = wiz.editor_rows(wizard.plan, self.disk)

        header = Adw.HeaderBar()
        suggest = Gtk.Button(label=_(wiz.SUGGEST))
        suggest.connect("clicked", self._on_suggest)
        header.pack_start(suggest)
        save = Gtk.Button(label=_(conv.SAVE))
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save)
        header.pack_end(save)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        self.page = Adw.PreferencesPage()
        self.error = Gtk.Label(
            xalign=0, wrap=True, visible=False, accessible_role=Gtk.AccessibleRole.ALERT
        )
        self.error.add_css_class("vilna-error")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.page)
        err = Gtk.Box(margin_start=24, margin_end=24, margin_bottom=12)
        err.append(self.error)
        box.append(err)
        view.set_content(box)
        self.set_child(view)
        self._fill()

    def _uses(self, with_none: bool) -> Gtk.StringList:
        _ = self._
        uses = USES if with_none else USES[1:]
        return Gtk.StringList.new([_(wiz.USE_TEXT[u]) for u in uses])

    def _fill(self) -> None:
        _ = self._
        for group in getattr(self, "_groups", []):
            self.page.remove(group)
        lead = _group(wiz.disk_title(self.disk) if self.disk else "", _(wiz.EDITOR_LEAD))
        existing = _group(_(wiz.EXISTING))
        parts = {p["id"]: p for p in self.disk.get("partitions", [])}
        new = _group(_(wiz.NEW_PARTITIONS))
        free = self.disk.get("largest_free", 0)
        new.set_description(_(wiz.FREE).format(size=wiz.size_text(free)))
        add = Gtk.Button(label=_(wiz.ADD_PARTITION), valign=Gtk.Align.CENTER)
        add.add_css_class("flat")
        add.connect("clicked", self._on_add)
        new.set_header_suffix(add)
        for row in self.rows:
            if row.partition is not None:
                self._existing_row(existing, row, parts.get(row.partition, {}))
            else:
                self._new_rows(new, row)
        self._groups = [lead, existing, new] if parts else [lead, new]
        for group in self._groups:
            self.page.add(group)

    def _existing_row(self, group, row: wiz.EditorRow, part: dict[str, Any]) -> None:
        _ = self._
        title = wiz.partition_title(part)
        combo = plain_row(Adw.ComboRow, title=title, model=self._uses(True))
        combo.set_selected(USES.index(row.use))
        fmt = Adw.SwitchRow(title=_(wiz.FORMAT), active=row.format)
        fmt.update_property([Gtk.AccessibleProperty.LABEL], [f"{_(wiz.FORMAT)}: {title}"])

        def sync(*_args: Any) -> None:
            row.use = USES[combo.get_selected()]
            forced = row.use in ("system", "swap")
            if forced:
                fmt.set_active(True)
            fmt.set_visible(row.use is not None)
            fmt.set_sensitive(not forced)
            row.format = fmt.get_active() if row.use is not None else False

        combo.connect("notify::selected", sync)
        fmt.connect("notify::active", sync)
        sync()
        group.add(combo)
        group.add(fmt)

    def _new_rows(self, group, row: wiz.EditorRow) -> None:
        _ = self._
        combo = Adw.ComboRow(title=_(wiz.USE), model=self._uses(False))
        combo.set_selected(USES.index(row.use) - 1 if row.use else 1)
        top = max(self.disk.get("size", 0) // wiz.GiB, 1)
        spin = Adw.SpinRow.new_with_range(0.5, top, 1)
        spin.set_title(_(wiz.SIZE_GIB))
        spin.set_digits(1)
        spin.set_value(round(row.size / wiz.GiB, 1))
        remove = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER)
        remove.add_css_class("flat")
        remove.set_tooltip_text(_(wiz.REMOVE))
        remove.update_property([Gtk.AccessibleProperty.LABEL], [_(wiz.REMOVE)])
        combo.add_suffix(remove)

        def sync(*_args: Any) -> None:
            row.use = USES[combo.get_selected() + 1]
            row.size = int(spin.get_value() * wiz.GiB)

        combo.connect("notify::selected", sync)
        spin.connect("notify::value", sync)
        remove.connect("clicked", self._on_remove, row)
        sync()
        group.add(combo)
        group.add(spin)

    def _on_add(self, _button: Gtk.Button) -> None:
        self.rows.append(wiz.EditorRow("home", None, True, 0))
        self._fill()

    def _on_remove(self, _button: Gtk.Button, row: wiz.EditorRow) -> None:
        self.rows.remove(row)
        self._fill()

    def _on_suggest(self, _button: Gtk.Button) -> None:
        self.rows = wiz.editor_suggest(self.disk)
        self._fill()

    def _show_error(self, text: str) -> None:
        self.error.set_label(text)
        self.error.set_visible(True)
        announce(self.error, text)

    def _on_save(self, _button: Gtk.Button) -> None:
        _ = self._
        error = self.wizard.set_partitions(self.rows)
        if error:
            self._show_error(_(error))
            return
        problems = [
            p
            for p in self.wizard.report()["problems"]
            if p["field"].startswith("disk.partitions") or p["code"] == "not_enough_space"
        ]
        if problems:
            self._show_error("\n".join(dict.fromkeys(wiz.problem_text(p, _) for p in problems)))
            return
        self.close()
        self.on_saved()


class WizardWindow(Adw.ApplicationWindow):
    def __init__(
        self,
        app: Adw.Application,
        plan_path: Path,
        machine: machines.Machine,
        *,
        secrets: Vault | None = None,
        demo: str | None = None,
    ) -> None:
        super().__init__(application=app)
        self.wizard = wiz.Wizard(plan_path, machine)
        self.secrets = secrets if secrets is not None else Vault()
        self.demo = demo  # the synthetic computer, for the Herkos screen too
        self.confirmed: confirm_window.confirm.Outcome | None = None
        code = (self.wizard.plan.get("ui") or {}).get("language", "en")
        self._ = start.Texts(code if code in start.CODES else "en")
        _ = self._
        self.set_default_size(1280, 800)
        self.set_title(_(start.TITLE))
        self.commit: Callable[[], str | None] = lambda: None
        self.key = ""

        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=_(start.TITLE)))

        self.steps = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.steps.add_css_class("navigation-sidebar")
        self.steps.update_property([Gtk.AccessibleProperty.LABEL], [_(conv.PLAN)])
        self.step_rows: dict[str, tuple[Gtk.ListBoxRow, Gtk.Image]] = {}
        for key in wiz.STEPS:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=10)
            box.add_css_class("vilna-step")
            done = Gtk.Image(icon_name="object-select-symbolic")
            box.append(done)
            box.append(Gtk.Label(label=_(wiz.section_title(key)), xalign=0))
            row.set_child(box)
            row.key = key
            self.steps.append(row)
            self.step_rows[key] = (row, done)
        self.steps.connect("row-activated", lambda _l, row: self.go(row.key, strict=False))
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=260)
        side.add_css_class("vilna-steps")
        side.append(self.steps)

        self.title = Gtk.Label(
            xalign=0,
            margin_start=24,
            margin_top=18,
            margin_end=24,
            accessible_role=Gtk.AccessibleRole.HEADING,
        )
        self.title.add_css_class("vilna-step-title")
        self.holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.error = Gtk.Label(
            xalign=0, wrap=True, visible=False, accessible_role=Gtk.AccessibleRole.ALERT
        )
        self.error.add_css_class("vilna-error")
        self.back = Gtk.Button(label=_(wiz.BACK))
        self.back.add_css_class("pill")
        self.back.connect("clicked", lambda _b: self.step(-1))
        self.next = Gtk.Button(label=_(wiz.NEXT))
        self.next.add_css_class("pill")
        self.next.add_css_class("suggested-action")
        self.next.connect("clicked", lambda _b: self.step(+1))
        bar = Gtk.Box(spacing=10, margin_start=24, margin_end=24, margin_top=8, margin_bottom=18)
        holder = Gtk.Box(hexpand=True)
        holder.append(self.error)
        bar.append(holder)
        bar.append(self.back)
        bar.append(self.next)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        content.append(self.title)
        content.append(self.holder)
        content.append(bar)

        body = Gtk.Box()
        body.append(side)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        body.append(content)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(body)
        self.set_content(view)
        self.set_default_widget(self.next)
        self.go(self.wizard.first_open(), strict=False)

    # moving between steps

    def step(self, delta: int) -> None:
        i = wiz.STEPS.index(self.key)
        if delta > 0 and self.key == "summary":
            self._finish()
            return
        target = wiz.STEPS[max(0, min(len(wiz.STEPS) - 1, i + delta))]
        self.go(target, strict=delta > 0)

    def go(self, key: str, *, strict: bool) -> None:
        """Save the step shown, then show ``key``; with ``strict`` an error keeps it here."""
        error = self.commit() if self.key else None
        if error and strict:
            self._show_error(self._(error))
            return
        self.key = key
        self.wizard.seen.add(key)
        _ = self._
        self.error.set_visible(False)
        child = self.holder.get_first_child()
        if child is not None:
            self.holder.remove(child)
        page, self.commit = getattr(self, f"_page_{key}")()
        page.set_vexpand(True)
        self.holder.append(page)
        number = wiz.STEPS.index(key) + 1
        heading = _(wiz.section_title(key))
        self.title.set_label(heading)
        self.back.set_sensitive(number > 1)
        self.next.set_label(_(conv.CHECK_PLAN) if key == "summary" else _(wiz.NEXT))
        (self.next.add_css_class if key == "summary" else self.next.remove_css_class)("vilna-go")
        self._mark_steps()
        announce(
            self.title,
            _(wiz.STEP_OF).format(number=number, count=len(wiz.STEPS), title=heading),
        )
        GLib.idle_add(_once(lambda: page.child_focus(Gtk.DirectionType.TAB_FORWARD)))

    def _mark_steps(self) -> None:
        for key, (row, done) in self.step_rows.items():
            current = key == self.key
            box = row.get_child()
            (box.add_css_class if current else box.remove_css_class)("vilna-step-current")
            finished = key != "summary" and self.wizard.done(key)
            done.set_opacity(1.0 if finished else 0.0)
            row.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [self._(wiz.section_title(key)) + (" ✓" if finished else "")],
            )

    def _show_error(self, text: str) -> None:
        self.error.set_label(text)
        self.error.set_visible(True)
        announce(self.error, text)

    def _finish(self) -> None:
        """The plan is ready: the Herkos screen shows every change and asks for the yes."""
        if self.wizard.to_do():
            self.go("summary", strict=False)
            return
        confirm_window.ask(
            self,
            self.wizard.plan_path,
            self.secrets,
            self._confirm_done,
            demo=self.demo,
            window=not self.is_fullscreen(),
            stage=self.wizard.stage,
        )

    def _confirm_done(self, outcome: confirm_window.confirm.Outcome) -> None:
        if outcome.confirmed:
            self.confirmed = outcome
            app = self.get_application()
            if app is not None:
                app.exit_code = EXIT_CONFIRM
                app.quit()
            return
        # Not confirmed: back to the summary, as the plan is now.
        self.go("summary", strict=False)
        self.next.grab_focus()

    # the pages: each returns (widget, commit) where commit saves it into the plan

    def _page(self, *groups: Adw.PreferencesGroup) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        for group in groups:
            page.add(group)
        return page

    def _page_languages(self):
        _ = self._
        order = self.wizard.chosen_languages()
        choices = self.wizard.language_choices()
        group = _group(description=_(wiz.LANGUAGES_LEAD))
        main = plain_row(title=_(wiz.MAIN_LANGUAGE))
        names = dict(choices)
        layouts = self.wizard.keyboards()
        layout_names = {k["code"]: k["name"] for k in layouts}
        kbd_row = plain_row(title=_(wiz.KEYBOARDS))
        extra_codes = [None] + [k["code"] for k in layouts]
        extra = Adw.ComboRow(
            title=_(wiz.ANOTHER_KEYBOARD),
            model=Gtk.StringList.new([_(wiz.NONE)] + [k["name"] for k in layouts]),
        )
        if hasattr(extra, "set_enable_search"):
            extra.set_enable_search(True)
        had = (self.wizard.plan.get("locale") or {}).get("keyboards") or []
        derived = wiz.keyboards_for(order)
        chosen_extra = next((k for k in had if k not in derived), None)
        if chosen_extra in extra_codes:
            extra.set_selected(extra_codes.index(chosen_extra))

        def refresh(*_args: Any) -> None:
            main.set_subtitle(names.get(order[0], order[0]) if order else "—")
            kbds = wiz.keyboards_for(order, extra_codes[extra.get_selected()])
            kbd_row.set_subtitle(", ".join(layout_names.get(k, k) for k in kbds))

        for loc, name in choices:
            row = plain_row(title=name)
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=loc in order)
            check.update_property([Gtk.AccessibleProperty.LABEL], [name])
            row.add_prefix(check)
            row.set_activatable_widget(check)

            def toggled(button: Gtk.CheckButton, loc: str = loc) -> None:
                if button.get_active() and loc not in order:
                    order.append(loc)
                elif not button.get_active() and loc in order:
                    order.remove(loc)
                refresh()

            check.connect("toggled", toggled)
            group.add(row)
        extra.connect("notify::selected", refresh)
        summary = _group()
        for row in (main, kbd_row, extra):
            summary.add(row)
        refresh()

        def commit() -> str | None:
            return self.wizard.set_languages(list(order), extra_codes[extra.get_selected()])

        return self._page(summary, group), commit

    def _page_network(self):
        _ = self._
        plan_net = self.wizard.plan.get("network") or {}
        ways = _group(description=_(wiz.NETWORK_LEAD))
        leader = radio_group()
        offline_row, offline = radio_row(_(conv.OFFLINE), None, leader)
        ways.add(offline_row)
        wired = None
        if self.wizard.wired():
            wired_row, wired = radio_row(_(conv.WIRED), None, leader)
            ways.add(wired_row)
        wifi = _group(_(wiz.WIFI_NETWORKS))
        rescan = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER)
        rescan.add_css_class("flat")
        rescan.set_tooltip_text(_(wiz.SCAN_AGAIN))
        rescan.update_property([Gtk.AccessibleProperty.LABEL], [_(wiz.SCAN_AGAIN)])
        wifi.set_header_suffix(rescan)
        password = Adw.PasswordEntryRow(title=_(conv.WIFI_PASSWORD).format(name=""), visible=False)
        connect = Gtk.Button(label=_(conv.CONNECT), valign=Gtk.Align.CENTER)
        connect.add_css_class("suggested-action")
        password.add_suffix(connect)
        state: dict[str, Any] = {"rows": [], "chosen": None}
        if plan_net.get("mode") == "wired" and wired is not None:
            wired.set_active(True)
        elif plan_net.get("mode") != "wifi":
            offline.set_active(True)

        def connected(ssid: str) -> bool:
            net = self.wizard.plan.get("network") or {}
            return net.get("wifi") == ssid and net.get("secret") == plans.ENTERED

        def chose(button: Gtk.CheckButton, net: dict[str, Any]) -> None:
            if not button.get_active():
                return
            state["chosen"] = net
            need = net["secured"] and not connected(net["ssid"])
            password.set_title(_(conv.WIFI_PASSWORD).format(name=net["ssid"]))
            password.set_visible(need)
            if need:
                GLib.idle_add(_once(password.grab_focus))

        def show(networks: list[dict[str, Any]]) -> bool:
            for row in state["rows"]:
                wifi.remove(row)
            state["rows"] = []
            if not networks:
                empty = plain_row(title=_(wiz.NO_WIFI))
                wifi.add(empty)
                state["rows"].append(empty)
            for net in networks:
                sub = f"{net['signal']} %" + (
                    f" · {_(wiz.CONNECTED)}" if connected(net["ssid"]) else ""
                )
                row, check = radio_row(net["ssid"], sub, leader)
                if net["secured"]:
                    row.add_suffix(Gtk.Image(icon_name="network-wireless-encrypted-symbolic"))
                check.connect("toggled", chose, net)
                wifi.add(row)
                state["rows"].append(row)
                if plan_net.get("mode") == "wifi" and plan_net.get("wifi") == net["ssid"]:
                    check.set_active(True)
            if password.get_parent() is not None:
                wifi.remove(password)
            wifi.add(password)
            return False

        def scan(*_args: Any) -> None:
            def work() -> None:
                GLib.idle_add(show, self.wizard.networks())

            threading.Thread(target=work, daemon=True).start()

        def on_connect(*_args: Any) -> None:
            net = state["chosen"]
            if net is None:
                return
            value = password.get_text()
            connect.set_sensitive(False)

            def work() -> None:
                ok = chat.connect_wifi(net["ssid"], value, demo=bool(self.demo))
                GLib.idle_add(done, ok, value)

            threading.Thread(target=work, daemon=True).start()

        def done(ok: bool, value: str) -> bool:
            connect.set_sensitive(True)
            net = state["chosen"]
            if not ok or net is None:
                self._show_error(_(conv.CONNECT_FAILED))
                return False
            self.wizard.set_network("wifi", net["ssid"], True)
            self.wizard.mark_entered("network")
            self.secrets.put("network", value)
            password.set_text("")
            password.set_visible(False)
            self.error.set_visible(False)
            announce(password, _(wiz.CONNECTED))
            self._mark_steps()
            scan()
            return False

        rescan.connect("clicked", scan)
        connect.connect("clicked", on_connect)
        password.connect("entry-activated", on_connect)
        show([])
        scan()

        def commit() -> str | None:
            if offline.get_active():
                return self.wizard.set_network("offline")
            if wired is not None and wired.get_active():
                return self.wizard.set_network("wired")
            net = state["chosen"]
            if net is None:
                return wiz.CHOOSE
            if net["secured"] and not connected(net["ssid"]):
                return wiz.WIFI_NOT_CONNECTED
            return self.wizard.set_network("wifi", net["ssid"], net["secured"])

        return self._page(ways, wifi), commit

    def _page_disk(self):
        _ = self._
        disk_plan = self.wizard.plan.get("disk") or {}
        disks = _group(_(wiz.DISKS), _(wiz.DISK_LEAD))
        disk_leader, what_leader = radio_group(), radio_group()
        chosen: dict[str, Any] = {"disk": None}
        what = _group(_(wiz.WHAT_TO_DO))
        replace_row, replace = radio_row(_(wiz.ERASE_AND_INSTALL), None, what_leader)
        manual_row, manual = radio_row(_(conv.MANUAL), _(wiz.MANUAL_HINT), what_leader)
        editor = Gtk.Button(label=_(wiz.OPEN_EDITOR), valign=Gtk.Align.CENTER)
        manual_row.add_suffix(editor)
        what.add(replace_row)
        what.add(manual_row)
        bitlocker = plain_row(title=_(wiz.BITLOCKER_KEY), visible=False)
        bitlocker.set_title_lines(0)
        bitlocker.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
        what.add(bitlocker)
        if disk_plan.get("layout") == "manual":
            manual.set_active(True)
        elif disk_plan.get("layout") == "replace":
            replace.set_active(True)

        def update(*_args: Any) -> None:
            disk = chosen["disk"]
            ok = disk is not None and not disk.get("needs_manual_layout")
            replace_row.set_sensitive(ok)
            if disk is not None and disk.get("needs_manual_layout"):
                replace_row.set_subtitle(_(wiz.PROBLEM_TEXT["needs_manual"]))
                manual.set_active(True)
            elif disk is not None and (lost := wiz.erased_by_replace(disk)):
                replace_row.set_subtitle(_(wiz.WILL_ERASE).format(what=lost))
            else:
                replace_row.set_subtitle("")
            editor.set_sensitive(disk is not None and manual.get_active())
            bitlocker.set_visible(manual.get_active() and wiz.has_bitlocker(disk))
            parts = (self.wizard.plan.get("disk") or {}).get("partitions")
            same = disk is not None and disk["id"] == disk_plan.get("target")
            if manual.get_active() and parts and same:
                manual_row.set_subtitle(_(wiz.PARTITIONS_CHOSEN).format(count=len(parts)))
            else:
                manual_row.set_subtitle(_(wiz.MANUAL_HINT))

        for disk in self.wizard.disks():
            row, check = radio_row(wiz.disk_title(disk), wiz.disk_subtitle(disk, _), disk_leader)

            def pick(button: Gtk.CheckButton, disk: dict[str, Any] = disk) -> None:
                if button.get_active():
                    chosen["disk"] = disk
                    update()

            check.connect("toggled", pick)
            disks.add(row)
            if disk["id"] == disk_plan.get("target") or len(self.wizard.disks()) == 1:
                check.set_active(True)
        replace.connect("toggled", update)
        manual.connect("toggled", update)

        def open_editor(*_args: Any) -> None:
            if chosen["disk"] is None:
                return
            error = self.wizard.set_disk(chosen["disk"]["id"], "manual")
            if error:
                self._show_error(_(error))
                return

            def saved() -> None:
                disk_plan.update(self.wizard.plan.get("disk") or {})
                update()
                self._mark_steps()

            PartitionEditor(self.wizard, _, saved).present(self)

        editor.connect("clicked", open_editor)
        update()

        def commit() -> str | None:
            disk = chosen["disk"]
            if disk is None:
                return wiz.CHOOSE_DISK
            if not (replace.get_active() or manual.get_active()):
                return wiz.CHOOSE
            layout = "replace" if replace.get_active() else "manual"
            error = self.wizard.set_disk(disk["id"], layout)
            if error:
                return error
            if layout == "manual" and not (self.wizard.plan.get("disk") or {}).get("partitions"):
                return wiz.PROBLEM_TEXT["manual_needs_editor"]
            return None

        return self._page(disks, what), commit

    def _page_encryption(self):
        _ = self._
        enc = ((self.wizard.plan.get("disk") or {}).get("encryption")) or {}
        group = _group(description=_(wiz.ENCRYPTION_LEAD))
        tpm_ok = self.wizard.tpm()
        leader = radio_group()
        tpm_row, tpm = radio_row(
            _(conv.ENCRYPT_TPM), _(wiz.TPM_HINT) if tpm_ok else _(wiz.NO_TPM), leader
        )
        tpm_row.set_sensitive(tpm_ok)
        pass_row, passphrase = radio_row(_(conv.ENCRYPT_PASS), _(wiz.PASSPHRASE_HINT), leader)
        none_row, none = radio_row(_(conv.NOT_ENCRYPTED), _(wiz.NONE_HINT), leader)
        first, second = password_rows(_(conv.PASSPHRASE), _(conv.REPEAT))
        for row in (tpm_row, pass_row, first, second, none_row):
            group.add(row)
        note = _group(description=_(wiz.KEY_NEXT))
        method = enc.get("method") or ("tpm2" if tpm_ok else "passphrase")
        {"tpm2": tpm, "passphrase": passphrase, "none": none}[method].set_active(True)

        def update(*_args: Any) -> None:
            show = passphrase.get_active()
            entered = enc.get("method") == "passphrase" and enc.get("passphrase") == plans.ENTERED
            first.set_visible(show)
            second.set_visible(show)
            if entered:
                first.set_title(f"{_(conv.PASSPHRASE)} · {_(conv.ENTERED_TEXT)}")
            note.set_visible(not none.get_active())

        for check in (tpm, passphrase, none):
            check.connect("toggled", update)
        update()

        def commit() -> str | None:
            if none.get_active():
                return self.wizard.set_encryption("none")
            if tpm.get_active():
                return self.wizard.set_encryption("tpm2")
            value = first.get_text()
            had = enc.get("method") == "passphrase" and enc.get("passphrase") == plans.ENTERED
            if not value and had:
                return self.wizard.set_encryption("passphrase")
            if len(value) < chat.MIN_PASSWORD:
                return conv.SHORT
            if second.get_text() != value:
                return conv.MISMATCH
            error = self.wizard.set_encryption("passphrase")
            if error:
                return error
            self.secrets.put("passphrase", value)
            self.wizard.mark_entered("passphrase")
            return None

        return self._page(group, note), commit

    def _page_user(self):
        _ = self._
        user = self.wizard.plan.get("user") or {}
        group = _group()
        name = Adw.EntryRow(title=_(wiz.FULL_NAME), text=user.get("full_name", ""))
        login = Adw.EntryRow(title=_(wiz.USERNAME), text=user.get("username", ""))
        host = Adw.EntryRow(title=_(wiz.HOSTNAME), text=user.get("hostname", ""))
        title = _(conv.PASSWORD).format(name="").strip()
        if user.get("password") == plans.ENTERED:
            title = f"{title} · {_(conv.ENTERED_TEXT)}"
        first, second = password_rows(title, _(conv.REPEAT))
        for row in (name, login, host, first, second):
            group.add(row)
        touched = {"login": bool(user.get("username")), "host": bool(user.get("hostname"))}
        filling = {"now": False}

        def suggest(*_args: Any) -> None:
            filling["now"] = True
            if not touched["login"]:
                login.set_text(wiz.suggest_username(name.get_text()))
            if not touched["host"]:
                host.set_text(wiz.suggest_hostname(login.get_text()))
            filling["now"] = False

        def edited(key: str) -> None:
            if not filling["now"]:
                touched[key] = True
                if key == "login" and not touched["host"]:
                    suggest()

        name.connect("changed", suggest)
        login.connect("changed", lambda *_a: edited("login"))
        host.connect("changed", lambda *_a: edited("host"))

        def commit() -> str | None:
            error = self.wizard.set_user(name.get_text(), login.get_text(), host.get_text())
            if error:
                return error
            value = first.get_text()
            if not value and self.wizard.plan.get("user", {}).get("password") == plans.ENTERED:
                return None
            if len(value) < chat.MIN_PASSWORD:
                return conv.SHORT
            if second.get_text() != value:
                return conv.MISMATCH
            self.secrets.put("user", value)
            self.wizard.mark_entered("user")
            return None

        return self._page(group), commit

    def _page_time(self):
        _ = self._
        zone, region = self.wizard.suggested_time()
        zones = machines.timezones()
        group = _group()
        zone_row = Adw.ComboRow(title=_(wiz.TIME_ZONE), model=Gtk.StringList.new(zones))
        if hasattr(zone_row, "set_enable_search"):
            zone_row.set_enable_search(True)
        if zone in zones:
            zone_row.set_selected(zones.index(zone))
        regions = list(dict.fromkeys([*self.wizard.chosen_languages(), *start.MAIN_LOCALES]))
        regions.sort(key=lambda loc: (loc not in self.wizard.chosen_languages(), loc))
        names = dict(self.wizard.language_choices())
        labels = [f"{names.get(loc, loc.split('.')[0])} ({loc.split('.')[0]})" for loc in regions]
        region_row = Adw.ComboRow(title=_(wiz.REGION), model=Gtk.StringList.new(labels))
        if region in regions:
            region_row.set_selected(regions.index(region))
        group.add(zone_row)
        group.add(region_row)

        def commit() -> str | None:
            return self.wizard.set_time(
                zones[zone_row.get_selected()], regions[region_row.get_selected()]
            )

        return self._page(group), commit

    def _page_privacy(self):
        group = _group(description=self._(wiz.PRIVACY_LEAD))
        return self._page(group), lambda: None

    def _page_summary(self):
        _ = self._
        report = self.wizard.report()
        lines = chat.describe(self.wizard.plan, _)
        plan_group = _group(_(conv.PLAN))
        for key, title in conv.SECTIONS.items():
            row = plain_row(title=_(title), subtitle=lines.get(key) or _(conv.NOT_DECIDED))
            row.add_css_class("property")
            plan_group.add(row)
        changes = _group(_(wiz.DISK_CHANGES))
        for change in report["changes"]:
            icon = {"erase": "dialog-warning-symbolic", "create": "list-add-symbolic"}.get(
                change["action"], "drive-harddisk-symbolic"
            )
            row = plain_row(title=wiz.change_text(change, _))
            row.add_prefix(Gtk.Image(icon_name=icon))
            changes.add(row)
        to_do = self.wizard.to_do(report)
        todo = _group(_(wiz.STILL_TO_DO) if to_do else "", "" if to_do else _(wiz.READY))
        for problem in to_do:
            key = _step_of(problem)
            text = (
                f"{_(wiz.section_title(key))}: {_(wiz.NEEDED_TEXT)}"
                if problem["code"] == "missing"
                else wiz.problem_text(problem, _)
            )
            row = plain_row(title=text, activatable=True)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, key=key: self.go(key, strict=False))
            todo.add(row)
        known = _group(_(wiz.WORTH_KNOWING))
        for warning in report["warnings"]:
            row = plain_row(title=wiz.problem_text(warning, _))
            row.set_title_lines(0)
            row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
            known.add(row)
        self.next.set_sensitive(not to_do)
        groups = [todo, known] if report["warnings"] else [todo]
        page = self._page(*groups, plan_group, changes)

        def commit() -> str | None:
            self.next.set_sensitive(True)
            return None

        return page, commit


def _step_of(problem: dict[str, str]) -> str:
    """The step where a problem of the validator is fixed."""
    field = problem.get("field", "")
    if problem["code"] == "missing":
        return field if field in wiz.STEPS else "summary"
    for prefix, key in (
        ("disk.encryption", "encryption"),
        ("disk", "disk"),
        ("network", "network"),
        ("user", "user"),
        ("locale.timezone", "time"),
        ("locale.region", "time"),
        ("locale", "languages"),
        ("migrate", "disk"),
    ):
        if field.startswith(prefix):
            return key
    return "summary"


def install_css() -> None:
    css = Gtk.CssProvider()
    css.load_from_string(CSS)
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


class WizardApp(Adw.Application):
    def __init__(self, plan_path: Path, window: bool, demo: str | None) -> None:
        super().__init__(application_id=APP_ID)
        self.plan_path, self.window, self.demo = plan_path, window, demo
        self.exit_code = EXIT_CLOSED

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        install_css()

    def do_activate(self) -> None:
        win = self.get_active_window() or WizardWindow(
            self, self.plan_path, wiz.machine_for(self.demo), demo=self.demo
        )
        if not self.window:
            win.fullscreen()
        win.present()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vilna-installer-wizard")
    p.add_argument("--plan", type=Path, default=None, help="the plan the first screen wrote")
    p.add_argument("--window", action="store_true", help="a window, not the whole screen")
    p.add_argument("--demo", choices=tuple(machines.DEMOS), help="a synthetic computer")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    app = WizardApp(args.plan or start.default_plan_path(), args.window, args.demo)
    GLib.set_application_name("Vilna")
    app.run([sys.argv[0]])
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())
