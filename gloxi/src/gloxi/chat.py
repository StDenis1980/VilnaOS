# SPDX-License-Identifier: GPL-3.0-or-later
"""A conversation: system prompt plus the running history."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from gloxi.llm import Message, ToolCall
from gloxi.router import Route, Router, Target
from gloxi.tools import FileTools, ToolOutcome

# Small local models have small context windows; keep the tail of the talk.
DEFAULT_MAX_TURNS = 20
# Tool calls the model may make while answering one message.
MAX_TOOL_ROUNDS = 4


class Conversation:
    def __init__(
        self,
        router: Router,
        system_prompt: str,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        tools: FileTools | None = None,
    ) -> None:
        self.router = router
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.tools = tools
        # Called after each tool call, from the thread that runs ask().
        self.on_tool: Callable[[ToolOutcome], None] | None = None
        self.history: list[Message] = []
        self.last_route: Route | None = None
        self.user_turn = 0

    def reset(self) -> None:
        self.history.clear()
        self.user_turn = 0
        self.router.new_conversation()
        if self.tools:
            self.tools.reset()

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
        context. On the local model, the answer may include calls to the
        files tool through Herkos (``gloxi.tools``).
        """
        self.last_route = route
        self.user_turn += 1
        pending: Message = {"role": "user", "content": text}
        request = [*self.messages(), pending]
        tools = self.tools if route.target == "local" and self.tools else None
        if tools and not tools.available():
            tools = None
        if tools:
            request[0] = {"role": "system", "content": self.system_prompt + tools.prompt()}

        parts: list[str] = []
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            # The last round offers no tools, so the model has to answer in words.
            offered = tools.definitions() if tools and round_ < MAX_TOOL_ROUNDS else None
            calls: list[ToolCall] = []
            said: list[str] = []
            for event in self.router.stream_events(request, route, offered):
                if isinstance(event, ToolCall):
                    calls.append(event)
                else:
                    said.append(event)
                    parts.append(event)
                    yield event
            if not calls or tools is None:
                break
            request.append(
                {
                    "role": "assistant",
                    "content": "".join(said) or None,
                    "tool_calls": [c.as_message_part() for c in calls],
                }
            )
            for call in calls:
                outcome = tools.call(call.name, call.arguments, user_turn=self.user_turn)
                if self.on_tool:
                    self.on_tool(outcome)
                request.append(
                    {"role": "tool", "tool_call_id": call.id, "content": outcome.for_model}
                )
        # Tool messages are not kept: file contents stay out of the history,
        # which a later message may send to the cloud.
        self.history.append(pending)
        self.history.append({"role": "assistant", "content": "".join(parts)})
