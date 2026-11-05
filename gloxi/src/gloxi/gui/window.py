# SPDX-License-Identifier: GPL-3.0-or-later
"""The Gloxi chat window (GTK 4 + libadwaita)."""

from __future__ import annotations

import signal
import sys
import threading
from gettext import gettext as _
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from gloxi import crash  # noqa: E402
from gloxi.backup_intro import BackupIntro, backup_intro  # noqa: E402
from gloxi.chat import Conversation  # noqa: E402
from gloxi.config import ConfigError, load_settings  # noqa: E402
from gloxi.gui import APP_ID  # noqa: E402
from gloxi.gui.controller import (  # noqa: E402
    ChatController,
    ModelSetup,
    cloud_status,
    current_choice,
    model_choices,
)
from gloxi.herkos_local import (  # noqa: E402
    LocalHerkos,
    add_folder,
    allowed_folders,
    remove_folder,
    start_if_needed,
)
from gloxi.i18n import setup  # noqa: E402
from gloxi.llm import ChatClient  # noqa: E402
from gloxi.privacy import guard_factory  # noqa: E402
from gloxi.router import CloudNotAllowed, Router  # noqa: E402
from gloxi.tools import file_tools  # noqa: E402

CSS = """
.bubble { padding: 8px 12px; border-radius: 12px; }
.bubble.user { background: alpha(@accent_bg_color, 0.15); }
.bubble.assistant { background: alpha(@card_bg_color, 1.0); }
"""


def _idle(func):
    """Run a view callback on the GTK main loop, whatever thread calls it."""

    def wrapper(self, *args):
        GLib.idle_add(lambda: func(self, *args) and False)

    return wrapper


class ChatWindow(Adw.ApplicationWindow):
    def __init__(
        self, app: Adw.Application, conversation: Conversation, intro: BackupIntro | None = None
    ) -> None:
        super().__init__(application=app, title="Gloxi", default_width=520, default_height=680)
        self.controller = ChatController(conversation, self, intro=intro)
        self._answer_label: Gtk.Label | None = None

        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title="Gloxi", subtitle=cloud_status(conversation))
        header.set_title_widget(self.title)
        new_button = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=_("New conversation"))
        new_button.connect("clicked", self._on_new)
        header.pack_start(new_button)
        # The model switcher (brief 10.3: in the input bar, Ctrl+/), also in the
        # middle of a conversation; it holds from the next message.
        self.model_switch = Gtk.DropDown(valign=Gtk.Align.CENTER)
        self.model_switch.update_property([Gtk.AccessibleProperty.LABEL], [_("Model")])
        self._fill_model_switch()
        self.model_switch.connect("notify::selected", self._on_model_selected)
        # Only with the Herkos bundled in the Flatpak: the folders it may use.
        self.herkos: LocalHerkos | None = None
        self.tools = None
        self.folders_button = Gtk.Button(
            icon_name="folder-symbolic", tooltip_text=_("Folders Gloxi may use"), visible=False
        )
        self.folders_button.connect("clicked", self._on_folders)
        header.pack_end(self.folders_button)
        report_button = Gtk.Button(
            icon_name="mail-message-new-symbolic",
            tooltip_text=_("Report a problem or suggest something"),
        )
        report_button.connect("clicked", lambda *_a: self.controller.report_problem())
        header.pack_end(report_button)

        self.messages = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.messages, f"set_margin_{side}")(12)
        self.scroller = Gtk.ScrolledWindow(vexpand=True, child=self.messages)

        self.entry = Gtk.Entry(
            hexpand=True,
            placeholder_text=_("Ask Gloxi… (/local or /cloud to pick the model)"),
        )
        self.entry.connect("activate", self._on_send)
        self.send_button = Gtk.Button(icon_name="go-up-symbolic", tooltip_text=_("Send"))
        self.send_button.add_css_class("suggested-action")
        self.send_button.add_css_class("circular")
        self.send_button.connect("clicked", self._on_send)
        bottom = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(bottom, f"set_margin_{side}")(12)
        bottom.append(self.model_switch)
        bottom.append(self.entry)
        # Capture phase: the entry itself binds Ctrl+/ to "select all".
        shortcuts = Gtk.ShortcutController(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        shortcuts.add_shortcut(
            Gtk.Shortcut(
                trigger=Gtk.ShortcutTrigger.parse_string("<Control>slash"),
                action=Gtk.CallbackAction.new(lambda *_a: self.model_switch.activate()),
            )
        )
        self.add_controller(shortcuts)
        bottom.append(self.send_button)

        self.toasts = Adw.ToastOverlay(child=self.scroller)
        # The bundled model: download progress and start-up (ModelSetup).
        self.model_banner = Adw.Banner(revealed=False)
        self.model_banner.connect("button-clicked", self._on_cancel_download)
        self.model_setup: ModelSetup | None = None
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        view.add_top_bar(self.model_banner)
        view.add_bottom_bar(bottom)
        self.set_content(view)
        self.entry.grab_focus()

    # -- user actions -------------------------------------------------------

    def _on_send(self, *_args) -> None:
        if self.controller.submit(self.entry.get_text()):
            self.entry.set_text("")

    def _on_new(self, *_args) -> None:
        if self.controller.busy:
            return
        self.controller.reset()
        while child := self.messages.get_first_child():
            self.messages.remove(child)

    # -- model switcher -----------------------------------------------------------

    def _fill_model_switch(self) -> None:
        router = self.controller.conversation.router
        self._choices = model_choices(router)
        self._filling = True
        self.model_switch.set_model(Gtk.StringList.new([c.label for c in self._choices]))
        modes = [c.mode for c in self._choices]
        self.model_switch.set_selected(modes.index(current_choice(router)))
        self._filling = False
        self._show_model_choice()
        if len(self._choices) == 1:
            self.model_switch.set_sensitive(False)
            self.model_switch.set_tooltip_text(
                _("Only the local model: turn the cloud on in the settings to choose another")
            )

    def _show_model_choice(self) -> None:
        if len(self._choices) > 1:
            choice = self._choices[self.model_switch.get_selected()]
            self.model_switch.set_tooltip_text(choice.detail)
        self.title.set_subtitle(cloud_status(self.controller.conversation))

    def _on_model_selected(self, *_args) -> None:
        if self._filling:
            return
        choice = self._choices[self.model_switch.get_selected()]
        try:
            self.controller.conversation.router.set_mode(choice.mode)
        except CloudNotAllowed:
            self.show_error(_("The cloud is turned off in the settings."))
            self._fill_model_switch()
            return
        self._show_model_choice()

    # -- folders (bundled Herkos) -----------------------------------------------

    def enable_folders(self, herkos: LocalHerkos, tools) -> None:
        self.herkos, self.tools = herkos, tools
        self.folders_button.set_visible(True)

    def _on_folders(self, *_args) -> None:
        dialog = Adw.PreferencesDialog(title=_("Folders Gloxi may use"))
        page = Adw.PreferencesPage()
        self._folders_group = Adw.PreferencesGroup(
            description=_(
                "Gloxi sees only the folders you choose here, and every action in them "
                "goes through Herkos, which asks you first."
            )
        )
        add = Gtk.Button(label=_("Add folder…"), valign=Gtk.Align.CENTER)
        add.add_css_class("suggested-action")
        add.connect("clicked", self._on_add_folder)
        self._folders_group.set_header_suffix(add)
        page.add(self._folders_group)
        dialog.add(page)
        self._folder_rows: list = []
        self._fill_folders()
        dialog.present(self)

    def _fill_folders(self) -> None:
        for row in self._folder_rows:
            self._folders_group.remove(row)
        self._folder_rows = []
        folders = allowed_folders()
        if not folders:
            row = Adw.ActionRow(title=_("No folders yet: Gloxi cannot see any of your files."))
            self._folders_group.add(row)
            self._folder_rows.append(row)
        for folder in folders:
            row = Adw.ActionRow(title=folder.name or str(folder), subtitle=str(folder))
            remove = Gtk.Button(
                icon_name="user-trash-symbolic",
                tooltip_text=_("Stop sharing this folder"),
                valign=Gtk.Align.CENTER,
            )
            remove.add_css_class("flat")
            remove.connect("clicked", lambda _b, f=folder: self._apply(remove_folder(f)))
            row.add_suffix(remove)
            self._folders_group.add(row)
            self._folder_rows.append(row)

    def _on_add_folder(self, *_args) -> None:
        chooser = Gtk.FileDialog(title=_("Choose a folder for Gloxi"))

        def done(dialog, result) -> None:
            try:
                chosen = dialog.select_folder_finish(result)
            except GLib.Error:
                return  # cancelled
            path = chosen.get_path() if chosen else None
            if path:
                self._apply(add_folder(Path(path)))

        chooser.select_folder(self, None, done)

    def _apply(self, folders) -> None:
        self._fill_folders()

        def job() -> None:
            try:
                self.herkos.restart()
            except Exception as exc:  # noqa: BLE001 - shown to the person
                self.show_error(_("Herkos could not restart: {why}").format(why=exc))
                return
            if self.tools is not None:
                self.tools.set_roots(folders)

        threading.Thread(target=job, daemon=True).start()

    def _on_cancel_download(self, *_args) -> None:
        if self.model_setup is not None:
            self.model_setup.cancel()

    # -- ModelSetupView -----------------------------------------------------

    @_idle
    def ask_model_download(self, text: str, answer) -> None:
        dialog = Adw.AlertDialog(heading=_("Download the language model?"), body=text)
        dialog.add_response("later", _("Not now"))
        dialog.add_response("download", _("Download"))
        dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("download")
        dialog.set_close_response("later")
        dialog.connect("response", lambda _d, response: answer(response == "download"))
        dialog.present(self)

    @_idle
    def show_model_progress(self, text: str, fraction: float | None) -> None:
        self.model_banner.set_title(text)
        # Only a download can be cancelled; starting takes seconds.
        self.model_banner.set_button_label(_("Cancel") if fraction is not None else None)
        self.model_banner.set_revealed(True)

    @_idle
    def hide_model_progress(self) -> None:
        self.model_banner.set_revealed(False)

    # -- ChatView -----------------------------------------------------------

    def _bubble(self, text: str, kind: str, caption: str | None = None) -> Gtk.Label:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_halign(Gtk.Align.END if kind == "user" else Gtk.Align.START)
        if caption:
            small = Gtk.Label(label=caption, xalign=0)
            small.add_css_class("caption")
            small.add_css_class("dim-label")
            box.append(small)
        label = Gtk.Label(label=text, wrap=True, xalign=0, selectable=True, max_width_chars=48)
        label.add_css_class("bubble")
        label.add_css_class(kind)
        box.append(label)
        self.messages.append(box)
        GLib.idle_add(self._scroll_to_end)
        return label

    def _scroll_to_end(self) -> bool:
        adj = self.scroller.get_vadjustment()
        adj.set_value(adj.get_upper())
        return False

    def show_user_message(self, text: str) -> None:
        self._bubble(text, "user")

    def start_answer(self, label: str) -> None:
        self._answer_label = self._bubble("…", "assistant", caption=label)
        self._answer_text = ""

    @_idle
    def append_answer(self, piece: str) -> None:
        self._answer_text += piece
        if self._answer_label is not None:
            self._answer_label.set_text(self._answer_text)
        self._scroll_to_end()

    @_idle
    def finish_answer(self) -> None:
        if self._answer_label is not None and not self._answer_text:
            self._answer_label.get_parent().set_visible(False)
        self._answer_label = None

    @_idle
    def show_error(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=6))

    @_idle
    def show_note(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    @_idle
    def open_uri(self, url: str) -> None:
        # Through the OpenURI portal in Flatpak; the address is also in the chat.
        Gtk.UriLauncher.new(url).launch(self, None, None, None)

    @_idle
    def set_busy(self, busy: bool) -> None:
        self.send_button.set_sensitive(not busy)
        if not busy:
            self.entry.grab_focus()


class GloxiApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self._clients: list[ChatClient] = []

    def do_activate(self) -> None:
        window = self.get_active_window()
        if window is None:
            window = self._build_window()
        if window is not None:
            window.present()

    def _build_window(self) -> ChatWindow | None:
        try:
            settings = load_settings()
        except ConfigError as exc:
            print(f"gloxi: {exc}", file=sys.stderr)
            self.quit()
            return None
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        local = ChatClient(settings.local)
        cloud = ChatClient(settings.cloud) if settings.cloud else None
        self._clients = [c for c in (local, cloud) if c]
        router = Router(local, cloud, settings.router, guard_factory=guard_factory(settings.router))
        self._herkos = start_if_needed(settings.tools_enabled)
        self._tools = file_tools(settings.tools_enabled)
        conversation = Conversation(router, settings.system_prompt, tools=self._tools)
        self._intro = backup_intro(settings.tools_enabled)
        window = ChatWindow(self, conversation, self._intro)
        if self._herkos is not None:
            window.enable_folders(self._herkos, self._tools)
        self._model_setup = window.model_setup = ModelSetup(settings, window)
        self._model_setup.begin()
        GLib.idle_add(lambda: window.controller.offer_intro() and False)
        return window

    def do_shutdown(self) -> None:
        for client in self._clients:
            client.close()
        if getattr(self, "_tools", None):
            self._tools.close()
        if getattr(self, "_intro", None):
            self._intro.close()
        if getattr(self, "_model_setup", None):
            self._model_setup.stop()
        if getattr(self, "_herkos", None):
            self._herkos.stop()
        Adw.Application.do_shutdown(self)


def main(argv: list[str] | None = None) -> int:
    setup()
    crash.install()
    GLib.set_application_name("Gloxi")
    app = GloxiApp()
    # Closing the terminal or `kill` must stop the model and Herkos too:
    # quitting runs do_shutdown, which stops them.
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, _quit, app)
    return app.run(sys.argv[:1] if argv is None else argv)


def _quit(app: Adw.Application) -> bool:
    app.quit()
    return GLib.SOURCE_REMOVE


if __name__ == "__main__":
    raise SystemExit(main())
