# SPDX-License-Identifier: GPL-3.0-or-later
"""A conversation: system prompt plus the running history."""

from __future__ import annotations

from collections.abc import Iterator

from gloxi.llm import Message
from gloxi.router import Route, Router, Target

# Small local models have small context windows; keep the tail of the talk.
DEFAULT_MAX_TURNS = 20


class Conversation:
    def __init__(
        self, router: Router, system_prompt: str, *, max_turns: int = DEFAULT_MAX_TURNS
    ) -> None:
        self.router = router
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.history: list[Message] = []
        self.last_route: Route | None = None

    def reset(self) -> None:
        self.history.clear()

    def messages(self) -> list[Message]:
        # One turn is a user message and the assistant's answer.
        tail = self.history[-2 * self.max_turns :]
        return [{"role": "system", "content": self.system_prompt}, *tail]

    def route(self, text: str, *, force: Target | None = None) -> Route:
        """Decide where ``text`` would go, without sending it."""
        return self.router.decide(text, force=force)

    def ask(self, text: str, route: Route) -> Iterator[str]:
        """Send ``text`` along ``route`` and yield the answer as it streams in.

        The exchange is added to the history only once the answer is
        complete, so a failed or interrupted reply does not poison the
        context.
        """
        self.last_route = route
        pending: Message = {"role": "user", "content": text}
        request = [*self.messages(), pending]
        parts: list[str] = []
        for piece in self.router.stream(request, route):
            parts.append(piece)
            yield piece
        self.history.append(pending)
        self.history.append({"role": "assistant", "content": "".join(parts)})
