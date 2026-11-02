# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings and file locations for Herkos-lite.

The config file is ``$XDG_CONFIG_HOME/herkos/config.toml``::

    # Folders Gloxi may work in. Resources elsewhere are out of bounds
    # (+1 risk level, permissions.md 4.2) and never get a lasting permission.
    allowed_roots = ["~/Documents", "~/Desktop"]

    # More resources than this in one request is a bulk action (+1 level).
    bulk_threshold = 20

    # Seconds to wait for the user before a confirmation counts as "deny".
    confirm_timeout = 120

The highest level Herkos-lite will carry out is R2 (roadmap 2.3); this is not
a setting. Runtime state lives in ``$XDG_STATE_HOME/herkos/`` (journal,
permissions) and the socket in ``$XDG_RUNTIME_DIR/vilna/herkos.sock`` (ADR 0005).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from herkos.risk import Level

# Herkos-lite runs in the user's session and cannot isolate anything, so it
# carries out R0-R2 only; R3-R4 wait for the system Herkos (roadmap 2.3).
LITE_MAX_LEVEL = Level.R2
DEFAULT_ALLOWED_ROOTS = ("~/Documents",)
DEFAULT_BULK_THRESHOLD = 20
DEFAULT_CONFIRM_TIMEOUT = 120.0
# A "session" permission lapses after this much idle time (permissions.md 5.3).
SESSION_IDLE_SECONDS = 30 * 60


class ConfigError(Exception):
    """The config file exists but cannot be used."""


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def runtime_dir() -> Path:
    value = os.environ.get("XDG_RUNTIME_DIR")
    if not value or not os.path.isabs(value):
        raise ConfigError("XDG_RUNTIME_DIR is not set; Herkos needs a private runtime folder")
    return Path(value)


@dataclass(frozen=True)
class Config:
    allowed_roots: tuple[Path, ...] = field(
        default_factory=lambda: tuple(Path(p).expanduser() for p in DEFAULT_ALLOWED_ROOTS)
    )
    bulk_threshold: int = DEFAULT_BULK_THRESHOLD
    confirm_timeout: float = DEFAULT_CONFIRM_TIMEOUT
    max_level: Level = LITE_MAX_LEVEL
    state_dir: Path = field(
        default_factory=lambda: _xdg("XDG_STATE_HOME", ".local/state") / "herkos"
    )

    @property
    def journal_path(self) -> Path:
        return self.state_dir / "journal.jsonl"

    @property
    def backup_dir(self) -> Path:
        """Backups the files tool keeps for undo (retention: permissions.md 11)."""
        return self.state_dir / "backups" / "files"

    @property
    def grants_path(self) -> Path:
        return self.state_dir / "permissions.json"


def default_config_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "herkos" / "config.toml"


def socket_path() -> Path:
    return runtime_dir() / "vilna" / "herkos.sock"


def load_config(path: Path | None = None) -> Config:
    path = path or default_config_path()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return Config()
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None

    unknown = set(data) - {"allowed_roots", "bulk_threshold", "confirm_timeout"}
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")
    kwargs: dict = {}
    if "allowed_roots" in data:
        roots = data["allowed_roots"]
        if not isinstance(roots, list) or not all(isinstance(r, str) and r for r in roots):
            raise ConfigError(f"{path}: allowed_roots must be a list of folders")
        expanded = tuple(Path(r).expanduser() for r in roots)
        if not all(r.is_absolute() for r in expanded):
            raise ConfigError(f"{path}: allowed_roots must be absolute or start with ~")
        kwargs["allowed_roots"] = expanded
    if "bulk_threshold" in data:
        n = data["bulk_threshold"]
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ConfigError(f"{path}: bulk_threshold must be a positive integer")
        kwargs["bulk_threshold"] = n
    if "confirm_timeout" in data:
        t = data["confirm_timeout"]
        if not isinstance(t, int | float) or isinstance(t, bool) or not 1 <= t <= 3600:
            raise ConfigError(f"{path}: confirm_timeout must be 1-3600 seconds")
        kwargs["confirm_timeout"] = float(t)
    return Config(**kwargs)
