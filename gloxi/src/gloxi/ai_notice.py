# SPDX-License-Identifier: GPL-3.0-or-later
"""Gloxi says it is an AI and marks what its model writes (ADR 0033).

The EU AI Act, Art. 50(1): a system that talks with people tells them they
are talking with an AI, "at the latest at the time of the first interaction"
(Art. 50(5)). Art. 50(2): text and audio the system generates are "marked in
a machine-readable format and detectable as artificially generated". Both
apply from 2 August 2026, to free software too (Art. 2(12) does not exempt
Art. 50), so to Gloxi 0.1.

Gloxi's first message in every conversation, typed or read aloud, starts
with ``notice()``. Answers written by the model carry ``mark()``: in the
service protocol, in the history a window gets back, and as the accessible
description of the answer in the window (AT-SPI).
"""

from __future__ import annotations

from gettext import gettext as _

from gloxi import __version__
from gloxi.i18n import N_

NOTICE = N_(
    "I am Gloxi, an AI assistant: a language model writes my answers, and they can be wrong."
)
# What a screen reader, or another program through AT-SPI, reads for an answer.
WRITTEN_BY_AI = N_("Written by AI")

# The IPTC "digital source type" for content made by a trained model: the
# vocabulary C2PA and image metadata use for the same statement.
DIGITAL_SOURCE_TYPE = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"


def notice() -> str:
    """The sentence Gloxi starts a conversation with, in the interface language."""
    return _(NOTICE)


def mark(target: str | None = None) -> dict[str, object]:
    """The machine-readable mark of an answer the model wrote.

    ``target`` is where the model ran ("local" or "cloud"), when known.
    """
    out: dict[str, object] = {
        "ai_generated": True,
        "digital_source_type": DIGITAL_SOURCE_TYPE,
        "generator": f"Gloxi {__version__}",
    }
    if target:
        out["model_location"] = target
    return out


def with_notice(text: str) -> str:
    """``text`` as the first message of a conversation: the notice first."""
    return f"{notice()}\n\n{text}" if text else notice()
