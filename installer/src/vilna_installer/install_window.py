# SPDX-License-Identifier: GPL-3.0-or-later
"""Step 13 on the screen: the engine's progress, then «Restart» (GTK 4 + libadwaita).

After the Herkos screen the conversation and the classic wizard call
``start``: it hands the plan's SHA-256 and the secrets in the ``Vault`` to
the engine (``vilna-installer-engine`` as root, one JSON line on its
standard input) and shows ``progress.json`` while the engine works. The
window has no button that stops the engine: half a layout is worse than
waiting a few minutes more.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from vilna_installer import a11y, confirm, release, style  # noqa: E402
from vilna_installer import engine as eng  # noqa: E402
from vilna_installer.branch import Branch  # noqa: E402
from vilna_installer.vault import Vault  # noqa: E402

POLL_MS = 500


class InstallWindow(Adw.ApplicationWindow):
    def __init__(
        self,
        app: Adw.Application,
        plan_path: Path,
        *,
        texts: Callable[[str], str],
        on_end: Callable[[bool], None] = lambda _ok: None,
    ) -> None:
        super().__init__(application=app)
        self._ = _ = texts
        self.progress_path = plan_path.with_name("progress.json")
        self.on_end = on_end
        self.ended = False
        self.said: str | None = None  # the step last said to the screen reader
        self.set_title(_(eng.TITLE))
        self.set_default_size(720, 560)
        self.connect("close-request", lambda *_a: not self.ended)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for side in ("top", "bottom", "start", "end"):
            getattr(page, f"set_margin_{side}")(32)
        self.title = Gtk.Label(label=_(eng.TITLE), xalign=0, wrap=True)
        self.title.add_css_class("title-1")
        page.append(self.title)
        self.lead = Gtk.Label(label=_(eng.LEAD), xalign=0, wrap=True)
        page.append(self.lead)
        # Decorative: the branch grows with the percent; at the end the
        # release sign sits on it (branch.py, release.py).
        self.branch = Branch()
        page.append(self.branch)
        self.release = Gtk.Label(label=f"Vilna {release.NAME}", visible=False)
        self.release.add_css_class("dim-label")
        page.append(self.release)
        self.bar = Gtk.ProgressBar(show_text=True)
        self.bar.update_property([Gtk.AccessibleProperty.LABEL], [_(eng.TITLE)])
        page.append(self.bar)

        self.rows: dict[str, Gtk.Label] = {}
        self.steps = steps = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for key in eng.PHASE_KEYS:
            label = Gtk.Label(xalign=0, wrap=True)
            self.rows[key] = label
            steps.append(label)
        page.append(steps)

        self.error = Gtk.Label(xalign=0, wrap=True, selectable=True, visible=False)
        self.error.add_css_class("monospace")
        page.append(self.error)

        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.END, valign=Gtk.Align.END, vexpand=True)
        self.close_button = Gtk.Button(label=_(eng.CLOSE), visible=False)
        self.close_button.connect("clicked", lambda *_a: self._end(False))
        self.restart = Gtk.Button(label=_(eng.RESTART), visible=False)
        self.restart.add_css_class("suggested-action")
        self.restart.connect("clicked", self._on_restart)
        buttons.append(self.close_button)
        buttons.append(self.restart)
        page.append(buttons)
        # The theme button stays in the corner while the installation runs.
        overlay = Gtk.Overlay(child=page)
        theme = style.theme_button(_)
        theme.set_halign(Gtk.Align.END)
        theme.set_valign(Gtk.Align.START)
        theme.set_margin_top(12)
        theme.set_margin_end(12)
        theme.add_css_class("flat")
        overlay.add_overlay(theme)
        self.set_content(overlay)
        self.show_state(None)

    # what the engine wrote

    def show_state(self, data: dict | None) -> str:
        _ = self._
        state = (data or {}).get("state", "running")
        phase = (data or {}).get("phase", "check")
        percent = int((data or {}).get("percent", 0))
        self.bar.set_fraction(max(0, min(100, percent)) / 100)
        self.branch.set_fraction(max(0, min(100, percent)) / 100)
        self.bar.set_text(f"{percent} %")
        reached = False
        for key in eng.PHASE_KEYS:
            text = _(eng.PHASE_TEXT[key])
            if state == "done" or (not reached and key != phase):
                mark = "✓"
            elif key == phase and state == "running":
                mark, reached = "…", True
                if self.said != key:  # each step once, as it begins
                    self.said = key
                    a11y.announce(self.rows[key], text)
            elif key == phase:
                mark, reached = "✗", True
            else:
                mark = "·"
            self.rows[key].set_label(f"{mark}  {text}")
        if state == "done":
            self._finished(_(eng.DONE), _(eng.DONE_LEAD), ok=True)
        elif state == "failed":
            touched = bool((data or {}).get("disk_changed"))
            self._finished(
                _(eng.FAILED), _(eng.FAILED_TOUCHED if touched else eng.FAILED_LEAD), ok=False
            )
            error = eng.printable(str((data or {}).get("error") or ""))
            self.error.set_label(f"{_(eng.WHAT_HAPPENED)}\n{error}")
            self.error.set_visible(True)
        return state

    def _finished(self, title: str, lead: str, *, ok: bool) -> None:
        if self.ended:
            return
        self.ended = True
        self.title.set_label(title)
        self.lead.set_label(lead)
        self.restart.set_visible(ok)
        self.close_button.set_visible(True)
        if ok:
            # «Done»: the whole branch with the release sign, no bar and no list.
            self.branch.finish()
            self.branch.set_vexpand(True)
            self.release.set_visible(True)
            self.bar.set_visible(False)
            self.steps.set_visible(False)
        (self.restart if ok else self.close_button).grab_focus()
        if hasattr(self.lead, "announce"):  # GTK 4.14+
            self.lead.announce(f"{title}. {lead}", Gtk.AccessibleAnnouncementPriority.HIGH)

    def poll(self) -> bool:
        state = self.show_state(eng.read_progress(self.progress_path))
        return state == "running"

    def engine_ended(self, ok: bool) -> None:
        """The engine's process is gone: its last word is in progress.json."""
        data = eng.read_progress(self.progress_path)
        if not ok and (data is None or data.get("state") == "running"):
            data = {"state": "failed", "phase": (data or {}).get("phase", "check"),
                    "error": "the engine stopped without a word", "disk_changed": True}  # fmt: skip
        self.show_state(data)

    def _on_restart(self, *_a) -> None:
        try:
            Gio.Subprocess.new(
                ["systemctl", "--no-ask-password", "reboot"], Gio.SubprocessFlags.NONE
            )
        except GLib.Error as e:
            print(f"vilna-installer: restart failed: {e.message}", file=sys.stderr)
        self._end(True)

    def _end(self, ok: bool) -> None:
        self.on_end(ok)
        self.close()


def start(
    parent: Gtk.Window,
    plan_path: Path,
    outcome: confirm.Outcome,
    vault: Vault,
    *,
    texts: Callable[[str], str],
    demo: bool = False,
    on_end: Callable[[bool], None] = lambda _ok: None,
) -> InstallWindow:
    """Start the engine for the confirmed plan and show its progress instead of ``parent``."""
    progress = plan_path.with_name("progress.json")
    progress.unlink(missing_ok=True)
    win = InstallWindow(parent.get_application(), plan_path, texts=texts, on_end=on_end)
    if parent.is_fullscreen():
        win.fullscreen()
    line = eng.request_line(plan_path, outcome.plan_sha256 or "", vault, os.getuid())
    try:
        proc = Gio.Subprocess.new(
            eng.command(demo=demo, progress=progress), Gio.SubprocessFlags.STDIN_PIPE
        )
    except GLib.Error as e:
        print(f"vilna-installer: the engine did not start: {e.message}", file=sys.stderr)
        win.show_state({"state": "failed", "error": e.message, "disk_changed": False})
        win.present()
        parent.set_visible(False)
        return win

    def written(p: Gio.Subprocess, result: Gio.AsyncResult) -> None:
        with contextlib.suppress(GLib.Error):
            p.communicate_utf8_finish(result)
        p.wait_check_async(None, lambda q, r: win.engine_ended(_ok(q, r)))

    def _ok(q: Gio.Subprocess, r: Gio.AsyncResult) -> bool:
        try:
            return q.wait_check_finish(r)
        except GLib.Error:
            return False

    proc.communicate_utf8_async(line + "\n", None, written)
    GLib.timeout_add(POLL_MS, win.poll)
    win.present()
    parent.set_visible(False)
    return win
