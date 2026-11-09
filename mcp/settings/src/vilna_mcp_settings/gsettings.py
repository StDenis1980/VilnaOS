# SPDX-License-Identifier: Apache-2.0
"""GSettings (dconf) backend, through PyGObject.

PyGObject comes from the system or the Flatpak runtime, not from PyPI, so
this module is imported only when the server really starts.

Some schemas (night light is gnome-settings-daemon's) may be missing from a
Flatpak runtime. ``VILNA_MCP_SETTINGS_SCHEMA_DIR`` names an extra directory
of compiled schemas to look in; the values still go to the person's dconf.
"""

from __future__ import annotations

import contextlib
import os

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from vilna_mcp_settings.settings import SettingsError, Value  # noqa: E402

SCHEMA_DIR_ENV = "VILNA_MCP_SETTINGS_SCHEMA_DIR"


class GSettingsBackend:
    def __init__(self) -> None:
        source = Gio.SettingsSchemaSource.get_default()
        extra = os.environ.get(SCHEMA_DIR_ENV)
        if extra and os.path.isdir(extra):
            with contextlib.suppress(GLib.Error):
                source = Gio.SettingsSchemaSource.new_from_directory(extra, source, False)
        self._source = source
        self._settings: dict[str, Gio.Settings] = {}

    def _lookup(self, schema: str, key: str):
        if self._source is None:
            return None, None
        found = self._source.lookup(schema, True)
        if found is None or not found.has_key(key):
            return None, None
        if schema not in self._settings:
            self._settings[schema] = Gio.Settings.new_full(found, None, None)
        return self._settings[schema], found.get_key(key)

    def get(self, schema: str, key: str) -> Value | None:
        settings, _ = self._lookup(schema, key)
        if settings is None:
            return None
        return settings.get_value(key).unpack()

    def set(self, schema: str, key: str, value: Value) -> None:
        settings, info = self._lookup(schema, key)
        if settings is None:
            raise SettingsError(f"{schema} {key} is not available on this system")
        variant = GLib.Variant(info.get_value_type().dup_string(), value)
        if not info.range_check(variant):
            raise SettingsError(f"{key}: {value!r} is out of range on this system")
        if not settings.set_value(key, variant):
            raise SettingsError(f"{key} is not writable")
        Gio.Settings.sync()
