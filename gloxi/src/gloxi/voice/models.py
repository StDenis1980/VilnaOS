# SPDX-License-Identifier: GPL-3.0-or-later
"""What Gloxi downloads for its voice (ADR 0015, sections 1, 2 and 4).

Every file is pinned by size and SHA-256, as the language model is
(``gloxi.engine``). They come from the releases of sherpa-onnx on GitHub,
which repackages the original models for the engine Gloxi uses; the
licence is that of the original model, named here and shown to the person.
``tools/check_voice.py`` checks these values against GitHub.

Languages without a voice here (Lithuanian) are read by eSpeak NG (``VOICES``): it is
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
)


def _piper(lang: str, name: str, size: int, sha256: str, licence: str, author: str) -> Pack:
    """A Piper voice trained on eSpeak NG phonemes, as sherpa-onnx repackages it."""
    folder = f"vits-piper-{lang}-{name}-medium"
    return Pack(
        key=f"piper-{lang.split('_')[0]}-{name}-medium",
        kind="piper",
        file=ModelFile(
            name=f"Piper {lang} {name} (medium)",
            filename=f"{folder}.tar.bz2",
            url=f"{RELEASES}/tts-models/{folder}.tar.bz2",
            sha256=sha256,
            size=size,
            source=N_("Piper via sherpa-onnx on GitHub"),
            licence=licence,
        ),
        folder=folder,
        author=author,
    )


# Section 2.1: Piper voices with CC0 datasets, in place of RHVoice (RHVoice is
# an HTS voice that sherpa-onnx cannot read; see ADR 0015, 2026-11-10).
PIPER_PL = _piper(
    "pl_PL",
    "gosia",
    67_211_182,
    "75bd34dcbdc4dd98d763954756b4b34b4208100497c836381542e4d73dcefa9c",
    "CC0-1.0",
    "Rhasspy Piper, OHF-Voice voice-datasets (gosia)",
)
PIPER_CS = _piper(
    "cs_CZ",
    "jirka",
    67_212_775,
    "321aafd83dd89b3d5b2c36c5898e0710d4f472f3c191cb7f73901a89b7f8539f",
    "CC0-1.0",
    "Rhasspy Piper (jirka)",
)
PIPER_RO = _piper(
    "ro_RO",
    "mihai",
    67_182_137,
    "cfbee3cdc25cf063339f69e189f4d93cc3be6610d267567537323ced3cf87f4d",
    "CC0-1.0",
    "Rhasspy Piper (mihai)",
)
PIPER_DE = _piper(
    "de_DE",
    "thorsten",
    67_214_254,
    "50487d9c95fdf2191f31d2588569381063ba1591dcd4c7d4bdd30f12b2191714",
    "CC0-1.0",
    "Rhasspy Piper, Thorsten Müller (Thorsten-Voice)",
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
)

PACKS = (PARAKEET, SILERO_VAD, PIPER_UK, PIPER_PL, PIPER_CS, PIPER_RO, PIPER_DE, KOKORO)


@dataclass(frozen=True)
class Voice:
    """How one language is read aloud."""

    lang: str
    pack: Pack | None  # None: eSpeak NG, nothing to download
    speaker: int = 0
    espeak: str = ""  # eSpeak NG voice for the phonemes, or for the whole speech
    name: str = ""  # shown in the settings


# ADR 0015, decision 4. Lithuanian (ingute, section 2.3) is not pinned yet: it
# needs a phonemizer of its own, not eSpeak NG's; until then eSpeak NG reads
# Lithuanian, as section 2 allows. French uses Kokoro's SIWIS voice.
VOICES: dict[str, Voice] = {
    "uk": Voice("uk", PIPER_UK, speaker=1, name="Mykyta"),
    "en": Voice("en", KOKORO, speaker=3, espeak="en-us", name="Heart"),
    "es": Voice("es", KOKORO, speaker=28, espeak="es", name="Dora"),
    "it": Voice("it", KOKORO, speaker=35, espeak="it", name="Sara"),
    "fr": Voice("fr", KOKORO, speaker=30, espeak="fr-fr", name="Siwis"),
    "de": Voice("de", PIPER_DE, espeak="de", name="Thorsten"),
    "pl": Voice("pl", PIPER_PL, espeak="pl", name="Gosia"),
    "cs": Voice("cs", PIPER_CS, espeak="cs", name="Jirka"),
    "ro": Voice("ro", PIPER_RO, espeak="ro", name="Mihai"),
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
