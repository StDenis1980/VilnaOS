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
        "OTHER",
        "SEARCH",
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
        "ui": {
            "language": "lt",
            "locale": "lt_LT.UTF-8",
            "translated": True,
            "screen_reader": True,
        },
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


SUPPORTED_SAMPLE = """\
de_AT.UTF-8 UTF-8
de_AT ISO-8859-1
pt_BR.UTF-8 UTF-8
pt_BR ISO-8859-1
sr_RS@latin UTF-8
uk_UA.UTF-8 UTF-8
crh_UA UTF-8
ja_JP.EUC-JP EUC-JP
not a line
"""


def test_supported_utf8_locales_are_normalized():
    assert start.parse_supported(SUPPORTED_SAMPLE) == [
        "de_AT.UTF-8",
        "pt_BR.UTF-8",
        "sr_RS.UTF-8@latin",
        "uk_UA.UTF-8",
        "crh_UA.UTF-8",
    ]
    assert start.normalize_locale("pt_PT.utf8") == "pt_PT.UTF-8"
    assert start.normalize_locale("C.utf8") is None
    assert start.normalize_locale("ja_JP.eucjp") is None


def test_system_locales_falls_back_to_locale_a(tmp_path):
    supported = tmp_path / "SUPPORTED"
    supported.write_text(SUPPORTED_SAMPLE)
    assert "pt_BR.UTF-8" in start.system_locales(supported)
    # Without the file, `locale -a`: at least it does not fail.
    assert isinstance(start.system_locales(tmp_path / "missing"), list)


def test_other_languages_leave_out_the_ten_and_sort_by_own_name():
    names = {"pt_BR.UTF-8": "Português (Brasil)", "de_AT.UTF-8": "Deutsch (Österreich)"}
    rows = start.other_languages(
        ["uk_UA.UTF-8", "pt_BR.UTF-8", "de_AT.UTF-8", "crh_UA.UTF-8", "bad"], names.get
    )
    assert rows == [
        ("crh_UA.UTF-8", "crh_UA.UTF-8"),  # no name known: the locale itself
        ("de_AT.UTF-8", "Deutsch (Österreich)"),
        ("pt_BR.UTF-8", "Português (Brasil)"),
    ]


def test_plan_for_another_language_says_it_is_not_translated():
    plan = start.start_plan("pt", False, True, locale="pt_BR.UTF-8")
    assert plan["ui"] == {
        "language": "pt",
        "locale": "pt_BR.UTF-8",
        "translated": False,
        "screen_reader": False,
    }
    # A region of one of the ten languages keeps its translation.
    assert start.start_plan("de", False, True, locale="de_AT.UTF-8")["ui"]["translated"]
    with pytest.raises(ValueError, match="not in language"):
        start.start_plan("pt", False, True, locale="de_AT.UTF-8")
    with pytest.raises(ValueError, match="not a UTF-8 locale"):
        start.start_plan("pt", False, True, locale="pt_BR.ISO-8859-1")


def test_the_menu_entry_is_named_in_the_ten_languages():
    desktop = (
        SRC.parent.parent / "data" / "io.github.stdenis1980.VilnaInstaller.desktop"
    ).read_text(encoding="utf-8")
    entry = dict(
        line.split("=", 1)
        for line in desktop.splitlines()
        if "=" in line and not line.startswith("#")
    )
    assert entry["Exec"] == "vilna-installer"
    assert entry["Icon"] == "vilna-installer"
    for key in ("Name", "Comment"):
        assert entry[key]
        for code in start.CODES:
            if code != "en":
                assert entry.get(f"{key}[{code}]"), f"{key}[{code}]"


def test_euro_is_the_same_utf8_locale():
    assert start.normalize_locale("pt_PT.UTF-8@euro") == "pt_PT.UTF-8"
    assert start.parse_supported("pt_PT.UTF-8@euro UTF-8\npt_PT.UTF-8 UTF-8\n") == [
        "pt_PT.UTF-8",
        "pt_PT.UTF-8",
    ]


def test_the_wallpaper_loses_the_corner_with_the_vilna_mark():
    # The mark in the 3840x2160 wallpapers: x 130-250, y 1940-2070.
    x, y, width, height = start.wallpaper_crop(3840, 2160)
    assert (x, y) == (461, 0)
    assert x > 250 and y + height < 1940
    assert abs(width / height - 16 / 9) < 0.01
    assert start.wallpaper_crop(1, 1) == (0, 0, 1, 1)


def test_a_row_chosen_right_after_the_dialog_opens_does_not_count():
    assert not start.pick_allowed(1_000_000, 1_000_000 + 200_000)
    assert start.pick_allowed(1_000_000, 1_000_000 + start.PICK_GUARD_MS * 1000)
