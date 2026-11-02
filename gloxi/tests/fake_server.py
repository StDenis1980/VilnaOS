# SPDX-License-Identifier: GPL-3.0-or-later
"""An in-process stand-in for llama-server's OpenAI-compatible API."""

from __future__ import annotations

import json

import httpx


class FakeLlamaServer:
    def __init__(self, reply: str = "Привіт! Я Gloxi.", *, status: int = 200) -> None:
        self.reply = reply
        self.status = status
        self.requests: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "local"}]})
        body = json.loads(request.content)
        self.requests.append(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "model not loaded"}})
        if not body.get("stream"):
            return httpx.Response(
                200, json={"choices": [{"message": {"role": "assistant", "content": self.reply}}]}
            )
        # Split into a few chunks like a real token stream, plus llama-server's
        # empty role chunk at the start and [DONE] at the end.
        pieces = [self.reply[i : i + 4] for i in range(0, len(self.reply), 4)]
        events = [{"choices": [{"delta": {"role": "assistant"}}]}]
        events += [{"choices": [{"delta": {"content": p}}]} for p in pieces]
        text = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)
        text += "data: [DONE]\n\n"
        return httpx.Response(
            200, content=text.encode(), headers={"content-type": "text/event-stream"}
        )
