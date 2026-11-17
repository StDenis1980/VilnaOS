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
    pytest.importorskip("gi")
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


def test_installer_prefers_the_night_blue_theme():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    if not Gtk.init_check():
        pytest.skip("no display")
    Adw.init()
    style.install()
    assert Adw.StyleManager.get_default().get_color_scheme() == Adw.ColorScheme.PREFER_DARK
