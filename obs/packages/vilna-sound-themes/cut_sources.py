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
    "Fringilla_coelebs_singing_in_Norfolk,_England.ogg": "1af4f5d5f4b0ba2fbd906dab7f69f59d0d500285",
    "Binging_glass.ogg": "6dc71d3fa58bbe10001fd8d3199add30facaf101",
}

CHAFFINCH = "Fringilla_coelebs_singing_in_Norfolk,_England.ogg"

# excerpt -> (original, start in s, length in s)
CUTS = {
    "birds-chaffinch-1": (CHAFFINCH, 53.40, 0.15),
    "birds-chaffinch-2": (CHAFFINCH, 53.56, 0.18),
    "birds-chaffinch-3": (CHAFFINCH, 53.74, 0.19),
    "birds-chaffinch-4": (CHAFFINCH, 53.95, 0.15),
    "birds-chaffinch-5": (CHAFFINCH, 54.09, 0.16),
    "birds-chaffinch-6": (CHAFFINCH, 54.30, 0.11),
    "birds-chaffinch-7": (CHAFFINCH, 54.41, 0.11),
    "birds-chaffinch-8": (CHAFFINCH, 54.99, 0.17),
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
