# SPDX-License-Identifier: GPL-3.0-or-later
"""The Gloxi chat window (GTK 4 + libadwaita)."""

from __future__ import annotations

import sys
from gettext import gettext as _

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from gloxi.backup_intro import BackupIntro, backup_intro  # noqa: E402
from gloxi.chat import Conversation  # noqa: E402
from gloxi.config import ConfigError, load_settings  # noqa: E402
from gloxi.gui import APP_ID  # noqa: E402
from gloxi.gui.controller import ChatController, ModelSetup, cloud_status  # noqa: E402
from gloxi.i18n import setup  # noqa: E402
from gloxi.llm import ChatClient  # noqa: E402
from gloxi.privacy import guard_factory  # noqa: E402
from gloxi.router import Router  # noqa: E402
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
        bottom.append(self.entry)
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
        self._tools = file_tools(settings.tools_enabled)
        conversation = Conversation(router, settings.system_prompt, tools=self._tools)
        self._intro = backup_intro(settings.tools_enabled)
        window = ChatWindow(self, conversation, self._intro)
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
        Adw.Application.do_shutdown(self)


def main(argv: list[str] | None = None) -> int:
    setup()
    GLib.set_application_name("Gloxi")
    app = GloxiApp()
    return app.run(sys.argv[:1] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
