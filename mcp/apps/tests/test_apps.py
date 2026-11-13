# SPDX-License-Identifier: Apache-2.0
import gzip
import io
import json
from pathlib import Path

import pytest

from vilna_mcp_apps.apps import (
    Apps,
    AppsError,
    Installed,
    MemoryBackend,
    RemoteRef,
    facts,
    human_size,
    permissions,
)
from vilna_mcp_apps.appstream import Component, is_app_id, read_components, read_picks
from vilna_mcp_apps.launcher import Launcher, LauncherServer
from vilna_mcp_apps.server import TOOLS, Server, demo_apps

REPO = Path(__file__).resolve().parents[3]
PICKS = REPO / "obs/packages/vilna-software-picks"

FLATHUB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<components version="0.8" origin="flathub">
  <component type="desktop-application">
    <id>org.kde.kdenlive</id>
    <name>Kdenlive</name>
    <name xml:lang="uk">Kdenlive</name>
    <summary>Video editor</summary>
    <summary xml:lang="uk">Відеоредактор</summary>
    <developer id="org.kde"><name>KDE</name></developer>
    <project_license>GPL-3.0-or-later</project_license>
    <categories><category>AudioVideo</category></categories>
    <keywords><keyword>montage</keyword></keywords>
    <custom><value key="flathub::verification::verified">true</value></custom>
  </component>
  <component type="desktop">
    <id>org.example.Old.desktop</id>
    <name>Old style</name>
    <summary>Notes</summary>
    <developer_name>Somebody</developer_name>
  </component>
  <component type="runtime"><id>org.gnome.Platform</id><name>Platform</name></component>
  <component type="desktop-application"><id>not an id</id></component>
</components>
""".encode()


def flathub_components() -> list[Component]:
    return list(read_components_bytes(FLATHUB_XML))


def read_components_bytes(raw: bytes):
    from vilna_mcp_apps.appstream import parse_components

    return parse_components(raw)


def make(installed=None, refs=None, vilna=None) -> tuple[Apps, MemoryBackend]:
    backend = MemoryBackend(
        {"flathub": flathub_components(), "vilna": vilna or []},
        refs=refs,
        installed=installed,
    )
    return Apps(backend, picks={"org.kde.kdenlive": "media"}), backend


# ------------------------------------------------------------- AppStream


def test_appstream_reads_apps_and_skips_runtimes_and_bad_ids(tmp_path):
    f = tmp_path / "appstream.xml.gz"
    f.write_bytes(gzip.compress(FLATHUB_XML))
    apps = {c.id: c for c in read_components(f)}
    assert set(apps) == {"org.kde.kdenlive", "org.example.Old"}
    k = apps["org.kde.kdenlive"]
    assert (k.developer, k.license, k.verified) == ("KDE", "GPL-3.0-or-later", True)
    assert k.summary("uk") == "Відеоредактор" and k.summary("en") == "Video editor"
    assert apps["org.example.Old"].developer == "Somebody"
    assert not apps["org.example.Old"].verified


def test_picks_come_from_the_file_that_gnome_software_reads(tmp_path):
    import subprocess
    import sys

    if not (PICKS / "build_picks.py").exists():
        pytest.skip("not in a VilnaOS checkout (the OBS build has only mcp/apps)")

    out = tmp_path / "picks"
    subprocess.run(
        [sys.executable, str(PICKS / "build_picks.py"), "build", str(out)],
        check=True,
        capture_output=True,
    )
    picks = read_picks([out / "vilna-software-picks.xml", tmp_path / "missing.xml"])
    assert picks["org.mozilla.firefox"] == "work"  # a default app: only Vilna::Pick
    assert len(picks) >= 30
    assert all(is_app_id(a) for a in picks)


def test_app_ids_are_checked():
    assert is_app_id("org.gnome.Calculator")
    assert is_app_id("io.github.stdenis1980.tintatexto")
    for bad in ("calculator", "org.gnome", "../etc/passwd", "org.gnome.Calc;rm", "", 5, None):
        assert not is_app_id(bad)


# ---------------------------------------------------------------- search


def test_search_puts_recommended_by_vilna_first_then_verified():
    vilna = [
        Component(
            "io.github.stdenis1980.tintatexto", names={"": "Tinta Texto"}, summaries={"": "Notes"}
        )
    ]
    apps, _ = make(vilna=vilna)
    found = apps.search("notes")["apps"]
    # Old style is neither a pick nor verified; Tinta is Vilna's own.
    assert [a["app_id"] for a in found] == ["io.github.stdenis1980.tintatexto", "org.example.Old"]
    assert found[0]["verified"] is True and found[1]["verified"] is False
    video = apps.search("video")["apps"]
    assert video[0]["pick"] == "media"


def test_search_matches_every_language_and_keywords():
    apps, _ = make()
    assert apps.search("відеоредактор")["apps"][0]["app_id"] == "org.kde.kdenlive"
    assert apps.search("montage")["apps"][0]["app_id"] == "org.kde.kdenlive"
    assert apps.search("montage nothing")["apps"] == []
    with pytest.raises(AppsError):
        apps.search("   ")
    with pytest.raises(AppsError):
        apps.search("x" * 201)


def test_search_never_looks_at_other_remotes():
    backend = MemoryBackend(
        {"flathub": [], "somebody-else": flathub_components(), "flathub-beta": []}
    )
    apps = Apps(backend)
    assert apps.search("video")["apps"] == []


# ---------------------------------------------------------- describe/facts


def test_describe_gives_the_dialog_everything_adr_0012_asks_for():
    meta = (
        "[Application]\nname=org.kde.kdenlive\n[Context]\nshared=network;ipc;\n"
        "sockets=x11;wayland;pulseaudio;\ndevices=dri;\nfilesystems=home;xdg-run/pipewire-0:ro;\n"
    )
    refs = {("flathub", "org.kde.kdenlive"): RemoteRef("abc", 120_000_000, 400_000_000, meta)}
    apps, _ = make(refs=refs)
    d = apps.describe("org.kde.kdenlive")
    assert d["download_size"] == 120_000_000 and d["remote"] == "flathub"
    assert "your whole home folder" in d["permissions"]
    assert "the internet and local network" in d["permissions"]
    f = d["facts"]
    assert list(f) == [
        "App",
        "Developer",
        "Verified",
        "In Recommended by Vilna",
        "Source",
        "Download",
        "License",
        "Can reach",
    ]
    assert f["Download"] == "120.0 MB" and f["In Recommended by Vilna"] == "yes (media)"
    other = apps.describe("org.example.Old")["facts"]
    assert other["Verified"].startswith("no") and other["In Recommended by Vilna"].startswith("no")


def test_permissions_ignore_the_sandbox_and_denied_entries():
    assert permissions("[Context]\nsockets=wayland;\nfilesystems=!home;xdg-download;\n") == [
        "the folder xdg-download"
    ]
    assert permissions("[Application]\n") == []
    assert permissions("not a key file") == ["its permissions could not be read"]


def test_sizes_read_like_gnome_software():
    assert human_size(0) == "unknown"
    assert human_size(512) == "512 B"
    assert human_size(3_400_000_000) == "3.4 GB"


# ----------------------------------------------- install, update, uninstall


def test_install_and_its_undo():
    apps, backend = make()
    out = apps.install("org.kde.kdenlive")
    assert out["remote"] == "flathub"
    assert out["undo_arguments"] == {"action": "uninstall", "app_id": "org.kde.kdenlive"}
    with pytest.raises(AppsError, match="already installed"):
        apps.install("org.kde.kdenlive")
    apps.restore(**out["undo_arguments"])
    assert backend.log == [
        ("install", "flathub", "org.kde.kdenlive"),
        ("uninstall", "org.kde.kdenlive"),
    ]


def test_uninstall_keeps_data_and_can_be_undone_from_the_same_source():
    apps, backend = make(installed=[Installed("org.kde.kdenlive", "flathub", "c1")])
    out = apps.uninstall("org.kde.kdenlive")
    assert out["data_kept"] == "~/.var/app/org.kde.kdenlive"
    assert out["undo_arguments"] == {
        "action": "install",
        "app_id": "org.kde.kdenlive",
        "remote": "flathub",
    }
    apps.restore(**out["undo_arguments"])
    assert backend.log[-1] == ("install", "flathub", "org.kde.kdenlive")


def test_only_flathub_and_vilna_ever():
    apps, backend = make()
    for remote in ("flathub-beta", "https://evil.example/repo", "somebody"):
        with pytest.raises(AppsError, match="only from flathub, vilna"):
            apps.install("org.kde.kdenlive", remote)
    with pytest.raises(AppsError, match="not set up"):
        Apps(MemoryBackend({"flathub": []})).install("org.kde.kdenlive", "vilna")
    with pytest.raises(AppsError, match="not a Flatpak app id"):
        apps.install("/tmp/evil.flatpakref")
    with pytest.raises(AppsError, match="search for it first"):
        apps.install("org.example.NotThere")
    assert backend.log == []


def test_vilna_apps_come_from_vilna():
    tinta = Component("io.github.stdenis1980.tintatexto", names={"": "Tinta Texto"})
    both = MemoryBackend({"flathub": [tinta], "vilna": [tinta]})
    assert Apps(both).install("io.github.stdenis1980.tintatexto")["remote"] == "vilna"


def test_update_skips_apps_from_other_sources():
    apps, backend = make(
        installed=[
            Installed("org.kde.kdenlive", "flathub", "c1"),
            Installed("org.other.App", "somebody", "c2"),
        ]
    )
    apps.update()
    assert backend.log == [("update", "org.kde.kdenlive")]
    with pytest.raises(AppsError, match="not installed"):
        apps.update("org.example.Old")


# ------------------------------------------------------------- the server


def call(server, name, arguments):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
    msg["params"] = {"name": name, "arguments": arguments}
    return server.handle(msg)["result"]


def test_hints_never_raise_an_operation_above_its_manifest_level():
    # Herkos (policy.rs, level_from_annotations) treats openWorldHint and
    # destructiveHint as R3 and anything not read-only as R2, and raises the
    # manifest's level to that. With openWorldHint on search, Herkos asked
    # before every search (seen in a VM, 2026-11-13).
    import tomllib

    manifest = REPO / "herkos/src/herkos/manifests/org.vilna.mcp.apps.toml"
    ops = tomllib.loads(manifest.read_text())["operations"]
    order = ["R0", "R1", "R2", "R3", "R4"]
    for tool in TOOLS:
        hints = tool["annotations"]
        if hints.get("openWorldHint") is True or hints.get("destructiveHint") is True:
            hinted = "R3"
        elif hints.get("readOnlyHint") is True:
            hinted = "R0"
        else:
            hinted = "R2"
        level = ops[tool["name"]]["level"]
        assert order.index(hinted) <= order.index(level), (tool["name"], hinted, level)


def test_server_lists_no_way_to_add_a_source_or_install_a_file():
    names = {t["name"] for t in TOOLS}
    assert names == {
        "search",
        "describe",
        "list_installed",
        "install",
        "update",
        "uninstall",
        "restore",
    }
    text = json.dumps(TOOLS)
    assert "flatpakref" not in text and "bundle" not in text and "url" not in text
    s = Server(demo_apps())
    r = call(s, "install", {"app_id": "org.kde.kdenlive", "from_file": "/tmp/x.flatpak"})
    assert r["isError"] and "cannot add app sources" in r["content"][0]["text"]
    r = call(s, "install", {"app_id": "org.kde.kdenlive", "remote": "elsewhere"})
    assert r["isError"]
    r = call(s, "add_remote", {"name": "x"})
    assert r["isError"] and "unknown tool" in r["content"][0]["text"]


def test_server_round_trip_over_stdio():
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "describe", "arguments": {"app_id": "org.kde.kdenlive"}},
        },
    ]
    out = io.StringIO()
    Server(demo_apps()).serve(io.StringIO("\n".join(json.dumps(m) for m in lines)), out)
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert replies[0]["result"]["serverInfo"]["name"] == "vilna-mcp-apps"
    d = replies[1]["result"]["structuredContent"]
    assert d["pick"] == "media" and d["facts"]["Source"] == "flathub"


def test_hostile_arguments_never_crash_the_server():
    s = Server(demo_apps())
    for name in [t["name"] for t in TOOLS]:
        for args in (None, [], {"app_id": 5}, {"query": None}, {"app_id": "x" * 10_000}, {}):
            r = s.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": args},
                }
            )
            assert "result" in r, (name, args)


def test_facts_without_a_developer_or_licence_say_so():
    f = facts(
        {
            "app_id": "a.b.c",
            "name": "a.b.c",
            "developer": "",
            "license": "",
            "verified": False,
            "pick": None,
            "remote": "flathub",
        }
    )
    assert f["Developer"] == "not given" and f["Can reach"] == "only its own files"


# --------------------------------------------------------------- launcher


def test_launcher_opens_only_installed_apps(tmp_path):
    (tmp_path / "org.kde.kdenlive" / "current").mkdir(parents=True)
    ran = []
    s = LauncherServer(Launcher(dirs=[tmp_path], run=ran.append))
    assert not call(s, "launch", {"app_id": "org.kde.kdenlive"})["isError"]
    assert ran == [["flatpak", "run", "org.kde.kdenlive"]]
    assert call(s, "launch", {"app_id": "org.other.App"})["isError"]
    assert call(s, "launch", {"app_id": "../../bin/sh"})["isError"]
    r = call(s, "launch", {"app_id": "org.kde.kdenlive", "args": ["--evil"]})
    assert r["isError"]
    assert len(ran) == 1
