# SPDX-License-Identifier: GPL-3.0-or-later
"""The conversation window: chat with Gloxi on the left, the plan on the right.

    vilna-installer-chat [--plan PATH] [--window] [--demo empty|windows]

image/docs/installer.md, sections 1–3. The plan is always visible, and the
person can change any part of it with «Change», which takes the conversation
back to that step. Passwords are typed into protected fields of this window
(``Gtk.PasswordEntry``; the Vilna Shell extension switches them to the
Latin layout), never into the chat: the plan gets ``"entered"`` and the
secret stays in this process's memory (``Secrets``) for the installation
engine.

«Check and confirm the plan» opens the Herkos confirmation screen
(``confirm_window.ask``) from this same process, so the secrets are still in
its ``Vault`` for the engine. Exit status: 0 when the person confirmed the
plan there (the installation engine, step 13, comes next), 3 for «Without
the assistant» or when there is no model (the classic wizard goes on with the
same plan), 1 when the window was closed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402
from vilna_mcp_installer import plan as plans  # noqa: E402

from vilna_installer import confirm_window, start, summary  # noqa: E402
from vilna_installer import conversation as conv  # noqa: E402
from vilna_installer.vault import Vault  # noqa: E402

APP_ID = "io.github.stdenis1980.VilnaInstaller.Chat"
EXIT_CONFIRM, EXIT_CLOSED, EXIT_WIZARD = 0, 1, 3
MIN_PASSWORD = 8

CSS = """
.vilna-chat { background-color: @view_bg_color; }
.vilna-bubble { padding: 10px 14px; border-radius: 16px; }
.vilna-gloxi { background-color: alpha(@accent_bg_color, 0.12); }
.vilna-person { background-color: @accent_bg_color; color: @accent_fg_color; }
.vilna-card { padding: 14px; border-radius: 16px;
    border: 1px solid alpha(@view_fg_color, 0.15); background-color: @card_bg_color; }
.vilna-plan { padding: 18px; background-color: @window_bg_color; }
.vilna-plan-title { font-weight: 800; font-size: 15pt; }
.vilna-section { font-weight: 700; }
.vilna-value { opacity: 0.8; }
.vilna-pending { opacity: 0.55; font-style: italic; }
.vilna-current { border-left: 3px solid @accent_bg_color; padding-left: 8px; }
.vilna-go { background-color: #F5C24A; color: #13213A; font-weight: 800; padding: 10px 22px; }
"""


def _once(fn):
    """For GLib.idle_add: grab_focus returns True, which would run it again forever."""

    def run() -> bool:
        fn()
        return False

    return run


# The protected fields' secrets: one vault for the conversation, the classic
# wizard and the Herkos screen of the same process (vault.py).
Secrets = Vault


def connect_wifi(ssid: str, password: str, *, demo: bool) -> bool:
    """Connect through NetworkManager; the password goes in a private file, never argv."""
    if demo:
        return bool(password)
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
    fd, path = tempfile.mkstemp(dir=runtime, prefix=".vilna-wifi-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"802-11-wireless-security.psk:{password}\n")
        name = "Vilna install"
        subprocess.run(
            ["nmcli", "connection", "delete", "id", name], capture_output=True, check=False
        )
        made = subprocess.run(
            [
                "nmcli",
                "connection",
                "add",
                "type",
                "wifi",
                "con-name",
                name,
                "ifname",
                "*",
                "ssid",
                ssid,
                "wifi-sec.key-mgmt",
                "wpa-psk",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if made.returncode != 0:
            return False
        up = subprocess.run(
            ["nmcli", "--wait", "30", "connection", "up", "id", name, "passwd-file", path],
            capture_output=True,
            timeout=40,
            check=False,
        )
        return up.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        Path(path).unlink(missing_ok=True)


# The plan in the person's words: the same lines in the wizard and on the Herkos screen.
describe = summary.describe


STEP_OF_SECTION = {
    "languages": "languages",
    "network": "network",
    "disk": "disk",
    "encryption": "encryption",
    "user": "user",
    "time": "time",
    "privacy": "privacy",
}
# The section of the plan each step of the conversation is about.
SECTION_OF_STEP = {
    **{step: section for section, step in STEP_OF_SECTION.items()},
    "survey": "disk",
    "old_files": "disk",
    "old_encrypted": "disk",
}


class ChatWindow(Adw.ApplicationWindow):
    def __init__(
        self,
        app: Adw.Application,
        conversation: conv.Conversation | None,
        plan_path: Path,
        *,
        demo: bool,
        starting: Callable[[], conv.Conversation | None] | None = None,
        demo_kind: str | None = None,
    ) -> None:
        super().__init__(application=app)
        self.conversation = conversation
        self.plan_path = plan_path
        self.demo = demo
        self.demo_kind = demo_kind  # the synthetic computer, for the Herkos screen too
        self.secrets = Vault()
        self.confirmed: confirm_window.confirm.Outcome | None = None
        plan = plans.load(plan_path)
        code = (plan.get("ui") or {}).get("language", "en")
        self._ = start.Texts(code if code in start.CODES else "en")
        _ = self._
        self.set_default_size(1280, 800)
        self.set_title(_(start.TITLE))
        self.busy = False
        self.cards: set[str] = set()
        # The last «Change» pressed: a second press (a double click) asks nothing new.
        self.last_change: str | None = None
        talks = conversation is not None or starting is not None

        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=_(start.TITLE)))
        without = Gtk.Button(label=_(start.WITHOUT))
        without.connect("clicked", lambda *_a: self._finish(EXIT_WIZARD))
        header.pack_end(without)

        # The chat
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.list.set_margin_top(18)
        self.list.set_margin_bottom(18)
        self.list.set_margin_start(24)
        self.list.set_margin_end(24)
        self.scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroll.set_child(self.list)
        self.scroll.add_css_class("vilna-chat")
        self.entry = Gtk.Entry(hexpand=True, placeholder_text=_(conv.MESSAGE))
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], [_(conv.MESSAGE)])
        self.entry.connect("activate", self._on_send)
        self.send = Gtk.Button(label=_(conv.SEND))
        self.send.add_css_class("suggested-action")
        self.send.connect("clicked", self._on_send)
        self.thinking = Gtk.Label(label=_(conv.THINKING), visible=False, xalign=0)
        self.thinking.add_css_class("vilna-pending")
        bar = Gtk.Box(spacing=8, margin_top=8, margin_bottom=12, margin_start=24, margin_end=24)
        bar.append(self.entry)
        bar.append(self.send)
        chat = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        chat.append(self.scroll)
        thinking = Gtk.Box(margin_start=24)
        thinking.append(self.thinking)
        chat.append(thinking)
        chat.append(bar)

        # The plan
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, width_request=380)
        panel.set_hexpand(False)
        panel.add_css_class("vilna-plan")
        title = Gtk.Label(label=_(conv.PLAN), xalign=0)
        title.add_css_class("vilna-plan-title")
        panel.append(title)
        self.rows: dict[str, tuple[Gtk.Box, Gtk.Label]] = {}
        for key, name in conv.SECTIONS.items():
            row = Gtk.Box(spacing=8)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            head = Gtk.Label(label=_(name), xalign=0)
            head.add_css_class("vilna-section")
            value = Gtk.Label(xalign=0, wrap=True)
            texts.append(head)
            texts.append(value)
            row.append(texts)
            if key != "privacy":
                change = Gtk.Button(label=_(conv.CHANGE), valign=Gtk.Align.CENTER)
                change.add_css_class("flat")
                change.update_property(
                    [Gtk.AccessibleProperty.LABEL], [f"{_(conv.CHANGE)}: {_(name)}"]
                )
                change.connect("clicked", self._on_change, key)
                change.set_visible(talks)  # «Change» talks to Gloxi
                row.append(change)
            panel.append(row)
            self.rows[key] = (row, value)
        panel.append(Gtk.Box(vexpand=True))
        self.confirm = Gtk.Button(label=_(conv.CHECK_PLAN), visible=False)
        self.confirm.add_css_class("vilna-go")
        self.confirm.add_css_class("pill")
        self.confirm.connect("clicked", self._on_confirm)
        panel.append(self.confirm)

        body = Gtk.Box()
        body.append(chat)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        body.append(panel)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(body)
        self.set_content(view)

        self._show_plan(plan, None)
        self._ready(False)
        self.waking: Gtk.Widget | None = None
        self.closed = False
        self.connect("close-request", self._on_close)
        if not talks:
            self._say(_(conv.NO_MODEL))
        else:
            if starting is not None:
                # Starting the tools reads Python from the USB stick while the model
                # loads from it too: seconds on a live image. The window shows first
                # and says Gloxi is waking up, so it never stops answering.
                self._show_waking()
            threading.Thread(target=self._wait_for_model, args=(starting,), daemon=True).start()
        GLib.idle_add(_once(self.entry.grab_focus))

    def _on_close(self, *_args) -> bool:
        self.closed = True
        return False

    def _ready(self, ready: bool) -> None:
        self.entry.set_sensitive(ready)
        self.send.set_sensitive(ready)

    def _wait_for_model(
        self, starting: Callable[[], conv.Conversation | None] | None = None
    ) -> None:
        """The model may still be starting (live image): say so and wait, then begin."""
        conversation = self.conversation
        if starting is not None:
            conversation = starting()
            if conversation is None:
                GLib.idle_add(self._no_model)
                return
            GLib.idle_add(self._attach, conversation)
        if conversation is None:
            return
        if not conversation.model.available():
            if starting is None:  # otherwise the window already says so
                GLib.idle_add(self._show_waking)
            if not conv.wait_ready(conversation.model, stopped=lambda: self.closed):
                GLib.idle_add(self._no_model)
                return
        GLib.idle_add(self._model_ready)

    def _attach(self, conversation: conv.Conversation) -> bool:
        self.conversation = conversation
        return False

    def _show_waking(self) -> bool:
        start.log("waiting for the model")
        self._say(self._(conv.WAKING))
        self.waking = self.list.get_last_child()
        self.thinking.set_visible(True)
        return False

    def _model_ready(self) -> bool:
        start.log("model ready")
        if self.waking is not None:
            self.list.remove(self.waking)
            self.waking = None
        self.thinking.set_visible(False)
        if self.conversation is not None:
            self._show(self.conversation.opening())
            self._ready(True)
            self.entry.grab_focus()
        return False

    def _no_model(self) -> bool:
        self.thinking.set_visible(False)
        self._say(self._(conv.NO_MODEL))
        self.conversation = None
        for row, _value in self.rows.values():
            change = row.get_last_child()
            if isinstance(change, Gtk.Button):
                change.set_visible(False)
        return False

    # showing

    def _bubble(self, text: str, mine: bool) -> None:
        label = Gtk.Label(label=text, wrap=True, xalign=0, max_width_chars=60)
        label.add_css_class("vilna-bubble")
        label.add_css_class("vilna-person" if mine else "vilna-gloxi")
        label.set_halign(Gtk.Align.END if mine else Gtk.Align.START)
        self.list.append(label)
        GLib.idle_add(self._scroll_down)

    def _say(self, text: str) -> None:
        self._bubble(text, False)

    def _scroll_down(self) -> bool:
        adj = self.scroll.get_vadjustment()
        adj.set_value(adj.get_upper())
        return False

    def _show(self, reply: conv.Reply) -> None:
        if reply.text:
            self._say(reply.text)
        self._show_plan(reply.plan, reply.step.key)
        for need in reply.needs:
            if need not in self.cards:
                self._card(need, reply.plan)

    def _show_plan(self, plan: dict[str, Any], step: str | None) -> None:
        lines = describe(plan, self._)
        for key, (row, value) in self.rows.items():
            text = lines.get(key)
            value.set_label(text or self._(conv.NOT_DECIDED))
            for css, on in (("vilna-value", bool(text)), ("vilna-pending", not text)):
                (value.add_css_class if on else value.remove_css_class)(css)
            current = SECTION_OF_STEP.get(step or "") == key
            (row.add_css_class if current else row.remove_css_class)("vilna-current")
        needs = self.conversation.needs() if self.conversation else []
        self.confirm.set_visible("confirm" in needs)

    # protected fields

    def _card(self, need: str, plan: dict[str, Any]) -> None:
        _ = self._
        if need == "confirm":
            return
        if need == "partition_editor":
            # The partition editor belongs to the classic wizard (separate work).
            return
        self.cards.add(need)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.START)
        card.add_css_class("vilna-card")
        user = plan.get("user") or {}
        if need == "wifi_password":
            title = _(conv.WIFI_PASSWORD).format(name=(plan.get("network") or {}).get("wifi"))
        elif need == "user_password":
            title = _(conv.PASSWORD).format(name=user.get("username", ""))
        else:
            title = _(conv.PASSPHRASE)
        heading = Gtk.Label(label=title, xalign=0)
        heading.add_css_class("vilna-section")
        card.append(heading)
        first = Gtk.PasswordEntry(show_peek_icon=True, width_chars=32)
        first.update_property([Gtk.AccessibleProperty.LABEL], [title])
        card.append(first)
        second = None
        if need != "wifi_password":
            second = Gtk.PasswordEntry(show_peek_icon=True, width_chars=32)
            second.set_property("placeholder-text", _(conv.REPEAT))
            second.update_property([Gtk.AccessibleProperty.LABEL], [_(conv.REPEAT)])
            card.append(second)
        note = Gtk.Label(label=_(conv.NOT_SEEN), xalign=0, wrap=True)
        note.add_css_class("dim-label")
        card.append(note)
        button = Gtk.Button(
            label=_(conv.CONNECT if need == "wifi_password" else conv.SAVE),
            halign=Gtk.Align.START,
        )
        button.add_css_class("suggested-action")
        card.append(button)
        button.connect("clicked", self._on_secret, need, card, first, second, note)
        first.connect("activate", lambda *_a: button.emit("clicked"))
        self.list.append(card)
        GLib.idle_add(_once(first.grab_focus))

    def _on_secret(self, button, need, card, first, second, note) -> None:
        _ = self._
        value = first.get_text()
        if need == "wifi_password":
            ssid = (plans.load(self.plan_path).get("network") or {}).get("wifi", "")
            note.set_label(_(conv.CONNECTING))
            button.set_sensitive(False)

            def work() -> None:
                ok = connect_wifi(ssid, value, demo=self.demo)
                GLib.idle_add(self._secret_done, need, card, note, button, ok, value)

            threading.Thread(target=work, daemon=True).start()
            return
        if len(value) < MIN_PASSWORD:
            note.set_label(_(conv.SHORT))
            return
        if second is not None and second.get_text() != value:
            note.set_label(_(conv.MISMATCH))
            return
        self._secret_done(need, card, note, button, True, value)

    def _secret_done(self, need, card, note, button, ok, value) -> bool:
        _ = self._
        if not ok:
            note.set_label(_(conv.CONNECT_FAILED))
            button.set_sensitive(True)
            return False
        which = {"wifi_password": "network", "user_password": "user", "passphrase": "passphrase"}
        self.secrets.put(which[need], value)
        plan = plans.load(self.plan_path)
        plans.mark_entered(plan, which[need])
        plans.save(plan, self.plan_path)
        self.list.remove(card)
        self.cards.discard(need)
        self.last_change = None
        self._turn(None)
        return False

    # the conversation

    def _on_change(self, _button, key: str) -> None:
        if self.conversation is None or self.busy or self.last_change == key:
            return
        self.last_change = key
        # What the person did shows in the chat like a message of theirs.
        self._bubble(f"{self._(conv.CHANGE)}: {self._(conv.SECTIONS[key])}", True)
        self.conversation.goto(STEP_OF_SECTION[key])
        self._turn(None)

    def _on_send(self, *_args) -> None:
        text = self.entry.get_text().strip()
        if not text or self.busy or self.conversation is None:
            return
        self.entry.set_text("")
        self.last_change = None
        self._bubble(text, True)
        self._turn(text)

    def _turn(self, text: str | None) -> None:
        """A message from the person, or None: go on after a protected field or «Change»."""
        if self.conversation is None:
            return
        self.busy = True
        self.thinking.set_visible(True)
        self.send.set_sensitive(False)
        conversation = self.conversation

        def work() -> None:
            reply = conversation.send(text) if text is not None else conversation.resume()
            GLib.idle_add(self._turn_done, reply)

        threading.Thread(target=work, daemon=True).start()

    def _turn_done(self, reply: conv.Reply) -> bool:
        self.busy = False
        self.thinking.set_visible(False)
        self.send.set_sensitive(True)
        self._show(reply)
        self.entry.grab_focus()
        return False

    def _on_confirm(self, *_args) -> None:
        """Step 12: the Herkos screen, a separate process; this window waits for it."""
        if self.busy:
            return
        confirm_window.ask(
            self,
            self.plan_path,
            self.secrets,
            self._confirm_done,
            demo=self.demo_kind,
            window=not self.is_fullscreen(),
        )

    def _confirm_done(self, outcome) -> None:
        if outcome.confirmed:
            self.confirmed = outcome
            self._finish(EXIT_CONFIRM)
            return
        # Back to the plan: the screen may have changed nothing; show it as it is.
        if self.conversation is not None:
            self.conversation.refresh()
            self._show_plan(self.conversation.plan, self.conversation.current().key)
        self.confirm.grab_focus()

    def _finish(self, code: int) -> None:
        app = self.get_application()
        if app is not None:
            app.exit_code = code  # ChatApp or the first screen's InstallerApp
            app.quit()


class ChatApp(Adw.Application):
    def __init__(self, plan_path: Path, window: bool, demo: str | None) -> None:
        super().__init__(application_id=APP_ID)
        self.plan_path, self.window, self.demo = plan_path, window, demo
        self.exit_code = EXIT_CLOSED
        self.close_tools = lambda: None

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
        win = self.get_active_window()
        if win is None:
            try:
                conversation, self.close_tools = conv.start_conversation(
                    self.plan_path, demo=self.demo
                )
            except conv.ToolsError as e:
                print(f"vilna-installer-chat: {e}", file=sys.stderr)
                conversation = None
            win = ChatWindow(
                self, conversation, self.plan_path, demo=bool(self.demo), demo_kind=self.demo
            )
        if not self.window:
            win.fullscreen()
        win.present()

    def do_shutdown(self) -> None:
        self.close_tools()
        Adw.Application.do_shutdown(self)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vilna-installer-chat")
    p.add_argument("--plan", type=Path, default=None, help="the plan the first screen wrote")
    p.add_argument("--window", action="store_true", help="a window, not the whole screen")
    p.add_argument("--demo", choices=("empty", "windows"), help="a synthetic computer")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    app = ChatApp(args.plan or start.default_plan_path(), args.window, args.demo)
    GLib.set_application_name("Vilna")
    app.run([sys.argv[0]])
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())
