# SPDX-License-Identifier: GPL-3.0-or-later
"""Translations (ADR 0009): catalogues up to date, every main language complete and used."""

from __future__ import annotations

import gettext
import importlib.util
import sys
from pathlib import Path

import pytest

from gloxi.cli import HELP, yes_words
from gloxi.i18n import DOMAIN, LOCALE_DIR
from gloxi.tools import ToolOutcome

TOOL = Path(__file__).resolve().parents[1] / "tools" / "i18n.py"


def _tool():
    spec = importlib.util.spec_from_file_location("i18n_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up
    spec.loader.exec_module(module)
    return module


def test_catalogues_are_up_to_date(capsys):
    assert _tool().cmd_check() == 0, capsys.readouterr().err


def test_every_main_language_has_a_compiled_catalogue():
    for lang in _tool().MAIN_LANGUAGES:
        assert (LOCALE_DIR / lang / "LC_MESSAGES" / f"{DOMAIN}.mo").is_file(), lang


def test_ukrainian_is_used():
    uk = gettext.translation(DOMAIN, str(LOCALE_DIR), languages=["uk"])
    assert uk.gettext(HELP).startswith("Команди:")
    assert uk.gettext("[local]") == "[локально]"


@pytest.mark.parametrize(
    "lang, start",
    [
        ("es", "Órdenes:"),
        ("pl", "Polecenia:"),
        ("de", "Befehle:"),
        ("fr", "Commandes :"),
        ("it", "Comandi:"),
        ("ro", "Comenzi:"),
        ("cs", "Příkazy:"),
        ("lt", "Komandos:"),
    ],
)
def test_every_main_language_is_used(lang, start):
    t = gettext.translation(DOMAIN, str(LOCALE_DIR), languages=[lang])
    assert t.gettext(HELP).startswith(start)


def test_language_without_a_catalogue_falls_back_to_english():
    t = gettext.translation(DOMAIN, str(LOCALE_DIR), languages=["pt"], fallback=True)
    assert t.gettext(HELP) == HELP


@pytest.mark.parametrize("lang, letter", [("de", "j"), ("fr", "o"), ("ro", "d"), ("uk", "т")])
def test_the_yes_letter_of_the_prompt_is_accepted(monkeypatch, lang, letter):
    t = gettext.translation(DOMAIN, str(LOCALE_DIR), languages=[lang])
    monkeypatch.setattr("gloxi.cli._", t.gettext)
    assert f"[{letter}/" in t.gettext("Download it now? [y/N] ").casefold()
    assert letter in yes_words()
    assert "n" not in yes_words()


def test_mo_round_trip(tmp_path):
    tool = _tool()
    entries = {
        "Send": tool.Entry("Send", "Надіслати"),
        "Later": tool.Entry("Later", "Пізніше", fuzzy=True),
        "No": tool.Entry("No"),
    }
    path = tmp_path / "x.mo"
    path.write_bytes(tool.mo_bytes(tool.header_for("uk"), entries))
    with path.open("rb") as f:
        t = gettext.GNUTranslations(f)
    assert t.gettext("Send") == "Надіслати"
    assert t.gettext("Later") == "Later"  # fuzzy is not used
    assert t.gettext("No") == "No"


def test_po_round_trip(tmp_path):
    tool = _tool()
    msgid = 'Say "hi"\n\tnow'
    entries = {msgid: tool.Entry(msgid, "Скажи «привіт»\n\tзараз", ["a.py"])}
    path = tmp_path / "x.po"
    text = tool.write_po(path, tool.header_for("uk"), entries)
    header, back = tool.read_po(path)
    assert header == tool.header_for("uk")
    assert back[msgid].msgstr == "Скажи «привіт»\n\tзараз"
    assert tool.write_po(None, header, back) == text


@pytest.mark.parametrize("status", ["done", "denied", "error"])
def test_tool_summary_is_translatable(monkeypatch, status):
    uk = gettext.translation(DOMAIN, str(LOCALE_DIR), languages=["uk"])
    monkeypatch.setattr("gloxi.tools._", uk.gettext)
    outcome = ToolOutcome("read", "/srv/a.txt", status, "x", "no", "{}")
    word = {"done": "виконано", "denied": "відмовлено", "error": "помилка"}[status]
    assert word in outcome.summary()
