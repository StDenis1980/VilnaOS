# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/flatpak_repo.py: the files that point at the Vilna Flatpak repository (ADR 0024)."""

from __future__ import annotations

import base64
import configparser
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "flatpak_repo.py"
# Not a real key: just bytes that start like the public key gpg --export writes.
FAKE_KEY = bytes([0x99, 0x01, 0x0D]) + b"\x04" * 40


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("flatpak_repo_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def key(tmp_path: Path) -> Path:
    path = tmp_path / "key.gpg"
    path.write_bytes(FAKE_KEY)
    return path


def _ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # Flatpak keys are case-sensitive
    parser.read(path, encoding="utf-8")
    return parser


def test_writes_both_files_with_the_key(tool, key, tmp_path):
    out = tmp_path / "out"
    repo, ref = tool.write(out, "https://flatpak.example.org/repo", key)
    expected_key = base64.b64encode(FAKE_KEY).decode()

    r = _ini(repo)["Flatpak Repo"]
    assert r["Url"] == "https://flatpak.example.org/repo/"
    assert r["Title"] == "Vilna"
    assert r["GPGKey"] == expected_key

    f = _ini(ref)["Flatpak Ref"]
    assert ref.name == "io.github.stdenis1980.Gloxi.flatpakref"
    assert f["Name"] == "io.github.stdenis1980.Gloxi"
    assert f["Branch"] == "master"
    assert f["Url"] == "https://flatpak.example.org/repo/"
    assert f["SuggestRemoteName"] == "vilna"
    assert f["IsRuntime"] == "false"
    assert f["RuntimeRepo"] == "https://dl.flathub.org/repo/flathub.flatpakrepo"
    assert f["GPGKey"] == expected_key


def test_the_app_id_matches_the_manifest(tool):
    manifest = ROOT / "flatpak" / f"{tool.APP_ID}.yml"
    assert manifest.is_file()
    assert f"id: {tool.APP_ID}\n" in manifest.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "url", ["http://example.org/repo/", "file:///tmp/repo/", "example.org/repo", "https://a b/"]
)
def test_only_https_for_real_files(tool, key, tmp_path, url):
    with pytest.raises(tool.RepoError):
        tool.write(tmp_path, url, key)


def test_file_url_only_for_the_ci_check(tool, key, tmp_path):
    _, ref = tool.write(tmp_path, "file:///srv/repo", key, test=True)
    assert _ini(ref)["Flatpak Ref"]["Url"] == "file:///srv/repo/"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n...",
        b"-----BEGIN PGP PRIVATE KEY BLOCK-----\n...",
        bytes([0x95, 0x01]) + b"\x00" * 10,  # a secret-key packet
        b"hello",
    ],
)
def test_refuses_anything_but_a_binary_public_key(tool, tmp_path, data):
    path = tmp_path / "key"
    path.write_bytes(data)
    with pytest.raises(tool.RepoError):
        tool.write(tmp_path / "out", "https://example.org/repo/", path)


def test_bad_branch(tool, key, tmp_path):
    with pytest.raises(tool.RepoError):
        tool.write(tmp_path, "https://example.org/repo/", key, branch="stable\nUrl=evil")


def test_cli(tool, key, tmp_path, capsys):
    assert (
        tool.main(["--url", "https://example.org/repo/", "--key", str(key), "--out", str(tmp_path)])
        == 0
    )
    assert "vilna.flatpakrepo" in capsys.readouterr().out
    assert (
        tool.main(["--url", "http://example.org/", "--key", str(key), "--out", str(tmp_path)]) == 1
    )
    assert "https://" in capsys.readouterr().err
