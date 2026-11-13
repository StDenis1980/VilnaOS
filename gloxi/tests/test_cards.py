# SPDX-License-Identifier: GPL-3.0-or-later
"""Action cards in the conversation (design wave 2) and the shared styles."""

from pathlib import Path

import pytest

from gloxi.gui import style
from gloxi.gui.cards import MAX_FIELD, card_view
from gloxi.gui.controller import show_action, show_outcome
from gloxi.gui.remote import RemoteController
from gloxi.tools import ToolOutcome

REPO = Path(__file__).resolve().parents[2]


class Notes:
    def __init__(self):
        self.events = []

    def show_note(self, text):
        self.events.append(("note", text))


class Cards(Notes):
    def show_action(self, card):
        self.events.append(("action", card))


def outcome(status="done", warnings=()):
    return ToolOutcome("read", "/srv/a.txt", status, "denied_user", "you said no", "{}", warnings)


def test_outcome_becomes_a_json_safe_card():
    card = outcome("denied", ("asks to ignore earlier instructions",)).card()
    assert card == {
        "program": "files",
        "operation": "read",
        "target": "/srv/a.txt",
        "status": "denied",
        "message": "you said no",
        "warnings": ["asks to ignore earlier instructions"],
    }
    assert outcome("done").card()["message"] == ""


def test_card_view_shows_status_title_and_detail():
    view = card_view(outcome("error").card())
    assert (view.status, view.status_label, view.title) == ("error", "Failed", "files: read")
    assert view.detail == "you said no"
    assert card_view(outcome("done").card()).detail == ""


@pytest.mark.parametrize(
    "card", [None, "x", {}, {"status": "hacked"}, {"warnings": "no list", "program": 5}]
)
def test_malformed_card_is_shown_as_a_failure(card):
    view = card_view(card)
    assert view.status == "error" and view.title == "Action" and view.warnings == ()


def test_long_fields_and_many_warnings_are_cut():
    view = card_view({"status": "done", "target": "a" * 1000, "warnings": ["w"] * 50})
    assert len(view.target) == MAX_FIELD and view.target.endswith("…")
    assert len(view.warnings) == 5


def test_window_with_cards_gets_a_card_terminal_gets_a_note():
    cards, notes = Cards(), Notes()
    show_outcome(cards, outcome())
    show_outcome(notes, outcome())
    assert cards.events[0][0] == "action"
    assert notes.events == [("note", "files: read /srv/a.txt (done)")]
    show_action(notes, outcome().card())
    assert notes.events[-1] == ("note", "files: read")


def test_service_action_event_reaches_the_window():
    view = Cards()
    remote = RemoteController(Path("/nonexistent"), view, language="en")
    remote._event("action", {"card": outcome().card()})
    remote._event("action", {"card": "not a card"})
    assert [e[0] for e in view.events] == ["action"]


@pytest.mark.parametrize("name", style.STYLE_FILES)
def test_styles_ship_and_match_the_design_source(name):
    shipped = style.stylesheet(name)
    assert "@define-color vilna_" in shipped
    source = REPO / "docs" / "design" / "style" / name
    if source.exists():  # only in a checkout of the whole repository
        assert shipped == source.read_text(encoding="utf-8"), (
            f"copy docs/design/style/{name} to gloxi/src/gloxi/data/style/"
        )


def test_switcher_groups_by_where_the_words_go():
    from gloxi.gui.controller import ModelChoice
    from gloxi.gui.switcher import sections, switch_note, tag

    local = ModelChoice("local", "Local", "here")
    cloud = ModelChoice("cloud", "Cloud", "there, masked")
    auto = ModelChoice("auto", "Auto", "short here, long there")
    got = sections([local, auto, cloud])
    assert [(s.title, [c.mode for c in s.choices]) for s in got] == [
        (None, ["auto"]),
        ("On this computer", ["local"]),
        ("In the cloud", ["cloud"]),
    ]
    # With the cloud off the section stays, with the reason, so it is findable.
    off = sections([local])
    assert off[-1].title == "In the cloud" and off[-1].choices == ()
    assert off[-1].note == "Turned off in the settings"
    assert [tag(m) for m in ("auto", "local", "cloud")] == ["recommended", "local", "cloud"]
    assert "Cloud" in switch_note(cloud)


def test_empty_panel_offers_starts():
    from gloxi.gui.cards import starter_prompts

    assert len(starter_prompts()) == 3
