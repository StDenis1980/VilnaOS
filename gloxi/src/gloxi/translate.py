# SPDX-License-Identifier: GPL-3.0-or-later
"""Short public texts translated by the local model, for other parts of Vilna.

The first user is the weather card of the Vilna Shell extension: MeteoAlarm
warnings come in the national service's languages, and Gloxi puts them into
the person's language (decision of Denys, 18.11.2026). The extension asks
gloxid (``text.translate``, docs/service-protocol.md) only when the text is
not already in that language.

Only the model on this computer translates: never the cloud and never the
router, whatever the person chose for the conversation. The request has no
system prompt of Gloxi, no functions and no history, so it is short, and it
does not take the conversation's slot.
"""

from __future__ import annotations

import dataclasses
import re

from gloxi.llm import ChatClient, ModelError

# The ten languages of Vilna (ADR 0009), by their English names for the model.
LANGUAGES = {
    "uk": "Ukrainian",
    "en": "English",
    "es": "Spanish",
    "pl": "Polish",
    "de": "German",
    "fr": "French",
    "it": "Italian",
    "ro": "Romanian",
    "cs": "Czech",
    "lt": "Lithuanian",
}

# A warning's headline is a line or two; a whole description is a paragraph.
MAX_TEXT = 4000

_THINK = re.compile(r"<think>.*?</think>", re.S)


def translator_client(client: ChatClient) -> ChatClient:
    """A client of the same local server that leaves the conversation's slot alone."""
    return ChatClient(dataclasses.replace(client.endpoint, slot=None))


def translate(client: ChatClient, text: str, language: str) -> str:
    """``text`` in ``language`` (a code from LANGUAGES); raises ModelError."""
    name = LANGUAGES.get(language)
    if name is None:
        raise ValueError(f"language must be one of {', '.join(LANGUAGES)}")
    text = text.strip()
    if not text:
        return ""
    if len(text) > MAX_TEXT:
        raise ValueError(f"text is longer than {MAX_TEXT} characters")
    messages = [
        {
            "role": "system",
            "content": (
                f"Translate the user's text into {name}. It is an official weather "
                "warning. Reply with the translation only: no notes, no quotes, no "
                "explanations. Keep numbers, units, times, dates and place names."
            ),
        },
        {"role": "user", "content": text},
    ]
    out = _THINK.sub("", client.complete(messages)).strip()
    if not out:
        raise ModelError("the model gave no translation")
    # A runaway answer is not a translation of a short text.
    return out[: MAX_TEXT * 2]
