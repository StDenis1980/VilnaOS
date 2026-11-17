# SPDX-License-Identifier: GPL-3.0-or-later
"""Every text in all ten languages of ADR 0009, with the same placeholders."""

import re

import pytest

from vilna_feedback import texts as t

PLACEHOLDER = re.compile(r"\{\w+\}")


@pytest.mark.parametrize("code", [c for c in t.CODES if c != "en"])
def test_every_text_is_translated(code):
    catalog = t.catalog(code)
    missing = [m for m in t.TEXTS if m not in catalog]
    assert not missing, f"{code}: {missing}"
    for msgid in t.TEXTS:
        assert sorted(PLACEHOLDER.findall(msgid)) == sorted(PLACEHOLDER.findall(catalog[msgid]))
    unused = set(catalog) - set(t.TEXTS)
    assert not unused, f"{code}: texts nobody shows: {unused}"


def test_every_shown_text_is_listed():
    shown = [
        value
        for name, value in vars(t).items()
        if name.isupper() and isinstance(value, str) and name not in {"CODES"}
    ]
    assert set(shown) <= set(t.TEXTS)


def test_the_answers_match_the_installer_words():
    # The same question and answers as in the installer (conversation.py).
    uk = t.Texts("uk")
    assert uk(t.ANSWER_ASK) == "Питати щоразу"
    assert uk(t.ANSWER) == "Якщо програма впаде, надсилати нам звіт про збій?"


def test_language_from_the_session():
    assert t.language({"LANGUAGE": "pl_PL:en"}) == "pl"
    assert t.language({"LANG": "de_DE.UTF-8"}) == "de"
    assert t.language({"LANG": "ja_JP.UTF-8"}) == "en"
    assert t.language({}) == "en"


def test_parse_po_skips_fuzzy_and_joins_lines():
    po = 'msgid "A"\nmsgstr ""\n"B"\n"C"\n\n#, fuzzy\nmsgid "D"\nmsgstr "E"\n'
    assert t.parse_po(po) == {"A": "BC"}
