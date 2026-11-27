#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pins of the Cursor and Grok Bot .deb packages for their extra-data Flatpaks.

    python3 tools/cursor_apps.py check [APP]          # the pins are well formed (no network)
    python3 tools/cursor_apps.py latest [APP]         # compare the pins with the newest release
    python3 tools/cursor_apps.py latest --write [APP] # move the pins to the newest release
    python3 tools/cursor_apps.py fetch APP ARCH DIR   # download the pinned .deb and check it

APP is ``cursor`` or ``grok-bot`` (both when left out). Both apps come from
Anysphere, the maker of Cursor, which also builds and serves xAI's Grok Bot:
the same update service (api2.cursor.sh) names the newest release, and the
packages live on downloads.cursor.com.

thirdparty/<app>/<id>.yml does not carry the app: Flatpak downloads the .deb
on the person's computer at install time and checks it against the size and
SHA-256 pinned there. ``latest`` takes the newest version and .deb address
from the update service the official download pages use. It has no hashes,
so ``latest`` downloads each .deb over HTTPS and hashes it; where Anysphere's
signed apt repository lists the same package (Cursor, amd64), the hash must
also agree with that index, checked with Anysphere's key (fingerprint pinned
below). Stdlib and gpg.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_desktop as cd  # noqa: E402  (shared apt and manifest helpers)

ROOT = Path(__file__).resolve().parent.parent
API = "https://api2.cursor.sh/updates/api/download/stable"
DOWNLOADS = "https://downloads.cursor.com/"
APT_REPO = "https://downloads.cursor.com/aptrepo"
APT_KEY_URL = "https://downloads.cursor.com/keys/anysphere.asc"
# Anysphere's apt signing key. The fingerprint is the one install guides for
# Cursor's apt repository check against (2026-11-27); CI checks it against
# the served key and the InRelease signature, and a different key is an error.
APT_KEY_FINGERPRINT = "380FF4BCDC34A4BD92A3565342A1772E62E492D6"
# Flatpak architecture -> (update-service platform, Debian architecture).
ARCHES = {"x86_64": ("linux-x64", "amd64"), "aarch64": ("linux-arm64", "arm64")}

PinError = cd.PinError


@dataclass(frozen=True)
class App:
    key: str
    app_id: str
    api_name: str  # the app's name in the update service
    package: str  # the Debian package name

    @property
    def dir(self) -> Path:
        return ROOT / "thirdparty" / self.key

    @property
    def manifest(self) -> Path:
        return self.dir / f"{self.app_id}.yml"

    @property
    def metainfo(self) -> Path:
        return self.dir / f"{self.app_id}.metainfo.xml"


APPS = {
    "cursor": App("cursor", "io.github.stdenis1980.Cursor", "cursor", "cursor"),
    # "sand" is Grok Bot's name in the update service (x.ai/bot downloads).
    "grok-bot": App("grok-bot", "io.github.stdenis1980.GrokBot", "sand", "grok-bot"),
}


@dataclass(frozen=True)
class Pin:
    arch: str
    url: str
    sha256: str
    size: int


def deb_version(app: App, url: str) -> str:
    m = re.search(rf"/{re.escape(app.package)}_([^_/]+)_[^_/]+\.deb$", url)
    return m.group(1) if m else ""


def read_pins(text: str) -> dict[str, Pin]:
    return {a: Pin(p.arch, p.url, p.sha256, p.size) for a, p in cd.read_pins(text).items()}


def check_pins(app: App, pins: dict[str, Pin]) -> list[str]:
    problems = []
    if set(pins) != set(ARCHES):
        problems.append(f"extra-data for {sorted(pins)}, expected {sorted(ARCHES)}")
    for arch, pin in sorted(pins.items()):
        deb_arch = ARCHES.get(arch, ("", "?"))[1]
        if not pin.url.startswith(DOWNLOADS):
            problems.append(f"{arch}: {pin.url} is not on {DOWNLOADS}")
        if not pin.url.endswith(f"_{deb_arch}.deb"):
            problems.append(f"{arch}: {pin.url} is not the {deb_arch} package")
        if not cd.HEX64.fullmatch(pin.sha256) or pin.sha256 == "0" * 64:
            problems.append(f"{arch}: no SHA-256 pinned")
        if pin.size <= 0:
            problems.append(f"{arch}: no size pinned")
        if deb_version(app, pin.url) in ("", "0.0.0"):
            problems.append(f"{arch}: no version pinned")
    versions = {deb_version(app, p.url) for p in pins.values()}
    if len(versions) > 1:
        problems.append(f"the architectures pin different versions: {sorted(versions)}")
    return problems


def write_pins(app: App, manifest: str, metainfo: str, new: dict[str, Pin], today: datetime.date) -> tuple[str, str]:
    as_cd = {a: cd.Pin(p.arch, p.url, p.sha256, p.size) for a, p in new.items()}
    # cd.write_pins takes the version from Claude's file names; give it ours.
    version = deb_version(app, next(iter(new.values())).url)
    manifest, _ = cd.write_pins(manifest, "", as_cd, today)
    if cd.metainfo_version(metainfo) != version:
        metainfo = re.sub(
            r"(\n(\s*)<releases>\n)",
            lambda m: f'{m[1]}{m[2]}  <release version="{version}" date="{today.isoformat()}"/>\n',
            metainfo,
            count=1,
        )
        metainfo = re.sub(r'\n\s*<release version="0\.0\.0" date="[^"]*"/>', "", metainfo)
    return manifest, metainfo


# --- network ----------------------------------------------------------------


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "vilna-flatpak-pins"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def release_info(app: App, platform: str) -> dict:
    info = json.loads(fetch(f"{API}/{platform}/{app.api_name}"))
    if not isinstance(info, dict) or not str(info.get("debUrl", "")).startswith(DOWNLOADS):
        raise PinError(f"{app.key} {platform}: the update service gave no .deb on {DOWNLOADS}: {info!r}")
    return info


def apt_hashes() -> dict[tuple[str, str], dict[str, str]]:
    """(package, deb arch) -> newest stanza of Anysphere's signed apt index."""
    saved = (cd.REPO, cd.KEY_URL, cd.KEY_FINGERPRINT)
    cd.REPO, cd.KEY_URL, cd.KEY_FINGERPRINT = APT_REPO, APT_KEY_URL, APT_KEY_FINGERPRINT
    try:
        hashes = cd.release_hashes(cd.verified_inrelease())
        out: dict[tuple[str, str], dict[str, str]] = {}
        for _, deb_arch in ARCHES.values():
            try:
                text = cd.packages_index(hashes, deb_arch)
            except PinError:
                continue  # the repository has amd64 only
            for s in cd.parse_stanzas(text):
                k = (s.get("Package", ""), s.get("Architecture", ""))
                if k[0] and (k not in out or cd.compare_versions(s.get("Version", ""), out[k].get("Version", "")) > 0):
                    out[k] = s
        return out
    finally:
        cd.REPO, cd.KEY_URL, cd.KEY_FINGERPRINT = saved


def latest_pins(app: App, apt: dict[tuple[str, str], dict[str, str]]) -> dict[str, Pin]:
    pins = {}
    for arch, (platform, deb_arch) in ARCHES.items():
        url = release_info(app, platform)["debUrl"]
        data = fetch(url)
        sha = hashlib.sha256(data).hexdigest()
        pin = Pin(arch, url, sha, len(data))
        listed = apt.get((app.package, deb_arch))
        version = deb_version(app, url)
        if listed and listed.get("Version", "").split(":")[-1] == version:
            if listed.get("SHA256") != sha or int(listed.get("Size", "0")) != len(data):
                raise PinError(f"{app.key} {arch}: {url} does not match Anysphere's signed apt index")
            print(f"{app.key} {arch}: {version} agrees with the signed apt index")
        else:
            has = f" (it has {listed.get('Version')})" if listed else ""
            print(f"{app.key} {arch}: {version} is not in the signed apt index{has}; hash taken over HTTPS")
        pins[arch] = pin
    versions = {deb_version(app, p.url) for p in pins.values()}
    if len(versions) != 1:
        raise PinError(f"{app.key}: the newest versions differ between architectures: {sorted(versions)}")
    return pins


# --- commands ---------------------------------------------------------------


def cmd_check(app: App) -> int:
    pins = read_pins(app.manifest.read_text(encoding="utf-8"))
    problems = check_pins(app, pins)
    meta = cd.metainfo_version(app.metainfo.read_text(encoding="utf-8"))
    versions = {deb_version(app, p.url) for p in pins.values()}
    if versions and meta not in versions:
        problems.append(f"metainfo's newest release is {meta!r}, the pins are {sorted(versions)}")
    for p in problems:
        print(f"{app.key}: {p}", file=sys.stderr)
    if not problems:
        print(f"{app.key} {meta}: pins ok")
    return 1 if problems else 0


def cmd_latest(app: App, write: bool, apt: dict[tuple[str, str], dict[str, str]]) -> int:
    manifest = app.manifest.read_text(encoding="utf-8")
    current = read_pins(manifest)
    new = latest_pins(app, apt)
    for arch, pin in sorted(new.items()):
        print(f"{app.key} {arch}: {deb_version(app, pin.url)} {pin.size} {pin.sha256} {pin.url}")
    if new == current:
        print(f"{app.key}: the pins are the newest version")
        return 0
    if write:
        m, meta = write_pins(app, manifest, app.metainfo.read_text(encoding="utf-8"), new, datetime.date.today())
        app.manifest.write_text(m, encoding="utf-8")
        app.metainfo.write_text(meta, encoding="utf-8")
        print(f"{app.key}: pins moved; review the diff, then commit")
        return 0
    print(f"{app.key}: the pins are not the newest version (run with --write)", file=sys.stderr)
    return 1


def cmd_fetch(app: App, arch: str, out: Path) -> int:
    pin = read_pins(app.manifest.read_text(encoding="utf-8"))[arch]
    data = fetch(pin.url)
    if hashlib.sha256(data).hexdigest() != pin.sha256 or len(data) != pin.size:
        raise PinError(f"{app.key} {arch}: {pin.url} does not match the pin")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{app.package}.deb").write_bytes(data)
    print(out / f"{app.package}.deb")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check")
    check.add_argument("app", nargs="?", choices=sorted(APPS))
    latest = sub.add_parser("latest")
    latest.add_argument("--write", action="store_true")
    latest.add_argument("app", nargs="?", choices=sorted(APPS))
    fetch_p = sub.add_parser("fetch")
    fetch_p.add_argument("app", choices=sorted(APPS))
    fetch_p.add_argument("arch", choices=sorted(ARCHES))
    fetch_p.add_argument("dir", type=Path)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "fetch":
            return cmd_fetch(APPS[args.app], args.arch, args.dir)
        apps = [APPS[args.app]] if args.app else list(APPS.values())
        if args.cmd == "check":
            return max(cmd_check(a) for a in apps)
        apt = apt_hashes()
        return max(cmd_latest(a, args.write, apt) for a in apps)
    except (PinError, OSError, ValueError) as e:
        print(f"cursor_apps: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
