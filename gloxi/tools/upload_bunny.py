# SPDX-License-Identifier: GPL-3.0-or-later
"""Upload the Vilna Flatpak repository to a bunny.net storage zone (ADR 0024).

    BUNNY_STORAGE_PASSWORD=… python3 tools/upload_bunny.py vilna-flatpak \\
        --zone vilna-flatpak --endpoint storage.bunnycdn.com

Every file under the folder goes to the same path in the storage zone, with
the zone's own password (not the account API key), over the Edge Storage
API. The order matters for people who update while we upload: first the
objects and deltas, then the refs and the rest, and only at the very end
``summary.sig`` and ``summary``, which tell Flatpak what is new. So a client
never sees a summary that points at objects that are not there yet. Old
objects stay on the server; that does no harm, Flatpak only follows the
summary. Stdlib only, so it runs in CI as it is.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

# Written last, in this order: Flatpak reads the summary and checks it with its .sig.
LAST = ("summary.sig", "summary")
FIRST_DIRS = ("objects", "deltas")
MAX_FILES = 200_000
TIMEOUT = 120
RETRIES = 3


class UploadError(Exception):
    pass


def ordered_files(root: Path) -> list[Path]:
    """Relative paths, in the order they must be uploaded."""
    if not root.is_dir():
        raise UploadError(f"{root} is not a folder")
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise UploadError(f"{path} is a symbolic link; the repository has none")
        if path.is_file():
            files.append(path.relative_to(root))
            if len(files) > MAX_FILES:
                raise UploadError(f"more than {MAX_FILES} files under {root}")
    if not any(f.name == "summary" for f in files):
        raise UploadError(f"{root} has no summary: not a Flatpak repository")

    def rank(rel: Path) -> tuple[int, str]:
        name = rel.as_posix()
        if rel.name in LAST:
            return (2 + LAST.index(rel.name), name)
        if any(part in FIRST_DIRS for part in rel.parts[:-1]):
            return (0, name)
        return (1, name)

    return sorted(files, key=rank)


def check_name(value: str, what: str) -> str:
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-.")
    if not value or not set(value.lower()) <= allowed or value.startswith((".", "-")):
        raise UploadError(f"not a bunny.net {what}: {value!r}")
    return value


def url_for(endpoint: str, zone: str, rel: Path) -> str:
    path = "/".join(urllib.parse.quote(part) for part in rel.parts)
    return f"https://{endpoint}/{zone}/{path}"


Send = Callable[[urllib.request.Request], int]


def _send(request: urllib.request.Request) -> int:
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.status


def upload(
    root: Path,
    zone: str,
    endpoint: str,
    password: str,
    *,
    send: Send = _send,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> int:
    zone = check_name(zone, "storage zone name")
    endpoint = check_name(endpoint, "storage endpoint")
    if not password:
        raise UploadError("no storage zone password (BUNNY_STORAGE_PASSWORD)")
    files = ordered_files(root)
    for rel in files:
        data = (root / rel).read_bytes()
        request = urllib.request.Request(
            url_for(endpoint, zone, rel),
            data=data,
            method="PUT",
            headers={
                "AccessKey": password,
                "Content-Type": "application/octet-stream",
                "Checksum": hashlib.sha256(data).hexdigest().upper(),
            },
        )
        for attempt in range(1, RETRIES + 1):
            try:
                status = send(request)
            except (urllib.error.URLError, OSError) as e:
                # Never print the request: its headers hold the password.
                status, reason = None, getattr(e, "code", None) or type(e).__name__
            else:
                reason = status
            if status in (200, 201):
                break
            if attempt == RETRIES:
                raise UploadError(f"{rel.as_posix()}: upload failed ({reason})")
            sleep(2**attempt)
    log(f"uploaded {len(files)} files to {zone}, summary last")
    return len(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "folder", type=Path, help="the folder to upload (repo/ and the .flatpakref)"
    )
    parser.add_argument("--zone", required=True, help="storage zone name")
    parser.add_argument("--endpoint", default="storage.bunnycdn.com", help="storage region host")
    args = parser.parse_args(argv)
    try:
        upload(args.folder, args.zone, args.endpoint, os.environ.get("BUNNY_STORAGE_PASSWORD", ""))
    except (UploadError, OSError) as e:
        print(f"upload_bunny: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
