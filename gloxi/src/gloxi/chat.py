# SPDX-License-Identifier: GPL-3.0-or-later
"""A conversation: system prompt plus the running history."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator

from gloxi.llm import Message, ModelError, ToolCall
from gloxi.router import Route, Router, Target
from gloxi.tools import FileTools, ToolOutcome

# Small local models have small context windows; keep the tail of the talk.
DEFAULT_MAX_TURNS = 20
# Tool calls the model may make while answering one message.
MAX_TOOL_ROUNDS = 4

# Letters Russian has and Ukrainian does not.
_RUSSIAN_ONLY = frozenset("ыэъёЫЭЪЁ")
UKRAINIAN_HINT = "(Answer in Ukrainian, not in Russian.)"


def is_ukrainian(text: str) -> bool:
    """True when ``text`` is written in Cyrillic and not in Russian.

    Of Gloxi's languages (ADR 0009) only Ukrainian is written in Cyrillic, so
    a message mostly in Cyrillic without ы, э, ъ or ё is taken as Ukrainian,
    even a short one with no і, ї, є or ґ ("Покажи файли в Документах").
    """
    letters = [c for c in text if c.isalpha()]
    cyrillic = sum(1 for c in letters if "\u0400" <= c <= "\u04ff")
    if not letters or 2 * cyrillic <= len(letters):
        return False
    return not any(c in _RUSSIAN_ONLY for c in text)


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
        # The cloud gets the history only from this index on: after a switch
        # to the cloud mid-conversation the person decides (brief 10.3).
        self.cloud_from = 0
        self.sent_to_cloud = False
        # A message waits for warm() to finish, then reuses what it had the model read.
        self._model_lock = threading.Lock()

    def reset(self) -> None:
        self.history.clear()
        self.user_turn = 0
        self.cloud_from = 0
        self.sent_to_cloud = False
        self.router.new_conversation()
        if self.tools:
            self.tools.reset()

    def messages(self, target: str = "local") -> list[Message]:
        # One turn is a user message and the assistant's answer.
        history = self.history[self.cloud_from :] if target == "cloud" else self.history
        tail = history[-2 * self.max_turns :]
        return [{"role": "system", "content": self.system_prompt}, *tail]

    def hold_history_from_cloud(self) -> int:
        """Keep what was said so far on this computer; returns how many messages."""
        self.cloud_from = len(self.history)
        return self.cloud_from

    def share_history_with_cloud(self) -> None:
        self.cloud_from = 0

    def warm(self) -> bool:
        """Have the local model read the system prompt and the functions in advance.

        On a slow processor the local model reads about 16 tokens a second,
        and the functions alone are well over a thousand (Build14.6): read
        once before the first message, they are cached by llama-server and the
        first answer starts at once (``ChatClient.prefill``). The request
        starts exactly as ``ask`` will start its own. Best effort: False when
        the model was not reached; the first message then waits as before.
        """
        if self.history or self.router.mode == "cloud":
            return False
        prefill = getattr(self.router.local, "prefill", None)
        if prefill is None:
            return False
        with self._model_lock:
            tools = self.tools if self.tools and self.tools.available() else None
            system = self.system_prompt + (tools.prompt() if tools else "")
            try:
                prefill(
                    [{"role": "system", "content": system}], tools.definitions() if tools else None
                )
            except ModelError:
                return False
        return True

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
        with self._model_lock:
            yield from self._ask(text, route)

    def _ask(self, text: str, route: Route) -> Iterator[str]:
        self.last_route = route
        self.user_turn += 1
        pending: Message = {"role": "user", "content": text}
        request = [*self.messages(route.target), pending]
        if route.target == "cloud":
            self.sent_to_cloud = True
        if route.target == "local" and is_ukrainian(text):
            # Small local models drift from Ukrainian into Russian, above all
            # after a function result in English (Build14.6). The reminder goes
            # only into this request: at the end, so the cached start stays,
            # and not into the history.
            request[-1] = {"role": "user", "content": f"{text}\n\n{UKRAINIAN_HINT}"}
        tools = self.tools if route.target == "local" and self.tools else None
        if tools and not tools.available():
            tools = None
        if tools:
            request[0] = {"role": "system", "content": self.system_prompt + tools.prompt()}

        parts: list[str] = []
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            # The last round offers no tools, so the model has to answer in words.
            offered = tools.definitions(text) if tools and round_ < MAX_TOOL_ROUNDS else None
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
