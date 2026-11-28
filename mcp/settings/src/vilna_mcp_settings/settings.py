# SPDX-License-Identifier: Apache-2.0
"""The GNOME settings Gloxi may change (ADR 0023, section 2).

Only the keys in ``KEYS`` can be read or written, each with its type and
range checked here: there is no "write any key", so Gloxi cannot turn off
the lock screen or change the proxy through this tool.

Every change returns ``undo_arguments``: the values the keys had before, in
the form ``restore`` takes. Herkos keeps them in the Mitos journal, and
"Undo" in the Permissions window calls ``restore`` with them.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class SettingsError(Exception):
    """Something the caller can fix: a bad value or a key this GNOME lacks."""


Value = bool | int | float | str | list[str]


@dataclass(frozen=True)
class Key:
    schema: str
    key: str
    check: Callable[[Any], bool]
    allowed: str  # for error messages


def _one_of(*values: str) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, str) and v in values


def _boolean(v: Any) -> bool:
    return isinstance(v, bool)


def _number(lo: float, hi: float) -> Callable[[Any], bool]:
    def check(v: Any) -> bool:
        return (
            isinstance(v, int | float) and not isinstance(v, bool) and _finite(v) and lo <= v <= hi
        )

    return check


def _finite(v: int | float) -> bool:
    try:
        return math.isfinite(v)
    except OverflowError:  # a whole number too large for a float
        return False


def _events(v: Any) -> bool:
    return _names(v) and all(e in SOUND_EVENTS for e in v)


def _names(v: Any) -> bool:
    """Distinct, plain names; an undo may hold events a newer Shell wrote."""
    return (
        isinstance(v, list)
        and len(v) <= MAX_NAMES
        and all(isinstance(e, str) and 0 < len(e) <= MAX_NAME and e.isprintable() for e in v)
        and len(v) == len(set(v))
    )


def _short(name: object) -> str:
    text = repr(name)
    return text if len(text) <= MAX_NAME else text[: MAX_NAME - 1] + "…"


MAX_NAMES = 100
MAX_NAME = 100


def _whole(lo: int, hi: int) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


INTERFACE = "org.gnome.desktop.interface"
COLOR = "org.gnome.settings-daemon.plugins.color"
NOTIFICATIONS = "org.gnome.desktop.notifications"
# Not GSettings: volume and output live in PipeWire (sound.py).
SOUND = "sound"
DESKTOP_SOUND = "org.gnome.desktop.sound"
# The Vilna Shell extension keeps the sound theme and the sounds turned off
# one by one, and writes them as the person's sound theme (lib/soundTheme.js).
SHELL = "org.gnome.shell.extensions.vilna"

SOUND_THEMES = ("wood", "bell", "water", "air", "birds", "glass", "star", "freedesktop")
# The events a Vilna sound theme has (sounds/, package vilna-sound-themes).
SOUND_EVENTS = (
    "message-new-instant",
    "bell",
    "dialog-warning",
    "dialog-error",
    "complete",
    "screen-capture",
    "audio-volume-change",
    "device-added",
    "device-removed",
    "power-plug",
    "power-unplug",
    "battery-low",
    "trash-empty",
    "desktop-login",
    "x-vilna-gloxi-listen",
    "x-vilna-gloxi-done",
    "x-vilna-gloxi-error",
    "x-vilna-herkos-consent",
)

COLOR_SCHEMES = ("default", "prefer-dark", "prefer-light")
# GNOME 47+ (org.gnome.desktop.interface accent-color).
ACCENT_COLORS = ("blue", "teal", "green", "yellow", "orange", "red", "pink", "purple", "slate")
# The range GNOME Settings offers for night light, in kelvin.
TEMPERATURE = (1700, 4700)

KEYS: dict[str, Key] = {
    "color_scheme": Key(INTERFACE, "color-scheme", _one_of(*COLOR_SCHEMES), str(COLOR_SCHEMES)),
    "accent_color": Key(INTERFACE, "accent-color", _one_of(*ACCENT_COLORS), str(ACCENT_COLORS)),
    "text_scale": Key(INTERFACE, "text-scaling-factor", _number(0.5, 3.0), "0.5 to 3.0"),
    "night_light": Key(COLOR, "night-light-enabled", _boolean, "true or false"),
    "night_light_automatic": Key(
        COLOR, "night-light-schedule-automatic", _boolean, "true or false"
    ),
    "night_light_from": Key(COLOR, "night-light-schedule-from", _number(0, 24), "0 to 24"),
    "night_light_to": Key(COLOR, "night-light-schedule-to", _number(0, 24), "0 to 24"),
    "night_light_temperature": Key(
        COLOR,
        "night-light-temperature",
        _whole(*TEMPERATURE),
        f"{TEMPERATURE[0]} to {TEMPERATURE[1]}",
    ),
    "show_banners": Key(NOTIFICATIONS, "show-banners", _boolean, "true or false"),
    "volume": Key(SOUND, "volume", _whole(0, 100), "0 to 100 (per cent)"),
    "muted": Key(SOUND, "muted", _boolean, "true or false"),
    "output": Key(
        SOUND, "output", lambda v: isinstance(v, str) and 0 < len(v) <= 256, "an output name"
    ),
    "event_sounds": Key(DESKTOP_SOUND, "event-sounds", _boolean, "true or false"),
    "sound_theme": Key(SHELL, "sound-theme", _one_of(*SOUND_THEMES), str(SOUND_THEMES)),
    "disabled_sounds": Key(
        SHELL, "disabled-sounds", _events, f"a list of distinct events from {SOUND_EVENTS}"
    ),
}
# What an undo may put back that a change itself may not ask for.


class Backend(Protocol):
    def get(self, schema: str, key: str) -> Value | None:
        """The key's value, or None if this system has no such key."""
        ...

    def set(self, schema: str, key: str, value: Value) -> None:
        """Write the key; raise SettingsError if the system refuses it."""
        ...


class MemoryBackend:
    """Settings kept in a dict: for tests and for systems without GSettings."""

    def __init__(self, values: Mapping[tuple[str, str], Value] | None = None) -> None:
        self.values: dict[tuple[str, str], Value] = dict(values or {})

    def get(self, schema: str, key: str) -> Value | None:
        return self.values.get((schema, key))

    def set(self, schema: str, key: str, value: Value) -> None:
        if (schema, key) not in self.values:
            raise SettingsError(f"{schema} {key} is not available here")
        self.values[(schema, key)] = value


class MemorySound(MemoryBackend):
    """Sound kept in memory, with a fixed list of outputs."""

    def __init__(self, volume: int, muted: bool, output: str, outputs: list[dict]) -> None:
        super().__init__(
            {(SOUND, "volume"): volume, (SOUND, "muted"): muted, (SOUND, "output"): output}
        )
        self._outputs = outputs

    def outputs(self) -> list[dict]:
        return list(self._outputs)

    def set(self, schema: str, key: str, value: Value) -> None:
        if key == "output" and value not in {o["name"] for o in self._outputs}:
            raise SettingsError(f"no output {value!r}; see 'outputs' in get")
        super().set(schema, key, value)


class Settings:
    def __init__(self, backend: Backend, sound: Backend | None = None) -> None:
        self.backend = backend
        self.sound = sound
        self._boosted: set[int] = set()  # louder than 100 % volumes an undo may put back

    def _for(self, k: Key) -> Backend | None:
        return self.sound if k.schema == SOUND else self.backend

    # -- reading ----------------------------------------------------------

    def get(self) -> dict:
        """Current values of every known key this system has."""
        values = {}
        for name, k in KEYS.items():
            backend = self._for(k)
            v = backend.get(k.schema, k.key) if backend is not None else None
            if v is not None:
                values[name] = v
        out: dict[str, Any] = {"values": values}
        outputs = getattr(self.sound, "outputs", None)
        if callable(outputs):
            out["outputs"] = outputs()
        return out

    # -- changing ---------------------------------------------------------

    def set_color_scheme(self, scheme: str) -> dict:
        raw = {"light": "default", "dark": "prefer-dark"}.get(scheme)
        if raw is None:
            raise SettingsError("'scheme' must be 'light' or 'dark'")
        return self._apply({"color_scheme": raw})

    def set_accent_color(self, color: str) -> dict:
        return self._apply({"accent_color": color})

    def set_text_scale(self, scale: float) -> dict:
        return self._apply({"text_scale": scale})

    def set_night_light(
        self,
        enabled: bool | None = None,
        automatic: bool | None = None,
        from_hour: float | None = None,
        to_hour: float | None = None,
        temperature: int | None = None,
    ) -> dict:
        changes = {
            "night_light": enabled,
            "night_light_automatic": automatic,
            "night_light_from": from_hour,
            "night_light_to": to_hour,
            "night_light_temperature": temperature,
        }
        changes = {k: v for k, v in changes.items() if v is not None}
        if not changes:
            raise SettingsError("say what to change: enabled, automatic, hours or temperature")
        # Hours given means a fixed schedule, unless the caller said otherwise.
        if ("night_light_from" in changes or "night_light_to" in changes) and automatic is None:
            changes["night_light_automatic"] = False
        return self._apply(changes)

    def set_do_not_disturb(self, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            raise SettingsError("'enabled' must be true or false")
        return self._apply({"show_banners": not enabled})

    def set_volume(self, percent: int) -> dict:
        return self._apply({"volume": percent})

    def set_mute(self, muted: bool) -> dict:
        return self._apply({"muted": muted})

    def set_output(self, output: str) -> dict:
        return self._apply({"output": output})

    def set_event_sounds(self, enabled: bool) -> dict:
        """All event sounds on or off (the sounds of the theme, not the volume)."""
        return self._apply({"event_sounds": enabled})

    def set_sound_theme(self, theme: str) -> dict:
        return self._apply({"sound_theme": theme})

    def set_sound(self, event: str, enabled: bool) -> dict:
        """Turn one event sound on or off, whatever the theme."""
        if event not in SOUND_EVENTS:
            raise SettingsError(f"'event' must be one of {list(SOUND_EVENTS)}")
        if not isinstance(enabled, bool):
            raise SettingsError("'enabled' must be true or false")
        k = KEYS["disabled_sounds"]
        current = self.backend.get(k.schema, k.key)
        if current is None:
            raise SettingsError("disabled_sounds is not available on this system")
        # Keep the order of SOUND_EVENTS; names a newer Shell wrote stay as they were,
        # so that the undo can put back exactly this list.
        off = (set(current) | {event}) if not enabled else (set(current) - {event})
        kept = [e for e in current if isinstance(e, str) and e not in SOUND_EVENTS]
        value = [e for e in SOUND_EVENTS if e in off] + list(dict.fromkeys(kept))
        return self._apply({"disabled_sounds": value}, {"disabled_sounds": self._sounds_check()})

    def _sounds_check(self) -> Callable[[Any], bool]:
        """Known events, and the unknown ones the key already holds."""
        k = KEYS["disabled_sounds"]
        current = self.backend.get(k.schema, k.key)
        held = {e for e in current if isinstance(e, str)} if isinstance(current, list) else set()
        return lambda v: _names(v) and all(e in SOUND_EVENTS or e in held for e in v)

    def restore(self, values: Any) -> dict:
        """Put keys back as they were (the undo of an earlier change)."""
        if not isinstance(values, dict) or not values:
            raise SettingsError("'values' must be a non-empty object")
        boosted = self._boosted
        checks: dict[str, Callable[[Any], bool]] = {
            # PipeWire lets the sound go past 100 % (GNOME's louder sound). An undo may put
            # back exactly the louder volume a change here found, and nothing else.
            "volume": lambda v: KEYS["volume"].check(v) or (type(v) is int and v in boosted)
        }
        if "disabled_sounds" in values:
            checks["disabled_sounds"] = self._sounds_check()
        return self._apply(values, checks)

    def _apply(
        self, changes: Mapping[str, Any], checks: Mapping[str, Callable[[Any], bool]] | None = None
    ) -> dict:
        for name, value in changes.items():
            k = KEYS.get(name) if isinstance(name, str) else None
            if k is None:
                raise SettingsError(f"unknown setting {_short(name)}")
            if not (checks or {}).get(name, k.check)(value):
                raise SettingsError(f"{name} must be {k.allowed}")
        before: dict[str, Value] = {}
        for name in changes:
            k = KEYS[name]
            backend = self._for(k)
            old = backend.get(k.schema, k.key) if backend is not None else None
            if old is None:
                raise SettingsError(f"{name} is not available on this system")
            before[name] = old
        done: list[str] = []
        try:
            for name, value in changes.items():
                k = KEYS[name]
                self._for(k).set(k.schema, k.key, _as_type(before[name], value))
                done.append(name)
        except Exception:
            # All or nothing: put back what was already written.
            for name in reversed(done):
                k = KEYS[name]
                # Best effort: the first error is the one to report.
                with contextlib.suppress(Exception):
                    self._for(k).set(k.schema, k.key, before[name])
            raise
        old_volume = before.get("volume")
        if type(old_volume) is int and old_volume > 100:
            self._boosted.add(old_volume)
        return {
            "changed": dict(changes),
            "before": before,
            "undo_arguments": {"values": before},
        }


def _as_type(old: Value, value: Any) -> Any:
    """A whole number given for a double key is stored as a double."""
    if isinstance(old, float) and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value
