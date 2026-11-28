#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the Flatpak defaults of Vilna: lists, remotes, picks and third-party apps.

What is read (nothing else, nothing is run or written):

* ``obs/packages/vilna-flatpak-defaults/``: ``flatpaks.list`` and
  ``flatpaks-vilna.list`` (what first boot installs), ``vilna.flatpakrepo``,
  ``vilna-flatpak-defaults.sh``, the ``.spec`` and ``gnome-mimeapps.list``;
* ``obs/packages/vilna-software-picks/picks.toml``;
* ``thirdparty/*``: Flatpak manifests (``<app id>.yml`` or ``.json``) with their
  ``.desktop`` and AppStream files;
* any other tracked ``*.flatpakrepo`` and ``*.flatpakref``.

What is checked:

* every app id is a Flatpak application id in reverse-DNS form (at least three
  dot-separated elements, ``[A-Za-z0-9_-]``, no element starting with a digit,
  no ``-`` in the last element, at most 255 characters); an app id in a list is
  never an option (``--...``): the first-boot script hands list lines to
  ``flatpak install``, and it deletes all spaces, so two ids on one line would
  be glued into one;
* the branch is ``stable``: a list line with a branch (``id//branch``,
  ``app/id/arch/branch``), a ``Branch=`` or ``DefaultBranch=`` in a ``.flatpakref``
  or ``.flatpakrepo``, ``--branch`` in the script, and a ``branch`` of a pick or
  of a manifest must all say ``stable``; a manifest without ``branch:`` builds
  ``master`` (a LEGACY warning, see below);
* remotes are the documented ones: ``flathub`` (``https://dl.flathub.org/repo/
  flathub.flatpakrepo``) and ``vilna`` (ADR 0024, ``flatpak.vilnaos.com``). A
  list is named after its remote (``flatpaks.list`` is Flathub,
  ``flatpaks-vilna.list`` is ``vilna``); the script's ``install_from`` calls, the
  picks' ``source`` and ``remote``, and every address in the script and in the
  ``.flatpakrepo``/``.flatpakref`` files use only these;
* ``.flatpakrepo`` and ``.flatpakref``: the right group, no repeated key, ``Url``
  is https (no user name, no query), every other address (``Homepage``, ``Icon``,
  ``RuntimeRepo``) is https too, and ``GPGKey`` is there;
* ``GPGKey`` is one line of base64 holding exactly one OpenPGP *public* key:
  well-formed packets from end to end, a version 4 or 6 key whose algorithm can
  sign (RSA of 2048 bits or more, DSA, ECDSA, EdDSA), at least one User ID, and
  no secret key packet; its fingerprint is the one named in the ``.spec``;
* a third-party manifest's id is in the ``io.github.stdenis1980.`` namespace
  and is the file name; its ``.desktop`` and AppStream ``<id>`` match; every
  source address is https with a SHA-256 (or SHA-512), and ``extra-data`` also
  has a ``size``;
* the lists, the picks and the shipped files agree: an app is in one list only,
  a pick with ``default = true`` is in the list of its remote, a ``vilna`` pick
  has our namespace, and the script's lists and ``.flatpakrepo`` are installed by
  the ``.spec``; ``gnome-mimeapps.list`` names only valid ids and, for our own
  apps, only ones that the Vilna list installs.

Errors make the exit code 1. Warnings (``path: warning: ...``) are listed but do
not fail the check; they mark things the repo already does today (LEGACY).

Only ``git ls-files`` is run (to find the files). Python 3.11+, standard library.

Run: python3 tools/check_flatpak_defaults.py [FILE ...]
Without arguments every tracked file of the areas above is checked. With
files, each is classified by its name and cross-checked with the others given.
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

BRANCH = "stable"
OWN_NAMESPACE = "io.github.stdenis1980."
FLATHUB_REPO_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"
# remote name -> the https host its repository file may point to (ADR 0024).
REMOTE_HOSTS = {"flathub": "dl.flathub.org", "vilna": "flatpak.vilnaos.com"}
ALLOWED_HOSTS = frozenset(REMOTE_HOSTS.values())
# Where the picks' source says an app comes from.
SOURCE_REMOTE = {"flathub-verified": "flathub", "flathub-community": "flathub", "vilna": "vilna"}
ARCHES = ("", "x86_64", "aarch64")

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_KEY_BYTES = 64 * 1024
MAX_ID_CHARS = 255

# Flatpak application id: reverse-DNS, >= 3 elements, no digit first, no "-" last.
ID_ELEMENT = r"[A-Za-z_][A-Za-z0-9_-]*"
APP_ID = re.compile(rf"{ID_ELEMENT}(?:\.{ID_ELEMENT}){{2,}}")
MANIFEST_NAME = re.compile(rf"({ID_ELEMENT}(?:\.{ID_ELEMENT}){{2,}})\.(?:ya?ml|json)")
FINGERPRINT = re.compile(r"\b[0-9A-Fa-f]{40}\b")
HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
HEX_SHA512 = re.compile(r"[0-9a-f]{128}")

# Violations the repo already has today. They are reported as warnings, not errors,
# so that the check can be switched on in CI; fix the file, then delete the line.
# Each entry is (path from the repo root, rule).
LEGACY = frozenset(
    {
        # No `branch:` in the manifest, so flatpak-builder builds the ref on master.
        (
            "thirdparty/claude-desktop/io.github.stdenis1980.ClaudeDesktop.yml",
            "branch-missing",
        ),
        ("thirdparty/cursor/io.github.stdenis1980.Cursor.yml", "branch-missing"),
        ("thirdparty/devin-desktop/io.github.stdenis1980.DevinDesktop.yml", "branch-missing"),
        ("thirdparty/grok-bot/io.github.stdenis1980.GrokBot.yml", "branch-missing"),
    }
)

# OpenPGP packet tags (RFC 9580 section 5) a public key export may hold.
TAG_SIGNATURE, TAG_SECRET_KEY, TAG_PUBLIC_KEY = 2, 5, 6
TAG_SECRET_SUBKEY, TAG_USER_ID, TAG_PUBLIC_SUBKEY, TAG_USER_ATTRIBUTE = 7, 13, 14, 17
PUBLIC_TAGS = {TAG_SIGNATURE, TAG_PUBLIC_KEY, TAG_USER_ID, TAG_PUBLIC_SUBKEY, TAG_USER_ATTRIBUTE}
RSA_ALGOS = {1, 3}
# RSA, RSA sign-only, DSA, ECDSA, EdDSA (legacy), Ed25519, Ed448.
SIGNING_ALGOS = {1, 3, 17, 19, 22, 27, 28}
MIN_RSA_BITS = 2048


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False
    rule: str = ""

    def __str__(self) -> str:
        kind = "warning: " if self.warning else ""
        return f"{self.path}: {kind}{self.message}"


def q(value: object, limit: int = 60) -> str:
    """A file's text for a message: quoted, and cut short so a huge line is not echoed whole."""
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 4] + "...'"


def is_app_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= MAX_ID_CHARS
        and APP_ID.fullmatch(value) is not None
        and not value.rsplit(".", 1)[1].count("-")
    )


def host_of(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def check_https(path: str, where: str, url: str, *, strict_host: bool) -> list[Problem]:
    """An address must be https, without credentials; optionally on a documented host."""
    problems: list[Problem] = []
    if any(c.isspace() or ord(c) < 32 for c in url):
        return [Problem(path, f"{where}: the address has a space or control character")]
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError:
        return [Problem(path, f"{where}: not a valid address: {q(url)}")]
    if parts.scheme != "https":
        problems.append(Problem(path, f"{where}: the address must be https://, not {q(url)}"))
    elif not host:
        problems.append(Problem(path, f"{where}: the address has no host: {q(url)}"))
    if parts.username is not None or parts.password is not None:
        problems.append(Problem(path, f"{where}: the address has a user name or password"))
    if strict_host and parts.scheme == "https" and host and host not in ALLOWED_HOSTS:
        hosts = ", ".join(sorted(ALLOWED_HOSTS))
        problems.append(Problem(path, f"{where}: {q(host)} is not a documented remote ({hosts})"))
    return problems


def split_ref(entry: str) -> tuple[str, str | None] | None:
    """(app id, branch or None) of a list line; None if the line has no valid form."""
    if "/" not in entry:
        return (entry, None)
    parts = entry.split("/")
    if len(parts) == 3 and parts[1] == "":  # id//branch
        return (parts[0], parts[2])
    if len(parts) == 4 and parts[0] == "app" and parts[2] in ARCHES:  # app/id/arch/branch
        return (parts[1], parts[3])
    return None


# ----------------------------------------------------------------- app lists


@dataclass
class AppList:
    remote: str
    ids: list[str] = field(default_factory=list)


def list_remote(name: str) -> str:
    """The remote a list is for: flatpaks.list is Flathub, flatpaks-<remote>.list the rest."""
    match = re.fullmatch(r"flatpaks(?:-(.+))?\.list", name)
    return match.group(1) or "flathub" if match else ""


def check_app_list(path: str, text: str) -> tuple[list[Problem], AppList]:
    """One id per line; the first-boot script drops comments and all white space."""
    problems: list[Problem] = []
    remote = list_remote(Path(path).name)
    parsed = AppList(remote)
    if remote not in REMOTE_HOSTS:
        problems.append(
            Problem(path, f"the list is for the remote {q(remote)}, not a documented one")
        )
    seen: dict[str, int] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        entry = raw.split("#", 1)[0]
        if any(c.isspace() for c in entry.strip()):
            problems.append(
                Problem(
                    path,
                    f"line {number}: more than one word on a line; the script deletes spaces "
                    "and would glue them into one id",
                )
            )
        entry = "".join(entry.split())
        if not entry:
            continue
        if entry.startswith("-"):
            problems.append(
                Problem(path, f"line {number}: {q(entry)} would be passed to flatpak as an option")
            )
            continue
        ref = split_ref(entry)
        if ref is None:
            problems.append(Problem(path, f"line {number}: {q(entry)} is not an app id or ref"))
            continue
        app_id, branch = ref
        if not is_app_id(app_id):
            problems.append(
                Problem(path, f"line {number}: {q(app_id)} is not a reverse-DNS application id")
            )
            continue
        if branch is not None and branch != BRANCH:
            problems.append(
                Problem(
                    path,
                    f"line {number}: {app_id} is on branch {q(branch)}, expected {q(BRANCH)}",
                    rule="branch-not-stable",
                )
            )
        if app_id in seen:
            problems.append(
                Problem(path, f"line {number}: {app_id} is listed twice (line {seen[app_id]})")
            )
        seen.setdefault(app_id, number)
        parsed.ids.append(app_id)
    return problems, parsed


# --------------------------------------------------------------------- picks


@dataclass
class Picks:
    apps: list[dict] = field(default_factory=list)


def check_picks(path: str, text: str) -> tuple[list[Problem], Picks]:
    problems: list[Problem] = []
    picks = Picks()
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError) as err:
        return [Problem(path, f"not valid TOML: {q(str(err))}")], picks
    apps = data.get("app", [])
    if not isinstance(apps, list):
        return [Problem(path, "'app' must be an array of tables ([[app]])")], picks
    seen: set[str] = set()
    for number, app in enumerate(apps, start=1):
        if not isinstance(app, dict):
            problems.append(Problem(path, f"app #{number}: not a table"))
            continue
        app_id = app.get("id")
        where = f"app #{number} ({app_id if isinstance(app_id, str) else '?'})"
        if not is_app_id(app_id):
            problems.append(Problem(path, f"{where}: id is not a reverse-DNS application id"))
            continue
        if app_id in seen:
            problems.append(Problem(path, f"{where}: listed twice"))
        seen.add(app_id)
        source = app.get("source")
        if source not in SOURCE_REMOTE:
            problems.append(
                Problem(path, f"{where}: source {q(source)} is not one of {sorted(SOURCE_REMOTE)}")
            )
        elif source == "vilna" and not app_id.startswith(OWN_NAMESPACE):
            problems.append(Problem(path, f"{where}: source vilna but not in {OWN_NAMESPACE}*"))
        elif source != "vilna" and app_id.startswith(OWN_NAMESPACE):
            problems.append(Problem(path, f"{where}: our own app with a Flathub source"))
        if "branch" in app and app["branch"] != BRANCH:
            problems.append(
                Problem(
                    path,
                    f"{where}: branch {q(app['branch'])}, expected {q(BRANCH)}",
                    rule="branch-not-stable",
                )
            )
        if "remote" in app and app["remote"] not in REMOTE_HOSTS:
            problems.append(
                Problem(path, f"{where}: remote {q(app['remote'])} is not a documented remote")
            )
        elif "remote" in app and source in SOURCE_REMOTE and SOURCE_REMOTE[source] != app["remote"]:
            problems.append(
                Problem(path, f"{where}: remote {q(app['remote'])} does not match source")
            )
        if "default" in app and not isinstance(app["default"], bool):
            problems.append(Problem(path, f"{where}: default must be true or false"))
        picks.apps.append(app)
    return problems, picks


# ---------------------------------------------------------- OpenPGP key data


@dataclass(frozen=True)
class KeyInfo:
    fingerprint: str
    version: int
    algorithm: int


class KeyFormatError(ValueError):
    """The GPGKey value is not what Flatpak needs."""


def read_packets(data: bytes) -> list[tuple[int, bytes]]:
    """Split binary OpenPGP data into (tag, body); the packets must fill it exactly."""
    packets: list[tuple[int, bytes]] = []
    pos, end = 0, len(data)
    while pos < end:
        first = data[pos]
        pos += 1
        if not first & 0x80:
            raise KeyFormatError(f"byte {pos - 1} is not an OpenPGP packet header")
        if first & 0x40:
            tag = first & 0x3F
            if pos >= end:
                raise KeyFormatError("truncated packet length")
            octet = data[pos]
            pos += 1
            if octet < 192:
                length = octet
            elif octet < 224:
                if pos >= end:
                    raise KeyFormatError("truncated packet length")
                length = ((octet - 192) << 8) + data[pos] + 192
                pos += 1
            elif octet == 255:
                if pos + 4 > end:
                    raise KeyFormatError("truncated packet length")
                length = int.from_bytes(data[pos : pos + 4], "big")
                pos += 4
            else:
                raise KeyFormatError(f"packet {tag} has a partial length, a key never does")
        else:
            tag = (first >> 2) & 0x0F
            size = {0: 1, 1: 2, 2: 4}.get(first & 3)
            if size is None:
                raise KeyFormatError(f"packet {tag} has an indeterminate length")
            if pos + size > end:
                raise KeyFormatError("truncated packet length")
            length = int.from_bytes(data[pos : pos + size], "big")
            pos += size
        if pos + length > end:
            raise KeyFormatError(f"packet {tag} says {length} bytes but only {end - pos} are left")
        packets.append((tag, data[pos : pos + length]))
        pos += length
    return packets


def key_fingerprint(body: bytes) -> tuple[str, int, int]:
    """(fingerprint, version, algorithm) of a public key packet body."""
    if len(body) < 6:
        raise KeyFormatError("the public key packet is too short")
    version, algorithm = body[0], body[5]
    if version == 4:
        digest = hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest()  # noqa: S324
    elif version == 6:
        digest = hashlib.sha256(b"\x9b" + len(body).to_bytes(4, "big") + body).hexdigest()
    else:
        raise KeyFormatError(f"key version {version}: only versions 4 and 6 are accepted")
    return digest.upper(), version, algorithm


def check_key_bytes(data: bytes) -> KeyInfo:
    """Validate the decoded GPGKey and return the key's fingerprint."""
    if not data:
        raise KeyFormatError("the key is empty")
    if len(data) > MAX_KEY_BYTES:
        raise KeyFormatError(f"the key is {len(data)} bytes; export only the signing key")
    packets = read_packets(data)
    tags = [tag for tag, _ in packets]
    if TAG_SECRET_KEY in tags or TAG_SECRET_SUBKEY in tags:
        raise KeyFormatError("it holds a SECRET key; only the public key goes into this file")
    odd = sorted({t for t in tags if t not in PUBLIC_TAGS})
    if odd:
        raise KeyFormatError(f"packet type(s) {odd} do not belong in an exported public key")
    if tags[0] != TAG_PUBLIC_KEY:
        raise KeyFormatError("the first packet is not a public key")
    if tags.count(TAG_PUBLIC_KEY) != 1:
        raise KeyFormatError("it holds more than one primary key; export only the signing key")
    if TAG_USER_ID not in tags:
        raise KeyFormatError("the key has no User ID packet (gpg does not import such a key)")
    body = packets[0][1]
    fingerprint, version, algorithm = key_fingerprint(body)
    if algorithm not in SIGNING_ALGOS:
        raise KeyFormatError(f"public-key algorithm {algorithm} cannot sign a repository")
    if algorithm in RSA_ALGOS:
        offset = 6 if version == 4 else 10
        if len(body) < offset + 2:
            raise KeyFormatError("the RSA key is too short")
        bits = int.from_bytes(body[offset : offset + 2], "big")
        if bits < MIN_RSA_BITS:
            raise KeyFormatError(f"RSA key of {bits} bits; at least {MIN_RSA_BITS} are needed")
    return KeyInfo(fingerprint, version, algorithm)


def decode_gpgkey(value: str) -> bytes:
    if "-----BEGIN" in value:
        raise KeyFormatError("an ASCII-armored key; Flatpak wants base64 of the binary key")
    if any(c.isspace() for c in value):
        raise KeyFormatError("the key has white space inside; it must be one base64 string")
    if len(value) > MAX_KEY_BYTES * 2:
        raise KeyFormatError("the key value is far too long")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise KeyFormatError("not valid base64") from None


# ------------------------------------------- .flatpakrepo and .flatpakref files


def parse_ini(path: str, text: str) -> tuple[dict[str, dict[str, str]], list[Problem]]:
    """Groups of Key=Value pairs, in the freedesktop way (no interpolation, case kept)."""
    groups: dict[str, dict[str, str]] = {}
    problems: list[Problem] = []
    current: dict[str, str] | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("["):
            if not line.endswith("]") or len(line) < 3:
                problems.append(Problem(path, f"line {number}: invalid group header {q(line)}"))
                current = None
            elif line[1:-1] in groups:
                problems.append(Problem(path, f"line {number}: group {line} repeated"))
                current = None
            else:
                current = groups.setdefault(line[1:-1], {})
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z0-9-]+(\[[^\]\s]+\])?", key):
            problems.append(Problem(path, f"line {number}: not a Key=Value line: {line[:40]!r}"))
        elif current is None:
            problems.append(Problem(path, f"line {number}: key before the first group"))
        elif key in current:
            problems.append(Problem(path, f"line {number}: duplicate key {key}"))
        else:
            current[key] = value
    return groups, problems


URL_KEYS = ("Url", "Homepage", "Icon", "RuntimeRepo", "SuggestRemoteName")


def check_repo_file(path: str, text: str) -> tuple[list[Problem], KeyInfo | None]:
    """A .flatpakrepo or .flatpakref file; returns the problems and the key's identity."""
    ref = path.endswith(".flatpakref")
    group_name = "Flatpak Ref" if ref else "Flatpak Repo"
    groups, problems = parse_ini(path, text)
    if not groups or next(iter(groups)) != group_name:
        problems.append(Problem(path, f"the first group must be [{group_name}]"))
        return problems, None
    keys = groups[group_name]
    for extra in sorted(set(groups) - {group_name}):
        problems.append(Problem(path, f"unexpected group [{extra}]", warning=True))
    if not keys.get("Title"):
        problems.append(Problem(path, "Title is missing"))
    url = keys.get("Url")
    if not url:
        problems.append(Problem(path, "Url is missing"))
    else:
        problems += check_https(path, "Url", url, strict_host=True)
        if url.startswith("https://") and not url.endswith("/") and not ref:
            problems.append(Problem(path, "Url should end with '/'", warning=True))
    for key in ("Homepage", "Icon", "RuntimeRepo"):
        if keys.get(key):
            # RuntimeRepo points at a .flatpakrepo of another remote: host rules apply to it.
            problems += check_https(path, key, keys[key], strict_host=key == "RuntimeRepo")
    runtime_repo = keys.get("RuntimeRepo")
    if runtime_repo and runtime_repo != FLATHUB_REPO_URL:
        problems.append(Problem(path, f"RuntimeRepo is not {FLATHUB_REPO_URL}"))
    for key in ("Branch", "DefaultBranch"):
        if key in keys and keys[key] != BRANCH:
            problems.append(
                Problem(
                    path,
                    f"{key}={q(keys[key])}, expected {q(BRANCH)}",
                    rule="branch-not-stable",
                )
            )
    if ref:
        name = keys.get("Name", "")
        if not is_app_id(name):
            problems.append(Problem(path, f"Name {q(name)} is not a reverse-DNS application id"))
    value = keys.get("GPGKey")
    if not value:
        problems.append(
            Problem(
                path, "GPGKey is missing: Flatpak would add the remote without a signature check"
            )
        )
        return problems, None
    try:
        return problems, check_key_bytes(decode_gpgkey(value))
    except KeyFormatError as err:
        problems.append(Problem(path, f"GPGKey: {err}"))
        return problems, None


# ------------------------------------------------------ first-boot script, spec


def shell_statements(text: str) -> list[tuple[int, str]]:
    """(first line number, statement) with comments removed and ``\\`` lines joined."""
    out: list[tuple[int, str]] = []
    buffer, start = "", 0
    for number, raw in enumerate(text.splitlines(), start=1):
        if not buffer:
            start = number
        line = raw.rstrip()
        buffer += line[:-1] + " " if line.endswith("\\") else line
        if not line.endswith("\\"):
            out.append((start, buffer))
            buffer = ""
    if buffer:
        out.append((start, buffer))
    return out


@dataclass
class Script:
    installed_files: list[str] = field(default_factory=list)


def check_script(path: str, text: str) -> tuple[list[Problem], Script]:
    problems: list[Problem] = []
    script = Script()
    for number, statement in shell_statements(text):
        try:
            words = shlex.split(statement, comments=True)
        except ValueError:
            continue  # an open quote spans lines (a here-document or message); not our business
        code = " ".join(words)
        if not words:
            continue
        if "--no-gpg-verify" in words or "--no-gpg-verify" in code:
            problems.append(
                Problem(path, f"line {number}: --no-gpg-verify switches the signature check off")
            )
        for url in re.findall(r"https?://[^\s\"'`;)]+", code):
            host = host_of(url)
            if not url.startswith("https://"):
                problems.append(Problem(path, f"line {number}: {q(url)} is not https"))
            elif host not in ALLOWED_HOSTS:
                problems.append(
                    Problem(path, f"line {number}: {q(host)} is not a documented remote")
                )
        for index, word in enumerate(words):
            value = None
            if word.startswith("--branch="):
                value = word.split("=", 1)[1]
            elif word == "--branch" and index + 1 < len(words):
                value = words[index + 1]
            if value is not None and value != BRANCH:
                problems.append(
                    Problem(
                        path,
                        f"line {number}: --branch {q(value)}, expected {q(BRANCH)}",
                        rule="branch-not-stable",
                    )
                )
        if words[0] != "install_from":
            continue
        if len(words) < 4:
            problems.append(
                Problem(path, f"line {number}: install_from needs REMOTE REPOFILE LIST")
            )
            continue
        remote, repofile, listfile = words[1], words[2], words[3]
        if remote not in REMOTE_HOSTS:
            problems.append(
                Problem(path, f"line {number}: remote {q(remote)} is not a documented remote")
            )
        elif (
            remote == "flathub"
            and repofile.startswith(("http://", "https://"))
            and repofile != FLATHUB_REPO_URL
        ):
            problems.append(
                Problem(path, f"line {number}: the flathub address is not {FLATHUB_REPO_URL}")
            )
        if not listfile.endswith(".list"):
            problems.append(Problem(path, f"line {number}: {q(listfile)} is not a .list file"))
        elif remote in REMOTE_HOSTS and list_remote(Path(listfile).name) != remote:
            problems.append(
                Problem(
                    path,
                    f"line {number}: list {Path(listfile).name} is not the list of remote {remote}",
                )
            )
        script.installed_files.append(Path(listfile).name)
    for _, statement in shell_statements(text):
        match = re.match(r'\s*vilna_repo="?\$SHARE/([^"\s]+)"?', statement) or re.search(
            r'\[ -r "\$SHARE/([^"]+\.flatpakrepo)" \]', statement
        )
        if match:
            script.installed_files.append(match.group(1))
    return problems, script


def check_spec(path: str, text: str) -> tuple[list[Problem], dict[str, set[str]]]:
    """Files the spec installs into /usr/share/vilna, and the fingerprint it names."""
    sources: dict[str, str] = {}
    shipped: set[str] = set()
    fingerprints: set[str] = set()
    block: list[str] = []
    for line in [*text.splitlines(), ""]:
        match = re.match(r"Source(\d+):\s*(\S+)", line)
        if match:
            sources[match.group(1)] = match.group(2)
        if line.startswith("#"):
            block.append(line)
            continue
        joined_block = " ".join(block)
        if re.search(r"\bkey\b", joined_block, re.IGNORECASE):
            fingerprints |= {f.upper() for f in FINGERPRINT.findall(joined_block)}
        block = []
    joined = text.replace("\\\n", " ")
    for line in joined.splitlines():
        match = re.match(
            r"\s*install\b.*%\{SOURCE(\d+)\}\s+%\{buildroot\}%\{_datadir\}/vilna/(\S+)", line
        )
        if match:
            shipped.add(match.group(2))
            if match.group(1) not in sources:
                return [Problem(path, f"SOURCE{match.group(1)} is installed but not declared")], {
                    "shipped": shipped,
                    "fingerprints": fingerprints,
                }
    return [], {"shipped": shipped, "fingerprints": fingerprints}


def check_mimeapps(path: str, text: str) -> tuple[list[Problem], set[str]]:
    problems: list[Problem] = []
    ids: set[str] = set()
    in_group = False
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_group = line == "[Default Applications]"
            continue
        if not in_group or "=" not in line:
            continue
        value = line.split("=", 1)[1]
        for entry in filter(None, (v.strip() for v in value.split(";"))):
            if not entry.endswith(".desktop"):
                problems.append(Problem(path, f"line {number}: {q(entry)} is not a .desktop id"))
                continue
            app_id = entry[: -len(".desktop")]
            if not is_app_id(app_id):
                problems.append(
                    Problem(path, f"line {number}: {q(app_id)} is not a reverse-DNS application id")
                )
            else:
                ids.add(app_id)
    return problems, ids


# ----------------------------------------------------------- third-party apps


@dataclass
class Manifest:
    app_id: str | None
    branch: str | None
    sources: list[dict]


def yaml_scalar(value: str) -> str:
    value = value.strip()
    if value and value[0] in "\"'":
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end > 0 else value[1:]
    return value.split(" #", 1)[0].strip()


def parse_yaml_manifest(text: str) -> Manifest:
    """The few fields we need from a flatpak-builder YAML manifest (no YAML library)."""
    top: dict[str, str] = {}
    sources: list[dict] = []
    current: dict | None = None
    in_sources = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indented = raw[:1] in " \t"
        if not indented:
            match = re.match(r"([A-Za-z0-9_-]+):\s*(.*)$", raw)
            if match:
                top.setdefault(match.group(1), yaml_scalar(match.group(2)))
            in_sources = False
            current = None
            continue
        stripped = raw.strip()
        if stripped == "sources:":
            in_sources = True
            continue
        item = re.match(r"-\s+(type):\s*(.*)$", stripped)
        if in_sources and item:
            current = {"type": yaml_scalar(item.group(2))}
            sources.append(current)
            continue
        pair = re.match(r"([a-z0-9_-]+):\s*(.*)$", stripped)
        if (
            in_sources
            and current is not None
            and pair
            and pair.group(1)
            in (
                "url",
                "sha256",
                "sha512",
                "sha1",
                "md5",
                "size",
                "filename",
            )
        ):
            current.setdefault(pair.group(1), yaml_scalar(pair.group(2)))
    return Manifest(top.get("id") or top.get("app-id"), top.get("branch"), sources)


def parse_json_manifest(text: str) -> Manifest | str:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as err:
        return f"not valid JSON: {err}"
    if not isinstance(data, dict):
        return "the manifest must be a JSON object"
    sources: list[dict] = []

    def walk(modules: object, depth: int = 0) -> None:
        if not isinstance(modules, list) or depth > 20:
            return
        for module in modules:
            if not isinstance(module, dict):
                continue
            for source in (
                module.get("sources", []) if isinstance(module.get("sources"), list) else []
            ):
                if isinstance(source, dict):
                    sources.append(
                        {k: str(v) for k, v in source.items() if not isinstance(v, (dict, list))}
                    )
            walk(module.get("modules"), depth + 1)

    walk(data.get("modules"))
    app_id = data.get("id", data.get("app-id"))
    branch = data.get("branch")
    return Manifest(
        app_id if isinstance(app_id, str) else None,
        branch if isinstance(branch, str) else None,
        sources,
    )


def check_manifest(path: str, text: str, root: Path) -> list[Problem]:
    problems: list[Problem] = []
    parsed = parse_json_manifest(text) if path.endswith(".json") else parse_yaml_manifest(text)
    if isinstance(parsed, str):
        return [Problem(path, parsed)]
    stem = MANIFEST_NAME.fullmatch(Path(path).name)
    app_id = parsed.app_id
    if not app_id:
        problems.append(Problem(path, "the manifest has no 'id'"))
    elif not is_app_id(app_id):
        problems.append(Problem(path, f"id {q(app_id)} is not a reverse-DNS application id"))
    else:
        if not app_id.startswith(OWN_NAMESPACE):
            problems.append(
                Problem(
                    path, f"id {app_id} is not in our namespace {OWN_NAMESPACE}* (not ours to name)"
                )
            )
        if stem and stem.group(1) != app_id:
            problems.append(Problem(path, f"id {app_id} differs from the file name"))
        problems += check_siblings(path, app_id, root)
    if parsed.branch is None:
        problems.append(
            Problem(
                path,
                f"no 'branch:' (flatpak-builder builds master); expected branch: {BRANCH}",
                rule="branch-missing",
            )
        )
    elif parsed.branch != BRANCH:
        problems.append(
            Problem(
                path, f"branch {q(parsed.branch)}, expected {q(BRANCH)}", rule="branch-not-stable"
            )
        )
    for number, source in enumerate(parsed.sources, start=1):
        problems += check_source(path, number, source)
    return problems


def check_source(path: str, number: int, source: dict) -> list[Problem]:
    url = source.get("url")
    if url is None:
        return []
    where = f"source #{number}"
    problems = check_https(path, f"{where} url", url, strict_host=False)
    kind = source.get("type", "")
    if kind == "git":
        return problems
    if "md5" in source or "sha1" in source:
        problems.append(Problem(path, f"{where}: md5 and sha1 are not accepted as a checksum"))
    digest256, digest512 = source.get("sha256"), source.get("sha512")
    if digest256 is None and digest512 is None:
        problems.append(Problem(path, f"{where}: {q(url)} has no sha256"))
    if digest256 is not None and not HEX_SHA256.fullmatch(digest256):
        problems.append(Problem(path, f"{where}: sha256 is not 64 lowercase hex digits"))
    if digest512 is not None and not HEX_SHA512.fullmatch(digest512):
        problems.append(Problem(path, f"{where}: sha512 is not 128 lowercase hex digits"))
    if kind == "extra-data":
        size = source.get("size", "")
        if not size.isdigit() or int(size) <= 0:
            problems.append(Problem(path, f"{where}: extra-data needs a size in bytes"))
        if not source.get("filename"):
            problems.append(Problem(path, f"{where}: extra-data needs a filename"))
    return problems


def check_siblings(path: str, app_id: str, root: Path) -> list[Problem]:
    """The .desktop file and the AppStream file of a manifest carry the same id."""
    problems: list[Problem] = []
    folder = (root / path).parent
    desktop = folder / f"{app_id}.desktop"
    metainfo = folder / f"{app_id}.metainfo.xml"
    if not desktop.is_file():
        problems.append(Problem(path, f"{app_id}.desktop is missing beside the manifest"))
    if not metainfo.is_file():
        problems.append(Problem(path, f"{app_id}.metainfo.xml is missing beside the manifest"))
        return problems
    try:
        if metainfo.stat().st_size > MAX_FILE_BYTES:
            return problems + [Problem(path, f"{metainfo.name} is too large")]
        found = ET.parse(metainfo).getroot().findtext("id")
    except (ET.ParseError, OSError) as err:
        return problems + [Problem(path, f"{metainfo.name} cannot be read: {err}")]
    if (found or "").strip() != app_id:
        problems.append(Problem(path, f"{metainfo.name} has <id> {q(found)}, expected {app_id}"))
    return problems


# ------------------------------------------------------------- cross checks


def classify(rel: str) -> str | None:
    name = Path(rel).name
    if name.endswith(".flatpakrepo"):
        return "repo"
    if name.endswith(".flatpakref"):
        return "ref"
    if name == "gnome-mimeapps.list":
        return "mimeapps"
    if re.fullmatch(r"flatpaks(?:-.+)?\.list", name):
        return "applist"
    if name == "picks.toml":
        return "picks"
    if name == "vilna-flatpak-defaults.sh":
        return "script"
    if name.endswith(".spec") and "flatpak-defaults" in name:
        return "spec"
    if MANIFEST_NAME.fullmatch(name) and rel.startswith("thirdparty/"):
        return "manifest"
    return None


def read_text(root: Path, rel: str) -> tuple[str | None, Problem | None]:
    """The file as text, or a problem. Symlinks, huge, binary or non-UTF-8 files are refused."""
    full = root / rel
    try:
        info = os.lstat(full)
    except OSError as err:
        return None, Problem(rel, f"cannot read the file: {err.strerror or err}")
    if not stat.S_ISREG(info.st_mode):
        return None, Problem(rel, "not a regular file (a symbolic link or a folder)")
    if info.st_size > MAX_FILE_BYTES:
        return None, Problem(
            rel, f"the file is {info.st_size} bytes; the limit is {MAX_FILE_BYTES}"
        )
    try:
        with open(full, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as err:
        return None, Problem(rel, f"cannot read the file: {err.strerror or err}")
    if b"\0" in data:
        return None, Problem(rel, "the file is binary (it has a NUL byte)")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, Problem(rel, "the file is not valid UTF-8")
    if text.startswith("\ufeff"):
        return text[1:], Problem(rel, "the file starts with a byte order mark (BOM)")
    return text, None


def downgrade(problem: Problem) -> Problem:
    """Turn a known old violation (see LEGACY) into a warning."""
    if (problem.path, problem.rule) in LEGACY:
        return Problem(problem.path, problem.message, warning=True, rule=problem.rule)
    return problem


def check_files(root: Path, paths: Sequence[str]) -> tuple[list[Problem], int]:
    """Check the given files (relative to ``root``); returns the problems and the file count."""
    problems: list[Problem] = []
    lists: list[tuple[str, AppList]] = []
    picks: list[tuple[str, Picks]] = []
    scripts: list[tuple[str, Script]] = []
    specs: list[tuple[str, dict[str, set[str]]]] = []
    mime: list[tuple[str, set[str]]] = []
    keys: list[tuple[str, KeyInfo]] = []
    checked = 0
    for rel in dict.fromkeys(paths):
        kind = classify(rel)
        if kind is None:
            continue
        checked += 1
        text, problem = read_text(root, rel)
        if problem:
            problems.append(problem)
        if text is None:
            continue
        if kind == "applist":
            found, parsed = check_app_list(rel, text)
            problems += found
            lists.append((rel, parsed))
        elif kind == "picks":
            found, parsed_picks = check_picks(rel, text)
            problems += found
            picks.append((rel, parsed_picks))
        elif kind in ("repo", "ref"):
            found, info = check_repo_file(rel, text)
            problems += found
            if info is not None and kind == "repo":
                keys.append((rel, info))
        elif kind == "script":
            found, parsed_script = check_script(rel, text)
            problems += found
            scripts.append((rel, parsed_script))
        elif kind == "spec":
            found, parsed_spec = check_spec(rel, text)
            problems += found
            specs.append((rel, parsed_spec))
        elif kind == "mimeapps":
            found, ids = check_mimeapps(rel, text)
            problems += found
            mime.append((rel, ids))
        elif kind == "manifest":
            problems += check_manifest(rel, text, root)
    problems += cross_check(lists, picks, scripts, specs, mime, keys)
    return [downgrade(p) for p in problems], checked


def cross_check(
    lists: list[tuple[str, AppList]],
    picks: list[tuple[str, Picks]],
    scripts: list[tuple[str, Script]],
    specs: list[tuple[str, dict[str, set[str]]]],
    mime: list[tuple[str, set[str]]],
    keys: list[tuple[str, KeyInfo]],
) -> list[Problem]:
    problems: list[Problem] = []
    owner: dict[str, str] = {}
    for rel, parsed in lists:
        for app_id in parsed.ids:
            other = owner.setdefault(app_id, rel)
            if other != rel:
                problems.append(
                    Problem(rel, f"{app_id} is also in {other}: it would come from two remotes")
                )
    for rel, parsed_picks in picks:
        by_id = {a["id"]: a for a in parsed_picks.apps}
        for _, parsed in lists:
            for app_id in parsed.ids:
                app = by_id.get(app_id)
                if app is None:
                    continue
                want = SOURCE_REMOTE.get(app.get("source"))
                if want and parsed.remote in REMOTE_HOSTS and want != parsed.remote:
                    problems.append(
                        Problem(
                            rel,
                            f"{app_id} is in the {parsed.remote} list but its source is "
                            f"{app.get('source')!r}",
                        )
                    )
        if lists:
            listed = {(p.remote, i) for _, p in lists for i in p.ids}
            remotes_known = {p.remote for _, p in lists}
            for app_id, app in by_id.items():
                want = SOURCE_REMOTE.get(app.get("source"))
                if (
                    app.get("default") is True
                    and want in remotes_known
                    and (want, app_id) not in listed
                ):
                    problems.append(
                        Problem(rel, f"{app_id} has default = true but is not in the {want} list")
                    )
                if app.get("default") is False and (want, app_id) in listed:
                    problems.append(
                        Problem(rel, f"{app_id} is installed by default: set default = true")
                    )
    vilna_ids = {i for _, p in lists if p.remote == "vilna" for i in p.ids}
    have_vilna_list = any(p.remote == "vilna" for _, p in lists)
    for rel, ids in mime:
        if not have_vilna_list:
            continue
        for app_id in sorted(ids):
            if app_id.startswith(OWN_NAMESPACE) and app_id not in vilna_ids:
                problems.append(
                    Problem(
                        rel,
                        f"{app_id} is a default for a file type but no list installs it",
                        warning=True,
                    )
                )
    for rel, script in scripts:
        for spec_rel, spec in specs:
            for name in dict.fromkeys(script.installed_files):
                if name not in spec["shipped"]:
                    problems.append(
                        Problem(rel, f"the script reads {name} but {spec_rel} does not install it")
                    )
    for rel, info in keys:
        for spec_rel, spec in specs:
            named = spec["fingerprints"]
            if named and info.fingerprint not in named:
                problems.append(
                    Problem(
                        rel,
                        f"the key's fingerprint {info.fingerprint} is not the one {spec_rel} names "
                        f"({', '.join(sorted(named))})",
                    )
                )
    return problems


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--",
            "obs/packages/vilna-flatpak-defaults",
            "obs/packages/vilna-software-picks/picks.toml",
            "thirdparty",
            "*.flatpakrepo",
            "*.flatpakref",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted({p for p in out.split("\0") if p})


def main(argv: list[str] | None = None) -> int:
    try:
        args = sys.argv[1:] if argv is None else argv
        if any(a.startswith("-") for a in args):
            print("usage: python3 tools/check_flatpak_defaults.py [FILE ...]", file=sys.stderr)
            return 0 if args in (["-h"], ["--help"]) else 2
        if args:
            paths, root = args, Path.cwd()
        else:
            try:
                paths, root = tracked_files(ROOT), ROOT
            except (OSError, subprocess.CalledProcessError) as err:
                print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
                return 2
        problems, checked = check_files(root, paths)
        if not checked:
            print("no Flatpak default files found", file=sys.stderr)
            return 2
        for problem in problems:
            print(problem, file=sys.stderr)
        errors = sum(not p.warning for p in problems)
        print(f"{checked} Flatpak files checked: {errors} errors, {len(problems) - errors} warnings")
        return 1 if errors else 0
    except (OSError, ValueError) as err:  # no traceback for a bad name, argument or bytes
        print(f"{os.path.basename(__file__)}: cannot check: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
