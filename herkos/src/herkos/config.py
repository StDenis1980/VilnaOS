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

    # Local model for Keros (ADR 0010): the llama-server Gloxi uses, on this
    # device only, or "off". Seconds it may spend on one tool result.
    keros_model = "http://127.0.0.1:8080/v1"
    keros_budget = 20

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
DEFAULT_KEROS_MODEL = "http://127.0.0.1:8080/v1"
DEFAULT_KEROS_BUDGET = 20.0
# A "session" permission lapses after this much idle time (permissions.md 5.3).
SESSION_IDLE_SECONDS = 30 * 60


# The system Herkos's settings for every person (ADR 0013, ADR 0025): ``~``
# in them is the home of the person a request is for.
SYSTEM_CONFIG_PATH = Path("/etc/herkos/config.toml")


class ConfigError(Exception):
    """The config file exists but cannot be used."""


def expand_user(path: str, home: Path | None = None) -> Path:
    """``~`` as this process's home, or as ``home`` (the person gloxid serves)."""
    if home is None:
        return Path(path).expanduser()
    if path == "~":
        return home
    if path.startswith("~/"):
        return home / path[2:]
    return Path(path)


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
    keros_model: str | None = DEFAULT_KEROS_MODEL  # None: off
    keros_budget: float = DEFAULT_KEROS_BUDGET
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
    # HERKOS_SOCKET: the system herkosd (/run/herkos/herkos.sock) for gloxid,
    # which has no session of its own (ADR 0025).
    value = os.environ.get("HERKOS_SOCKET")
    if value and os.path.isabs(value):
        return Path(value)
    return runtime_dir() / "vilna" / "herkos.sock"


def load_config(path: Path | None = None, *, home: Path | None = None) -> Config:
    """The settings at ``path``; with ``home``, ``~`` is that person's home, not ours."""
    path = path or default_config_path()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        if home is None:
            return Config()
        return Config(allowed_roots=tuple(expand_user(r, home) for r in DEFAULT_ALLOWED_ROOTS))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None

    known = {"allowed_roots", "bulk_threshold", "confirm_timeout", "keros_model", "keros_budget"}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")
    kwargs: dict = {}
    if "allowed_roots" in data:
        roots = data["allowed_roots"]
        if not isinstance(roots, list) or not all(isinstance(r, str) and r for r in roots):
            raise ConfigError(f"{path}: allowed_roots must be a list of folders")
        expanded = tuple(expand_user(r, home) for r in roots)
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
    if "keros_model" in data:
        m = data["keros_model"]
        if m == "off":
            kwargs["keros_model"] = None
        else:
            from herkos.keros_model import require_loopback

            try:
                kwargs["keros_model"] = require_loopback(m) if isinstance(m, str) else None
            except ValueError as e:
                raise ConfigError(f"{path}: keros_model: {e}") from None
            if kwargs["keros_model"] is None:
                raise ConfigError(f'{path}: keros_model must be a URL or "off"')
    if "keros_budget" in data:
        b = data["keros_budget"]
        if not isinstance(b, int | float) or isinstance(b, bool) or not 1 <= b <= 300:
            raise ConfigError(f"{path}: keros_budget must be 1-300 seconds")
        kwargs["keros_budget"] = float(b)
    return Config(**kwargs)
