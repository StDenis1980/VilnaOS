# SPDX-License-Identifier: GPL-3.0-or-later
"""Gloxi's voice (ADR 0015): models, safe unpacking, what is read, the controller.

The real models are not needed here. With GLOXI_VOICE_MODELS pointing at a
folder where they are installed (``gloxi voice download``), the last tests
also read a sentence aloud in every language and recognise it.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import struct
import tarfile
from array import array
from dataclasses import replace
from pathlib import Path

import pytest

from gloxi.bench import voice as bench
from gloxi.engine import DownloadCancelled, EngineError, ModelFile
from gloxi.voice import language, models, phonemes, speech, store, tts
from gloxi.voice.session import VoiceController, VoicePrefs, consent_text, load_prefs, save_prefs
from gloxi.voice.stt import Dictation
from gloxi.voice.tts import Audio, SpeechError, Synthesizer, wav_to_audio

# -- the catalogue -----------------------------------------------------------------


def test_every_pack_is_pinned():
    for pack in models.PACKS:
        assert re.fullmatch(r"[0-9a-f]{64}", pack.file.sha256), pack.key
        assert pack.file.size > 0
        assert pack.file.url.startswith(models.RELEASES + "/")
        assert pack.file.url.endswith("/" + pack.file.filename)
        assert pack.file.licence in {"CC-BY-4.0", "MIT", "CC0-1.0", "Apache-2.0"}
        assert pack.author


def test_every_language_has_a_voice():
    from gloxi.voice.language import LANGUAGES

    assert set(LANGUAGES) == {"uk", "en", "es", "pl", "de", "fr", "it", "ro", "cs", "lt"}
    for lang, voice in models.VOICES.items():
        assert voice.lang == lang
        assert voice.pack is not None or voice.espeak, lang


def test_packs_for_a_language():
    assert models.packs_for("uk") == (models.PARAKEET, models.SILERO_VAD, models.PIPER_UK)
    assert models.packs_for("es")[-1] is models.KOKORO
    assert models.packs_for("pl") == (models.PARAKEET, models.SILERO_VAD)  # eSpeak NG
    assert models.voice_for("xx") is models.VOICES["en"]


def test_no_lessac_or_ryan_or_non_commercial_voices():
    # ADR 0015 2.1: voices fine-tuned from these cannot be shipped.
    for pack in models.PACKS:
        assert not re.search(r"lessac|ryan|pavoque|cezary", pack.file.filename)


def test_every_voice_is_trained_from_scratch():
    # Most Piper voices (pl gosia, cs jirka, ro mihai, de thorsten, lt ingute…)
    # are fine-tuned from en_US-lessac: their own data is clean, but the base
    # is not. A voice states what its MODEL_CARD says, and only "scratch" ships.
    for pack in models.PACKS:
        if pack.kind in {"piper", "kokoro"}:
            assert pack.base == "scratch", pack.key


# -- downloading and unpacking -------------------------------------------------------


def _archive(tmp_path: Path, files: dict[str, bytes], *, extra=None) -> Path:
    path = tmp_path / "pack.tar.bz2"
    with tarfile.open(path, "w:bz2") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if extra:
            extra(tar)
    return path


def _pack(path: Path, folder="voice-x") -> models.Pack:
    data = path.read_bytes()
    return models.Pack(
        key="x",
        kind="piper",
        file=ModelFile(
            name="X",
            filename=path.name,
            url=f"{models.RELEASES}/x/{path.name}",
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            source="test",
            licence="CC0-1.0",
        ),
        folder=folder,
        author="test",
    )


def _local_fetch(source: Path):
    def fetch(model, dest, **_kw):
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / model.filename
        shutil.copyfile(source, target)
        return target

    return fetch


def test_install_unpacks_and_marks(tmp_path):
    src = _archive(tmp_path, {"voice-x/a.onnx": b"model", "voice-x/sub/tokens.txt": b"a 0\n"})
    pack = _pack(src)
    base = tmp_path / "voice"
    assert store.installed(pack, base) is None
    folder = store.install(pack, base, fetch=_local_fetch(src))
    assert (folder / "a.onnx").read_bytes() == b"model"
    assert (folder / "sub" / "tokens.txt").exists()
    assert store.installed(pack, base) == folder
    assert not (base / ".downloads" / src.name).exists()  # the archive is not kept
    assert store.missing((pack,), base) == ()
    store.remove(pack, base)
    assert store.installed(pack, base) is None


def test_install_a_single_file(tmp_path):
    src = tmp_path / "silero_vad.onnx"
    src.write_bytes(b"vad")
    pack = replace(_pack(src, folder="silero-vad"), kind="vad")
    folder = store.install(pack, tmp_path / "voice", fetch=_local_fetch(src))
    assert (folder / "silero_vad.onnx").read_bytes() == b"vad"


def test_a_marker_from_another_file_is_not_installed(tmp_path):
    src = _archive(tmp_path, {"voice-x/a.onnx": b"model"})
    pack = _pack(src)
    folder = tmp_path / "voice" / "voice-x"
    folder.mkdir(parents=True)
    (folder / store.MARKER).write_text("0" * 64)
    assert store.installed(pack, tmp_path / "voice") is None


def test_unpack_refuses_links(tmp_path):
    def link(tar):
        info = tarfile.TarInfo("voice-x/evil")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)

    src = _archive(tmp_path, {"voice-x/a.onnx": b"m"}, extra=link)
    with pytest.raises(EngineError):
        store.install(_pack(src), tmp_path / "voice", fetch=_local_fetch(src))
    assert not (tmp_path / "voice" / "voice-x").exists()  # nothing half-installed


def test_unpack_refuses_paths_outside(tmp_path):
    src = _archive(tmp_path, {"voice-x/../../escape.txt": b"x"})
    with pytest.raises(EngineError):
        store.install(_pack(src), tmp_path / "voice", fetch=_local_fetch(src))
    assert not (tmp_path / "escape.txt").exists()


def test_unpack_stops_past_the_size_limit(tmp_path):
    src = _archive(tmp_path, {"voice-x/big": bytes(200_000)})
    with pytest.raises(EngineError, match="larger"):
        store.unpack(src, tmp_path / "out", "voice-x", limit=1000)


def test_unpack_reports_a_damaged_archive(tmp_path):
    bad = tmp_path / "bad.tar.bz2"
    bad.write_bytes(b"not bzip2")
    with pytest.raises(EngineError, match="damaged"):
        store.unpack(bad, tmp_path / "out", "voice-x", limit=1000)


# -- language ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,lang",
    [
        ("Привіт! Як справи?", "uk"),
        ("Hello, what is the weather like today?", "en"),
        ("Hola, ¿qué tiempo hace hoy en la ciudad?", "es"),
        ("Cześć, jaka jest dziś pogoda w mieście?", "pl"),
        ("Hallo, wie ist das Wetter heute? Ich weiß es nicht.", "de"),
        ("Bonjour, quel temps fait-il aujourd'hui dans la ville ?", "fr"),
        ("Ciao, che tempo fa oggi in città? Non lo so.", "it"),
        ("Bună ziua, cum este vremea astăzi în oraș?", "ro"),
        ("Dobrý den, jaké je dnes počasí ve městě? Nevím.", "cs"),
        ("Labas, koks šiandien oras mieste? Aš nežinau.", "lt"),
    ],
)
def test_detect(text, lang):
    assert language.detect(text, fallback="en") == lang


def test_detect_falls_back():
    assert language.detect("12345 ...", fallback="pl") == "pl"
    assert language.detect("OK", fallback="de") == "de"


def test_interface_language():
    assert language.interface_language({"LANGUAGE": "uk:en", "LANG": "C"}) == "uk"
    assert language.interface_language({"LANG": "de_DE.UTF-8"}) == "de"
    assert language.interface_language({"LANG": "C.UTF-8"}) == "en"
    assert language.interface_language({"LANGUAGE": "ja", "LANG": "pl_PL"}) == "en"
    assert language.interface_language({"LANGUAGE": "ja:cs"}) == "cs"
    assert language.interface_language({}) == "en"


# -- what is read aloud ----------------------------------------------------------------


def test_a_hidden_password_is_not_spelled():
    out = speech.spoken("Your password is •••••• and the login is anna.")
    assert "•" not in out
    assert "password hidden" in out


def test_markdown_links_and_code_are_not_read():
    out = speech.spoken(
        "**Done.** See [the guide](https://example.org/x) or https://vilna.eu.\n"
        "```\nrm -rf /\n```\n- `ls` lists files"
    )
    assert "*" not in out and "`" not in out and "rm -rf" not in out
    assert "https" not in out and "the guide" in out and "link." in out
    assert "The code is on the screen." in out
    assert "ls lists files" in out


def test_sentences_are_short():
    long = "word, " * 200
    parts = speech.sentences("First one. Second one!\nThird? " + long)
    assert parts[:3] == ["First one.", "Second one!", "Third?"]
    assert all(len(p) <= speech.MAX_SENTENCE for p in parts)
    assert speech.sentences("  ...  \n\n") == []


@pytest.mark.parametrize(
    "n,words",
    [
        (0, "нуль"),
        (9, "дев'ять"),
        (11, "одинадцять"),
        (21, "двадцять один"),
        (1001, "одна тисяча один"),
        (2022, "дві тисячі двадцять два"),
        (5000, "п'ять тисяч"),
        (12_500, "дванадцять тисяч п'ятсот"),
        (2_000_000, "два мільйони"),
        (-3, "мінус три"),
    ],
)
def test_uk_number(n, words):
    assert speech.uk_number(n) == words


def test_uk_numbers_in_text():
    assert speech.uk_numbers("о 9:30") == "о дев'ять тридцять"
    assert speech.uk_numbers("2,5 ГБ") == "два кома п'ять ГБ"
    assert speech.uk_numbers("12 500 грн") == "дванадцять тисяч п'ятсот грн"
    assert speech.uk_numbers("кроки 1, 2 і 3") == "кроки один, два і три"
    assert speech.uk_numbers("В1 і x2") == "В1 і x2"  # not a number on its own


# -- phonemes and sound ----------------------------------------------------------------


def test_letters_and_codepoints(tmp_path):
    tokens = tmp_path / "tokens.txt"
    tokens.write_text("_ 0\n  1\nа 2\nи 3\n̆ 4\n", encoding="utf-8")
    known = phonemes.read_tokens(tokens)
    assert known == {"_", " ", "а", "и", "̆"}
    # й is и + combining breve, as the voice was trained.
    assert phonemes.codepoints(phonemes.letters("Ай, X"), known) == [
        ord("а"),
        ord("и"),
        0x306,
        ord(" "),
    ]


def test_espeak_missing_is_an_error():
    with pytest.raises(phonemes.PhonemeError):
        phonemes.espeak_ipa("hi", "en-us", binary="/nonexistent/espeak-ng")


def _fake_espeak(tmp_path: Path, script: str) -> str:
    path = tmp_path / "espeak-ng"
    path.write_text("#!/bin/sh\n" + script + "\n")
    path.chmod(0o755)
    return str(path)


def test_secondary_stress_only_for_piper(tmp_path):
    binary = _fake_espeak(tmp_path, "cat >/dev/null; printf 'ˈa_ˌb\\n'")
    assert phonemes.espeak_ipa("x", "pl", binary=binary) == "ˈab"
    assert phonemes.espeak_ipa("x", "pl", binary=binary, secondary_stress=True) == "ˈaˌb"


def test_phoneme_failure_is_a_speech_error(tmp_path, monkeypatch):
    # gloxi-bench and the window catch SpeechError, not the phonemizer's own error.
    monkeypatch.setattr(tts, "installed", lambda pack, base: tmp_path)
    synth = Synthesizer(tmp_path, espeak=_fake_espeak(tmp_path, "exit 3"))
    monkeypatch.setattr(synth, "_engine", lambda voice: (None, set()))
    with pytest.raises(SpeechError):
        synth.synthesize("Good morning.", "en")


def _wav(samples: list[int], rate=22050, data_size=None) -> bytes:
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    size = len(pcm) if data_size is None else data_size
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    return (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
        + b"fmt " + struct.pack("<I", 16) + fmt
        + b"data" + struct.pack("<I", size) + pcm
    )  # fmt: skip


def test_wav_from_a_pipe():
    # eSpeak NG writes a placeholder length when its output is a pipe.
    audio = wav_to_audio(_wav([0, 16384, -32768], data_size=0x7FFFFFF0))
    assert audio.rate == 22050
    assert list(audio.samples) == [0.0, 0.5, -1.0]


def test_wav_not_wav():
    with pytest.raises(SpeechError):
        wav_to_audio(b"nope")


def test_missing_voice_model_falls_back_to_espeak(tmp_path):
    synth = Synthesizer(tmp_path)
    assert synth.voice("uk").pack is None
    assert synth.voice("uk").espeak == "uk"
    assert synth.voice("de").espeak == "de"


@pytest.mark.skipif(shutil.which("espeak-ng") is None, reason="espeak-ng is not installed")
def test_espeak_reads_aloud(tmp_path):
    audio = Synthesizer(tmp_path).synthesize("Dzień dobry.", "pl")
    assert audio.rate > 0 and audio.seconds > 0.3


# -- dictation ---------------------------------------------------------------------------


def loud(samples) -> bool:
    return any(abs(x) > 0.1 for x in samples)


def test_dictation_ends_after_silence():
    d = Dictation(loud, rate=1000, silence_after_speech=0.5, nothing_said=2)
    assert not d.feed([0.0] * 300)
    assert not d.feed([0.5] * 300)
    assert not d.feed([0.0] * 300)
    assert d.feed([0.0] * 300)
    assert d.heard_speech


def test_dictation_ends_when_nothing_is_said():
    d = Dictation(loud, rate=1000, nothing_said=1)
    assert not d.feed([0.0] * 600)
    assert d.feed([0.0] * 600)
    assert not d.heard_speech


def test_dictation_is_capped_and_wiped():
    d = Dictation(loud, rate=1000, max_seconds=1, silence_after_speech=10)
    assert d.feed([0.5] * 5000)
    assert len(d.sound) == 1000

    class Rec:
        def transcribe(self, samples, rate):
            self.got = (len(samples), rate)
            return "текст"

    rec = Rec()
    assert d.finish(rec) == "текст"
    assert rec.got == (1000, 1000)
    assert len(d.sound) == 0


def test_dictation_without_speech_is_not_recognised():
    d = Dictation(loud, rate=1000)
    d.feed([0.0] * 100)
    assert d.finish(None) == ""  # the recognizer is not even touched


# -- the controller ------------------------------------------------------------------------


class View:
    def __init__(self, answer=None):
        self.events = []
        self.answer = answer  # True/False answers the consent at once

    def ask_voice_download(self, text, answer):
        self.events.append(("ask", text))
        if self.answer is not None:
            answer(self.answer)

    def show_voice_progress(self, text, fraction):
        self.events.append(("progress", fraction))

    def hide_voice_progress(self):
        self.events.append(("hide",))

    def set_listening(self, on):
        self.events.append(("listening", on))

    def set_speaking(self, on):
        self.events.append(("speaking", on))

    def put_dictation(self, text, send):
        self.events.append(("dictation", text, send))

    def show_error(self, text):
        self.events.append(("error", text))

    def show_note(self, text):
        self.events.append(("note", text))

    def kinds(self):
        return [e[0] for e in self.events]


class FakeMic:
    made = []

    def __init__(self, on_samples, on_error=None):
        self.on_samples = on_samples
        self.running = False
        FakeMic.made.append(self)

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


class FakePlayer:
    made = []

    def __init__(self, on_done, on_error):
        self.on_done = on_done
        self.pushed = []
        self.stopped = False
        FakePlayer.made.append(self)

    def push(self, samples, rate):
        self.pushed.append(len(samples))

    def finish(self):
        self.on_done()

    def stop(self):
        self.stopped = True


class FakeRecognizer:
    def __init__(self, text="Привіт, Gloxi."):
        self.text = text
        self.loaded = 0
        self.unloaded = 0

    def load(self):
        self.loaded += 1

    def transcribe(self, samples, rate):
        return self.text

    def idle_for(self):
        return 1000.0

    def unload(self):
        self.unloaded += 1


class FakeSynth:
    def __init__(self):
        self.said = []

    def synthesize(self, sentence, lang):
        self.said.append((sentence, lang))
        return Audio(array("f", [0.1] * 10), 16000)

    def unload(self):
        pass


def sync(job):
    job()


def _fetching(tmp_path):
    """A fetch that 'downloads' tiny archives with the real packs' folder names."""

    def fetch(model, dest, **kw):
        pack = next(p for p in models.PACKS if p.file is model)
        kw["progress"](model.size, model.size)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / model.filename
        if pack.archive:
            with tarfile.open(target, "w:bz2") as tar:
                info = tarfile.TarInfo(f"{pack.folder}/tokens.txt")
                info.size = 4
                tar.addfile(info, io.BytesIO(b"a 0\n"))
        else:
            target.write_bytes(b"vad")
        return target

    return fetch


def make_controller(tmp_path, view, **kwargs):
    FakeMic.made.clear()
    FakePlayer.made.clear()
    defaults = dict(
        base=tmp_path / "voice",
        lang="uk",
        runner=sync,
        fetch=_fetching(tmp_path),
        recognizer=FakeRecognizer(),
        synthesizer=FakeSynth(),
        detector=lambda base: loud,
        microphone=FakeMic,
        player=FakePlayer,
        save=lambda prefs: None,
    )
    defaults.update(kwargs)
    return VoiceController(view, **defaults)


def test_consent_names_every_model(tmp_path):
    text = consent_text(models.packs_for("uk"), tmp_path)
    for pack in models.packs_for("uk"):
        assert pack.file.name in text and pack.file.licence in text
    assert str(tmp_path) in text
    assert "never leaves this computer" in text


def test_no_download_without_a_yes(tmp_path):
    view = View(answer=False)
    ctl = make_controller(tmp_path, view)
    ctl.press_microphone()
    assert view.kinds() == ["ask", "note"]
    assert not (tmp_path / "voice").exists()
    assert not FakeMic.made


def test_yes_downloads_then_listens(tmp_path):
    view = View(answer=True)
    ctl = make_controller(tmp_path, view)
    ctl.press_microphone()
    assert ctl.missing() == ()
    assert ("listening", True) in view.events
    assert FakeMic.made[0].running
    # The person speaks, then is silent: the phrase ends by itself.
    mic = FakeMic.made[0]
    mic.on_samples([0.5] * 16000)
    mic.on_samples([0.0] * 32000)
    assert not mic.running
    assert ("dictation", "Привіт, Gloxi.", False) in view.events
    assert view.events.index(("listening", False)) < view.events.index(
        ("dictation", "Привіт, Gloxi.", False)
    )


def _installed(tmp_path, lang="uk"):
    fetch = _fetching(tmp_path)
    for pack in models.packs_for(lang):
        store.install(pack, tmp_path / "voice", fetch=fetch)


def test_second_press_ends_the_phrase_and_auto_send(tmp_path):
    _installed(tmp_path)
    view = View()
    ctl = make_controller(tmp_path, view, prefs=VoicePrefs(auto_send=True))
    ctl.press_microphone()
    FakeMic.made[0].on_samples([0.5] * 100)
    ctl.press_microphone()
    assert ("dictation", "Привіт, Gloxi.", True) in view.events
    assert not ctl.listening


def test_nothing_said(tmp_path):
    _installed(tmp_path)
    view = View()
    ctl = make_controller(tmp_path, view)
    ctl.press_microphone()
    FakeMic.made[0].on_samples([0.0] * 16000 * 9)
    assert "dictation" not in view.kinds()
    assert view.kinds()[-1] == "note"


def test_escape_drops_the_sound(tmp_path):
    _installed(tmp_path)
    view = View()
    ctl = make_controller(tmp_path, view)
    ctl.press_microphone()
    FakeMic.made[0].on_samples([0.5] * 100)
    ctl.cancel_listening()
    assert not ctl.listening and "dictation" not in view.kinds()


def test_reading_aloud_is_off_by_default(tmp_path):
    _installed(tmp_path)
    synth = FakeSynth()
    ctl = make_controller(tmp_path, View(), synthesizer=synth)
    ctl.speak("Привіт.")
    assert synth.said == []


def test_read_aloud_in_the_answers_language_without_the_password(tmp_path):
    _installed(tmp_path)
    _installed(tmp_path, "en")
    synth = FakeSynth()
    view = View()
    ctl = make_controller(tmp_path, view, synthesizer=synth, prefs=VoicePrefs(read_aloud=True))
    ctl.speak("Готово. Ваш пароль ••••••, збережено.")
    assert [s for s, _ in synth.said] == ["Готово.", "Ваш пароль password hidden, збережено."]
    assert {lang for _, lang in synth.said} == {"uk"}
    assert view.events[0] == ("speaking", True) and view.events[-1] == ("speaking", False)
    synth.said.clear()
    ctl.speak("The weather is fine and it is warm.")
    assert synth.said[0][1] == "en"


def test_microphone_silences_gloxi(tmp_path):
    _installed(tmp_path)

    class SlowPlayer(FakePlayer):
        def finish(self):  # still playing
            pass

    view = View()
    ctl = make_controller(tmp_path, view, player=SlowPlayer, prefs=VoicePrefs(read_aloud=True))
    ctl.speak("Довга відповідь.")
    assert ctl.speaking
    ctl.press_microphone()
    assert SlowPlayer.made[0].stopped and not ctl.speaking
    assert ctl.listening
    # No reading aloud while the microphone is open.
    ctl.speak("Ще одна.")
    assert len(SlowPlayer.made) == 1


def test_turning_read_aloud_on_asks_once(tmp_path):
    saved = []
    view = View(answer=False)
    ctl = make_controller(tmp_path, view, save=saved.append)
    ctl.set_read_aloud(True)
    assert saved[-1].read_aloud and view.kinds() == ["ask", "note"]
    ctl.speak("Привіт.")  # not asked again for every answer
    assert view.kinds() == ["ask", "note"]


def test_cancelled_download(tmp_path):
    def fetch(model, dest, **kw):
        raise DownloadCancelled("cancelled")

    view = View(answer=True)
    ctl = make_controller(tmp_path, view, fetch=fetch)
    ctl.press_microphone()
    assert "listening" not in view.kinds()
    assert ("note", "The download was cancelled.") in view.events


def test_idle_model_is_unloaded(tmp_path):
    rec = FakeRecognizer()
    ctl = make_controller(tmp_path, View(), recognizer=rec)
    ctl.unload_if_idle()
    assert rec.unloaded == 1


def test_prefs_round_trip(tmp_path):
    path = tmp_path / "voice.json"
    assert load_prefs(path) == VoicePrefs()
    save_prefs(VoicePrefs(read_aloud=True), path)
    assert load_prefs(path) == VoicePrefs(read_aloud=True)
    path.write_text('{"read_aloud": "yes", "auto_send": true}')
    assert load_prefs(path) == VoicePrefs(auto_send=True)
    path.write_text("[1]")
    assert load_prefs(path) == VoicePrefs()


# -- the bench --------------------------------------------------------------------------------


def test_wer():
    assert bench.wer("Нагадай мені завтра", "нагадай мені завтра.") == 0.0
    assert bench.wer("a b c d", "a x c") == 0.5
    assert bench.wer("", "") == 0.0
    assert set(bench.PHRASES) == set(models.VOICES)


# -- with the real models ------------------------------------------------------------------

REAL = os.environ.get("GLOXI_VOICE_MODELS")


@pytest.mark.skipif(not REAL, reason="set GLOXI_VOICE_MODELS to a folder with the voice models")
@pytest.mark.parametrize("lang", ["uk", "en", "es", "it", "fr"])
def test_round_trip_with_real_models(lang):
    pytest.importorskip("sherpa_onnx")
    [result] = bench.run(Path(REAL), [lang])
    assert result.wer <= 0.35, result
