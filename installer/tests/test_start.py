# SPDX-License-Identifier: GPL-3.0-or-later
import json
import re
import stat
from pathlib import Path

import pytest

from vilna_installer import start

SRC = Path(__file__).parent.parent / "src" / "vilna_installer"


def test_the_ten_languages_of_adr_0009():
    assert start.CODES == ("uk", "en", "es", "pl", "de", "fr", "it", "ro", "cs", "lt")
    assert len({lang.name for lang in start.LANGUAGES}) == 10


@pytest.mark.parametrize("code", [c for c in start.CODES if c != "en"])
def test_every_text_is_translated(code):
    catalog = start.catalog(code)
    missing = [t for t in start.TEXTS if t not in catalog]
    assert missing == []
    assert start.Texts(code)(start.RELEASE_CHIP).count("Blavet") == 1


def test_english_is_the_msgid_and_unknown_texts_stay_english():
    assert start.Texts("en")(start.WELCOME) == "Welcome to Vilna"
    assert start.Texts("uk")("Something new") == "Something new"


def test_the_texts_used_by_the_window_are_the_known_ones():
    used = set(re.findall(r"_\(start\.([A-Z_]+)\)", (SRC / "welcome.py").read_text()))
    assert used <= {
        "WELCOME",
        "RELEASE_CHIP",
        "LEAD",
        "SCREEN_READER",
        "WITHOUT",
        "WITH_GLOXI",
        "TITLE",
    }
    assert len(used) == len(start.TEXTS)


@pytest.mark.parametrize(
    ("env", "code"),
    [
        ({"LANG": "uk_UA.UTF-8"}, "uk"),
        ({"LANGUAGE": "ro:en", "LANG": "pl_PL.UTF-8"}, "ro"),
        ({"LANG": "ja_JP.UTF-8"}, "en"),
        ({"LANG": "C.UTF-8"}, "en"),
        ({}, "en"),
    ],
)
def test_default_language(env, code):
    assert start.default_language(env) == code


def test_plan_starts_unconfirmed_and_rejects_unknown_languages(tmp_path):
    plan = start.start_plan("lt", True, False)
    assert plan == {
        "version": 1,
        "ui": {"language": "lt", "screen_reader": True},
        "assistant": False,
        "confirmed_by_user": False,
    }
    with pytest.raises(ValueError, match="one of uk"):
        start.start_plan("xx", False, True)
    path = tmp_path / "run" / "plan.json"
    start.write_plan(plan, path)
    assert json.loads(path.read_text()) == plan
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert list(path.parent.iterdir()) == [path]


def test_the_blavet_marks_ship_with_the_package():
    for variant in ("light", "dark"):
        svg = (SRC / "data" / f"blavet-mark-{variant}.svg").read_text()
        assert svg.startswith("<svg") and "c2pa" not in svg
