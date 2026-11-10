# SPDX-License-Identifier: GPL-3.0-or-later
"""Check that the voice models pinned in gloxi.voice.models are what GitHub serves.

    uv run python tools/check_voice.py

CI runs this (it needs the network; GITHUB_TOKEN raises the API limit). The
sherpa-onnx releases list each file with its size and SHA-256 digest; on a
mismatch the published values are printed, so that an update is a copy.
"""

from __future__ import annotations

import os
import sys

import httpx

from gloxi.voice.models import PACKS, RELEASES

API = "https://api.github.com/repos/k2-fsa/sherpa-onnx/releases"


def assets(client: httpx.Client, tag: str) -> dict[str, dict]:
    release = client.get(f"{API}/tags/{tag}")
    release.raise_for_status()
    found: dict[str, dict] = {}
    url = f"{API}/{release.json()['id']}/assets?per_page=100"
    while url:
        page = client.get(url)
        page.raise_for_status()
        found.update({a["name"]: a for a in page.json()})
        url = page.links.get("next", {}).get("url")
    return found


def main() -> int:
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    failed = 0
    published: dict[str, dict[str, dict]] = {}
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        for pack in PACKS:
            tag, name = pack.file.url.removeprefix(RELEASES + "/").split("/", 1)
            if tag not in published:
                published[tag] = assets(client, tag)
            asset = published[tag].get(name)
            if asset is None:
                print(f"{name}: not in the {tag} release", file=sys.stderr)
                failed = 1
                continue
            digest = (asset.get("digest") or "").removeprefix("sha256:")
            if (digest, asset["size"]) != (pack.file.sha256, pack.file.size):
                pinned = f"sha256={pack.file.sha256} size={pack.file.size}"
                print(f"{name}: pinned {pinned}", file=sys.stderr)
                print(f"{name}: published sha256={digest} size={asset['size']}", file=sys.stderr)
                failed = 1
            else:
                print(f"{name}: matches GitHub ({asset['size']} bytes)")
    return failed


if __name__ == "__main__":
    sys.exit(main())
