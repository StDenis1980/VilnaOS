# SPDX-License-Identifier: Apache-2.0
"""Metadata, catalogues and lines that once fooled the consent dialog or ended a server."""

from __future__ import annotations

import gzip
import io
import json

import pytest

from vilna_mcp_apps.apps import Apps, MemoryBackend, RemoteRef, facts, permissions
from vilna_mcp_apps.appstream import Component, parse_components, read_components, read_picks
from vilna_mcp_apps.launcher import Launcher, LauncherServer
from vilna_mcp_apps.server import Server, demo_apps

TOOL = "io.github.stdenis1980.Tool"


@pytest.mark.parametrize(
    "metadata",
    [
        "[Context]\nshared=network\n sockets=x11\n filesystems=host\n",
        "[Context]\nsockets=wayland\n\tfilesystems=host\n",
    ],
)
def test_an_indented_line_is_a_permission_of_its_own(metadata):
    assert "all files on the computer" in permissions(metadata)


@pytest.mark.parametrize(
    "metadata,word",
    [
        ("[Session Bus Policy]\norg.freedesktop.Flatpak=talk\n", "org.freedesktop.Flatpak"),
        ("[System Bus Policy]\norg.freedesktop.systemd1=talk\n", "org.freedesktop.systemd1"),
        ("[Context]\nfeatures=devel;\n", "other programs"),
        ("[Environment]\nLD_PRELOAD=/tmp/x.so\n", "LD_PRELOAD"),
    ],
)
def test_buses_features_and_environment_are_shown(metadata, word):
    assert any(word in p for p in permissions(metadata))


def test_a_folder_permission_is_one_short_line():
    (line,) = permissions("[Context]\nfilesystems=/a b‮c" + "x" * 1_000_000 + "\n")
    assert len(line) < 300 and line.isprintable() and "‮" not in line


def test_an_app_name_cannot_hide_its_id_with_direction_marks():
    entry = {
        "name": "Safe‮)ppA.live.gro(rotaluclaC",
        "app_id": "org.evil.App",
        "developer": "",
        "verified": False,
        "remote": "flathub",
        "license": "",
    }
    assert "‮" not in facts(entry)["App"]


def test_a_broken_flathub_catalogue_leaves_installs_from_vilna():
    class Broken(MemoryBackend):
        def components(self, remote):
            if remote == "flathub":
                return parse_components(b"<components><component>")
            return super().components(remote)

    backend = Broken({"flathub": [], "vilna": [Component(TOOL, names={"": "Tool"})]})
    assert Apps(backend).install(TOOL, "vilna")


@pytest.mark.parametrize(
    "raw",
    [
        b'<?xml version="1.0" encoding="x-evil"?><components/>',
        gzip.compress(b"<components>" * 1000)[:50],
        b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"garbage",
    ],
)
def test_picks_that_cannot_be_read_are_skipped(tmp_path, raw):
    path = tmp_path / ("picks.xml.gz" if raw.startswith(b"\x1f\x8b") else "picks.xml")
    path.write_bytes(raw)
    assert read_picks([path]) == {}


def test_a_huge_plain_catalogue_is_not_read_whole(tmp_path, monkeypatch):
    from vilna_mcp_apps import appstream

    monkeypatch.setattr(appstream, "MAX_APPSTREAM_BYTES", 1000)
    path = tmp_path / "appstream.xml"
    path.write_bytes(b"<components>" + b" " * 10_000 + b"</components>")
    with pytest.raises(ValueError):
        list(read_components(path))


def test_languages_of_one_name_are_bounded():
    names = "".join(f'<name xml:lang="l{i}">n</name>' for i in range(5000))
    raw = f"<components><component><id>{TOOL}</id>{names}</component></components>"
    (c,) = parse_components(raw.encode())
    assert len(c.names) < 5000


@pytest.mark.parametrize(
    "line",
    [
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":[1]}}',
        "[" * 100_000,
        '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        '"params":{"name":"describe","arguments":{"app_id":"\\ud800"}}}',
    ],
)
@pytest.mark.parametrize("which", ["apps", "launcher"])
def test_one_line_never_ends_a_server(line, which):
    server = (
        Server(demo_apps()) if which == "apps" else LauncherServer(Launcher([], lambda c: None))
    )
    ping = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    out = io.StringIO()
    server.serve(io.StringIO(line + "\n" + ping + "\n"), out)
    out.getvalue().encode("utf-8")
    assert json.loads(out.getvalue().splitlines()[-1])["result"] == {}


def test_a_long_start_error_is_cut(monkeypatch):
    import subprocess

    from vilna_mcp_apps import launcher

    def run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "systemd-run", stderr=b"x" * 5_000_000)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(launcher.AppsError) as e:
        launcher._systemd_run(["flatpak", "run", TOOL])
    assert len(str(e.value)) < 5000


def test_remote_ref_metadata_is_read_by_describe():
    backend = MemoryBackend({"flathub": [Component(TOOL)], "vilna": []})
    backend._refs[("flathub", TOOL)] = RemoteRef("c", 1, 1, "[Context]\n filesystems=host\n")
    assert "all files" in Apps(backend).describe(TOOL, "flathub")["facts"]["Can reach"]
