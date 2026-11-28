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
# Plain http only to this machine (a rest-server or MinIO for tests).
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_BOOL_FIELDS = ("enabled", "gloxi_history", "system_secrets", "recovery_confirmed")


def bad_path_char(path: str) -> bool:
    """A character restic's one-path-per-line files cannot carry: a line break or NUL."""
    return any(c in "\n\r\x00" for c in path)


# Not a user folder, whatever user-dirs.dirs or an app's settings say.
SYSTEM_DIRS = tuple(
    Path(p)
    for p in (
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/lib",
        "/lib64",
        "/proc",
        "/root",
        "/sbin",
        "/sys",
        "/usr",
        "/var",
    )
)


def user_folder(path: Path, home: Path) -> bool:
    """A folder that can hold the person's files: in the home, or on another disk.

    Never the root, a folder above the home (that would take the whole disk or
    other people's homes) or a system folder.
    """
    if path.is_relative_to(home) and path != home:
        return True  # also when the home itself is under /var (/var/home)
    if path == Path("/") or home.is_relative_to(path):
        return False
    return not any(path == d or path.is_relative_to(d) for d in SYSTEM_DIRS)


def clean_folder(path: str) -> str:
    """normpath, and one leading slash: POSIX keeps ``//x``, which nothing else matches."""
    return "/" + os.path.normpath(path).lstrip("/")


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
        if "://" in loc:
            scheme, rest = loc.split("://", 1)
            netloc, slash, path = rest.partition("/")
            loc = f"{scheme}://{netloc.rpartition('@')[2]}{slash}{path}"
        elif self.kind == "sftp" and "@" in loc:
            # user:password@host:/path: never show what follows the first colon of the login.
            user, _, host = loc.rpartition("@")
            loc = f"{user.split(':', 1)[0]}@{host}"
        names = {"local": "disk", "sftp": "SFTP", "s3": "S3", "rest": "Vilna server"}
        return f"{names[self.kind]}: {loc}"


def parse_destination(text: str) -> Destination:
    """``/media/usb/vilna``, ``sftp:user@host:/srv/vilna``, ``s3:https://...``, ``rest:https://...``."""
    if not isinstance(text, str):
        raise SettingsError("the destination is not text")
    text = text.strip()
    if not text:
        raise SettingsError("the destination is empty")
    if text.startswith("/"):
        return validate_destination("local", text)
    kind, sep, location = text.partition(":")
    if not sep or kind not in DESTINATION_KINDS or kind == "local" or not location:
        raise SettingsError(
            "destination must be an absolute folder or start with sftp:, s3: or rest:"
        )
    return validate_destination(kind, location)


def validate_destination(kind: Any, location: Any) -> Destination:
    """The one check for a place, typed in or read back from a settings file."""
    if kind not in DESTINATION_KINDS:
        raise SettingsError("unknown destination kind")
    if not isinstance(location, str) or not location or bad_path_char(location):
        raise SettingsError("the destination is not a usable address")
    if kind == "local":
        if not location.startswith("/"):
            raise SettingsError("a disk destination is an absolute folder")
        return Destination("local", os.path.normpath(location))
    if kind == "sftp":
        login, _, host = location.rpartition("@")
        if host.startswith("-") or location.startswith("-"):
            raise SettingsError("an sftp address cannot start with '-'")
        if ":" in login:
            raise SettingsError("put the password in the key store, not in the address")
        return Destination(kind, location)
    scheme, sep, rest = location.partition("://")
    netloc = rest.split("/", 1)[0] if sep else ""
    host = netloc.rpartition(":")[0] if netloc.count(":") == 1 else netloc
    if netloc.startswith("[") and "]" in netloc:
        host = netloc[: netloc.index("]") + 1]
    if not sep or not netloc or scheme not in ("https", "http"):
        raise SettingsError(f"{kind} needs an https:// address")
    if scheme == "http" and host.lower() not in LOCAL_HOSTS:
        raise SettingsError(f"{kind} needs an https:// address")
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
        for name in _BOOL_FIELDS:
            if not isinstance(getattr(self, name), bool):
                raise SettingsError(f"{name} must be true or false")
        if self.preset not in PRESETS:
            raise SettingsError(f"preset must be one of {', '.join(PRESETS)}")
        if self.schedule not in SCHEDULES:
            raise SettingsError(f"schedule must be one of {', '.join(SCHEDULES)}")
        for p in (*self.include, *self.exclude):
            if not isinstance(p, str) or not os.path.isabs(p) or bad_path_char(p):
                raise SettingsError(f"not an absolute folder: {p!r}")
        if self.limit_bytes is not None and (
            not isinstance(self.limit_bytes, int)
            or isinstance(self.limit_bytes, bool)
            or self.limit_bytes <= 0
        ):
            raise SettingsError("the limit must be a positive number of bytes")
        if self.destination is not None and not isinstance(self.destination, Destination):
            raise SettingsError("bad destination")
        for key_id in (self.device_key_id, self.recovery_key_id):
            if key_id is not None and not isinstance(key_id, str):
                raise SettingsError("bad key id")
        if (
            not isinstance(self.repo_id, str)
            or not self.repo_id.isascii()
            or not self.repo_id.isalnum()
        ):
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
    except (OSError, ValueError, RecursionError) as e:
        raise SettingsError(f"{path}: {e}") from None
    version = data.get("version") if isinstance(data, dict) else None
    if type(version) is not int or version != FORMAT_VERSION:
        raise SettingsError(f"{path}: unknown format")
    data.pop("version")
    known = set(Settings.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise SettingsError(f"{path}: unknown keys {sorted(unknown)}")
    dest = data.get("destination")
    try:
        if dest is not None:
            data["destination"] = validate_destination(dest["kind"], dest["location"])
        for name in ("include", "exclude"):
            value = data.get(name, ())
            if not isinstance(value, list | tuple):
                raise SettingsError(f"{name} must be a list of folders")
            data[name] = tuple(value)
        settings = Settings(**data)
        settings.validate()
    except (TypeError, KeyError) as e:
        raise SettingsError(f"{path}: {e}") from None
    except SettingsError as e:
        raise SettingsError(f"{path}: {e}") from None
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
