# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/upload_bunny.py: the Vilna Flatpak repository goes to bunny.net, summary last."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "upload_bunny.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("upload_bunny_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    out = tmp_path / "vilna-flatpak"
    for rel in [
        "repo/summary",
        "repo/summary.sig",
        "repo/config",
        "repo/refs/heads/app/io.github.stdenis1980.Gloxi/x86_64/master",
        "repo/objects/ab/cdef.filez",
        "repo/deltas/xy/z/superblock",
        "io.github.stdenis1980.Gloxi.flatpakref",
        "vilna.flatpakrepo",
    ]:
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rel.encode())
    return out


def _upload(tool, repo, statuses=None):
    sent = []
    statuses = list(statuses or [])

    def send(request):
        sent.append(request)
        if statuses:
            result = statuses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return 201

    n = tool.upload(
        repo,
        "vilna-flatpak",
        "storage.bunnycdn.com",
        "secret",
        send=send,
        sleep=lambda s: None,
        log=lambda m: None,
    )
    return n, sent


def test_objects_first_summary_last(tool, repo):
    n, sent = _upload(tool, repo)
    urls = [r.full_url for r in sent]
    assert n == 8 == len(urls)
    base = "https://storage.bunnycdn.com/vilna-flatpak/"
    assert urls[-2:] == [base + "repo/summary.sig", base + "repo/summary"]
    assert set(urls[:2]) == {
        base + "repo/objects/ab/cdef.filez",
        base + "repo/deltas/xy/z/superblock",
    }
    assert base + "io.github.stdenis1980.Gloxi.flatpakref" in urls


def test_each_request_carries_the_password_and_checksum(tool, repo):
    _, sent = _upload(tool, repo)
    for request in sent:
        assert request.get_method() == "PUT"
        assert request.get_header("Accesskey") == "secret"
        assert request.get_header("Checksum") == hashlib.sha256(request.data).hexdigest().upper()


def test_retries_then_fails_without_showing_the_password(tool, repo):
    with pytest.raises(tool.UploadError) as e:
        _upload(tool, repo, [500, urllib.error.URLError("down"), 401])
    assert "secret" not in str(e.value)
    assert "401" in str(e.value)


def test_a_retry_that_works(tool, repo):
    n, sent = _upload(tool, repo, [503])
    assert n == 8 and len(sent) == 9


def test_not_a_repository(tool, tmp_path):
    (tmp_path / "x").write_text("x")
    with pytest.raises(tool.UploadError):
        tool.upload(tmp_path, "zone", "storage.bunnycdn.com", "p", send=lambda r: 201)


@pytest.mark.parametrize(
    ("zone", "endpoint", "password"),
    [
        ("", "storage.bunnycdn.com", "p"),
        ("a/b", "storage.bunnycdn.com", "p"),
        ("zone", "evil.example/x", "p"),
        ("zone", "storage.bunnycdn.com", ""),
    ],
)
def test_bad_settings(tool, repo, zone, endpoint, password):
    with pytest.raises(tool.UploadError):
        tool.upload(repo, zone, endpoint, password, send=lambda r: 201)


def test_cli_without_password(tool, repo, monkeypatch, capsys):
    monkeypatch.delenv("BUNNY_STORAGE_PASSWORD", raising=False)
    assert tool.main([str(repo), "--zone", "vilna-flatpak"]) == 1
    assert "BUNNY_STORAGE_PASSWORD" in capsys.readouterr().err
