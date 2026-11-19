# SPDX-License-Identifier: GPL-3.0-or-later
"""Text into the symbols a voice is trained on.

- The Ukrainian Piper voice reads letters: lower case, with й, ї split into a
  letter and its mark (Unicode NFD), as it was trained.
- Kokoro and the other Piper voices read the phonemes of eSpeak NG
  (``espeak-ng --ipa``), as the phonemizer they were trained with does.

The sherpa-onnx wheel has no phonemizer of its own, so Gloxi runs the
``espeak-ng`` program that ships in the package (GPL-3.0-or-later, like
Gloxi). The text goes in on standard input, never on the command line.
"""

from __future__ import annotations

import shutil
import subprocess
import unicodedata
from collections.abc import Iterable


class PhonemeError(Exception):
    """eSpeak NG is missing or failed; the message is for logs."""


def espeak_binary() -> str | None:
    return shutil.which("espeak-ng")


def espeak_ipa(
    text: str,
    voice: str,
    *,
    binary: str | None = None,
    timeout: float = 10.0,
    secondary_stress: bool = False,
) -> str:
    """The IPA of ``text`` by eSpeak NG, one line per clause joined by spaces."""
    binary = binary or espeak_binary()
    if binary is None:
        raise PhonemeError("espeak-ng is not installed")
    try:
        done = subprocess.run(
            [binary, "-q", "--ipa", "-v", voice],
            input=text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PhonemeError(f"espeak-ng failed: {exc}") from exc
    if done.returncode != 0:
        raise PhonemeError(f"espeak-ng failed: exit {done.returncode}")
    # The joining marks are in no voice's symbols; secondary stress is in
    # Piper's but not in Kokoro's.
    ipa = " ".join(line.strip() for line in done.stdout.splitlines() if line.strip())
    ipa = ipa.replace("_", "").replace("\u200d", "")
    return ipa if secondary_stress else ipa.replace("ˌ", "")


def letters(text: str) -> str:
    """What a voice trained on letters (``phoneme_type: text``) reads."""
    return unicodedata.normalize("NFD", text.lower())


def codepoints(symbols: str, known: Iterable[str]) -> list[int]:
    """The symbols the voice knows, as code points; the others are dropped."""
    allowed = set(known)
    return [ord(c) for c in symbols if c in allowed]


def read_tokens(path) -> set[str]:
    """The symbols of a sherpa-onnx ``tokens.txt`` ("<symbol> <id>" per line)."""
    known: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            symbol, _sep, ident = line.rpartition(" ")
            if ident.isdigit():
                known.add(symbol if symbol else " ")
    return known
