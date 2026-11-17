# SPDX-License-Identifier: GPL-3.0-or-later
"""Vilna's colours and components in the installer's windows (docs/design/style/).

``vilna.css`` holds the light colours and the components, ``vilna-dark.css``
only the dark colours; the dark one is loaded while the system is dark.
Missing files or an old GTK leave the window in plain libadwaita: the window
must still open, so nothing here may fail. The welcome screen, the
conversation and the wizard each call :func:`install`; only the first loads.

The installer also has surfaces of its own (``installer-dark.css``,
``installer-light.css``): night blue, as in the designer's installer screens,
or warm cream, instead of libadwaita's grey and white. It prefers the dark
theme, so the live image, whose session asks for neither, shows night blue;
a system set to light explicitly gets the cream one.
"""

from __future__ import annotations

from importlib import resources

STYLE_FILES = ("vilna.css", "vilna-dark.css")
SURFACE_FILES = {True: "installer-dark.css", False: "installer-light.css"}
_installed: set[int] = set()


def stylesheet(name: str) -> str:
    """The text of one of the shared style files, or "" if it is missing."""
    try:
        path = resources.files("vilna_installer") / "data" / "style" / name
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""


def install() -> None:
    """Load the styles for the default display and follow the light/dark switch."""
    try:
        from gi.repository import Adw, Gdk, Gtk
    except ImportError:
        return
    display = Gdk.Display.get_default()
    if display is None or id(display) in _installed:
        return
    _installed.add(id(display))
    base = Gtk.CssProvider()
    base.load_from_string(stylesheet("vilna.css"))
    dark = Gtk.CssProvider()
    surfaces = Gtk.CssProvider()
    priority = Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    Gtk.StyleContext.add_provider_for_display(display, surfaces, priority)
    Gtk.StyleContext.add_provider_for_display(display, base, priority)
    Gtk.StyleContext.add_provider_for_display(display, dark, priority + 1)
    manager = Adw.StyleManager.get_default()
    manager.set_color_scheme(Adw.ColorScheme.PREFER_DARK)

    def follow(*_args) -> None:
        is_dark = manager.get_dark()
        surfaces.load_from_string(stylesheet(SURFACE_FILES[is_dark]))
        dark.load_from_string(stylesheet("vilna-dark.css") if is_dark else "")

    manager.connect("notify::dark", follow)
    follow()
