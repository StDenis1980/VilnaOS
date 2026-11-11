# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal client for OpenAI-compatible chat APIs.

Gloxi talks to every model, local or cloud, through this one interface
(ADR 0004). A local llama-server is an endpoint with the key Gloxi gave it (gloxi.localkey).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict

import httpx

from gloxi import localkey
from gloxi.config import ModelEndpoint

Role = Literal["system", "user", "assistant", "tool"]


class Message(TypedDict):
    role: Role
    content: str | None
    # Only in the short-lived exchange of one turn with tools (OpenAI format).
    tool_calls: NotRequired[list[dict[str, Any]]]
    tool_call_id: NotRequired[str]


@dataclass(frozen=True)
class ToolCall:
    """A function call the model asked for; ``arguments`` is raw JSON text."""

    id: str
    name: str
    arguments: str

    def as_message_part(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


# Seconds to wait for llama-server to read a whole prompt (prefill): about
# 2,300 tokens at 16 tokens a second is two and a half minutes.
PREFILL_TIMEOUT = 600.0


class ModelError(Exception):
    """The model endpoint failed or returned something we cannot use."""


class ModelUnavailable(ModelError):
    """The model endpoint cannot be reached (e.g. llama-server is not running)."""


class ChatClient:
    def __init__(
        self, endpoint: ModelEndpoint, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        headers = localkey.headers_for(endpoint.url, endpoint.api_key)
        self.endpoint = endpoint
        self._http = httpx.Client(
            base_url=endpoint.url,
            headers=headers,
            timeout=httpx.Timeout(endpoint.timeout, connect=5.0),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ChatClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _payload(
        self, messages: Sequence[Message], stream: bool, tools: Sequence[dict] | None = None
    ) -> dict:
        payload = {"model": self.endpoint.model, "messages": list(messages), "stream": stream}
        if tools:
            payload["tools"] = list(tools)
        return payload

    def is_available(self) -> bool:
        """True if the endpoint answers ``GET /models``."""
        try:
            return self._http.get("/models").status_code == 200
        except httpx.HTTPError:
            return False

    def complete(self, messages: Sequence[Message]) -> str:
        """Return the whole reply at once."""
        try:
            response = self._http.post("/chat/completions", json=self._payload(messages, False))
        except httpx.TransportError as exc:
            raise ModelUnavailable(self._unavailable_text(exc)) from exc
        _raise_for_status(response)
        try:
            return response.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"unexpected response: {response.text[:200]}") from exc

    def prefill(self, messages: Sequence[Message], tools: Sequence[dict] | None = None) -> None:
        """Have llama-server read ``messages`` and ``tools`` now and keep them cached.

        The next request that starts the same way reuses what was read
        (``cache_prompt``), so the person does not wait for the model to read
        the system prompt and the functions after their first message. One
        token is generated and thrown away. Reading may take minutes on a slow
        processor, so this waits longer than a normal request.
        """
        payload = self._payload(messages, False, tools)
        payload.update(max_tokens=1, cache_prompt=True)
        timeout = httpx.Timeout(max(self.endpoint.timeout, PREFILL_TIMEOUT), connect=5.0)
        try:
            response = self._http.post("/chat/completions", json=payload, timeout=timeout)
        except httpx.TransportError as exc:
            raise ModelUnavailable(self._unavailable_text(exc)) from exc
        _raise_for_status(response)

    def stream(self, messages: Sequence[Message]) -> Iterator[str]:
        """Yield the reply in pieces as the model produces them (SSE)."""
        for event in self.stream_events(messages):
            if isinstance(event, str):
                yield event

    def stream_events(
        self, messages: Sequence[Message], tools: Sequence[dict] | None = None
    ) -> Iterator[str | ToolCall]:
        """Like ``stream``, offering ``tools``: text pieces first, then any tool calls.

        For a local model, llama-server needs a chat template that knows tools
        (``--jinja``, the default in current builds).
        """
        try:
            with self._http.stream(
                "POST", "/chat/completions", json=self._payload(messages, True, tools)
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    _raise_for_status(response)
                yield from _parse_sse(response.iter_lines())
        except httpx.TransportError as exc:
            raise ModelUnavailable(self._unavailable_text(exc)) from exc

    def _unavailable_text(self, exc: Exception) -> str:
        return f"cannot reach the model at {self.endpoint.url}: {exc}"


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    detail = response.text[:200]
    with contextlib.suppress(ValueError, KeyError, TypeError):
        detail = response.json()["error"]["message"]
    raise ModelError(f"model endpoint returned HTTP {response.status_code}: {detail}")


def _parse_sse(lines: Iterator[str]) -> Iterator[str | ToolCall]:
    # Tool calls arrive in fragments keyed by index; hand them out whole at the end.
    calls: dict[int, dict[str, str]] = {}
    for line in lines:
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ModelError(f"bad stream chunk: {data[:200]}") from exc
        if "error" in chunk:
            raise ModelError(str(chunk["error"]))
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:
                yield piece
            for part in delta.get("tool_calls") or []:
                slot = calls.setdefault(part.get("index", 0), {"id": "", "name": "", "args": ""})
                if part.get("id"):
                    slot["id"] = part["id"]
                fn = part.get("function") or {}
                slot["name"] += fn.get("name") or ""
                slot["args"] += fn.get("arguments") or ""
    for index in sorted(calls):
        slot = calls[index]
        yield ToolCall(slot["id"] or f"call_{index}", slot["name"], slot["args"] or "{}")
