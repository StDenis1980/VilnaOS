# SPDX-License-Identifier: GPL-3.0-or-later
"""The Herkos window's look by level (design wave 2) and the shared styles."""

from pathlib import Path

import pytest

from herkos import style
from herkos.dialog import (
    default_choice,
    needs_acknowledgement,
    risk_label,
    risk_level,
    shown_choices,
)

REPO = Path(__file__).resolve().parents[2]


def question(level="R2", choices=("once", "session", "always"), taint=()):
    return {"level": level, "choices": list(choices), "taint_sources": list(taint)}


def test_level_is_shown_and_unknown_is_strictest():
    assert risk_level(question("r3")) == "R3"
    assert risk_level(question("R9")) == "R4"
    assert risk_level({}) == "R4"


def test_an_r4_change_that_can_be_undone_does_not_say_it_cannot():
    # A return of the system to a saved state: the chip said "cannot be
    # undone" above "Can be undone." (laptop, 2026-11-26).
    assert "cannot" not in risk_label({**question("R4"), "undoable": True})
    assert "cannot" in risk_label(question("R4"))
    assert risk_label({**question("R2"), "undoable": True}) != risk_label(question("R4"))


def test_choices_come_from_herkos_unchanged_for_plain_questions():
    assert shown_choices(question("R2")) == ["once", "session", "always"]
    assert shown_choices(question("R3", ("once", "session"))) == ["once", "session"]
    assert shown_choices(question("R2", ("once", "forever"))) == ["once"]


def test_suspicious_content_and_r4_leave_only_allow_once():
    assert shown_choices(question("R2", taint=("web:example.net",))) == ["once"]
    assert shown_choices(question("R4", ("once", "always"))) == ["once"]


def test_always_allow_only_up_to_r2():
    assert "always" in shown_choices(question("R1"))
    assert "always" in shown_choices(question("R2"))
    assert shown_choices(question("R3")) == ["once", "session"]


def test_every_level_has_a_shape_as_well_as_a_colour():
    from herkos.dialog import RISK_ICONS, RISK_LABELS

    assert set(RISK_ICONS) == set(RISK_LABELS)
    assert len(set(RISK_ICONS.values())) == len(RISK_ICONS)
    assert all(name.endswith("-symbolic") for name in RISK_ICONS.values())


def test_only_r4_waits_for_the_tick():
    assert needs_acknowledgement(question("R4"))
    assert not needs_acknowledgement(question("R3"))


@pytest.mark.parametrize("name", style.STYLE_FILES)
def test_styles_ship_and_match_the_design_source(name):
    shipped = style.stylesheet(name)
    assert "@define-color vilna_" in shipped
    source = REPO / "docs" / "design" / "style" / name
    if source.exists():  # only in a checkout of the whole repository
        assert shipped == source.read_text(encoding="utf-8"), (
            f"copy docs/design/style/{name} to herkos/src/herkos/data/style/"
        )


def test_missing_style_is_empty_not_an_error():
    assert style.stylesheet("nope.css") == ""


def test_enter_presses_the_safest_button_for_the_level():
    plain = {"choices": ["once", "session", "always"]}
    assert default_choice({**plain, "level": "R1"}) == "once"
    assert default_choice({**plain, "level": "R2"}) == "once"
    assert default_choice({**plain, "level": "R3"}) == "deny"
    assert default_choice({**plain, "level": "R4"}) == "deny"
    assert default_choice({**plain, "level": "??"}) == "deny"  # shown as R4
    tainted = {**plain, "level": "R2", "taint_sources": ["web"]}
    assert default_choice(tainted) == "deny"
    assert default_choice({"level": "R2", "choices": ["session"]}) == "deny"


def test_risk_icons_ship_and_match_the_design_source():
    """Each level's shield is in data/icons, the same file as in the Vilna icon theme."""
    from herkos.dialog import RISK_ICONS

    icons = Path(style.icon_dir() or "") / "hicolor" / "symbolic" / "apps"
    theme = REPO / "docs" / "design" / "icons" / "system" / "Vilna" / "symbolic" / "status"
    for name in RISK_ICONS.values():
        shipped = icons / f"{name}.svg"
        assert shipped.is_file(), f"{name}.svg missing from {icons}/"
        source = theme / f"{name}.svg"
        if source.exists():  # only in a checkout of the whole repository
            assert shipped.read_bytes() == source.read_bytes(), (
                f"copy {source.relative_to(REPO)} to {icons}/"
            )
