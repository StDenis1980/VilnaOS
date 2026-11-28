#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check that every source file carries the SPDX licence of its folder.

Folders with their own Apache-2.0 LICENSE (see LICENSING.md) need
``SPDX-License-Identifier: Apache-2.0``; everything else needs
``GPL-3.0-or-later``. Only tracked files are checked.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

APACHE_DIRS = ("privacy-gateway/", "mcp/", "sdk/")
SOURCE_SUFFIXES = {".py", ".rs", ".sh", ".toml", ".js", ".ts", ".c", ".h"}
SKIP = {"uv.lock", "Cargo.lock"}
HEAD_LINES = 5


def expected_licence(path: str) -> str:
    return "Apache-2.0" if path.startswith(APACHE_DIRS) else "GPL-3.0-or-later"


def head_of(path: Path) -> list[str] | None:
    """The first lines of a regular file, or ``None`` when it cannot be read."""
    try:
        if not path.is_file():
            return None
        with path.open("rb") as f:
            data = f.read(4096)
    except OSError:
        return None
    return data.decode("utf-8", errors="replace").splitlines()[:HEAD_LINES]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    try:
        files = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            errors="surrogateescape",
        ).stdout.split("\0")
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    problems = []
    for rel in files:
        path = root / rel
        if not rel or path.suffix not in SOURCE_SUFFIXES or path.name in SKIP:
            continue
        head = head_of(path)
        if head is None:
            problems.append(f"{rel}: tracked, but not a file that can be read")
            continue
        if not any(line.strip() for line in head):
            continue  # empty file
        want = f"SPDX-License-Identifier: {expected_licence(rel)}"
        if not any(want in line for line in head):
            found = next((ln.strip() for ln in head if "SPDX-License-Identifier" in ln), None)
            problems.append(f"{rel}: expected '{want}'" + (f", found '{found}'" if found else ""))
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    # A file name that is not UTF-8 is printed escaped, not as a traceback.
    sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    sys.exit(main())
