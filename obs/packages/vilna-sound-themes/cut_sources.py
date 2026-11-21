#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cut the short excerpts the recorded themes use from their originals.

The originals are CC0 or public domain recordings from Wikimedia Commons
(SOURCES.md: title, author, page, licence, SHA-1). This keeps a record of
exactly which moment of which file each excerpt is, so the cut can be
checked and repeated:

    python3 cut_sources.py ORIGINALS_DIR

writes sources/<name>.flac (48 kHz mono, high-passed at 120 Hz, faded).
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The original files, by the SHA-1 Commons gives for them.
ORIGINALS = {
    "Drip_coffee_maker_dripping.ogg": "e183b6e34a4cc92651bf28675c76b8bece53e8aa",
    "Water_flowing_pouring_trickling.ogg": "4a302adbb0f1382c2d0d2ce7f13f204f264bc3ea",
    "Me_blowing_laptop_microphone.wav": "17529bb63c0b03be367aa6f8a2d5977c57ee4723",
    "Breathing_man.ogg": "e588e74da090efea5b528b979dcd1a93cc728eeb",
    "Common_blackbird_singing_denoised.flac": "d96cad234eacb1888658a29a85791a7edf664f7f",
    "Bird_call_separated_chirps.ogg": "c27612d85addec63e95f61da51645a83c51d2a71",
    "Binging_glass.ogg": "6dc71d3fa58bbe10001fd8d3199add30facaf101",
}

# excerpt -> (original, start in s, length in s)
CUTS = {
    "water-drip-1": ("Drip_coffee_maker_dripping.ogg", 46.24, 0.16),
    "water-drip-2": ("Drip_coffee_maker_dripping.ogg", 43.06, 0.16),
    "water-drip-3": ("Drip_coffee_maker_dripping.ogg", 31.78, 0.16),
    "water-trickle-1": ("Water_flowing_pouring_trickling.ogg", 70.02, 0.12),
    "air-blow-1": ("Me_blowing_laptop_microphone.wav", 2.22, 0.80),
    "air-breath-1": ("Breathing_man.ogg", 17.30, 0.30),
    "birds-blackbird-1": ("Common_blackbird_singing_denoised.flac", 3.68, 0.26),
    "birds-blackbird-2": ("Common_blackbird_singing_denoised.flac", 0.96, 0.27),
    "birds-chirp-1": ("Bird_call_separated_chirps.ogg", 30.95, 0.10),
    "glass-bing-1": ("Binging_glass.ogg", 62.09, 0.95),
    "glass-bing-2": ("Binging_glass.ogg", 52.54, 0.95),
}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    src = Path(argv[1])
    for name, sha1 in ORIGINALS.items():
        got = hashlib.sha1((src / name).read_bytes()).hexdigest()
        if got != sha1:
            print(f"{name}: SHA-1 {got}, expected {sha1}", file=sys.stderr)
            return 1
    out = HERE / "sources"
    out.mkdir(exist_ok=True)
    for name, (orig, start, length) in CUTS.items():
        fade = min(0.08, length / 3)
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{start}",
                "-t",
                f"{length}",
                "-i",
                str(src / orig),
                "-ac",
                "1",
                "-ar",
                "48000",
                "-af",
                f"highpass=f=120,afade=t=in:d=0.003,afade=t=out:st={length - fade}:d={fade}",
                "-map_metadata",
                "-1",
                "-fflags",
                "+bitexact",
                "-sample_fmt",
                "s16",
                str(out / f"{name}.flac"),
            ],
            check=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
