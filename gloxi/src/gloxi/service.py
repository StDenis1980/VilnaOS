# SPDX-License-Identifier: GPL-3.0-or-later
"""gloxid: Gloxi as a service, apart from the window (ADR 0025).

In Vilna each person's Gloxi runs as its own system user (``gloxi-<uid>``,
unit ``gloxi@<uid>.service``). It keeps the conversation, the router, the
privacy gateway, the cloud key and the Herkos connection; the window in the
person's session (``gloxi-gtk``) only shows what it sends and passes on what
the person types. They talk over a Unix socket, one JSON object per line
(``docs/service-protocol.md``).

The service answers only the person it belongs to: the uid the kernel
reports for the socket (``SO_PEERCRED``) must be the owner's, or root.
One window and one side panel at a time (the panel is the one the Vilna
Shell extension draws, stage B of the Gloxi window): both see the same
conversation. A new window takes over from the old window, a new panel from
the old panel; the old one is told (``replaced``) and closed.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import signal
import socket
import stat
import struct
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gloxi import __version__, ai_notice
from gloxi.vpn_intro import CHOSEN

# 2: hello takes "client": "panel" (the Shell's side panel beside the window).
PROTOCOL = 2
# A line longer than this is not a request: the connection is closed.
MAX_LINE = 1 << 20
# What the window may send as its language (LANGUAGE / LANG values).
LANGUAGE_RE = re.compile(r"^[A-Za-z0-9_@.:-]{1,100}$")
# The login session a window belongs to (gloxi.gui.remote.login_session).
SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# At most this much of the kept conversation goes back to a window in hello.
HISTORY_BYTES = 256 * 1024
MODES = ("auto", "local", "cloud")
# Requests that other parts of the desktop make (the Shell extension's weather
# warnings): a connection that makes only these is not a window, so it does
# not take the events away from the Gloxi window that is open.
NOT_A_WINDOW = frozenset({"text.translate"})
# Who looks at the conversation: the Gloxi window, the Shell's side panel.
CLIENTS = ("window", "panel")
# Events that do something on the desktop (open a link, start an app): only
# the client the person last wrote in gets them, so nothing opens twice.
ONE_CLIENT = frozenset({"open_uri", "launch_app"})


def service_socket(env: dict[str, str] | None = None) -> Path:
    """Where the window finds the service: GLOXI_SERVICE_SOCKET or /run/gloxi/<uid>/."""
    env = os.environ if env is None else env
    if env.get("GLOXI_SERVICE_SOCKET"):
        return Path(env["GLOXI_SERVICE_SOCKET"])
    return Path("/run/gloxi") / str(os.getuid()) / "gloxi.sock"


def wake(path: Path | None = None, *, timeout: float = 5.0) -> bool:
    """Say hello to gloxid at login, so it starts and reads the prompt in advance.

    gloxid starts on the first connection (gloxi@<uid>.socket) and then has
    the local model read the system prompt and the functions, which takes
    about two minutes on a slow processor (Build14.9). At login, not when the
    window first opens, that wait is over before the person writes. The
    session id also tells gloxid that a new login began.
    """
    from gloxi.gui.remote import login_session, window_language

    params: dict[str, str] = {}
    language = window_language()
    if language and LANGUAGE_RE.match(language):
        params["language"] = language
    session = login_session()
    if session:
        params["session"] = session
    line = json.dumps({"id": 1, "method": "hello", "params": params}) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path or service_socket()))
            sock.sendall(line.encode())
            return bool(sock.recv(MAX_LINE))
    except OSError:
        return False


def peer_uid(conn: socket.socket) -> int | None:
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except (OSError, AttributeError):
        return None
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


class EventView:
    """A ChatView that sends every call to the clients connected now.

    At most one window and one panel (CLIENTS). Every event goes to both,
    except ONE_CLIENT events, which go only to the client that last made a
    request (the one the person is using).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conns: dict[str, socket.socket] = {}
        self._active: socket.socket | None = None

    def attach(self, conn: socket.socket, client: str = "window") -> socket.socket | None:
        """Make ``conn`` the window (or the panel); returns the one it replaces."""
        with self._lock:
            old = self._conns.get(client)
            self._conns[client] = conn
            self._active = conn
        return old

    def detach(self, conn: socket.socket) -> None:
        with self._lock:
            for client, c in list(self._conns.items()):
                if c is conn:
                    del self._conns[client]
            if self._active is conn:
                self._active = next(iter(self._conns.values()), None)

    def touch(self, conn: socket.socket) -> None:
        """The person used ``conn``: it gets the ONE_CLIENT events now."""
        with self._lock:
            if conn in self._conns.values():
                self._active = conn

    def send(self, obj: dict) -> None:
        line = _line(obj)
        with self._lock:
            if obj.get("event") in ONE_CLIENT:
                targets = [self._active] if self._active is not None else []
            else:
                targets = list(self._conns.values())
            # Nobody looking: the answer still lands in the history.
            for conn in targets:
                try:
                    conn.sendall(line)
                except OSError:
                    self._drop(conn)

    def _drop(self, conn: socket.socket) -> None:
        for client, c in list(self._conns.items()):
            if c is conn:
                del self._conns[client]
        if self._active is conn:
            self._active = next(iter(self._conns.values()), None)

    def reply(self, conn: socket.socket, obj: dict) -> None:
        """A response to ``conn``, never in the middle of an event's bytes."""
        with self._lock:
            try:
                conn.sendall(_line(obj))
            except OSError:
                self._drop(conn)

    def _event(self, name: str, **data: Any) -> None:
        self.send({"event": name, **data})

    # -- ChatView --------------------------------------------------------------

    def show_user_message(self, text: str) -> None:
        self._event("user_message", text=text)

    def start_answer(self, label: str) -> None:
        self._event("start_answer", label=label)

    def mark_answer(self, mark: dict) -> None:
        self._event("answer_mark", mark=mark)

    def append_answer(self, piece: str) -> None:
        self._event("append_answer", piece=piece)

    def finish_answer(self) -> None:
        self._event("finish_answer")

    def show_error(self, text: str) -> None:
        self._event("error", text=text)

    def set_busy(self, busy: bool) -> None:
        self._event("busy", busy=busy)

    def show_note(self, text: str) -> None:
        self._event("note", text=text)

    def show_action(self, card: dict) -> None:
        self._event("action", card=card)

    def open_uri(self, url: str) -> None:
        self._event("open_uri", url=url)

    def launch_app(self, app_id: str) -> None:
        self._event("launch_app", app=app_id)


def client_of(request: Any) -> str:
    """Which client a first request makes of its connection: a panel says so in hello."""
    if isinstance(request, dict) and request.get("method") == "hello":
        params = request.get("params")
        if isinstance(params, dict) and params.get("client") == "panel":
            return "panel"
    return "window"


class RequestError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Service:
    """Answers the window's requests with a ChatController behind them."""

    def __init__(
        self,
        controller,
        view: EventView,
        *,
        owner_uid: int,
        environ: dict[str, str] | None = None,
        allow_root: bool = True,
        translator: Callable[[str, str], str] | None = None,
    ) -> None:
        self.controller = controller
        # (text, language) -> text, by the model on this computer only
        # (gloxi.translate); None when there is none.
        self.translator = translator
        self.view = view
        self.owner_uid = owner_uid
        self.allow_root = allow_root
        # gettext reads LANGUAGE on every call: the window's language wins.
        self.environ = os.environ if environ is None else environ
        # The login session of the last window: gloxid outlives a logout.
        self.session: str | None = None
        self._handlers: dict[str, Callable[[dict], Any]] = {
            "hello": self._hello,
            "state": lambda _p: self.state(),
            "chat.submit": self._submit,
            "chat.reset": self._reset,
            "chat.set_mode": self._set_mode,
            "chat.share_history": self._share_history,
            "chat.offer_intro": self._offer_intro,
            "chat.report_problem": lambda _p: {"started": self.controller.report_problem()},
            "text.translate": self._translate,
        }

    # -- requests ------------------------------------------------------------------

    def state(self) -> dict:
        c = self.controller
        return {
            "busy": bool(c.busy),
            "cloud_status": c.cloud_status(),
            "current": c.current_choice(),
            "held_back": c.held_back(),
            "choices": [
                {"mode": ch.mode, "label": ch.label, "detail": ch.detail} for ch in c.choices()
            ],
        }

    def _hello(self, params: dict) -> dict:
        client = params.get("client", "window")
        if client not in CLIENTS:
            raise RequestError("bad_params", f"client must be one of {', '.join(CLIENTS)}")
        language = params.get("language")
        if language is not None:
            if not isinstance(language, str) or not LANGUAGE_RE.match(language):
                raise RequestError("bad_params", "language must look like uk_UA.UTF-8 or uk:en")
            self.environ["LANGUAGE"] = language
        session = params.get("session")
        if session is not None:
            if not isinstance(session, str) or not SESSION_RE.match(session):
                raise RequestError("bad_params", "session must be a login session id")
            if self.session is not None and session != self.session:
                # gloxid lives on after the person logs out (Build14.9): a new
                # login starts a new conversation, as a new window would
                # without the service, and does not answer from the old one.
                self.controller.reset()
            self.session = session
        return {
            "protocol": PROTOCOL,
            "version": __version__,
            "state": self.state(),
            "history": self.history(),
        }

    def history(self) -> list[dict]:
        """The kept conversation, newest last, for a window that opens again.

        Only what the person wrote and Gloxi answered: tool results never
        enter the history, and the language hint is never stored. The oldest
        messages are left out past HISTORY_BYTES.
        """
        conversation = getattr(self.controller, "conversation", None)
        kept = list(getattr(conversation, "history", None) or [])
        out: list[dict] = []
        size = 0
        for message in reversed(kept):
            role, text = message.get("role"), message.get("content")
            if role not in ("user", "assistant") or not isinstance(text, str):
                continue
            size += len(text.encode("utf-8"))
            if size > HISTORY_BYTES:
                break
            item: dict = {"role": role, "text": text}
            if role == "assistant":
                # Every answer kept here is the model's (ADR 0033).
                item["mark"] = ai_notice.mark()
            out.append(item)
        out.reverse()
        return out

    def _submit(self, params: dict) -> dict:
        text = params.get("text")
        if not isinstance(text, str):
            raise RequestError("bad_params", "text must be a string")
        if len(text) > MAX_LINE // 2:
            raise RequestError("bad_params", "text is too long")
        return {"accepted": self.controller.submit(text)}

    def _reset(self, _params: dict) -> dict:
        busy = self.controller.busy
        self.controller.reset()
        return {"reset": not busy}

    def _set_mode(self, params: dict) -> dict:
        mode = params.get("mode")
        if mode not in MODES:
            raise RequestError("bad_params", f"mode must be one of {', '.join(MODES)}")
        return {"ok": self.controller.set_mode(mode), "state": self.state()}

    def _share_history(self, params: dict) -> dict:
        share = params.get("share")
        if not isinstance(share, bool):
            raise RequestError("bad_params", "share must be true or false")
        self.controller.share_history(share)
        return {"state": self.state()}

    def _offer_intro(self, params: dict) -> dict:
        always = params.get("always", False)
        if not isinstance(always, bool):
            raise RequestError("bad_params", "always must be true or false")
        # The VPN the person chose in the installer wizard: the wizard wrote it
        # in their home, which this service cannot read (ProtectHome=yes), so
        # their window hands it on (gloxi.vpn_intro.installer_choice).
        handed = params.get("vpn_chosen")
        if handed is not None and handed not in CHOSEN:
            raise RequestError("bad_params", f"vpn_chosen must be one of {', '.join(CHOSEN)}")
        vpn = getattr(self.controller, "vpn", None)
        adopted = handed is not None and vpn is not None and vpn.adopt(handed)
        return {
            "started": self.controller.offer_intro(always=always),
            "vpn_adopted": adopted,
        }

    def _translate(self, params: dict) -> dict:
        from gloxi.llm import ModelError
        from gloxi.translate import LANGUAGES, MAX_TEXT

        text, language = params.get("text"), params.get("language")
        if not isinstance(text, str) or not text.strip():
            raise RequestError("bad_params", "text must be a non-empty string")
        if len(text) > MAX_TEXT:
            raise RequestError("bad_params", f"text must be at most {MAX_TEXT} characters")
        if language not in LANGUAGES:
            raise RequestError("bad_params", f"language must be one of {', '.join(LANGUAGES)}")
        if self.translator is None:
            raise RequestError("unavailable", "no model on this computer")
        try:
            return {"text": self.translator(text, language), "language": language}
        except ModelError as exc:
            raise RequestError("unavailable", str(exc)) from exc

    def handle(self, request: Any) -> dict | None:
        """One request in, one response out (None for a request without id)."""
        if not isinstance(request, dict):
            return {"id": None, "error": {"code": "bad_request", "message": "not an object"}}
        rid = request.get("id")
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return {
                "id": rid,
                "error": {"code": "bad_params", "message": "params must be an object"},
            }
        handler = self._handlers.get(method) if isinstance(method, str) else None
        if handler is None:
            return {
                "id": rid,
                "error": {"code": "unknown_method", "message": f"no method {method!r}"},
            }
        try:
            result = handler(params)
        except RequestError as exc:
            return {"id": rid, "error": {"code": exc.code, "message": exc.message}}
        return {"id": rid, "result": result}

    # -- connections -------------------------------------------------------------

    def allowed(self, uid: int | None) -> bool:
        if uid is None:
            return False
        return uid == self.owner_uid or (self.allow_root and uid == 0)

    def serve_connection(self, conn: socket.socket) -> None:
        with conn:
            if not self.allowed(peer_uid(conn)):
                return  # anybody else gets no answer at all
            try:
                self._read(conn)
            finally:
                self.view.detach(conn)

    def _attach(self, conn: socket.socket, client: str = "window") -> None:
        """``conn`` becomes the window (or the panel); the one before it is told and closed."""
        old = self.view.attach(conn, client)
        if old is not None and old is not conn:
            try:
                old.sendall(_line({"event": "replaced"}))
                old.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _read(self, conn: socket.socket) -> None:
        # A connection becomes the window with its first request that is
        # not in NOT_A_WINDOW (for the window itself, its hello).
        window = False
        buf = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                if not raw.strip():
                    continue
                try:
                    request = json.loads(raw)
                except ValueError:
                    response = {"id": None, "error": {"code": "bad_json", "message": "not JSON"}}
                else:
                    method = request.get("method") if isinstance(request, dict) else None
                    if not window and method not in NOT_A_WINDOW:
                        self._attach(conn, client_of(request))
                        window = True
                    elif window and isinstance(method, str) and method.startswith("chat."):
                        self.view.touch(conn)
                    response = self.handle(request)
                if response is not None:
                    self.view.reply(conn, response)
            if len(buf) > MAX_LINE:
                return

    def serve(self, listener: socket.socket, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        listener.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _addr = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if stop.is_set():
                    return
                raise
            conn.settimeout(None)
            threading.Thread(target=self.serve_connection, args=(conn,), daemon=True).start()


# -- start-up --------------------------------------------------------------------------


def owner_home(owner_uid: int) -> Path | None:
    """The home of the person gloxid serves, when that is not gloxid's own user.

    gloxi@<uid> runs as gloxi-<uid> with HOME=/var/lib/gloxi/<uid>; ``~`` and
    the default folders must still be the person's (ADR 0025).
    """
    if owner_uid == os.getuid():
        return None
    try:
        return Path(pwd.getpwuid(owner_uid).pw_dir)
    except KeyError:
        return None


def build_controller(view: EventView, owner_uid: int | None = None):
    """The same Gloxi as the window builds for itself, without the model download.

    In Vilna the model runs as vilna-llm.service; outside Vilna whoever starts
    gloxid runs llama-server (or the Flatpak's ModelSetup does).
    """
    from gloxi.backup_intro import backup_intro
    from gloxi.chat import Conversation
    from gloxi.config import load_settings
    from gloxi.gui.controller import ChatController
    from gloxi.llm import ChatClient
    from gloxi.network import herkos_network
    from gloxi.privacy import guard_factory
    from gloxi.router import Router
    from gloxi.tools import file_tools
    from gloxi.vpn_intro import vpn_intro

    settings = load_settings()
    local = ChatClient(settings.local)
    cloud = ChatClient(settings.cloud) if settings.cloud else None
    # The service may reach only this computer; the cloud needs a yes from
    # the person through Herkos first (ADR 0025, section 3).
    network = herkos_network()
    router = Router(
        local,
        cloud,
        settings.router,
        guard_factory=guard_factory(settings.router),
        network=network,
    )
    if network is not None and not router.cloud_enabled:
        # The cloud was turned off: close the way it may have left open.
        threading.Thread(target=network.close, daemon=True).start()
    home = owner_home(owner_uid) if owner_uid is not None else None
    tools = file_tools(settings.tools_enabled, owner_home=home)
    conversation = Conversation(router, settings.system_prompt, tools=tools)
    controller = ChatController(
        conversation,
        view,
        intro=backup_intro(settings.tools_enabled),
        vpn=vpn_intro(settings.tools_enabled),
    )
    return controller, local_translator(local)


def local_translator(local) -> Callable[[str, str], str]:
    """Translations by the model on this computer, never the cloud (gloxi.translate)."""
    from gloxi.translate import translate, translator_client

    client = translator_client(local)
    return lambda text, language: translate(client, text, language)


def inherited_listener() -> socket.socket | None:
    """The listening socket systemd passes as stdin (StandardInput=socket)."""
    try:
        if not stat.S_ISSOCK(os.fstat(0).st_mode):
            return None
        sock = socket.socket(fileno=os.dup(0))
    except OSError:
        return None
    try:
        listening = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
    except OSError:
        listening = 0
    if not listening:
        sock.close()
        return None
    return sock


def bind(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise SystemExit(f"gloxid: {path} exists and is not a socket")
        path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(str(path))
    finally:
        os.umask(old)
    sock.listen(8)
    return sock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gloxid", description="Gloxi as a service (ADR 0025)")
    parser.add_argument(
        "--socket", type=Path, help="listen here (default: the socket from systemd)"
    )
    parser.add_argument(
        "--owner-uid", type=int, default=os.getuid(), help="the person this Gloxi serves"
    )
    args = parser.parse_args(argv)

    from gloxi import crash
    from gloxi.config import ConfigError
    from gloxi.i18n import setup

    setup()
    crash.install()
    listener = inherited_listener() if args.socket is None else None
    if listener is None:
        listener = bind(args.socket or service_socket())
    view = EventView()
    try:
        controller, translator = build_controller(view, args.owner_uid)
    except ConfigError as exc:
        print(f"gloxid: {exc}", file=sys.stderr)
        return 78  # EX_CONFIG: systemd does not restart in a loop
    service = Service(controller, view, owner_uid=args.owner_uid, translator=translator)
    # The window that started gloxid is about to be used: have the model read
    # the prompt and the functions while the person types.
    controller.warm()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_a: stop.set())
    signal.signal(signal.SIGINT, lambda *_a: stop.set())
    print(f"gloxid {__version__}: serving uid {args.owner_uid}", file=sys.stderr, flush=True)
    service.serve(listener, stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
