# SPDX-License-Identifier: GPL-3.0-or-later
"""Text to speech on this computer (ADR 0015, section 2).

One voice model is kept in memory at a time: the one of the language being
read. Languages without a downloaded voice are read by eSpeak NG, which
needs no model. Nothing here touches the network or writes sound to disk.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import threading
from array import array
from dataclasses import dataclass
from pathlib import Path

from gloxi.voice import phonemes, speech
from gloxi.voice.models import Voice, voice_for
from gloxi.voice.store import installed


class SpeechError(Exception):
    """A sentence could not be turned into sound; the message is for logs."""


@dataclass
class Audio:
    samples: array  # float32, -1..1, mono
    rate: int

    @property
    def seconds(self) -> float:
        return len(self.samples) / self.rate if self.rate else 0.0


def threads() -> int:
    return max(1, min(4, os.cpu_count() or 1))


class Synthesizer:
    """Turns one sentence at a time into sound, with the voice of its language."""

    def __init__(self, base: Path, *, espeak: str | None = None) -> None:
        self.base = base
        self.espeak = espeak
        self._lock = threading.Lock()
        self._loaded: tuple[str, object, set[str]] | None = None  # pack key, engine, symbols

    def voice(self, lang: str) -> Voice:
        """The voice that will read ``lang`` now: eSpeak NG if its model is missing."""
        voice = voice_for(lang)
        if voice.pack is not None and installed(voice.pack, self.base) is None:
            return Voice(voice.lang, None, espeak=voice.espeak or voice.lang, name="eSpeak NG")
        return voice

    def synthesize(self, sentence: str, lang: str) -> Audio:
        voice = self.voice(lang)
        if voice.lang == "uk":
            sentence = speech.uk_numbers(sentence)
        if voice.pack is None:
            return self._espeak(sentence, voice.espeak or voice.lang)
        with self._lock:
            engine, symbols = self._engine(voice)
            if voice.pack.kind == "piper":
                text = phonemes.letters(sentence)
            else:
                text = phonemes.espeak_ipa(sentence, voice.espeak, binary=self.espeak)
            points = phonemes.codepoints(text, symbols)
            if not points:
                return Audio(array("f"), 22050)
            import sherpa_onnx as so

            config = so.GenerationConfig()
            config.sid = voice.speaker
            config.speed = 1.0
            config.phoneme_codepoints = [points]
            result = engine.generate(sentence, config)
        if result.sample_rate <= 0:
            raise SpeechError("the voice produced no sound")
        return Audio(array("f", result.samples), result.sample_rate)

    def _engine(self, voice: Voice):
        pack = voice.pack
        if self._loaded is not None and self._loaded[0] == pack.key:
            return self._loaded[1], self._loaded[2]
        self._loaded = None  # the previous voice goes first: one in memory
        folder = installed(pack, self.base)
        if folder is None:
            raise SpeechError(f"{pack.key} is not installed")
        try:
            import sherpa_onnx as so
        except ImportError as exc:
            raise SpeechError("sherpa-onnx is not installed") from exc
        tokens = folder / "tokens.txt"
        if pack.kind == "piper":
            onnx = next(iter(sorted(folder.glob("*.onnx"))), None)
            if onnx is None:
                raise SpeechError(f"{pack.key}: no model file")
            model = so.OfflineTtsModelConfig(
                vits=so.OfflineTtsVitsModelConfig(model=str(onnx), tokens=str(tokens)),
                num_threads=threads(),
            )
        else:
            model = so.OfflineTtsModelConfig(
                kokoro=so.OfflineTtsKokoroModelConfig(
                    model=str(folder / "model.int8.onnx"),
                    voices=str(folder / "voices.bin"),
                    tokens=str(tokens),
                ),
                num_threads=threads(),
            )
        config = so.OfflineTtsConfig(model=model)
        if not config.validate():
            raise SpeechError(f"{pack.key}: the model files are not complete")
        engine = so.OfflineTts(config)
        self._loaded = (pack.key, engine, phonemes.read_tokens(tokens))
        return engine, self._loaded[2]

    def _espeak(self, sentence: str, voice: str) -> Audio:
        binary = self.espeak or phonemes.espeak_binary()
        if binary is None:
            raise SpeechError("espeak-ng is not installed")
        try:
            done = subprocess.run(
                [binary, "--stdout", "-v", voice],
                input=sentence.encode("utf-8"),
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SpeechError(f"espeak-ng failed: {exc}") from exc
        if done.returncode != 0 or not done.stdout:
            raise SpeechError(f"espeak-ng failed: exit {done.returncode}")
        return wav_to_audio(done.stdout)

    def unload(self) -> None:
        with self._lock:
            self._loaded = None


def wav_to_audio(data: bytes) -> Audio:
    """16-bit mono PCM WAV to float samples.

    eSpeak NG writes to a pipe, so its header gives a placeholder length:
    the chunks are walked by hand and the data runs to the end.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise SpeechError("not a WAV file")
    pos, rate, fmt_ok = 12, 0, False
    while pos + 8 <= len(data):
        chunk, size = data[pos : pos + 4], int.from_bytes(data[pos + 4 : pos + 8], "little")
        body = pos + 8
        if chunk == b"fmt " and size >= 16:
            fmt, channels, rate = struct.unpack_from("<HHI", data, body)
            bits = struct.unpack_from("<H", data, body + 14)[0]
            fmt_ok = fmt == 1 and channels == 1 and bits == 16
        elif chunk == b"data":
            if not fmt_ok or rate <= 0:
                raise SpeechError("unexpected WAV format")
            pcm = data[body : min(len(data), body + size)]
            samples = array("h")
            samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
            if sys.byteorder == "big":
                samples.byteswap()
            return Audio(array("f", (x / 32768.0 for x in samples)), rate)
        pos = body + size + (size & 1)
    raise SpeechError("no sound in the WAV file")
