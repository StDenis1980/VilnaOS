#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fetch what a package's _service downloads on OBS, for building it in CI.

    fetch_sources.py <assembled dir> [--cache <dir>]

OBS runs ``download_url`` itself and ``verify_file`` checks the result. CI
has no OBS, so this does the same for every package with a ``_service``:
it downloads each file (or takes it from the cache) and refuses it unless
the SHA-256 that ``verify_file`` names matches. Only these two services
are understood; anything else is an error, so a spec cannot silently miss
a source.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

PREFIX = "_service:download_url:"


def services(path: Path) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """(url, filename) of each download, and the SHA-256 of each file."""
    downloads, sums = [], {}
    for service in ET.parse(path).getroot().findall("service"):
        params = {p.get("name"): (p.text or "").strip() for p in service.findall("param")}
        name = service.get("name")
        if name == "download_url":
            if params.get("protocol", "https") != "https":
                raise SystemExit(f"{path}: only https downloads")
            filename = params.get("filename") or Path(params["path"]).name
            downloads.append((f"https://{params['host']}{params['path']}", filename))
        elif name == "verify_file":
            if params.get("verifier") != "sha256":
                raise SystemExit(f"{path}: verify_file must use sha256")
            sums[params["file"].removeprefix(PREFIX)] = params["checksum"].lower()
        else:
            raise SystemExit(f"{path}: service {name} is not supported in CI")
    for _url, filename in downloads:
        if filename not in sums:
            raise SystemExit(f"{path}: {filename} has no verify_file")
    return downloads, sums


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(url: str, dest: Path, want: str, cache: Path | None) -> None:
    cached = cache / want if cache else None
    if cached and cached.is_file() and sha256(cached) == want:
        shutil.copyfile(cached, dest)
        print(f"{dest.name}: from the cache")
        return
    part = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url, timeout=60) as r, part.open("wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    got = sha256(part)
    if got != want:
        part.unlink()
        raise SystemExit(f"{dest.name}: SHA-256 {got}, expected {want}")
    part.replace(dest)
    print(f"{dest.name}: downloaded, SHA-256 matches")
    if cached:
        cache.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dest, cached)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("assembled", type=Path)
    p.add_argument("--cache", type=Path)
    args = p.parse_args(argv)
    for service in sorted(args.assembled.glob("*/_service")):
        downloads, sums = services(service)
        for url, filename in downloads:
            fetch(url, service.parent / filename, sums[filename], args.cache)
    return 0


if __name__ == "__main__":
    sys.exit(main())
