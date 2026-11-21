#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Make the Vilna sound themes (freedesktop Sound Theme spec).

Every event has one motif, the same in every theme; a theme changes only
the timbre. The output is deterministic: the same script gives the same
samples. Needs numpy and ffmpeg (libvorbis).

    python3 make_sounds.py OUT_DIR [--theme wood] [--preview]

writes OUT_DIR/vilna-<theme>/index.theme and stereo/<event>.oga.
The sounds are CC-BY-SA-4.0, like the other Vilna artwork. Wood, bell
and star are synthesised; water, air, birds and glass play short excerpts
of CC0 and public-domain recordings (sources/, SOURCES.md) tuned to the
motif.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

SR = 48000
PEAK = 0.35  # about -9 dBFS: quiet next to music and speech

# Pentatonic D major, so any two sounds that overlap stay in tune.
A4, D5, E5, Fs5, A5, B5, D6 = 440.0, 587.33, 659.26, 739.99, 880.0, 987.77, 1174.66

# event -> (notes, gap between notes in s, ring length in s)
MOTIFS: dict[str, tuple[list[float], float, float]] = {
    "message-new-instant": ([A5, D6], 0.09, 0.7),
    "bell": ([A5], 0.0, 0.5),
    "dialog-warning": ([A5, Fs5], 0.10, 0.7),
    "dialog-error": ([A5, Fs5, D5], 0.10, 0.8),
    "complete": ([D5, Fs5, A5], 0.08, 0.8),
    "screen-capture": ([D6], 0.0, 0.25),
    "audio-volume-change": ([A5], 0.0, 0.25),
    "device-added": ([D5, A5], 0.10, 0.6),
    "device-removed": ([A5, D5], 0.10, 0.6),
    "power-plug": ([Fs5, D6], 0.10, 0.6),
    "power-unplug": ([D6, Fs5], 0.10, 0.6),
    "battery-low": ([Fs5, Fs5], 0.18, 0.6),
    "trash-empty": ([D6, B5, A5, Fs5], 0.05, 0.6),
    "desktop-login": ([D5, Fs5, A5, D6], 0.14, 1.6),
    "x-vilna-gloxi-listen": ([B5], 0.0, 0.35),
    "x-vilna-gloxi-done": ([B5, D6], 0.07, 0.5),
    "x-vilna-gloxi-error": ([B5, Fs5], 0.08, 0.5),
    "x-vilna-herkos-consent": ([A5, B5], 0.12, 0.6),
}

# Theme names in the ten Vilna languages (ADR 0009).
THEMES: dict[str, dict[str, str]] = {
    "water": {
        "en": "Vilna Water",
        "uk": "Vilna Вода",
        "es": "Vilna Agua",
        "pl": "Vilna Woda",
        "de": "Vilna Wasser",
        "fr": "Vilna Eau",
        "it": "Vilna Acqua",
        "ro": "Vilna Apă",
        "cs": "Vilna Voda",
        "lt": "Vilna Vanduo",
    },
    "air": {
        "en": "Vilna Air",
        "uk": "Vilna Повітря",
        "es": "Vilna Aire",
        "pl": "Vilna Powietrze",
        "de": "Vilna Luft",
        "fr": "Vilna Air",
        "it": "Vilna Aria",
        "ro": "Vilna Aer",
        "cs": "Vilna Vzduch",
        "lt": "Vilna Oras",
    },
    "birds": {
        "en": "Vilna Birds",
        "uk": "Vilna Птахи",
        "es": "Vilna Pájaros",
        "pl": "Vilna Ptaki",
        "de": "Vilna Vögel",
        "fr": "Vilna Oiseaux",
        "it": "Vilna Uccelli",
        "ro": "Vilna Păsări",
        "cs": "Vilna Ptáci",
        "lt": "Vilna Paukščiai",
    },
    "glass": {
        "en": "Vilna Glass",
        "uk": "Vilna Скло",
        "es": "Vilna Cristal",
        "pl": "Vilna Szkło",
        "de": "Vilna Glas",
        "fr": "Vilna Verre",
        "it": "Vilna Vetro",
        "ro": "Vilna Sticlă",
        "cs": "Vilna Sklo",
        "lt": "Vilna Stiklas",
    },
    "star": {
        "en": "Vilna Star",
        "uk": "Vilna Зірка",
        "es": "Vilna Estrella",
        "pl": "Vilna Gwiazda",
        "de": "Vilna Stern",
        "fr": "Vilna Étoile",
        "it": "Vilna Stella",
        "ro": "Vilna Stea",
        "cs": "Vilna Hvězda",
        "lt": "Vilna Žvaigždė",
    },
    "wood": {
        "en": "Vilna Wood",
        "uk": "Vilna Дерево",
        "es": "Vilna Madera",
        "pl": "Vilna Drewno",
        "de": "Vilna Holz",
        "fr": "Vilna Bois",
        "it": "Vilna Legno",
        "ro": "Vilna Lemn",
        "cs": "Vilna Dřevo",
        "lt": "Vilna Medis",
    },
    "bell": {
        "en": "Vilna Bell",
        "uk": "Vilna Дзвіночок",
        "es": "Vilna Campanilla",
        "pl": "Vilna Dzwonek",
        "de": "Vilna Glöckchen",
        "fr": "Vilna Clochette",
        "it": "Vilna Campanella",
        "ro": "Vilna Clopoțel",
        "cs": "Vilna Zvoneček",
        "lt": "Vilna Varpelis",
    },
}


def _rng(seed: str) -> np.random.Generator:
    return np.random.default_rng(sum(map(ord, seed)))


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    return np.convolve(x, np.ones(n) / n, mode="same")


def _strike(n: int, ms: float, width: int, rng: np.random.Generator) -> np.ndarray:
    """A short soft noise burst: the mallet touching the bar."""
    t = np.arange(n) / SR
    return _smooth(rng.standard_normal(n), width) * np.exp(-t / (ms / 1000))


def _modes(f: float, n: int, modes: list[tuple[float, float, float]]) -> np.ndarray:
    """Modal synthesis: decaying partials (ratio, amplitude, decay in s)."""
    t = np.arange(n) / SR
    out = np.zeros(n)
    for ratio, amp, decay in modes:
        if f * ratio < SR / 2.2:
            out += amp * np.sin(2 * np.pi * f * ratio * t) * np.exp(-t / decay)
    return out


def wood(f: float, n: int, rng: np.random.Generator) -> np.ndarray:
    # A tuned marimba bar: partials near 1 : 4 : 10, upper ones die fast.
    body = _modes(f, n, [(1, 1.0, 0.18), (3.99, 0.28, 0.05), (9.9, 0.08, 0.015)])
    body += 0.10 * _modes(f / 2, n, [(1, 1.0, 0.12)])  # resonator tube
    return body + 0.25 * _strike(n, 4, 6, rng)


def bell(f: float, n: int, rng: np.random.Generator) -> np.ndarray:
    # A small metal bell: inharmonic partials, a slow beat from a
    # near-twin of the base partial, a bright hard strike.
    body = _modes(
        f,
        n,
        [
            (1, 0.8, 0.55),
            (1.004, 0.35, 0.5),
            (2.76, 0.45, 0.25),
            (5.40, 0.25, 0.12),
            (8.93, 0.12, 0.06),
        ],
    )
    return body + 0.12 * _strike(n, 2, 2, rng)


def star(f: float, n: int, rng: np.random.Generator) -> np.ndarray:
    # Space: a slow-blooming pure tone with a detuned twin (a slow beat), a
    # shimmering octave and a faint sparkle, all in a long echo (ROOMS).
    t = np.arange(n) / SR
    glide = f * (1 - 0.02 * np.exp(-t / 0.05))  # rises into the note
    ph = 2 * np.pi * np.cumsum(glide) / SR
    swell = np.minimum(t / 0.03, 1) * np.exp(-t / 0.55)
    tone = np.sin(ph) + 0.6 * np.sin(ph * 1.0023) + 0.6 * np.sin(ph * 0.9977)
    shimmer = 0.25 * np.sin(2 * ph) * (0.6 + 0.4 * np.sin(2 * np.pi * 6.5 * t))
    sparkle = 0.12 * np.sin(3.01 * ph) * np.exp(-t / 0.08)
    return (tone + shimmer) * swell + sparkle


# -- recorded timbres --------------------------------------------------------

# Theme -> excerpts in sources/ (cut_sources.py, SOURCES.md) and how far to
# move the whole motif so that the recordings shift as little as possible.
RECORDED = {
    "water": (["water-drip-1", "water-drip-2", "water-drip-3", "water-trickle-1"], 0.75),
    "birds": (["birds-blackbird-1", "birds-blackbird-2", "birds-chirp-1"], 2.0),
    "glass": (["glass-bing-1", "glass-bing-2"], 1.33),
    "air": (["air-blow-1", "air-breath-1"], 1.0),
}
SOURCES = Path(__file__).resolve().parent / "sources"
_cache: dict[str, np.ndarray] = {}


def _load(name: str) -> np.ndarray:
    if name not in _cache:
        raw = subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-i",
                str(SOURCES / f"{name}.flac"),
                "-ac",
                "1",
                "-ar",
                str(SR),
                "-f",
                "f32le",
                "-",
            ],
            capture_output=True,
            check=True,
        ).stdout
        x = np.frombuffer(raw, dtype="<f4").astype(float)
        _cache[name] = x / (np.max(np.abs(x)) or 1.0)
    return _cache[name]


def _pitch(x: np.ndarray) -> float:
    """The strongest partial between 200 Hz and 6 kHz."""
    size = 1 << 16
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)), size))
    freq = np.fft.rfftfreq(size, 1 / SR)
    spec[(freq < 200) | (freq > 6000)] = 0
    return float(freq[int(np.argmax(spec))])


def _resample(x: np.ndarray, ratio: float) -> np.ndarray:
    """Play faster by ratio: higher and shorter, as a real object would be."""
    pos = np.arange(0, len(x) - 1, ratio)
    return np.interp(pos, np.arange(len(x)), x)


def _fit(x: np.ndarray, n: int) -> np.ndarray:
    out = np.zeros(n)
    out[: min(n, len(x))] = x[:n]
    return out


def _resonate(noise: np.ndarray, f: float) -> np.ndarray:
    """Breath through a pipe: keep the noise only near f and its overtones."""
    size = len(noise)
    spec = np.fft.rfft(noise)
    freq = np.fft.rfftfreq(size, 1 / SR)
    gain = sum(
        a * np.exp(-(((freq - f * k) / (f * 0.012)) ** 2)) for k, a in ((1, 1), (2, 0.4), (3, 0.15))
    )
    return np.fft.irfft(spec * gain, size)


def recorded(theme: str):
    names, _ = RECORDED[theme]

    def timbre(f: float, n: int, rng: np.random.Generator) -> np.ndarray:
        x = _load(names[int(rng.integers(len(names)))])
        if theme == "air":
            breath = _fit(x, n)
            env = np.sqrt(np.convolve(breath**2, np.ones(480) / 480, "same"))
            tone = _resonate(breath, f)
            # A steady pitch over the breath's own swell, a little raw air on top.
            pipe = np.sin(2 * np.pi * f * np.arange(n) / SR) * env / (np.max(env) or 1)
            tone = tone / (np.max(np.abs(tone)) or 1)
            return 0.55 * tone + 0.35 * pipe + 0.10 * breath
        return _fit(_resample(x, f / _pitch(x)), n)

    return timbre


TIMBRES = {"wood": wood, "bell": bell, "star": star} | {t: recorded(t) for t in RECORDED}
# The echo of each theme: (wet level, decay in s).
ROOMS = {"star": (0.30, 0.35), "glass": (0.08, 0.08), "water": (0.12, 0.10)}


def _room(x: np.ndarray, rng: np.random.Generator, wet: float, decay: float) -> np.ndarray:
    """A room: convolution with decaying noise."""
    n = int(5 * decay * SR)
    t = np.arange(n) / SR
    ir = rng.standard_normal(n) * np.exp(-t / decay)
    ir = _smooth(ir, 3)
    ir /= np.sqrt(np.sum(ir**2))
    size = len(x) + n
    wetsig = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)
    out = np.concatenate([x, np.zeros(n)]) + wet * wetsig
    return out


def render(theme: str, event: str) -> np.ndarray:
    notes, gap, ring = MOTIFS[event]
    rng = _rng(theme + event)
    n = int(ring * SR)
    total = int((gap * (len(notes) - 1)) * SR) + n
    out = np.zeros(total)
    for i, f in enumerate(notes):
        start = int(i * gap * SR)
        # Later notes slightly softer, so a motif falls away gently.
        f *= RECORDED.get(theme, ((), 1.0))[1]
        out[start : start + n] += TIMBRES[theme](f, n, rng) * (0.9**i)
    wet, decay = ROOMS.get(theme, (0.10, 0.08))
    out = _room(out, rng, wet, decay)
    # Trim the silent tail the long echoes leave, then fade out.
    loud = np.nonzero(np.abs(out) > 0.01 * np.max(np.abs(out)))[0]
    out = out[: int(loud[-1]) + 1] if len(loud) else out
    fade = min(int(0.03 * SR), len(out))
    out[-fade:] *= np.linspace(1, 0, fade)
    peak = np.max(np.abs(out))
    return out / peak * PEAK if peak > 0 else out


def _encode(samples: np.ndarray, path: Path, codec: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "s.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(wav),
                *codec,
                "-map_metadata",
                "-1",
                "-fflags",
                "+bitexact",
                str(path),
            ],
            check=True,
        )


def index_theme(theme: str) -> str:
    names = THEMES[theme]
    lines = ["[Sound Theme]", f"Name={names['en']}"]
    lines += [f"Name[{lang}]={name}" for lang, name in names.items() if lang != "en"]
    lines += [
        "Inherits=freedesktop",
        "Directories=stereo",
        "",
        "[stereo]",
        "OutputProfile=stereo",
        "",
    ]
    return "\n".join(lines)


def make(out: Path, theme: str, preview: bool) -> None:
    root = out / f"vilna-{theme}"
    stereo = root / "stereo"
    stereo.mkdir(parents=True, exist_ok=True)
    (root / "index.theme").write_text(index_theme(theme), encoding="utf-8")
    parts = []
    for event in MOTIFS:
        s = render(theme, event)
        _encode(s, stereo / f"{event}.oga", ["-c:a", "libvorbis", "-q:a", "5"])
        parts += [s, np.zeros(int(0.5 * SR))]
    if preview:
        _encode(
            np.concatenate(parts), out / f"preview-{theme}.mp3", ["-c:a", "libmp3lame", "-q:a", "3"]
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("out", type=Path)
    p.add_argument("--theme", choices=sorted(TIMBRES), action="append")
    p.add_argument("--preview", action="store_true", help="also write preview-<theme>.mp3")
    a = p.parse_args(argv)
    if shutil.which("ffmpeg") is None:
        print("ffmpeg is needed", file=sys.stderr)
        return 1
    for theme in a.theme or sorted(TIMBRES):
        make(a.out, theme, a.preview)
    return 0


if __name__ == "__main__":
    sys.exit(main())
