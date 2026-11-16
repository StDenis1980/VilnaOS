# SPDX-License-Identifier: GPL-3.0-or-later
"""The «Копії» window ships Vilna's shared styles (docs/design/style/)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vilna_backup import style

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", style.STYLE_FILES)
def test_styles_ship_and_match_the_design_source(name):
    shipped = style.stylesheet(name)
    assert "@define-color vilna_" in shipped
    source = REPO / "docs" / "design" / "style" / name
    if source.exists():  # only in a checkout of the whole repository
        assert shipped == source.read_text(encoding="utf-8"), (
            f"copy docs/design/style/{name} to backup/src/vilna_backup/data/style/"
        )


def test_backup_classes_are_in_the_shared_styles():
    css = style.stylesheet("vilna.css")
    for name in (".vilna-key", ".vilna-safe"):
        assert name in css


def test_missing_style_file_is_empty():
    assert style.stylesheet("no-such.css") == ""
