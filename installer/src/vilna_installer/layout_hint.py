# SPDX-License-Identifier: GPL-3.0-or-later
"""The keyboard layout under a password field, always in the same place.

The Vilna Shell extension switches a password field to the Latin layout
and used to show it in a small floating label near the text cursor. In the
installer that label moved as the person typed, lay over «Repeat the
password», and was white on cream in the light theme (VM check of
Build12.19, 2026-11-24). Denys asked for a fixed place: right under the
field, at its left edge. So the installer draws the hint itself, as part of
its own layout, in its own colours, and the extension leaves the
installer's windows alone (passwordLayout.js).

The name follows the Shell's top bar («en», «uk»…), so it reads the same
there and here. Where it comes from: GTK's keyboard device (``layout-names``
and ``active-layout-index``, GTK 4.18 on Wayland) or, without them, the
first of the session's most recently used input sources. Nothing here may
fail: without either the hint stays hidden.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable

LAYOUT = "Keyboard layout: {name}"
TEXTS = (LAYOUT,)

# xkb layout → the short name the GNOME top bar shows.
SHORT_OF_XKB = {"us": "en", "gb": "en", "ua": "uk", "cz": "cs"}
# The start of an xkb layout description (what GTK reports) → short name.
SHORT_OF_NAME = {
    "English": "en",
    "Ukrainian": "uk",
    "Spanish": "es",
    "Polish": "pl",
    "German": "de",
    "French": "fr",
    "Italian": "it",
    "Romanian": "ro",
    "Czech": "cs",
    "Lithuanian": "lt",
}
INPUT_SOURCES_SCHEMA = "org.gnome.desktop.input-sources"


def short_of_xkb(layout: str) -> str:
    """«us» → «en», «ua+phonetic» → «uk»: as the top bar names it."""
    base = (layout or "").split("+")[0].split("(")[0].strip()
    return SHORT_OF_XKB.get(base, base)


def short_of_name(name: str) -> str:
    """«English (US)» → «en»; an unknown description stays as it is."""
    for start, short in SHORT_OF_NAME.items():
        if (name or "").startswith(start):
            return short
    return name or ""


def _from_keyboard(keyboard) -> tuple[str, str] | None:
    if keyboard is None or keyboard.find_property("layout-names") is None:
        return None
    names = keyboard.get_property("layout-names") or []
    index = keyboard.get_property("active-layout-index")
    if not isinstance(index, int) or not 0 <= index < len(names):
        return None
    return short_of_name(names[index]), names[index]


def _from_settings(settings) -> tuple[str, str] | None:
    if settings is None:
        return None
    for key in ("mru-sources", "sources"):
        with contextlib.suppress(Exception):
            for kind, layout in settings.get_value(key).unpack():
                if kind == "xkb":
                    return short_of_xkb(layout), layout
    return None


def _keyboard():
    with contextlib.suppress(Exception):
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
        seat = display.get_default_seat() if display else None
        return seat.get_keyboard() if seat else None
    return None


def _settings():
    with contextlib.suppress(Exception):
        from gi.repository import Gio

        source = Gio.SettingsSchemaSource.get_default()
        if source is not None and source.lookup(INPUT_SOURCES_SCHEMA, True) is not None:
            return Gio.Settings(schema_id=INPUT_SOURCES_SCHEMA)
    return None


def current(keyboard=None, settings=None) -> tuple[str, str] | None:
    """(short name, full name) of the layout in use now, or None."""
    return _from_keyboard(keyboard) or _from_settings(settings)


def hint(texts: Callable[[str], str] = str, keyboard=None, settings=None):
    """A small row «⌨ en» to put right under a password field, left-aligned.

    It is laid out with the field, so it never moves, and it updates when the
    layout changes (for instance when the Shell switches the field to Latin).
    """
    from gi.repository import Gtk

    keyboard = keyboard if keyboard is not None else _keyboard()
    settings = settings if settings is not None else _settings()
    box = Gtk.Box(spacing=4, halign=Gtk.Align.START, visible=False)
    box.add_css_class("vilna-layout-hint")
    box.add_css_class("dim-label")
    box.append(Gtk.Image(icon_name="input-keyboard-symbolic", pixel_size=12))
    label = Gtk.Label(xalign=0)
    label.add_css_class("caption")
    box.append(label)
    box.label = label

    def show(*_args) -> None:
        now = current(keyboard, settings)
        if now is None:
            box.set_visible(False)
            return
        short, full = now
        label.set_label(short)
        words = texts(LAYOUT).format(name=full)
        box.set_tooltip_text(words)
        box.update_property([Gtk.AccessibleProperty.LABEL], [words])
        box.set_visible(True)

    with contextlib.suppress(Exception):
        if keyboard is not None and keyboard.find_property("active-layout-index") is not None:
            keyboard.connect("notify::active-layout-index", show)
            keyboard.connect("notify::layout-names", show)
    with contextlib.suppress(Exception):
        if settings is not None:
            settings.connect("changed::mru-sources", show)
            settings.connect("changed::sources", show)
    box.refresh = show
    show()
    return box
