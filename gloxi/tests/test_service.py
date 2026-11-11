# SPDX-License-Identifier: GPL-3.0-or-later
"""gloxid and the window's RemoteController over a real Unix socket (ADR 0025)."""

import json
import os
import socket
import threading
import time

import pytest
from fake_server import FakeLlamaServer
from helpers import make_conversation
from test_gui_controller import RecordingView, sync

from gloxi.gui.controller import ChatController
from gloxi.gui.remote import RemoteController, ServiceError, window_language
from gloxi.service import EventView, Service, bind, inherited_listener, service_socket


class Running:
    def __init__(self, tmp_path, local=None, *, owner_uid=None, allow_root=True, **kwargs):
        self.path = tmp_path / "gloxi.sock"
        self.view = EventView()
        conv = make_conversation(local or FakeLlamaServer("Привіт, я Gloxi."), **kwargs)
        self.controller = ChatController(conv, self.view, runner=sync)
        self.environ = {}
        self.service = Service(
            self.controller,
            self.view,
            owner_uid=os.getuid() if owner_uid is None else owner_uid,
            environ=self.environ,
            allow_root=allow_root,
        )
        self.listener = bind(self.path)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.service.serve, args=(self.listener, self.stop))
        self.thread.start()

    def client(self, view=None, **kwargs):
        view = view or RecordingView()
        remote = RemoteController(self.path, view, language="uk_UA.UTF-8", timeout=5, **kwargs)
        remote.connect()
        return remote, view

    def close(self):
        self.stop.set()
        self.thread.join(5)
        self.listener.close()


@pytest.fixture
def running(tmp_path):
    started = []

    def start(*args, **kwargs):
        r = Running(tmp_path, *args, **kwargs)
        started.append(r)
        return r

    yield start
    for r in started:
        r.close()


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_answer_streams_to_the_window_in_order(running):
    r = running()
    remote, view = r.client()
    assert remote.submit("  привіт ")
    assert wait_for(lambda: ("busy", False) in view.events[1:])
    assert view.events[:3] == [("busy", True), ("user", "привіт"), ("start", "On this computer")]
    assert view.answer() == "Привіт, я Gloxi."
    assert view.kinds()[-2:] == ["finish", "busy"]
    assert not remote.busy
    # The conversation lives in the service.
    assert [m["role"] for m in r.controller.conversation.history] == ["user", "assistant"]


def test_hello_sets_the_language_and_reports_the_state(running):
    r = running()
    remote, _view = r.client()
    assert r.environ["LANGUAGE"] == "uk_UA.UTF-8"
    assert remote.cloud_status() == "Cloud off: everything stays on this computer"
    assert [c.mode for c in remote.choices()] == ["local"]
    assert remote.current_choice() == "local"


def test_model_switch_goes_through_the_service(running):
    r = running(cloud=FakeLlamaServer("хмара"))
    remote, _view = r.client()
    # The cloud client exists but the policy keeps it off.
    assert not remote.set_mode("cloud")
    assert r.controller.conversation.router.mode != "cloud"
    assert remote.current_choice() == "local"


def test_reset_clears_the_service_history(running):
    r = running()
    remote, view = r.client()
    remote.submit("раз")
    assert wait_for(lambda: not remote.busy and ("finish",) in view.events)
    remote.reset()
    assert r.controller.conversation.history == []


def test_a_new_window_takes_over(running):
    r = running()
    first, first_view = r.client()
    second, second_view = r.client()
    assert wait_for(lambda: ("note", "Gloxi is open in another window.") in first_view.events)
    assert ("error",) not in [e[:1] for e in first_view.events]
    assert second.submit("ти тут?")
    assert wait_for(lambda: ("finish",) in second_view.events)
    assert ("finish",) not in first_view.events


def test_somebody_else_gets_no_answer(running):
    # The tests may run as root, whom the service trusts; not here.
    r = running(owner_uid=os.getuid() + 12345, allow_root=False)
    remote = RemoteController(r.path, RecordingView(), timeout=2)
    with pytest.raises(ServiceError):
        remote.connect()
    assert r.controller.conversation.history == []


def raw_exchange(path, *lines):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(path))
    sock.settimeout(5)
    for line in lines:
        sock.sendall(line if isinstance(line, bytes) else (json.dumps(line) + "\n").encode())
    out, buf = [], b""
    while len(out) < len(lines):
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            msg = json.loads(raw)
            if "event" not in msg:
                out.append(msg)
    sock.close()
    return out


def test_hostile_requests_get_errors_not_crashes(running):
    r = running()
    answers = raw_exchange(
        r.path,
        b"not json\n",
        [1, 2],
        {"id": 3, "method": "chat.nothing"},
        {"id": 4, "method": "chat.submit", "params": {"text": 5}},
        {"id": 5, "method": "chat.set_mode", "params": {"mode": "everything"}},
        {"id": 6, "method": "hello", "params": {"language": "uk; rm -rf /"}},
        {"id": 7, "method": "chat.offer_intro", "params": {"always": "yes"}},
        {"id": 8, "method": "state", "params": []},
    )
    codes = [a["error"]["code"] for a in answers]
    assert codes == [
        "bad_json",
        "bad_request",
        "unknown_method",
        "bad_params",
        "bad_params",
        "bad_params",
        "bad_params",
        "bad_params",
    ]
    assert "LANGUAGE" not in r.environ
    # Still serving.
    assert raw_exchange(r.path, {"id": 9, "method": "state"})[0]["result"]["busy"] is False


def test_the_window_hears_when_the_service_goes_away(running):
    r = running()
    remote, view = r.client()
    r.close()
    # Closing the listener does not end live connections: end it from the service side.
    conn = r.view._conn
    conn.shutdown(socket.SHUT_RDWR)
    assert wait_for(lambda: any(e[0] == "error" for e in view.events))
    assert not remote.submit("є хто?")  # nothing to reconnect to


def test_root_is_trusted_only_when_allowed():
    view = EventView()
    assert Service(None, view, owner_uid=1000).allowed(0)
    assert not Service(None, view, owner_uid=1000, allow_root=False).allowed(0)
    assert not Service(None, view, owner_uid=1000).allowed(1001)
    assert not Service(None, view, owner_uid=1000).allowed(None)


def test_window_language_comes_from_the_environment():
    assert window_language({"LANG": "pl_PL.UTF-8"}) == "pl_PL.UTF-8"
    assert window_language({"LANGUAGE": "uk:en", "LANG": "C"}) == "uk:en"
    assert window_language({}) is None


def test_service_socket_default_and_override():
    assert str(service_socket({})) == f"/run/gloxi/{os.getuid()}/gloxi.sock"
    assert str(service_socket({"GLOXI_SERVICE_SOCKET": "/tmp/x.sock"})) == "/tmp/x.sock"


def test_stdin_that_is_not_a_listening_socket_is_ignored():
    # pytest's stdin is a file or /dev/null, never systemd's socket.
    assert inherited_listener() is None


def test_bind_refuses_to_replace_a_file(tmp_path):
    path = tmp_path / "gloxi.sock"
    path.write_text("not a socket")
    with pytest.raises(SystemExit):
        bind(path)
    path.unlink()
    sock = bind(path)
    try:
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    finally:
        sock.close()


def test_gloxid_finds_the_home_of_the_person_it_serves():
    import os
    import pwd

    from gloxi.service import owner_home

    # Its own user: Gloxi in the person's session, ~ is already theirs.
    assert owner_home(os.getuid()) is None
    other = next((p for p in pwd.getpwall() if p.pw_uid != os.getuid()), None)
    if other is not None:
        assert str(owner_home(other.pw_uid)) == other.pw_dir
    assert owner_home(2**31 - 7) is None  # no such person


# -- a new login (Build14.9: gloxid kept the conversation after a logout) ----------


def test_a_new_login_session_starts_a_new_conversation(running, monkeypatch):
    r = running()
    monkeypatch.setenv("XDG_SESSION_ID", "3")
    remote, view = r.client()
    assert remote.submit("привіт")
    assert wait_for(lambda: len(r.controller.conversation.history) == 2)
    remote.close()

    # The same login opens the window again: the conversation goes on.
    remote, _view = r.client()
    assert len(r.controller.conversation.history) == 2
    remote.close()

    # After logging out and in again it starts afresh.
    monkeypatch.setenv("XDG_SESSION_ID", "7")
    remote, _view = r.client()
    assert r.controller.conversation.history == []
    assert r.service.session == "7"


def test_a_bad_session_id_is_refused(running):
    r = running()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(r.path))
        s.sendall(b'{"id": 1, "method": "hello", "params": {"session": "../x y"}}\n')
        answer = json.loads(s.makefile().readline())
    assert answer["error"]["code"] == "bad_params"


def test_wake_says_hello_with_the_language_and_session(running, monkeypatch):
    from gloxi.service import wake

    r = running()
    monkeypatch.setenv("XDG_SESSION_ID", "c2")
    monkeypatch.setenv("LANGUAGE", "pl_PL.UTF-8")
    assert wake(r.path) is True
    assert r.environ["LANGUAGE"] == "pl_PL.UTF-8"
    assert r.service.session == "c2"


def test_wake_without_gloxid_is_quiet(tmp_path):
    from gloxi.service import wake

    assert wake(tmp_path / "nothing.sock") is False


def test_the_wake_autostart_runs_gloxi_wake():
    from pathlib import Path

    desktop = Path(__file__).parents[1] / "data/autostart/io.github.stdenis1980.Gloxi-wake.desktop"
    text = desktop.read_text(encoding="utf-8")
    assert "Exec=gloxi wake\n" in text and "NoDisplay=true\n" in text
