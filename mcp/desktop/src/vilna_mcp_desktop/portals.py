# SPDX-License-Identifier: Apache-2.0
"""The "app permissions" tool (org.vilna.mcp.portals, ADR 0031,
permissions.md 9.3): what the Flatpak portals let each app do, read from
and written to the portals' permission store.

Only the seven permissions people also see in Settings > Apps. Taking one
away returns ``undo_arguments`` with the value from before, so Mitos can put
it back through ``restore``.

    vilna-mcp-portals
"""

from __future__ import annotations

import re
import sys

from vilna_mcp_desktop.bus import Bus
from vilna_mcp_desktop.mcp import CHANGE, READ, Server, ToolError, tool

STORE = "org.freedesktop.impl.portal.PermissionStore"
STORE_PATH = "/org/freedesktop/impl/portal/PermissionStore"
# Permission -> (table, id) of xdg-desktop-portal's permission store.
PERMISSIONS = {
    "camera": ("devices", "camera"),
    "microphone": ("devices", "microphone"),
    "speakers": ("devices", "speakers"),
    "location": ("location", "location"),
    "notifications": ("notifications", "notification"),
    "background": ("background", "background"),
    "screenshot": ("screenshot", "screenshot"),
}
# The location portal keeps [accuracy, last use]; the others ["yes" | "no" | "ask"].
ACCURACIES = ("NONE", "COUNTRY", "CITY", "NEIGHBORHOOD", "STREET", "EXACT")
WORDS = ("yes", "no", "ask")
APP_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z0-9_][A-Za-z0-9_-]*){2,}")
MAX_APPS = 500

_APP = {"type": "string", "maxLength": 255, "description": "Flatpak app id, e.g. org.gnome.Maps"}
_PERMISSION = {"type": "string", "enum": list(PERMISSIONS)}

TOOLS = [
    tool(
        "list",
        "Which apps may use the camera, microphone, speakers, location, notifications, "
        "run in the background or take screenshots (one app or all)",
        {"app": _APP},
        [],
        **READ,
    ),
    tool(
        "revoke",
        "Take a permission away from an app",
        {"app": _APP, "permission": _PERMISSION},
        ["app", "permission"],
        **CHANGE,
    ),
    tool(
        "grant",
        "Give an app a permission",
        {"app": _APP, "permission": _PERMISSION},
        ["app", "permission"],
        **CHANGE,
    ),
    tool(
        "restore",
        "Put a permission back as it was (undo_arguments of revoke or grant)",
        {
            "app": _APP,
            "permission": _PERMISSION,
            "previous": {"type": "array", "maxItems": 4},
        },
        ["app", "permission"],
        **CHANGE,
    ),
]


def state(permission: str, value: list[str] | None) -> str:
    """allowed, denied, ask or unset (the portal asks the person the first time)."""
    if not value:
        return "unset"
    if permission == "location":
        return "denied" if value[0] == "NONE" else "allowed"
    return {"yes": "allowed", "no": "denied", "ask": "ask"}.get(value[0], "unset")


def _check_app(app: str) -> str:
    if not APP_ID.fullmatch(app):
        raise ToolError(f"{app!r} is not a Flatpak app id")
    return app


def _seconds(text: str) -> bool:
    """A time the store keeps: ASCII digits only (not ² or ١٢٣)."""
    return text.isascii() and text.isdigit()


def _check_value(permission: str, value: list) -> list[str]:
    if not value or not all(isinstance(v, str) and len(v) <= 32 for v in value):
        raise ToolError("'previous' must be a list of short strings")
    if permission == "location":
        if len(value) != 2 or value[0] not in ACCURACIES or not _seconds(value[1]):
            raise ToolError("'previous' for location is [accuracy, time]")
    elif len(value) != 1 or value[0] not in WORDS:
        raise ToolError(f"'previous' must be one of {[[w] for w in WORDS]}")
    return list(value)


class Portals:
    def __init__(self, bus: Bus) -> None:
        self.bus = bus

    def _lookup(self, permission: str) -> dict[str, list[str]]:
        table, id_ = PERMISSIONS[permission]
        try:
            perms, _data = self.bus.call(
                "session", STORE, STORE_PATH, STORE, "Lookup", "ss", (table, id_), "a{sas}v"
            )
        except ToolError as e:
            if "NotFound" in str(e) or "No entry" in str(e):
                return {}  # nobody asked yet
            raise
        return {app: list(v) for app, v in perms.items() if app and APP_ID.fullmatch(app)}

    def _set(self, permission: str, app: str, value: list[str]) -> None:
        table, id_ = PERMISSIONS[permission]
        self.bus.call(
            "session",
            STORE,
            STORE_PATH,
            STORE,
            "SetPermission",
            "sbssas",
            (table, True, id_, app, value),
            None,
        )

    def list(self, app: str | None = None) -> dict:
        if app is not None:
            _check_app(app)
        apps: dict[str, dict[str, str]] = {}
        for permission in PERMISSIONS:
            for name, value in self._lookup(permission).items():
                if app is not None and name != app:
                    continue
                if name in apps or len(apps) < MAX_APPS:
                    apps.setdefault(name, {})[permission] = state(permission, value)
        return {"apps": dict(sorted(apps.items()))}

    def _change(self, app: str, permission: str, allow: bool) -> dict:
        _check_app(app)
        before = self._lookup(permission).get(app)
        if before:
            try:
                _check_value(permission, before)
            except ToolError:
                # restore would refuse it: a change now could not be undone.
                raise ToolError(
                    "the permission holds a value Gloxi cannot put back; "
                    "the person can change it in Settings"
                ) from None
        if permission == "location":
            used = before[1] if before and len(before) == 2 and _seconds(before[1]) else "0"
            value = ["EXACT" if allow else "NONE", used]
        else:
            value = ["yes" if allow else "no"]
        self._set(permission, app, value)
        undo: dict = {"app": app, "permission": permission}
        if before:
            undo["previous"] = before
        return {
            "app": app,
            "permission": permission,
            "before": state(permission, before),
            "now": state(permission, value),
            "undo_arguments": undo,
        }

    def revoke(self, app: str, permission: str) -> dict:
        return self._change(app, permission, allow=False)

    def grant(self, app: str, permission: str) -> dict:
        return self._change(app, permission, allow=True)

    def restore(self, app: str, permission: str, previous: list | None = None) -> dict:
        """Back to ``previous``; without it, unset again (the portal asks the person)."""
        _check_app(app)
        before = self._lookup(permission).get(app)
        if previous is None:
            # Never set before: forget it, so the portal asks again.
            table, id_ = PERMISSIONS[permission]
            if before:
                self.bus.call(
                    "session",
                    STORE,
                    STORE_PATH,
                    STORE,
                    "DeletePermission",
                    "sss",
                    (table, id_, app),
                    None,
                )
            value = None
        else:
            value = _check_value(permission, previous)
            self._set(permission, app, value)
        undo: dict = {"app": app, "permission": permission}
        if before:
            undo["previous"] = before
        return {
            "app": app,
            "permission": permission,
            "now": state(permission, value),
            "undo_arguments": undo,
        }


def server(portals: Portals) -> Server:
    return Server(
        "vilna-mcp-portals",
        TOOLS,
        {
            "list": portals.list,
            "revoke": portals.revoke,
            "grant": portals.grant,
            "restore": portals.restore,
        },
    )


def main(argv: list[str] | None = None) -> int:
    try:
        from vilna_mcp_desktop.bus import GioBus

        bus = GioBus()
    except (ImportError, ValueError) as e:
        print(f"vilna-mcp-portals: PyGObject is not available ({e})", file=sys.stderr)
        return 2
    server(Portals(bus)).serve(sys.stdin, sys.stdout)
    return 0
