#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pins of Cognition's Devin Desktop .deb (formerly Windsurf) for its extra-data Flatpak.

    python3 tools/devin_desktop.py check            # the pins are well formed (no network)
    python3 tools/devin_desktop.py latest           # compare the pins with Cognition's apt index
    python3 tools/devin_desktop.py latest --write   # move the pins to the newest version

Cognition publishes Devin Desktop (the editor that was called Windsurf) in a
signed apt repository, amd64 only, the same way Anthropic publishes Claude.
So this is tools/claude_desktop.py with Cognition's repository, key and
package: ``latest`` checks the InRelease signature with Cognition's key
(fingerprint pinned below), checks the Packages index against InRelease and
pins the newest devin-desktop from it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_desktop as cd  # noqa: E402

cd.APP = cd.ROOT / "thirdparty" / "devin-desktop"
cd.APP_ID = "io.github.stdenis1980.DevinDesktop"
cd.MANIFEST = cd.APP / f"{cd.APP_ID}.yml"
cd.METAINFO = cd.APP / f"{cd.APP_ID}.metainfo.xml"
# The repository still has the pre-rename "windsurf" name; the package is
# devin-desktop ("windsurf" is a transitional package that pulls it in).
cd.REPO = "https://windsurf-stable.codeiumdata.com/wVxQEIWkwPUEAGf3/apt"
cd.KEY_URL = "https://windsurf-stable.codeiumdata.com/wVxQEIWkwPUEAGf3/windsurf.gpg"
cd.SUITE = "stable"
cd.COMPONENT = "main"
cd.PACKAGE = "devin-desktop"
# Cognition's apt signing key, as windsurf.gpg served it on 2026-11-27 (first
# seen by CI, PR #262;
# trust on first use like `apt` itself); a different key is then an error.
cd.KEY_FINGERPRINT = "EE2C46CC44E1578C23135B0E9E608A1B9FD62A3F"
cd.ARCHES = {"x86_64": "amd64"}
cd.NAME = "devin-desktop"
cd.VENDOR = "Cognition"
cd.TOOL = "tools/devin_desktop.py"

if __name__ == "__main__":
    sys.exit(cd.main())
