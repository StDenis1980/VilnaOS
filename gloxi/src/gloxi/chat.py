# SPDX-License-Identifier: GPL-3.0-or-later
"""A conversation: system prompt plus the running history."""

from __future__ import annotations

from collections.abc import Iterator

from gloxi.llm import ChatClient, Message

# Small local models have small context windows; keep the tail of the talk.
DEFAULT_MAX_TURNS = 20


class Conversation:
    def __init__(
        self, client: ChatClient, system_prompt: str, *, max_turns: int = DEFAULT_MAX_TURNS
    ) -> None:
        self.client = client
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.history: list[Message] = []

    def reset(self) -> None:
        self.history.clear()

    def messages(self) -> list[Message]:
        # One turn is a user message and the assistant's answer.
        tail = self.history[-2 * self.max_turns :]
        return [{"role": "system", "content": self.system_prompt}, *tail]

    def ask(self, text: str) -> Iterator[str]:
        """Send ``text`` and yield the answer as it streams in.

        The exchange is added to the history only once the answer is
        complete, so a failed or interrupted reply does not poison the
        context.
        """
        pending: Message = {"role": "user", "content": text}
        request = [*self.messages(), pending]
        parts: list[str] = []
        for piece in self.client.stream(request):
            parts.append(piece)
            yield piece
        self.history.append(pending)
        self.history.append({"role": "assistant", "content": "".join(parts)})
