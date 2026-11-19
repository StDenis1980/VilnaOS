# SPDX-License-Identifier: GPL-3.0-or-later
"""``gloxi voice``: the voice from a terminal, also for checking a computer.

gloxi voice status            what is downloaded, and which voice reads each language
gloxi voice download          ask, then download what the interface language needs
gloxi voice say [--lang] TEXT read TEXT aloud
gloxi voice listen            one phrase from the microphone, printed as text
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from gettext import gettext as _
from typing import TextIO

from gloxi.engine import DownloadCancelled, EngineError, human_size
from gloxi.voice import audio, speech, store
from gloxi.voice.language import detect, interface_language
from gloxi.voice.models import PACKS, VOICES, packs_for
from gloxi.voice.session import consent_text
from gloxi.voice.stt import Dictation, RecognitionError, Recognizer, SpeechDetector
from gloxi.voice.tts import SpeechError, Synthesizer


def status(out: TextIO, base=None) -> int:
    base = store.voice_dir() if base is None else base
    for pack in PACKS:
        mark = "✓" if store.installed(pack, base) else "–"
        out.write(f"{mark} {pack.file.name} ({human_size(pack.size)}, {pack.file.licence})\n")
    synth = Synthesizer(base)
    for lang in VOICES:
        voice = synth.voice(lang)
        engine = voice.pack.file.name if voice.pack else "eSpeak NG"
        out.write(f"  {lang}: {engine} — {voice.name}\n")
    out.write(f"{base}\n")
    return 0


def download_packs(
    lang: str, read: Callable[[str], str], out: TextIO, base=None, fetch=None
) -> int:
    from gloxi.cli import YES

    base = store.voice_dir() if base is None else base
    packs = store.missing(packs_for(lang), base)
    if not packs:
        out.write(_("Voice is ready.") + "\n")
        return 0
    out.write(consent_text(packs, base) + "\n")
    answer = read(_("Download it now? [y/N] ")).strip().casefold()
    if answer not in YES | {_("yes").casefold()}:
        out.write(_("Not downloaded. Gloxi will ask when you use voice again.") + "\n")
        return 1
    kwargs = {"fetch": fetch} if fetch else {}
    try:
        for pack in packs:

            def progress(done: int, total: int, name=pack.file.name) -> None:
                out.write(f"\r{name}: {int(done * 100 / max(total, 1))}%")
                out.flush()

            store.install(pack, base, progress=progress, **kwargs)
            out.write("\n")
    except (DownloadCancelled, KeyboardInterrupt):
        out.write("\n" + _("The download was cancelled.") + "\n")
        return 1
    except EngineError as exc:
        out.write("\n" + _("The voice models were not downloaded: {why}").format(why=exc) + "\n")
        return 1
    out.write(_("Voice is ready.") + "\n")
    return 0


def say(text: str, lang: str | None, base=None) -> int:
    base = store.voice_dir() if base is None else base
    synth = Synthesizer(base)
    lang = lang or detect(text, interface_language())
    done = threading.Event()
    player = audio.Player(on_done=done.set, on_error=lambda why: done.set())
    try:
        for sentence in speech.sentences(text):
            sound = synth.synthesize(sentence, lang)
            if len(sound.samples):
                player.push(sound.samples, sound.rate)
        player.finish()
        _run_until(done, 120)
    except (SpeechError, audio.AudioError) as exc:
        print(f"gloxi: {exc}", file=sys.stderr)
        return 1
    finally:
        player.stop()
    return 0


def listen(out: TextIO, base=None) -> int:
    base = store.voice_dir() if base is None else base
    recognizer = Recognizer(base)
    if not recognizer.ready():
        print("gloxi: no voice models; run: gloxi voice download", file=sys.stderr)
        return 1
    try:
        dictation = Dictation(SpeechDetector(base))
        recognizer.load()
    except (RecognitionError, ImportError) as exc:
        print(f"gloxi: {exc}", file=sys.stderr)
        return 1
    done = threading.Event()

    def on_samples(samples) -> None:
        if dictation.feed(samples):
            done.set()

    mic = audio.Microphone(on_samples, on_error=lambda why: done.set())
    try:
        mic.start()
        print(_("Listening… speak now."), file=sys.stderr)
        _run_until(done, 75)
    except audio.AudioError as exc:
        print(f"gloxi: {exc}", file=sys.stderr)
        return 1
    finally:
        mic.stop()
    try:
        text = dictation.finish(recognizer)
    except RecognitionError as exc:
        print(f"gloxi: {exc}", file=sys.stderr)
        return 1
    out.write(text + "\n")
    return 0 if text else 1


def _run_until(event: threading.Event, timeout: float) -> None:
    """Wait, running GLib's loop so that GStreamer's messages arrive."""
    try:
        from gi.repository import GLib
    except ImportError:
        event.wait(timeout)
        return
    context = GLib.MainContext.default()
    deadline = GLib.get_monotonic_time() + int(timeout * 1e6)
    while not event.is_set() and GLib.get_monotonic_time() < deadline:
        context.iteration(False)
        event.wait(0.02)


def main(action: str, words: list[str], lang: str | None) -> int:
    if action == "status":
        return status(sys.stdout)
    if action == "download":
        return download_packs(lang or interface_language(), input, sys.stdout)
    if action == "say":
        text = " ".join(words) or sys.stdin.read()
        return say(text, lang)
    return listen(sys.stdout)
