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
    slot = 0                     # llama-server slot that keeps the conversation cached; -1: any
    repeat_penalty = 1.1         # llama-server: penalise repeated words; 1: off

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

import math
import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

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


def _slot(value: object) -> int | None:
    """A llama-server slot number; a negative one lets the server choose."""
    try:
        slot = int(str(value))
    except ValueError as exc:  # also a number with 5000 digits
        raise ConfigError(f"slot must be a whole number, got {value!r}") from exc
    return slot if slot >= 0 else None


def _penalty(value: object) -> float | None:
    """A llama-server repeat penalty: 1 or less sends none (the server's default, off)."""
    penalty = _number(value, "repeat_penalty")
    if penalty != penalty or penalty > 2:  # NaN, or so high that answers stop making sense
        raise ConfigError(f"repeat_penalty must be between 1 and 2, got {value!r}")
    return penalty if penalty > 1 else None


@dataclass(frozen=True)
class ModelEndpoint:
    """An OpenAI-compatible chat endpoint."""

    url: str
    model: str
    timeout: float = 120.0
    # The cloud key. A local server gets Gloxi's own key (gloxi.localkey).
    api_key: str | None = field(default=None, repr=False)
    # llama-server only: the slot whose cache keeps this conversation
    # (``id_slot``). Left to itself, llama-server often gave a turn an idle
    # slot without it and read the whole prompt again: up to 61 s to choose an
    # action (Build14.23). Keros asks in the last slot (herkosd keros_model).
    slot: int | None = None
    # llama-server only (``repeat_penalty``). Its default is 1, off, and on
    # long answers the 4B model looped, repeating one sentence (Build15.6).
    repeat_penalty: float | None = None


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
        except (OSError, ValueError, RecursionError) as exc:  # also not UTF-8, too deep
            raise ConfigError(f"cannot read {path}: {exc}") from exc

    local = _table(data, "local", path)
    cloud = _table(data, "cloud", path)
    router = _table(data, "router", path)
    tools = _table(data, "tools", path)

    local_endpoint = ModelEndpoint(
        url=_url(_get(env, "GLOXI_LOCAL_URL", local, "url", DEFAULT_LOCAL_URL), "[local] url"),
        model=_text(_get(env, "GLOXI_LOCAL_MODEL", local, "model", "local"), "[local] model"),
        timeout=_seconds(_get(env, "GLOXI_LOCAL_TIMEOUT", local, "timeout", 120)),
        api_key=_key_file(env.get("GLOXI_LOCAL_KEY_FILE", "")),
        slot=_slot(_get(env, "GLOXI_LOCAL_SLOT", local, "slot", 0)),
        repeat_penalty=_penalty(
            _get(env, "GLOXI_LOCAL_REPEAT_PENALTY", local, "repeat_penalty", 1.1)
        ),
    )

    cloud_endpoint = None
    cloud_url = _get(env, "GLOXI_CLOUD_URL", cloud, "url", "")
    if cloud_url:
        cloud_endpoint = ModelEndpoint(
            url=_url(cloud_url, "[cloud] url"),
            model=_text(_get(env, "GLOXI_CLOUD_MODEL", cloud, "model", ""), "[cloud] model"),
            timeout=_seconds(_get(env, "GLOXI_CLOUD_TIMEOUT", cloud, "timeout", 120)),
            api_key=_text(_get(env, "GLOXI_CLOUD_API_KEY", cloud, "api_key", ""), "api_key")
            or None,
        )
        if not cloud_endpoint.model:
            raise ConfigError("[cloud] needs a model name")

    mode = _get(env, "GLOXI_ROUTER_MODE", router, "mode", "auto")
    if not isinstance(mode, str) or mode not in ROUTER_MODES:
        raise ConfigError(f"router mode must be one of {', '.join(ROUTER_MODES)}, got {mode!r}")
    policy = RouterPolicy(
        mode=mode,
        cloud_allowed=_flag(_get(env, "GLOXI_CLOUD_ALLOWED", router, "cloud_allowed", False)),
        long_message_chars=_characters(_get(env, "", router, "long_message_chars", 1500)),
        mask_cloud=_flag(_get(env, "GLOXI_MASK_CLOUD", router, "mask_cloud", True)),
        mask_strict=_flag(_get(env, "GLOXI_MASK_STRICT", router, "mask_strict", False)),
    )
    if policy.cloud_allowed and cloud_endpoint is None:
        raise ConfigError("cloud_allowed is on, but no [cloud] url is configured")

    system_prompt = _text(data.get("system_prompt") or DEFAULT_SYSTEM_PROMPT, "system_prompt")
    return Settings(
        local=local_endpoint,
        cloud=cloud_endpoint,
        router=policy,
        system_prompt=system_prompt,
        tools_enabled=_flag(_get(env, "GLOXI_TOOLS", tools, "enabled", True)),
        start_server=_flag(_get(env, "GLOXI_START_SERVER", local, "start_server", True)),
    )


def _key_file(name: str) -> str | None:
    """The key of a model server Gloxi did not start (vilna-llm.service, ADR 0025)."""
    if not name:
        return None
    try:
        # A pipe would never answer, a huge file would fill the memory: look first.
        info = os.stat(name)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"cannot read the local model key {name}: not a file")
        if info.st_size > MAX_KEY_FILE:
            raise ConfigError(f"{name} does not hold a key")
        key = Path(name).read_text(encoding="ascii").strip()
    except FileNotFoundError:
        # The shared model has not started yet (no weights): Gloxi still
        # starts and says the model is not running when asked.
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read the local model key {name}: {exc}") from exc
    if not key or len(key) > MAX_KEY or not all(c.isprintable() and not c.isspace() for c in key):
        raise ConfigError(f"{name} does not hold a key")
    return key


MAX_KEY = 512
MAX_KEY_FILE = 4096
MAX_TIMEOUT = 3600.0  # seconds; longer is no answer at all
MAX_CHARS = 10_000_000


def _text(raw, name: str) -> str:
    if not isinstance(raw, str):
        raise ConfigError(f"{name} must be text, got {raw!r}")
    return raw


def _url(raw, name: str) -> str:
    return _text(raw, name).rstrip("/")


def address_problem(url: str, *, cloud: bool = False) -> str | None:
    """Why a client cannot use ``url``, or None. The cloud only over https."""
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises for a port that is not a number or out of range
    except ValueError as exc:  # [::1 , :abc, 65536
        return f"not an address: {url!r} ({exc})"
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host or not host.isascii():
        return f"must be an http:// or https:// address, got {url!r}"
    if parts.port == 0:
        return f"port 0 is not a port to listen on: {url!r}"
    if cloud and parts.scheme != "https" and host not in ("127.0.0.1", "localhost", "::1"):
        # The key and every message would cross the internet unencrypted.
        return f"must start with https://, got {url!r}"
    return None


def check_addresses(settings: Settings) -> Settings:
    """``settings`` if a client can open its addresses, else a ConfigError (not a crash)."""
    endpoints = (("[local]", settings.local, False), ("[cloud]", settings.cloud, True))
    for name, endpoint, cloud in endpoints:
        problem = address_problem(endpoint.url, cloud=cloud) if endpoint else None
        if problem:
            raise ConfigError(f"{name} url {problem}")
    return settings


def _seconds(raw) -> float:
    if isinstance(raw, bool):
        raise ConfigError(f"timeout must be a number of seconds, got {raw!r}")
    value = _number(raw, "timeout")
    if not 0 < value <= MAX_TIMEOUT:  # also NaN
        raise ConfigError(f"timeout must be between 0 and {MAX_TIMEOUT:.0f} seconds, got {raw!r}")
    return value


def _characters(raw) -> int:
    value = _number(raw, "long_message_chars") if not isinstance(raw, bool) else math.nan
    if not 1 <= value <= MAX_CHARS:  # -1 or 0 would send every message to the cloud
        raise ConfigError(f"long_message_chars must be between 1 and {MAX_CHARS}, got {raw!r}")
    return int(value)


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
