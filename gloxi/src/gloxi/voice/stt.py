# SPDX-License-Identifier: GPL-3.0-or-later
"""Speech to text on this computer (ADR 0015, section 1).

"Press and speak": a ``Dictation`` collects the sound of one phrase, in
memory only, until the person stops speaking (the Silero VAD), presses again
or reaches a minute; then Parakeet turns it into text with punctuation. The
sound is overwritten with zeros as soon as the text is ready.

The model takes about 1 GB of memory, so it is loaded at the first press and
unloaded after a few minutes without one (``Recognizer.unload``).
"""

from __future__ import annotations

import threading
import time
from array import array
from collections.abc import Callable, Sequence
from pathlib import Path

from gloxi.voice.models import PARAKEET, SILERO_VAD
from gloxi.voice.store import installed
from gloxi.voice.tts import threads

RATE = 16_000  # what Parakeet and the VAD expect
MAX_SECONDS = 60
# Stop this long after the last speech, or this long if nobody speaks at all.
SILENCE_AFTER_SPEECH = 1.2
NOTHING_SAID = 8.0
# Unload the model after this much time without dictation.
IDLE_UNLOAD = 300.0


class RecognitionError(Exception):
    """The model is missing or failed; the message is for logs."""


class Recognizer:
    """Parakeet TDT 0.6B v3 through sherpa-onnx, loaded on first use."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self._lock = threading.Lock()
        self._engine = None
        self.last_used = 0.0

    def ready(self) -> bool:
        return installed(PARAKEET, self.base) is not None

    def load(self):
        with self._lock:
            if self._engine is None:
                self._engine = self._make()
            self.last_used = time.monotonic()
            return self._engine

    def _make(self):
        folder = installed(PARAKEET, self.base)
        if folder is None:
            raise RecognitionError("the speech model is not installed")
        try:
            import sherpa_onnx as so
        except ImportError as exc:
            raise RecognitionError("sherpa-onnx is not installed") from exc
        try:
            return so.OfflineRecognizer.from_transducer(
                encoder=str(folder / "encoder.int8.onnx"),
                decoder=str(folder / "decoder.int8.onnx"),
                joiner=str(folder / "joiner.int8.onnx"),
                tokens=str(folder / "tokens.txt"),
                model_type="nemo_transducer",
                num_threads=threads(),
            )
        except (RuntimeError, ValueError) as exc:
            raise RecognitionError(f"the speech model could not be loaded: {exc}") from exc

    def transcribe(self, samples: Sequence[float], rate: int = RATE) -> str:
        if not samples:
            return ""
        engine = self.load()
        stream = engine.create_stream()
        stream.accept_waveform(rate, samples)
        engine.decode_stream(stream)
        self.last_used = time.monotonic()
        return stream.result.text.strip()

    def idle_for(self) -> float:
        return time.monotonic() - self.last_used if self._engine is not None else 0.0

    def unload(self) -> None:
        with self._lock:
            self._engine = None


class SpeechDetector:
    """Whether a piece of sound holds speech: the Silero VAD via sherpa-onnx."""

    WINDOW = 512  # samples at 16 kHz, what Silero takes

    def __init__(self, base: Path) -> None:
        folder = installed(SILERO_VAD, base)
        if folder is None:
            raise RecognitionError("the speech detector is not installed")
        import sherpa_onnx as so

        config = so.VadModelConfig()
        config.silero_vad.model = str(folder / SILERO_VAD.file.filename)
        config.silero_vad.min_silence_duration = 0.3
        config.sample_rate = RATE
        config.num_threads = 1
        self._vad = so.VoiceActivityDetector(config, buffer_size_in_seconds=MAX_SECONDS + 5)

    def __call__(self, samples: Sequence[float]) -> bool:
        self._vad.accept_waveform(samples)
        speech = self._vad.is_speech_detected()
        while not self._vad.empty():  # only the yes/no is needed, not the segments
            self._vad.pop()
        return speech


class Dictation:
    """The sound of one press of the microphone, until the phrase ends."""

    def __init__(
        self,
        detector: Callable[[Sequence[float]], bool],
        *,
        rate: int = RATE,
        max_seconds: float = MAX_SECONDS,
        silence_after_speech: float = SILENCE_AFTER_SPEECH,
        nothing_said: float = NOTHING_SAID,
    ) -> None:
        self.detector = detector
        self.rate = rate
        self.max_samples = int(max_seconds * rate)
        self.silence_after = int(silence_after_speech * rate)
        self.nothing_said = int(nothing_said * rate)
        self.sound = array("f")
        self.heard_speech = False
        self._since_speech = 0
        self.done = False

    def feed(self, samples: Sequence[float]) -> bool:
        """Add sound; True once the phrase is over (then stop the microphone)."""
        if self.done:
            return True
        room = self.max_samples - len(self.sound)
        piece = array("f", samples[:room])
        self.sound.extend(piece)
        if self.detector(piece):
            self.heard_speech = True
            self._since_speech = 0
        else:
            self._since_speech += len(piece)
        full = len(self.sound) >= self.max_samples
        ended = self.heard_speech and self._since_speech >= self.silence_after
        silent = not self.heard_speech and len(self.sound) >= self.nothing_said
        self.done = full or ended or silent
        return self.done

    def finish(self, recognizer: Recognizer) -> str:
        """The text of the phrase ("" if nothing was said); the sound is wiped."""
        self.done = True
        try:
            if not self.heard_speech:
                return ""
            return recognizer.transcribe(self.sound, self.rate)
        finally:
            self.wipe()

    def wipe(self) -> None:
        with memoryview(self.sound).cast("B") as raw:
            raw[:] = bytes(len(raw))
        del self.sound[:]
