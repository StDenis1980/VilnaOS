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
from dataclasses import dataclass
from gettext import gettext as _
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from gloxi import upgrade
from gloxi.backup_intro import BackupIntro, Turn
from gloxi.chat import Conversation
from gloxi.cli import model_consent_text, split_command, upgrade_consent_text
from gloxi.config import Settings
from gloxi.engine import (
    LARGE_MODEL,
    Engine,
    EngineError,
    Plan,
    download,
    model_name,
    plan,
    total_ram,
)
from gloxi.llm import ModelError, ModelUnavailable
from gloxi.router import CloudNotAllowed, Route, Router

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


@dataclass(frozen=True)
class ModelChoice:
    mode: str  # auto | local | cloud
    label: str  # shown in the switcher
    detail: str  # where messages go, shown as its tooltip


def current_choice(router: Router) -> str:
    """The switcher's selection; with the cloud off every message is local."""
    return router.mode if router.cloud_enabled else "local"


def model_choices(router: Router) -> list[ModelChoice]:
    """The model switcher in the header: the cloud only when it is turned on."""
    local = router.local.endpoint.model
    if local == "local":  # the default name says nothing
        here = _("On this computer")
    else:
        here = _("{model}, on this computer").format(model=local)
    choices = [ModelChoice("local", _("Local"), here)]
    if router.cloud_enabled:
        cloud = router.cloud.endpoint
        host = urlsplit(cloud.url).hostname or cloud.url
        if router.masks_cloud:
            where = _("{model} at {host}, private data masked")
        else:
            where = _("{model} at {host}, NOT masked")
        choices.insert(
            0,
            ModelChoice(
                "auto",
                _("Auto"),
                _("Short messages stay on this computer, long ones go to the cloud"),
            ),
        )
        choices.append(ModelChoice("cloud", _("Cloud"), where.format(model=cloud.model, host=host)))
    return choices


class ChatController:
    def __init__(
        self,
        conversation: Conversation,
        view: ChatView,
        *,
        runner: Runner = thread_runner,
        intro: BackupIntro | None = None,
    ) -> None:
        self.conversation = conversation
        self.view = view
        self.runner = runner
        self.intro = intro
        self.busy = False
        conversation.on_tool = lambda outcome: view.show_note(outcome.summary())

    def submit(self, raw: str) -> bool:
        """Handle one line of input. Returns False if it was ignored."""
        text = raw.strip()
        if not text or self.busy:
            return False
        if text == "/backup":
            return self.offer_intro(always=True)
        if self.intro and self.intro.active:
            self.view.show_user_message(text)
            return self._intro_step(lambda: self.intro.answer(text))
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

    def offer_intro(self, *, always: bool = False) -> bool:
        """The backup talk (design.md 2a): on start if due, or on /backup."""
        if self.busy or self.intro is None:
            if always and self.intro is None:
                self.view.show_error(_("Backups need Herkos, and it is not running."))
            return False
        if not always and not self.intro.due():
            return False
        return self._intro_step(self.intro.start)

    def _intro_step(self, step: Callable[[], Turn | None]) -> bool:
        # Herkos may show its window and wait for the person: off the UI thread.
        self.busy = True
        self.view.set_busy(True)

        def job() -> None:
            try:
                turn = step()
                if turn is not None:
                    self.view.start_answer(_("Gloxi, backups"))
                    self.view.append_answer(turn.render())
                    self.view.finish_answer()
            finally:
                self.busy = False
                self.view.set_busy(False)

        self.runner(job)
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


class ModelSetupView(Protocol):
    """What ``ModelSetup`` needs from a window; calls may come from a worker."""

    def ask_model_download(self, text: str, answer: Callable[[bool], None]) -> None: ...
    def show_model_progress(self, text: str, fraction: float | None) -> None: ...
    def hide_model_progress(self) -> None: ...
    def show_error(self, text: str) -> None: ...
    def show_note(self, text: str) -> None: ...


class ModelSetup:
    """The bundled engine on start-up: ask before downloading, then start it."""

    def __init__(
        self,
        settings: Settings,
        view: ModelSetupView,
        *,
        runner: Runner = thread_runner,
        planner: Callable[..., Plan] = plan,
        fetch: Callable[..., object] = download,
        engine_type: Callable[..., Engine] = Engine,
        ram: Callable[[], int | None] = total_ram,
        measure: Callable[..., upgrade.SpeedResult] = upgrade.measure_speed,
    ) -> None:
        self.settings = settings
        self.ram = ram
        self.measure = measure
        self.view = view
        self.runner = runner
        self.planner = planner
        self.fetch = fetch
        self.engine_type = engine_type
        self.engine: Engine | None = None
        self._plan: Plan | None = None
        self._cancel = threading.Event()

    def begin(self) -> None:
        self.runner(self._look)

    def _look(self) -> None:
        p = self.planner(self.settings.local.url, start_server=self.settings.start_server)
        self._plan = p
        if p.action == "download":
            self.view.ask_model_download(model_consent_text(p), self.answer)
        elif p.action == "start":
            self._start(p.model_path)

    def answer(self, yes: bool) -> None:
        if not yes:
            self.view.show_note(_("Not downloaded. Gloxi will ask again next time."))
            return
        self.runner(self._download)

    def cancel(self) -> None:
        self._cancel.set()

    def _download(self) -> None:
        p = self._plan
        self._cancel.clear()

        def progress(done: int, total: int) -> None:
            fraction = done / max(total, 1)
            text = _("Downloading the model: {percent}%").format(percent=int(fraction * 100))
            self.view.show_model_progress(text, fraction)

        try:
            path = self.fetch(p.model, p.folder, progress=progress, cancelled=self._cancel.is_set)
        except EngineError as exc:
            self.view.hide_model_progress()
            self.view.show_error(_("The model was not downloaded: {why}").format(why=exc))
            return
        self._start(path)

    def _start(self, model_path) -> None:
        text = _("Starting {name} on this computer…").format(name=model_name(model_path))
        self.view.show_model_progress(text, None)
        engine = self.engine_type(self._plan.binary, model_path, self.settings.local.url)
        try:
            engine.start()
        except EngineError as exc:
            self.view.show_error(_("The model could not be started: {why}").format(why=exc))
            return
        finally:
            self.view.hide_model_progress()
        self.engine = engine
        self._offer_large_model()

    # -- the larger model, once, after the speed test (gloxi.upgrade) ----------------

    def _offer_large_model(self) -> None:
        p = self._plan
        if upgrade.needs_test(p, self.ram()):
            self.view.show_model_progress(
                _("Checking how fast this computer runs the model (about a minute)…"), None
            )
            try:
                upgrade.record_result(p.folder, self.measure(self.settings.local.url))
            except (httpx.HTTPError, ValueError, OSError):
                return
            finally:
                self.view.hide_model_progress()
        if upgrade.should_offer(p.folder):
            expected = upgrade.load_state(p.folder).get("large_expected", 0.0)
            self.view.ask_model_download(
                upgrade_consent_text(p.folder, expected), self.answer_large
            )

    def answer_large(self, yes: bool) -> None:
        upgrade.record_answer(self._plan.folder, yes)
        if yes:
            self.runner(self._download_large)
        else:
            self.view.show_note(_("Gloxi keeps the current model and will not ask again."))

    def _download_large(self) -> None:
        folder = self._plan.folder
        self._cancel.clear()

        def progress(done: int, total: int) -> None:
            fraction = done / max(total, 1)
            text = _("Downloading the model: {percent}%").format(percent=int(fraction * 100))
            self.view.show_model_progress(text, fraction)

        try:
            self.fetch(LARGE_MODEL, folder, progress=progress, cancelled=self._cancel.is_set)
        except EngineError as exc:
            upgrade.record_answer_reset(folder)
            self.view.show_error(_("The model was not downloaded: {why}").format(why=exc))
            return
        finally:
            self.view.hide_model_progress()
        self.view.show_note(_("The model is ready. Gloxi will use it from the next start."))

    def stop(self) -> None:
        self._cancel.set()
        if self.engine is not None:
            self.engine.stop()
            self.engine = None
