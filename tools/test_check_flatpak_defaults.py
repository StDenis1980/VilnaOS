#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_flatpak_defaults.py (the check of the Flatpak defaults).

Run: python3 tools/test_check_flatpak_defaults.py
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_flatpak_defaults as cf  # noqa: E402  (after sys.path.insert above)

DEFAULTS = "obs/packages/vilna-flatpak-defaults"
PICKS_DIR = "obs/packages/vilna-software-picks"
APP = "thirdparty/demo"
OWN = "io.github.stdenis1980.Demo"


# ---------------------------------------------------------------- OpenPGP data


def packet(tag: int, body: bytes) -> bytes:
    """An old-format packet with a one- or two-byte length."""
    if len(body) < 256:
        return bytes([0x80 | tag << 2, len(body)]) + body
    return bytes([0x80 | tag << 2 | 1]) + len(body).to_bytes(2, "big") + body


def new_packet(tag: int, body: bytes) -> bytes:
    """A new-format packet (tag in the low six bits, one- or two-octet length)."""
    if len(body) < 192:
        return bytes([0xC0 | tag, len(body)]) + body
    n = len(body) - 192
    return bytes([0xC0 | tag, 192 + (n >> 8), n & 0xFF]) + body


def ed25519_body(seed: int = 1) -> bytes:
    oid = bytes.fromhex("2b06010401da470f01")
    point = b"\x40" + bytes([seed]) * 32
    return b"\x04" + b"\x6a\xf2\x45\x2a" + b"\x16" + bytes([len(oid)]) + oid + b"\x01\x07" + point


def rsa_body(bits: int) -> bytes:
    n = bytes([0x80]) + b"\x11" * ((bits + 7) // 8 - 1)
    return b"\x04" + b"\x00\x00\x00\x01" + b"\x01" + bits.to_bytes(2, "big") + n + b"\x00\x11\x03"


def key_blob(body: bytes | None = None, *, user: bool = True, extra: bytes = b"") -> bytes:
    out = packet(6, ed25519_body() if body is None else body)
    if user:
        out += packet(13, b"Vilna Test")
    out += packet(2, b"\x04\x13" + b"\x00" * 20)
    return out + extra


def fingerprint_v4(body: bytes) -> str:
    return hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper()  # noqa: S324


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


GOOD_KEY = b64(key_blob())
GOOD_FPR = fingerprint_v4(ed25519_body())


# ----------------------------------------------------------------- the files


def repo_file(
    *,
    url: str = "https://flatpak.vilnaos.com/repo/",
    key: str | None = GOOD_KEY,
    extra: str = "",
    title: str | None = "Vilna",
) -> str:
    lines = ["[Flatpak Repo]"]
    if title is not None:
        lines.append(f"Title={title}")
    if url:
        lines.append(f"Url={url}")
    lines.append("Homepage=https://vilnaos.com/gloxi/")
    if key is not None:
        lines.append(f"GPGKey={key}")
    return "\n".join(lines) + "\n" + extra


SCRIPT = """#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -uo pipefail
SHARE=${VILNA_FLATPAK_SHARE:-/usr/share/vilna}
install_from() {
    "$FLATPAK" remote-add --system --if-not-exists "$1" "$2"
    "$FLATPAK" install --system --noninteractive --or-update "$1" "$3"
}
install_from flathub https://dl.flathub.org/repo/flathub.flatpakrepo \\
    "$SHARE/flatpaks.list" || failed=1

vilna_repo=""
[ -r "$SHARE/vilna.flatpakrepo" ] && vilna_repo="$SHARE/vilna.flatpakrepo"
install_from vilna "$vilna_repo" "$SHARE/flatpaks-vilna.list"
"""

SPEC = f"""Name:           vilna-flatpak-defaults
Source0:        flatpaks.list
Source1:        vilna-flatpak-defaults.sh
Source3:        flatpaks-vilna.list
# Public key of Vilna's Flatpak repository (Vilna Flatpak, ed25519,
# {GOOD_FPR}), copied unchanged.
Source5:        vilna.flatpakrepo

%install
install -D -m 0644 %{{SOURCE0}} %{{buildroot}}%{{_datadir}}/vilna/flatpaks.list
install -D -m 0755 %{{SOURCE1}} %{{buildroot}}%{{_libexecdir}}/vilna-flatpak-defaults
install -D -m 0644 %{{SOURCE3}} %{{buildroot}}%{{_datadir}}/vilna/flatpaks-vilna.list
install -D -m 0644 %{{SOURCE5}} %{{buildroot}}%{{_datadir}}/vilna/vilna.flatpakrepo
"""

PICKS = """[section]
selector = "vilna"

[[app]]
id = "org.example.Browser"
category = "work"
license = "MPL-2.0"
source = "flathub-verified"
default = true
why = "x"

[[app]]
id = "org.example.Editor"
category = "work"
license = "MIT"
source = "flathub-verified"
default = true
why = "x"

[[app]]
id = "org.example.Extra"
category = "work"
license = "MIT"
source = "flathub-verified"
default = false
why = "x"

[[app]]
id = "io.github.stdenis1980.Office"
category = "work"
license = "MIT"
source = "vilna"
default = true
why = "x"
"""

MIMEAPPS = """[Default Applications]
application/pdf=io.github.stdenis1980.Office.desktop
text/csv=io.github.stdenis1980.Office.desktop;org.example.Editor.desktop
"""

MANIFEST = f"""# comment
id: {OWN}
runtime: org.freedesktop.Platform
runtime-version: "25.08"
branch: stable
command: demo
finish-args:
  - --share=network
modules:
  - name: demo
    buildsystem: simple
    sources:
      - type: extra-data
        filename: demo.deb
        only-arches: [x86_64]
        url: https://downloads.example.org/demo_1.deb
        sha256: "{"a" * 64}"
        size: 1234
      - type: file
        path: demo.sh
"""

DESKTOP = f"[Desktop Entry]\nType=Application\nName=Demo\nExec=demo\nIcon={OWN}\n"
METAINFO = f"<component><id>{OWN}</id></component>\n"

GOOD_FILES: dict[str, str] = {
    f"{DEFAULTS}/flatpaks.list": "# comment\norg.example.Browser\norg.example.Editor\n",
    f"{DEFAULTS}/flatpaks-vilna.list": "io.github.stdenis1980.Office  # ours\n",
    f"{DEFAULTS}/vilna.flatpakrepo": repo_file(),
    f"{DEFAULTS}/vilna-flatpak-defaults.sh": SCRIPT,
    f"{DEFAULTS}/vilna-flatpak-defaults.spec": SPEC,
    f"{DEFAULTS}/gnome-mimeapps.list": MIMEAPPS,
    f"{PICKS_DIR}/picks.toml": PICKS,
    f"{APP}/{OWN}.yml": MANIFEST,
    f"{APP}/{OWN}.desktop": DESKTOP,
    f"{APP}/{OWN}.metainfo.xml": METAINFO,
}


class Tree:
    """A temporary repo with the good files; tests change some and check."""

    def __init__(self, test: unittest.TestCase, **changes: str | None) -> None:
        self._dir = tempfile.TemporaryDirectory()
        test.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        for rel, text in GOOD_FILES.items():
            self.write(rel, text)
        for key, text in changes.items():
            self.write(self.rel(key), text)

    @staticmethod
    def rel(name: str) -> str:
        for rel in GOOD_FILES:
            if Path(rel).name == name or rel == name:
                return rel
        return name

    def write(self, rel: str, text: str | bytes | None) -> None:
        path = self.root / rel
        if text is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")

    def check(self, only: list[str] | None = None) -> list[cf.Problem]:
        paths = only if only is not None else sorted(GOOD_FILES)
        return cf.check_files(self.root, paths)[0]


def errors(problems: list[cf.Problem]) -> list[str]:
    return [str(p) for p in problems if not p.warning]


def warnings(problems: list[cf.Problem]) -> list[str]:
    return [str(p) for p in problems if p.warning]


def one_file(test: unittest.TestCase, rel: str, text: str | bytes) -> list[cf.Problem]:
    tree = Tree(test)
    tree.write(rel, text)
    return tree.check([rel])


def has(lines: list[str], *needles: str) -> bool:
    return any(all(n in line for n in needles) for line in lines)


class GoodFiles(unittest.TestCase):
    def test_good_tree_passes(self) -> None:
        self.assertEqual(Tree(self).check(), [])

    def test_good_tree_counts_all_ten_files_it_knows(self) -> None:
        tree = Tree(self)
        problems, count = cf.check_files(tree.root, sorted(GOOD_FILES))
        self.assertEqual((problems, count), ([], 8))  # the .desktop and .xml are not classified

    def test_message_form_is_path_colon_problem(self) -> None:
        tree = Tree(self, **{"flatpaks.list": "not an id\n"})
        found = errors(tree.check([f"{DEFAULTS}/flatpaks.list"]))
        self.assertTrue(found)
        for line in found:
            self.assertTrue(line.startswith(f"{DEFAULTS}/flatpaks.list: "), line)

    def test_unknown_files_are_ignored(self) -> None:
        tree = Tree(self)
        tree.write("README.md", "x")
        self.assertEqual(cf.check_files(tree.root, ["README.md"]), ([], 0))


class AppIds(unittest.TestCase):
    def test_valid_ids(self) -> None:
        for good in (
            "org.mozilla.firefox",
            "io.github.stdenis1980.tintatexto",
            "net.cozic.joplin_desktop",
            "org.gnome.gitlab.YaLTeR.VideoTrimmer",
            "com.example-corp.App",
            "_a._b._c",
            "a.b.c",
        ):
            self.assertTrue(cf.is_app_id(good), good)

    def test_invalid_ids(self) -> None:
        for bad in (
            "",
            "firefox",
            "org.mozilla",
            "org..mozilla.firefox",
            ".org.mozilla.firefox",
            "org.mozilla.firefox.",
            "org.mozilla.1firefox",
            "1org.mozilla.firefox",
            "org.mozilla.fire-fox",
            "org.mozilla.fire fox",
            "org.mozilla.firefox\n",
            "org/mozilla/firefox",
            "org.mozilla.fire$fox",
            "org.mozilla.firefox --user",
            "--org.mozilla.firefox",
            "org.mozilla.fïrefox",
            "org.mozilla." + "a" * 300,
            None,
            42,
        ):
            self.assertFalse(cf.is_app_id(bad), repr(bad))

    def test_unicode_digits_and_letters_are_not_ids(self) -> None:
        self.assertFalse(cf.is_app_id("org.example.Аpp"))  # Cyrillic A
        self.assertFalse(cf.is_app_id("org.example.٣app"))  # Arabic-Indic digit


class AppLists(unittest.TestCase):
    def list_errors(self, text: str, name: str = "flatpaks.list") -> list[str]:
        return errors(one_file(self, f"{DEFAULTS}/{name}", text))

    def test_comments_blank_lines_and_crlf_are_fine(self) -> None:
        text = "# one\r\n\r\norg.example.Browser # trailing\r\n   org.example.Editor   \r\n"
        self.assertEqual(self.list_errors(text), [])

    def test_empty_list_is_fine(self) -> None:
        self.assertEqual(self.list_errors("# nothing yet\n"), [])
        self.assertEqual(self.list_errors(""), [])

    def test_bad_id_has_its_line_number(self) -> None:
        found = self.list_errors("org.example.Browser\nbrowser\n")
        self.assertTrue(has(found, "line 2", "'browser'", "reverse-DNS"))

    def test_two_ids_on_one_line_would_be_glued(self) -> None:
        found = self.list_errors("org.example.Browser org.example.Editor\n")
        self.assertTrue(has(found, "line 1", "glue"))

    def test_option_in_a_list_is_refused(self) -> None:
        found = self.list_errors("--no-gpg-verify\n--user\n")
        self.assertEqual(sum("as an option" in line for line in found), 2)

    def test_branch_must_be_stable(self) -> None:
        self.assertEqual(self.list_errors("org.example.Browser//stable\n"), [])
        self.assertEqual(self.list_errors("app/org.example.Browser/x86_64/stable\n"), [])
        self.assertEqual(self.list_errors("app/org.example.Browser//stable\n"), [])
        for bad in ("org.example.Browser//master", "app/org.example.Browser/x86_64/beta"):
            found = self.list_errors(bad + "\n")
            self.assertTrue(has(found, "expected 'stable'"), bad)

    def test_malformed_refs(self) -> None:
        for bad in (
            "org.example.Browser/stable",
            "runtime/org.example.Browser/x86_64/stable",
            "app/org.example.Browser/riscv/stable",
            "org.example.Browser///stable",
        ):
            self.assertTrue(has(self.list_errors(bad + "\n"), "not an app id or ref"), bad)

    def test_duplicate_id(self) -> None:
        found = self.list_errors("org.example.Browser\n# x\norg.example.Browser\n")
        self.assertTrue(has(found, "line 3", "listed twice", "line 1"))

    def test_unknown_remote_in_the_file_name(self) -> None:
        found = self.list_errors("org.example.Browser\n", "flatpaks-evil.list")
        self.assertTrue(has(found, "'evil'", "not a documented one"))

    def test_remote_names_of_the_lists(self) -> None:
        self.assertEqual(cf.list_remote("flatpaks.list"), "flathub")
        self.assertEqual(cf.list_remote("flatpaks-vilna.list"), "vilna")
        self.assertEqual(cf.list_remote("flatpaks-.list"), "")
        self.assertEqual(cf.list_remote("other.list"), "")

    def test_bom_is_reported(self) -> None:
        found = self.list_errors("\ufefforg.example.Browser\n")
        self.assertTrue(has(found, "byte order mark"))

    def test_one_id_in_both_lists(self) -> None:
        tree = Tree(self, **{"flatpaks-vilna.list": "org.example.Browser\n"})
        self.assertTrue(has(errors(tree.check()), "also in", "two remotes"))

    def test_many_lines_run_in_linear_time(self) -> None:
        text = "".join(f"org.example.App{i}\n" for i in range(100_000))
        start = time.monotonic()
        problems = one_file(self, f"{DEFAULTS}/flatpaks.list", text)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(errors(problems), [])


class RepoFiles(unittest.TestCase):
    def repo_errors(self, text: str, name: str = "vilna.flatpakrepo") -> list[str]:
        return errors(one_file(self, f"{DEFAULTS}/{name}", text))

    def test_good_file(self) -> None:
        self.assertEqual(self.repo_errors(repo_file()), [])

    def test_http_url_is_refused(self) -> None:
        found = self.repo_errors(repo_file(url="http://flatpak.vilnaos.com/repo/"))
        self.assertTrue(has(found, "Url", "must be https://"))

    def test_other_schemes_are_refused(self) -> None:
        for url in (
            "file:///srv/repo/",
            "ftp://flatpak.vilnaos.com/repo/",
            "//flatpak.vilnaos.com/",
        ):
            self.assertTrue(has(self.repo_errors(repo_file(url=url)), "Url"), url)

    def test_url_missing(self) -> None:
        self.assertTrue(has(self.repo_errors(repo_file(url="")), "Url is missing"))

    def test_undocumented_host(self) -> None:
        found = self.repo_errors(repo_file(url="https://example.org/repo/"))
        self.assertTrue(has(found, "example.org", "not a documented remote"))

    def test_flathub_is_documented(self) -> None:
        self.assertEqual(self.repo_errors(repo_file(url="https://dl.flathub.org/repo/")), [])

    def test_lookalike_hosts_are_refused(self) -> None:
        for url in (
            "https://flatpak.vilnaos.com.evil.example/repo/",
            "https://flatpak.vilnaos.com@evil.example/repo/",
            "https://evil.example/flatpak.vilnaos.com/",
            "https://flatpak.vilnaos.com:99999/repo/",
        ):
            self.assertTrue(self.repo_errors(repo_file(url=url)), url)

    def test_credentials_in_url(self) -> None:
        found = self.repo_errors(repo_file(url="https://user:pw@flatpak.vilnaos.com/repo/"))
        self.assertTrue(has(found, "user name or password"))

    def test_space_in_url(self) -> None:
        found = self.repo_errors(repo_file(url="https://flatpak.vilnaos.com/re po/"))
        self.assertTrue(has(found, "space or control character"))

    def test_url_without_slash_is_a_warning(self) -> None:
        problems = one_file(
            self, f"{DEFAULTS}/vilna.flatpakrepo", repo_file(url="https://flatpak.vilnaos.com/repo")
        )
        self.assertEqual(errors(problems), [])
        self.assertTrue(has(warnings(problems), "should end with '/'"))

    def test_homepage_must_be_https(self) -> None:
        text = repo_file().replace("https://vilnaos.com/gloxi/", "http://vilnaos.com/gloxi/")
        self.assertTrue(has(self.repo_errors(text), "Homepage"))

    def test_title_missing(self) -> None:
        self.assertTrue(has(self.repo_errors(repo_file(title=None)), "Title is missing"))

    def test_wrong_group(self) -> None:
        text = repo_file().replace("[Flatpak Repo]", "[Flatpak Ref]")
        self.assertTrue(has(self.repo_errors(text), "first group must be [Flatpak Repo]"))

    def test_no_group_and_empty_file(self) -> None:
        for text in ("", "Title=x\n", "# nothing\n"):
            self.assertTrue(self.repo_errors(text), repr(text))

    def test_duplicate_key_and_bad_lines(self) -> None:
        self.assertTrue(
            has(self.repo_errors(repo_file(extra="Title=Other\n")), "duplicate key Title")
        )
        self.assertTrue(has(self.repo_errors(repo_file(extra="garbage line\n")), "not a Key=Value"))

    def test_duplicate_group(self) -> None:
        self.assertTrue(
            has(self.repo_errors(repo_file(extra="[Flatpak Repo]\nTitle=x\n")), "repeated")
        )

    def test_default_branch_must_be_stable(self) -> None:
        self.assertEqual(self.repo_errors(repo_file(extra="DefaultBranch=stable\n")), [])
        found = self.repo_errors(repo_file(extra="DefaultBranch=master\n"))
        self.assertTrue(has(found, "DefaultBranch", "expected 'stable'"))

    def test_extra_group_is_only_a_warning(self) -> None:
        problems = one_file(
            self, f"{DEFAULTS}/vilna.flatpakrepo", repo_file(extra="[Other]\nA=b\n")
        )
        self.assertEqual(errors(problems), [])
        self.assertTrue(has(warnings(problems), "unexpected group [Other]"))

    def test_crlf_file(self) -> None:
        text = repo_file().replace("\n", "\r\n")
        self.assertEqual(self.repo_errors(text), [])

    def test_no_final_newline(self) -> None:
        self.assertEqual(self.repo_errors(repo_file().rstrip("\n")), [])


class RefFiles(unittest.TestCase):
    def ref(self, **keys: str) -> str:
        base = {
            "Name": OWN,
            "Branch": "stable",
            "Title": "Demo",
            "Url": "https://flatpak.vilnaos.com/repo/",
            "RuntimeRepo": cf.FLATHUB_REPO_URL,
            "GPGKey": GOOD_KEY,
        }
        base.update(keys)
        return "[Flatpak Ref]\n" + "".join(f"{k}={v}\n" for k, v in base.items() if v != "")

    def ref_errors(self, text: str) -> list[str]:
        return errors(one_file(self, f"{APP}/{OWN}.flatpakref", text))

    def test_good_ref(self) -> None:
        self.assertEqual(self.ref_errors(self.ref()), [])

    def test_master_branch(self) -> None:
        self.assertTrue(has(self.ref_errors(self.ref(Branch="master")), "Branch", "'stable'"))

    def test_name_must_be_an_id(self) -> None:
        self.assertTrue(has(self.ref_errors(self.ref(Name="demo")), "Name 'demo'"))

    def test_runtime_repo_must_be_flathub(self) -> None:
        found = self.ref_errors(self.ref(RuntimeRepo="https://flatpak.vilnaos.com/x.flatpakrepo"))
        self.assertTrue(has(found, "RuntimeRepo"))
        found = self.ref_errors(
            self.ref(RuntimeRepo="http://dl.flathub.org/repo/flathub.flatpakrepo")
        )
        self.assertTrue(has(found, "RuntimeRepo", "https"))


class GpgKeys(unittest.TestCase):
    def key_errors(self, value: str | None) -> list[str]:
        found = errors(one_file(self, f"{DEFAULTS}/vilna.flatpakrepo", repo_file(key=value)))
        return [e for e in found if "GPGKey" in e]

    def test_good_key(self) -> None:
        self.assertEqual(self.key_errors(GOOD_KEY), [])

    def test_missing_key_is_an_error(self) -> None:
        found = errors(one_file(self, f"{DEFAULTS}/vilna.flatpakrepo", repo_file(key=None)))
        self.assertTrue(has(found, "GPGKey is missing", "without a signature check"))

    def test_empty_key(self) -> None:
        found = errors(one_file(self, f"{DEFAULTS}/vilna.flatpakrepo", repo_file(key="")))
        self.assertTrue(has(found, "GPGKey is missing"))

    def test_not_base64(self) -> None:
        for bad in ("not base64!", "%%%%", "abc", GOOD_KEY[:-1], GOOD_KEY + "="):
            self.assertTrue(has(self.key_errors(bad), "base64"), bad)

    def test_ascii_armor_is_refused(self) -> None:
        armor = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
        self.assertTrue(has(self.key_errors(armor), "ASCII-armored"))

    def test_white_space_inside(self) -> None:
        found = self.key_errors(GOOD_KEY[:20] + " " + GOOD_KEY[20:])
        self.assertTrue(has(found, "white space"))

    def test_secret_key_is_refused(self) -> None:
        secret = packet(5, ed25519_body() + b"\x00" * 40) + packet(13, b"Vilna")
        self.assertTrue(has(self.key_errors(b64(secret)), "SECRET"))

    def test_secret_subkey_after_a_public_key_is_refused(self) -> None:
        blob = key_blob(extra=packet(7, ed25519_body(2) + b"\x00" * 10))
        self.assertTrue(has(self.key_errors(b64(blob)), "SECRET"))

    def test_not_a_key_at_all(self) -> None:
        for data in (b"hello world", b"\x00" * 50, b"\xff" * 10):
            self.assertTrue(self.key_errors(b64(data)), data)

    def test_empty_decoded_key(self) -> None:
        self.assertTrue(has(self.key_errors("===="), "GPGKey"))

    def test_truncated_key(self) -> None:
        blob = key_blob()
        for cut in (1, 2, 5, 30, len(blob) - 1):
            self.assertTrue(self.key_errors(b64(blob[:cut])), cut)

    def test_trailing_garbage(self) -> None:
        self.assertTrue(self.key_errors(b64(key_blob() + b"\x01\x02\x03")))

    def test_first_packet_must_be_the_public_key(self) -> None:
        blob = packet(13, b"Vilna") + packet(6, ed25519_body())
        self.assertTrue(has(self.key_errors(b64(blob)), "first packet"))

    def test_two_primary_keys(self) -> None:
        blob = key_blob() + packet(6, ed25519_body(7)) + packet(13, b"Other")
        self.assertTrue(has(self.key_errors(b64(blob)), "more than one primary key"))

    def test_no_user_id(self) -> None:
        self.assertTrue(has(self.key_errors(b64(key_blob(user=False))), "no User ID"))

    def test_unexpected_packet_type(self) -> None:
        blob = key_blob(extra=packet(1, b"\x03" * 20))
        self.assertTrue(has(self.key_errors(b64(blob)), "packet type(s) [1]"))

    def test_old_key_versions(self) -> None:
        body = b"\x03" + ed25519_body()[1:]
        self.assertTrue(has(self.key_errors(b64(key_blob(body))), "version 3"))

    def test_algorithm_that_cannot_sign(self) -> None:
        body = bytearray(ed25519_body())
        body[5] = 18  # ECDH, encryption only
        self.assertTrue(has(self.key_errors(b64(key_blob(bytes(body)))), "cannot sign"))

    def test_rsa_sizes(self) -> None:
        self.assertEqual(self.key_errors(b64(key_blob(rsa_body(2048)))), [])
        self.assertEqual(self.key_errors(b64(key_blob(rsa_body(4096)))), [])
        self.assertTrue(has(self.key_errors(b64(key_blob(rsa_body(1024)))), "1024 bits"))
        self.assertTrue(has(self.key_errors(b64(key_blob(rsa_body(512)))), "512 bits"))

    def test_new_format_packets_and_long_lengths(self) -> None:
        blob = new_packet(6, ed25519_body()) + new_packet(13, b"Vilna " * 40)
        blob += new_packet(2, b"\x04" * 300)
        self.assertEqual(self.key_errors(b64(blob)), [])

    def test_partial_length_is_refused(self) -> None:
        blob = bytes([0xC0 | 6, 0xE0]) + b"\x00" * 10
        self.assertTrue(has(self.key_errors(b64(blob)), "partial length"))

    def test_indeterminate_length_is_refused(self) -> None:
        blob = bytes([0x80 | 6 << 2 | 3]) + ed25519_body()
        self.assertTrue(has(self.key_errors(b64(blob)), "indeterminate"))

    def test_length_beyond_the_data(self) -> None:
        blob = bytes([0x98, 200]) + ed25519_body()
        self.assertTrue(has(self.key_errors(b64(blob)), "says 200 bytes"))

    def test_key_too_big(self) -> None:
        blob = key_blob(extra=packet(2, b"\x00" * 255) * 300)
        self.assertGreater(len(blob), cf.MAX_KEY_BYTES)
        self.assertTrue(has(self.key_errors(b64(blob)), "export only the signing key"))

    def test_huge_value_is_cut_short(self) -> None:
        self.assertTrue(has(self.key_errors("A" * 1_000_000), "far too long"))

    def test_fingerprint_v4(self) -> None:
        info = cf.check_key_bytes(key_blob())
        self.assertEqual((info.fingerprint, info.version, info.algorithm), (GOOD_FPR, 4, 22))

    def test_fingerprint_of_the_real_repository_key(self) -> None:
        text = (ROOT / DEFAULTS / "vilna.flatpakrepo").read_text(encoding="utf-8")
        value = next(line for line in text.splitlines() if line.startswith("GPGKey=")).split(
            "=", 1
        )[1]
        info = cf.check_key_bytes(cf.decode_gpgkey(value))
        # gpg --show-keys prints the same fingerprint for this key.
        self.assertEqual(info.fingerprint, "B1932C41A5D58E73F12C8A7825496EF9222B4D2C")
        self.assertEqual(info.version, 4)


class Script(unittest.TestCase):
    def script_errors(self, text: str) -> list[str]:
        return errors(one_file(self, f"{DEFAULTS}/vilna-flatpak-defaults.sh", text))

    def test_good_script(self) -> None:
        self.assertEqual(self.script_errors(SCRIPT), [])

    def test_no_gpg_verify(self) -> None:
        text = SCRIPT.replace("--if-not-exists", "--if-not-exists --no-gpg-verify")
        self.assertTrue(has(self.script_errors(text), "--no-gpg-verify"))

    def test_http_address(self) -> None:
        text = SCRIPT.replace("https://dl.flathub.org", "http://dl.flathub.org")
        found = self.script_errors(text)
        self.assertTrue(has(found, "is not https"))

    def test_undocumented_address(self) -> None:
        text = SCRIPT.replace(
            "https://dl.flathub.org/repo/flathub.flatpakrepo", "https://evil.example/x.flatpakrepo"
        )
        found = self.script_errors(text)
        self.assertTrue(has(found, "evil.example", "not a documented remote"))
        self.assertTrue(has(found, "flathub address is not"))

    def test_flathub_with_a_different_documented_path(self) -> None:
        text = SCRIPT.replace("/repo/flathub.flatpakrepo", "/repo/other.flatpakrepo")
        self.assertTrue(has(self.script_errors(text), "flathub address is not"))

    def test_unknown_remote(self) -> None:
        text = SCRIPT + 'install_from evil "$vilna_repo" "$SHARE/flatpaks-evil.list"\n'
        self.assertTrue(has(self.script_errors(text), "remote 'evil'"))

    def test_list_of_another_remote(self) -> None:
        text = SCRIPT.replace('"$SHARE/flatpaks-vilna.list"', '"$SHARE/flatpaks.list"')
        self.assertTrue(has(self.script_errors(text), "is not the list of remote vilna"))

    def test_not_a_list_file(self) -> None:
        text = SCRIPT.replace('"$SHARE/flatpaks-vilna.list"', '"$SHARE/apps.txt"')
        self.assertTrue(has(self.script_errors(text), "not a .list file"))

    def test_install_from_without_arguments(self) -> None:
        self.assertTrue(has(self.script_errors(SCRIPT + "install_from flathub\n"), "needs REMOTE"))

    def test_branch_option(self) -> None:
        for flag in ("--branch=master", "--branch master"):
            text = SCRIPT + f'"$FLATPAK" install {flag} flathub x\n'
            self.assertTrue(has(self.script_errors(text), "expected 'stable'"), flag)
        self.assertEqual(self.script_errors(SCRIPT + '"$FLATPAK" install --branch=stable x\n'), [])

    def test_comments_do_not_count(self) -> None:
        text = SCRIPT + "# remote-add --no-gpg-verify http://evil.example/\n"
        self.assertEqual(self.script_errors(text), [])

    def test_unbalanced_quote_does_not_crash(self) -> None:
        self.assertEqual(self.script_errors(SCRIPT + 'echo "open\n'), [])


def without_install(spec: str, name: str) -> str:
    """The spec without the ``install`` line that puts ``name`` into /usr/share/vilna."""
    keep = [
        x for x in spec.splitlines() if not (x.startswith("install") and x.endswith(f"/{name}"))
    ]
    assert len(keep) == len(spec.splitlines()) - 1
    return "\n".join(keep) + "\n"


class Spec(unittest.TestCase):
    def test_fingerprint_must_match_the_key(self) -> None:
        other = "0" * 40
        tree = Tree(self, **{"vilna-flatpak-defaults.spec": SPEC.replace(GOOD_FPR, other)})
        self.assertTrue(has(errors(tree.check()), "fingerprint", GOOD_FPR, other))

    def test_fingerprint_case_does_not_matter(self) -> None:
        tree = Tree(
            self, **{"vilna-flatpak-defaults.spec": SPEC.replace(GOOD_FPR, GOOD_FPR.lower())}
        )
        self.assertEqual(tree.check(), [])

    def test_spec_without_a_fingerprint_is_fine(self) -> None:
        tree = Tree(self, **{"vilna-flatpak-defaults.spec": SPEC.replace(GOOD_FPR, "unknown")})
        self.assertEqual(tree.check(), [])

    def test_script_list_not_installed_by_the_spec(self) -> None:
        text = without_install(SPEC, "flatpaks-vilna.list")
        tree = Tree(self, **{"vilna-flatpak-defaults.spec": text})
        self.assertTrue(
            has(errors(tree.check()), "reads flatpaks-vilna.list", "does not install it")
        )

    def test_script_repofile_not_installed_by_the_spec(self) -> None:
        text = without_install(SPEC, "vilna.flatpakrepo")
        tree = Tree(self, **{"vilna-flatpak-defaults.spec": text})
        self.assertTrue(has(errors(tree.check()), "reads vilna.flatpakrepo"))

    def test_install_of_an_undeclared_source(self) -> None:
        text = SPEC + "install -D -m 0644 %{SOURCE9} %{buildroot}%{_datadir}/vilna/x.list\n"
        tree = Tree(self, **{"vilna-flatpak-defaults.spec": text})
        self.assertTrue(has(errors(tree.check()), "SOURCE9", "not declared"))

    def test_line_continuation_in_install(self) -> None:
        text = SPEC.replace(
            "install -D -m 0644 %{SOURCE3} %{buildroot}",
            "install -D -m 0644 %{SOURCE3} \\\n    %{buildroot}",
        )
        tree = Tree(self, **{"vilna-flatpak-defaults.spec": text})
        self.assertEqual(tree.check(), [])


class Picks(unittest.TestCase):
    def pick_errors(self, text: str) -> list[str]:
        return errors(one_file(self, f"{PICKS_DIR}/picks.toml", text))

    def with_app(self, **fields: object) -> str:
        app = {"id": "org.example.New", "source": "flathub-verified", "default": False}
        app.update(fields)
        body = "".join(
            f"{k} = {v!r}\n".replace("'", '"').replace("True", "true").replace("False", "false")
            for k, v in app.items()
        )
        return PICKS + "\n[[app]]\n" + body

    def test_good(self) -> None:
        self.assertEqual(self.pick_errors(PICKS), [])

    def test_invalid_toml(self) -> None:
        self.assertTrue(has(self.pick_errors("[[app]\nid="), "not valid TOML"))

    def test_app_is_not_an_array(self) -> None:
        self.assertTrue(has(self.pick_errors('app = "x"\n'), "array of tables"))

    def test_bad_ids(self) -> None:
        for bad in ("browser", "org.example", "org.example.1x", "org.example.a b", ""):
            found = self.pick_errors(self.with_app(id=bad))
            self.assertTrue(has(found, "not a reverse-DNS"), bad)

    def test_id_of_the_wrong_type(self) -> None:
        text = PICKS + '\n[[app]]\nid = 5\nsource = "vilna"\n'
        self.assertTrue(has(self.pick_errors(text), "not a reverse-DNS"))

    def test_missing_id(self) -> None:
        text = PICKS + '\n[[app]]\nsource = "vilna"\n'
        self.assertTrue(has(self.pick_errors(text), "not a reverse-DNS"))

    def test_duplicate_id(self) -> None:
        self.assertTrue(
            has(self.pick_errors(self.with_app(id="org.example.Editor")), "listed twice")
        )

    def test_unknown_source(self) -> None:
        found = self.pick_errors(self.with_app(source="aur"))
        self.assertTrue(has(found, "source 'aur'"))

    def test_vilna_source_needs_our_namespace(self) -> None:
        self.assertTrue(
            has(self.pick_errors(self.with_app(source="vilna")), "source vilna but not in")
        )

    def test_our_app_with_a_flathub_source(self) -> None:
        found = self.pick_errors(self.with_app(id="io.github.stdenis1980.Other"))
        self.assertTrue(has(found, "our own app with a Flathub source"))

    def test_branch_and_remote(self) -> None:
        self.assertEqual(self.pick_errors(self.with_app(branch="stable", remote="flathub")), [])
        self.assertTrue(has(self.pick_errors(self.with_app(branch="beta")), "branch 'beta'"))
        self.assertTrue(has(self.pick_errors(self.with_app(remote="evil")), "remote 'evil'"))
        self.assertTrue(
            has(self.pick_errors(self.with_app(remote="vilna")), "does not match source")
        )

    def test_default_must_be_a_boolean(self) -> None:
        text = (
            PICKS
            + '\n[[app]]\nid = "org.example.New"\nsource = "flathub-verified"\ndefault = "yes"\n'
        )
        self.assertTrue(has(self.pick_errors(text), "default must be true or false"))


class CrossChecks(unittest.TestCase):
    def test_default_pick_must_be_in_its_list(self) -> None:
        tree = Tree(self, **{"flatpaks.list": "org.example.Editor\n"})
        self.assertTrue(has(errors(tree.check()), "org.example.Browser", "not in the flathub list"))

    def test_vilna_default_pick_must_be_in_the_vilna_list(self) -> None:
        tree = Tree(self, **{"flatpaks-vilna.list": "# none\n"})
        self.assertTrue(has(errors(tree.check()), "Office", "not in the vilna list"))

    def test_installed_app_must_be_marked_default(self) -> None:
        tree = Tree(
            self,
            **{"flatpaks.list": "org.example.Browser\norg.example.Editor\norg.example.Extra\n"},
        )
        self.assertTrue(has(errors(tree.check()), "org.example.Extra", "set default = true"))

    def test_app_from_the_wrong_remote(self) -> None:
        tree = Tree(
            self, **{"flatpaks-vilna.list": "io.github.stdenis1980.Office\norg.example.Editor\n"}
        )
        found = errors(tree.check())
        self.assertTrue(has(found, "org.example.Editor", "vilna list", "flathub-verified"))

    def test_mimeapps_ids_must_be_valid(self) -> None:
        tree = Tree(
            self, **{"gnome-mimeapps.list": "[Default Applications]\ntext/csv=tintacalc.desktop\n"}
        )
        self.assertTrue(has(errors(tree.check()), "'tintacalc'"))

    def test_mimeapps_without_desktop_suffix(self) -> None:
        tree = Tree(
            self, **{"gnome-mimeapps.list": "[Default Applications]\ntext/csv=org.example.Editor\n"}
        )
        self.assertTrue(has(errors(tree.check()), "not a .desktop id"))

    def test_mimeapps_for_our_app_nobody_installs_is_a_warning(self) -> None:
        text = "[Default Applications]\ntext/csv=io.github.stdenis1980.Ghost.desktop\n"
        tree = Tree(self, **{"gnome-mimeapps.list": text})
        problems = tree.check()
        self.assertEqual(errors(problems), [])
        self.assertTrue(has(warnings(problems), "Ghost", "no list installs it"))

    def test_other_groups_in_mimeapps_are_not_checked(self) -> None:
        text = "[Added Associations]\ntext/csv=junk\n[Default Applications]\n"
        tree = Tree(self, **{"gnome-mimeapps.list": text})
        self.assertEqual(tree.check(), [])

    def test_files_given_alone_are_checked_alone(self) -> None:
        tree = Tree(self)
        self.assertEqual(tree.check([f"{DEFAULTS}/flatpaks.list"]), [])
        self.assertEqual(tree.check([f"{PICKS_DIR}/picks.toml"]), [])


class Manifests(unittest.TestCase):
    def manifest_errors(self, text: str, name: str = f"{OWN}.yml") -> list[cf.Problem]:
        return one_file(self, f"{APP}/{name}", text)

    def test_good_manifest(self) -> None:
        self.assertEqual(self.manifest_errors(MANIFEST), [])

    def test_missing_branch_is_the_legacy_rule(self) -> None:
        text = MANIFEST.replace("branch: stable\n", "")
        found = self.manifest_errors(text)
        self.assertTrue(has(errors(found), "no 'branch:'"))
        self.assertEqual(found[0].rule, "branch-missing")

    def test_wrong_branch(self) -> None:
        found = errors(self.manifest_errors(MANIFEST.replace("branch: stable", "branch: master")))
        self.assertTrue(has(found, "branch 'master'", "'stable'"))

    def test_quoted_branch(self) -> None:
        self.assertEqual(
            self.manifest_errors(MANIFEST.replace("branch: stable", 'branch: "stable"')), []
        )

    def test_comment_after_branch(self) -> None:
        self.assertEqual(
            self.manifest_errors(MANIFEST.replace("branch: stable", "branch: stable # ok")), []
        )

    def test_id_not_ours(self) -> None:
        text = MANIFEST.replace(OWN, "com.example.Demo")
        found = errors(self.manifest_errors(text))
        self.assertTrue(has(found, "not in our namespace"))

    def test_id_differs_from_the_file_name(self) -> None:
        found = errors(self.manifest_errors(MANIFEST, name="io.github.stdenis1980.Other.yml"))
        self.assertTrue(has(found, "differs from the file name"))

    def test_bad_id(self) -> None:
        found = errors(self.manifest_errors(MANIFEST.replace(f"id: {OWN}", "id: demo")))
        self.assertTrue(has(found, "id 'demo'"))

    def test_no_id(self) -> None:
        found = errors(self.manifest_errors(MANIFEST.replace(f"id: {OWN}\n", "")))
        self.assertTrue(has(found, "no 'id'"))

    def test_app_id_key_is_understood(self) -> None:
        text = MANIFEST.replace(f"id: {OWN}", f"app-id: {OWN}")
        self.assertEqual(self.manifest_errors(text), [])

    def test_http_source(self) -> None:
        text = MANIFEST.replace("https://downloads", "http://downloads")
        self.assertTrue(
            has(errors(self.manifest_errors(text)), "source #1 url", "must be https://")
        )

    def test_source_without_a_checksum(self) -> None:
        text = MANIFEST.replace(f'        sha256: "{"a" * 64}"\n', "")
        self.assertTrue(has(errors(self.manifest_errors(text)), "has no sha256"))

    def test_bad_checksums(self) -> None:
        for bad in ("abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65):
            text = MANIFEST.replace("a" * 64, bad)
            self.assertTrue(has(errors(self.manifest_errors(text)), "sha256 is not 64"), bad)

    def test_sha1_is_not_a_checksum(self) -> None:
        text = MANIFEST.replace("sha256:", "sha1:")
        self.assertTrue(has(errors(self.manifest_errors(text)), "md5 and sha1"))

    def test_sha512_is_accepted(self) -> None:
        text = MANIFEST.replace(f'sha256: "{"a" * 64}"', f'sha512: "{"b" * 128}"')
        self.assertEqual(self.manifest_errors(text), [])

    def test_extra_data_needs_size_and_filename(self) -> None:
        for field in ("        size: 1234\n", "        filename: demo.deb\n"):
            found = errors(self.manifest_errors(MANIFEST.replace(field, "")))
            self.assertTrue(has(found, "extra-data needs"), field)
        found = errors(self.manifest_errors(MANIFEST.replace("size: 1234", "size: 0")))
        self.assertTrue(has(found, "needs a size"))

    def test_git_source_needs_no_checksum_but_https(self) -> None:
        text = MANIFEST + "      - type: git\n        url: https://github.com/x/y.git\n"
        self.assertEqual(self.manifest_errors(text), [])
        text = MANIFEST + "      - type: git\n        url: git://github.com/x/y.git\n"
        self.assertTrue(has(errors(self.manifest_errors(text)), "must be https://"))

    def test_second_arch_source_is_checked_too(self) -> None:
        text = MANIFEST + (
            "      - type: extra-data\n        filename: d.deb\n        size: 5\n"
            "        url: https://x.example/d.deb\n"
        )
        self.assertTrue(has(errors(self.manifest_errors(text)), "source #3", "no sha256"))

    def test_siblings_must_exist(self) -> None:
        tree = Tree(self, **{f"{OWN}.desktop": None})
        self.assertTrue(has(errors(tree.check()), ".desktop is missing"))
        tree = Tree(self, **{f"{OWN}.metainfo.xml": None})
        self.assertTrue(has(errors(tree.check()), "metainfo.xml is missing"))

    def test_metainfo_id_must_match(self) -> None:
        tree = Tree(self, **{f"{OWN}.metainfo.xml": "<component><id>org.x.Y</id></component>"})
        self.assertTrue(has(errors(tree.check()), "has <id>", "org.x.Y"))

    def test_broken_metainfo_xml(self) -> None:
        tree = Tree(self, **{f"{OWN}.metainfo.xml": "<component><id>"})
        self.assertTrue(has(errors(tree.check()), "cannot be read"))

    def test_json_manifest(self) -> None:
        nested = {"sources": [{"type": "file", "url": "http://x.example/b"}]}
        extra = {
            "type": "extra-data",
            "url": "https://x.example/a.deb",
            "sha256": "c" * 64,
            "size": 3,
            "filename": "a.deb",
        }
        manifest = {
            "id": OWN,
            "branch": "stable",
            "modules": [{"name": "m", "sources": [extra], "modules": [nested]}],
        }
        found = errors(self.manifest_errors(json.dumps(manifest), name=f"{OWN}.json"))
        self.assertEqual(len(found), 2, found)  # the nested source: http and no sha256
        self.assertTrue(has(found, "source #2", "must be https://"))

    def test_invalid_and_odd_json(self) -> None:
        for text in ("{", "[]", "null", '"x"', "[" * 100_000):
            found = errors(self.manifest_errors(text, name=f"{OWN}.json"))
            self.assertTrue(found, text[:10])

    def test_nested_json_is_not_followed_forever(self) -> None:
        text = json.dumps({"id": OWN, "branch": "stable"})[:-1] + ', "modules": '
        text += '[{"modules": ' * 500 + "[]" + "}]" * 500 + "}"
        self.assertIsInstance(self.manifest_errors(text, name=f"{OWN}.json"), list)

    def test_only_thirdparty_manifests_are_classified(self) -> None:
        self.assertEqual(cf.classify(f"thirdparty/x/{OWN}.yml"), "manifest")
        self.assertIsNone(cf.classify(f"docs/{OWN}.yml"))
        self.assertIsNone(cf.classify("thirdparty/x/notes.yml"))


class HostileInput(unittest.TestCase):
    """Odd files: the check says what is wrong and never fails with a traceback."""

    FILES = (
        f"{DEFAULTS}/flatpaks.list",
        f"{DEFAULTS}/vilna.flatpakrepo",
        f"{DEFAULTS}/vilna-flatpak-defaults.sh",
        f"{DEFAULTS}/vilna-flatpak-defaults.spec",
        f"{DEFAULTS}/gnome-mimeapps.list",
        f"{PICKS_DIR}/picks.toml",
        f"{APP}/{OWN}.yml",
    )

    def test_binary_content(self) -> None:
        for rel in self.FILES:
            found = errors(one_file(self, rel, b"\x00\x01\x02binary\xff\xfe"))
            self.assertTrue(has(found, "binary"), rel)

    def test_invalid_utf8(self) -> None:
        for rel in self.FILES:
            found = errors(one_file(self, rel, b"org.example.Browser\n\xff\xfe\xfa\n"))
            self.assertTrue(has(found, "not valid UTF-8"), rel)

    def test_utf16_file(self) -> None:
        for rel in self.FILES:
            data = "org.example.Browser\n".encode("utf-16")
            self.assertTrue(errors(one_file(self, rel, data)), rel)

    def test_bom_in_every_kind(self) -> None:
        for rel in self.FILES:
            text = "\ufeff" + GOOD_FILES[rel]
            found = errors(one_file(self, rel, text))
            self.assertTrue(has(found, "byte order mark"), rel)

    def test_empty_files(self) -> None:
        for rel in self.FILES:
            one_file(self, rel, "")  # must not raise

    def test_no_final_newline_everywhere(self) -> None:
        for rel in self.FILES:
            self.assertEqual(errors(one_file(self, rel, GOOD_FILES[rel].rstrip("\n"))), [], rel)

    def test_crlf_everywhere(self) -> None:
        for rel in self.FILES:
            text = GOOD_FILES[rel].replace("\n", "\r\n")
            self.assertEqual(errors(one_file(self, rel, text)), [], rel)

    def test_missing_file(self) -> None:
        tree = Tree(self)
        found = errors(tree.check([f"{DEFAULTS}/flatpaks.list", f"{DEFAULTS}/flatpaks-none.list"]))
        self.assertTrue(has(found, "flatpaks-none.list", "cannot read"))

    def test_symbolic_links_are_refused(self) -> None:
        tree = Tree(self)
        outside = tree.root / "outside.txt"
        outside.write_text("org.example.Browser\n", encoding="utf-8")
        link = tree.root / DEFAULTS / "flatpaks-extra.list"
        link.symlink_to(outside)
        self_link = tree.root / DEFAULTS / "flatpaks-loop.list"
        self_link.symlink_to(self_link.name)
        found = errors(
            tree.check([f"{DEFAULTS}/flatpaks-extra.list", f"{DEFAULTS}/flatpaks-loop.list"])
        )
        self.assertEqual(sum("not a regular file" in line for line in found), 2)

    def test_folder_with_a_file_name(self) -> None:
        tree = Tree(self)
        (tree.root / DEFAULTS / "flatpaks-dir.list").mkdir()
        self.assertTrue(
            has(errors(tree.check([f"{DEFAULTS}/flatpaks-dir.list"])), "not a regular file")
        )

    def test_big_file_is_refused_not_read(self) -> None:
        tree = Tree(self)
        big = tree.root / DEFAULTS / "flatpaks.list"
        with open(big, "wb") as handle:
            handle.truncate(cf.MAX_FILE_BYTES + 1)  # a sparse file: nothing is written
        found = errors(tree.check([f"{DEFAULTS}/flatpaks.list"]))
        self.assertTrue(has(found, "the limit is"))

    def test_names_with_spaces_and_unicode(self) -> None:
        tree = Tree(self)
        name = f"{DEFAULTS}/flatpaks-vilna.list"
        weird = tree.root / "обс пакет" / "flatpaks.list"
        weird.parent.mkdir()
        weird.write_text("org.example.Browser\n", encoding="utf-8")
        self.assertEqual(tree.check([name, "обс пакет/flatpaks.list"]), [])

    def test_control_characters_and_long_lines(self) -> None:
        text = "org.example.Browser\x07\n" + "x" * 100_000 + "\n"
        found = errors(one_file(self, f"{DEFAULTS}/flatpaks.list", text))
        self.assertEqual(len(found), 2)
        self.assertLess(max(len(line) for line in found), 300)  # a long line is not echoed whole

    def test_long_repo_file_and_many_groups(self) -> None:
        text = "".join(f"[G{i}]\nA=b\n" for i in range(50_000))
        start = time.monotonic()
        one_file(self, f"{DEFAULTS}/vilna.flatpakrepo", text)
        self.assertLess(time.monotonic() - start, 10)

    def test_many_picks_run_in_linear_time(self) -> None:
        apps = "".join(
            f'[[app]]\nid = "org.example.App{i}"\nsource = "flathub-verified"\ndefault = false\n'
            for i in range(20_000)
        )
        start = time.monotonic()
        problems = one_file(self, f"{PICKS_DIR}/picks.toml", apps)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(errors(problems), [])

    def test_deeply_nested_toml(self) -> None:
        found = errors(one_file(self, f"{PICKS_DIR}/picks.toml", "a = " + "[" * 100_000))
        self.assertTrue(has(found, "not valid TOML") or found)

    def test_crazy_yaml_manifest(self) -> None:
        text = (
            "id: "
            + OWN
            + "\n"
            + "  - - - - x\n" * 50_000
            + "sources:\n"
            + "      - type: a\n" * 50_000
        )
        one_file(self, f"{APP}/{OWN}.yml", text)  # must not raise

    def test_odd_shell(self) -> None:
        for text in (
            "install_from",
            "install_from '",
            "\\\n\\\n",
            "$(",
            "((",
            "`",
            "install_from a b",
        ):
            one_file(self, f"{DEFAULTS}/vilna-flatpak-defaults.sh", text)


class Legacy(unittest.TestCase):
    def test_legacy_downgrades_to_a_warning(self) -> None:
        path, rule = next(iter(cf.LEGACY))
        problem = cf.Problem(path, "x", rule=rule)
        self.assertTrue(cf.downgrade(problem).warning)

    def test_other_rule_of_a_legacy_file_stays_an_error(self) -> None:
        path, _ = next(iter(cf.LEGACY))
        self.assertFalse(cf.downgrade(cf.Problem(path, "x", rule="branch-not-stable")).warning)
        self.assertFalse(cf.downgrade(cf.Problem(path, "x")).warning)

    def test_other_file_with_the_legacy_rule_stays_an_error(self) -> None:
        _, rule = next(iter(cf.LEGACY))
        self.assertFalse(cf.downgrade(cf.Problem("other.yml", "x", rule=rule)).warning)

    def test_every_legacy_entry_still_applies(self) -> None:
        # A fixed file must lose its LEGACY line: the entry has to match a real problem.
        problems, _ = cf.check_files(ROOT, cf.tracked_files(ROOT))
        seen = {(p.path, p.rule) for p in problems if p.warning}
        self.assertEqual(seen, set(cf.LEGACY))


class RealRepo(unittest.TestCase):
    def test_no_errors_on_the_repo(self) -> None:
        problems, count = cf.check_files(ROOT, cf.tracked_files(ROOT))
        self.assertEqual(errors(problems), [])
        self.assertEqual(count, 8)

    def test_main_on_the_repo(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cf.main([])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("0 errors", out.getvalue())

    def test_picks_agree_with_the_shipped_lists(self) -> None:
        # The check's own cross rules, run on the real files, are what CI relies on.
        problems, _ = cf.check_files(ROOT, cf.tracked_files(ROOT))
        self.assertFalse([p for p in problems if "default" in p.message])

    def test_repository_file_is_https_with_a_key(self) -> None:
        problems, _ = cf.check_files(ROOT, [f"{DEFAULTS}/vilna.flatpakrepo"])
        self.assertEqual(problems, [])


class Main(unittest.TestCase):
    def run_main(self, args: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old = Path.cwd()
        try:
            if cwd is not None:
                os.chdir(cwd)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cf.main(args)
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()

    def test_files_from_the_command_line(self) -> None:
        tree = Tree(self)
        code, out, _ = self.run_main([f"{DEFAULTS}/flatpaks.list"], cwd=tree.root)
        self.assertEqual(code, 0)
        self.assertIn("1 Flatpak files checked: 0 errors, 0 warnings", out)

    def test_exit_1_on_errors(self) -> None:
        tree = Tree(self, **{"flatpaks.list": "bad\n"})
        code, out, err = self.run_main([f"{DEFAULTS}/flatpaks.list"], cwd=tree.root)
        self.assertEqual(code, 1)
        self.assertIn("1 errors", out)
        self.assertIn("flatpaks.list: line 1", err)

    def test_exit_0_with_only_warnings(self) -> None:
        tree = Tree(
            self, **{"vilna.flatpakrepo": repo_file(url="https://flatpak.vilnaos.com/repo")}
        )
        code, out, err = self.run_main([f"{DEFAULTS}/vilna.flatpakrepo"], cwd=tree.root)
        self.assertEqual(code, 0)
        self.assertIn("warning:", err)
        self.assertIn("0 errors, 1 warnings", out)

    def test_exit_2_for_unknown_options_and_no_files(self) -> None:
        self.assertEqual(self.run_main(["--nope"])[0], 2)
        tree = Tree(self)
        tree.write("README.md", "x")
        code, _, err = self.run_main(["README.md"], cwd=tree.root)
        self.assertEqual(code, 2)
        self.assertIn("no Flatpak default files found", err)

    def test_help_exits_0(self) -> None:
        for flag in ("-h", "--help"):
            code, _, err = self.run_main([flag])
            self.assertEqual(code, 0)
            self.assertIn("usage:", err)

    def test_missing_file_is_an_error_not_a_crash(self) -> None:
        tree = Tree(self)
        code, _, err = self.run_main([f"{DEFAULTS}/gone.flatpakrepo"], cwd=tree.root)
        self.assertEqual(code, 1)
        self.assertIn("cannot read the file", err)

    def test_nothing_is_written(self) -> None:
        tree = Tree(self)
        before = sorted(p.relative_to(tree.root) for p in tree.root.rglob("*"))
        self.run_main(sorted(GOOD_FILES), cwd=tree.root)
        after = sorted(p.relative_to(tree.root) for p in tree.root.rglob("*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
