# SPDX-License-Identifier: GPL-3.0-or-later
"""The installer ships Vilna's shared styles (docs/design/style/)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vilna_installer import style

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", style.STYLE_FILES)
def test_styles_ship_and_match_the_design_source(name):
    shipped = style.stylesheet(name)
    assert "@define-color vilna_" in shipped
    source = REPO / "docs" / "design" / "style" / name
    if source.exists():  # only in a checkout of the whole repository
        assert shipped == source.read_text(encoding="utf-8"), (
            f"copy docs/design/style/{name} to installer/src/vilna_installer/data/style/"
        )


def test_backup_classes_are_in_the_shared_styles():
    css = style.stylesheet("vilna.css")
    for name in (".vilna-key", ".vilna-go"):
        assert name in css


def test_missing_style_file_is_empty():
    assert style.stylesheet("no-such.css") == ""


def test_window_styles_take_vilna_colours_not_raw_hex():
    pytest.importorskip("gi.repository", exc_type=ImportError)
    from vilna_installer import chat, confirm_window, welcome, wizard_window

    for css in (chat.CSS, confirm_window.CSS, welcome.CSS, wizard_window.CSS):
        assert "#F5C24A" not in css and "@error_color" not in css


@pytest.mark.parametrize("dark", (True, False))
def test_installer_surfaces_replace_libadwaita_white_and_grey(dark):
    css = style.stylesheet(style.SURFACE_FILES[dark])
    for name in ("window_bg_color", "view_bg_color", "card_bg_color", "headerbar_bg_color"):
        assert f"@define-color {name} " in css
    # Night blue ground of the dark theme, warm cream of the light one (brief, section 2).
    assert ("#0E1A2E" if dark else "#F7F1E6") in css


def test_light_by_day_night_blue_at_night():
    assert style.dark_by_clock(3) and style.dark_by_clock(22) and style.dark_by_clock(6)
    assert not style.dark_by_clock(7) and not style.dark_by_clock(13)
    assert not style.dark_by_clock(19) and style.dark_by_clock(20)


def test_the_choice_is_kept_and_wins_over_the_clock(tmp_path, monkeypatch):
    monkeypatch.setenv("VILNA_INSTALLER_THEME_FILE", str(tmp_path / "theme"))
    monkeypatch.setattr(style, "dark_by_clock", lambda hour=None: True)
    assert style.saved_choice() is None and style.wanted_dark()
    style.save_choice(False)
    assert style.saved_choice() is False and not style.wanted_dark()
    (tmp_path / "theme").write_text("purple")
    assert style.saved_choice() is None


def test_unwritable_choice_file_does_not_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("VILNA_INSTALLER_THEME_FILE", str(tmp_path / "no" / "dir" / "theme"))
    style.save_choice(True)
    assert style.saved_choice() is None


@pytest.fixture
def gtk(tmp_path, monkeypatch):
    pytest.importorskip("gi.repository", exc_type=ImportError)
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    if not Gtk.init_check():
        pytest.skip("no display")
    Adw.init()
    monkeypatch.setenv("VILNA_INSTALLER_THEME_FILE", str(tmp_path / "theme"))
    manager = Adw.StyleManager.get_default()
    before = manager.get_color_scheme()
    yield Adw, Gtk
    manager.set_color_scheme(before)


def test_the_theme_follows_the_clock_until_the_button_is_pressed(gtk, monkeypatch):
    Adw, Gtk = gtk
    manager = Adw.StyleManager.get_default()
    monkeypatch.setattr(style, "dark_by_clock", lambda hour=None: False)
    style.apply()
    assert manager.get_color_scheme() == Adw.ColorScheme.FORCE_LIGHT
    button = style.theme_button(lambda text: text.upper())
    assert button.get_tooltip_text() == style.TO_DARK.upper()
    button.emit("clicked")
    assert manager.get_dark() and style.saved_choice() is True
    assert button.get_tooltip_text() == style.TO_LIGHT.upper()
    # Night falls, but the person chose: the clock no longer decides.
    monkeypatch.setattr(style, "dark_by_clock", lambda hour=None: True)
    button.emit("clicked")
    style.apply()
    assert not manager.get_dark()


def test_every_installer_screen_shows_the_theme_button(gtk):
    Adw, Gtk = gtk
    from vilna_installer import chat, confirm_window, install_window, welcome, wizard_window

    for module in (chat, confirm_window, install_window, welcome, wizard_window):
        assert "style.theme_button(" in Path(module.__file__).read_text(encoding="utf-8"), module


@pytest.mark.parametrize("code", ("uk", "es", "pl", "de", "fr", "it", "ro", "cs", "lt"))
def test_theme_button_texts_are_translated(code):
    from vilna_installer import start

    catalog = start.catalog(code)
    assert [t for t in style.TEXTS if t not in catalog] == []
