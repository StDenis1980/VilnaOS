# SPDX-License-Identifier: GPL-3.0-or-later
"""What the person chose for backups (design.md, section 2a), and where it lives.

``$XDG_CONFIG_HOME/vilna-backup/settings.json`` holds choices only, never a
secret: the repository password and storage credentials are in the key store
(``keys``). The file itself goes into every backup, so a new device knows
what was backed up and where.

Run state (progress, the run log, the lock) lives in
``$XDG_STATE_HOME/vilna-backup/``.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1

# Step 2 of the first-run dialog (design.md 2a).
PRESETS = ("settings", "documents", "home", "custom")
DEFAULT_PRESET = "documents"
# Step 6: how often.
SCHEDULES = ("hourly", "daily", "weekly", "off")
DEFAULT_SCHEDULE = "daily"
# Step 5: where. "rest" is a restic rest-server (ours in the EU, append-only).
DESTINATION_KINDS = ("local", "sftp", "s3", "rest")


class SettingsError(Exception):
    """The settings file exists but cannot be used, or a value is wrong."""


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "vilna-backup"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "vilna-backup"


@dataclass(frozen=True)
class Destination:
    kind: str
    # What restic calls the repository: a folder, sftp:host:/path,
    # s3:https://host/bucket/path or rest:https://host/user/.
    # Never carries a password; credentials are in the key store.
    location: str

    def repository(self) -> str:
        if self.kind == "local":
            return self.location
        return f"{self.kind}:{self.location}"

    def describe(self) -> str:
        """For people: the place without anything that looks like a credential."""
        loc = self.location
        if "@" in loc and "://" in loc:
            scheme, rest = loc.split("://", 1)
            loc = f"{scheme}://{rest.split('@', 1)[1]}"
        names = {"local": "disk", "sftp": "SFTP", "s3": "S3", "rest": "Vilna server"}
        return f"{names[self.kind]}: {loc}"


def parse_destination(text: str) -> Destination:
    """``/media/usb/vilna``, ``sftp:user@host:/srv/vilna``, ``s3:https://...``, ``rest:https://...``."""
    text = text.strip()
    if not text:
        raise SettingsError("the destination is empty")
    if text.startswith("/"):
        return Destination("local", os.path.normpath(text))
    kind, sep, location = text.partition(":")
    if not sep or kind not in DESTINATION_KINDS or kind == "local" or not location:
        raise SettingsError(
            "destination must be an absolute folder or start with sftp:, s3: or rest:"
        )
    local_http = location.startswith(("http://127.0.0.1", "http://localhost"))
    if kind in ("s3", "rest") and not location.startswith("https://") and not local_http:
        raise SettingsError(f"{kind} needs an https:// address")
    if kind in ("s3", "rest"):
        netloc = location.split("://", 1)[1].split("/", 1)[0]
        if "@" in netloc:
            raise SettingsError("put the login in the key store, not in the address")
    return Destination(kind, location)


@dataclass(frozen=True)
class Settings:
    # Identifies this repository's entries in the key store.
    repo_id: str = field(default_factory=lambda: secrets.token_hex(8))
    destination: Destination | None = None
    enabled: bool = False
    preset: str = DEFAULT_PRESET
    # Absolute folders added to the preset ("add Music to backups") ...
    include: tuple[str, ...] = ()
    # ... and folders left out (too large for the limit, step 4).
    exclude: tuple[str, ...] = ()
    # Step 3: sensitive parts, off unless the person says yes.
    gloxi_history: bool = False
    system_secrets: bool = False
    # Step 4: how much space the person is ready to give; None: no limit.
    limit_bytes: int | None = None
    schedule: str = DEFAULT_SCHEDULE
    # Step 7: the person confirmed the recovery sheet is saved. Backups do
    # not run before that (design.md 3a.2).
    recovery_confirmed: bool = False
    # restic key ids, so a device can later be blocked or a key replaced.
    device_key_id: str | None = None
    recovery_key_id: str | None = None

    def with_changes(self, **changes: Any) -> Settings:
        new = replace(self, **changes)
        new.validate()
        return new

    def validate(self) -> None:
        if self.preset not in PRESETS:
            raise SettingsError(f"preset must be one of {', '.join(PRESETS)}")
        if self.schedule not in SCHEDULES:
            raise SettingsError(f"schedule must be one of {', '.join(SCHEDULES)}")
        for p in (*self.include, *self.exclude):
            if not isinstance(p, str) or not os.path.isabs(p) or "\x00" in p:
                raise SettingsError(f"not an absolute folder: {p!r}")
        if self.limit_bytes is not None and (
            not isinstance(self.limit_bytes, int) or self.limit_bytes <= 0
        ):
            raise SettingsError("the limit must be a positive number of bytes")
        if not isinstance(self.repo_id, str) or not self.repo_id.isalnum():
            raise SettingsError("bad repo_id")

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["include"] = list(self.include)
        data["exclude"] = list(self.exclude)
        return {"version": FORMAT_VERSION, **data}


def settings_path() -> Path:
    return config_dir() / "settings.json"


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError) as e:
        raise SettingsError(f"{path}: {e}") from None
    if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
        raise SettingsError(f"{path}: unknown format")
    data.pop("version")
    known = set(Settings.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise SettingsError(f"{path}: unknown keys {sorted(unknown)}")
    dest = data.get("destination")
    try:
        if dest is not None:
            data["destination"] = Destination(kind=dest["kind"], location=dest["location"])
            if data["destination"].kind not in DESTINATION_KINDS:
                raise SettingsError(f"{path}: unknown destination kind")
        data["include"] = tuple(data.get("include", ()))
        data["exclude"] = tuple(data.get("exclude", ()))
        settings = Settings(**data)
    except (TypeError, KeyError) as e:
        raise SettingsError(f"{path}: {e}") from None
    settings.validate()
    return settings


def save_settings(settings: Settings, path: Path | None = None) -> None:
    settings.validate()
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings.to_json(), f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
