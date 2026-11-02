# SPDX-License-Identifier: GPL-3.0-or-later
import re
from pathlib import Path

import pytest

from herkos import i18n
from herkos.confirm import _LABELS, Question, choices_for, render
from herkos.dialog import LABELS
from herkos.keros import RULES
from herkos.manifest import ManifestRegistry
from herkos.risk import Level

SRC = Path(__file__).parent.parent / "src" / "herkos"


def used_messages() -> set[str]:
    found = set()
    for py in SRC.glob("*.py"):
        found |= set(re.findall(r'_\("((?:[^"\\]|\\.)*)"\)', py.read_text(encoding="utf-8")))
    found |= {r.description for r in RULES}
    found.add("contains invisible or direction-changing characters")
    found |= set(_LABELS.values()) | set(LABELS.values())
    for tool in ("org.vilna.mcp.files",):
        found |= {op.summary for op in ManifestRegistry.builtin().get(tool).operations.values()}
    return found


def test_every_message_is_translated_to_ukrainian():
    catalog = i18n.load(["uk"])
    missing = sorted(m for m in used_messages() if m not in catalog)
    assert missing == []


def test_placeholders_survive_translation():
    for msgid, msgstr in i18n.load(["uk"]).items():
        assert set(re.findall(r"\{\w+\}", msgid)) == set(re.findall(r"\{\w+\}", msgstr)), msgid


def test_language_from_environment(monkeypatch):
    monkeypatch.setenv("LANGUAGE", "")
    monkeypatch.setenv("LC_ALL", "")
    monkeypatch.setenv("LC_MESSAGES", "")
    monkeypatch.setenv("LANG", "uk_UA.UTF-8")
    assert i18n.languages()[:2] == ["uk_UA", "uk"]
    monkeypatch.setenv("LANG", "C.UTF-8")
    assert i18n.languages() == []


def test_parse_po_skips_fuzzy_and_joins_lines():
    po = 'msgid "a"\nmsgstr ""\n"b"\n"c"\n\n#, fuzzy\nmsgid "x"\nmsgstr "y"\n'
    assert i18n.parse_po(po) == {"a": "bc"}


@pytest.fixture
def ukrainian():
    i18n.use(["uk"])
    yield
    i18n.use([])


def test_terminal_question_in_ukrainian(ukrainian):
    q = Question(
        summary="Змінити файл",
        reason="так попросили",
        tool="org.vilna.mcp.files",
        operation="write",
        resources=("file:///a",),
        level=Level.R2,
        undoable=True,
        raises=(),
        taint_sources=(),
        choices=choices_for(Level.R2, True),
    )
    text = render(q)
    assert "Gloxi просить дозволу" in text
    assert "Можна скасувати." in text
    assert "[n] відхилити" in text
