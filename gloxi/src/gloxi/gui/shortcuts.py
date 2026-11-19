# SPDX-License-Identifier: GPL-3.0-or-later
"""The key that shows or hides Gloxi from anywhere (ADR 0023, section 5).

Gloxi asks for it through the GlobalShortcuts portal (GNOME 48 and later):
it proposes Super+G and the assistant key of new laptops (which sends
Super+Shift+F23), and the person agrees or picks another key in GNOME's own
window. Gloxi never grabs a key by itself. Without the portal (GNOME 46-47)
nothing is bound, and ``gloxi-gtk --toggle`` is the command to put on a key
of one's own (``FALLBACK_COMMAND``).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from gettext import gettext as _

from gi.repository import Gio, GLib

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
INTERFACE = "org.freedesktop.portal.GlobalShortcuts"
REQUEST = "org.freedesktop.portal.Request"

# (id, preferred trigger in the portal's notation)
SHORTCUTS = (
    ("toggle", "LOGO+g"),
    ("toggle-assistant-key", "LOGO+SHIFT+F23"),
)
FALLBACK_COMMAND = "flatpak run io.github.stdenis1980.Gloxi --toggle"


def token() -> str:
    """A handle token: letters, digits and underscores only (portal spec)."""
    return "gloxi_" + secrets.token_hex(8)


def request_path(unique_name: str, handle_token: str) -> str:
    """Where the portal's Request object for our call will be."""
    sender = unique_name.lstrip(":").replace(".", "_")
    return f"/org/freedesktop/portal/desktop/request/{sender}/{handle_token}"


def shortcut_entries() -> list[tuple[str, dict[str, GLib.Variant]]]:
    """The ``a(sa{sv})`` argument of BindShortcuts."""
    description = _("Show or hide Gloxi")
    return [
        (
            sid,
            {
                "description": GLib.Variant("s", description),
                "preferred_trigger": GLib.Variant("s", trigger),
            },
        )
        for sid, trigger in SHORTCUTS
    ]


class GlobalShortcut:
    """Binds the shortcuts once per run and calls ``on_activated`` on a press.

    Everything is asynchronous and every failure only means "no shortcut":
    a missing portal, an old GNOME, or the person saying no.
    """

    def __init__(self, on_activated: Callable[[], None], on_unavailable: Callable[[], None]):
        self._on_activated = on_activated
        self._on_unavailable = on_unavailable
        self._bus: Gio.DBusConnection | None = None
        self._session: str | None = None
        self._subscriptions: list[int] = []

    def start(self) -> None:
        Gio.bus_get(Gio.BusType.SESSION, None, self._got_bus)

    def stop(self) -> None:
        if self._bus is None:
            return
        for sub in self._subscriptions:
            self._bus.signal_unsubscribe(sub)
        self._subscriptions.clear()
        if self._session:
            self._bus.call(
                BUS_NAME, self._session, "org.freedesktop.portal.Session", "Close",
                None, None, Gio.DBusCallFlags.NONE, -1, None, None,
            )  # fmt: skip
            self._session = None

    # -- steps --------------------------------------------------------------

    def _got_bus(self, _source, result) -> None:
        try:
            self._bus = Gio.bus_get_finish(result)
        except GLib.Error:
            self._on_unavailable()
            return
        handle = token()
        self._await_response(handle, self._session_created)
        options = {
            "handle_token": GLib.Variant("s", handle),
            "session_handle_token": GLib.Variant("s", token()),
        }
        self._call("CreateSession", GLib.Variant("(a{sv})", (options,)))

    def _session_created(self, response: int, results: dict) -> None:
        session = results.get("session_handle") if response == 0 else None
        if not isinstance(session, str):
            self._on_unavailable()
            return
        self._session = session
        assert self._bus is not None
        self._subscriptions.append(
            self._bus.signal_subscribe(
                BUS_NAME,
                INTERFACE,
                "Activated",
                OBJECT_PATH,
                None,
                Gio.DBusSignalFlags.NONE,
                self._activated,
            )  # fmt: skip
        )
        handle = token()
        self._await_response(handle, self._bound)
        self._call(
            "BindShortcuts",
            GLib.Variant(
                "(oa(sa{sv})sa{sv})",
                (session, shortcut_entries(), "", {"handle_token": GLib.Variant("s", handle)}),
            ),
        )

    def _bound(self, response: int, results: dict) -> None:
        if response != 0 or not results.get("shortcuts"):
            self._on_unavailable()

    def _activated(self, _conn, _sender, _path, _iface, _signal, params) -> None:
        session, shortcut_id = params.unpack()[:2]
        if session == self._session and shortcut_id in {sid for sid, _t in SHORTCUTS}:
            self._on_activated()

    # -- plumbing -----------------------------------------------------------

    def _call(self, method: str, params: GLib.Variant) -> None:
        assert self._bus is not None

        def done(bus, result) -> None:
            try:
                bus.call_finish(result)
            except GLib.Error:  # no portal, or no GlobalShortcuts in it
                self._on_unavailable()

        self._bus.call(
            BUS_NAME, OBJECT_PATH, INTERFACE, method, params, None,
            Gio.DBusCallFlags.NONE, -1, None, done,
        )  # fmt: skip

    def _await_response(self, handle: str, then: Callable[[int, dict], None]) -> None:
        """Listen for the Request's Response before calling (no race)."""
        assert self._bus is not None
        path = request_path(self._bus.get_unique_name() or "", handle)
        sub = 0

        def response(_conn, _sender, _path, _iface, _signal, params) -> None:
            assert self._bus is not None
            self._bus.signal_unsubscribe(sub)
            code, results = params.unpack()
            then(code, results)

        sub = self._bus.signal_subscribe(
            BUS_NAME, REQUEST, "Response", path, None, Gio.DBusSignalFlags.NONE, response
        )
