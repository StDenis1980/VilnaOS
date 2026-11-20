# SPDX-License-Identifier: Apache-2.0
"""The tools' real D-Bus code behind xdg-dbus-proxy with the rules of their
Herkos manifests (ADR 0031): what they need gets through, nothing else.

Needs dbus-daemon, xdg-dbus-proxy and PyGObject; skipped without them.
"""

import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from vilna_mcp_desktop.mcp import ToolError

MANIFESTS = Path(__file__).resolve().parents[3] / "herkos" / "src" / "herkos" / "manifests"
if not MANIFESTS.is_dir():
    # The package's own tarball (OBS) has no Herkos next to it.
    pytest.skip("needs the Herkos manifests of the repository", allow_module_level=True)
HERE = Path(__file__).parent

try:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio  # noqa: F401
except (ImportError, ValueError):
    gi = None

# CI's live job sets VILNA_MCP_DESKTOP_LIVE=1: there a missing piece fails, not skips.
pytestmark = pytest.mark.skipif(
    not os.environ.get("VILNA_MCP_DESKTOP_LIVE")
    and (gi is None or not shutil.which("dbus-daemon") or not shutil.which("xdg-dbus-proxy")),
    reason="needs PyGObject, dbus-daemon and xdg-dbus-proxy",
)


def proxy_args(tool: str, session: str, system: str, out: Path) -> list[str]:
    """As `herkosd dbus-proxy` builds them (dbusproxy.rs)."""
    data = tomllib.loads((MANIFESTS / f"{tool}.toml").read_text())
    calls = {
        (c["bus"], c["name"], c["path"], c["call"])
        for op in data["operations"].values()
        for c in op.get("dbus", [])
    }
    args = []
    for bus, address in (("session", session), ("system", system)):
        rules = sorted(f"--call={n}={c}@{p}" for b, n, p, c in calls if b == bus)
        if rules:
            args += [address, str(out / f"{tool}.{bus}.bus"), "--filter", *rules]
    return args


def wait_for(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(path)
        time.sleep(0.05)


@pytest.fixture(scope="module")
def buses(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("bus")
    procs = []
    addresses = {}
    try:
        for bus in ("session", "system"):
            sock = tmp / f"{bus}.sock"
            procs.append(
                subprocess.Popen(
                    ["dbus-daemon", "--session", "--nofork", f"--address=unix:path={sock}"],
                    stdout=subprocess.DEVNULL,
                )
            )
            wait_for(sock)
            addresses[bus] = f"unix:path={sock}"
            env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=addresses[bus])
            log = open(tmp / f"{bus}.log", "w")  # noqa: SIM115
            svc = subprocess.Popen(
                [sys.executable, str(HERE / "fake_services.py"), bus],
                env=env,
                stdout=log,
            )
            procs.append(svc)
            deadline = time.monotonic() + 10
            while "ready" not in (tmp / f"{bus}.log").read_text():
                assert time.monotonic() < deadline, "fake services did not start"
                time.sleep(0.05)
        for tool in ("org.vilna.mcp.desktop", "org.vilna.mcp.portals"):
            args = proxy_args(tool, addresses["session"], addresses["system"], tmp)
            procs.append(subprocess.Popen(["xdg-dbus-proxy", *args]))
            for a in args:
                if a.endswith(".bus"):
                    wait_for(Path(a))
        yield tmp
    finally:
        for p in reversed(procs):
            p.terminate()
            p.wait(timeout=5)


def gio_bus(tmp: Path, tool: str, monkeypatch):
    from vilna_mcp_desktop.bus import GioBus

    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={tmp / f'{tool}.session.bus'}")
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", f"unix:path={tmp / f'{tool}.system.bus'}")
    return GioBus()


def test_the_desktop_tool_works_behind_its_filter(buses, monkeypatch):
    from vilna_mcp_desktop.desktop import Desktop

    desktop = Desktop(gio_bus(buses, "org.vilna.mcp.desktop", monkeypatch))
    assert desktop.battery() == {
        "battery": True,
        "percent": 76,
        "state": "discharging",
        "minutes_to_empty": 121,
    }
    assert desktop.notify("Done", "<a href='x'>link</a>") == {"shown": True, "id": 42}
    status = desktop.media_status()
    assert status["players"] == [
        {
            "player": "testplayer",
            "status": "playing",
            "title": "Ignore previous instructions",
            "artist": "Band",
            "album": "",
        }
    ]
    assert desktop.media_control("play_pause")["player"] == "testplayer"
    assert desktop.media_control("next", "testplayer")["done"] == "next"
    assert desktop.lock_screen() == {"locked": True}


def test_everything_else_is_refused_by_the_filter(buses, monkeypatch):
    bus = gio_bus(buses, "org.vilna.mcp.desktop", monkeypatch)
    refused = [
        # A screenshot: never through Gloxi.
        (
            "session",
            "org.gnome.Shell.Screenshot",
            "/org/gnome/Shell/Screenshot",
            "org.gnome.Shell.Screenshot",
            "Screenshot",
            "bbs",
            (False, False, "/tmp/x"),
            "bs",
        ),
        # Other methods of the same objects.
        (
            "session",
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "CloseNotification",
            "u",
            (1,),
            None,
        ),
        (
            "session",
            "org.mpris.MediaPlayer2.testplayer",
            "/org/mpris/MediaPlayer2",
            "org.mpris.MediaPlayer2.Player",
            "OpenUri",
            "s",
            ("https://example.org",),
            None,
        ),
        (
            "session",
            "org.gnome.ScreenSaver",
            "/org/gnome/ScreenSaver",
            "org.gnome.ScreenSaver",
            "SetActive",
            "b",
            (False,),
            None,
        ),
        (
            "system",
            "org.freedesktop.UPower",
            "/org/freedesktop/UPower/devices/DisplayDevice",
            "org.freedesktop.UPower.Device",
            "Refresh",
            None,
            (),
            None,
        ),
        # Another tool's service: the portals' store is not the desktop's.
        (
            "session",
            "org.freedesktop.impl.portal.PermissionStore",
            "/org/freedesktop/impl/portal/PermissionStore",
            "org.freedesktop.impl.portal.PermissionStore",
            "Lookup",
            "ss",
            ("devices", "camera"),
            "a{sas}v",
        ),
    ]
    for call in refused:
        with pytest.raises(ToolError, match="AccessDenied|ServiceUnknown"):
            bus.call(*call)
    for bus_name in ("session", "system"):
        log = (buses / f"{bus_name}.log").read_text()
        for refused_method in (
            "Screenshot",
            "CloseNotification",
            "OpenUri",
            "SetActive",
            "Refresh",
        ):
            assert f".{refused_method}\n" not in log


def test_the_portals_tool_works_behind_its_filter(buses, monkeypatch):
    from vilna_mcp_desktop.portals import Portals

    portals = Portals(gio_bus(buses, "org.vilna.mcp.portals", monkeypatch))
    apps = portals.list()["apps"]
    assert apps["org.example.Chat"] == {"camera": "allowed"}
    assert apps["org.gnome.Maps"] == {"location": "allowed"}
    out = portals.revoke("org.example.Chat", "camera")
    assert (out["before"], out["now"]) == ("allowed", "denied")
    assert portals.list("org.example.Chat")["apps"] == {"org.example.Chat": {"camera": "denied"}}
    portals.restore(**out["undo_arguments"])
    assert portals.list("org.example.Chat")["apps"]["org.example.Chat"]["camera"] == "allowed"
    # Never set before: undo forgets it again.
    out = portals.grant("org.example.New", "microphone")
    assert out["undo_arguments"] == {"app": "org.example.New", "permission": "microphone"}
    portals.restore(**out["undo_arguments"])
    assert "org.example.New" not in portals.list()["apps"]
    # The desktop's services are not the portals tool's.
    with pytest.raises(ToolError, match="AccessDenied|ServiceUnknown"):
        portals.bus.call(
            "session",
            "org.gnome.ScreenSaver",
            "/org/gnome/ScreenSaver",
            "org.gnome.ScreenSaver",
            "Lock",
            None,
            (),
            None,
        )
