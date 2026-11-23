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


def icon_dir() -> str | None:
    """The Vilna symbolic icons shipped with this package (docs/design/icons/system).

    The folder is added to GTK's icon search path, so the icons show on any
    desktop; on Vilna the Vilna icon theme has the same files. They sit in
    hicolor/symbolic/apps under it: GTK recolours a symbolic icon only when it
    is found in a theme folder, not as a loose file in the search path.
    """
    try:
        path = resources.files("herkos") / "data" / "icons"
        return str(path) if path.is_dir() else None
    except (OSError, ValueError, TypeError):
        return None


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
    icons = icon_dir()
    if icons:
        Gtk.IconTheme.get_for_display(display).add_search_path(icons)
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
