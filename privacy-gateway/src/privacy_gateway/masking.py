# SPDX-License-Identifier: Apache-2.0
"""Reversible masking: hide values behind numbered placeholders and put them back.

One :class:`Masker` lives as long as one conversation. The same value always
gets the same placeholder (``[EMAIL_1]``), so the model can still tell that two
mentions are the same person, and placeholders stay stable across turns. The
real values never leave the device: they live only in this object's memory.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from privacy_gateway.detectors import Kind, detect

PLACEHOLDER_RE = re.compile(r"\[([A-Z_]+?)_(\d+)\]")
# What a model may make of a placeholder: another case, brackets or separator,
# a letter or two changed ("[Email_1]", "{EMAIL 1}", "[[EMIAL_1]]", "EMAIL_1").
# Measured on 2026-11-05: Mistral models turned {PERSON_2} into {Person_2} and
# {ОСОБА_1} into {ОСАБА_1}, so the real value was not put back.
LOOSE_RE = re.compile(
    r"[\[{(]{1,2}\s*(?P<kind>[^\W\d_]{2,}(?:[ _-][^\W\d_]+)*)(?P<sep>[ _-]*)(?P<n>\d{1,4})"
    r"\s*[\]})]{1,2}"
)
# Without brackets only the exact form counts, in any case: "EMAIL_1", "email_1".
BARE_RE = re.compile(
    r"(?<![\w\[{(])(?P<kind>[A-Za-z]{2,}(?:_[A-Za-z]+)*)(?P<sep>_)(?P<n>\d{1,4})(?![\w\]})])"
)
# The longest placeholder a model may return, to know how much of a stream to hold back.
_MAX_PLACEHOLDER = max(len(k.value) for k in Kind) + 16


def _normalise(kind: Kind, value: str) -> str:
    if kind is Kind.EMAIL:
        return value.lower()
    if kind in (Kind.PHONE, Kind.CARD, Kind.IBAN):
        return re.sub(r"[^0-9A-Za-z]", "", value).upper()
    return value.strip()


class Masker:
    def __init__(self, kinds: Iterable[Kind] | None = None, *, strict: bool = False) -> None:
        """``strict`` hides every number that passes its check, even with nothing
        tying it to a person; see :func:`privacy_gateway.detect`."""
        self.kinds = frozenset(kinds) if kinds is not None else frozenset(Kind)
        self.strict = strict
        self._placeholder_of: dict[tuple[Kind, str], str] = {}
        self._value_of: dict[str, str] = {}
        self._count: dict[Kind, int] = {}

    def __repr__(self) -> str:
        # Never print the values: this object ends up in logs and tracebacks.
        return f"Masker(kinds={len(self.kinds)}, masked={len(self._value_of)})"

    def __len__(self) -> int:
        return len(self._value_of)

    def mask(self, text: str) -> str:
        """Replace every finding in ``text`` with its placeholder."""
        parts: list[str] = []
        pos = 0
        known = (value for _, value in self._placeholder_of)
        for f in detect(text, self.kinds, strict=self.strict, known=known):
            parts.append(text[pos : f.start])
            parts.append(self._placeholder(f.kind, f.value))
            pos = f.end
        parts.append(text[pos:])
        return "".join(parts)

    def mask_messages(self, messages: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Mask the ``content`` of chat messages (OpenAI-style dicts); copies, never edits."""
        out = []
        for message in messages:
            copy = dict(message)
            if isinstance(copy.get("content"), str):
                copy["content"] = self.mask(copy["content"])
            out.append(copy)
        return out

    def unmask(self, text: str) -> str:
        """Put real values back, also where the model changed a placeholder a little.

        Only this masker's own placeholders are restored; anything else that
        looks like one is left as it is (see :meth:`unresolved`).
        """

        def put_back(m: re.Match) -> str:
            return self._value_of.get(self._known(m) or "", m.group(0))

        return BARE_RE.sub(put_back, LOOSE_RE.sub(put_back, text))

    def unresolved(self, text: str) -> list[str]:
        """Placeholder-like pieces of an unmasked answer that match none of ours.

        A non-empty list means a real value may be missing from the answer:
        the model changed a placeholder beyond recognition or made one up.
        """
        return [
            m.group(0)
            for m in LOOSE_RE.finditer(text)
            if self._known(m) is None
            and (m.group("kind").isupper() or "_" in m.group("sep") + m.group("kind"))
        ]

    def unmask_stream(self, chunks: Iterable[str]) -> Iterator[str]:
        """Unmask a streamed answer whose placeholders may be split across chunks."""
        held = ""
        for chunk in chunks:
            held += chunk
            cut = _safe_cut(held)
            if cut:
                yield self.unmask(held[:cut])
                held = held[cut:]
        if held:
            yield self.unmask(held)

    def conceal(self, text: str, shown: str = "••••••") -> str:
        """Show this masker's placeholders as ``shown`` instead of the real values.

        For values that must not reappear at all, such as a password the model
        repeats in its answer. Unknown placeholders are left as they are.
        """

        def hide(m: re.Match) -> str:
            return shown if self._known(m) else m.group(0)

        return BARE_RE.sub(hide, LOOSE_RE.sub(hide, text))

    def conceal_stream(self, chunks: Iterable[str], shown: str = "••••••") -> Iterator[str]:
        """:meth:`conceal` for a streamed answer."""
        held = ""
        for chunk in chunks:
            held += chunk
            cut = _safe_cut(held)
            if cut:
                yield self.conceal(held[:cut], shown)
                held = held[cut:]
        if held:
            yield self.conceal(held, shown)

    def _known(self, m: re.Match) -> str | None:
        """Which of our placeholders a loose match stands for, if any."""
        bracketed = m.re is LOOSE_RE
        kind = re.sub(r"[ -]", "_", m.group("kind")).upper()
        exact = f"[{kind}_{int(m.group('n'))}]"
        if exact in self._value_of or not bracketed:
            return exact if exact in self._value_of else None
        best, best_distance = None, None
        for placeholder in self._value_of:
            ours = PLACEHOLDER_RE.fullmatch(placeholder)
            if ours is None or int(ours.group(2)) != int(m.group("n")):
                continue
            distance = _distance(kind, ours.group(1))
            if distance <= max(1, len(ours.group(1)) // 4) and (
                best_distance is None or distance < best_distance
            ):
                best, best_distance = placeholder, distance
        return best

    def _placeholder(self, kind: Kind, value: str) -> str:
        key = (kind, _normalise(kind, value))
        placeholder = self._placeholder_of.get(key)
        if placeholder is None:
            n = self._count.get(kind, 0) + 1
            self._count[kind] = n
            placeholder = f"[{kind.value}_{n}]"
            self._placeholder_of[key] = placeholder
            self._value_of[placeholder] = value
        return placeholder


def _distance(a: str, b: str) -> int:
    """Edit distance where swapping two neighbouring letters counts as one edit.

    The strings are short placeholder names, so the plain table is enough.
    """
    d = [[i + j if i * j == 0 else 0 for j in range(len(b) + 1)] for i in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


# An opened placeholder that is not closed yet: "[EMA", "{Person_", "[[EMAIL_1".
_OPEN_RE = re.compile(r"[\[{(]{1,2}\s*(?:[^\W\d_]+(?:[ _-][^\W\d_]*)*)?[ _-]*\d*\s*")
# A bare name that may still become "EMAIL_1": "EMAIL", "EMAIL_", "Email_1".
_BARE_TAIL_RE = re.compile(r"(?:[A-Z][A-Z_]+|[^\W\d_]+_\d*)$")


def _safe_cut(text: str) -> int:
    """How much of ``text`` can be released without splitting a placeholder."""
    window = max(0, len(text) - _MAX_PLACEHOLDER)
    start = max(text.rfind(ch, window) for ch in "[{(")
    if start != -1:
        if start > 0 and text[start - 1] in "[{(":
            start -= 1  # "[[": keep both brackets together
        if _OPEN_RE.fullmatch(text, start):
            return start
        doubled = text[start + 1 : start + 2] in ("[", "{", "(")
        if doubled and text[-1] in "]})" and text[-2:-1] not in ("]", "}", ")"):
            return start  # "[[EMAIL_1]" waits for its second bracket
    tail = _BARE_TAIL_RE.search(text, window)
    if tail and (tail.start() == 0 or not text[tail.start() - 1].isalnum()):
        return tail.start()
    return len(text)
