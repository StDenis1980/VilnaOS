# SPDX-License-Identifier: GPL-3.0-or-later
"""Which of the ten languages (ADR 0009) to listen for and to speak.

Gloxi answers in the language the person writes in, which is not always the
interface language. To read an answer with the right voice it is enough to
tell the ten languages apart on a sentence or more: Cyrillic is Ukrainian;
for the Latin ones, the letters only one language uses and its most common
short words decide. When nothing decides, the interface language wins.
"""

from __future__ import annotations

import os
import re

from gloxi.voice.models import LANGUAGES

# Letters that point at one language (or two).
LETTERS = {
    "pl": "ąęłńśźżó",
    "cs": "ěřůčšžťďň",
    "ro": "ăâîșşțţ",
    "lt": "ąčęėįšųūž",
    "de": "äöüß",
    "es": "ñ¿¡",
    "fr": "çœàèêëîïôûù",
    "it": "àèìòù",
}

# The most frequent short words, which almost every sentence has.
WORDS = {
    "en": "the and is are of to you it that this with for not have what",
    "de": "der die das und ist nicht ich sie es ein eine mit zu auf für",
    "fr": "le la les et est des une un pas que pour vous dans sur avec quel ce qui aujourd",
    "es": "el la los las y es que de en un una por para con no lo",
    "it": "il lo la gli le e è che di un una per non con sono questo",
    "pl": "i w nie jest to się na że z do co jak tak ale",
    "cs": "a je to se na že v s do jak ale jsem není co tak",
    "ro": "și este în nu că la cu o un pe mai sunt din ce",
    "lt": "ir yra kad bet tai su į ne kaip aš jūs dar arba",
}
_WORDS = {lang: set(words.split()) for lang, words in WORDS.items()}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def interface_language(env: dict[str, str] | None = None) -> str:
    """The first of the ten languages in LANGUAGE/LC_ALL/LC_MESSAGES/LANG, else en."""
    env = os.environ if env is None else env
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        codes = [
            part.split("_")[0].split(".")[0].split("@")[0].lower()
            for part in (env.get(var) or "").split(":")
        ]
        codes = [c for c in codes if c and c not in ("c", "posix")]
        if codes:
            # As gettext: the first language of the list Gloxi has, else English.
            return next((c for c in codes if c in LANGUAGES), "en")
    return "en"


def detect(text: str, fallback: str = "en") -> str:
    """The language of ``text``, or ``fallback`` when it cannot be told."""
    letters = [c for c in text.lower() if c.isalpha()]
    if not letters:
        return fallback
    cyrillic = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
    if cyrillic * 2 > len(letters):
        return "uk"
    words = _WORD.findall(text.lower())
    scores = dict.fromkeys(LANGUAGES, 0.0)
    scores.pop("uk")
    for word in words:
        for lang, common in _WORDS.items():
            if word in common:
                scores[lang] += 1
    for lang, special in LETTERS.items():
        scores[lang] += 0.5 * sum(1 for c in letters if c in special)
    best = max(scores.values())
    if best <= 0:
        return fallback
    winners = [lang for lang, score in scores.items() if score == best]
    return fallback if fallback in winners else winners[0]
