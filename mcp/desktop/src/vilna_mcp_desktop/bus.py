# SPDX-License-Identifier: Apache-2.0
"""D-Bus for the tools, through Gio (ADR 0031).

Under the system Herkos the tool does not see the person's buses: its unit
points DBUS_SESSION_BUS_ADDRESS and DBUS_SYSTEM_BUS_ADDRESS at the sockets of
xdg-dbus-proxy (vilna-dbus-proxy-<tool>@), which lets through only the calls
the tool's manifest names. Anything else comes back as AccessDenied.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from vilna_mcp_desktop.mcp import ToolError

CALL_TIMEOUT_MS = 10_000
# The proxy unit starts together with the tool; give it a moment.
CONNECT_TIMEOUT = 5.0


class Bus(Protocol):
    def call(
        self,
        bus: str,
        name: str,
        path: str,
        interface: str,
        method: str,
        signature: str | None,
        args: tuple,
        reply: str | None,
    ) -> tuple: ...

    def names(self, bus: str) -> list[str]: ...


class GioBus:
    """The buses as Gio finds them (the environment), connected on first use."""

    def __init__(self) -> None:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        self._Gio, self._GLib = Gio, GLib
        self._connections: dict[str, Any] = {}

    def _connection(self, bus: str):
        if bus in self._connections:
            return self._connections[bus]
        kind = {"session": self._Gio.BusType.SESSION, "system": self._Gio.BusType.SYSTEM}[bus]
        address = self._Gio.dbus_address_get_for_bus_sync(kind, None)
        deadline = time.monotonic() + CONNECT_TIMEOUT
        while True:
            try:
                conn = self._Gio.DBusConnection.new_for_address_sync(
                    address,
                    self._Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                    | self._Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                    None,
                    None,
                )
                break
            except self._GLib.Error as e:
                if time.monotonic() > deadline:
                    raise ToolError(f"the {bus} bus is not reachable: {e.message}") from None
                time.sleep(0.1)
        self._connections[bus] = conn
        return conn

    def call(self, bus, name, path, interface, method, signature, args, reply) -> tuple:
        GLib = self._GLib
        params = GLib.Variant(f"({signature})", args) if signature else None
        try:
            out = self._connection(bus).call_sync(
                name,
                path,
                interface,
                method,
                params,
                GLib.VariantType(f"({reply})") if reply else None,
                self._Gio.DBusCallFlags.NONE,
                CALL_TIMEOUT_MS,
                None,
            )
        except GLib.Error as e:
            raise ToolError(f"{interface}.{method}: {e.message}") from None
        return out.unpack() if out is not None else ()

    def names(self, bus: str) -> list[str]:
        (names,) = self.call(
            bus,
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "ListNames",
            None,
            (),
            "as",
        )
        return list(names)


def text(value: Any, limit: int = 300) -> str:
    """A string from another program, cut to a sane length."""
    if not isinstance(value, str):
        return ""
    return value if len(value) <= limit else value[: limit - 1] + "…"
