# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal client for OpenAI-compatible chat APIs.

Gloxi talks to every model, local or cloud, through this one interface
(ADR 0004). A local llama-server is just an endpoint without an API key.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator, Sequence
from typing import Literal, TypedDict

import httpx

from gloxi.config import ModelEndpoint

Role = Literal["system", "user", "assistant"]


class Message(TypedDict):
    role: Role
    content: str


class ModelError(Exception):
    """The model endpoint failed or returned something we cannot use."""


class ModelUnavailable(ModelError):
    """The model endpoint cannot be reached (e.g. llama-server is not running)."""


class ChatClient:
    def __init__(
        self, endpoint: ModelEndpoint, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        headers = {}
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
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

    def _payload(self, messages: Sequence[Message], stream: bool) -> dict:
        return {"model": self.endpoint.model, "messages": list(messages), "stream": stream}

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

    def stream(self, messages: Sequence[Message]) -> Iterator[str]:
        """Yield the reply in pieces as the model produces them (SSE)."""
        try:
            with self._http.stream(
                "POST", "/chat/completions", json=self._payload(messages, True)
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


def _parse_sse(lines: Iterator[str]) -> Iterator[str]:
    for line in lines:
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ModelError(f"bad stream chunk: {data[:200]}") from exc
        if "error" in chunk:
            raise ModelError(str(chunk["error"]))
        for choice in chunk.get("choices", []):
            piece = (choice.get("delta") or {}).get("content")
            if piece:
                yield piece
