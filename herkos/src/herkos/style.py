# SPDX-License-Identifier: GPL-3.0-or-later
"""Vilna's colours and components in Herkos's windows (docs/design/style/).

``vilna.css`` holds the light colours and the components, ``vilna-dark.css``
only the dark colours; the dark one is loaded while the system is dark.
Missing files or an old GTK leave the window in plain libadwaita: the window
must still open, so nothing here may fail.
"""

from __future__ import annotations

from importlib import resources

STYLE_FILES = ("vilna.css", "vilna-dark.css")


def stylesheet(name: str) -> str:
    """The text of one of the shared style files, or "" if it is missing."""
    try:
        return (resources.files("herkos") / "data" / "style" / name).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""


def install() -> None:
    """Load the styles for the default display and follow the light/dark switch."""
    try:
        from gi.repository import Adw, Gdk, Gtk
    except ImportError:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    base = Gtk.CssProvider()
    base.load_from_string(stylesheet("vilna.css"))
    dark = Gtk.CssProvider()
    priority = Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    Gtk.StyleContext.add_provider_for_display(display, base, priority)
    Gtk.StyleContext.add_provider_for_display(display, dark, priority + 1)
    manager = Adw.StyleManager.get_default()

    def follow(*_args) -> None:
        dark.load_from_string(stylesheet("vilna-dark.css") if manager.get_dark() else "")

    manager.connect("notify::dark", follow)
    follow()
