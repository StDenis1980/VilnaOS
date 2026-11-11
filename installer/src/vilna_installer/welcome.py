# SPDX-License-Identifier: GPL-3.0-or-later
"""The first installer screen (GTK 4 + libadwaita), design 10.12.

    vilna-installer [--window] [--plan PATH] [--demo empty|windows]

Step 0 of image/docs/installer.md: no model yet. The person picks the
language (its own name, no flags), can turn on the screen reader, and goes
on with Gloxi or without the assistant. The screen writes the start of the
plan (``start.write_plan``). With Gloxi, the conversation window
(``chat.ChatWindow``, steps 1–13) follows in the same program; without the
assistant the program ends with status 3 and the classic wizard goes on
with the same plan.

Under the greeting: the Blavet mark and the release name, on the Blavet
wallpaper (light by default, dark when the system is dark).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import resources
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from vilna_installer import start  # noqa: E402

APP_ID = "io.github.stdenis1980.VilnaInstaller"
BACKGROUNDS = Path(os.environ.get("VILNA_BACKGROUNDS", "/usr/share/backgrounds/vilna"))
A11Y_SCHEMA = "org.gnome.desktop.a11y.applications"
EXIT_WIZARD = 3  # «Without the assistant»: the classic wizard goes on

CSS = """
.vilna-veil { background-image: radial-gradient(ellipse 55% 62% at 50% 52%,
    alpha(@window_bg_color, 0.94), alpha(@window_bg_color, 0.86) 60%,
    alpha(@window_bg_color, 0.30)); }
.vilna-welcome { font-size: 30pt; font-weight: 800; letter-spacing: -0.5px; }
.vilna-lead { font-size: 12.5pt; }
.vilna-chip { padding: 4px 14px 4px 4px; border-radius: 999px; font-weight: 700;
    background-color: alpha(@view_fg_color, 0.08); }
.vilna-lang { min-width: 132px; min-height: 40px; border-radius: 12px; font-weight: 700;
    background-color: alpha(@window_bg_color, 0.62);
    border: 1px solid alpha(@view_fg_color, 0.14); }
.vilna-glass { background-color: alpha(@window_bg_color, 0.72);
    border: 1px solid alpha(@view_fg_color, 0.14); font-weight: 700; padding: 10px 18px; }
.vilna-lang:checked { background-color: @accent_bg_color; color: @accent_fg_color;
    border-color: transparent; }
.vilna-go { background-color: #F5C24A; color: #13213A; font-weight: 800; padding: 10px 22px; }
.vilna-go:hover { background-color: #F7CD66; }
.vilna-other { font-weight: 700; border-radius: 12px; padding: 6px 16px;
    background-color: alpha(@window_bg_color, 0.62); }
.vilna-other.chosen { background-color: @accent_bg_color; color: @accent_fg_color; }
.vilna-note { font-size: 10.5pt; }
"""


def _wallpaper(path: Path) -> Gdk.Texture | None:
    """The Blavet wallpaper without its bottom-left corner (start.wallpaper_crop).

    The corner holds the Vilna mark: cut by the screen edge on 16:10 screens
    and under «Screen reader» on any screen (VM test of 2026-11-09), while
    the screen shows the Blavet mark itself.
    """
    try:
        full = GdkPixbuf.Pixbuf.new_from_file(str(path))
    except GLib.Error:
        return None
    x, y, width, height = start.wallpaper_crop(full.get_width(), full.get_height())
    return Gdk.Texture.new_for_pixbuf(full.new_subpixbuf(x, y, width, height))


def language_names() -> tuple[object, object]:
    """(own name, name in the screen's language) of a locale, as GNOME Settings
    and gnome-initial-setup show them (iso-codes); without GnomeDesktop, None."""
    try:
        gi.require_version("GnomeDesktop", "4.0")
        from gi.repository import GnomeDesktop
    except (ImportError, ValueError):
        return (lambda _loc: None), (lambda _loc, _ui: None)

    def lookup(loc: str, ui: str) -> str | None:
        # LANGUAGE outranks the locale GnomeDesktop switches to for the
        # translation (glibc gettext), so every name would come out in it.
        saved = os.environ.pop("LANGUAGE", None)
        try:
            return GnomeDesktop.get_language_from_locale(loc, ui)
        finally:
            if saved is not None:
                os.environ["LANGUAGE"] = saved

    def own(loc: str) -> str | None:
        return lookup(loc, loc)

    return own, lookup


def _data(name: str) -> str:
    return str(resources.files("vilna_installer.data") / name)


class Welcome(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, plan_path: Path, language: str) -> None:
        super().__init__(application=app)
        self.plan_path = plan_path
        self.texts = start.Texts(language)
        self.locale: str | None = None  # set only by «Other languages»
        self.own_name, self.translated_name = language_names()
        self.others: list[tuple[str, str]] | None = None
        self.set_default_size(1280, 800)
        dark = Adw.StyleManager.get_default().get_dark()
        variant = "dark" if dark else "light"

        overlay = Gtk.Overlay()
        backdrop = Gtk.Box(hexpand=True, vexpand=True)
        texture = _wallpaper(BACKGROUNDS / f"vilna-blavet-{variant}.jpg")
        if texture is not None:
            backdrop = Gtk.Picture.new_for_paintable(texture)
            backdrop.set_content_fit(Gtk.ContentFit.COVER)
        overlay.set_child(backdrop)
        veil = Gtk.Box(hexpand=True, vexpand=True)
        veil.add_css_class("vilna-veil")
        overlay.add_overlay(veil)

        center = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        mark = Gtk.Image.new_from_file(_data(f"blavet-mark-{variant}.svg"))
        mark.set_pixel_size(104)
        center.append(mark)
        self.title = Gtk.Label()
        self.title.add_css_class("vilna-welcome")
        center.append(self.title)

        chip = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        chip.add_css_class("vilna-chip")
        small = Gtk.Image.new_from_file(_data(f"blavet-mark-{variant}.svg"))
        small.set_pixel_size(26)
        chip.append(small)
        self.chip = Gtk.Label()
        chip.append(self.chip)
        center.append(chip)

        self.lead = Gtk.Label(wrap=True, max_width_chars=48, justify=Gtk.Justification.CENTER)
        self.lead.add_css_class("vilna-lead")
        center.append(self.lead)

        grid = Gtk.Grid(column_spacing=8, row_spacing=8, halign=Gtk.Align.CENTER, margin_top=8)
        first = None
        self.buttons: dict[str, Gtk.ToggleButton] = {}
        for i, lang in enumerate(start.LANGUAGES):
            b = Gtk.ToggleButton(label=lang.name, active=lang.code == language)
            b.add_css_class("vilna-lang")
            # The name is in its own language; say so to the screen reader.
            b.update_property([Gtk.AccessibleProperty.LABEL], [lang.name])
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.connect("toggled", self._on_language, lang.code)
            grid.attach(b, i % 5, i // 5, 1, 1)
            self.buttons[lang.code] = b
        center.append(grid)

        self.other = Gtk.Button(halign=Gtk.Align.CENTER)
        self.other.add_css_class("flat")
        self.other.add_css_class("vilna-other")
        self.other.connect("clicked", self._on_other)
        center.append(self.other)
        self.note = Gtk.Label(
            label=start.NOT_TRANSLATED,
            wrap=True,
            max_width_chars=60,
            visible=False,
            justify=Gtk.Justification.CENTER,
        )
        self.note.add_css_class("vilna-note")
        self.note.add_css_class("dim-label")
        center.append(self.note)
        overlay.add_overlay(center)

        self.reader = Gtk.ToggleButton(halign=Gtk.Align.START, valign=Gtk.Align.END)
        self.reader.set_margin_start(24)
        self.reader.set_margin_bottom(24)
        self.reader_label = Gtk.Label()
        reader_box = Gtk.Box(spacing=8)
        reader_box.append(Gtk.Image(icon_name="preferences-desktop-accessibility-symbolic"))
        reader_box.append(self.reader_label)
        self.reader.set_child(reader_box)
        self.reader.add_css_class("pill")
        self.reader.add_css_class("vilna-glass")
        self.reader.connect("toggled", self._on_reader)
        overlay.add_overlay(self.reader)

        go = Gtk.Box(spacing=10, halign=Gtk.Align.END, valign=Gtk.Align.END)
        go.set_margin_end(24)
        go.set_margin_bottom(24)
        self.without = Gtk.Button()
        self.without.add_css_class("pill")
        self.without.add_css_class("vilna-glass")
        self.without.connect("clicked", lambda _b: self._finish(assistant=False))
        self.with_gloxi = Gtk.Button()
        self.with_gloxi.add_css_class("pill")
        self.with_gloxi.add_css_class("vilna-go")
        self.with_gloxi.connect("clicked", lambda _b: self._finish(assistant=True))
        go.append(self.without)
        go.append(self.with_gloxi)
        overlay.add_overlay(go)

        self.set_content(overlay)
        self.set_default_widget(self.with_gloxi)
        self._relabel()
        self.buttons[language].grab_focus()
        # Read every language of the system once the screen is up, so that
        # «Other languages…» opens at once (it took 1-2 s in the VM).
        GLib.timeout_add(500, self._load_others)
        self.connect("map", self._on_map)

    def _on_map(self, _window: Gtk.Window) -> None:
        if not getattr(self, "_model_woken", False):
            self._model_woken = True
            GLib.timeout_add_seconds(start.MODEL_WAKE_DELAY_S, self._wake_model)

    def _wake_model(self) -> bool:
        start.wake_model()
        return GLib.SOURCE_REMOVE

    def _load_others(self) -> bool:
        if self.others is None:
            self.others = start.other_languages(start.system_locales(), self.own_name)
        return GLib.SOURCE_REMOVE

    @property
    def language(self) -> str:
        return self.texts.code

    def _on_language(self, button: Gtk.ToggleButton, code: str) -> None:
        if button.get_active():
            self.locale = None
            self.texts = start.Texts(code)
            self._relabel()

    def _ui_locale(self) -> str:
        if self.locale is not None:
            return self.locale
        return next(lang.locale for lang in start.LANGUAGES if lang.code == self.language)

    def _on_other(self, _button: Gtk.Button) -> None:
        if self.others is None:
            self.others = start.other_languages(start.system_locales(), self.own_name)
        _ = self.texts
        dialog = Adw.Dialog(title=_(start.OTHER), content_width=460, content_height=560)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(12)
        search = Gtk.SearchEntry(placeholder_text=_(start.SEARCH))
        search.update_property([Gtk.AccessibleProperty.LABEL], [_(start.SEARCH)])
        box.append(search)
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows.add_css_class("boxed-list")
        ui = self._ui_locale()
        for loc, name in self.others:
            row = Adw.ActionRow(title=name, activatable=True)
            local = self.translated_name(loc, ui) or ""
            if local and local != name:
                row.set_subtitle(local)
            row.locale, row.search = loc, f"{name} {local} {loc}".casefold()
            if loc == self.locale:
                row.add_suffix(Gtk.Image(icon_name="object-select-symbolic"))
            rows.append(row)
        rows.set_filter_func(lambda row: search.get_text().casefold() in row.search)
        search.connect("search-changed", lambda _s: rows.invalidate_filter())
        # The dialog opens where the person just clicked «Other languages…»:
        # a double click, or a second click a moment later, landed on a row
        # and chose a language by accident (VM tests of 2026-11-09). Rows
        # answer only start.PICK_GUARD_MS after the dialog is on screen
        # ("map"), not after it was built: building can take a second or two.
        shown: list[int] = []
        dialog.connect("map", lambda _d: shown.append(GLib.get_monotonic_time()))

        def on_row(_list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
            if shown and start.pick_allowed(shown[0], GLib.get_monotonic_time()):
                self._pick_other(dialog, row.locale)

        rows.connect("row-activated", on_row)
        # Esc in the search field closes the dialog, as anywhere else.
        search.connect("stop-search", lambda _s: dialog.close())
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(rows)
        box.append(scroll)
        view.set_content(box)
        dialog.set_child(view)
        dialog.set_focus(search)
        dialog.present(self)

    def _pick_other(self, dialog: Adw.Dialog, loc: str) -> None:
        dialog.close()
        code = start.language_of(loc)
        for button in self.buttons.values():
            button.set_active(False)
        self.locale = loc
        self.texts = start.Texts(code)
        self._relabel()

    def _relabel(self) -> None:
        _ = self.texts
        self.set_title(_(start.TITLE))
        self.title.set_label(_(start.WELCOME))
        self.chip.set_label(_(start.RELEASE_CHIP))
        self.lead.set_label(_(start.LEAD))
        self.reader_label.set_label(_(start.SCREEN_READER))
        self.without.set_label(_(start.WITHOUT))
        self.with_gloxi.set_label(_(start.WITH_GLOXI))
        if self.locale is None:
            self.other.set_label(_(start.OTHER) + "\u2026")
            self.other.remove_css_class("chosen")
        else:
            name = self.own_name(self.locale) or self.locale
            self.other.set_label(name)
            self.other.update_property([Gtk.AccessibleProperty.LABEL], [name])
            self.other.add_css_class("chosen")
        self.note.set_visible(self.locale is not None and self.language not in start.CODES)

    def _on_reader(self, button: Gtk.ToggleButton) -> None:
        # Orca, as GNOME's own setting does it; without the schema only the plan knows.
        source = Gio.SettingsSchemaSource.get_default()
        if source is not None and source.lookup(A11Y_SCHEMA, True) is not None:
            Gio.Settings.new(A11Y_SCHEMA).set_boolean("screen-reader-enabled", button.get_active())

    def _finish(self, assistant: bool) -> None:
        plan = start.start_plan(
            self.language, self.reader.get_active(), assistant, locale=self.locale
        )
        start.write_plan(plan, self.plan_path)
        print(json.dumps(plan, ensure_ascii=False))
        app = self.get_application()
        if isinstance(app, InstallerApp):
            if assistant:
                app.open_chat(self)
            else:
                app.exit_code = EXIT_WIZARD
                app.quit()


class InstallerApp(Adw.Application):
    def __init__(
        self, plan_path: Path, window: bool, language: str, demo: str | None = None
    ) -> None:
        super().__init__(application_id=APP_ID)
        self.plan_path, self.window, self.language = plan_path, window, language
        self.demo = demo
        self.exit_code = 1  # closed before the end
        self.close_tools = lambda: None

    def open_chat(self, welcome: Gtk.Window) -> None:
        """Steps 1–13: the conversation with Gloxi, or a note that it cannot start."""
        from vilna_installer import chat, conversation

        css = Gtk.CssProvider()
        css.load_from_string(chat.CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

        def starting() -> conversation.Conversation | None:
            # Runs off the main thread (ChatWindow): the window answers meanwhile.
            try:
                talk, self.close_tools = conversation.start_conversation(
                    self.plan_path, demo=self.demo
                )
            except conversation.ToolsError as e:
                print(f"vilna-installer: {e}", file=sys.stderr)
                return None
            return talk

        win = chat.ChatWindow(self, None, self.plan_path, demo=bool(self.demo), starting=starting)
        if not self.window:
            win.fullscreen()
        win.present()
        welcome.destroy()

    def do_shutdown(self) -> None:
        self.close_tools()
        Adw.Application.do_shutdown(self)

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
        win = self.get_active_window() or Welcome(self, self.plan_path, self.language)
        if not self.window:
            win.fullscreen()
        win.present()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vilna-installer")
    p.add_argument("--plan", type=Path, default=None, help="where to write the plan")
    p.add_argument("--window", action="store_true", help="a window, not the whole screen")
    p.add_argument("--language", choices=start.CODES, help="start in this language")
    p.add_argument("--demo", choices=("empty", "windows"), help="a synthetic computer")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    language = args.language or start.default_language()
    app = InstallerApp(args.plan or start.default_plan_path(), args.window, language, args.demo)
    GLib.set_application_name("Vilna")
    app.run([sys.argv[0]])
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())
