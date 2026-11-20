# SPDX-License-Identifier: Apache-2.0
"""The "desktop" tool (org.vilna.mcp.desktop, ADR 0031): battery,
notifications from Gloxi, music and video players (MPRIS), screen lock.

Every D-Bus call here is named in the tool's manifest; the system Herkos
lets nothing else through (tests/test_proxy.py runs this code behind
xdg-dbus-proxy with the manifest's rules).

    vilna-mcp-desktop
"""

from __future__ import annotations

import sys
from html import escape

from vilna_mcp_desktop.bus import Bus, text
from vilna_mcp_desktop.mcp import CHANGE, READ, Server, ToolError, tool

PROPS = "org.freedesktop.DBus.Properties"
UPOWER = "org.freedesktop.UPower"
DISPLAY_DEVICE = "/org/freedesktop/UPower/devices/DisplayDevice"
NOTIFICATIONS = "org.freedesktop.Notifications"
NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"
MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
SCREENSAVER = "org.gnome.ScreenSaver"
SCREENSAVER_PATH = "/org/gnome/ScreenSaver"
GLOXI_APP = "io.github.stdenis1980.Gloxi"
MAX_PLAYERS = 16
# UPower's device state, as words for the model.
STATES = {
    1: "charging",
    2: "discharging",
    3: "empty",
    4: "fully-charged",
    5: "pending-charge",
    6: "pending-discharge",
}
MEDIA_ACTIONS = {
    "play_pause": "PlayPause",
    "play": "Play",
    "pause": "Pause",
    "stop": "Stop",
    "next": "Next",
    "previous": "Previous",
}

TOOLS = [
    tool("battery", "The battery charge and whether it is charging", {}, [], **READ),
    tool(
        "notify",
        "Show a desktop notification from Gloxi (plain text, no links or buttons)",
        {
            "title": {"type": "string", "maxLength": 120},
            "body": {"type": "string", "maxLength": 1000},
        },
        ["title"],
        readOnlyHint=False,
        destructiveHint=False,
    ),
    tool("media_status", "What each music or video player is playing", {}, [], **READ),
    tool(
        "media_control",
        "Play, pause, stop or skip in a player (a name from media_status; "
        "may be left out when only one player is open)",
        {
            "action": {"type": "string", "enum": list(MEDIA_ACTIONS)},
            "player": {"type": "string", "maxLength": 200},
        },
        ["action"],
        **CHANGE,
    ),
    tool("lock_screen", "Lock the screen now", {}, [], **CHANGE),
]


class Desktop:
    def __init__(self, bus: Bus) -> None:
        self.bus = bus

    def battery(self) -> dict:
        (props,) = self.bus.call(
            "system",
            UPOWER,
            DISPLAY_DEVICE,
            PROPS,
            "GetAll",
            "s",
            ("org.freedesktop.UPower.Device",),
            "a{sv}",
        )
        if not props.get("IsPresent", False):
            return {"battery": False}
        out = {
            "battery": True,
            "percent": round(float(props.get("Percentage", 0.0))),
            "state": STATES.get(props.get("State"), "unknown"),
        }
        for key, name in (("TimeToEmpty", "minutes_to_empty"), ("TimeToFull", "minutes_to_full")):
            seconds = props.get(key, 0)
            if isinstance(seconds, int) and seconds > 0:
                out[name] = seconds // 60
        return out

    def notify(self, title: str, body: str = "") -> dict:
        # GNOME reads a little markup in the body; escaped, nothing becomes a link.
        (nid,) = self.bus.call(
            "session",
            NOTIFICATIONS,
            NOTIFICATIONS_PATH,
            NOTIFICATIONS,
            "Notify",
            "susssasa{sv}i",
            ("Gloxi", 0, GLOXI_APP, title, escape(body, quote=False), [], {}, -1),
            "u",
        )
        return {"shown": True, "id": nid}

    def _players(self) -> list[str]:
        names = sorted(n for n in self.bus.names("session") if n.startswith(MPRIS_PREFIX))
        return names[:MAX_PLAYERS]

    def media_status(self) -> dict:
        players = []
        for name in self._players():
            try:
                (props,) = self.bus.call(
                    "session", name, MPRIS_PATH, PROPS, "GetAll", "s", (PLAYER,), "a{sv}"
                )
            except ToolError:
                continue  # it closed meanwhile
            meta = props.get("Metadata") or {}
            artists = meta.get("xesam:artist") or []
            players.append(
                {
                    "player": name.removeprefix(MPRIS_PREFIX),
                    "status": text(props.get("PlaybackStatus"), 20).lower() or "unknown",
                    "title": text(meta.get("xesam:title")),
                    "artist": ", ".join(text(a) for a in artists[:4] if isinstance(a, str)),
                    "album": text(meta.get("xesam:album")),
                }
            )
        return {"players": players}

    def media_control(self, action: str, player: str | None = None) -> dict:
        players = self._players()
        if player is None:
            if len(players) != 1:
                short = [p.removeprefix(MPRIS_PREFIX) for p in players]
                raise ToolError(f"say which player: {short}" if short else "no player is open")
            name = players[0]
        else:
            name = MPRIS_PREFIX + player
            if name not in players:
                raise ToolError(f"no player {player!r} is open")
        self.bus.call("session", name, MPRIS_PATH, PLAYER, MEDIA_ACTIONS[action], None, (), None)
        return {"player": name.removeprefix(MPRIS_PREFIX), "done": action}

    def lock_screen(self) -> dict:
        self.bus.call("session", SCREENSAVER, SCREENSAVER_PATH, SCREENSAVER, "Lock", None, (), None)
        return {"locked": True}


def server(desktop: Desktop) -> Server:
    return Server(
        "vilna-mcp-desktop",
        TOOLS,
        {
            "battery": desktop.battery,
            "notify": desktop.notify,
            "media_status": desktop.media_status,
            "media_control": desktop.media_control,
            "lock_screen": desktop.lock_screen,
        },
    )


def main(argv: list[str] | None = None) -> int:
    try:
        from vilna_mcp_desktop.bus import GioBus

        bus = GioBus()
    except (ImportError, ValueError) as e:
        print(f"vilna-mcp-desktop: PyGObject is not available ({e})", file=sys.stderr)
        return 2
    server(Desktop(bus)).serve(sys.stdin, sys.stdout)
    return 0
