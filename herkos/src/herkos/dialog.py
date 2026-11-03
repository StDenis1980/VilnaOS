# SPDX-License-Identifier: GPL-3.0-or-later
"""Herkos's confirmation window (GTK 4 + libadwaita), run as its own process.

    python -m herkos.dialog < question.json   →   prints the answer

Herkos starts this process for each question (``confirm.Dialog``), so the
window belongs to Herkos and not to Gloxi: the model can neither change its
text nor press its buttons through Gloxi. "Deny" is the default button;
closing the window or the timeout also means "deny".

Known limit of the preview: in a normal desktop session another program of
the same user could still drive the window through accessibility (AT-SPI).
The system Herkos in Vilna closes this (permissions.md 5.2).
"""

from __future__ import annotations

import json
import os
import sys

from herkos.i18n import _


def translated_raises(question: dict) -> list[str]:
    """Reasons for the higher level, in the window's language.

    herkosd sends them untranslated with their message ids (``raises_i18n``);
    Herkos-lite translates them itself and sends only ``raises``.
    """
    structured = question.get("raises_i18n")
    if not isinstance(structured, list):
        return [str(r) for r in question.get("raises", [])]
    out = []
    for r in structured:
        args = {
            k: "; ".join(_(str(x)) for x in v) if isinstance(v, list) else v
            for k, v in (r.get("args") or {}).items()
        }
        try:
            out.append(_(r["msgid"]).format(**args))
        except (KeyError, IndexError, ValueError):
            out.append(str(r.get("msgid", "")))
    return out


MAX_CONTENT_LINES = 40


def content_text(question: dict) -> str | None:
    """The exact arguments of an R4 action, as the person will read them.

    The system ``herkosd`` sends them as ``content`` for every R4 question:
    "allow" there means "allow exactly this", so the text must be visible.
    """
    if "content" not in question or question["content"] is None:
        return None
    text = json.dumps(question["content"], ensure_ascii=False, indent=2)
    lines = text.splitlines()
    if len(lines) > MAX_CONTENT_LINES:
        lines = [*lines[:MAX_CONTENT_LINES], "…"]
    return "\n".join(lines)


DIALOG_ID = "io.github.stdenis1980.Herkos.Dialog"


def application_id(env) -> str:
    """A Flatpak may only use names under its own id (Gloxi bundles Herkos-lite)."""
    flatpak = env.get("FLATPAK_ID")
    return f"{flatpak}.HerkosDialog" if flatpak else DIALOG_ID


LABELS = {
    "once": "Allow once",
    "session": "Allow for this session",
    "always": "Always allow",
}  # translated when shown


def main() -> int:
    try:
        question = json.loads(sys.stdin.read())
    except ValueError:
        print("deny")
        return 1
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk

    answer = ["deny"]
    from gi.repository import Gio

    # One window per question: never ask the bus for a name (NON_UNIQUE).
    # The id must still sit under the app's own id inside a Flatpak.
    app = Adw.Application(
        application_id=application_id(os.environ),
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )

    def finish(value: str) -> None:
        answer[0] = value
        app.quit()

    def build(app: Adw.Application) -> None:
        win = Adw.Window(application=app, title=_("Herkos: permission"), modal=True)
        win.set_default_size(460, -1)
        win.connect("close-request", lambda *_: finish("deny") or False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)

        title = Gtk.Label(xalign=0, wrap=True)
        title.set_markup(
            f"<b>{GLib.markup_escape_text(_(question['summary']))}</b>  ({question['level']})"
        )
        box.append(title)

        reason = Gtk.Label(xalign=0, wrap=True, selectable=True)
        reason.set_text(_("Gloxi's reason: ") + question["reason"])
        box.append(reason)

        resources = question.get("resources", [])
        if resources:
            listing = "\n".join(f"• {r}" for r in resources[:10])
            if len(resources) > 10:
                listing += "\n" + _("… and {n} more").format(n=len(resources) - 10)
            box.append(Gtk.Label(label=listing, xalign=0, wrap=True, selectable=True))

        notes = [_("Can be undone.") if question.get("undoable") else _("Cannot be undone.")]
        why_riskier = translated_raises(question)
        notes += [_("Riskier than usual: {why}.").format(why=w) for w in why_riskier]
        if question.get("taint_sources"):
            notes.append(
                _("Untrusted content came from: {src}").format(
                    src=", ".join(question["taint_sources"])
                )
            )
        box.append(Gtk.Label(label="\n".join(notes), xalign=0, wrap=True))

        content = content_text(question)
        if content is not None:
            box.append(Gtk.Label(label=_("Exactly this will be done:"), xalign=0))
            shown = Gtk.Label(label=content, xalign=0, wrap=True, selectable=True)
            shown.add_css_class("monospace")
            box.append(shown)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        deny = Gtk.Button(label=_("Deny"))
        deny.connect("clicked", lambda *_: finish("deny"))
        buttons.append(deny)
        for choice in question.get("choices", []):
            if choice in LABELS:
                b = Gtk.Button(label=_(LABELS[choice]))
                b.connect("clicked", lambda _b, c=choice: finish(c))
                buttons.append(b)
        box.append(buttons)

        win.set_content(box)
        win.set_default_widget(deny)
        deny.grab_focus()
        win.present()

    app.connect("activate", build)
    app.run([])
    print(answer[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
