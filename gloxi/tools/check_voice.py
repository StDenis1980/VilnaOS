# SPDX-License-Identifier: GPL-3.0-or-later
"""Check that the voice models pinned in gloxi.voice.models are what GitHub and PyPI serve.

    uv run python tools/check_voice.py

CI runs this (it needs the network; GITHUB_TOKEN raises the API limit). The
sherpa-onnx releases list each file with its size and SHA-256 digest, and so
does PyPI's JSON for a wheel; on a mismatch the published values are printed,
so that an update is a copy.
"""

from __future__ import annotations

import os
import sys

import httpx

from gloxi.voice.models import PACKS, PYPI, RELEASES

API = "https://api.github.com/repos/k2-fsa/sherpa-onnx/releases"
PYPI_JSON = "https://pypi.org/pypi/{project}/{version}/json"


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


def pypi_file(client: httpx.Client, filename: str) -> dict | None:
    """PyPI's entry for a wheel, as {"digest": "sha256:…", "size": n}."""
    project, version = filename.split("-")[:2]
    page = client.get(PYPI_JSON.format(project=project, version=version))
    page.raise_for_status()
    for entry in page.json()["urls"]:
        if entry["filename"] == filename:
            return {"digest": "sha256:" + entry["digests"]["sha256"], "size": entry["size"]}
    return None


def main() -> int:
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    failed = 0
    published: dict[str, dict[str, dict]] = {}
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        for pack in PACKS:
            if pack.file.url.startswith(PYPI + "/"):
                name, where = pack.file.filename, "PyPI"
                asset = pypi_file(client, name)
            else:
                tag, name = pack.file.url.removeprefix(RELEASES + "/").split("/", 1)
                where = f"the {tag} release"
                if tag not in published:
                    published[tag] = assets(client, tag)
                asset = published[tag].get(name)
            if asset is None:
                print(f"{name}: not in {where}", file=sys.stderr)
                failed = 1
                continue
            digest = (asset.get("digest") or "").removeprefix("sha256:")
            if (digest, asset["size"]) != (pack.file.sha256, pack.file.size):
                pinned = f"sha256={pack.file.sha256} size={pack.file.size}"
                print(f"{name}: pinned {pinned}", file=sys.stderr)
                print(f"{name}: published sha256={digest} size={asset['size']}", file=sys.stderr)
                failed = 1
            else:
                print(f"{name}: matches {where} ({asset['size']} bytes)")
    return failed


if __name__ == "__main__":
    sys.exit(main())
