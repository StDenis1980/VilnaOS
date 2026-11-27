#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pins of Anthropic's Claude desktop .deb for the extra-data Flatpak.

    python3 tools/claude_desktop.py check            # the pins are well formed (no network)
    python3 tools/claude_desktop.py latest           # compare the pins with Anthropic's apt index
    python3 tools/claude_desktop.py latest --write   # move the pins to the newest version

thirdparty/claude-desktop/io.github.stdenis1980.ClaudeDesktop.yml does not
carry the app: Flatpak downloads Anthropic's .deb on the person's computer at
install time and checks it against the size and SHA-256 pinned there. Those
pins come from Anthropic's apt repository, the same place `apt install
claude-desktop` uses: ``latest`` checks the signature of its InRelease file
with Anthropic's key (fingerprint pinned below), checks each Packages index
against InRelease and reads the newest claude-desktop from it. So the pins
are exactly what a Debian or Ubuntu computer would accept. Stdlib and gpg.
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import lzma
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "thirdparty" / "claude-desktop"
APP_ID = "io.github.stdenis1980.ClaudeDesktop"
MANIFEST = APP / f"{APP_ID}.yml"
METAINFO = APP / f"{APP_ID}.metainfo.xml"

REPO = "https://downloads.claude.ai/claude-desktop/apt/stable"
KEY_URL = "https://downloads.claude.ai/claude-desktop/key.asc"
SUITE = "stable"
COMPONENT = "main"
PACKAGE = "claude-desktop"
# Names in messages; tools/devin_desktop.py reuses this module for another
# vendor's signed apt repository and sets its own.
NAME = "claude-desktop"
VENDOR = "Anthropic"
TOOL = "tools/claude_desktop.py"
# How a .deb's file name carries its version and architecture: Debian's
# <package>_<version>_<arch>.deb unless a vendor names its files otherwise.
DEB_NAME = r"/{package}_(?P<version>[^_/]+)_(?P<arch>[^_/]+)\.deb$"
# Debian architecture -> its spelling in the file name, where that differs.
FILE_ARCH: dict[str, str] = {}
# Anthropic's apt signing key, as key.asc on downloads.claude.ai served it on
# 2026-11-25 (first seen by CI, PR #233). Anthropic publishes no fingerprint
# elsewhere, so this is trust on first use, like `apt` itself; from now on a
# different key is an error, not a quiet change.
KEY_FINGERPRINT = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"
# Flatpak architecture -> Debian architecture.
ARCHES = {"x86_64": "amd64", "aarch64": "arm64"}

HEX64 = re.compile(r"[0-9a-f]{64}")


class PinError(Exception):
    pass


@dataclass(frozen=True)
class Pin:
    arch: str  # Flatpak architecture
    url: str
    sha256: str
    size: int

    @property
    def version(self) -> str:
        m = re.search(DEB_NAME.format(package=re.escape(PACKAGE)), self.url)
        return m["version"] if m else ""

    @property
    def deb_arch(self) -> str:
        m = re.search(DEB_NAME.format(package=re.escape(PACKAGE)), self.url)
        return m["arch"] if m else ""


# --- the manifest -----------------------------------------------------------

EXTRA = re.compile(
    r"- type: extra-data\n"
    r"(?P<indent>[ ]+)filename: (?P<filename>\S+)\n"
    r"(?P=indent)only-arches: \[(?P<arch>[\w-]+)\]\n"
    r"(?P=indent)url: (?P<url>\S+)\n"
    r"(?P=indent)sha256: \"(?P<sha256>[^\"]*)\"\n"
    r"(?P=indent)size: (?P<size>\d+)\n"
)


def read_pins(text: str) -> dict[str, Pin]:
    pins: dict[str, Pin] = {}
    for m in EXTRA.finditer(text):
        arch = m["arch"]
        if arch in pins:
            raise PinError(f"two extra-data sources for {arch}")
        pins[arch] = Pin(arch, m["url"], m["sha256"], int(m["size"]))
    if text.count("type: extra-data") != len(pins):
        raise PinError("an extra-data source is not in the expected form (filename, only-arches, url, sha256, size)")
    return pins


def check_pins(pins: dict[str, Pin]) -> list[str]:
    problems = []
    if set(pins) != set(ARCHES):
        problems.append(f"extra-data for {sorted(pins)}, expected {sorted(ARCHES)}")
    for arch, pin in sorted(pins.items()):
        deb_arch = ARCHES.get(arch, "?")
        if not pin.url.startswith(f"{REPO}/pool/"):
            problems.append(f"{arch}: {pin.url} is not in {VENDOR}'s apt repository {REPO}")
        if pin.deb_arch != FILE_ARCH.get(deb_arch, deb_arch):
            problems.append(f"{arch}: {pin.url} is not the {deb_arch} package")
        if not HEX64.fullmatch(pin.sha256) or pin.sha256 == "0" * 64:
            problems.append(f"{arch}: no SHA-256 pinned")
        if pin.size <= 0:
            problems.append(f"{arch}: no size pinned")
        if pin.version in ("", "0.0.0"):
            problems.append(f"{arch}: no version pinned")
    versions = {p.version for p in pins.values()}
    if len(versions) > 1:
        problems.append(f"the architectures pin different versions: {sorted(versions)}")
    return problems


def metainfo_version(text: str) -> str:
    m = re.search(r'<release version="([^"]+)"', text)
    return m.group(1) if m else ""


def write_pins(manifest: str, metainfo: str, new: dict[str, Pin], today: datetime.date) -> tuple[str, str]:
    def repl(m: re.Match[str]) -> str:
        pin = new[m["arch"]]
        i = m["indent"]
        return (
            f"- type: extra-data\n{i}filename: {m['filename']}\n{i}only-arches: [{pin.arch}]\n"
            f"{i}url: {pin.url}\n{i}sha256: \"{pin.sha256}\"\n{i}size: {pin.size}\n"
        )

    manifest = EXTRA.sub(repl, manifest)
    version = next(iter(new.values())).version
    if metainfo_version(metainfo) != version:
        metainfo = re.sub(
            r"(\n(\s*)<releases>\n)",
            lambda m: f'{m[1]}{m[2]}  <release version="{version}" date="{today.isoformat()}"/>\n',
            metainfo,
            count=1,
        )
        # The placeholder release of the first pin is not a real version.
        metainfo = re.sub(r'\n\s*<release version="0\.0\.0" date="[^"]*"/>', "", metainfo)
    return manifest, metainfo


# --- Anthropic's apt repository ---------------------------------------------


def parse_stanzas(text: str) -> list[dict[str, str]]:
    stanzas = []
    for block in re.split(r"\n\s*\n", text.strip()):
        fields: dict[str, str] = {}
        key = None
        for line in block.splitlines():
            if line[:1] in (" ", "\t") and key:
                fields[key] += "\n" + line.strip()
            elif ":" in line:
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        if fields:
            stanzas.append(fields)
    return stanzas


def release_hashes(inrelease: str) -> dict[str, tuple[str, int]]:
    """path -> (sha256, size) from the SHA256 field of a Release file."""
    fields = parse_stanzas(inrelease)[0] if inrelease.strip() else {}
    out = {}
    for line in fields.get("SHA256", "").splitlines():
        parts = line.split()
        if len(parts) == 3 and HEX64.fullmatch(parts[0]):
            out[parts[2]] = (parts[0], int(parts[1]))
    return out


def _order(c: str) -> int:
    if c == "~":
        return -1
    if c.isalpha():
        return ord(c)
    return ord(c) + 256


def _compare_part(a: str, b: str) -> int:
    while a or b:
        na = re.match(r"\D*", a)[0]
        nb = re.match(r"\D*", b)[0]
        a, b = a[len(na):], b[len(nb):]
        for i in range(max(len(na), len(nb))):
            ca = _order(na[i]) if i < len(na) else 0
            cb = _order(nb[i]) if i < len(nb) else 0
            if ca != cb:
                return ca - cb
        da = re.match(r"\d*", a)[0]
        db = re.match(r"\d*", b)[0]
        a, b = a[len(da):], b[len(db):]
        if int(da or 0) != int(db or 0):
            return int(da or 0) - int(db or 0)
    return 0


def compare_versions(a: str, b: str) -> int:
    """Debian version order (epoch:upstream-revision), as dpkg compares."""

    def split(v: str) -> tuple[int, str, str]:
        epoch, _, rest = v.rpartition(":") if ":" in v else ("0", "", v)
        upstream, _, revision = rest.rpartition("-") if "-" in rest else (rest, "", "")
        return int(epoch or 0), upstream, revision

    ea, ua, ra = split(a)
    eb, ub, rb = split(b)
    if ea != eb:
        return ea - eb
    return _compare_part(ua, ub) or _compare_part(ra, rb)


def newest(packages: str, deb_arch: str) -> dict[str, str]:
    found = [
        s for s in parse_stanzas(packages)
        if s.get("Package") == PACKAGE and s.get("Architecture") == deb_arch
    ]
    if not found:
        raise PinError(f"no {PACKAGE} for {deb_arch} in the index")
    best = found[0]
    for s in found[1:]:
        if compare_versions(s["Version"], best["Version"]) > 0:
            best = s
    for field in ("Filename", "Size", "SHA256", "Version"):
        if field not in best:
            raise PinError(f"{PACKAGE} {best.get('Version')} for {deb_arch} has no {field}")
    if not HEX64.fullmatch(best["SHA256"]) or not best["Filename"].startswith("pool/"):
        raise PinError(f"{PACKAGE} {best['Version']} for {deb_arch}: unexpected Filename or SHA256")
    return best


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"vilna-{NAME}-pins"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def verified_inrelease() -> str:
    key = fetch(KEY_URL)
    data = fetch(f"{REPO}/dists/{SUITE}/InRelease")
    with tempfile.TemporaryDirectory() as home:
        env = dict(os.environ, GNUPGHOME=home)
        status_file = Path(home) / "status"
        subprocess.run(["gpg", "--batch", "--quiet", "--import"], input=key, env=env, check=True)
        out = subprocess.run(
            ["gpg", "--batch", "--status-file", str(status_file), "--decrypt"],
            input=data, env=env, capture_output=True,
        )
        status = status_file.read_text(encoding="utf-8", errors="replace") if status_file.exists() else ""
    if out.returncode != 0:
        raise PinError("InRelease signature does not verify:\n" + out.stderr.decode("utf-8", "replace"))
    # VALIDSIG <signing key> ... <primary key>: either may be the one pinned.
    sigs = []
    for line in status.splitlines():
        parts = line.split()
        if parts[:2] == ["[GNUPG:]", "VALIDSIG"]:
            sigs += [parts[-1], parts[2]] if len(parts) > 3 else parts[2:]
    if not sigs:
        raise PinError("InRelease has no valid signature")
    if not KEY_FINGERPRINT:
        raise PinError(
            f"InRelease is signed by {sigs[0]}; pin it as KEY_FINGERPRINT in {TOOL} "
            f"after checking it against {VENDOR}'s published key"
        )
    if KEY_FINGERPRINT not in sigs:
        raise PinError(f"InRelease is signed by {sigs}, not {VENDOR}'s key {KEY_FINGERPRINT}")
    # Only what the signature covers: gpg prints the signed text on stdout.
    return out.stdout.decode("utf-8")


def packages_index(hashes: dict[str, tuple[str, int]], deb_arch: str) -> str:
    base = f"{COMPONENT}/binary-{deb_arch}/Packages"
    for suffix, unpack in (("", lambda b: b), (".xz", lzma.decompress), (".gz", gzip.decompress)):
        path = base + suffix
        if path not in hashes:
            continue
        raw = fetch(f"{REPO}/dists/{SUITE}/{path}")
        sha, size = hashes[path]
        if hashlib.sha256(raw).hexdigest() != sha or len(raw) != size:
            raise PinError(f"{path} does not match InRelease")
        return unpack(raw).decode("utf-8")
    raise PinError(f"InRelease lists no {base}")


def latest_pins() -> dict[str, Pin]:
    hashes = release_hashes(verified_inrelease())
    pins = {}
    for arch, deb_arch in ARCHES.items():
        p = newest(packages_index(hashes, deb_arch), deb_arch)
        pins[arch] = Pin(arch, f"{REPO}/{p['Filename']}", p["SHA256"], int(p["Size"]))
    versions = {p.version for p in pins.values()}
    if len(versions) != 1:
        raise PinError(f"the newest versions differ between architectures: {sorted(versions)}")
    return pins


# --- commands ---------------------------------------------------------------


def cmd_check() -> int:
    manifest = MANIFEST.read_text(encoding="utf-8")
    pins = read_pins(manifest)
    problems = check_pins(pins)
    meta = metainfo_version(METAINFO.read_text(encoding="utf-8"))
    versions = {p.version for p in pins.values()}
    if versions and meta not in versions:
        problems.append(f"metainfo's newest release is {meta!r}, the pins are {sorted(versions)}")
    for p in problems:
        print(f"{NAME}: {p}", file=sys.stderr)
    if not problems:
        print(f"{NAME} {meta}: pins ok")
    return 1 if problems else 0


def cmd_latest(write: bool) -> int:
    manifest = MANIFEST.read_text(encoding="utf-8")
    current = read_pins(manifest)
    new = latest_pins()
    for arch, pin in sorted(new.items()):
        print(f"{arch}: {pin.version} {pin.size} {pin.sha256} {pin.url}")
    if new == current:
        print("the pins are the newest version")
        return 0
    if write:
        m, meta = write_pins(manifest, METAINFO.read_text(encoding="utf-8"), new, datetime.date.today())
        MANIFEST.write_text(m, encoding="utf-8")
        METAINFO.write_text(meta, encoding="utf-8")
        print("pins moved; review the diff, then commit")
        return 0
    print("the pins are not the newest version (run with --write)", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    latest = sub.add_parser("latest")
    latest.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "check":
            return cmd_check()
        return cmd_latest(args.write)
    except PinError as e:
        print(f"{NAME}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
