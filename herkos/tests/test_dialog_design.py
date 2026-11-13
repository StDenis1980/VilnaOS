# SPDX-License-Identifier: GPL-3.0-or-later
"""The Herkos window's look by level (design wave 2) and the shared styles."""

from pathlib import Path

import pytest

from herkos import style
from herkos.dialog import needs_acknowledgement, risk_level, shown_choices

REPO = Path(__file__).resolve().parents[2]


def question(level="R2", choices=("once", "session", "always"), taint=()):
    return {"level": level, "choices": list(choices), "taint_sources": list(taint)}


def test_level_is_shown_and_unknown_is_strictest():
    assert risk_level(question("r3")) == "R3"
    assert risk_level(question("R9")) == "R4"
    assert risk_level({}) == "R4"


def test_choices_come_from_herkos_unchanged_for_plain_questions():
    assert shown_choices(question("R2")) == ["once", "session", "always"]
    assert shown_choices(question("R3", ("once", "session"))) == ["once", "session"]
    assert shown_choices(question("R2", ("once", "forever"))) == ["once"]


def test_suspicious_content_and_r4_leave_only_allow_once():
    assert shown_choices(question("R2", taint=("web:example.net",))) == ["once"]
    assert shown_choices(question("R4", ("once", "always"))) == ["once"]


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
