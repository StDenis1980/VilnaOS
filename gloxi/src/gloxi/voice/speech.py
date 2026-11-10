# SPDX-License-Identifier: GPL-3.0-or-later
"""What Gloxi says when it reads an answer aloud (ADR 0015, section 6).

- A hidden password (``••••••``, gloxi.router) is said as "password hidden",
  never letter by letter.
- Markdown marks are not read; a link is read by its text, a bare address
  as "link"; code is not read, the person is told it is on the screen.
- Text is cut into sentences, so the first one is heard while the rest is
  still being made.
- The Ukrainian voice reads letters only, so numbers are spelled out.
"""

from __future__ import annotations

import re
from gettext import gettext as _

from gloxi.i18n import N_

HIDDEN = re.compile(r"•{3,}")
CODE_BLOCK = re.compile(r"```.*?(```|$)", re.DOTALL)
INLINE_CODE = re.compile(r"`([^`\n]*)`")
LINK = re.compile(r"\[([^\]\n]*)\]\([^)\s]*\)")
URL = re.compile(r"\b(?:https?://|www\.)\S+?(?=[.,!?;:)]*(?:\s|$))", re.IGNORECASE)
MARKS = re.compile(r"(\*\*|__|\*|~~|^#{1,6}\s+|^\s*>\s?|^\s*[-*+]\s+|^\s*\d+[.)]\s+)", re.M)
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n+")
# A sentence longer than this is cut at a comma or a space, so that the
# first sound comes quickly and a run-on answer does not hold the voice.
MAX_SENTENCE = 240

PASSWORD_HIDDEN = N_("password hidden")
LINK_WORD = N_("link")
CODE_ON_SCREEN = N_("The code is on the screen.")


def spoken(text: str) -> str:
    """``text`` as it should be heard."""
    text = CODE_BLOCK.sub(" " + _(CODE_ON_SCREEN) + " ", text)
    text = HIDDEN.sub(_(PASSWORD_HIDDEN), text)
    text = LINK.sub(r"\1", text)
    text = URL.sub(_(LINK_WORD), text)
    text = INLINE_CODE.sub(r"\1", text)
    text = MARKS.sub("", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def sentences(text: str) -> list[str]:
    """``spoken(text)`` cut into sentences of at most ``MAX_SENTENCE`` characters."""
    out: list[str] = []
    for part in SENTENCE_END.split(spoken(text)):
        part = part.strip()
        while len(part) > MAX_SENTENCE:
            cut = part.rfind(", ", 0, MAX_SENTENCE)
            if cut < MAX_SENTENCE // 3:
                cut = part.rfind(" ", 0, MAX_SENTENCE)
            if cut <= 0:
                cut = MAX_SENTENCE
            out.append(part[: cut + 1].strip())
            part = part[cut + 1 :].strip()
        if any(c.isalnum() for c in part):
            out.append(part)
    return out


# -- Ukrainian numbers -------------------------------------------------------------

_ONES = ["нуль", "один", "два", "три", "чотири", "п'ять", "шість", "сім", "вісім", "дев'ять"]
_TEENS = [
    "десять",
    "одинадцять",
    "дванадцять",
    "тринадцять",
    "чотирнадцять",
    "п'ятнадцять",
    "шістнадцять",
    "сімнадцять",
    "вісімнадцять",
    "дев'ятнадцять",
]
_TENS = [
    "_",
    "_",
    "двадцять",
    "тридцять",
    "сорок",
    "п'ятдесят",
    "шістдесят",
    "сімдесят",
    "вісімдесят",
    "дев'яносто",
]
_HUNDREDS = [
    "_",
    "сто",
    "двісті",
    "триста",
    "чотириста",
    "п'ятсот",
    "шістсот",
    "сімсот",
    "вісімсот",
    "дев'ятсот",
]
# (one, two-four, five+) and whether the group is feminine.
_SCALES = (
    (("", "", ""), False),
    (("тисяча", "тисячі", "тисяч"), True),
    (("мільйон", "мільйони", "мільйонів"), False),
    (("мільярд", "мільярди", "мільярдів"), False),
)


def _triple(n: int, feminine: bool) -> list[str]:
    words: list[str] = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        words.append(_HUNDREDS[hundreds])
    if 10 <= rest < 20:
        words.append(_TEENS[rest - 10])
        return words
    tens, ones = divmod(rest, 10)
    if tens:
        words.append(_TENS[tens])
    if ones:
        if feminine and ones in (1, 2):
            words.append("одна" if ones == 1 else "дві")
        else:
            words.append(_ONES[ones])
    return words


def _form(n: int, forms: tuple[str, str, str]) -> str:
    if 10 <= n % 100 < 20:
        return forms[2]
    last = n % 10
    return forms[0] if last == 1 else forms[1] if 2 <= last <= 4 else forms[2]


def uk_number(n: int) -> str:
    """A whole number in Ukrainian words (up to 999 999 999 999)."""
    if n == 0:
        return _ONES[0]
    if n < 0:
        return "мінус " + uk_number(-n)
    if n >= 10**12:
        return " ".join(_ONES[int(d)] for d in str(n))
    words: list[str] = []
    for scale in range(len(_SCALES) - 1, -1, -1):
        group = (n // 1000**scale) % 1000
        if not group:
            continue
        forms, feminine = _SCALES[scale]
        words += _triple(group, feminine)
        if scale:
            words.append(_form(group, forms))
    return " ".join(words)


# Digit groups ("12 500") or plain digits, with an optional decimal part.
_NUMBER = re.compile(r"(?<![\w,.])(-?\d{1,3}(?:[   ]\d{3})+|-?\d+)(?:[,.](\d+))?(?![\w])")


def uk_numbers(text: str) -> str:
    """Spell out the numbers of a Ukrainian sentence ("о 9:30" → "о дев'ять тридцять")."""

    def say(m: re.Match) -> str:
        whole = re.sub(r"[\s  ]", "", m.group(1))
        words = uk_number(int(whole))
        if m.group(2):
            words += " кома " + uk_number(int(m.group(2)))
        return words

    text = re.sub(r"(\d):(\d\d)\b", r"\1 \2", text)
    text = text.replace("%", " відсотків")
    return _NUMBER.sub(say, text)
