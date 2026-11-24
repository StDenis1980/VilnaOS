# SPDX-License-Identifier: GPL-3.0-or-later
"""The keyboard layout under a password field: fixed place, right name."""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject, Gtk  # noqa: E402

from vilna_installer import layout_hint, start  # noqa: E402


class Keyboard(GObject.Object):
    """GTK 4.18's keyboard device, as far as the hint reads it."""

    layout_names = GObject.Property(type=GObject.TYPE_STRV)
    active_layout_index = GObject.Property(type=int, default=0)

    def __init__(self, names, index):
        super().__init__()
        self.set_property("layout-names", names)
        self.set_property("active-layout-index", index)


class Settings:
    def __init__(self, sources):
        self.sources = sources

    def get_value(self, _key):
        return GLib.Variant("a(ss)", self.sources)

    def connect(self, *_args):
        return 0


def test_names_follow_the_top_bar():
    assert layout_hint.short_of_xkb("us") == "en"
    assert layout_hint.short_of_xkb("ua") == "uk"
    assert layout_hint.short_of_xkb("cz") == "cs"
    assert layout_hint.short_of_xkb("de+nodeadkeys") == "de"
    assert layout_hint.short_of_name("English (US)") == "en"
    assert layout_hint.short_of_name("Ukrainian") == "uk"
    assert layout_hint.short_of_name("Klingon") == "Klingon"


def test_the_hint_shows_the_layout_in_use_and_follows_a_switch():
    """Build12.19: the floating label slid along the field; this one is part of the layout."""
    keyboard = Keyboard(["Ukrainian", "English (US)"], 0)
    hint = layout_hint.hint(keyboard=keyboard, settings=None)
    assert hint.get_visible()
    assert hint.label.get_label() == "uk"
    assert hint.get_halign() == Gtk.Align.START
    keyboard.set_property("active-layout-index", 1)  # the Shell switched the field to Latin
    assert hint.label.get_label() == "en"
    assert hint.get_tooltip_text() == "Keyboard layout: English (US)"


def test_without_gtk_layouts_the_session_sources_say_it():
    hint = layout_hint.hint(keyboard=None, settings=Settings([("xkb", "us"), ("xkb", "ua")]))
    assert hint.label.get_label() == "en"


def test_nothing_known_hides_the_hint():
    hint = layout_hint.hint(keyboard=Keyboard([], 0), settings=Settings([("ibus", "anthy")]))
    assert not hint.get_visible()


@pytest.mark.parametrize("code", ("uk", "es", "pl", "de", "fr", "it", "ro", "cs", "lt"))
def test_hint_texts_are_translated(code):
    catalog = start.catalog(code)
    assert [t for t in layout_hint.TEXTS if t not in catalog] == []
