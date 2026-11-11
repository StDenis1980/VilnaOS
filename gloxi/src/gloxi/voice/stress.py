# SPDX-License-Identifier: GPL-3.0-or-later
"""Stress marks for the Ukrainian voice, from a downloaded dictionary.

The Ukrainian voice reads letters, so it has to guess where a word is
stressed, and often guesses wrong ("зАмок" for "замОк" is the least of it).
With the dictionary of lang-uk's ukrainian-word-stress (``models.STRESS_UK``)
every known word gets U+0301 after its stressed vowel, which the voice reads.

Only the dictionary is used, without its part-of-speech model: a word whose
stress depends on its meaning or form (замок, руки) is left as it is, and
so is a word the dictionary does not know. Without the dictionary, or
without the marisa-trie module, the text passes through unchanged.

The dictionary format (from ukrainian-word-stress, MIT): a marisa BytesTrie
from word to one value. A value without 0xFF lists the stress positions, one
byte each: the mark goes after that many letters (after the vowel). Otherwise the value
holds records split by 0xFF, each "positions 0xFE tags"; if all records agree
on the positions they are used, if not the word is ambiguous.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

log = logging.getLogger(__name__)

MARK = "́"
DICTIONARY = "stress.trie"
RECORDS = 0xFF
TAGS = 0xFE

# Letters, joined by apostrophes or hyphens (м'яч, буль-буль), as the dictionary has them.
WORD = re.compile(r"[^\W\d_]+(?:['’ʼ`-][^\W\d_]+)*")
APOSTROPHES = str.maketrans("’ʼ`", "'''")
LONGEST = 64  # no Ukrainian word is longer; a longer run is not looked up


def positions(value: bytes) -> tuple[int, ...]:
    """The stress positions in a dictionary value, or () if they depend on the form."""
    if RECORDS not in value:
        return tuple(value)
    found: set[tuple[int, ...]] = set()
    for record in value.split(bytes([RECORDS])):
        if record:
            found.add(tuple(record.partition(bytes([TAGS]))[0]))
    return found.pop() if len(found) == 1 else ()


def mark(word: str, where: tuple[int, ...]) -> str:
    """``word`` with U+0301 inserted at each position; a bad position leaves it alone."""
    if not where or any(not 0 < p <= len(word) for p in where):
        return word
    for p in sorted(set(where), reverse=True):
        word = word[:p] + MARK + word[p:]
    return word


class Stresses:
    """Looks words up in the dictionary in ``folder``; opened on first use."""

    def __init__(self, folder: Path | None) -> None:
        self.folder = folder
        self._lock = threading.Lock()
        self._trie = None
        self._tried = False

    def _open(self):
        with self._lock:
            if not self._tried:
                self._tried = True
                self._trie = load(self.folder / DICTIONARY) if self.folder else None
            return self._trie

    def value(self, trie, word: str) -> bytes | None:
        candidates = [word, word.lower(), word.title(), word.capitalize()]
        plain = word.translate(APOSTROPHES)
        if plain != word:
            candidates += [plain, plain.lower(), plain.title(), plain.capitalize()]
        for candidate in candidates:
            try:
                values = trie.get(candidate)
            except (KeyError, UnicodeError):
                values = None
            if values:
                return values[0]
        return None

    def word(self, trie, word: str) -> str | None:
        """The word with its stress, or None if the dictionary does not have it."""
        value = self.value(trie, word)
        if value is None:
            return None
        return mark(word, positions(value))

    def __call__(self, text: str) -> str:
        trie = self._open()
        if trie is None or MARK in text:  # already marked by hand: read as written
            return text
        out: list[str] = []
        end = 0
        for match in WORD.finditer(text):
            word = match.group()
            if len(word) > LONGEST:
                continue
            stressed = self.word(trie, word)
            if stressed is None and "-" in word:  # Київ-Львів: each part alone
                stressed = "-".join(self.word(trie, part) or part for part in word.split("-"))
            if stressed is not None and stressed != word:
                out.append(text[end : match.start()])
                out.append(stressed)
                end = match.end()
        out.append(text[end:])
        return "".join(out)


def load(path: Path):
    """The dictionary as a marisa BytesTrie, or None (missing, unreadable, no module)."""
    if not path.is_file():
        return None
    try:
        import marisa_trie
    except ImportError:
        log.warning("marisa-trie is not installed: Ukrainian is read without stress marks")
        return None
    trie = marisa_trie.BytesTrie()
    try:
        trie.mmap(str(path))
    except Exception as exc:  # the C++ library raises its own errors
        log.warning("the stress dictionary could not be opened: %s", exc)
        return None
    return trie
