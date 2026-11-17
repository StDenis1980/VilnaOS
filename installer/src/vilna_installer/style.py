# SPDX-License-Identifier: GPL-3.0-or-later
"""Vilna's colours and components in the installer's windows (docs/design/style/).

``vilna.css`` holds the light colours and the components, ``vilna-dark.css``
only the dark colours; the dark one is loaded while the system is dark.
Missing files or an old GTK leave the window in plain libadwaita: the window
must still open, so nothing here may fail. The welcome screen, the
conversation and the wizard each call :func:`install`; only the first loads.

The installer also has surfaces of its own (``installer-dark.css``,
``installer-light.css``): night blue, as in the designer's installer screens,
or warm cream, instead of libadwaita's grey and white.

Dark or light follows the clock (:data:`DAY`: light by day, night blue at
night, checked again every minute) until the person presses the theme
button that every installer screen shows (:func:`theme_button`). From then
on their choice holds, in every installer window, including the Herkos
screen that runs as its own process: it is kept in a small file in the
runtime directory (:func:`choice_file`), and each window reads it again
when it becomes active.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path

STYLE_FILES = ("vilna.css", "vilna-dark.css")
SURFACE_FILES = {True: "installer-dark.css", False: "installer-light.css"}
_installed: set[int] = set()

# Light from 07:00 to 19:59 local time, night blue the rest of the day. On
# the live image the clock is in UTC until the plan has a time zone.
DAY = range(7, 20)
TO_DARK = "Switch to the dark theme"
TO_LIGHT = "Switch to the light theme"
TEXTS = (TO_DARK, TO_LIGHT)


def dark_by_clock(hour: int | None = None) -> bool:
    """Night blue at night, light by day."""
    if hour is None:
        hour = time.localtime().tm_hour
    return hour not in DAY


def choice_file() -> Path:
    """Where the person's own choice of theme is kept while the installer runs."""
    override = os.environ.get("VILNA_INSTALLER_THEME_FILE")
    if override:
        return Path(override)
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / "vilna-installer-theme"


def saved_choice() -> bool | None:
    """True for dark, False for light, None if the person has not chosen."""
    try:
        text = choice_file().read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return {"dark": True, "light": False}.get(text)


def save_choice(dark: bool) -> None:
    # If the file cannot be written, the choice holds only in this window.
    with contextlib.suppress(OSError):
        choice_file().write_text("dark" if dark else "light", encoding="utf-8")


def wanted_dark() -> bool:
    """The theme the installer should show now."""
    choice = saved_choice()
    return dark_by_clock() if choice is None else choice


def apply(dark: bool | None = None) -> None:
    """Show the dark or the light theme (by default: the choice, else the clock)."""
    try:
        from gi.repository import Adw
    except ImportError:
        return
    if dark is None:
        dark = wanted_dark()
    scheme = Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT
    manager = Adw.StyleManager.get_default()
    if manager.get_color_scheme() != scheme:
        manager.set_color_scheme(scheme)


def toggle() -> None:
    """The theme button: the other theme, kept for every installer window."""
    try:
        from gi.repository import Adw
    except ImportError:
        return
    dark = not Adw.StyleManager.get_default().get_dark()
    save_choice(dark)
    apply(dark)


def theme_button(texts: Callable[[str], str] = str):
    """A button that switches between the light and the dark theme.

    Every installer screen shows one, so the person can change the theme at
    any step. Its icon shows the theme it switches to; the screen reader
    and the tooltip say it in words.
    """
    from gi.repository import Adw, Gtk

    button = Gtk.Button(valign=Gtk.Align.CENTER)
    button.add_css_class("vilna-theme")
    manager = Adw.StyleManager.get_default()

    def show(*_args) -> None:
        dark = manager.get_dark()
        button.set_icon_name("weather-clear-symbolic" if dark else "weather-clear-night-symbolic")
        label = texts(TO_LIGHT if dark else TO_DARK)
        button.set_tooltip_text(label)
        button.update_property([Gtk.AccessibleProperty.LABEL], [label])

    def on_root(*_args) -> None:
        # Another installer window (the Herkos screen, its own process) may
        # have changed the choice: read it again when this window is active.
        root = button.get_root()
        if isinstance(root, Gtk.Window) and not getattr(root, "_vilna_theme_follows", False):
            root._vilna_theme_follows = True
            root.connect("notify::is-active", lambda w, _p: w.is_active() and apply())

    button.relabel = show  # a screen whose language changes calls it
    button.connect("clicked", lambda _b: toggle())
    handler = manager.connect("notify::dark", show)
    button.connect("destroy", lambda _b: manager.disconnect(handler))
    button.connect("realize", on_root)
    show()
    return button


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
        from gi.repository import Adw, Gdk, GLib, Gtk
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
    apply()

    def follow(*_args) -> None:
        is_dark = manager.get_dark()
        surfaces.load_from_string(stylesheet(SURFACE_FILES[is_dark]))
        dark.load_from_string(stylesheet("vilna-dark.css") if is_dark else "")

    manager.connect("notify::dark", follow)
    follow()

    def by_clock() -> bool:
        # Day turns to night while the installer runs: follow the clock,
        # unless the person has chosen.
        if saved_choice() is None:
            apply()
        return True

    GLib.timeout_add_seconds(60, by_clock)
