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
    "Common_blackbird_singing_denoised.flac": "d96cad234eacb1888658a29a85791a7edf664f7f",
    "Bird_call_separated_chirps.ogg": "c27612d85addec63e95f61da51645a83c51d2a71",
    "Binging_glass.ogg": "6dc71d3fa58bbe10001fd8d3199add30facaf101",
}

# excerpt -> (original, start in s, length in s)
CUTS = {
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
