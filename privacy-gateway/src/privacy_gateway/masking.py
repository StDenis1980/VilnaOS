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
# The longest placeholder we produce, to know how much of a stream to hold back.
_MAX_PLACEHOLDER = max(len(k.value) for k in Kind) + 8


def _normalise(kind: Kind, value: str) -> str:
    if kind is Kind.EMAIL:
        return value.lower()
    if kind in (Kind.PHONE, Kind.CARD, Kind.IBAN):
        return re.sub(r"[^0-9A-Za-z]", "", value).upper()
    return value.strip()


class Masker:
    def __init__(self, kinds: Iterable[Kind] | None = None) -> None:
        self.kinds = frozenset(kinds) if kinds is not None else frozenset(Kind)
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
        for f in detect(text, self.kinds):
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
        """Put real values back. Unknown placeholders are left as they are."""
        return PLACEHOLDER_RE.sub(lambda m: self._value_of.get(m.group(0), m.group(0)), text)

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
        return PLACEHOLDER_RE.sub(
            lambda m: shown if m.group(0) in self._value_of else m.group(0), text
        )

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


def _safe_cut(text: str) -> int:
    """How much of ``text`` can be released without splitting a placeholder."""
    start = text.rfind("[", max(0, len(text) - _MAX_PLACEHOLDER))
    if start == -1 or "]" in text[start:]:
        return len(text)
    tail = text[start + 1 :]
    if re.fullmatch(r"[A-Z_]*\d*", tail):
        return start  # may still become a placeholder: hold it back
    return len(text)
