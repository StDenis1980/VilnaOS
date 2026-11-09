# SPDX-License-Identifier: GPL-3.0-or-later
"""Dictation and reading aloud, without the toolkit (ADR 0015, steps 1 and 2).

The window calls ``press_microphone`` (the button, Ctrl+M), ``speak`` (an
answer has finished) and ``stop_speaking`` (Stop, Esc); this module asks
the window to show things through ``VoiceView``. Rules:

- Nothing is downloaded before the person agrees; the question names each
  model, its licence, size and source, and the folder.
- Dictated text goes into the input field; the person sends it (decision 3),
  unless they turned on "Send dictated text at once".
- Half duplex: pressing the microphone silences Gloxi first, and Gloxi does
  not start speaking while the microphone is open.
- A password hidden in an answer is said as "password hidden" (gloxi.voice.speech).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from gettext import gettext as _
from pathlib import Path
from typing import Protocol

from gloxi.config import config_path
from gloxi.engine import DownloadCancelled, EngineError, download, human_size
from gloxi.i18n import N_
from gloxi.voice import audio, speech, store
from gloxi.voice.language import detect, interface_language
from gloxi.voice.models import Pack, packs_for
from gloxi.voice.stt import IDLE_UNLOAD, Dictation, RecognitionError, Recognizer, SpeechDetector
from gloxi.voice.tts import SpeechError, Synthesizer

Runner = Callable[[Callable[[], None]], None]


def thread_runner(job: Callable[[], None]) -> None:
    threading.Thread(target=job, daemon=True).start()


VOICE_CONSENT = N_(
    "Gloxi can listen to you and read its answers aloud on this computer, without the "
    "internet. For that it needs:\n\n{models}\n\nIn total {size}, kept in {folder}. "
    "The sound of your voice never leaves this computer and is not saved."
)
MODEL_LINE = N_("• {name}: {what}, {size}, licence {licence}, from {source}")
WHAT = {
    "stt": N_("speech recognition"),
    "vad": N_("knows when you stop speaking"),
    "piper": N_("voice"),
    "kokoro": N_("voices"),
}


def consent_text(packs: tuple[Pack, ...], folder: Path) -> str:
    lines = "\n".join(
        _(MODEL_LINE).format(
            name=p.file.name,
            what=_(WHAT[p.kind]),
            size=human_size(p.size),
            licence=p.file.licence,
            source=_(p.file.source),
        )
        for p in packs
    )
    total = human_size(sum(p.size for p in packs))
    return _(VOICE_CONSENT).format(models=lines, size=total, folder=folder)


# -- the person's choices -----------------------------------------------------------


@dataclass(frozen=True)
class VoicePrefs:
    read_aloud: bool = False
    auto_send: bool = False  # decision 3: off; the person presses Send


def prefs_path(env: dict[str, str] | None = None) -> Path:
    """Next to config.toml: ``~/.config/gloxi/voice.json``."""
    return config_path(env).with_name("voice.json")


def load_prefs(path: Path | None = None) -> VoicePrefs:
    path = prefs_path() if path is None else path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return VoicePrefs()
    if not isinstance(data, dict):
        return VoicePrefs()
    prefs = VoicePrefs()
    for key in ("read_aloud", "auto_send"):
        if isinstance(data.get(key), bool):
            prefs = replace(prefs, **{key: data[key]})
    return prefs


def save_prefs(prefs: VoicePrefs, path: Path | None = None) -> None:
    path = prefs_path() if path is None else path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".voice-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(prefs), f, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


# -- the controller ---------------------------------------------------------------


class VoiceView(Protocol):
    """What ``VoiceController`` needs from a window; calls may come from a worker."""

    def ask_voice_download(self, text: str, answer: Callable[[bool], None]) -> None: ...
    def show_voice_progress(self, text: str, fraction: float | None) -> None: ...
    def hide_voice_progress(self) -> None: ...
    def set_listening(self, listening: bool) -> None: ...
    def set_speaking(self, speaking: bool) -> None: ...
    def put_dictation(self, text: str, send: bool) -> None: ...
    def show_error(self, text: str) -> None: ...
    def show_note(self, text: str) -> None: ...


class VoiceController:
    def __init__(
        self,
        view: VoiceView,
        base: Path | None = None,
        *,
        prefs: VoicePrefs | None = None,
        save: Callable[[VoicePrefs], None] = save_prefs,
        lang: str | None = None,
        runner: Runner = thread_runner,
        fetch: Callable[..., Path] = download,
        recognizer: Recognizer | None = None,
        synthesizer: Synthesizer | None = None,
        detector: Callable[[Path], Callable] = SpeechDetector,
        microphone: Callable[..., audio.Microphone] = audio.Microphone,
        player: Callable[..., audio.Player] = audio.Player,
    ) -> None:
        self.view = view
        self.base = store.voice_dir() if base is None else base
        self.prefs = prefs or VoicePrefs()
        self._save = save
        self.lang = lang or interface_language()
        self.runner = runner
        self.fetch = fetch
        self.recognizer = recognizer or Recognizer(self.base)
        self.synthesizer = synthesizer or Synthesizer(self.base)
        self.detector = detector
        self.microphone = microphone
        self.player = player
        self._lock = threading.Lock()
        self._mic: audio.Microphone | None = None
        self._dictation: Dictation | None = None
        self._finishing = False
        self._speaking: audio.Player | None = None
        self._speech_id = 0
        self._downloading = False
        self._cancel = threading.Event()
        self._after_download: Callable[[], None] | None = None

    # -- downloads ---------------------------------------------------------------

    def missing(self) -> tuple[Pack, ...]:
        return store.missing(packs_for(self.lang), self.base)

    def _ensure(self, then: Callable[[], None]) -> bool:
        """True if everything is here; otherwise ask, and run ``then`` after a yes."""
        packs = self.missing()
        if not packs:
            return True
        if self._downloading:
            self.view.show_note(_("The voice models are still downloading."))
            return False
        self._after_download = then
        self.view.ask_voice_download(consent_text(packs, self.base), self.answer)
        return False

    def answer(self, yes: bool) -> None:
        if not yes:
            self._after_download = None
            self.view.show_note(_("Not downloaded. Gloxi will ask when you use voice again."))
            return
        self._downloading = True
        self.runner(self._download)

    def cancel_download(self) -> None:
        self._cancel.set()

    def _download(self) -> None:
        self._cancel.clear()
        packs = self.missing()
        total = max(sum(p.size for p in packs), 1)
        before = 0
        try:
            for pack in packs:

                def progress(done: int, _size: int, before=before) -> None:
                    fraction = (before + done) / total
                    text = _("Downloading the voice models: {percent}%").format(
                        percent=int(fraction * 100)
                    )
                    self.view.show_voice_progress(text, fraction)

                store.install(
                    pack,
                    self.base,
                    progress=progress,
                    cancelled=self._cancel.is_set,
                    fetch=self._fetch_then_unpacking,
                )
                before += pack.size
        except DownloadCancelled:
            self.view.show_note(_("The download was cancelled."))
            return
        except EngineError as exc:
            self.view.show_error(_("The voice models were not downloaded: {why}").format(why=exc))
            return
        finally:
            self._downloading = False
            self.view.hide_voice_progress()
        then, self._after_download = self._after_download, None
        self.view.show_note(_("Voice is ready."))
        if then is not None:
            then()

    def _fetch_then_unpacking(self, *args, **kwargs) -> Path:
        path = self.fetch(*args, **kwargs)
        self.view.show_voice_progress(_("Unpacking the voice models…"), None)
        return path

    # -- dictation -------------------------------------------------------------

    @property
    def listening(self) -> bool:
        return self._mic is not None

    def press_microphone(self) -> None:
        """The microphone button: start listening, or end the phrase now."""
        if self.listening:
            self.runner(self._finish)
            return
        self.stop_speaking()  # half duplex
        if self._ensure(self.press_microphone):
            self._listen()

    def _listen(self) -> None:
        try:
            dictation = Dictation(self.detector(self.base))
        except (RecognitionError, ImportError, RuntimeError) as exc:
            self.view.show_error(_("Voice could not be started: {why}").format(why=exc))
            return
        mic = self.microphone(self._on_samples, on_error=self._on_mic_error)
        with self._lock:
            self._dictation, self._mic, self._finishing = dictation, mic, False
        try:
            mic.start()
        except audio.AudioError as exc:
            with self._lock:
                self._dictation, self._mic = None, None
            dictation.wipe()
            self.view.show_error(_("The microphone could not be opened: {why}").format(why=exc))
            return
        self.view.set_listening(True)
        # Load the model while the person speaks, so the text comes sooner.
        self.runner(self._preload)

    def _preload(self) -> None:
        with contextlib.suppress(RecognitionError):  # reported by _finish
            self.recognizer.load()

    def _on_samples(self, samples) -> None:
        with self._lock:
            dictation = self._dictation
        if dictation is not None and dictation.feed(samples):
            self.runner(self._finish)

    def _on_mic_error(self, why: str) -> None:
        self._stop_listening()
        self.view.show_error(_("The microphone stopped: {why}").format(why=why))

    def _stop_listening(self) -> Dictation | None:
        with self._lock:
            mic, dictation = self._mic, self._dictation
            self._mic = self._dictation = None
        if mic is not None:
            mic.stop()
            self.view.set_listening(False)
        return dictation

    def _finish(self) -> None:
        with self._lock:
            if self._finishing:
                return
            self._finishing = True
        dictation = self._stop_listening()
        if dictation is None:
            return
        self.view.show_voice_progress(_("Recognising…"), None)
        try:
            text = dictation.finish(self.recognizer)
        except (RecognitionError, RuntimeError) as exc:
            self.view.show_error(_("The speech was not recognised: {why}").format(why=exc))
            return
        finally:
            self.view.hide_voice_progress()
        if text:
            self.view.put_dictation(text, self.prefs.auto_send)
        else:
            self.view.show_note(_("Nothing was heard. Press the microphone and speak."))

    def cancel_listening(self) -> None:
        """Esc while listening: the sound is dropped, nothing is recognised."""
        dictation = self._stop_listening()
        if dictation is not None:
            dictation.wipe()

    def unload_if_idle(self) -> None:
        """Called every minute: the speech model goes after five idle minutes."""
        if not self.listening and self.recognizer.idle_for() > IDLE_UNLOAD:
            self.recognizer.unload()

    # -- reading aloud ------------------------------------------------------------

    def set_read_aloud(self, on: bool) -> None:
        self.prefs = replace(self.prefs, read_aloud=on)
        self._save(self.prefs)
        if on:
            self._ensure(lambda: None)
        else:
            self.stop_speaking()

    def set_auto_send(self, on: bool) -> None:
        self.prefs = replace(self.prefs, auto_send=on)
        self._save(self.prefs)

    @property
    def speaking(self) -> bool:
        return self._speaking is not None

    def speak(self, text: str) -> None:
        """Read a finished answer aloud, if the person asked for that."""
        if not self.prefs.read_aloud or self.listening or not speech.sentences(text):
            return
        if self.missing():
            return  # asked when read aloud was turned on; not again for every answer
        self.stop_speaking()
        with self._lock:
            self._speech_id += 1
            ident = self._speech_id
            player = self.player(
                on_done=lambda: self._spoken(ident), on_error=lambda why: self._spoken(ident)
            )
            self._speaking = player
        self.view.set_speaking(True)
        self.runner(lambda: self._read(ident, player, text))

    def _current(self, ident: int) -> bool:
        with self._lock:
            return self._speech_id == ident and self._speaking is not None

    def _read(self, ident: int, player: audio.Player, text: str) -> None:
        lang = detect(text, self.lang)
        try:
            for sentence in speech.sentences(text):
                if not self._current(ident):
                    return
                sound = self.synthesizer.synthesize(sentence, lang)
                if not self._current(ident):
                    return
                if len(sound.samples):
                    player.push(sound.samples, sound.rate)
        except (SpeechError, audio.AudioError, RuntimeError) as exc:
            self._spoken(ident)
            player.stop()
            self.view.show_error(_("The answer could not be read aloud: {why}").format(why=exc))
            return
        if self._current(ident):
            player.finish()

    def _spoken(self, ident: int) -> None:
        with self._lock:
            if self._speech_id != ident or self._speaking is None:
                return
            self._speaking = None
        self.view.set_speaking(False)

    def stop_speaking(self) -> None:
        with self._lock:
            player, self._speaking = self._speaking, None
            self._speech_id += 1
        if player is not None:
            player.stop()
            self.view.set_speaking(False)

    def stop(self) -> None:
        """Gloxi is closing."""
        self._cancel.set()
        self.cancel_listening()
        self.stop_speaking()
        self.recognizer.unload()
        self.synthesizer.unload()
