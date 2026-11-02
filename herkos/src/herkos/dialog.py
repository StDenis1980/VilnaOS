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
import sys
from gettext import gettext as _

LABELS = {
    "once": _("Allow once"),
    "session": _("Allow for this session"),
    "always": _("Always allow"),
}


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
    app = Adw.Application(application_id="io.github.stdenis1980.Herkos.Dialog")

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
            f"<b>{GLib.markup_escape_text(question['summary'])}</b>  ({question['level']})"
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
        notes += [_("Riskier than usual: {why}.").format(why=w) for w in question.get("raises", [])]
        if question.get("taint_sources"):
            notes.append(
                _("Untrusted content came from: {src}").format(
                    src=", ".join(question["taint_sources"])
                )
            )
        box.append(Gtk.Label(label="\n".join(notes), xalign=0, wrap=True))

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        deny = Gtk.Button(label=_("Deny"))
        deny.connect("clicked", lambda *_: finish("deny"))
        buttons.append(deny)
        for choice in question.get("choices", []):
            if choice in LABELS:
                b = Gtk.Button(label=LABELS[choice])
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
