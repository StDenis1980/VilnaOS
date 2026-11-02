# SPDX-License-Identifier: GPL-3.0-or-later
"""Check that the model pinned in gloxi.engine is the file Hugging Face serves.

    uv run python tools/check_model.py

CI runs this (it needs the network). On a mismatch it prints the published
size and SHA-256, so that a deliberate update is a copy, not a guess.
"""

from __future__ import annotations

import sys
from urllib.parse import urlsplit

import httpx

from gloxi.engine import DEFAULT_MODEL


def published(url: str) -> dict:
    # https://huggingface.co/<org>/<repo>/resolve/<rev>/<path>
    org, repo, _resolve, rev, *path = urlsplit(url).path.strip("/").split("/")
    api = f"https://huggingface.co/api/models/{org}/{repo}/tree/{rev}"
    response = httpx.get(api, timeout=30, follow_redirects=True)
    response.raise_for_status()
    name = "/".join(path)
    for entry in response.json():
        if entry.get("path") == name:
            return entry
    raise SystemExit(f"{name} is not in {org}/{repo}@{rev}")


def main() -> int:
    m = DEFAULT_MODEL
    entry = published(m.url)
    lfs = entry.get("lfs") or {}
    sha, size = lfs.get("oid"), lfs.get("size", entry.get("size"))
    if (sha, size) != (m.sha256, m.size):
        print(f"{m.filename}: pinned sha256={m.sha256} size={m.size}", file=sys.stderr)
        print(f"{m.filename}: published sha256={sha} size={size}", file=sys.stderr)
        return 1
    print(f"{m.filename}: matches Hugging Face ({size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
