#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Make the Vilna sound themes (freedesktop Sound Theme spec).

Every event has one motif, the same in every theme; a theme changes only
the timbre. The output is deterministic: the same script gives the same
samples. Needs numpy and ffmpeg (libvorbis).

    python3 make_sounds.py OUT_DIR [--theme wood] [--preview]

writes OUT_DIR/vilna-<theme>/index.theme and stereo/<event>.oga.
The sounds are CC-BY-SA-4.0, like the other Vilna artwork.
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


TIMBRES = {"wood": wood, "bell": bell}


def _room(x: np.ndarray, rng: np.random.Generator, wet: float = 0.10) -> np.ndarray:
    """A small dry room: convolution with decaying noise."""
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    ir = rng.standard_normal(n) * np.exp(-t / 0.08)
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
        out[start : start + n] += TIMBRES[theme](f, n, rng) * (0.9**i)
    out = _room(out, rng)
    fade = int(0.03 * SR)
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
