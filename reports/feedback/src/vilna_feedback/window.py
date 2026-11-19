# SPDX-License-Identifier: GPL-3.0-or-later
"""«Feedback about Vilna»: the window and the crash notifications (ADR 0018 section 5).

Two pages. **Feedback**: what it is about, the text, and the choices the
person makes (facts about this computer, an e-mail address for an answer,
permission to publish, and on the live system the installer's conversation
with Gloxi, added only after the person read it); «What will be sent» shows
the exact request before «Send». **Crash reports**: the answer about crash reports, each report of
this person and of the system with «View», «Send» and «Do not send», and
«Delete everything I sent from the server» (GDPR).

``vilna-feedback --watch`` runs in the session (autostart) for the answer
«Ask me every time»: one notification per new crash, with «View», «Send»
and «Do not send». It needs no model and no network of its own: everything
goes through ``vilna-reportd``.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from vilna_feedback import form  # noqa: E402
from vilna_feedback import texts as t  # noqa: E402
from vilna_feedback.client import Reportd, ReportdError  # noqa: E402

APP_ID = "io.github.stdenis1980.VilnaFeedback"
WATCH_SECONDS = 60
# Changing the answer is the administrator's (polkit asks); see the .policy file.
CONSENT_COMMAND = ("pkexec", "/usr/bin/vilna-reportd", "consent")


def in_background(work: Callable[[], Any], done: Callable[[Any], None]) -> None:
    """Run ``work`` off the main loop (sending waits for the server), then ``done`` on it."""

    def run() -> None:
        try:
            result: Any = work()
        except ReportdError as e:
            result = e
        GLib.idle_add(lambda: done(result) and False)

    threading.Thread(target=run, daemon=True).start()


def plain_row(**kwargs: Any) -> Adw.ActionRow:
    """Program names and server answers are text, never Pango markup."""
    row = Adw.ActionRow(**kwargs)
    row.set_use_markup(False)
    return row


class FeedbackWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, reportd: Reportd, texts: t.Texts) -> None:
        super().__init__(application=app, title=texts(t.TITLE))
        self.reportd = reportd
        self._ = _ = texts
        self.set_default_size(720, 760)
        self.system = ""
        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self.stack.add_titled_with_icon(
            self._feedback_page(), "feedback", _(t.TAB_FEEDBACK), "mail-send-symbolic"
        )
        self.reports_page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(
            self.reports_page, "reports", _(t.TAB_REPORTS), "dialog-warning-symbolic"
        )
        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        self.toasts.set_child(self.stack)
        view.set_content(self.toasts)
        self.set_content(view)
        self.stack.connect("notify::visible-child-name", lambda *_a: self._on_page())
        in_background(self.reportd.system, self._got_system)

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=6))

    # --- feedback -------------------------------------------------------------

    def _feedback_page(self) -> Gtk.Widget:
        _ = self._
        page = Adw.PreferencesPage()
        lead = Adw.PreferencesGroup(description=_(t.LEAD))
        page.add(lead)

        kinds = Adw.PreferencesGroup(title=_(t.KIND))
        leader = Gtk.CheckButton()
        self.kind_checks: dict[str, Gtk.CheckButton] = {}
        for kind in form.KINDS:
            row = plain_row(title=_(t.KIND_TEXT[kind]))
            check = Gtk.CheckButton(group=leader, valign=Gtk.Align.CENTER)
            check.leader = leader
            check.update_property([Gtk.AccessibleProperty.LABEL], [_(t.KIND_TEXT[kind])])
            row.add_prefix(check)
            row.set_activatable_widget(check)
            kinds.add(row)
            self.kind_checks[kind] = check
        self.kind_checks["bug"].set_active(True)
        page.add(kinds)

        message = Adw.PreferencesGroup(title=_(t.MESSAGE))
        self.text = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
        self.text.set_top_margin(10)
        self.text.set_bottom_margin(10)
        self.text.set_left_margin(10)
        self.text.set_right_margin(10)
        self.text.update_property([Gtk.AccessibleProperty.LABEL], [_(t.MESSAGE)])
        self.text.get_buffer().connect("changed", lambda *_a: self._changed())
        scroll = Gtk.ScrolledWindow(min_content_height=160, child=self.text)
        scroll.add_css_class("card")
        message.add(scroll)
        page.add(message)

        choices = Adw.PreferencesGroup()
        self.add_system = Adw.SwitchRow(title=_(t.ADD_SYSTEM))
        self.add_system.set_use_markup(False)
        self.reply = Adw.SwitchRow(title=_(t.REPLY))
        self.email = Adw.EntryRow(title=_(t.EMAIL), visible=False)
        self.email.set_input_purpose(Gtk.InputPurpose.EMAIL)
        self.publish = Adw.SwitchRow(title=_(t.PUBLISH))
        for row in (self.add_system, self.reply, self.email, self.publish):
            choices.add(row)
        for row in (self.add_system, self.reply, self.publish):
            row.connect("notify::active", lambda *_a: self._changed())
        self.email.connect("changed", lambda *_a: self._changed())
        page.add(choices)
        page.add(self._conversation_group())

        sent = Adw.PreferencesGroup()
        self.preview = Adw.ExpanderRow(title=_(t.PREVIEW))
        self.preview_text = Gtk.Label(xalign=0, selectable=True, wrap=True)
        self.preview_text.add_css_class("monospace")
        for side in ("top", "bottom", "start", "end"):
            getattr(self.preview_text, f"set_margin_{side}")(12)
        self.preview.add_row(self.preview_text)
        self.preview.connect("notify::expanded", lambda *_a: self._changed())
        sent.add(self.preview)
        self.problem = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.problem.add_css_class("error")
        self.problem.set_margin_top(12)
        sent.add(self.problem)
        self.send = Gtk.Button(label=_(t.SEND), halign=Gtk.Align.END, margin_top=12)
        self.send.add_css_class("suggested-action")
        self.send.add_css_class("pill")
        self.send.connect("clicked", self._on_send)
        sent.add(self.send)
        page.add(sent)
        # Only now: every widget _changed reads exists.
        for check in self.kind_checks.values():
            check.connect("toggled", lambda *_a: self._changed())
        return page

    def _conversation_group(self) -> Adw.PreferencesGroup:
        """The installer's conversation with Gloxi, when this session has one.

        Off at first. Turning it on first shows the whole conversation; only
        «Add» there adds it (it may have a name or a Wi-Fi network).
        """
        _ = self._
        self.conversation = form.read_conversation(form.conversation_path())
        self.conversation_read = False
        self.conversation_dialog: Adw.AlertDialog | None = None
        self.conversation_group = group = Adw.PreferencesGroup(visible=bool(self.conversation))
        self.add_conversation = Adw.SwitchRow(
            title=_(t.ADD_CONVERSATION), subtitle=_(t.CONVERSATION_HINT)
        )
        read = Gtk.Button(label=_(t.READ), valign=Gtk.Align.CENTER)
        read.add_css_class("flat")
        read.connect("clicked", lambda *_a: self.show_conversation())
        self.add_conversation.add_suffix(read)
        self.add_conversation.connect("notify::active", self._on_add_conversation)
        group.add(self.add_conversation)
        return group

    def _on_add_conversation(self, *_args: Any) -> None:
        if self.add_conversation.get_active() and not self.conversation_read:
            self.add_conversation.set_active(False)  # not before the person read it
            self.show_conversation()
            return
        self._changed()

    def show_conversation(self) -> None:
        _ = self._
        dialog = Adw.AlertDialog(heading=_(t.CONVERSATION), body=_(t.CONVERSATION_HINT))
        view = Gtk.TextView(
            editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR
        )
        view.get_buffer().set_text(self.conversation)
        view.update_property([Gtk.AccessibleProperty.LABEL], [_(t.CONVERSATION)])
        dialog.set_extra_child(Gtk.ScrolledWindow(child=view, min_content_height=320))
        dialog.add_response("no", _(t.DO_NOT_ADD))
        dialog.add_response("add", _(t.ADD))
        dialog.set_close_response("no")
        dialog.connect("response", self._on_conversation_answer)
        self.conversation_dialog = dialog
        dialog.present(self)

    def _on_conversation_answer(self, _dialog: Any, response: str) -> None:
        if response == "add":
            self.conversation_read = True
        self.add_conversation.set_active(response == "add")

    def _got_system(self, result: Any) -> None:
        if isinstance(result, str) and result:
            self.system = result
            self.add_system.set_subtitle(result)
        elif isinstance(result, ReportdError) and result.offline:
            self.problem.set_label(self._(t.NO_SERVICE))
            self.problem.set_visible(True)

    def form(self) -> form.Form:
        buffer = self.text.get_buffer()
        kind = next((k for k, c in self.kind_checks.items() if c.get_active()), "other")
        return form.Form(
            kind=kind,
            text=buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False),
            add_system=self.add_system.get_active(),
            system=self.system,
            reply=self.reply.get_active(),
            contact=self.email.get_text(),
            may_publish=self.publish.get_active(),
            add_conversation=self.add_conversation.get_active(),
            conversation=self.conversation,
            locale=self._.code,
        )

    def _changed(self) -> None:
        self.email.set_visible(self.reply.get_active())
        self.problem.set_visible(False)
        if not self.preview.get_expanded():
            return
        body = self.form().body()
        try:
            # The service's own check and scrub: what it would really send.
            shown = self.reportd.preview(body) if body["text"] else body
        except ReportdError:
            shown = body
        self.preview_text.set_label(form.preview(shown))

    def _on_send(self, *_args: Any) -> None:
        _ = self._
        f = self.form()
        problem = f.problem()
        if problem:
            self._show_problem(_(problem))
            return
        self.send.set_sensitive(False)
        self.send.set_label(_(t.SENDING))
        in_background(lambda: self.reportd.feedback(f.body()), self._sent)

    def _show_problem(self, text: str) -> None:
        self.problem.set_label(text)
        self.problem.set_visible(True)
        if hasattr(self.problem, "announce"):  # GTK 4.14 and later
            self.problem.announce(text, Gtk.AccessibleAnnouncementPriority.HIGH)

    def _sent(self, result: Any) -> None:
        _ = self._
        self.send.set_sensitive(True)
        self.send.set_label(_(t.SEND))
        if isinstance(result, ReportdError):
            text = _(t.NO_SERVICE) if result.offline else _(t.NOT_SENT).format(reason=result)
            self._show_problem(text)
            return
        self.text.get_buffer().set_text("")
        self.toast(_(t.SENT))

    # --- crash reports ------------------------------------------------------------

    def _on_page(self) -> None:
        if self.stack.get_visible_child_name() == "reports":
            self.refresh_reports()

    def show_reports(self) -> None:
        self.stack.set_visible_child_name("reports")
        self.refresh_reports()

    def refresh_reports(self) -> None:
        def work() -> tuple[dict[str, Any], list[dict[str, Any]]]:
            return self.reportd.status(), self.reportd.reports()

        in_background(work, self._show_reports)

    def _show_reports(self, result: Any) -> None:
        _ = self._
        page = Adw.PreferencesPage()
        if isinstance(result, ReportdError):
            page.add(Adw.PreferencesGroup(description=_(t.NO_SERVICE)))
            self._replace_reports(page)
            return
        status, reports = result
        page.add(self._answer_group(status.get("consent")))
        group = Adw.PreferencesGroup(title=_(t.TAB_REPORTS), description=_(t.REPORTS_LEAD))
        if not reports:
            group.add(plain_row(title=_(t.NO_REPORTS)))
        for report in reports:
            group.add(self._report_row(report))
        page.add(group)
        forget = Adw.PreferencesGroup()
        button = Gtk.Button(label=_(t.FORGET), halign=Gtk.Align.START)
        button.add_css_class("destructive-action")
        button.connect("clicked", self._on_forget)
        forget.add(button)
        page.add(forget)
        self._replace_reports(page)

    def _replace_reports(self, page: Adw.PreferencesPage) -> None:
        name = "reports"
        old = self.stack.get_child_by_name(name)
        visible = self.stack.get_visible_child_name() == name
        title = self._(t.TAB_REPORTS)
        if old is not None:
            self.stack.remove(old)
        self.stack.add_titled_with_icon(page, name, title, "dialog-warning-symbolic")
        self.reports_page = page
        if visible:
            self.stack.set_visible_child_name(name)

    def _answer_group(self, consent: Any) -> Adw.PreferencesGroup:
        _ = self._
        group = Adw.PreferencesGroup()
        keys = ["auto", "ask", "never"]
        names = [_(t.ANSWER_TEXT[k]) for k in keys]
        if consent not in keys:
            keys.insert(0, "")
            names.insert(0, _(t.ANSWER_NONE))
        row = Adw.ComboRow(title=_(t.ANSWER), model=Gtk.StringList.new(names))
        row.set_selected(keys.index(consent if consent in keys else ""))
        row.answer_keys = keys
        row.connect("notify::selected", self._on_answer, consent)
        group.add(row)
        return group

    def _on_answer(self, row: Adw.ComboRow, _pspec: Any, was: Any) -> None:
        key = row.answer_keys[row.get_selected()]
        if not key or key == was:
            return

        def work() -> bool:
            try:
                done = subprocess.run([*CONSENT_COMMAND, key], capture_output=True, timeout=120)
            except (OSError, subprocess.SubprocessError):
                return False
            return done.returncode == 0

        def done(ok: Any) -> None:
            if ok is not True:
                self.toast(self._(t.ANSWER_NOT_CHANGED))
            self.refresh_reports()

        in_background(work, done)

    def _report_row(self, report: dict[str, Any]) -> Adw.ActionRow:
        _ = self._
        row = plain_row(title=str(report.get("what") or report.get("program")))
        row.set_subtitle(form.status_text(report, _))
        report_id = str(report.get("id"))
        view = Gtk.Button(label=_(t.VIEW), valign=Gtk.Align.CENTER)
        view.add_css_class("flat")
        view.connect("clicked", lambda *_a: self.view_report(report_id))
        row.add_suffix(view)
        if report.get("state") == "waiting":
            send = Gtk.Button(label=_(t.SEND_REPORT), valign=Gtk.Align.CENTER)
            send.connect("clicked", lambda *_a: self.send_report(report_id))
            discard = Gtk.Button(label=_(t.DISCARD), valign=Gtk.Align.CENTER)
            discard.add_css_class("flat")
            discard.connect("clicked", lambda *_a: self.discard_report(report_id))
            row.add_suffix(send)
            row.add_suffix(discard)
        return row

    def view_report(self, report_id: str) -> None:
        """The report exactly as it is or was sent."""
        _ = self._

        def show(result: Any) -> None:
            if isinstance(result, ReportdError):
                self.toast(str(result))
                return
            dialog = Adw.AlertDialog(heading=_(t.REPORT), body=str(result.get("what") or ""))
            label = Gtk.Label(
                label=form.preview(result.get("report") or {}), xalign=0, selectable=True
            )
            label.add_css_class("monospace")
            dialog.set_extra_child(Gtk.ScrolledWindow(child=label, min_content_height=320))
            dialog.add_response("close", _(t.CLOSE))
            if result.get("state") == "waiting":
                dialog.add_response("discard", _(t.DISCARD))
                dialog.add_response("send", _(t.SEND_REPORT))
            dialog.connect("response", self._on_dialog, report_id)
            dialog.present(self)

        in_background(lambda: self.reportd.show(report_id), show)

    def _on_dialog(self, _dialog: Any, response: str, report_id: str) -> None:
        if response == "send":
            self.send_report(report_id)
        elif response == "discard":
            self.discard_report(report_id)

    def send_report(self, report_id: str) -> None:
        def done(result: Any) -> None:
            if isinstance(result, ReportdError):
                self.toast(self._(t.NOT_SENT).format(reason=result))
            self.refresh_reports()

        in_background(lambda: self.reportd.send(report_id), done)

    def discard_report(self, report_id: str) -> None:
        in_background(lambda: self.reportd.discard(report_id), lambda _r: self.refresh_reports())

    def _on_forget(self, *_args: Any) -> None:
        def done(result: Any) -> None:
            if isinstance(result, ReportdError):
                self.toast(str(result))
            else:
                self.toast(self._(t.FORGET_DONE).format(count=result))
            self.refresh_reports()

        in_background(self.reportd.forget, done)


class FeedbackApp(Adw.Application):
    def __init__(self, reportd: Reportd | None = None) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.reportd = reportd or Reportd()
        self.texts = t.Texts()
        self.window: FeedbackWindow | None = None
        self.watching = False
        self.notify_file = form.state_dir() / "notified.json"
        self.add_main_option(
            "watch", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, "Ask about new crashes", None
        )
        self.add_main_option(
            "reports", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, "Open crash reports", None
        )
        for name, handler in (
            ("show-report", self._show_report),
            ("send-report", self._send_report),
            ("discard-report", self._discard_report),
        ):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect("activate", handler)
            self.add_action(action)

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        options = command_line.get_options_dict().end().unpack()
        if options.get("watch"):
            self.watch()
        elif options.get("reports"):
            self.open().show_reports()
        else:
            self.open()
        return 0

    def open(self) -> FeedbackWindow:
        if self.window is None:
            self.window = FeedbackWindow(self, self.reportd, self.texts)
            self.window.connect("close-request", self._closed)
        self.window.present()
        return self.window

    def _closed(self, *_args: Any) -> bool:
        self.window = None
        return False

    # «Ask me every time»

    def watch(self) -> None:
        if self.watching:
            return
        self.watching = True
        self.hold()  # stays in the session without a window
        self._poll()
        GLib.timeout_add_seconds(WATCH_SECONDS, self._poll)

    def _poll(self) -> bool:
        def work() -> tuple[Any, list[dict[str, Any]]]:
            return self.reportd.status().get("consent"), self.reportd.reports()

        in_background(work, self._notify)
        return True

    def _notify(self, result: Any) -> None:
        if isinstance(result, ReportdError):
            return
        consent, reports = result
        notified = form.still_waiting(reports, form.load_notified(self.notify_file))
        _ = self.texts
        for report in form.to_notify(consent, reports, notified):
            report_id = str(report["id"])
            note = Gio.Notification.new(
                _(t.NOTIFY_TITLE).format(program=report.get("program") or "?")
            )
            note.set_body(_(t.NOTIFY_BODY))
            target = GLib.Variant.new_string(report_id)
            note.set_default_action_and_target("app.show-report", target)
            note.add_button_with_target(_(t.VIEW), "app.show-report", target)
            note.add_button_with_target(_(t.SEND_REPORT), "app.send-report", target)
            note.add_button_with_target(_(t.DISCARD), "app.discard-report", target)
            self.send_notification(f"crash-{report_id}", note)
            notified.add(report_id)
        form.save_notified(self.notify_file, notified)

    def _show_report(self, _action: Any, value: GLib.Variant) -> None:
        window = self.open()
        window.show_reports()
        window.view_report(value.get_string())

    def _send_report(self, _action: Any, value: GLib.Variant) -> None:
        report_id = value.get_string()
        self.withdraw_notification(f"crash-{report_id}")
        self.hold()
        in_background(lambda: self.reportd.send(report_id), lambda _r: self.release())

    def _discard_report(self, _action: Any, value: GLib.Variant) -> None:
        report_id = value.get_string()
        self.withdraw_notification(f"crash-{report_id}")
        self.hold()
        in_background(lambda: self.reportd.discard(report_id), lambda _r: self.release())


def main(argv: list[str] | None = None) -> int:
    return FeedbackApp().run(sys.argv if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
