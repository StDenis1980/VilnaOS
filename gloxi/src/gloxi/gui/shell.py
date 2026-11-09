# SPDX-License-Identifier: GPL-3.0-or-later
"""Talks to the Vilna GNOME Shell extension over D-Bus (shell-extension/README.md).

On Wayland only the Shell can keep Gloxi above other windows and at the
right edge of the screen. The extension offers that as properties of
``io.github.stdenis1980.VilnaShell1``; the Flatpak needs
``--talk-name=io.github.stdenis1980.VilnaShell``. Without the extension (any
other Linux) :func:`connect` returns ``None`` and Gloxi hides the switches.
"""

from __future__ import annotations

import sys

from gi.repository import Gio, GLib

BUS_NAME = "io.github.stdenis1980.VilnaShell"
OBJECT_PATH = "/io/github/stdenis1980/VilnaShell"
INTERFACE = "io.github.stdenis1980.VilnaShell1"
# Interface version 2 has GloxiPanelEdge and GloxiPanelSize.
PANEL_VERSION = 2
TIMEOUT_MS = 500


class VilnaShell:
    def __init__(self, bus: Gio.DBusConnection, version: int) -> None:
        self._bus = bus
        self.version = version

    @property
    def places_panel(self) -> bool:
        return self.version >= PANEL_VERSION

    def get(self, name: str):
        try:
            reply = self._bus.call_sync(
                BUS_NAME,
                OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", (INTERFACE, name)),
                GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NO_AUTO_START,
                TIMEOUT_MS,
                None,
            )
        except GLib.Error:
            return None
        return reply.unpack()[0]

    def set(self, name: str, value: GLib.Variant) -> None:
        """Fire and forget: the Shell applies it when it can."""

        def done(bus, result) -> None:
            try:
                bus.call_finish(result)
            except GLib.Error as exc:
                print(f"gloxi: the Shell did not take {name}: {exc.message}", file=sys.stderr)

        self._bus.call(
            BUS_NAME,
            OBJECT_PATH,
            "org.freedesktop.DBus.Properties",
            "Set",
            GLib.Variant("(ssv)", (INTERFACE, name, value)),
            None,
            Gio.DBusCallFlags.NO_AUTO_START,
            TIMEOUT_MS,
            None,
            done,
        )

    @property
    def keep_above(self) -> bool:
        return bool(self.get("GloxiKeepAbove"))

    def set_keep_above(self, on: bool) -> None:
        self.set("GloxiKeepAbove", GLib.Variant("b", bool(on)))

    def set_panel_size(self, size: str) -> None:
        if self.places_panel:
            self.set("GloxiPanelSize", GLib.Variant("s", size))


def connect() -> VilnaShell | None:
    """The extension, if it runs in this session; asked once, at start."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error:
        return None
    probe = VilnaShell(bus, 0)
    version = probe.get("Version")
    if not isinstance(version, int):
        return None
    return VilnaShell(bus, version)
