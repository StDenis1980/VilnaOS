# SPDX-License-Identifier: GPL-3.0-or-later
"""Toolkit-independent logic behind the chat window.

The window hands user input to ``ChatController.submit``; the controller
works out the route, streams the answer on a worker and reports back
through ``ChatView`` callbacks. Keeping GTK out of this module lets us
test it without a display.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from gettext import gettext as _
from typing import Protocol

from gloxi.chat import Conversation
from gloxi.cli import split_command
from gloxi.llm import ModelError, ModelUnavailable
from gloxi.router import CloudNotAllowed, Route

# Runs a callable on a worker; replaced by a synchronous runner in tests.
Runner = Callable[[Callable[[], None]], None]


def thread_runner(job: Callable[[], None]) -> None:
    threading.Thread(target=job, daemon=True).start()


class ChatView(Protocol):
    """What the controller needs from a window. Calls may come from a
    worker thread; a GTK window must forward them to the main loop."""

    def show_user_message(self, text: str) -> None: ...
    def start_answer(self, label: str) -> None: ...
    def append_answer(self, piece: str) -> None: ...
    def finish_answer(self) -> None: ...
    def show_error(self, text: str) -> None: ...
    def set_busy(self, busy: bool) -> None: ...
    def show_note(self, text: str) -> None: ...


def route_label(route: Route, masks_cloud: bool) -> str:
    if route.target == "local":
        return _("On this computer")
    if masks_cloud:
        return _("Cloud: {host}, masked").format(host=route.destination)
    return _("Cloud: {host}, not masked").format(host=route.destination)


def cloud_status(conversation: Conversation) -> str:
    router = conversation.router
    if not router.cloud_enabled:
        return _("Cloud off: everything stays on this computer")
    if router.masks_cloud:
        return _("Cloud on, private data is masked")
    return _("Cloud on, masking is off: messages are NOT masked")


class ChatController:
    def __init__(
        self, conversation: Conversation, view: ChatView, *, runner: Runner = thread_runner
    ) -> None:
        self.conversation = conversation
        self.view = view
        self.runner = runner
        self.busy = False
        conversation.on_tool = lambda outcome: view.show_note(outcome.summary())

    def submit(self, raw: str) -> bool:
        """Handle one line of input. Returns False if it was ignored."""
        text = raw.strip()
        if not text or self.busy:
            return False
        force, text = split_command(text)
        if not text:
            return False
        try:
            route = self.conversation.route(text, force=force)
        except CloudNotAllowed:
            self.view.show_error(_("The cloud is turned off in the settings."))
            return False

        self.busy = True
        self.view.set_busy(True)
        self.view.show_user_message(text)
        self.view.start_answer(route_label(route, self.conversation.router.masks_cloud))
        self.runner(lambda: self._stream(text, route))
        return True

    def reset(self) -> None:
        if not self.busy:
            self.conversation.reset()

    def _stream(self, text: str, route: Route) -> None:
        try:
            for piece in self.conversation.ask(text, route):
                self.view.append_answer(piece)
        except ModelUnavailable:
            if route.target == "local":
                self.view.show_error(
                    _("The local model is not running. Start llama-server and try again.")
                )
            else:
                self.view.show_error(_("Cannot reach the cloud model."))
        except ModelError as exc:
            self.view.show_error(_("The model returned an error: {error}").format(error=exc))
        finally:
            self.view.finish_answer()
            self.busy = False
            self.view.set_busy(False)
