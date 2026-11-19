# SPDX-License-Identifier: GPL-3.0-or-later
"""Write the files that point people at the Vilna Flatpak repository (ADR 0024).

    python3 tools/flatpak_repo.py --url https://example.org/repo/ --key key.gpg --out out/

The repository itself (OSTree) is built in CI from the very bundle that the
checks ran on (.github/workflows/flatpak.yml). This tool writes the two small
files beside it:

- ``vilna.flatpakrepo`` adds the Vilna repository as a source in GNOME
  Software or with ``flatpak remote-add``;
- ``io.github.stdenis1980.Gloxi.flatpakref`` installs Gloxi from it in one
  click, and pulls the GNOME runtime from Flathub.

Both carry the repository's public signing key, so Flatpak checks every
download against it. ``--key`` is the binary key that ``gpg --export``
writes. Only https:// addresses are accepted; ``--test`` also allows file://,
for the install check in CI. Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

APP_ID = "io.github.stdenis1980.Gloxi"
REMOTE = "vilna"
HOMEPAGE = "https://vilnaos.com/gloxi/"
# The runtime (org.gnome.Platform) is not in our repository; it comes from Flathub.
RUNTIME_REPO = "https://dl.flathub.org/repo/flathub.flatpakrepo"
# An OpenPGP key that ``gpg --export`` writes starts with a public-key packet:
# old format tag 6 (0x98, 0x99, 0x9a) or new format tag 6 (0xc6).
KEY_PACKET_TAGS = {0x98, 0x99, 0x9A, 0xC6}
MAX_KEY_BYTES = 64 * 1024


class RepoError(Exception):
    pass


def check_url(url: str, *, test: bool = False) -> str:
    schemes = ("https://", "file://") if test else ("https://",)
    if not url.startswith(schemes):
        raise RepoError(f"the repository address must start with {' or '.join(schemes)}: {url!r}")
    if any(c.isspace() for c in url):
        raise RepoError(f"the repository address has a space or line break: {url!r}")
    return url if url.endswith("/") else url + "/"


def read_key(path: Path) -> str:
    """The public key as Flatpak wants it in GPGKey=: base64 of the binary key."""
    data = path.read_bytes()
    if data.lstrip().startswith(b"-----BEGIN"):
        raise RepoError(
            f"{path} is an ASCII-armored key; export it without --armor "
            "(gpg --export KEYID > key.gpg)"
        )
    if b"PRIVATE KEY" in data or (data and data[0] in {0x94, 0x95, 0x96, 0xC5}):
        raise RepoError(f"{path} holds a secret key; only the public key goes into these files")
    if not data or data[0] not in KEY_PACKET_TAGS:
        raise RepoError(f"{path} is not an OpenPGP public key (gpg --export KEYID > key.gpg)")
    if len(data) > MAX_KEY_BYTES:
        raise RepoError(f"{path} is {len(data)} bytes; export only the signing key")
    return base64.b64encode(data).decode("ascii")


def flatpakrepo(url: str, key: str) -> str:
    return "\n".join(
        [
            "[Flatpak Repo]",
            "Title=Vilna",
            f"Url={url}",
            f"Homepage={HOMEPAGE}",
            "Comment=Apps made by Vilna OS",
            "Description=Gloxi and other apps of Vilna OS for any Linux with Flatpak",
            f"GPGKey={key}",
            "",
        ]
    )


def flatpakref(url: str, key: str, branch: str = "master") -> str:
    return "\n".join(
        [
            "[Flatpak Ref]",
            f"Name={APP_ID}",
            f"Branch={branch}",
            "Title=Gloxi",
            f"Url={url}",
            f"SuggestRemoteName={REMOTE}",
            f"Homepage={HOMEPAGE}",
            "Comment=AI assistant that acts only with your permission",
            "IsRuntime=false",
            f"RuntimeRepo={RUNTIME_REPO}",
            f"GPGKey={key}",
            "",
        ]
    )


def write(out: Path, url: str, key_path: Path, *, branch: str = "master", test: bool = False):
    url = check_url(url, test=test)
    if not branch or not all(c.isalnum() or c in "._-" for c in branch):
        raise RepoError(f"not a Flatpak branch name: {branch!r}")
    key = read_key(key_path)
    out.mkdir(parents=True, exist_ok=True)
    repo = out / f"{REMOTE}.flatpakrepo"
    ref = out / f"{APP_ID}.flatpakref"
    repo.write_text(flatpakrepo(url, key), encoding="utf-8")
    ref.write_text(flatpakref(url, key, branch), encoding="utf-8")
    return repo, ref


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="where the OSTree repository is served")
    parser.add_argument("--key", required=True, type=Path, help="public key from gpg --export")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--branch", default="master")
    parser.add_argument("--test", action="store_true", help="also allow a file:// address")
    args = parser.parse_args(argv)
    try:
        for path in write(args.out, args.url, args.key, branch=args.branch, test=args.test):
            print(path)
    except (RepoError, OSError) as e:
        print(f"flatpak_repo: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
