# SPDX-License-Identifier: Apache-2.0
"""Minimal chat clients for the measurements, on the standard library only.

Each client takes a system prompt and one user message and returns the text of
the answer. Keys come from the environment and are never written to reports.
"""

from __future__ import annotations

import json
import os
import stat
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

Chat = Callable[[str, str], str]

KEY_VARS = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "mistral": ("MISTRAL_API_KEY",),
    "local": (),
}


class ProviderError(RuntimeError):
    pass


# Keys live outside the repository and outside synced folders (OneDrive and the
# like): a file there leaves the computer. See bench/keys.env.example.
KEYS_FILE = Path.home() / ".config" / "vilna-bench" / "keys.env"


def load_keys(path: Path = KEYS_FILE) -> list[str]:
    """Put ``NAME=value`` lines from ``path`` into the environment.

    Only the known key names are read, a variable already set wins, and empty
    values are skipped. Returns the names that were set, never the values.
    A file others can read is refused: chmod 600 it first.
    """
    if not path.exists():
        return []
    if os.name == "posix" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ProviderError(f"{path} can be read by other users: run chmod 600 {path}")
    known = {var for names in KEY_VARS.values() for var in names} | {"LOCAL_API_KEY"}
    loaded = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        name, value = name.removeprefix("export ").strip(), value.strip().strip("\"'")
        if sep and name in known and value and not os.environ.get(name):
            os.environ[name] = value
            loaded.append(name)
    return loaded


@dataclass(frozen=True)
class Target:
    """``provider:model``, e.g. ``anthropic:claude-haiku-4-5`` or ``local:qwen3-4b``."""

    provider: str
    model: str

    @classmethod
    def parse(cls, spec: str) -> Target:
        provider, sep, model = spec.partition(":")
        if not sep or not model or provider not in KEY_VARS:
            known = ", ".join(KEY_VARS)
            raise ValueError(f"expected provider:model with provider one of {known}: {spec!r}")
        return cls(provider, model)

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


def _key(provider: str) -> str:
    for var in KEY_VARS[provider]:
        if os.environ.get(var):
            return os.environ[var]
    names = " or ".join(KEY_VARS[provider])
    raise ProviderError(f"set {names} for {provider}")


def _post(url: str, headers: dict[str, str], body: dict, timeout: float = 120) -> dict:
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", **headers}
    )
    last: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 529):
                detail = e.read(500).decode(errors="replace")
                raise ProviderError(f"HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        time.sleep(2 * 2**attempt)
    raise ProviderError(f"gave up after retries: {last}")


def _openai_style(base_url: str, key: str | None, model: str) -> Chat:
    def chat(system: str, user: str) -> str:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        reply = _post(f"{base_url.rstrip('/')}/chat/completions", headers, body)
        try:
            return reply["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"unexpected reply shape: {str(reply)[:300]}") from e

    return chat


def _anthropic(key: str, model: str) -> Chat:
    def chat(system: str, user: str) -> str:
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        body = {
            "model": model,
            "max_tokens": 2048,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        reply = _post("https://api.anthropic.com/v1/messages", headers, body)
        try:
            return "".join(b.get("text", "") for b in reply["content"] if b.get("type") == "text")
        except (KeyError, TypeError) as e:
            raise ProviderError(f"unexpected reply shape: {str(reply)[:300]}") from e

    return chat


def _google(key: str, model: str) -> Chat:
    def chat(system: str, user: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
        }
        reply = _post(url, {"x-goog-api-key": key}, body)
        try:
            parts = reply["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"unexpected reply shape: {str(reply)[:300]}") from e

    return chat


def client(target: Target, local_url: str = "http://127.0.0.1:8080/v1") -> Chat:
    """A chat function for ``target``. ``local`` is any OpenAI-compatible server."""
    if target.provider == "openai":
        return _openai_style("https://api.openai.com/v1", _key("openai"), target.model)
    if target.provider == "mistral":
        return _openai_style("https://api.mistral.ai/v1", _key("mistral"), target.model)
    if target.provider == "anthropic":
        return _anthropic(_key("anthropic"), target.model)
    if target.provider == "google":
        return _google(_key("google"), target.model)
    return _openai_style(local_url, os.environ.get("LOCAL_API_KEY"), target.model)
