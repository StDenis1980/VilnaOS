# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings for Gloxi.

Precedence, lowest to highest: built-in defaults, the TOML config file,
environment variables. The config file lives at
``$XDG_CONFIG_HOME/gloxi/config.toml`` (``~/.config/gloxi/config.toml``)::

    [local]
    url = "http://127.0.0.1:8080/v1"
    model = "local"
    timeout = 120
    start_server = true          # start the bundled llama-server if nothing answers at url

    [cloud]                      # optional: the user's own provider and key
    url = "https://api.mistral.ai/v1"
    model = "mistral-small-latest"
    # api_key = "..."            # better: GLOXI_CLOUD_API_KEY in the environment

    [router]
    mode = "auto"                # auto | local | cloud
    cloud_allowed = false        # the cloud is off until the user turns it on
    long_message_chars = 1500    # auto: longer messages go to the cloud
    mask_cloud = true            # privacy gateway: mask personal data sent to the cloud
    mask_strict = false          # also hide bare numbers that nothing ties to a person

    [tools]
    enabled = true               # files tool through Herkos, when Herkos is running
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LOCAL_URL = "http://127.0.0.1:8080/v1"

# No word about Ukrainian here: with one, Ministral 3 8B answered Spanish,
# Polish, German and English messages in Ukrainian (gloxi-bench 37/69, 51/69
# without it). A Ukrainian message gets its own reminder (chat.UKRAINIAN_HINT).
DEFAULT_SYSTEM_PROMPT = (
    "You are Gloxi, the assistant of Vilna OS. Answer briefly and clearly, "
    "in the language of the user's latest message. "
    "If you are not sure, say so."
)


class ConfigError(Exception):
    """The config file exists but cannot be used."""


@dataclass(frozen=True)
class ModelEndpoint:
    """An OpenAI-compatible chat endpoint."""

    url: str
    model: str
    timeout: float = 120.0
    # The cloud key. A local server gets Gloxi's own key (gloxi.localkey).
    api_key: str | None = field(default=None, repr=False)


ROUTER_MODES = ("auto", "local", "cloud")


@dataclass(frozen=True)
class RouterPolicy:
    mode: str = "auto"
    cloud_allowed: bool = False
    long_message_chars: int = 1500
    mask_cloud: bool = True
    mask_strict: bool = False


@dataclass(frozen=True)
class Settings:
    local: ModelEndpoint
    cloud: ModelEndpoint | None = None
    router: RouterPolicy = field(default_factory=RouterPolicy)
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    tools_enabled: bool = True
    # Start the bundled llama-server (engine.py) when nothing answers at local.url.
    start_server: bool = True


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

    local = _table(data, "local", path)
    cloud = _table(data, "cloud", path)
    router = _table(data, "router", path)
    tools = _table(data, "tools", path)

    local_endpoint = ModelEndpoint(
        url=_get(env, "GLOXI_LOCAL_URL", local, "url", DEFAULT_LOCAL_URL).rstrip("/"),
        model=_get(env, "GLOXI_LOCAL_MODEL", local, "model", "local"),
        timeout=_number(_get(env, "GLOXI_LOCAL_TIMEOUT", local, "timeout", 120), "timeout"),
        api_key=_key_file(env.get("GLOXI_LOCAL_KEY_FILE", "")),
    )

    cloud_endpoint = None
    cloud_url = _get(env, "GLOXI_CLOUD_URL", cloud, "url", "")
    if cloud_url:
        cloud_endpoint = ModelEndpoint(
            url=cloud_url.rstrip("/"),
            model=_get(env, "GLOXI_CLOUD_MODEL", cloud, "model", ""),
            timeout=_number(_get(env, "GLOXI_CLOUD_TIMEOUT", cloud, "timeout", 120), "timeout"),
            api_key=_get(env, "GLOXI_CLOUD_API_KEY", cloud, "api_key", "") or None,
        )
        if not cloud_endpoint.model:
            raise ConfigError("[cloud] needs a model name")

    mode = _get(env, "GLOXI_ROUTER_MODE", router, "mode", "auto")
    if mode not in ROUTER_MODES:
        raise ConfigError(f"router mode must be one of {', '.join(ROUTER_MODES)}, got {mode!r}")
    policy = RouterPolicy(
        mode=mode,
        cloud_allowed=_flag(_get(env, "GLOXI_CLOUD_ALLOWED", router, "cloud_allowed", False)),
        long_message_chars=int(
            _number(_get(env, "", router, "long_message_chars", 1500), "long_message_chars")
        ),
        mask_cloud=_flag(_get(env, "GLOXI_MASK_CLOUD", router, "mask_cloud", True)),
        mask_strict=_flag(_get(env, "GLOXI_MASK_STRICT", router, "mask_strict", False)),
    )
    if policy.cloud_allowed and cloud_endpoint is None:
        raise ConfigError("cloud_allowed is on, but no [cloud] url is configured")

    system_prompt = data.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
    return Settings(
        local=local_endpoint,
        cloud=cloud_endpoint,
        router=policy,
        system_prompt=str(system_prompt),
        tools_enabled=_flag(_get(env, "GLOXI_TOOLS", tools, "enabled", True)),
        start_server=_flag(_get(env, "GLOXI_START_SERVER", local, "start_server", True)),
    )


def _key_file(name: str) -> str | None:
    """The key of a model server Gloxi did not start (vilna-llm.service, ADR 0025)."""
    if not name:
        return None
    try:
        key = Path(name).read_text(encoding="ascii").strip()
    except FileNotFoundError:
        # The shared model has not started yet (no weights): Gloxi still
        # starts and says the model is not running when asked.
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read the local model key {name}: {exc}") from exc
    if not key or len(key) > 512 or any(c.isspace() for c in key):
        raise ConfigError(f"{name} does not hold a key")
    return key


def _table(data: dict, name: str, path: Path) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: [{name}] must be a table")
    return value


def _get(env: dict[str, str], env_name: str, table: dict, key: str, default):
    if env_name and env.get(env_name):
        return env[env_name]
    value = table.get(key)
    return default if value is None or value == "" else value


def _number(raw, name: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _flag(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    raise ConfigError(f"expected true or false, got {raw!r}")
