# SPDX-License-Identifier: Apache-2.0
"""A small GTK 4 window for test_atspi_live.py: a note, a password, «Send»."""

import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402


def activate(app):
    window = Gtk.ApplicationWindow(application=app, title=sys.argv[1])
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    note = Gtk.Entry()
    note.set_text("old text")
    note.update_property([Gtk.AccessibleProperty.LABEL], ["Note"])
    password = Gtk.PasswordEntry()
    password.set_text("secret123")
    password.update_property([Gtk.AccessibleProperty.LABEL], ["Password"])
    send = Gtk.Button(label="Send")
    send.connect("clicked", lambda *_: print("CLICKED", note.get_text(), flush=True))
    for w in (note, password, send):
        box.append(w)
    window.set_child(box)
    window.present()
    print("ready", flush=True)


app = Gtk.Application(application_id=None)
app.connect("activate", activate)
app.run([])
