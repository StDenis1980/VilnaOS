# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings for Gloxi.

Precedence, lowest to highest: built-in defaults, the TOML config file,
environment variables. The config file lives at
``$XDG_CONFIG_HOME/gloxi/config.toml`` (``~/.config/gloxi/config.toml``)::

    [local]
    url = "http://127.0.0.1:8080/v1"
    model = "local"
    timeout = 120
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LOCAL_URL = "http://127.0.0.1:8080/v1"

DEFAULT_SYSTEM_PROMPT = (
    "You are Gloxi, the assistant of Vilna OS. Answer briefly and clearly, "
    "in the language the user writes in. If you are not sure, say so."
)


class ConfigError(Exception):
    """The config file exists but cannot be used."""


@dataclass(frozen=True)
class ModelEndpoint:
    """An OpenAI-compatible chat endpoint."""

    url: str
    model: str
    timeout: float = 120.0
    # Only cloud endpoints need a key; a local llama-server needs none.
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Settings:
    local: ModelEndpoint
    system_prompt: str = DEFAULT_SYSTEM_PROMPT


def config_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "gloxi" / "config.toml"


def load_settings(path: Path | None = None, env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ) if env is None else env
    path = config_path(env) if path is None else path

    data: dict = {}
    if path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc

    local = data.get("local", {})
    if not isinstance(local, dict):
        raise ConfigError(f"{path}: [local] must be a table")

    url = env.get("GLOXI_LOCAL_URL") or local.get("url") or DEFAULT_LOCAL_URL
    model = env.get("GLOXI_LOCAL_MODEL") or local.get("model") or "local"
    timeout_raw = env.get("GLOXI_LOCAL_TIMEOUT") or local.get("timeout") or 120
    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"timeout must be a number, got {timeout_raw!r}") from exc

    system_prompt = data.get("system_prompt") or DEFAULT_SYSTEM_PROMPT

    return Settings(
        local=ModelEndpoint(url=str(url).rstrip("/"), model=str(model), timeout=timeout),
        system_prompt=str(system_prompt),
    )
