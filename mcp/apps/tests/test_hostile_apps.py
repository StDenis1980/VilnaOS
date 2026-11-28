# SPDX-License-Identifier: Apache-2.0
"""Hostile-input tests for the "programs" MCP server.

Everything here is fed by other people: AppStream files of Flathub, the ref
metadata of an app, the JSON a client sends, the names of installed apps.
Nothing real runs: no flatpak, no systemd-run, no network. libflatpak is a
fake, ``subprocess.run`` is replaced where a command is expected, and an
autouse fixture makes any other process or socket a test failure.
"""

import contextlib
import gzip
import importlib
import io
import json
import socket
import subprocess
import sys
import time
import types
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from vilna_mcp_apps import appstream
from vilna_mcp_apps.apps import (
    ALLOWED_REMOTES,
    MAX_QUERY_CHARS,
    MAX_RESULTS,
    Apps,
    AppsError,
    Installed,
    MemoryBackend,
    RemoteRef,
    branch_candidates,
    facts,
    human_size,
    permissions,
)
from vilna_mcp_apps.appstream import (
    MAX_FIELD_CHARS,
    Component,
    is_app_id,
    parse_components,
    read_components,
    read_picks,
)
from vilna_mcp_apps.launcher import (
    Launcher,
    LauncherServer,
    _systemd_run,
    installed_dirs,
)
from vilna_mcp_apps.launcher import main as launcher_main
from vilna_mcp_apps.server import TOOLS, Server, check_arguments, demo_apps
from vilna_mcp_apps.server import main as server_main

# ------------------------------------------------------------------ guards


@pytest.fixture(autouse=True)
def nothing_real_runs(monkeypatch):
    """A test that starts a process or opens a socket fails."""

    def refuse(*args, **kwargs):
        raise AssertionError("the tests must not start processes or use the network")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr("os.system", refuse)


ID = "org.example.App"


def xml_of(*components: str) -> bytes:
    return ("<components>" + "".join(components) + "</components>").encode()


def comp(id_: str = ID, body: str = "", attrs: str = "") -> str:
    return f"<component {attrs}><id>{escape(id_)}</id>{body}</component>"


def parse(raw: bytes) -> list[Component]:
    return list(parse_components(raw))


class SpyBackend(MemoryBackend):
    """Remembers every argument any call received."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[tuple] = []

    def remote_ref(self, remote, app_id):
        self.calls.append(("remote_ref", remote, app_id))
        return super().remote_ref(remote, app_id)

    def install(self, remote, app_id):
        self.calls.append(("install", remote, app_id))
        return super().install(remote, app_id)

    def update(self, app_ids):
        self.calls.append(("update", *app_ids))
        return super().update(app_ids)

    def uninstall(self, app_id):
        self.calls.append(("uninstall", app_id))
        return super().uninstall(app_id)


def make_apps(*components: Component, installed=None, picks=None, vilna=()):
    backend = SpyBackend({"flathub": list(components), "vilna": list(vilna)}, installed=installed)
    return Apps(backend, picks=picks or {}), backend


def rpc(server, method, params=None, id_=1):
    msg = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return server.handle(msg)


def call(server, name, arguments):
    return rpc(server, "tools/call", {"name": name, "arguments": arguments})["result"]


# --------------------------------------------------- app ids, in general

BAD_IDS = [
    "",
    " ",
    "org.example.App ",
    " org.example.App",
    "org.example.App\n",
    "org.example.App\r\n--evil",
    "org example App",
    "org.example",
    "org",
    "org..example.App",
    ".org.example.App",
    "org.example.App.",
    "../../bin/sh",
    "org/example/App",
    "org.example.App/../../x",
    "--user",
    "--",
    "-org.example.App",
    "org.example.App --installation=evil",
    "org.example.App;rm",
    "org.example.App|sh",
    "org.example.App&&id",
    "$(id).example.App",
    "`id`.example.App",
    "org.example.%f",
    "org.example.App\x00",
    "org.example.C\u0430lculator",  # Cyrillic a
    "org.example.App\u200b",
    "\uff4f\uff52\uff47.example.App",  # full-width letters
    "org.example.App\u0661",  # Arabic-Indic digit
    "org.1example.App",
    "9org.example.App",
    "org.example.-App",
    "org.example.App:stable",
    "org.example.App//stable",
    "app/org.example.App/x86_64/stable",
    "org.example.App@stable",
    "org-x.example.App",
    "~",
    "*",
    "org.example.*",
    "file:///tmp/x.flatpakref",
    "https://dl.flathub.org/repo/appstream/org.example.App.flatpakref",
    "org.example." + "a" * 300,
    "a" * 256,
    "a." + "b" * 253 + ".c",
]

NOT_STRINGS = [None, 1, 1.5, True, [], {}, b"org.example.App", ("a", "b", "c"), ["org.example.App"]]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_is_app_id_refuses_hostile_strings(bad):
    assert is_app_id(bad) is False


@pytest.mark.parametrize("bad", NOT_STRINGS)
def test_is_app_id_refuses_anything_that_is_not_a_string(bad):
    assert is_app_id(bad) is False


def test_is_app_id_accepts_what_flatpak_accepts():
    assert is_app_id("org.gnome.Calculator")
    assert is_app_id("_a._b._c")
    assert is_app_id("io.github.stdenis1980.Gloxi")
    assert is_app_id("com.example.my-app.Sub_part")
    # the length limit is 255 characters, both sides of it
    ok = "a." + "b" * 251 + ".c"
    assert len(ok) == 255 and is_app_id(ok)
    assert not is_app_id(ok + "c")


def test_is_app_id_is_fast_on_huge_strings():
    start = time.monotonic()
    for s in ("a." * 2_000_000, "a" * 5_000_000, "a.b." + "-" * 5_000_000):
        assert is_app_id(s) is False
    assert time.monotonic() - start < 2


@pytest.mark.parametrize("bad", BAD_IDS)
def test_apps_never_hands_a_hostile_id_to_the_backend(bad):
    apps, backend = make_apps(
        Component(ID, names={"": "App"}),
        installed=[Installed(ID, "flathub", "c1")],
    )
    operations = [
        lambda: apps.describe(bad),
        lambda: apps.install(bad),
        lambda: apps.install(bad, "flathub"),
        lambda: apps.update(bad),
        lambda: apps.uninstall(bad),
        lambda: apps.restore("install", bad),
        lambda: apps.restore("uninstall", bad),
    ]
    for op in operations:
        with pytest.raises(AppsError):
            op()
    assert backend.calls == []
    assert backend.log == []


@pytest.mark.parametrize("bad", NOT_STRINGS)
def test_apps_refuses_ids_that_are_not_strings(bad):
    apps, backend = make_apps(Component(ID))
    operations = [apps.describe, apps.install, apps.uninstall]
    if bad is not None:  # update(None) means "everything"
        operations.append(apps.update)
    for op in operations:
        with pytest.raises(AppsError):
            op(bad)
    assert backend.calls == []


@pytest.mark.parametrize(
    "lookalike",
    [
        "org.example.App.flatpakref",
        "org.example.App.flatpak",
        "org.example.App.flatpakrepo",
        "org.gnome.Platform",
        "org.freedesktop.Sdk",
        "org.example.App.Locale",
    ],
)
def test_ids_that_look_like_files_or_runtimes_are_not_installed(lookalike):
    # They are valid names, but nothing in the catalogues or on the remotes
    # is called that, so there is nothing to install.
    apps, backend = make_apps(Component(ID))
    with pytest.raises(AppsError):
        apps.install(lookalike)
    assert backend.log == []


# ------------------------------------------------------- AppStream: XML

DTD_BOMB = (
    '<?xml version="1.0"?><!DOCTYPE c ['
    '<!ENTITY l0 "lol">'
    + "".join(f'<!ENTITY l{i} "{f"&l{i - 1};" * 10}">' for i in range(1, 9))
    + "]><components><component><id>org.example.App</id><name>&l8;</name></component></components>"
).encode()

DTD_QUADRATIC = (
    '<!DOCTYPE c [<!ENTITY a "' + "A" * 100_000 + '">]><components><component>'
    "<id>org.example.App</id><name>" + "&a;" * 2000 + "</name></component></components>"
).encode()


@pytest.mark.parametrize("raw", [DTD_BOMB, DTD_QUADRATIC], ids=["nested", "quadratic"])
def test_entity_expansion_is_stopped_or_stays_small(raw):
    start = time.monotonic()
    try:
        found = parse(raw)
    except ET.ParseError:
        found = []
    assert time.monotonic() - start < 10
    for c in found:
        assert all(len(v) <= MAX_FIELD_CHARS for v in c.names.values())


def test_external_entities_are_never_read(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET-MARKER-123")
    raw = (
        f'<!DOCTYPE c [<!ENTITY xxe SYSTEM "file://{secret}">]>'
        "<components><component><id>org.example.App</id>"
        "<name>&xxe;</name><summary>&xxe;</summary></component></components>"
    ).encode()
    try:
        found = parse(raw)
    except ET.ParseError:
        found = []
    assert "SECRET-MARKER-123" not in repr(found)


@pytest.mark.parametrize(
    "dtd",
    [
        '<!DOCTYPE c [<!ENTITY % e SYSTEM "http://127.0.0.1:9/e.dtd"> %e;]>',
        '<!DOCTYPE c SYSTEM "http://127.0.0.1:9/c.dtd">',
        '<!DOCTYPE c PUBLIC "-//X//EN" "ftp://127.0.0.1:9/c.dtd">',
        '<!DOCTYPE c [<!ENTITY e SYSTEM "gopher://127.0.0.1:9/x">]>',
    ],
)
def test_a_doctype_never_makes_the_parser_reach_out(dtd):
    # The autouse fixture turns any socket use into a failure.
    raw = dtd.encode() + xml_of(comp(body="<name>n</name>"))
    with contextlib.suppress(ET.ParseError):
        parse(raw)


def test_very_deep_nesting_does_not_crash():
    inside = b"<x>" * 100_000 + b"</x>" * 100_000
    raw = b"<components><component><id>org.example.App</id><name>" + inside
    raw += b"</name></component></components>"
    start = time.monotonic()
    try:
        found = parse(raw)
    except ET.ParseError:
        found = []
    assert time.monotonic() - start < 10
    assert all(c.id == ID for c in found)
    outer = b"<components>" + b"<x>" * 100_000 + b"</x>" * 100_000 + b"</components>"
    with contextlib.suppress(ET.ParseError):
        assert parse(outer) == []


def test_every_field_is_cut_to_a_short_length():
    long = "x " * 200_000
    cats = "".join(f"<category>{long}</category>" for _ in range(100))
    words = "".join(f"<keyword>{long}</keyword>" for _ in range(500))
    body = (
        f"<name>{long}</name><summary>{long}</summary>"
        f"<developer_name>{long}</developer_name>"
        f"<project_license>{long}</project_license>"
        f"<categories>{cats}</categories><keywords>{words}</keywords>"
        f'<custom><value key="{"k" * 5000}">{long}</value></custom>'
    )
    (c,) = parse(xml_of(comp(body=body)))
    assert len(c.names[""]) <= MAX_FIELD_CHARS
    assert len(c.summaries[""]) <= MAX_FIELD_CHARS
    assert len(c.developer) <= MAX_FIELD_CHARS
    assert len(c.license) <= MAX_FIELD_CHARS
    assert len(c.categories) <= 20 and all(len(x) <= MAX_FIELD_CHARS for x in c.categories)
    assert len(c.keywords) <= 50 and all(len(x) <= MAX_FIELD_CHARS for x in c.keywords)
    assert all(len(k) <= 100 and len(v) <= MAX_FIELD_CHARS for k, v in c.custom.items())


def test_a_huge_description_is_not_kept():
    marker = "DESCRIPTION-MARKER"
    body = f"<name>n</name><description><p>{marker}{'z' * 20_000_000}</p></description>"
    (c,) = parse(xml_of(comp(body=body)))
    assert marker not in repr(c)
    assert c.name() == "n"


def test_launchable_and_its_arguments_are_never_used():
    body = (
        '<launchable type="desktop-id">../../bin/sh --evil %f; touch /tmp/x</launchable>'
        '<launchable type="service">evil.service</launchable>'
        '<provides><binary>sh</binary></provides><bundle type="flatpak">app/x/y/z</bundle>'
    )
    (c,) = parse(xml_of(comp(body="<name>n</name>" + body)))
    shown = repr(c)
    for word in ("bin/sh", "--evil", "touch", "evil.service", "bundle", "app/x/y/z"):
        assert word not in shown
    assert c.id == ID


XML_SAFE_BAD_IDS = [b for b in BAD_IDS if "\x00" not in b]


@pytest.mark.parametrize("bad", XML_SAFE_BAD_IDS)
def test_hostile_ids_in_a_catalogue_are_dropped(bad):
    raw = xml_of(comp(bad, "<name>n</name>"), comp(ID, "<name>fine</name>"))
    found = parse(raw)
    assert all(is_app_id(c.id) for c in found)
    assert ID in {c.id for c in found}
    # only the whitespace around an id is trimmed; nothing else is rewritten
    assert {c.id for c in found} <= {ID, bad.strip()}


def test_a_nul_character_makes_the_file_unreadable():
    with pytest.raises(ET.ParseError):
        parse(b"<components><component><id>org.example.App\x00</id></component></components>")


def test_old_style_desktop_ids_lose_one_suffix_only():
    found = parse(
        xml_of(
            comp("org.example.Old.desktop", "<name>a</name>"),
            comp("org.example.Twice.desktop.desktop", "<name>b</name>"),
            comp("org.example.desktop", "<name>c</name>"),
            comp(".desktop", "<name>d</name>"),
            comp("org.example.App;rm.desktop", "<name>e</name>"),
        )
    )
    assert [c.id for c in found] == ["org.example.Old", "org.example.Twice.desktop"]


def test_only_apps_are_read_not_runtimes_or_extensions():
    kinds = {
        "runtime": False,
        "addon": False,
        "extension": False,
        "generic": False,
        "Desktop-Application": False,
        "": False,
        "desktop": True,
        "desktop-application": True,
        "console-application": True,
    }
    for kind, expected in kinds.items():
        found = parse(xml_of(comp(attrs=f'type="{kind}"')))
        assert bool(found) is expected, kind
    assert parse(xml_of(comp()))  # no type at all is an app


VERIFIED = "flathub::verification::verified"


def test_verified_comes_only_from_the_custom_values_of_the_app():
    cases = {
        "elsewhere": "<verified>true</verified>",
        "in-description": f'<description><value key="{VERIFIED}">true</value></description>',
        "as-attribute": "",
        "nested-in-custom": f'<custom><x><value key="{VERIFIED}">true</value></x></custom>',
        "other-text": f'<custom><value key="{VERIFIED}">yes</value></custom>',
        "false-text": f'<custom><value key="{VERIFIED}">false</value></custom>',
        "one": f'<custom><value key="{VERIFIED}">1</value></custom>',
        "padded-key": f'<custom><value key=" {VERIFIED}">true</value></custom>',
    }
    for name, body in cases.items():
        attrs = 'verified="true"' if name == "as-attribute" else ""
        (c,) = parse(xml_of(comp(body="<name>n</name>" + body, attrs=attrs)))
        assert c.verified is False, name
    good = f'<custom><value key="{VERIFIED}">True</value></custom>'
    assert parse(xml_of(comp(body=good)))[0].verified is True


def test_a_pick_mark_inside_a_flathub_component_is_not_a_pick():
    pick = '<custom><value key="Vilna::Pick">media</value></custom>'
    components = parse(xml_of(comp(body="<name>Evil</name>" + pick)))
    apps = Apps(MemoryBackend({"flathub": components, "vilna": []}), picks={})
    assert apps.search("evil")["apps"][0]["pick"] is None
    assert apps.describe(ID)["facts"]["In Recommended by Vilna"].startswith("no")


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"   ",
        b"not xml at all",
        b"<components>",
        b"<components><component></components>",
        b"<components></COMPONENTS>",
        b"<components>\xc3\x28</components>",
        b"<components><component><id>a.b.c</id>&#0;</component></components>",
        b"<components><component><id>a.b.c</id>&#x1b;</component></components>",
        b"<components><component><id>a.b.c</id>&undefined;</component></components>",
        b"<a><b/></a><c/>",
        b"<components a='1' a='2'/>",
    ],
)
def test_broken_xml_gives_a_parse_error_and_nothing_else(raw):
    with pytest.raises(ET.ParseError):
        parse(raw)


def test_the_same_app_in_both_remotes_is_listed_once():
    apps, _backend = make_apps(
        Component(ID, names={"": "App"}), vilna=[Component(ID, names={"": "App"})]
    )
    assert [a["app_id"] for a in apps.search("app")["apps"]] == [ID]


# ---------------------------------------------------- AppStream: files


def test_a_catalogue_larger_than_the_limit_is_refused_before_and_after_unpacking(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(appstream, "MAX_APPSTREAM_BYTES", 1024 * 1024)
    bomb = tmp_path / "bomb.xml.gz"
    bomb.write_bytes(gzip.compress(b"\x00" * (3 * 1024 * 1024)))
    assert bomb.stat().st_size < 100_000
    with pytest.raises(ValueError, match="larger") as refused:
        list(read_components(bomb))
    assert str(bomb) in str(refused.value)  # refused while unpacking, not after
    with pytest.raises(ValueError, match="larger"):
        parse(b"<components>" + b" " * (2 * 1024 * 1024) + b"</components>")
    ok = tmp_path / "ok.xml.gz"
    ok.write_bytes(gzip.compress(xml_of(comp())))
    assert [c.id for c in read_components(ok)] == [ID]


class FileBackend(SpyBackend):
    """A backend that reads its catalogue from a file, like the real one."""

    def __init__(self, path: Path):
        super().__init__({"flathub": [], "vilna": []})
        self.path = path

    def components(self, remote):
        return read_components(self.path) if remote == "flathub" else iter(())


def test_a_file_that_is_not_gzip_is_an_apps_error(tmp_path):
    path = tmp_path / "appstream.xml.gz"
    path.write_bytes(b"not gzip at all")
    with pytest.raises(AppsError, match="cannot be read"):
        Apps(FileBackend(path)).search("anything")


def test_an_empty_catalogue_file_is_a_parse_error_and_not_an_empty_catalogue(tmp_path):
    path = tmp_path / "appstream.xml.gz"
    path.write_bytes(gzip.compress(b""))
    with pytest.raises(ET.ParseError):
        Apps(FileBackend(path)).search("anything")


def test_a_missing_or_unreadable_catalogue_is_an_apps_error(tmp_path):
    apps = Apps(FileBackend(tmp_path / "missing.xml.gz"))
    with pytest.raises(AppsError, match="cannot be read"):
        apps.search("anything")
    folder = tmp_path / "dir.xml.gz"
    folder.mkdir()
    with pytest.raises(AppsError, match="cannot be read"):
        Apps(FileBackend(folder)).search("anything")


def test_a_broken_catalogue_does_not_stop_the_server(tmp_path):
    path = tmp_path / "appstream.xml"
    path.write_bytes(b"<components><component>")
    server = Server(Apps(FileBackend(path)))
    assert call(server, "search", {"query": "x"})["isError"] is True
    assert call(server, "list_installed", {})["isError"] is False


def test_picks_skip_what_cannot_be_read_and_keep_the_rest(tmp_path):
    good = tmp_path / "good.xml"
    pick = '<custom><value key="Vilna::Pick">media</value></custom>'
    good.write_bytes(xml_of(comp("org.example.One", pick)))
    broken = tmp_path / "broken.xml"
    broken.write_bytes(b"<components><component><id>")
    bomb = tmp_path / "bomb.xml"
    bomb.write_bytes(DTD_BOMB)
    folder = tmp_path / "folder.xml"
    folder.mkdir()
    files = [tmp_path / "missing.xml", broken, bomb, folder, good, broken]
    assert read_picks(files) == {"org.example.One": "media"}


def test_picks_ignore_empty_sections_and_bad_ids_and_cut_long_ones(tmp_path):
    def pick(value):
        return f'<custom><value key="Vilna::Pick">{value}</value></custom>'

    f = tmp_path / "picks.xml"
    f.write_bytes(
        xml_of(
            comp("org.example.Empty", pick("")),
            comp("../../evil", pick("work")),
            comp("org.example.Long", pick("w" * 5000)),
            comp("org.example.Later", pick("first")),
            comp("org.example.Later", pick("second")),
        )
    )
    picks = read_picks([f])
    assert set(picks) == {"org.example.Long", "org.example.Later"}
    assert len(picks["org.example.Long"]) <= MAX_FIELD_CHARS
    assert picks["org.example.Later"] == "second"


def test_the_later_picks_file_wins_and_a_bad_later_file_removes_nothing(tmp_path):
    def pick_file(name, section):
        path = tmp_path / name
        value = f'<custom><value key="Vilna::Pick">{section}</value></custom>'
        path.write_bytes(xml_of(comp(ID, value)))
        return path

    first, second = pick_file("a.xml", "media"), pick_file("b.xml", "work")
    assert read_picks([first, second]) == {ID: "work"}
    broken = tmp_path / "c.xml"
    broken.write_bytes(b"<<<")
    assert read_picks([first, broken]) == {ID: "media"}


# ---------------------------------------------- what a person is shown

LINE_BREAKS = ["\n", "\r", "\t", "\x85", "\u2028", "\u2029", "\xa0"]


def test_names_and_other_fields_never_break_the_lines_of_the_dialog():
    name = "".join(f"a&#x{ord(x):x};b" for x in LINE_BREAKS)
    (c,) = parse(
        xml_of(
            comp(
                ID,
                f"<name>{name}</name><developer_name>x\ny&#x2028;z</developer_name>"
                "<project_license>MIT\nAND\r\nGPL</project_license>"
                '<custom><value key="Vilna::Pick">me\ndia</value></custom>',
            )
        )
    )
    apps = Apps(MemoryBackend({"flathub": [c], "vilna": []}), picks={ID: c.custom["Vilna::Pick"]})
    lines = apps.describe(ID)["facts"]
    for key, value in lines.items():
        assert not any(b in value for b in ("\n", "\r", "\u2028", "\u2029", "\x85")), key
    assert list(lines) == [
        "App",
        "Developer",
        "Verified",
        "In Recommended by Vilna",
        "Source",
        "Download",
        "License",
        "Can reach",
    ]


def test_facts_of_an_app_with_nothing_given_say_so():
    apps, _ = make_apps(Component(ID))
    f = apps.describe(ID)["facts"]
    assert f["Developer"] == "not given"
    assert f["License"] == "not given"
    assert f["Can reach"] == "only its own files"
    assert f["Verified"].startswith("no")
    assert f["App"] == f"{ID} ({ID})"


def test_facts_without_the_optional_keys_still_work():
    entry = {
        "app_id": ID,
        "name": "n",
        "developer": "",
        "verified": False,
        "remote": "flathub",
        "license": "",
    }
    assert facts(entry)["Download"] == "unknown"


@pytest.mark.parametrize(
    ("n", "text"),
    [
        (0, "unknown"),
        (-1, "unknown"),
        (-(10**30), "unknown"),
        (1, "1 B"),
        (999, "999 B"),
        (1000, "1 kB"),
        (10**9, "1.0 GB"),
        (10**12, "1000.0 GB"),
        (2**64, f"{2**64 / 1e9:.1f} GB"),
    ],
)
def test_human_size_survives_any_number_a_remote_can_report(n, text):
    assert human_size(n) == text


# -------------------------------------------- permissions of an app (ref)


@pytest.mark.parametrize(
    "metadata",
    [
        "",
        "garbage",
        "[Context\nshared=network",
        "shared=network",
        "\x00\x01\x02",
        "[Context]\n= =\n",
        "[Context]\nshared\n",
        "[Context]\nshared=%(x)s\nsockets=%\n",
        "[Context]\nshared=${x}\n",
        "[Context]\n" + "shared=network\n" * 10_000,
        "[Context]\nshared=" + "network;" * 200_000 + "\n",
        "[Context]\n[Context]\nshared=network\n",
        "[Application]\nname=a\n",
        "[Context]\nfilesystems=" + ";".join(f"/p{i}" for i in range(20000)) + "\n",
    ],
)
def test_permissions_never_raise_on_odd_metadata(metadata):
    start = time.monotonic()
    out = permissions(metadata)
    assert isinstance(out, list) and all(isinstance(x, str) for x in out)
    assert time.monotonic() - start < 10


def test_permissions_name_each_reach_in_plain_words_once():
    md = (
        "[Context]\n"
        "shared=network;network;ipc;\n"
        "sockets=x11;session-bus;system-bus;ssh-auth;pcsc;cups;gpg-agent;wayland;\n"
        "devices=all;input;dri;\n"
        "filesystems=host;host:ro;home;host-os;host-etc;/srv/data:ro;~/Documents;xdg-download;!host\n"
    )
    out = permissions(md)
    assert out == list(dict.fromkeys(out))
    joined = " | ".join(out)
    for phrase in (
        "internet",
        "X11",
        "session bus",
        "system bus",
        "SSH keys",
        "smart cards",
        "printers",
        "GPG keys",
        "all devices",
        "input devices",
        "all files on the computer",
        "home folder",
        "the folder /srv/data (read only)",
        "the folder ~/Documents",
        "the folder xdg-download",
    ):
        assert phrase in joined, phrase
    assert "wayland" not in joined and "dri" not in joined and "ipc" not in joined


def test_permissions_ignore_denied_entries_and_option_strings():
    md = "[Context]\nfilesystems=!host;!home;--filesystem=host;-x;host-reset;\nshared=!network\n"
    assert permissions(md) == []


def test_permissions_do_not_repeat_unknown_names_of_sockets_and_devices():
    md = (
        "[Context]\nsockets=<script>alert(1)</script>;evil-socket\n"
        "devices=evil-device\nshared=evil\n"
    )
    assert permissions(md) == []


def test_a_section_called_context_in_other_case_is_not_a_context():
    assert permissions("[context]\nshared=network\n") == []
    assert permissions("[CONTEXT]\nshared=network\n") == []


# -------------------------------------------------------- searching, etc.


def test_search_queries_of_every_odd_kind():
    apps, backend = make_apps(
        Component(ID, names={"": "App"}, summaries={"": "does (things) [a-z] .*"}),
    )
    for q in ("", " ", "\t\n", "\u2003", "\u3000", "x" * (MAX_QUERY_CHARS + 1)):
        with pytest.raises(AppsError):
            apps.search(q)
    odd = (
        "(",
        "[",
        ".*",
        "\\",
        "a|b",
        "%s",
        "{0}",
        "\x00",
        "\ud800",
        "\u00df",
        "\u0130",
        "x" * 200,
    )
    for q in odd:
        out = apps.search(q)
        assert isinstance(out["apps"], list) and out["more"] >= 0
    assert [a["app_id"] for a in apps.search("(things)")["apps"]] == [ID]
    assert backend.log == []


@pytest.mark.parametrize("limit", [-(10**12), -1, 0, 1, 25, 26, 10**6, 10**30])
def test_search_limit_is_always_clamped(limit):
    many = [Component(f"org.example.App{i}", names={"": f"App {i}"}) for i in range(60)]
    apps, _ = make_apps(*many)
    out = apps.search("app", limit=limit)
    assert 1 <= len(out["apps"]) <= MAX_RESULTS
    assert len(out["apps"]) + out["more"] == 60


def test_search_in_a_big_catalogue_stays_fast():
    many = [
        Component(f"org.example.App{i}", names={"": f"Name {i}"}, summaries={"": "s" * 300})
        for i in range(20_000)
    ]
    apps, _ = make_apps(*many)
    start = time.monotonic()
    assert apps.search("name 19999")["apps"][0]["app_id"] == "org.example.App19999"
    assert apps.search("zzz")["apps"] == []
    assert time.monotonic() - start < 10


def test_recommended_comes_first_even_when_another_app_is_verified():
    shouty = Component("org.example.Shout", names={"": "Video AAA"}, custom={VERIFIED: "true"})
    plain = Component("org.example.Plain", names={"": "Video ZZZ"})
    apps, _ = make_apps(shouty, plain, picks={"org.example.Plain": "media"})
    assert [a["app_id"] for a in apps.search("video")["apps"]] == [
        "org.example.Plain",
        "org.example.Shout",
    ]


def test_hostile_language_arguments_only_select_a_name():
    c = Component(ID, names={"": "Plain", "uk": "Звичайна"}, summaries={"uk": "Опис"})
    apps, _ = make_apps(c)
    for lang in ("uk", "uk_UA", "uk-UA", "../../etc", "", "uk\n", "\x00", "_", "-", "a" * 250):
        assert isinstance(apps.search("plain", language=lang)["apps"], list)
        assert apps.describe(ID, language=lang)["name"] in ("Plain", "Звичайна")


# --------------------------------------------- install, update, uninstall


@pytest.mark.parametrize(
    "remote",
    [
        "evil",
        "Flathub",
        "FLATHUB",
        "flathub ",
        " flathub",
        "flathub\n",
        "flathub/../x",
        "flathub-beta",
        "flathub,vilna",
        "https://evil.example/repo.flatpakrepo",
        "file:///tmp/x.flatpakref",
        "--user",
        "",
        "\x00",
        "vilna\x00",
    ],
)
def test_no_operation_accepts_a_remote_outside_the_list(remote):
    apps, backend = make_apps(Component(ID, names={"": "App"}))
    with pytest.raises(AppsError, match="only from flathub, vilna"):
        apps.install(ID, remote)
    with pytest.raises(AppsError, match="only from flathub, vilna"):
        apps.describe(ID, remote)
    with pytest.raises(AppsError, match="only from flathub, vilna"):
        apps.restore("install", ID, remote)
    assert backend.log == []
    assert not any(c[0] == "install" for c in backend.calls)


def test_a_remote_that_exists_here_but_is_not_allowed_is_never_used():
    c = Component(ID, names={"": "App"})
    backend = SpyBackend({"evil": [c], "flathub": [], "vilna": []})
    apps = Apps(backend)
    with pytest.raises(AppsError):
        apps.install(ID)
    with pytest.raises(AppsError):
        apps.install(ID, "evil")
    assert apps.search("app")["apps"] == []
    assert backend.log == []
    assert ALLOWED_REMOTES == ("flathub", "vilna")


@pytest.mark.parametrize(
    "origin", ["somebody", "Flathub", "flathub ", "flathub-beta", "", "vilna2"]
)
def test_update_only_touches_apps_from_the_two_remotes(origin):
    apps, backend = make_apps(
        installed=[
            Installed("org.example.Good", "flathub", "c1"),
            Installed("org.example.Mine", "vilna", "c2"),
            Installed("org.example.Odd", origin, "c3"),
        ]
    )
    apps.update()
    assert backend.log == [("update", "org.example.Good", "org.example.Mine")]
    backend.log.clear()
    apps.update("org.example.Odd")
    assert backend.log == [("update",)]


def test_uninstall_of_an_app_from_another_source_gives_no_remote_to_undo_with():
    apps, _ = make_apps(installed=[Installed(ID, "somebody", "c1")])
    out = apps.uninstall(ID)
    assert out["undo_arguments"] == {"action": "install", "app_id": ID}
    assert out["data_kept"] == f"~/.var/app/{ID}"
    with pytest.raises(AppsError, match="search for it first"):
        apps.restore(**out["undo_arguments"])


@pytest.mark.parametrize(
    "action", ["", "Install", "install ", "remove", "delete", "update", None, 1, "--"]
)
def test_restore_knows_two_actions(action):
    apps, backend = make_apps(Component(ID), installed=[Installed(ID, "flathub", "c")])
    with pytest.raises(AppsError, match="action must be"):
        apps.restore(action, ID)
    assert backend.log == []


def test_describe_of_an_installed_app_keeps_to_the_remote_it_came_from():
    apps, backend = make_apps(
        Component(ID, names={"": "App"}), installed=[Installed(ID, "somebody", "c")]
    )
    assert apps.describe(ID)["remote"] == "flathub"
    assert ("remote_ref", "somebody", ID) not in backend.calls


def test_vilna_prefix_is_matched_exactly():
    for app_id, wanted in (
        ("io.github.stdenis1980.Gloxi", "vilna"),
        ("io.github.StDenis1980.Gloxi", "flathub"),
        ("io.github.stdenis19800.Gloxi", "flathub"),
        ("io.github.stdenis1980x.Gloxi", "flathub"),
    ):
        c = Component(app_id)
        apps = Apps(MemoryBackend({"flathub": [c], "vilna": [c]}))
        assert apps.install(app_id)["remote"] == wanted


def test_branch_candidates_have_no_repeats_and_skip_empty_names():
    assert branch_candidates("flathub", None) == ["stable", "master"]
    assert branch_candidates("vilna", "") == ["master", "stable"]
    assert branch_candidates("vilna", "stable") == ["stable", "master"]
    assert branch_candidates("elsewhere", "x") == ["x", "stable", "master"]
    for own in (None, "", "stable", "master", "beta", "a" * 1000):
        out = branch_candidates("flathub", own)
        assert len(out) == len(set(out)) and "" not in out


# ------------------------------------------------------------ the server

PROPERTY_NAMES = {"query", "limit", "language", "app_id", "remote", "action"}
FORBIDDEN_WORDS = ("url", "path", "file", "flatpakref", "args", "flags", "options", "user", "repo")


def test_tools_take_only_plain_values_and_never_paths_or_flags():
    seen = set()
    for tool in TOOLS:
        schema = tool["inputSchema"]
        assert schema["additionalProperties"] is False
        for prop, spec in schema["properties"].items():
            seen.add(prop)
            assert spec["type"] in ("string", "integer")
            assert not any(word in prop.lower() for word in FORBIDDEN_WORDS)
            assert not prop.startswith("-")
        assert set(schema["required"]) <= set(schema["properties"])
    assert seen == PROPERTY_NAMES
    install = next(t for t in TOOLS if t["name"] == "install")
    assert install["inputSchema"]["properties"]["remote"]["enum"] == list(ALLOWED_REMOTES)


EXTRA_ARGUMENTS = [
    {"flatpakref": "https://evil.example/x.flatpakref"},
    {"url": "https://evil.example"},
    {"remote_url": "https://evil.example/repo"},
    {"bundle": "/tmp/x.flatpak"},
    {"--user": True},
    {"--": ""},
    {"args": ["--installation=evil"]},
    {"options": "--no-verify"},
    {"": 1},
    {"APP_ID": ID},
    {"__proto__": {}},
]


@pytest.mark.parametrize("name", [t["name"] for t in TOOLS])
@pytest.mark.parametrize("extra", EXTRA_ARGUMENTS, ids=lambda e: next(iter(e)) or "empty")
def test_extra_arguments_are_refused_by_every_tool(name, extra):
    apps, backend = make_apps(Component(ID, names={"": "App"}))
    args = {}
    if name == "search":
        args["query"] = "app"
    if name == "restore":
        args["action"] = "install"
    if name in ("describe", "install", "uninstall", "restore"):
        args["app_id"] = ID
    result = call(Server(apps), name, {**args, **extra})
    assert result["isError"] is True
    assert "unknown arguments" in result["content"][0]["text"]
    assert backend.log == [] and backend.calls == []


ODD_VALUES = [None, True, False, 0, 1, -1, 1.5, "", [], {}, [ID], {"app_id": ID}, "x" * 300]


@pytest.mark.parametrize("name", [t["name"] for t in TOOLS])
@pytest.mark.parametrize("value", ODD_VALUES, ids=repr)
def test_every_argument_of_every_tool_survives_odd_values(name, value):
    apps, backend = make_apps(
        Component(ID, names={"": "App"}), installed=[Installed(ID, "flathub", "c")]
    )
    server = Server(apps)
    props = next(t for t in TOOLS if t["name"] == name)["inputSchema"]["properties"]
    for prop in props:
        result = call(server, name, {prop: value})
        assert isinstance(result["isError"], bool)
        json.dumps(result)
    assert backend.log == []  # nothing changed hands whatever was sent


def test_changes_need_the_right_types_before_anything_happens():
    apps, backend = make_apps(Component(ID, names={"": "App"}))
    server = Server(apps)
    for args in (
        {"app_id": 1},
        {"app_id": [ID]},
        {"app_id": ID, "remote": ["flathub"]},
        {"app_id": ID, "remote": "other"},
        {"app_id": ID, "remote": None},
    ):
        assert call(server, "install", args)["isError"] is True
    assert backend.log == []


def test_limit_must_be_a_whole_number_not_a_flag_or_a_fraction():
    apps, _ = make_apps(Component(ID, names={"": "App"}))
    server = Server(apps)
    for limit in (True, False, 1.0, "5", None, [1]):
        assert call(server, "search", {"query": "app", "limit": limit})["isError"] is True
    for limit in (-5, 0, 10**9):
        assert call(server, "search", {"query": "app", "limit": limit})["isError"] is False


def test_long_strings_are_refused_before_any_work():
    apps, backend = make_apps(Component(ID, names={"": "App"}))
    server = Server(apps)
    start = time.monotonic()
    for name, key in (("search", "query"), ("describe", "app_id"), ("install", "app_id")):
        r = call(server, name, {key: "x" * 5_000_000})
        assert r["isError"] is True and "too long" in r["content"][0]["text"]
    assert time.monotonic() - start < 2
    assert backend.calls == []


def test_check_arguments_gives_back_what_it_was_given():
    args = {"query": "app", "limit": 3}
    assert check_arguments("search", args) is args
    assert args == {"query": "app", "limit": 3}
    with pytest.raises(AppsError):
        check_arguments("search", ["query"])
    with pytest.raises(AppsError):
        check_arguments("search", {})


def message(**fields):
    return {"jsonrpc": "2.0", "id": 1, **fields}


HOSTILE_MESSAGES = [
    None,
    1,
    "tools/list",
    [],
    [{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
    {},
    {"jsonrpc": "1.0", "id": 1, "method": "ping"},
    {"jsonrpc": 2.0, "id": 1, "method": "ping"},
    message(),
    message(method=None),
    message(method=["tools/list"]),
    message(method="tools/list", params=[]),
    message(method="tools/list", params="x"),
    message(method="tools/call", params={}),
    message(method="tools/call", params={"name": None}),
    message(method="tools/call", params={"name": 5}),
    message(method="tools/call", params={"name": "nope"}),
    message(method="tools/call", params={"name": "search", "arguments": None}),
    message(method="tools/call", params={"name": "search", "arguments": "q"}),
    message(method="tools/call", params={"name": "search", "arguments": []}),
    message(method="initialize", params={"protocolVersion": ["x"]}),
    message(method="initialize", params={"protocolVersion": {"a": 1}}),
    {"jsonrpc": "2.0", "id": "x" * 10_000, "method": "ping"},
    {"jsonrpc": "2.0", "id": 1.5, "method": "ping"},
    {"jsonrpc": "2.0", "id": None, "method": "ping"},
    {"jsonrpc": "2.0", "id": 10**40, "method": "ping"},
    {"jsonrpc": "2.0", "id": [1], "method": "ping"},
    message(method="../../etc/passwd"),
    message(method="tools/call\n"),
]


@pytest.mark.parametrize("msg", HOSTILE_MESSAGES, ids=lambda m: repr(m)[:60])
@pytest.mark.parametrize("which", ["apps", "launcher"])
def test_both_servers_answer_odd_messages_without_raising(msg, which):
    server = Server(demo_apps()) if which == "apps" else LauncherServer(Launcher(dirs=[]))
    reply = server.handle(msg)
    if reply is not None:
        json.dumps(reply)
        assert reply["jsonrpc"] == "2.0"
        assert ("result" in reply) != ("error" in reply)


def test_a_message_without_an_id_is_a_notification_and_gets_no_answer():
    for method in ("ping", "tools/call", "initialize", "nope", "notifications/initialized"):
        assert Server(demo_apps()).handle({"jsonrpc": "2.0", "method": method}) is None


def test_protocol_version_falls_back_to_a_known_one():
    for asked in (None, 5, "9999-01-01", "", "2025-06-18\n", ["2025-06-18"]):
        reply = rpc(Server(demo_apps()), "initialize", {"protocolVersion": asked})
        assert reply["result"]["protocolVersion"] == "2025-06-18"
    reply = rpc(Server(demo_apps()), "initialize", {"protocolVersion": "2024-11-05"})
    assert reply["result"]["protocolVersion"] == "2024-11-05"


def run_serve(server, text: str) -> list[dict]:
    out = io.StringIO()
    server.serve(io.StringIO(text), out)
    text_out = out.getvalue()
    assert text_out == "" or text_out.endswith("\n")
    lines = text_out.split("\n")[:-1]  # the protocol splits on newlines only
    assert all(line for line in lines)
    return [json.loads(line) for line in lines]


def test_serve_answers_garbage_lines_and_goes_on():
    good = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"})
    two = '{"jsonrpc":"2.0","id":1,"method":"ping"}{"jsonrpc":"2.0","id":2,"method":"ping"}'
    garbage = ["not json", "{", "\ufeff" + good, "NaN", "[1,2,3]", "null", '"s"', "{" * 50, two]
    replies = run_serve(Server(demo_apps()), "\n".join(garbage + ["", "   ", good]) + "\n")
    assert len(replies) == len(garbage) + 1
    assert replies[-1] == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert all(r["jsonrpc"] == "2.0" for r in replies)
    assert all(r["error"]["code"] in (-32700, -32600) for r in replies[:-1])


def test_serve_gives_one_line_per_request_whatever_the_text_contains():
    texts = ["a\nb", "x\u2028y", "\x00", "\\u0000", '"', "'\"`$(id)", "%f;%u", "\U0001f600", "é"]
    requests = []
    for i, t in enumerate(texts):
        for name in ("describe", "install", "uninstall"):
            params = {"name": name, "arguments": {"app_id": t}}
            requests.append(json.dumps(message(id=i, method="tools/call", params=params)))
    replies = run_serve(Server(demo_apps()), "\n".join(requests) + "\n")
    assert len(replies) == len(requests)
    assert all(r["result"]["isError"] is True for r in replies)


def test_serve_handles_a_very_long_line_and_many_lines():
    params = {"name": "search", "arguments": {"query": "x" * 5_000_000}}
    big = json.dumps(message(method="tools/call", params=params))
    assert run_serve(Server(demo_apps()), big + "\n")[0]["result"]["isError"] is True
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    start = time.monotonic()
    assert len(run_serve(Server(demo_apps()), (ping + "\n") * 20_000)) == 20_000
    assert time.monotonic() - start < 10


class BrokenBackend(MemoryBackend):
    def installed(self):
        raise RuntimeError("the backend is broken")

    def remote_ref(self, remote, app_id):
        raise OSError("disk gone")


def test_a_failing_backend_gives_an_error_and_the_server_keeps_serving():
    server = Server(Apps(BrokenBackend({"flathub": [Component(ID)], "vilna": []})))
    for name, args in (
        ("list_installed", {}),
        ("describe", {"app_id": ID}),
        ("install", {"app_id": ID}),
        ("update", {}),
        ("uninstall", {"app_id": ID}),
    ):
        result = call(server, name, args)
        assert result["isError"] is True
        assert "broken" in result["content"][0]["text"] or "disk" in result["content"][0]["text"]
    assert rpc(server, "ping")["result"] == {}


def test_reads_never_carry_the_marks_of_a_change():
    for tool in TOOLS:
        hints = tool["annotations"]
        if hints.get("readOnlyHint") is True:
            assert "openWorldHint" not in hints and "destructiveHint" not in hints
        else:
            assert hints.get("openWorldHint") is True
            assert hints.get("readOnlyHint") is False
    reads = {t["name"] for t in TOOLS if t["annotations"].get("readOnlyHint")}
    assert reads == {"search", "describe", "list_installed"}


def test_server_main_without_libflatpak_says_so_and_stops(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "gi", None)
    monkeypatch.delitem(sys.modules, "vilna_mcp_apps.flatpak", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n'))
    assert server_main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "libflatpak is not available" in captured.err


def test_server_main_in_memory_mode_serves_stdin(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\nx\n'))
    assert server_main(["--memory"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert json.loads(out[0]) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert json.loads(out[1])["error"]["code"] == -32700


@pytest.mark.parametrize("flag", ["--evil", "--memory=1", "-x", "--flatpak", "apps"])
def test_main_functions_refuse_unknown_options(flag):
    for main in (server_main, launcher_main):
        with pytest.raises(SystemExit) as stop:
            main([flag])
        assert stop.value.code == 2


# ---------------------------------------------------------- the launcher


def fake_install(root: Path, app_id: str = ID) -> Path:
    current = root / app_id / "current"
    current.mkdir(parents=True)
    return current


@pytest.mark.parametrize("bad", [b for b in BAD_IDS if b])
def test_launcher_refuses_hostile_ids_even_when_the_path_exists(tmp_path, bad):
    # A decoy that would be found if the id were joined onto the folder
    # without a check.
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    (tmp_path / "victim" / "current").mkdir(parents=True)
    ran = []
    launcher = Launcher(dirs=[apps_dir], run=ran.append)
    with pytest.raises(AppsError):
        launcher.launch(bad)
    with pytest.raises(AppsError):
        launcher.launch("../victim")
    assert ran == []


@pytest.mark.parametrize("bad", NOT_STRINGS)
def test_launcher_refuses_ids_that_are_not_strings(tmp_path, bad):
    ran = []
    with pytest.raises(AppsError):
        Launcher(dirs=[tmp_path], run=ran.append).launch(bad)
    assert ran == []


def test_launcher_runs_exactly_flatpak_run_and_the_id(tmp_path):
    fake_install(tmp_path)
    ran = []
    out = Launcher(dirs=[tmp_path], run=ran.append).launch(ID)
    assert out == {"launched": ID}
    assert ran == [["flatpak", "run", ID]]


def test_launcher_needs_the_app_to_be_installed(tmp_path):
    ran = []
    launcher = Launcher(dirs=[tmp_path], run=ran.append)
    with pytest.raises(AppsError, match="not installed"):
        launcher.launch(ID)
    (tmp_path / ID).mkdir()  # a folder with no "current" is a removed app
    with pytest.raises(AppsError, match="not installed"):
        launcher.launch(ID)
    (tmp_path / ID / "current").symlink_to(tmp_path / "gone")  # dangling
    with pytest.raises(AppsError, match="not installed"):
        launcher.launch(ID)
    assert ran == []


def test_launcher_with_no_folders_launches_nothing():
    ran = []
    with pytest.raises(AppsError):
        Launcher(dirs=[], run=ran.append).launch(ID)
    assert ran == []


LAUNCH_EXTRAS = [
    {"app_id": ID, "args": ["--evil"]},
    {"app_id": ID, "command": "sh"},
    {"app_id": ID, "file": "/etc/passwd"},
    {"app_id": ID, "--command": "sh"},
    {"app_id": ID, "env": {"LD_PRELOAD": "/tmp/x.so"}},
    {"app_id": ID, "app_id2": ID},
    {"args": [ID]},
    {"app_id": ID, "": ""},
    {},
]


@pytest.mark.parametrize("args", LAUNCH_EXTRAS, ids=lambda a: ",".join(sorted(a)) or "empty")
def test_launcher_server_takes_exactly_one_argument(tmp_path, args):
    fake_install(tmp_path)
    ran = []
    server = LauncherServer(Launcher(dirs=[tmp_path], run=ran.append))
    assert call(server, "launch", args)["isError"] is True
    assert ran == []


@pytest.mark.parametrize("args", [None, [], "app_id", 1, [ID], True])
def test_launcher_server_arguments_must_be_an_object(tmp_path, args):
    ran = []
    server = LauncherServer(Launcher(dirs=[tmp_path], run=ran.append))
    assert call(server, "launch", args)["isError"] is True
    assert ran == []


@pytest.mark.parametrize("name", ["", "Launch", "launch ", "launch\n", "run", "exec", None, 5])
def test_launcher_server_has_one_tool_only(tmp_path, name):
    ran = []
    server = LauncherServer(Launcher(dirs=[tmp_path], run=ran.append))
    assert call(server, name, {"app_id": ID})["isError"] is True
    assert ran == []


def test_launcher_tool_list_is_one_tool_with_one_string_argument():
    reply = rpc(LauncherServer(Launcher(dirs=[])), "tools/list")
    (tool,) = reply["result"]["tools"]
    assert tool["name"] == "launch"
    assert list(tool["inputSchema"]["properties"]) == ["app_id"]
    assert tool["inputSchema"]["additionalProperties"] is False


DESKTOP_FILE = """[Desktop Entry]
Type=Application
Name=Evil
Exec=sh -c "touch {marker}" %f %U %i %c %k ; echo done
TryExec=/tmp/evil
Path=/
Terminal=true
"""


def test_launcher_never_reads_or_runs_the_desktop_files_of_an_app(tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    apps_dir = tmp_path / "apps"
    current = fake_install(apps_dir)
    export = current / "active/export/share/applications"
    export.mkdir(parents=True)
    for name in (f"{ID}.desktop", "evil.desktop", "x;y.desktop", "%f.desktop"):
        (export / name).write_text(DESKTOP_FILE.format(marker=marker))
    (apps_dir / ID / "evil.desktop").write_text(DESKTOP_FILE.format(marker=marker))

    opened: list[str] = []
    real_open = io.open

    def spy(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    monkeypatch.setattr(io, "open", spy)
    ran = []
    Launcher(dirs=[apps_dir], run=ran.append).launch(ID)
    assert ran == [["flatpak", "run", ID]]
    assert not [p for p in opened if p.endswith(".desktop")]
    assert not marker.exists()


class Recorder:
    def __init__(self, error: BaseException | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.error = error

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, 0)


def test_systemd_run_gets_a_plain_list_and_the_id_after_two_dashes(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    _systemd_run(["flatpak", "run", ID])
    ((argv, kwargs),) = rec.calls
    assert argv[0] == "systemd-run" and "--user" in argv
    assert argv[argv.index("--") + 1 :] == ["flatpak", "run", ID]
    assert argv.count("--") == 1
    assert kwargs.get("shell", False) is False
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["check"] is True
    assert 0 < kwargs["timeout"] <= 60
    assert "env" not in kwargs and "cwd" not in kwargs


def test_the_launcher_goes_through_systemd_run_only(tmp_path, monkeypatch):
    fake_install(tmp_path)
    rec = Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    launcher = Launcher(dirs=[tmp_path])
    assert launcher.run is _systemd_run
    launcher.launch(ID)
    assert [c[0][0] for c in rec.calls] == ["systemd-run"]


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, "x", stderr=b"\xff\xfe broken \x00"),
        subprocess.CalledProcessError(1, "x", stderr=b""),
        subprocess.CalledProcessError(1, "x", stderr=None),
        subprocess.TimeoutExpired("x", 30),
        subprocess.TimeoutExpired("x", 30, stderr=b"slow"),
        FileNotFoundError("systemd-run"),
        PermissionError("no"),
        OSError(7, "argument list too long"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_a_failing_systemd_run_is_an_apps_error(monkeypatch, error):
    monkeypatch.setattr(subprocess, "run", Recorder(error))
    with pytest.raises(AppsError, match="did not start"):
        _systemd_run(["flatpak", "run", ID])


def test_launcher_error_text_goes_to_the_client_as_text(tmp_path, monkeypatch):
    fake_install(tmp_path)
    err = subprocess.CalledProcessError(1, "x", stderr=b"\xff bad \x1b[31mred\x1b[0m")
    monkeypatch.setattr(subprocess, "run", Recorder(err))
    result = call(LauncherServer(Launcher(dirs=[tmp_path])), "launch", {"app_id": ID})
    assert result["isError"] is True
    json.dumps(result)


def test_installed_dirs_follow_the_environment_without_failing(monkeypatch):
    for home, data in (
        (None, None),
        ("/home/p", None),
        ("/home/p", ""),
        ("/home/p", "/data"),
        ("", ""),
        ("/home/p/../..", None),
    ):
        for key, value in (("HOME", home), ("XDG_DATA_HOME", data)):
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        dirs = installed_dirs()
        assert dirs[0] == Path("/var/lib/flatpak/app")
        assert len(dirs) == 2 and all(isinstance(d, Path) for d in dirs)
        assert dirs[1].name == "app" and dirs[1].parent.name == "flatpak"


def test_launcher_main_in_memory_mode_never_starts_anything(monkeypatch, capsys):
    params = [
        {"name": "launch", "arguments": {"app_id": ID}},
        {"name": "launch", "arguments": {"app_id": "../../bin/sh"}},
    ]
    lines = [json.dumps(message(id=i, method="tools/call", params=p)) for i, p in enumerate(params)]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n"))
    assert launcher_main(["--memory"]) == 0
    replies = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [r["result"]["isError"] for r in replies] == [True, True]


# --------------------------------------------- libflatpak, as a fake


class GLibError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class RefKind:
    APP = "app"
    RUNTIME = "runtime"


class FakeRemoteRef:
    def __init__(self, name, branch="stable", commit="c1", download=10, installed=20, meta=b""):
        self.name, self.branch, self.commit = name, branch, commit
        self.download, self.installed, self.meta = download, installed, meta

    def get_branch(self):
        return self.branch

    def get_commit(self):
        return self.commit

    def get_download_size(self):
        return self.download

    def get_installed_size(self):
        return self.installed

    def get_metadata(self):
        data = self.meta
        return None if data is None else types.SimpleNamespace(get_data=lambda: data)


class FakeInstalledRef:
    def __init__(self, name, origin="flathub", arch="x86_64", branch="stable", kind=RefKind.APP):
        self.name, self.origin, self.arch, self.branch, self.kind = name, origin, arch, branch, kind
        self.commit = "c0"

    def get_name(self):
        return self.name

    def get_arch(self):
        return self.arch

    def get_origin(self):
        return self.origin

    def get_commit(self):
        return self.commit

    def get_appdata_version(self):
        return "1.0"

    def get_appdata_name(self):
        return self.name

    def get_installed_size(self):
        return 5

    def format_ref(self):
        return f"{self.kind}/{self.name}/{self.arch}/{self.branch}"


class FakeRemote:
    def __init__(self, name, folder: Path, disabled=False, default_branch=None):
        self.name, self.folder, self.disabled, self.branch = name, folder, disabled, default_branch

    def get_name(self):
        return self.name

    def get_disabled(self):
        return self.disabled

    def get_default_branch(self):
        return self.branch

    def get_appstream_dir(self, arch):
        return types.SimpleNamespace(get_path=lambda: str(self.folder))


class FakeInstallation:
    def __init__(self, lib):
        self.lib = lib
        self.remote_list: list[FakeRemote] = []
        self.remote_refs: dict[tuple, FakeRemoteRef] = {}
        self.installed_refs: list[FakeInstalledRef] = []
        self.appstream_updates: list[tuple] = []
        self.appstream_error: str | None = None

    def list_remotes(self):
        return self.remote_list

    def get_remote_by_name(self, name, cancellable):
        for r in self.remote_list:
            if r.name == name:
                return r
        raise GLibError(f"no remote {name}")

    def update_appstream_sync(self, remote, arch, a, b):
        self.appstream_updates.append((remote, arch))
        if self.appstream_error:
            raise GLibError(self.appstream_error)

    def fetch_remote_ref_sync(self, remote, kind, name, arch, branch, cancellable):
        try:
            return self.remote_refs[(remote, kind, name, branch)]
        except KeyError:
            raise GLibError(f"{name} not found on {remote}") from None

    def list_installed_refs_by_kind(self, kind, cancellable):
        return [r for r in self.installed_refs if r.kind == kind]


class FakeTransaction:
    def __init__(self, inst):
        self.inst = inst
        self.ops: list[tuple] = []
        self.handlers: dict[str, object] = {}
        self.no_interaction = False
        self.default_sources = False
        self.ran = False

    def set_no_interaction(self, value):
        self.no_interaction = value

    def add_default_dependency_sources(self):
        self.default_sources = True

    def connect(self, signal, handler):
        self.handlers[signal] = handler

    def add_install(self, remote, ref, subpaths):
        self.ops.append(("install", remote, ref))

    def add_update(self, ref, subpaths, commit):
        self.ops.append(("update", ref))

    def add_uninstall(self, ref):
        self.ops.append(("uninstall", ref))

    def run(self, cancellable):
        self.ran = True
        if self.inst.lib.run_error:
            raise GLibError(self.inst.lib.run_error)
        for op in self.ops:
            if op[0] == "install":
                _kind, name, arch, branch = op[2].split("/")
                if (op[1], RefKind.APP, name, branch) not in self.inst.remote_refs:
                    raise GLibError(f"{op[2]} not found")
                self.inst.installed_refs.append(FakeInstalledRef(name, op[1], arch, branch))
            elif op[0] == "uninstall":
                kept = [r for r in self.inst.installed_refs if r.format_ref() != op[1]]
                self.inst.installed_refs = kept
            else:
                for r in self.inst.installed_refs:
                    if r.format_ref() == op[1]:
                        r.commit = "c-new"


class FakeLib:
    def __init__(self):
        self.installation = FakeInstallation(self)
        self.transactions: list[FakeTransaction] = []
        self.run_error: str | None = None
        self.folders: dict[str, Path] = {}
        lib = self

        class Installation:
            @staticmethod
            def new_system(cancellable):
                return lib.installation

        class Transaction:
            @staticmethod
            def new_for_installation(inst, cancellable):
                t = FakeTransaction(inst)
                lib.transactions.append(t)
                return t

        self.Flatpak = types.SimpleNamespace(
            Installation=Installation,
            Transaction=Transaction,
            RefKind=RefKind,
            get_default_arch=lambda: "x86_64",
        )
        self.GLib = types.SimpleNamespace(Error=GLibError)


@pytest.fixture
def libflatpak(monkeypatch, tmp_path):
    """The flatpak module, loaded on top of a fake libflatpak."""
    lib = FakeLib()
    gi = types.ModuleType("gi")
    gi.require_version = lambda *args: None
    repository = types.ModuleType("gi.repository")
    repository.Flatpak, repository.GLib = lib.Flatpak, lib.GLib
    gi.repository = repository
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    monkeypatch.delitem(sys.modules, "vilna_mcp_apps.flatpak", raising=False)
    module = importlib.import_module("vilna_mcp_apps.flatpak")
    for name in ("flathub", "vilna", "evil"):
        folder = tmp_path / name
        folder.mkdir()
        lib.folders[name] = folder
        lib.installation.remote_list.append(FakeRemote(name, folder))
    yield lib, module
    sys.modules.pop("vilna_mcp_apps.flatpak", None)


def offer(lib, remote, app_id, branch="stable", **kwargs):
    ref = FakeRemoteRef(app_id, branch, **kwargs)
    lib.installation.remote_refs[(remote, RefKind.APP, app_id, branch)] = ref


def put_catalogue(lib, remote, *ids):
    raw = xml_of(*(comp(i, f"<name>{i}</name>") for i in ids))
    (lib.folders[remote] / "appstream.xml.gz").write_bytes(gzip.compress(raw))


def test_the_backend_installs_only_with_a_four_part_app_ref(libflatpak):
    lib, module = libflatpak
    offer(lib, "flathub", ID)
    put_catalogue(lib, "flathub", ID)
    apps = Apps(module.FlatpakBackend())
    out = apps.install(ID)
    (t,) = lib.transactions
    assert t.ops == [("install", "flathub", f"app/{ID}/x86_64/stable")]
    assert out["remote"] == "flathub"
    assert [i["app_id"] for i in apps.list_installed()["apps"]] == [ID]


def test_the_backend_never_adds_a_remote_or_asks_a_question(libflatpak):
    lib, module = libflatpak
    offer(lib, "flathub", ID)
    put_catalogue(lib, "flathub", ID)
    Apps(module.FlatpakBackend()).install(ID)
    (t,) = lib.transactions
    assert t.no_interaction is True
    assert t.default_sources is True
    assert t.handlers["add-new-remote"]("any", "args") is False
    assert t.ran


@pytest.mark.parametrize("hostile", ["evil", "Flathub", "../flathub"])
def test_the_backend_ignores_remotes_that_are_not_on_the_list(libflatpak, hostile):
    lib, module = libflatpak
    offer(lib, "evil", ID)
    put_catalogue(lib, "evil", ID)
    apps = Apps(module.FlatpakBackend())
    with pytest.raises(AppsError):
        apps.install(ID)
    with pytest.raises(AppsError):
        apps.install(ID, hostile)
    assert lib.transactions == []
    assert apps.search("example")["apps"] == []


def test_disabled_remotes_are_not_used(libflatpak):
    lib, module = libflatpak
    for r in lib.installation.remote_list:
        r.disabled = r.name == "flathub"
    assert module.FlatpakBackend().remotes() == ["vilna", "evil"]
    offer(lib, "flathub", ID)
    put_catalogue(lib, "flathub", ID)
    with pytest.raises(AppsError, match="not set up"):
        Apps(module.FlatpakBackend()).install(ID, "flathub")
    assert lib.transactions == []


def test_runtimes_are_not_apps_to_remove(libflatpak):
    lib, module = libflatpak
    lib.installation.installed_refs = [
        FakeInstalledRef("org.gnome.Platform", kind=RefKind.RUNTIME, branch="47"),
        FakeInstalledRef("org.example.Other", arch="i386"),
    ]
    apps = Apps(module.FlatpakBackend())
    assert apps.list_installed()["apps"] == []
    for app_id in ("org.gnome.Platform", "org.example.Other"):
        with pytest.raises(AppsError, match="not installed"):
            apps.uninstall(app_id)
    assert lib.transactions == []


def test_a_runtime_id_cannot_be_installed_as_an_app(libflatpak):
    lib, module = libflatpak
    key = ("flathub", RefKind.RUNTIME, "org.gnome.Platform", "47")
    lib.installation.remote_refs[key] = FakeRemoteRef("org.gnome.Platform", "47")
    with pytest.raises(AppsError):
        Apps(module.FlatpakBackend()).install("org.gnome.Platform")
    assert lib.transactions == []


def test_update_asks_only_for_installed_apps_and_reports_what_changed(libflatpak):
    lib, module = libflatpak
    lib.installation.installed_refs = [
        FakeInstalledRef("org.example.One"),
        FakeInstalledRef("org.example.Two", origin="vilna"),
        FakeInstalledRef("org.example.Odd", origin="evil"),
    ]
    apps = Apps(module.FlatpakBackend())
    out = apps.update()
    assert sorted(out["updated"]) == ["org.example.One", "org.example.Two"]
    (t,) = lib.transactions
    assert sorted(op[1] for op in t.ops) == [
        "app/org.example.One/x86_64/stable",
        "app/org.example.Two/x86_64/stable",
    ]
    assert apps.update("org.example.Odd") == {"updated": []}
    assert len(lib.transactions) == 1  # nothing to do, nothing started
    # ids the backend does not know are skipped, not sent as refs
    assert module.FlatpakBackend().update(["org.gnome.Platform", "x"]) == []
    assert lib.transactions[-1].ops == []


def test_a_failing_transaction_is_an_apps_error_and_the_cache_is_tidied(
    libflatpak, monkeypatch, tmp_path
):
    lib, module = libflatpak
    cache = tmp_path / "cache"
    (cache / "mount-point").mkdir(parents=True)
    monkeypatch.setenv("FLATPAK_SYSTEM_CACHE_DIR", str(cache))
    offer(lib, "flathub", ID)
    put_catalogue(lib, "flathub", ID)
    lib.run_error = "polkit says no"
    server = Server(Apps(module.FlatpakBackend()))
    with pytest.raises(AppsError, match="Flatpak: polkit says no"):
        server.apps.install(ID)
    result = call(server, "install", {"app_id": ID})
    assert result["isError"] is True
    assert "polkit says no" in result["content"][0]["text"]
    assert not (cache / "mount-point").exists()
    assert call(server, "list_installed", {})["isError"] is False


def test_the_cache_tidy_removes_only_empty_folders(tmp_path, monkeypatch, libflatpak):
    _lib, module = libflatpak
    cache = tmp_path / "cache"
    outside = tmp_path / "outside"
    cache.mkdir()
    outside.mkdir()
    (cache / "empty").mkdir()
    (cache / "full").mkdir()
    (cache / "full" / "keep.txt").write_text("keep")
    (cache / "file").write_text("keep")
    (cache / "link").symlink_to(outside, target_is_directory=True)
    (cache / "deep" / "inner").mkdir(parents=True)
    monkeypatch.setenv("FLATPAK_SYSTEM_CACHE_DIR", str(cache))
    module._tidy_system_cache()
    assert sorted(p.name for p in cache.iterdir()) == ["deep", "file", "full", "link"]
    assert (cache / "full" / "keep.txt").read_text() == "keep"
    assert outside.exists()
    assert (cache / "deep" / "inner").exists()


def test_the_cache_tidy_does_nothing_without_a_usable_folder(tmp_path, monkeypatch, libflatpak):
    _lib, module = libflatpak
    (tmp_path / "bystander").mkdir()
    monkeypatch.delenv("FLATPAK_SYSTEM_CACHE_DIR", raising=False)
    module._tidy_system_cache()
    monkeypatch.setenv("FLATPAK_SYSTEM_CACHE_DIR", "")
    module._tidy_system_cache()
    monkeypatch.setenv("FLATPAK_SYSTEM_CACHE_DIR", str(tmp_path / "nope"))
    module._tidy_system_cache()
    afile = tmp_path / "afile"
    afile.write_text("x")
    monkeypatch.setenv("FLATPAK_SYSTEM_CACHE_DIR", str(afile))
    module._tidy_system_cache()
    assert (tmp_path / "bystander").exists() and afile.read_text() == "x"


def test_the_catalogue_is_fetched_once_when_it_is_missing(libflatpak):
    lib, module = libflatpak
    assert list(module.FlatpakBackend().components("flathub")) == []
    assert lib.installation.appstream_updates == [("flathub", "x86_64")]


def test_a_catalogue_that_cannot_be_fetched_is_an_error_a_person_can_read(libflatpak):
    lib, module = libflatpak
    lib.installation.appstream_error = "network down"
    with pytest.raises(AppsError, match="cannot be read.*network down"):
        Apps(module.FlatpakBackend()).search("anything")


def test_the_compressed_catalogue_is_read_before_the_plain_one(libflatpak):
    lib, module = libflatpak
    folder = lib.folders["flathub"]
    (folder / "appstream.xml").write_bytes(xml_of(comp("org.example.Plain")))
    (folder / "appstream.xml.gz").write_bytes(gzip.compress(xml_of(comp("org.example.Zipped"))))
    assert [c.id for c in module.FlatpakBackend().components("flathub")] == ["org.example.Zipped"]
    assert lib.installation.appstream_updates == []


def test_hostile_catalogue_files_reach_a_person_as_errors_and_not_as_crashes(libflatpak):
    lib, module = libflatpak
    folder = lib.folders["flathub"]
    server = Server(Apps(module.FlatpakBackend()))
    for raw in (DTD_BOMB, b"<components><component>", b"\x00" * 100, b"<a/>" * 10):
        (folder / "appstream.xml.gz").write_bytes(gzip.compress(raw))
        server.apps._catalogue = None
        result = call(server, "search", {"query": "x"})
        assert isinstance(result["isError"], bool)
        json.dumps(result)
    assert rpc(server, "ping")["result"] == {}


def test_remote_ref_sizes_and_metadata_survive_odd_answers(libflatpak):
    lib, module = libflatpak
    odd = b"\xff\xfe[Context]\nshared=network\n"
    offer(lib, "flathub", ID, download=None, installed=None, meta=odd)
    ref = module.FlatpakBackend().remote_ref("flathub", ID)
    assert (ref.download_size, ref.installed_size) == (0, 0)
    assert isinstance(ref.metadata, str) and "shared=network" in ref.metadata
    offer(lib, "flathub", "org.example.NoMeta", meta=None)
    assert module.FlatpakBackend().remote_ref("flathub", "org.example.NoMeta").metadata == ""
    assert module.FlatpakBackend().remote_ref("flathub", "org.example.Missing") is None


def test_the_branch_comes_from_what_the_remote_has(libflatpak):
    lib, module = libflatpak
    lib.installation.remote_list[1].branch = "beta"
    offer(lib, "vilna", "io.github.stdenis1980.Tool", branch="master")
    apps = Apps(module.FlatpakBackend())
    apps.install("io.github.stdenis1980.Tool", "vilna")
    assert lib.transactions[-1].ops == [
        ("install", "vilna", "app/io.github.stdenis1980.Tool/x86_64/master")
    ]


def test_describe_shows_the_permissions_the_remote_reports(libflatpak):
    lib, module = libflatpak
    meta = b"[Context]\nshared=network;\nsockets=x11;\nfilesystems=home;\n"
    offer(lib, "flathub", ID, meta=meta)
    put_catalogue(lib, "flathub", ID)
    entry = Apps(module.FlatpakBackend()).describe(ID)
    assert entry["permissions"] == [
        "the internet and local network",
        "the X11 display (can see other X11 windows)",
        "your whole home folder",
    ]
    assert "internet" in entry["facts"]["Can reach"]


def test_server_main_with_a_libflatpak_that_will_not_load_stops(monkeypatch, capsys):
    broken = types.ModuleType("gi")

    def refuse(*args):
        raise ValueError("Namespace Flatpak not available")

    broken.require_version = refuse
    monkeypatch.setitem(sys.modules, "gi", broken)
    monkeypatch.delitem(sys.modules, "vilna_mcp_apps.flatpak", raising=False)
    assert server_main([]) == 2
    assert "libflatpak is not available" in capsys.readouterr().err


def test_the_memory_backend_refuses_what_the_real_one_refuses():
    backend = MemoryBackend({"flathub": [Component(ID)], "vilna": []})
    with pytest.raises(AppsError):
        backend.install("flathub", "org.example.Missing")
    assert backend.remote_ref("flathub", "org.example.Missing") is None
    assert backend.remote_ref("evil", ID) is None
    assert isinstance(RemoteRef("c", 1, 2, ""), RemoteRef)
