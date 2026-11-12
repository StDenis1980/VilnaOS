# SPDX-License-Identifier: GPL-3.0-or-later
"""The window's side of gloxid (ADR 0025).

``RemoteController`` has the same calls as ``ChatController``, so the window
does not care whether Gloxi runs in its own process or as the system service.
Requests go over the socket and wait for their answer; events from the
service (the answer as it streams in, notes, errors) arrive on a reader
thread and go to the view through ``dispatch`` (in the window: the GTK main
loop, so they stay in order).
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import re
import socket
import subprocess
import threading
from collections.abc import Callable
from gettext import gettext as _
from pathlib import Path
from typing import Any

from gloxi.gui.controller import ChatView, ModelChoice

TIMEOUT = 10.0
MAX_LINE = 1 << 20
# A login session id, as logind names them (XDG_SESSION_ID, "3", "c2").
SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# At most this many earlier messages are shown again when the window opens.
MAX_SHOWN = 200


class ServiceError(Exception):
    pass


def window_language(env: dict[str, str] | None = None) -> str | None:
    """The language the service should answer in: the window's own."""
    env = os.environ if env is None else env
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        if env.get(var):
            return env[var]
    return None


def login_session(
    env: dict[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str | None:
    """The person's login session; a new one tells gloxid that a new login began.

    XDG_SESSION_ID when the session sets it. Vilna's GNOME session does not
    (Build14.10: empty in the terminal, in ``systemctl --user
    show-environment`` and in gnome-shell), so otherwise logind is asked for
    the person's graphical session.
    """
    env = os.environ if env is None else env
    session = env.get("XDG_SESSION_ID", "")
    if SESSION_RE.match(session):
        return session
    try:
        done = run(
            ["loginctl", "show-user", str(os.getuid()), "--property=Display", "--value"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    session = done.stdout.strip() if done.returncode == 0 else ""
    return session if SESSION_RE.match(session) else None


class RemoteController:
    def __init__(
        self,
        path: Path,
        view: ChatView,
        *,
        dispatch: Callable[[Callable[[], None]], None] = lambda job: job(),
        language: str | None = None,
        timeout: float = TIMEOUT,
        session: Callable[[], str | None] = login_session,
    ) -> None:
        self.path = path
        self.view = view
        self.dispatch = dispatch
        self.language = language if language is not None else window_language()
        self.timeout = timeout
        self.session = session
        # The earlier messages are shown once, when the window opens.
        self._shown_history = False
        self.busy = False
        self._state: dict = {"cloud_status": "", "current": "local", "choices": []}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()  # one request at a time
        self._waiting: dict[int, dict] = {}
        self._answered = threading.Condition()
        self._sock: socket.socket | None = None
        self._closed = False

    # -- the connection -------------------------------------------------------------

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(self.path))
        except OSError as exc:
            sock.close()
            raise ServiceError(str(exc)) from None
        self._sock = sock
        threading.Thread(target=self._reader, args=(sock,), daemon=True).start()
        params = {"language": self.language} if self.language else {}
        session = self.session()
        if session:
            params["session"] = session
        hello = self._call("hello", params, reconnect=False)
        self._take_state(hello.get("state"))
        if not self._shown_history:
            self._shown_history = True
            self._show_history(hello.get("history"))

    def _show_history(self, history: Any) -> None:
        """Show the conversation gloxid kept, when the window is opened again.

        gloxid keeps the conversation while the window is closed (Build14.10:
        the reopened window was empty, yet Gloxi answered from what came
        before), so the person sees what Gloxi remembers.
        """
        if not isinstance(history, list):
            return
        view = self.view
        for item in history[-MAX_SHOWN:]:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            text = item["text"]
            if item.get("role") == "user":
                self.dispatch(lambda text=text: view.show_user_message(text))
            elif item.get("role") == "assistant":

                def answer(text: str = text) -> None:
                    view.start_answer(_("Gloxi"))
                    view.append_answer(text)
                    view.finish_answer()

                self.dispatch(answer)

    def close(self) -> None:
        self._closed = True
        sock, self._sock = self._sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()

    def _reader(self, sock: socket.socket) -> None:
        buf = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(message, dict):
                        self._receive(message)
                if len(buf) > MAX_LINE:
                    break
        except OSError:
            pass
        self._lost(sock)

    def _lost(self, sock: socket.socket) -> None:
        with self._answered:
            if self._sock is sock:
                self._sock = None
            self._answered.notify_all()
        if not self._closed:
            self._set_busy(False)
            self.dispatch(
                lambda: self.view.show_error(
                    _("The connection to the Gloxi service was lost. Try again.")
                )
            )

    def _receive(self, message: dict) -> None:
        if "event" in message:
            self._event(str(message["event"]), message)
            return
        rid = message.get("id")
        if isinstance(rid, int):
            with self._answered:
                self._waiting[rid] = message
                self._answered.notify_all()

    def _event(self, name: str, m: dict) -> None:
        view = self.view
        text = str(m.get("text", ""))
        if name == "user_message":
            self.dispatch(lambda: view.show_user_message(text))
        elif name == "start_answer":
            label = str(m.get("label", ""))
            self.dispatch(lambda: view.start_answer(label))
        elif name == "append_answer":
            piece = str(m.get("piece", ""))
            self.dispatch(lambda: view.append_answer(piece))
        elif name == "finish_answer":
            self.dispatch(view.finish_answer)
        elif name == "error":
            self.dispatch(lambda: view.show_error(text))
        elif name == "note":
            self.dispatch(lambda: view.show_note(text))
        elif name == "busy":
            self._set_busy(bool(m.get("busy")))
        elif name == "open_uri":
            url = str(m.get("url", ""))
            if url.startswith(("https://", "mailto:")):
                self.dispatch(lambda: view.open_uri(url))
        elif name == "replaced":
            self._closed = True  # another window took over; not an error
            self.dispatch(lambda: view.show_note(_("Gloxi is open in another window.")))

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.dispatch(lambda: self.view.set_busy(busy))

    def _call(self, method: str, params: dict | None = None, *, reconnect: bool = True) -> Any:
        with self._lock:
            if self._sock is None:
                if not reconnect:
                    raise ServiceError("not connected")
                self._closed = False
                self._lock.release()
                try:
                    self.connect()
                finally:
                    self._lock.acquire()
            sock = self._sock
            if sock is None:
                raise ServiceError("not connected")
            rid = next(self._ids)
            line = json.dumps({"id": rid, "method": method, "params": params or {}}) + "\n"
            try:
                sock.sendall(line.encode("utf-8"))
            except OSError as exc:
                raise ServiceError(str(exc)) from None
            with self._answered:
                ok = self._answered.wait_for(
                    lambda: rid in self._waiting or self._sock is not sock, self.timeout
                )
                message = self._waiting.pop(rid, None)
            if not ok or message is None:
                raise ServiceError("the Gloxi service did not answer")
            if "error" in message:
                err = message["error"] if isinstance(message["error"], dict) else {}
                raise ServiceError(str(err.get("message", "error")))
            return message.get("result", {})

    def _try(self, method: str, params: dict | None = None) -> Any | None:
        try:
            return self._call(method, params)
        except ServiceError:
            self.view.show_error(
                _("Gloxi is not reachable: its service is not running. Try again in a moment.")
            )
            return None

    def _take_state(self, state: Any) -> None:
        if isinstance(state, dict):
            self._state = state
            self.busy = bool(state.get("busy", self.busy))

    # -- what the window calls (ChatController's interface) -------------------------

    def submit(self, raw: str) -> bool:
        if not raw.strip() or self.busy:
            return False
        result = self._try("chat.submit", {"text": raw})
        return bool(result and result.get("accepted"))

    def reset(self) -> None:
        if not self.busy:
            self._try("chat.reset")

    def offer_intro(self, *, always: bool = False) -> bool:
        result = self._try("chat.offer_intro", {"always": always})
        return bool(result and result.get("started"))

    def report_problem(self) -> bool:
        result = self._try("chat.report_problem")
        return bool(result and result.get("started"))

    def choices(self) -> list[ModelChoice]:
        out = []
        for c in self._state.get("choices") or []:
            if isinstance(c, dict):
                out.append(
                    ModelChoice(str(c.get("mode")), str(c.get("label")), str(c.get("detail")))
                )
        return out or [ModelChoice("local", _("Local"), _("On this computer"))]

    def current_choice(self) -> str:
        modes = [c.mode for c in self.choices()]
        current = str(self._state.get("current", "local"))
        return current if current in modes else modes[0]

    def set_mode(self, mode: str) -> bool:
        result = self._try("chat.set_mode", {"mode": mode})
        if not result:
            return False
        self._take_state(result.get("state"))
        return bool(result.get("ok"))

    def cloud_status(self) -> str:
        return str(self._state.get("cloud_status", ""))
