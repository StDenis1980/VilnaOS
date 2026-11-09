# SPDX-License-Identifier: GPL-3.0-or-later
"""``gloxi-bench --tasks voice``: the voice on this computer (ADR 0015, section 7).

For each of the ten languages Gloxi reads a sentence aloud with its voice
(or eSpeak NG), then recognises that sound with Parakeet, and reports:

- the word error rate (WER) of the recognised text against the sentence;
- how long the first sentence took to make (time to first sound) and the
  real-time factor of making and of recognising;
- the peak memory of the process.

Synthetic speech is easier than a person in a room, so the WER here is a
floor; the release threshold (≤ 15 % per main language) is measured on
FLEURS and Common Voice. No recordings of people are used or kept.
"""

from __future__ import annotations

import json
import re
import resource
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from gloxi.voice import speech
from gloxi.voice.stt import RecognitionError, Recognizer
from gloxi.voice.tts import SpeechError, Synthesizer

# Everyday requests to an assistant, one per language, with a time and a number.
PHRASES = {
    "uk": (
        "Нагадай мені завтра о дев'ятій ранку зателефонувати лікарю. "
        "Скільки коштує квиток до Львова?"
    ),
    "en": (
        "Remind me tomorrow at nine in the morning to call the doctor. "
        "How much is a ticket to London?"
    ),
    "es": (
        "Recuérdame mañana a las nueve de la mañana llamar al médico. "
        "¿Cuánto cuesta un billete a Madrid?"
    ),
    "pl": (
        "Przypomnij mi jutro o dziewiątej rano, żeby zadzwonić do lekarza. "
        "Ile kosztuje bilet do Krakowa?"
    ),
    "de": (
        "Erinnere mich morgen um neun Uhr früh daran, den Arzt anzurufen. "
        "Was kostet eine Fahrkarte nach Berlin?"
    ),
    "fr": (
        "Rappelle-moi demain à neuf heures du matin d'appeler le médecin. "
        "Combien coûte un billet pour Paris ?"
    ),
    "it": (
        "Ricordami domani alle nove di mattina di chiamare il medico. "
        "Quanto costa un biglietto per Roma?"
    ),
    "ro": (
        "Amintește-mi mâine la nouă dimineața să sun la doctor. "
        "Cât costă un bilet până la București?"
    ),
    "cs": ("Připomeň mi zítra v devět ráno, abych zavolal lékaři. Kolik stojí jízdenka do Brna?"),
    "lt": (
        "Primink man rytoj devintą valandą ryto paskambinti gydytojui. "
        "Kiek kainuoja bilietas į Vilnių?"
    ),
}


@dataclass
class VoiceResult:
    lang: str
    voice: str
    wer: float
    first_sound_s: float
    tts_rtf: float
    stt_rtf: float
    heard: str


def words(text: str) -> list[str]:
    text = text.lower().replace("’", "'").replace("ʼ", "'")
    return re.findall(r"[\w']+", text)


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate: word edits (Levenshtein) over the reference length."""
    ref, hyp = words(reference), words(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1] / len(ref)


def run(base: Path, langs: list[str] | None = None) -> list[VoiceResult]:
    tts = Synthesizer(base)
    stt = Recognizer(base)
    stt.load()
    results = []
    for lang in langs or list(PHRASES):
        text = PHRASES[lang]
        voice = tts.voice(lang)
        parts = speech.sentences(text)
        start = time.monotonic()
        audio = [tts.synthesize(parts[0], lang)]
        first = time.monotonic() - start
        audio += [tts.synthesize(p, lang) for p in parts[1:]]
        made = time.monotonic() - start
        rate = audio[0].rate
        samples = [x for a in audio for x in a.samples]
        seconds = len(samples) / rate if rate else 0.0
        start = time.monotonic()
        heard = stt.transcribe(samples, rate)
        recognised = time.monotonic() - start
        results.append(
            VoiceResult(
                lang=lang,
                voice=f"{voice.pack.key if voice.pack else 'espeak-ng'}:{voice.name}",
                wer=round(wer(text, heard), 3),
                first_sound_s=round(first, 2),
                tts_rtf=round(made / seconds, 2) if seconds else 0.0,
                stt_rtf=round(recognised / seconds, 3) if seconds else 0.0,
                heard=heard,
            )
        )
        tts.unload()
    return results


def peak_memory_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def main(base: Path, langs: list[str] | None, as_json: bool) -> int:
    unknown = sorted(set(langs or ()) - set(PHRASES))
    if unknown:
        print(f"gloxi-bench: no voice phrase for {', '.join(unknown)}", file=sys.stderr)
        return 2
    if not Recognizer(base).ready():
        print("gloxi-bench: no voice models; run: gloxi voice download", file=sys.stderr)
        return 1
    try:
        results = run(base, langs)
    except (RecognitionError, SpeechError, ImportError) as exc:
        print(f"gloxi-bench: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2))
    else:
        print("lang  WER    first   TTS RTF  STT RTF  voice")
        for r in results:
            print(
                f"{r.lang:<5} {r.wer * 100:5.1f}%  {r.first_sound_s:5.2f}s  {r.tts_rtf:7.2f}  "
                f"{r.stt_rtf:7.3f}  {r.voice}"
            )
            print(f"      heard: {r.heard}")
    print(f"peak memory: {peak_memory_mb():.0f} MB", file=sys.stderr)
    return 0
