# SPDX-License-Identifier: GPL-3.0-or-later
"""A conversation: system prompt plus the running history."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from datetime import date

from gloxi import mail_flow
from gloxi.llm import Message, ModelError, ToolCall
from gloxi.router import Route, Router, Target
from gloxi.tools import FileTools, ToolOutcome

# Small local models have small context windows; keep the tail of the talk.
DEFAULT_MAX_TURNS = 20
# Tool calls the model may make while answering one message. Milestone В3's
# "read the email, add the event, prepare the answer" takes four (find, read,
# add, save the draft), so four left no room for a model that first looks at
# the accounts or the folders. Each round is one more request to the model.
MAX_TOOL_ROUNDS = 6
# Calls run from one answer of the model; a looping model may send hundreds,
# and each could open a window for the person.
MAX_CALLS_PER_ANSWER = 8

# Letters Russian has and Ukrainian does not.
_RUSSIAN_ONLY = frozenset("ыэъёЫЭЪЁ")
UKRAINIAN_HINT = "(Answer in Ukrainian, not in Russian.)"
# English names whatever the locale: the prompt to the model is in English.
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def today_line(day: date) -> str:
    """The sentence that tells the model the date.

    A small local model knows only the year it was trained in and answered
    "2023-10-12" for today (Build15.6). The date only, not the time: the
    system prompt is the start of every request the model caches, so it may
    change once a day, not every minute.
    """
    return f"Today is {_WEEKDAYS[day.weekday()]}, {day.isoformat()}."


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
        today: Callable[[], date] = date.today,
    ) -> None:
        self.router = router
        self.system_prompt = system_prompt
        self.today = today
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

    def system(self) -> str:
        """The system prompt with today's date."""
        return f"{self.system_prompt}\n\n{today_line(self.today())}"

    def messages(self, target: str = "local") -> list[Message]:
        # One turn is a user message and the assistant's answer.
        history = self.history[self.cloud_from :] if target == "cloud" else self.history
        tail = history[-2 * self.max_turns :]
        return [{"role": "system", "content": self.system()}, *tail]

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
            system = self.system() + (tools.prompt() if tools else "")
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
            request[0] = {"role": "system", "content": request[0]["content"] + tools.prompt()}

        parts: list[str] = []
        if tools:
            tools.heard(text)
        if tools and mail_flow.wants(tools, text):
            # Milestone В3 as fixed steps: the model only reads and writes.
            flow = mail_flow.MailFlow(
                tools,
                self.router.complete_local,
                today=self.today(),
                user_turn=self.user_turn,
                on_tool=self.on_tool,
            )
            for piece in flow.run(text):
                parts.append(piece)
                yield piece
            self.history.append(pending)
            self.history.append({"role": "assistant", "content": "".join(parts)})
            return
        refused: set[tuple[str, str]] = set()  # the person said no, this message
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
            if not calls or tools is None or not offered:
                break  # in the last round no function was offered: none runs
            names = {d["function"]["name"] for d in offered}
            request.append(
                {
                    "role": "assistant",
                    "content": "".join(said) or None,
                    "tool_calls": [c.as_message_part() for c in calls],
                }
            )
            # A model may read an email and, in the same answer, add the event
            # it has not seen yet, with a date it guessed (gloxi-bench --tasks
            # mail, 2026-11-23). After a read, the rest waits for the next round.
            read: str | None = None
            not_sent: ToolOutcome | None = None
            for i, call in enumerate(calls):
                key = (call.name, call.arguments)
                if i >= MAX_CALLS_PER_ANSWER:
                    outcome = tools.not_run(call.name, f"at most {MAX_CALLS_PER_ANSWER} at once")
                elif call.name not in names:
                    # Only what was offered this time (Herkos decides anyway).
                    outcome = tools.not_run(call.name, "it was not offered now")
                elif key in refused:
                    outcome = tools.not_run(call.name, "the person already said no to this")
                elif read is not None and not tools.reads_outside(call.name):
                    outcome = tools.not_yet(call.name, read)
                else:
                    outcome = tools.call(call.name, call.arguments, user_turn=self.user_turn)
                    if outcome.status == "denied":
                        refused.add(key)
                    if outcome.status == "done" and tools.reads_outside(call.name):
                        read = call.name
                    if self.on_tool:
                        self.on_tool(outcome)
                    if tools.sends_mail(call.name) and outcome.status != "done":
                        not_sent = not_sent or outcome
                request.append(
                    {"role": "tool", "tool_call_id": call.id, "content": outcome.for_model}
                )
            if not_sent is not None:
                # After a send Herkos did not run, the models passed Gloxi's
                # note on to the person, offered to try again, or answered in
                # English (6 of 12 right, VM check after PR #232): Gloxi says
                # it in fixed words, as on the steps of mail_flow.
                told = mail_flow.not_sent(text, not_sent)
                told = f"\n\n{told}" if parts else told
                parts.append(told)
                yield told
                break
        # Tool messages are not kept: file contents stay out of the history,
        # which a later message may send to the cloud.
        self.history.append(pending)
        self.history.append({"role": "assistant", "content": "".join(parts)})
