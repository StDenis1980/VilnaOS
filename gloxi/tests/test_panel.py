# SPDX-License-Identifier: GPL-3.0-or-later
"""The side panel, stage A: sizes, saved settings, translucency, the owl's states."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from gloxi.gui import panel
from gloxi.gui.panel import OwlState, OwlStates, PanelPrefs

GEOMETRY = (
    Path(__file__).resolve().parents[2]
    / "shell-extension"
    / "vilna@stdenis1980.github.io"
    / "lib"
    / "geometry.js"
)


# The RPM builds gloxi/ alone; the check runs in the repository (CI job gloxi).
@pytest.mark.skipif(not GEOMETRY.is_file(), reason="needs the repository's shell-extension/")
def test_numbers_agree_with_the_shell_extension():
    js = GEOMETRY.read_text(encoding="utf-8")

    def const(name: str) -> str:
        found = re.search(rf"export const {name} = ([^;]+);", js)
        assert found, name
        return found.group(1)

    assert int(const("PANEL_COMPACT_HEIGHT")) == panel.COMPACT_HEIGHT
    assert int(const("PANEL_MIN_HEIGHT")) == panel.MIN_HEIGHT
    assert int(const("PANEL_MARGIN")) == panel.MARGIN
    assert int(const("DOCK_MIN_WIDTH")) == panel.MIN_WIDTH
    assert json.loads(const("PANEL_SIZES").replace("'", '"')) == list(panel.SIZES)


@pytest.mark.parametrize(
    ("size", "screen", "want"),
    [
        ("one-sixth", (1920, 1080), (320, 1064)),
        ("one-third", (1920, 1080), (640, 1064)),
        ("one-sixth", (3840, 2160), (640, 2144)),
        ("compact", (1920, 1080), (320, 220)),
        # Never wider than half a small screen, never taller than it.
        ("one-sixth", (500, 200), (250, 184)),
    ],
)
def test_panel_size(size, screen, want):
    assert panel.panel_size(PanelPrefs(size=size), *screen) == want


def test_custom_size_is_kept_but_fits_the_screen():
    prefs = PanelPrefs(size="custom", custom_width=500, custom_height=5000)
    assert panel.panel_size(prefs, 1920, 1080) == (500, 1064)
    tiny = PanelPrefs(size="custom", custom_width=400, custom_height=10)
    assert panel.panel_size(tiny, 1920, 1080) == (400, panel.MIN_HEIGHT)


def test_junk_screen_sizes_do_not_crash():
    for screen in [(0, 0), (-5, float("nan")), ("x", None), (float("inf"), 1e12)]:
        width, height = panel.panel_size(PanelPrefs(), *screen)
        assert width >= 1 and height >= 1


def test_a_dragged_edge_becomes_the_own_size():
    prefs = PanelPrefs(size="one-sixth")
    # The Shell makes a tall panel shorter than the screen: still a sixth.
    assert panel.after_resize(prefs, 320, 1032, 1920, 1080) == prefs
    dragged = panel.after_resize(prefs, 480, 900, 1920, 1080)
    assert (dragged.size, dragged.custom_width, dragged.custom_height) == ("custom", 480, 900)
    # Compact counts its height too.
    small = PanelPrefs(size="compact")
    assert panel.after_resize(small, 320, 220, 1920, 1080) == small
    assert panel.after_resize(small, 320, 500, 1920, 1080).size == "custom"
    # An own size dragged again keeps the new size.
    again = panel.after_resize(dragged, 600, 700, 1920, 1080)
    assert (again.size, again.custom_width) == ("custom", 600)
    # A window not drawn yet changes nothing.
    assert panel.after_resize(prefs, 0, 0, 1920, 1080) == prefs


def test_prefs_round_trip(tmp_path):
    path = tmp_path / "gloxi" / "window.json"
    prefs = PanelPrefs(size="custom", custom_width=500, custom_height=700, opacity=0.6)
    panel.save_prefs(prefs, path)
    assert panel.load_prefs(path) == prefs
    assert not list(path.parent.glob(".window-*"))  # no temporary file left


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[1, 2]",
        '{"size": "huge", "opacity": "lots", "translucent": "yes"}',
        '{"opacity": NaN}',
        '{"opacity": 1e309, "custom_width": -4, "custom_height": true}',
        '{"size": "custom"}',
    ],
)
def test_broken_prefs_give_defaults(tmp_path, text):
    path = tmp_path / "window.json"
    path.write_text(text, encoding="utf-8")
    prefs = panel.load_prefs(path)
    assert prefs.size in ("one-sixth",)
    assert panel.MIN_OPACITY <= prefs.opacity <= 1.0
    assert isinstance(prefs.translucent, bool)


def test_missing_prefs_and_an_unwritable_folder(tmp_path):
    assert panel.load_prefs(tmp_path / "none.json") == PanelPrefs()
    blocker = tmp_path / "file"
    blocker.write_text("x")
    panel.save_prefs(PanelPrefs(), blocker / "window.json")  # no exception


def test_prefs_path_is_next_to_the_config():
    assert panel.prefs_path({"XDG_CONFIG_HOME": "/c"}) == Path("/c/gloxi/window.json")


def test_opacity_is_clamped():
    assert panel.clean(PanelPrefs(), {"opacity": 0.01}).opacity == panel.MIN_OPACITY
    assert panel.clean(PanelPrefs(), {"opacity": 7}).opacity == 1.0


def test_translucency_is_off_in_high_contrast_and_when_turned_off():
    prefs = PanelPrefs(opacity=0.7)
    assert panel.background_opacity(prefs, high_contrast=False) == 0.7
    assert panel.background_opacity(prefs, high_contrast=True) == 1.0
    off = PanelPrefs(translucent=False, opacity=0.7)
    assert panel.background_opacity(off, high_contrast=False) == 1.0


def test_owl_follows_a_conversation():
    owl = OwlStates()
    assert owl.state is OwlState.IDLE
    owl.focus(True)
    assert owl.state is OwlState.LISTENING
    owl.set_busy(True)
    assert owl.state is OwlState.THINKING
    owl.got_piece()
    assert owl.state is OwlState.ANSWERING
    owl.set_busy(False)
    assert owl.state is OwlState.LISTENING
    owl.focus(False)
    assert owl.state is OwlState.IDLE


def test_owl_error_lasts_until_the_next_message():
    owl = OwlStates()
    owl.set_busy(True)
    owl.failed()
    owl.set_busy(False)
    assert owl.state is OwlState.ERROR
    owl.set_busy(True)
    assert owl.state is OwlState.THINKING


def test_waiting_for_the_person_comes_first():
    owl = OwlStates()
    owl.failed()
    owl.ask()
    owl.ask()
    assert owl.state is OwlState.WAITING
    owl.answered()
    assert owl.state is OwlState.WAITING
    owl.answered()
    owl.answered()  # one too many does not go below zero
    assert owl.state is OwlState.ERROR


def test_every_state_has_words_for_screen_readers():
    texts = {panel.state_text(state) for state in OwlState}
    assert len(texts) == len(OwlState)
