# SPDX-License-Identifier: Apache-2.0
"""Volume, mute and output of the default sound output (ADR 0023, step 3).

GNOME keeps these in PipeWire, not in GSettings. We talk to PipeWire
through its PulseAudio protocol with ``pulsectl`` (MIT), which loads the
system's libpulse; in the Flatpak that needs ``--socket=pulseaudio``, the
same permission the voice of Gloxi needs (ADR 0015).
"""

from __future__ import annotations

import pulsectl

from vilna_mcp_settings.settings import SettingsError, Value

MAX_OUTPUTS = 32


class PulseSound:
    def __init__(self) -> None:
        try:
            self._pulse = pulsectl.Pulse("vilna-mcp-settings")
        except pulsectl.PulseError as e:
            raise SettingsError(f"no sound server: {e}") from e

    def _default(self):
        name = self._pulse.server_info().default_sink_name
        try:
            return self._pulse.get_sink_by_name(name)
        except pulsectl.PulseIndexError as e:
            raise SettingsError("there is no sound output") from e

    def outputs(self) -> list[dict]:
        return [
            {"name": s.name, "description": s.description}
            for s in self._pulse.sink_list()[:MAX_OUTPUTS]
        ]

    def get(self, schema: str, key: str) -> Value | None:
        try:
            sink = self._default()
        except SettingsError:
            return None
        if key == "volume":
            return round(sink.volume.value_flat * 100)
        if key == "muted":
            return bool(sink.mute)
        if key == "output":
            return sink.name
        return None

    def set(self, schema: str, key: str, value: Value) -> None:
        try:
            if key == "output":
                names = {s.name: s for s in self._pulse.sink_list()}
                if value not in names:
                    raise SettingsError(f"no output {value!r}; see 'outputs' in get")
                self._pulse.sink_default_set(names[value])
                return
            sink = self._default()
            if key == "volume":
                self._pulse.volume_set_all_chans(sink, int(value) / 100)
            elif key == "muted":
                self._pulse.mute(sink, bool(value))
            else:
                raise SettingsError(f"unknown sound setting {key}")
        except pulsectl.PulseError as e:
            raise SettingsError(f"the sound server refused: {e}") from e
