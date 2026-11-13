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
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from gloxi import crash  # noqa: E402
from gloxi.backup_intro import BackupIntro, backup_intro  # noqa: E402
from gloxi.chat import Conversation  # noqa: E402
from gloxi.config import ConfigError, load_settings  # noqa: E402
from gloxi.gui import (  # noqa: E402
    APP_ID,
    panel,
    style,
    switcher,  # noqa: E402
)
from gloxi.gui import shell as vilna_shell  # noqa: E402
from gloxi.gui.cards import card_view, starter_prompts  # noqa: E402
from gloxi.gui.controller import ChatController, ModelSetup  # noqa: E402
from gloxi.gui.panel import OwlState, OwlStates, PanelPrefs  # noqa: E402
from gloxi.gui.remote import RemoteController, ServiceError  # noqa: E402
from gloxi.gui.shortcuts import FALLBACK_COMMAND, GlobalShortcut  # noqa: E402
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
from gloxi.programs import Program, Programs, programs  # noqa: E402
from gloxi.router import Router  # noqa: E402
from gloxi.service import service_socket  # noqa: E402
from gloxi.tools import file_tools  # noqa: E402
from gloxi.voice import session as voice_session  # noqa: E402
from gloxi.vpn_intro import DESKTOP_IDS, LAUNCHABLE, VpnIntro, vpn_intro  # noqa: E402

CSS = """
.bubble { padding: 8px 12px; border-radius: 12px; }
.bubble.user { background: alpha(@accent_bg_color, 0.15); }
.bubble.assistant { background: alpha(@card_bg_color, 1.0); }

/* The owl and its states (gloxi.gui.panel.OwlState). The motion is only on
   .animated, which is off with the system's "Reduce animation". */
.owl-badge { min-width: 8px; min-height: 8px; border-radius: 999px; }
.owl.waiting .owl-badge { background: #F5C24A; }
.owl.error .owl-badge { background: @error_color; }
.owl.listening image { -gtk-icon-shadow: 0 0 3px @accent_color; }
@keyframes gloxi-breathe { from { opacity: 1; } to { opacity: 0.8; } }
@keyframes gloxi-think { from { opacity: 1; } to { opacity: 0.45; } }
@keyframes gloxi-hop {
  from { -gtk-icon-transform: translateY(0); }
  to { -gtk-icon-transform: translateY(-2px); }
}
.owl.animated.idle image { animation: gloxi-breathe 3s ease-in-out infinite alternate; }
.owl.animated.thinking image { animation: gloxi-think 0.8s ease-in-out infinite alternate; }
.owl.animated.answering image { animation: gloxi-think 0.35s ease-in-out infinite alternate; }
.owl.animated.waiting image { animation: gloxi-hop 0.6s ease-in-out 4 alternate; }

window.gloxi-panel.compact .messages { margin: 6px 10px; }
"""

# The translucent panel: only the background; the bars sit on it without a
# background of their own, so the alpha is not laid on twice.
TRANSLUCENT_CSS = """
window.gloxi-panel {{ background-color: alpha(@window_bg_color, {alpha}); }}
window.gloxi-panel toolbarview > .top-bar,
window.gloxi-panel toolbarview > .bottom-bar,
window.gloxi-panel headerbar {{ background: none; box-shadow: none; }}
"""

# Size keys in the panel, as in the Size buttons.
SIZE_KEYS = {"compact": "<Control>1", "one-sixth": "<Control>2", "one-third": "<Control>3"}


def _on_main_loop(job) -> None:
    GLib.idle_add(lambda: job() and False)


def _idle(func):
    """Run a view callback on the GTK main loop, whatever thread calls it."""

    def wrapper(self, *args):
        GLib.idle_add(lambda: func(self, *args) and False)

    return wrapper


class ChatWindow(Adw.ApplicationWindow):
    def __init__(
        self,
        app: Adw.Application,
        conversation: Conversation | None,
        intro: BackupIntro | None = None,
        *,
        shell: vilna_shell.VilnaShell | None = None,
        prefs: PanelPrefs | None = None,
        service: Path | None = None,
        vpn: VpnIntro | None = None,
    ) -> None:
        super().__init__(application=app, title="Gloxi")
        if service is not None:
            # Vilna: Gloxi is the system service gloxid (ADR 0025); this window
            # only shows it. Events go through the main loop, in order.
            remote = RemoteController(service, self, dispatch=_on_main_loop)
            try:
                remote.connect()
            except ServiceError as exc:
                print(f"gloxi: no Gloxi service at {service}: {exc}", file=sys.stderr)
            self.controller = remote
        else:
            self.controller = ChatController(conversation, self, intro=intro, vpn=vpn)
        self._answer_label: Gtk.Label | None = None
        # The side panel (stage A): its size and look, and the Shell that
        # keeps it at the right edge, when the Vilna extension runs.
        self.shell = shell
        self.prefs = prefs or PanelPrefs()
        self.owl_states = OwlStates()
        self.add_css_class("gloxi-panel")

        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title="Gloxi", subtitle=self.controller.cloud_status())
        header.set_title_widget(self.title)
        header.pack_start(self._make_owl())
        new_button = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=_("New conversation"))
        new_button.connect("clicked", self._on_new)
        header.pack_start(new_button)
        self.panel_button = Gtk.MenuButton(
            icon_name="sidebar-show-right-symbolic",
            tooltip_text=_("Panel size and look"),
            popover=self._make_panel_popover(),
        )
        header.pack_end(self.panel_button)
        # The model switcher (brief 10.3: in the input bar, Ctrl+/), also in the
        # middle of a conversation; it holds from the next message.
        # A list that opens upwards, grouped by where the words go (switcher.py).
        self.model_switch = Gtk.MenuButton(valign=Gtk.Align.CENTER, direction=Gtk.ArrowType.UP)
        self.model_switch.add_css_class("vilna-model-switch")
        self.model_switch.update_property([Gtk.AccessibleProperty.LABEL], [_("Model")])
        self._fill_model_switch()
        # Only with the Herkos bundled in the Flatpak: the folders it may use.
        self.herkos: LocalHerkos | None = None
        self.tools = None
        self.folders_button = Gtk.Button(
            icon_name="folder-symbolic", tooltip_text=_("Folders Gloxi may use"), visible=False
        )
        self.folders_button.connect("clicked", self._on_folders)
        header.pack_end(self.folders_button)
        # Programs from the catalogue, once Herkos says there are any (ADR 0017, step 6).
        self.programs: Programs | None = None
        self.programs_button = Gtk.Button(
            icon_name="view-app-grid-symbolic", tooltip_text=_("Programs for Gloxi"), visible=False
        )
        self.programs_button.connect("clicked", self._on_programs)
        header.pack_end(self.programs_button)
        report_button = Gtk.Button(
            icon_name="mail-message-new-symbolic",
            tooltip_text=_("Report a problem or suggest something"),
        )
        report_button.connect("clicked", lambda *_a: self.controller.report_problem())
        header.pack_end(report_button)

        self.messages = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.messages.add_css_class("messages")
        for side in ("top", "bottom", "start", "end"):
            getattr(self.messages, f"set_margin_{side}")(12)
        self.empty = self._make_empty()
        self.messages.append(self.empty)
        self.scroller = Gtk.ScrolledWindow(vexpand=True, child=self.messages)
        # Small: always the last lines, also right after the panel shrinks.
        # (Scrolled on idle: the adjustment changes in the middle of layout.)
        self.scroller.get_vadjustment().connect(
            "changed",
            lambda *_a: self.prefs.size == "compact" and GLib.idle_add(self._scroll_to_end),
        )

        self.entry = Gtk.Entry(
            hexpand=True,
            placeholder_text=_("Ask Gloxi… (/local or /cloud to pick the model)"),
        )
        self.entry.connect("activate", self._on_send)
        self.send_button = Gtk.Button(icon_name="go-up-symbolic", tooltip_text=_("Send"))
        self.send_button.add_css_class("suggested-action")
        self.send_button.add_css_class("circular")
        self.send_button.connect("clicked", self._on_send)
        # Voice (ADR 0015): press and speak; Stop while an answer is read aloud.
        self.voice: voice_session.VoiceController | None = None
        self.mic_button = Gtk.Button(
            icon_name="audio-input-microphone-symbolic",
            tooltip_text=_("Speak instead of typing (Ctrl+M)"),
            visible=False,
        )
        self.mic_button.add_css_class("circular")
        self.mic_button.connect("clicked", lambda *_a: self.voice and self.voice.press_microphone())
        self.stop_button = Gtk.Button(
            icon_name="media-playback-stop-symbolic",
            tooltip_text=_("Stop reading aloud (Esc)"),
            visible=False,
        )
        self.stop_button.add_css_class("circular")
        self.stop_button.connect("clicked", lambda *_a: self.voice and self.voice.stop_speaking())
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
                action=Gtk.CallbackAction.new(lambda *_a: self.model_switch.popup() or True),
            )
        )
        for size, key in SIZE_KEYS.items():
            shortcuts.add_shortcut(
                Gtk.Shortcut(
                    trigger=Gtk.ShortcutTrigger.parse_string(key),
                    action=Gtk.CallbackAction.new(
                        lambda *_a, s=size: self.set_panel_size(s) or True
                    ),
                )
            )
        shortcuts.add_shortcut(
            Gtk.Shortcut(
                trigger=Gtk.ShortcutTrigger.parse_string("<Control>m"),
                action=Gtk.CallbackAction.new(lambda *_a: self._on_mic_key()),
            )
        )
        shortcuts.add_shortcut(
            Gtk.Shortcut(
                trigger=Gtk.ShortcutTrigger.parse_string("Escape"),
                action=Gtk.CallbackAction.new(lambda *_a: self._on_escape()),
            )
        )
        self.add_controller(shortcuts)
        bottom.append(self.stop_button)
        bottom.append(self.mic_button)
        bottom.append(self.send_button)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_a: self._owl(lambda s: s.focus(True)))
        focus.connect("leave", lambda *_a: self._owl(lambda s: s.focus(False)))
        self.entry.add_controller(focus)

        self.toasts = Adw.ToastOverlay(child=self.scroller)
        # The bundled model: download progress and start-up (ModelSetup).
        self.model_banner = Adw.Banner(revealed=False)
        self.model_banner.connect("button-clicked", self._on_cancel_download)
        self.model_setup: ModelSetup | None = None
        self.voice_banner = Adw.Banner(revealed=False)
        self.voice_banner.connect(
            "button-clicked", lambda *_a: self.voice and self.voice.cancel_download()
        )
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        view.add_top_bar(self.model_banner)
        view.add_top_bar(self.voice_banner)
        view.add_bottom_bar(bottom)
        self.set_content(view)
        self.entry.grab_focus()

        self._translucency = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self._translucency, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        style = Adw.StyleManager.get_default()
        style.connect("notify::high-contrast", lambda *_a: self._apply_look())
        Gtk.Settings.get_default().connect(
            "notify::gtk-enable-animations", lambda *_a: self._show_owl()
        )
        self.connect("close-request", self._on_close_request)
        self._apply_look()
        self._apply_size()
        self._show_owl()

    # -- the side panel: size, look, the owl ------------------------------------

    def _make_empty(self) -> Gtk.Widget:
        """The empty panel (brief 10.4): the owl, a question and a few starts."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, vexpand=True)
        box.set_valign(Gtk.Align.CENTER)
        box.add_css_class("vilna-empty")
        box.append(Gtk.Image(icon_name=APP_ID, pixel_size=64))
        question = Gtk.Label(label=_("How can I help?"))
        question.add_css_class("title-2")
        box.append(question)
        starts = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3)
        starts.set_halign(Gtk.Align.CENTER)
        for text in starter_prompts():
            chip = Gtk.Button(label=text)
            chip.add_css_class("pill")
            chip.add_css_class("vilna-starter")
            chip.connect("clicked", lambda _b, t=text: self._start_with(t))
            starts.append(chip)
        box.append(starts)
        return box

    def _start_with(self, text: str) -> None:
        self.entry.set_text(text)
        self._on_send()

    def _make_owl(self) -> Gtk.Widget:
        self.owl_image = Gtk.Image(icon_name=APP_ID, pixel_size=24)
        badge = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.END)
        badge.add_css_class("owl-badge")
        self.owl = Gtk.Overlay(child=self.owl_image, valign=Gtk.Align.CENTER)
        self.owl.add_overlay(badge)
        self.owl.add_css_class("owl")
        return self.owl

    def _owl(self, change) -> None:
        change(self.owl_states)
        self._show_owl()

    def _show_owl(self) -> None:
        state = self.owl_states.state
        for other in OwlState:
            self.owl.remove_css_class(other.value)
        self.owl.add_css_class(state.value)
        if Gtk.Settings.get_default().get_property("gtk-enable-animations"):
            self.owl.add_css_class("animated")
        else:
            self.owl.remove_css_class("animated")
        text = panel.state_text(state)
        self.owl.set_tooltip_text(text)
        self.owl.update_property([Gtk.AccessibleProperty.LABEL], [text])

    def _make_panel_popover(self) -> Gtk.Popover:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(6)
        heading = Gtk.Label(label=_("Size"), xalign=0)
        heading.add_css_class("heading")
        box.append(heading)
        sizes = Gtk.Box(homogeneous=True)
        sizes.add_css_class("linked")
        self.size_buttons: dict[str, Gtk.ToggleButton] = {}
        labels = {
            "compact": (_("Small"), _("The last lines of the chat and the input")),
            "one-sixth": (_("1/6"), _("A sixth of the screen")),
            "one-third": (_("1/3"), _("A third of the screen")),
        }
        group = None
        for size, (label, tip) in labels.items():
            button = Gtk.ToggleButton(label=label, tooltip_text=f"{tip} ({SIZE_KEYS[size]})")
            if group is None:
                group = button
            else:
                button.set_group(group)
            button.connect("toggled", self._on_size_toggled, size)
            sizes.append(button)
            self.size_buttons[size] = button
        box.append(sizes)
        self.custom_note = Gtk.Label(
            label=_("Your own size. Drag an edge of the panel to change it."),
            wrap=True,
            xalign=0,
            max_width_chars=30,
        )
        self.custom_note.add_css_class("dim-label")
        self.custom_note.add_css_class("caption")
        box.append(self.custom_note)

        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows.add_css_class("boxed-list")
        self.translucent_row = Adw.SwitchRow(title=_("Translucent"))
        self.translucent_row.set_active(self.prefs.translucent)
        self.translucent_row.connect("notify::active", self._on_translucent)
        rows.append(self.translucent_row)
        self.opacity = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, panel.MIN_OPACITY, 1.0, 0.05
        )
        self.opacity.set_value(self.prefs.opacity)
        self.opacity.set_size_request(120, -1)
        self.opacity.connect("value-changed", self._on_opacity)
        self.opacity_row = Adw.ActionRow(title=_("Background"))
        self.opacity_row.add_suffix(self.opacity)
        self.opacity.update_property([Gtk.AccessibleProperty.LABEL], [_("Background opacity")])
        rows.append(self.opacity_row)
        # Only the Shell can keep a window above others on Wayland.
        self.above_row = Adw.SwitchRow(
            title=_("Above other windows"), visible=self.shell is not None
        )
        if self.shell is not None:
            self.above_row.set_active(self.shell.keep_above)
        self.above_row.connect("notify::active", self._on_keep_above)
        rows.append(self.above_row)
        box.append(rows)

        # Voice: off until the person turns it on (ADR 0015, section 2).
        self.voice_rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, visible=False)
        self.voice_rows.add_css_class("boxed-list")
        self.read_aloud_row = Adw.SwitchRow(title=_("Read answers aloud"))
        self.read_aloud_row.connect("notify::active", self._on_read_aloud)
        self.voice_rows.append(self.read_aloud_row)
        self.auto_send_row = Adw.SwitchRow(
            title=_("Send dictated text at once"),
            subtitle=_("Otherwise it waits in the field for you to check and send"),
        )
        self.auto_send_row.connect("notify::active", self._on_auto_send)
        self.voice_rows.append(self.auto_send_row)
        box.append(self.voice_rows)
        self._sync_size_buttons()
        box.set_size_request(320, -1)
        return Gtk.Popover(child=box)

    def _sync_size_buttons(self) -> None:
        self._syncing = True
        for size, button in self.size_buttons.items():
            button.set_active(size == self.prefs.size)
        self._syncing = False
        self.custom_note.set_visible(self.prefs.size == "custom")

    def _on_size_toggled(self, button: Gtk.ToggleButton, size: str) -> None:
        if button.get_active() and not getattr(self, "_syncing", False):
            self.set_panel_size(size)

    def set_panel_size(self, size: str) -> None:
        if size not in panel.SIZES or size == self.prefs.size:
            return
        self._set_prefs(size=size)
        self._sync_size_buttons()
        self._apply_size()

    def _screen(self) -> tuple[int, int]:
        display = Gdk.Display.get_default()
        surface = self.get_surface()
        monitor = display.get_monitor_at_surface(surface) if surface else None
        if monitor is None:
            monitors = display.get_monitors()
            monitor = monitors.get_item(0) if monitors.get_n_items() else None
        if monitor is None:
            return 1920, 1080
        geometry = monitor.get_geometry()
        return geometry.width, geometry.height

    def _apply_size(self) -> None:
        width, height = panel.panel_size(self.prefs, *self._screen())
        self.set_default_size(width, height)
        if self.prefs.size == "compact":
            self.add_css_class("compact")
        else:
            self.remove_css_class("compact")
        if self.shell is not None:
            # The Shell keeps the window at the right edge, at the work
            # area's height; without it GNOME chooses the place.
            self.shell.set_panel_size(self.prefs.size)

    def _apply_look(self) -> None:
        high_contrast = Adw.StyleManager.get_default().get_high_contrast()
        alpha = panel.background_opacity(self.prefs, high_contrast)
        self._translucency.load_from_string(
            TRANSLUCENT_CSS.format(alpha=f"{alpha:.2f}") if alpha < 1.0 else ""
        )
        self.translucent_row.set_sensitive(not high_contrast)
        self.translucent_row.set_subtitle(_("Off in high contrast") if high_contrast else "")
        self.opacity_row.set_sensitive(self.prefs.translucent and not high_contrast)

    def _set_prefs(self, **changes) -> None:
        self.prefs = panel.clean(self.prefs, changes)
        panel.save_prefs(self.prefs)

    def _on_translucent(self, row, _pspec) -> None:
        self._set_prefs(translucent=row.get_active())
        self._apply_look()

    def _on_opacity(self, scale) -> None:
        self._set_prefs(opacity=scale.get_value())
        self._apply_look()

    def _on_keep_above(self, row, _pspec) -> None:
        if self.shell is not None:
            self.shell.set_keep_above(row.get_active())

    # -- voice (ADR 0015) ------------------------------------------------------

    def enable_voice(self, voice: voice_session.VoiceController) -> None:
        self.voice = voice
        self.mic_button.set_visible(True)
        self.voice_rows.set_visible(True)
        self._syncing_voice = True
        self.read_aloud_row.set_active(voice.prefs.read_aloud)
        self.auto_send_row.set_active(voice.prefs.auto_send)
        self._syncing_voice = False
        # The speech model leaves memory after five idle minutes.
        GLib.timeout_add_seconds(60, lambda: bool(voice.unload_if_idle() or True))

    def _on_mic_key(self) -> bool:
        if self.voice is None:
            return False
        self.voice.press_microphone()
        return True

    def _on_escape(self) -> bool:
        """Esc drops the phrase being heard, or silences Gloxi; else it does what it did."""
        if self.voice is not None and self.voice.listening:
            self.voice.cancel_listening()
            return True
        if self.voice is not None and self.voice.speaking:
            self.voice.stop_speaking()
            return True
        return False

    def _on_read_aloud(self, row, _pspec) -> None:
        if self.voice is not None and not getattr(self, "_syncing_voice", False):
            self.voice.set_read_aloud(row.get_active())

    def _on_auto_send(self, row, _pspec) -> None:
        if self.voice is not None and not getattr(self, "_syncing_voice", False):
            self.voice.set_auto_send(row.get_active())

    @_idle
    def ask_voice_download(self, text: str, answer) -> None:
        self._owl(OwlStates.ask)

        def respond(_d, response) -> None:
            self._owl(OwlStates.answered)
            answer(response == "download")

        dialog = Adw.AlertDialog(heading=_("Download Gloxi's voice?"), body=text)
        dialog.add_response("later", _("Not now"))
        dialog.add_response("download", _("Download"))
        dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("download")
        dialog.set_close_response("later")
        dialog.connect("response", respond)
        dialog.present(self)

    @_idle
    def show_voice_progress(self, text: str, fraction: float | None) -> None:
        self.voice_banner.set_title(text)
        self.voice_banner.set_button_label(_("Cancel") if fraction is not None else None)
        self.voice_banner.set_revealed(True)

    @_idle
    def hide_voice_progress(self) -> None:
        self.voice_banner.set_revealed(False)

    @_idle
    def set_listening(self, listening: bool) -> None:
        self._owl(lambda s: s.set_mic(listening))
        self.mic_button.set_icon_name(
            "media-record-symbolic" if listening else "audio-input-microphone-symbolic"
        )
        self.mic_button.set_tooltip_text(
            _("Listening… press again to finish (Esc to cancel)")
            if listening
            else _("Speak instead of typing (Ctrl+M)")
        )
        if listening:
            self.mic_button.add_css_class("destructive-action")
        else:
            self.mic_button.remove_css_class("destructive-action")

    @_idle
    def set_speaking(self, speaking: bool) -> None:
        self._owl(lambda s: s.set_speaking(speaking))
        self.stop_button.set_visible(speaking)

    @_idle
    def put_dictation(self, text: str, send: bool) -> None:
        current = self.entry.get_text().rstrip()
        self.entry.set_text(f"{current} {text}" if current else text)
        self.entry.grab_focus_without_selecting()
        self.entry.set_position(-1)
        if send:
            self._on_send()

    def _on_close_request(self, *_args) -> bool:
        # A size dragged by hand is remembered as the person's own size.
        prefs = panel.after_resize(self.prefs, self.get_width(), self.get_height(), *self._screen())
        if prefs != self.prefs:
            self.prefs = prefs
            panel.save_prefs(prefs)
            if self.shell is not None:
                self.shell.set_panel_size(prefs.size)
        return False

    # -- user actions -------------------------------------------------------

    def _on_send(self, *_args) -> None:
        if self.controller.submit(self.entry.get_text()):
            self.entry.set_text("")

    def _on_new(self, *_args) -> None:
        if self.controller.busy:
            return
        self.controller.reset()
        while (child := self.messages.get_last_child()) is not None and child is not self.empty:
            self.messages.remove(child)
        self.empty.set_visible(True)

    # -- model switcher -----------------------------------------------------------

    def _fill_model_switch(self) -> None:
        self._choices = self.controller.choices()
        current = self.controller.current_choice()
        chosen = next((c for c in self._choices if c.mode == current), self._choices[0])
        self.model_switch.set_label(switcher.button_label(chosen))
        self.model_switch.set_tooltip_text(chosen.detail)
        self.model_switch.set_popover(self._make_model_popover(current))
        self.title.set_subtitle(self.controller.cloud_status())

    def _make_model_popover(self, current: str) -> Gtk.Popover:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(6)
        heading = Gtk.Label(label=_("Model for the next messages"), xalign=0)
        heading.add_css_class("caption-heading")
        heading.add_css_class("dim-label")
        box.append(heading)
        popover = Gtk.Popover(child=box)
        for section in switcher.sections(self._choices):
            if section.title:
                title = Gtk.Label(label=section.title, xalign=0, margin_top=6)
                title.add_css_class("caption")
                title.add_css_class("dim-label")
                box.append(title)
            if section.note:
                note = Gtk.Label(label=section.note, xalign=0, margin_start=26)
                note.add_css_class("dim-label")
                box.append(note)
            for choice in section.choices:
                box.append(self._model_row(choice, choice.mode == current, popover))
        hint = Gtk.Label(label=_("Ctrl + / opens this list"), xalign=0, margin_top=6)
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        box.append(hint)
        return popover

    def _model_row(self, choice, chosen: bool, popover: Gtk.Popover) -> Gtk.Button:
        row = Gtk.Box(spacing=8)
        mark = Gtk.Image.new_from_icon_name("object-select-symbolic")
        mark.set_opacity(1.0 if chosen else 0.0)
        mark.set_valign(Gtk.Align.START)
        row.append(mark)
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        name = Gtk.Label(label=choice.label, xalign=0)
        name.add_css_class("heading")
        words.append(name)
        detail = Gtk.Label(label=choice.detail, xalign=0, wrap=True, max_width_chars=34)
        detail.add_css_class("caption")
        detail.add_css_class("dim-label")
        words.append(detail)
        row.append(words)
        chip = Gtk.Label(label=switcher.tag(choice.mode), valign=Gtk.Align.START)
        chip.add_css_class("vilna-tag")
        chip.add_css_class(choice.mode)
        row.append(chip)
        button = Gtk.Button(child=row)
        button.add_css_class("flat")
        if chosen:
            button.add_css_class("vilna-chosen")
        button.connect("clicked", lambda *_a: self._on_model_chosen(choice, popover))
        return button

    def _on_model_chosen(self, choice, popover: Gtk.Popover) -> None:
        popover.popdown()
        if choice.mode == self.controller.current_choice():
            return
        if not self.controller.set_mode(choice.mode):
            self.show_error(_("The cloud is turned off in the settings."))
        else:
            self._system_line(switcher.switch_note(choice))
            if self.controller.held_back():
                self._ask_share_history()
        GLib.idle_add(lambda: self._fill_model_switch() and False)

    def _ask_share_history(self) -> None:
        """Brief 10.3, item 7: the earlier messages go to the cloud only if the person says so."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("vilna-consent-card")
        title = Gtk.Label(label=_("Send the conversation to the cloud?"), xalign=0, wrap=True)
        title.add_css_class("heading")
        card.append(title)
        card.append(
            Gtk.Label(
                label=_(
                    "The cloud model does not see the earlier messages of this conversation. "
                    "Send them too, so that it knows the context?"
                ),
                xalign=0,
                wrap=True,
            )
        )
        # One above the other: the panel can be narrow and the words are long.
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        only_new = Gtk.Button(label=_("Only the new message"))
        with_context = Gtk.Button(label=_("Send with context"))
        with_context.add_css_class("suggested-action")
        buttons.append(with_context)
        buttons.append(only_new)
        card.append(buttons)

        def answer(share: bool) -> None:
            self.controller.share_history(share)
            self.messages.remove(card)
            self._system_line(
                _("The earlier messages will go to the cloud with the next one.")
                if share
                else _("The earlier messages stay on this computer.")
            )

        only_new.connect("clicked", lambda *_a: answer(False))
        with_context.connect("clicked", lambda *_a: answer(True))
        self._add_message(card)

    def _system_line(self, text: str) -> None:
        """A quiet line across the conversation (a model switch)."""
        line = Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER)
        line.add_css_class("vilna-system-line")
        self._add_message(line)

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

    # -- programs for Gloxi (Herkos keeps the switches) --------------------------

    def enable_programs(self, programs: Programs) -> None:
        """Show the button if Herkos lists any catalogue program; asked off the UI thread."""
        self.programs = programs

        def job() -> None:
            if programs.list():
                GLib.idle_add(lambda: self.programs_button.set_visible(True) and False)

        threading.Thread(target=job, daemon=True).start()

    def _on_programs(self, *_args) -> None:
        programs = self.programs
        if programs is None:
            return

        # Asked off the UI thread; the dialog opens filled, so it gets its size right.
        def job() -> None:
            listed = programs.list()
            GLib.idle_add(lambda: self._show_programs(listed) and False)

        threading.Thread(target=job, daemon=True).start()

    def _show_programs(self, listed: list[Program]) -> None:
        dialog = Adw.PreferencesDialog(title=_("Programs for Gloxi"))
        page = Adw.PreferencesPage()
        self._programs_group = Adw.PreferencesGroup(
            description=_(
                "Gloxi can use only the programs you turn on here. Herkos asks you "
                "before turning one on, and every action still goes through it."
            )
        )
        self._hidden_group = Adw.PreferencesGroup(
            title=_("Actions hidden from Gloxi"),
            description=_("Herkos found something suspicious in how the program describes them."),
            visible=False,
        )
        page.add(self._programs_group)
        page.add(self._hidden_group)
        dialog.add(page)
        self._program_rows: list = []
        self._fill_programs(listed)
        dialog.present(self)

    def _load_programs(self) -> None:
        programs = self.programs
        if programs is None:
            return

        def job() -> None:
            listed = programs.list()
            GLib.idle_add(lambda: self._fill_programs(listed) and False)

        threading.Thread(target=job, daemon=True).start()

    def _fill_programs(self, listed: list[Program]) -> None:
        for group, row in self._program_rows:
            group.remove(row)
        self._program_rows = []
        if not listed:
            row = Adw.ActionRow(title=_("No programs for Gloxi are installed."))
            self._programs_group.add(row)
            self._program_rows.append((self._programs_group, row))
        for program in listed:
            row = Adw.SwitchRow(title=program.title, subtitle=program.status())
            row.set_active(program.enabled)
            row.connect("notify::active", self._on_program_switch, program)
            self._programs_group.add(row)
            self._program_rows.append((self._programs_group, row))
            for hidden in program.hidden:
                why = "; ".join(hidden.why)
                h = Adw.ActionRow(title=f"{program.title}: {hidden.operation}", subtitle=why)
                self._hidden_group.add(h)
                self._program_rows.append((self._hidden_group, h))
        self._hidden_group.set_visible(any(p.hidden for p in listed))

    def _on_program_switch(self, row, _pspec, program: Program) -> None:
        on = row.get_active()
        if on == program.enabled or self.programs is None:
            return
        row.set_sensitive(False)
        programs = self.programs

        def job() -> None:
            change = programs.set(program.tool, on)
            if change.error:
                self.show_error(_("Herkos could not change it: {why}").format(why=change.error))
            elif change.refused:
                self.show_note(_("Not turned on: you said no in the Herkos window."))
            elif change.restart_needed and self.herkos is not None:
                self._restart_herkos()
            elif change.restart_needed:
                self.show_note(_("It starts the next time Herkos starts."))
            GLib.idle_add(lambda: self._load_programs() and False)

        threading.Thread(target=job, daemon=True).start()

    def _restart_herkos(self) -> None:
        """The Herkos in the Flatpak starts the program it just turned on."""
        try:
            self.herkos.restart()
        except Exception as exc:  # noqa: BLE001 - shown to the person
            self.show_error(_("Herkos could not restart: {why}").format(why=exc))
            return
        if self.tools is not None:
            self.tools.set_roots(allowed_folders())

    def _on_cancel_download(self, *_args) -> None:
        if self.model_setup is not None:
            self.model_setup.cancel()

    # -- ModelSetupView -----------------------------------------------------

    @_idle
    def ask_model_download(self, text: str, answer) -> None:
        self._owl(OwlStates.ask)

        def respond(_d, response) -> None:
            self._owl(OwlStates.answered)
            answer(response == "download")

        dialog = Adw.AlertDialog(heading=_("Download the language model?"), body=text)
        dialog.add_response("later", _("Not now"))
        dialog.add_response("download", _("Download"))
        dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("download")
        dialog.set_close_response("later")
        dialog.connect("response", respond)
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
        self._add_message(box)
        return label

    def _add_message(self, widget: Gtk.Widget) -> None:
        self.empty.set_visible(False)
        self.messages.append(widget)
        GLib.idle_add(self._scroll_to_end)

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
        if piece and self.owl_states.busy and not self.owl_states.answering:
            self._owl(OwlStates.got_piece)
        if self._answer_label is not None:
            self._answer_label.set_text(self._answer_text)
        self._scroll_to_end()

    @_idle
    def finish_answer(self) -> None:
        if self._answer_label is not None and not self._answer_text:
            self._answer_label.get_parent().set_visible(False)
        if self._answer_label is not None and self._answer_text and self.voice is not None:
            self.voice.speak(self._answer_text)
        self._answer_label = None

    @_idle
    def show_error(self, text: str) -> None:
        self._owl(OwlStates.failed)
        self.toasts.add_toast(Adw.Toast(title=text, timeout=6))

    @_idle
    def show_note(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    @_idle
    def show_action(self, card: dict) -> None:
        """An action card in the conversation (gloxi.gui.cards)."""
        view = card_view(card)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("vilna-action-card")
        box.add_css_class(view.status)
        head = Gtk.Box(spacing=8)
        title = Gtk.Label(label=view.title, xalign=0, wrap=True, max_width_chars=40)
        title.add_css_class("heading")
        title.set_hexpand(True)
        head.append(title)
        status = Gtk.Label(label=view.status_label, valign=Gtk.Align.START)
        status.add_css_class("vilna-status")
        status.add_css_class(view.status)
        head.append(status)
        box.append(head)
        for text, css in ((view.target, "target"), (view.detail, "dim-label")):
            if text:
                line = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True)
                line.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                line.set_max_width_chars(48)
                line.add_css_class(css)
                box.append(line)
        if view.warnings:
            # Keros found something in the content: its own block, no panic.
            warn = Gtk.Box(spacing=8)
            warn.add_css_class("vilna-warning")
            icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
            icon.set_valign(Gtk.Align.START)
            warn.append(icon)
            words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            heading = Gtk.Label(label=_("Suspicious content"), xalign=0)
            heading.add_css_class("title")
            words.append(heading)
            for w in view.warnings:
                words.append(
                    Gtk.Label(label=_("The content {what}.").format(what=w), xalign=0, wrap=True)
                )
            strict = _("Herkos checks Gloxi's next actions more strictly.")
            words.append(Gtk.Label(label=strict, xalign=0, wrap=True))
            warn.append(words)
            box.append(warn)
        self._add_message(box)

    @_idle
    def open_uri(self, url: str) -> None:
        # Through the OpenURI portal in Flatpak; the address is also in the chat.
        Gtk.UriLauncher.new(url).launch(self, None, None, None)

    @_idle
    def launch_app(self, app_id: str) -> None:
        # Only the providers' own VPN apps (ADR 0027): Gloxi never starts
        # anything else on someone's say-so.
        if app_id not in LAUNCHABLE:
            return
        for desktop_id in DESKTOP_IDS[app_id]:
            info = Gio.DesktopAppInfo.new(f"{desktop_id}.desktop")
            if info is not None:
                break
        else:
            self.show_error(_("The app {app} is not installed.").format(app=app_id))
            return
        try:
            info.launch([], None)
        except GLib.Error as exc:
            self.show_error(str(exc))

    @_idle
    def set_busy(self, busy: bool) -> None:
        self._owl(lambda s: s.set_busy(busy))
        self.send_button.set_sensitive(not busy)
        if not busy:
            self.entry.grab_focus()


class GloxiApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self._clients: list[ChatClient] = []
        self._shortcut: GlobalShortcut | None = None
        # `gloxi-gtk --toggle`: what a key of one's own runs where the
        # GlobalShortcuts portal is missing (ADR 0023, section 5).
        self.add_main_option(
            "toggle",
            0,
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            _("Show Gloxi, or hide it if it is in front"),
            None,
        )

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        if command_line.get_options_dict().contains("toggle"):
            self.toggle()
        else:
            self.activate()
        return 0

    def do_activate(self) -> None:
        window = self.get_active_window()
        if window is None:
            window = self._build_window()
        if window is not None:
            window.present()
            self._bind_shortcut()

    def toggle(self) -> None:
        """Hide Gloxi when it is in front; otherwise bring it up, ready to type."""
        window = self.get_active_window()
        if window is not None and window.get_visible() and window.is_active():
            window.set_visible(False)  # the conversation stays as it is
            return
        self.activate()
        window = self.get_active_window()
        if isinstance(window, ChatWindow):
            window.entry.grab_focus()

    def _bind_shortcut(self) -> None:
        if self._shortcut is not None:
            return
        self._shortcut = GlobalShortcut(
            on_activated=self.toggle,
            on_unavailable=lambda: print(
                f"gloxi: no global shortcut; bind a key to: {FALLBACK_COMMAND}", file=sys.stderr
            ),
        )
        self._shortcut.start()

    def _build_window(self) -> ChatWindow | None:
        try:
            settings = load_settings()
        except ConfigError as exc:
            print(f"gloxi: {exc}", file=sys.stderr)
            self.quit()
            return None
        # Running from a source checkout: the owl from data/icons.
        icons = Path(__file__).resolve().parents[3] / "data" / "icons"
        if icons.is_dir():
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(icons))
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        style.install()  # Vilna's colours and components (docs/design/style)
        sock = service_socket()
        if sock.exists():
            # Vilna: the conversation, the models and Herkos are gloxid's.
            window = ChatWindow(
                self, None, shell=vilna_shell.connect(), prefs=panel.load_prefs(), service=sock
            )
            GLib.idle_add(lambda: window.controller.offer_intro() and False)
            return window
        local = ChatClient(settings.local)
        cloud = ChatClient(settings.cloud) if settings.cloud else None
        self._clients = [c for c in (local, cloud) if c]
        router = Router(local, cloud, settings.router, guard_factory=guard_factory(settings.router))
        self._herkos = start_if_needed(settings.tools_enabled)
        self._tools = file_tools(settings.tools_enabled)
        conversation = Conversation(router, settings.system_prompt, tools=self._tools)
        self._intro = backup_intro(settings.tools_enabled)
        self._vpn = vpn_intro(settings.tools_enabled)
        window = ChatWindow(
            self,
            conversation,
            self._intro,
            shell=vilna_shell.connect(),
            prefs=panel.load_prefs(),
            vpn=self._vpn,
        )
        if self._herkos is not None:
            window.enable_folders(self._herkos, self._tools)
        catalogue = programs(settings.tools_enabled)
        if catalogue is not None:
            window.enable_programs(catalogue)
        if voice_available():
            self._voice = voice_session.VoiceController(window, prefs=voice_session.load_prefs())
            window.enable_voice(self._voice)
        self._model_setup = window.model_setup = ModelSetup(settings, window)
        self._model_setup.begin()
        GLib.idle_add(lambda: window.controller.offer_intro() and False)
        return window

    def do_shutdown(self) -> None:
        if self._shortcut is not None:
            self._shortcut.stop()
        for client in getattr(self, "_clients", []):
            client.close()
        window = self.get_active_window()
        if isinstance(window, ChatWindow) and isinstance(window.controller, RemoteController):
            window.controller.close()
        if getattr(self, "_tools", None):
            self._tools.close()
        if getattr(self, "_intro", None):
            self._intro.close()
        if getattr(self, "_vpn", None):
            self._vpn.close()
        if getattr(self, "_model_setup", None):
            self._model_setup.stop()
        if getattr(self, "_voice", None):
            self._voice.stop()
        if getattr(self, "_herkos", None):
            self._herkos.stop()
        Adw.Application.do_shutdown(self)


def voice_available() -> bool:
    """The voice engine and GStreamer are installed (always in the Flatpak)."""
    import importlib.util

    if importlib.util.find_spec("sherpa_onnx") is None:
        return False
    try:
        gi.require_version("Gst", "1.0")
    except ValueError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    setup()
    crash.install()
    GLib.set_application_name("Gloxi")
    app = GloxiApp()
    # Closing the terminal or `kill` must stop the model and Herkos too:
    # quitting runs do_shutdown, which stops them.
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, _quit, app)
    return app.run(sys.argv if argv is None else argv)


def _quit(app: Adw.Application) -> bool:
    app.quit()
    return GLib.SOURCE_REMOVE


if __name__ == "__main__":
    raise SystemExit(main())
