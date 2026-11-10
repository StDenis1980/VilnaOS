# SPDX-License-Identifier: GPL-3.0-or-later
"""What Gloxi downloads for its voice (ADR 0015, sections 1, 2 and 4).

Every file is pinned by size and SHA-256, as the language model is
(``gloxi.engine``). They come from the releases of sherpa-onnx on GitHub,
which repackages the original models for the engine Gloxi uses; the
licence is that of the original model, named here and shown to the person.
``tools/check_voice.py`` checks these values against GitHub.

Languages without a voice here are read by eSpeak NG (``VOICES``): it is
part of the package, needs no download, and sounds mechanical but clear.
"""

from __future__ import annotations

from dataclasses import dataclass

from gloxi.engine import ModelFile
from gloxi.i18n import N_

RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


@dataclass(frozen=True)
class Pack:
    """One download: an archive unpacked into ``folder``, or a single file."""

    key: str
    kind: str  # "stt", "vad", "piper" or "kokoro"
    file: ModelFile
    folder: str  # the folder inside the archive; for a single file, where it goes
    author: str  # for the "About" window and share/licenses (CC-BY needs it)
    # For a voice: what it was trained from, "scratch" or the base model. ADR
    # 0015 section 2.1: a voice fine-tuned from en_US-lessac or -ryan inherits
    # their non-commercial terms, so only "scratch" (or Kokoro's own) ships.
    base: str = ""

    @property
    def archive(self) -> bool:
        return self.file.filename.endswith(".tar.bz2")

    @property
    def size(self) -> int:
        return self.file.size


# Section 1: NVIDIA Parakeet TDT 0.6B v3, int8 (25 European languages, all ten of ours).
PARAKEET = Pack(
    key="parakeet-tdt-0.6b-v3",
    kind="stt",
    file=ModelFile(
        name="NVIDIA Parakeet TDT 0.6B v3 (int8)",
        filename="sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2",
        url=f"{RELEASES}/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2",
        sha256="1495e91b59d7fc53ddca91f3431758ec99f8fa253fdb0e1e851be0f72b14b0f9",
        size=487_211_443,
        source=N_("NVIDIA via sherpa-onnx on GitHub"),
        licence="CC-BY-4.0",
    ),
    folder="sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
    author="NVIDIA (nvidia/parakeet-tdt-0.6b-v3)",
)

# When the person has stopped speaking.
SILERO_VAD = Pack(
    key="silero-vad",
    kind="vad",
    file=ModelFile(
        name="Silero VAD",
        filename="silero_vad.onnx",
        url=f"{RELEASES}/asr-models/silero_vad.onnx",
        sha256="9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
        size=643_854,
        source=N_("Silero via sherpa-onnx on GitHub"),
        licence="MIT",
    ),
    folder="silero-vad",
    author="Silero Team (snakers4/silero-vad)",
)

# Section 2.1: trained from scratch on a CC0 dataset; three speakers.
PIPER_UK = Pack(
    key="piper-uk-ukrainian_tts-medium",
    kind="piper",
    file=ModelFile(
        name="Piper uk_UA ukrainian_tts (medium)",
        filename="vits-piper-uk_UA-ukrainian_tts-medium.tar.bz2",
        url=f"{RELEASES}/tts-models/vits-piper-uk_UA-ukrainian_tts-medium.tar.bz2",
        sha256="92be6ff21ebb1cbcc0c71121a6437db8d060cee5785287aaa6a19a2548f59c03",
        size=80_355_083,
        source=N_("Piper via sherpa-onnx on GitHub"),
        licence="CC0-1.0",
    ),
    folder="vits-piper-uk_UA-ukrainian_tts-medium",
    author="Rhasspy Piper, NabuCasa voice-datasets (ukrainian_tts)",
    base="scratch",  # MODEL_CARD: "Trained from scratch."
)


# Section 2.2: one model and 54 voices; Gloxi uses those of en, es, it and fr.
KOKORO = Pack(
    key="kokoro-82m-v1.0-int8",
    kind="kokoro",
    file=ModelFile(
        name="Kokoro-82M v1.0 (int8)",
        filename="kokoro-int8-multi-lang-v1_0.tar.bz2",
        url=f"{RELEASES}/tts-models/kokoro-int8-multi-lang-v1_0.tar.bz2",
        sha256="72c347230107ef8a31098ec4d468a605244beef5f1ed00db97efa13518800653",
        size=132_353_410,
        source=N_("hexgrad via sherpa-onnx on GitHub"),
        licence="Apache-2.0",
    ),
    folder="kokoro-int8-multi-lang-v1_0",
    author="hexgrad (Kokoro-82M), StyleTTS 2; SIWIS (CC-BY-4.0) for French",
    base="scratch",  # its own model, not a Piper voice (section 2.2)
)

PACKS = (PARAKEET, SILERO_VAD, PIPER_UK, KOKORO)


@dataclass(frozen=True)
class Voice:
    """How one language is read aloud."""

    lang: str
    pack: Pack | None  # None: eSpeak NG, nothing to download
    speaker: int = 0
    espeak: str = ""  # eSpeak NG voice for the phonemes, or for the whole speech
    name: str = ""  # shown in the settings


# ADR 0015, decision 4. Every voice is trained from scratch (``base``): most
# Piper voices, among them pl gosia, cs jirka, ro mihai and de thorsten, are
# fine-tuned from en_US-lessac, whose data forbids commercial use (section
# 2.1). RHVoice (pl, cs, ro) is an HTS voice sherpa-onnx cannot read, and
# Lithuanian ingute is fine-tuned from lessac too; eSpeak NG reads those
# languages, as section 2 allows. French uses Kokoro's SIWIS voice.
VOICES: dict[str, Voice] = {
    "uk": Voice("uk", PIPER_UK, speaker=1, name="Mykyta"),
    "en": Voice("en", KOKORO, speaker=3, espeak="en-us", name="Heart"),
    "es": Voice("es", KOKORO, speaker=28, espeak="es", name="Dora"),
    "it": Voice("it", KOKORO, speaker=35, espeak="it", name="Sara"),
    "fr": Voice("fr", KOKORO, speaker=30, espeak="fr-fr", name="Siwis"),
    "de": Voice("de", None, espeak="de", name="eSpeak NG"),
    "pl": Voice("pl", None, espeak="pl", name="eSpeak NG"),
    "cs": Voice("cs", None, espeak="cs", name="eSpeak NG"),
    "ro": Voice("ro", None, espeak="ro", name="eSpeak NG"),
    "lt": Voice("lt", None, espeak="lt", name="eSpeak NG"),
}

LANGUAGES = tuple(VOICES)


def voice_for(lang: str) -> Voice:
    """The voice of ``lang``; a language Gloxi has no voice for is read in English."""
    return VOICES.get(lang, VOICES["en"])


def packs_for(lang: str) -> tuple[Pack, ...]:
    """What dictation and reading aloud in ``lang`` need, in download order."""
    voice = voice_for(lang)
    packs = [PARAKEET, SILERO_VAD]
    if voice.pack is not None:
        packs.append(voice.pack)
    return tuple(packs)
