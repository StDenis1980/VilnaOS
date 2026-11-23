# SPDX-License-Identifier: GPL-3.0-or-later
"""gloxid and the window's RemoteController over a real Unix socket (ADR 0025)."""

import dataclasses
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from fake_server import FakeLlamaServer
from helpers import make_conversation
from test_gui_controller import RecordingView, sync

from gloxi.gui.controller import ChatController
from gloxi.gui.remote import RemoteController, ServiceError, window_language
from gloxi.service import EventView, Service, bind, inherited_listener, service_socket


class Running:
    def __init__(
        self, tmp_path, local=None, *, owner_uid=None, allow_root=True, translator=None, **kwargs
    ):
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
            translator=translator,
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


def test_without_xdg_session_id_the_session_comes_from_logind(monkeypatch):
    # Build14.10: Vilna's GNOME session has no XDG_SESSION_ID.
    import subprocess

    from gloxi.gui.remote import login_session

    calls = []

    def loginctl(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="5\n", stderr="")

    assert login_session({}, run=loginctl) == "5"
    assert calls[0][:2] == ["loginctl", "show-user"] and "--property=Display" in calls[0]
    assert login_session({"XDG_SESSION_ID": "c2"}, run=loginctl) == "c2"
    assert len(calls) == 1

    def missing(argv, **_kwargs):
        raise FileNotFoundError(argv[0])

    def no_display(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="\n", stderr="")

    def odd(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="../x y\n", stderr="")

    assert login_session({}, run=missing) is None
    assert login_session({}, run=no_display) is None
    assert login_session({}, run=odd) is None


def test_a_new_login_without_xdg_session_id_starts_a_new_conversation(running, monkeypatch):
    monkeypatch.delenv("XDG_SESSION_ID", raising=False)
    r = running()
    remote, _view = r.client(session=lambda: "4")
    assert remote.submit("привіт")
    assert wait_for(lambda: len(r.controller.conversation.history) == 2)
    remote.close()
    remote, _view = r.client(session=lambda: "9")
    assert r.controller.conversation.history == []


def test_a_window_opened_again_shows_the_kept_conversation(running):
    r = running()
    remote, view = r.client(session=lambda: "4")
    assert remote.submit("привіт")
    assert wait_for(lambda: not remote.busy and ("finish",) in view.events)
    remote.close()

    # Build14.10: the window opened again was empty, yet gloxid remembered.
    remote, view = r.client(session=lambda: "4")
    assert wait_for(lambda: ("finish",) in view.events)
    assert view.events[:2] == [("user", "привіт"), ("start", "Gloxi")]
    assert view.answer() == "Привіт, я Gloxi."
    # Shown once: a reconnect does not show it twice.
    remote.close()
    remote._closed = False
    remote.connect()
    assert [e for e in view.events if e[0] == "user"] == [("user", "привіт")]


def test_hello_sends_back_only_what_was_said_and_not_too_much(running):
    from gloxi.service import HISTORY_BYTES

    r = running()
    r.controller.conversation.history[:] = [
        {"role": "user", "content": "старе " * HISTORY_BYTES},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "content": "вміст файлу"},
        {"role": "user", "content": "нове"},
        {"role": "assistant", "content": "відповідь"},
    ]
    assert r.service.history() == [
        {"role": "user", "text": "нове"},
        {"role": "assistant", "text": "відповідь"},
    ]


def test_wake_without_gloxid_is_quiet(tmp_path):
    from gloxi.service import wake

    assert wake(tmp_path / "nothing.sock") is False


def test_the_wake_autostart_runs_gloxi_wake():
    from pathlib import Path

    desktop = Path(__file__).parents[1] / "data/autostart/io.github.stdenis1980.Gloxi-wake.desktop"
    text = desktop.read_text(encoding="utf-8")
    assert "Exec=gloxi wake\n" in text and "NoDisplay=true\n" in text


def test_share_history_goes_through_the_service(running):
    r = running()
    remote, _view = r.client()
    assert remote.held_back() == 0
    remote.share_history(True)  # nothing held back: harmless
    assert remote.held_back() == 0
    answers = raw_exchange(
        r.path, {"id": 1, "method": "chat.share_history", "params": {"share": "yes"}}
    )
    assert answers[0]["error"]["code"] == "bad_params"


def test_the_local_model_keeps_idle_slots_cached():
    # Keros checks in another slot; without this flag llama-server clears
    # Gloxi's slot on every check and the next turn reads the prompt again.
    unit = Path(__file__).resolve().parents[1] / "systemd" / "vilna-llm.service"
    exec_start = unit.read_text().split("ExecStart=", 1)[1].split("\nWorkingDirectory=", 1)[0]
    assert "--no-cache-idle-slots" in exec_start


def test_the_local_model_keeps_its_weights_in_memory():
    # On 8 GB the browser pushed the weights out and the first answer took
    # 36 s instead of 6.8 s (image/docs/low-memory-check.md).
    unit = Path(__file__).resolve().parents[1] / "systemd" / "vilna-llm.service"
    assert "\nMemoryLow=5G\n" in unit.read_text()


def test_system_slice_lets_the_model_keep_its_protection():
    # cgroup v2 protects a unit only up to its parent slice, and system.slice
    # has memory.low=0: without this drop-in MemoryLow=5G of vilna-llm did
    # nothing on Build16.22 (memory.events "low 0").
    systemd = Path(__file__).resolve().parents[1] / "systemd"
    unit = (systemd / "vilna-llm.service").read_text()
    assert "\nSlice=" not in unit
    drop_in = (systemd / "system.slice.d" / "50-vilna-llm.conf").read_text()
    assert "\n[Slice]\nMemoryLow=5G\n" in drop_in
    spec_path = Path(__file__).resolve().parents[2] / "obs" / "packages" / "gloxi" / "gloxi.spec"
    spec = spec_path.read_text()
    assert "%{_unitdir}/system.slice.d/50-vilna-llm.conf" in spec


# -- text.translate (the weather warnings of the Shell extension) -------------------


def fake_translator(model):
    from helpers import LOCAL

    from gloxi.llm import ChatClient
    from gloxi.translate import translate

    client = ChatClient(dataclasses.replace(LOCAL, slot=None), transport=model.transport())
    return lambda text, language: translate(client, text, language)


def test_translate_asks_the_local_model_alone(running):
    model = FakeLlamaServer("Помаранчеве попередження: сильний вітер.")
    r = running(translator=fake_translator(model))
    [answer] = raw_exchange(
        r.path,
        {
            "id": 1,
            "method": "text.translate",
            "params": {"text": "Orange warning: strong wind.", "language": "uk"},
        },
    )
    assert answer["result"] == {
        "text": "Помаранчеве попередження: сильний вітер.",
        "language": "uk",
    }
    [request] = model.requests
    assert "Ukrainian" in request["messages"][0]["content"]
    assert request["messages"][1] == {"role": "user", "content": "Orange warning: strong wind."}
    assert "tools" not in request and "id_slot" not in request
    # Not part of the conversation.
    assert r.controller.conversation.history == []


def test_a_translation_does_not_take_the_window_away(running):
    r = running(translator=fake_translator(FakeLlamaServer("Переклад.")))
    remote, view = r.client()
    [answer] = raw_exchange(
        r.path, {"id": 1, "method": "text.translate", "params": {"text": "Fog.", "language": "uk"}}
    )
    assert answer["result"]["text"] == "Переклад."
    assert ("note", "Gloxi is open in another window.") not in view.events
    assert remote.submit("ти тут?")
    assert wait_for(lambda: ("finish",) in view.events)


def test_translate_refuses_bad_requests_and_says_when_there_is_no_model(running):
    r = running(translator=fake_translator(FakeLlamaServer("x", status=503)))
    answers = raw_exchange(
        r.path,
        {"id": 1, "method": "text.translate", "params": {"text": "", "language": "uk"}},
        {"id": 2, "method": "text.translate", "params": {"text": "Fog.", "language": "ru"}},
        {"id": 3, "method": "text.translate", "params": {"text": "x" * 5000, "language": "uk"}},
        {"id": 4, "method": "text.translate", "params": {"text": "Fog.", "language": "uk"}},
    )
    assert [a["error"]["code"] for a in answers] == [
        "bad_params",
        "bad_params",
        "bad_params",
        "unavailable",
    ]
    without = running(translator=None)
    [answer] = raw_exchange(
        without.path,
        {"id": 1, "method": "text.translate", "params": {"text": "Fog.", "language": "uk"}},
    )
    assert answer["error"]["code"] == "unavailable"


def test_a_translation_drops_thinking_and_refuses_an_empty_answer():
    from gloxi.llm import ModelError
    from gloxi.translate import translate

    think = fake_translator(FakeLlamaServer("<think>hmm</think>\n Туман. "))
    assert think("Fog.", "uk") == "Туман."
    with pytest.raises(ModelError):
        fake_translator(FakeLlamaServer("<think>hmm</think>"))("Fog.", "uk")
    with pytest.raises(ValueError):
        translate(None, "Fog.", "xx")


def test_the_window_hands_the_installers_vpn_choice_to_gloxid(running, tmp_path):
    """VM Build12.9: gloxid runs with ProtectHome=yes and its own state, so the
    wizard's file in the person's home is handed on by the window."""
    from test_vpn_intro import FakeHerkos as FakeVpn

    from gloxi.vpn_intro import MULLVAD_APP, VpnIntro

    r = running()
    service_state = tmp_path / "service" / "vpn-intro.json"
    r.controller.vpn = VpnIntro(FakeVpn(tunnels=(0, 1)), path=service_state)
    home_state = tmp_path / "home" / "vpn-intro.json"
    home_state.parent.mkdir()
    home_state.write_text('{"status": "chosen", "choice": "mullvad"}')
    remote, view = r.client(vpn_state=home_state)
    assert remote.offer_intro()
    assert wait_for(lambda: ("launch", MULLVAD_APP) in view.events)
    assert "You chose this while installing Vilna." in view.answer()
    assert json.loads(home_state.read_text())["status"] == "handed"
    # Not handed again: the next start finds nothing to send.
    assert json.loads(service_state.read_text())["choice"] == "mullvad"


def test_gloxid_keeps_its_own_vpn_talk_over_a_handed_choice(running, tmp_path):
    from test_vpn_intro import FakeHerkos as FakeVpn

    from gloxi.vpn_intro import VpnIntro

    r = running()
    service_state = tmp_path / "vpn-service.json"
    service_state.write_text('{"status": "declined", "time": 1}')
    r.controller.vpn = VpnIntro(FakeVpn(), path=service_state)
    answers = raw_exchange(
        r.path,
        {"id": 1, "method": "chat.offer_intro", "params": {"vpn_chosen": "proton"}},
        {"id": 2, "method": "chat.offer_intro", "params": {"vpn_chosen": "nordvpn"}},
    )
    assert answers[0]["result"]["vpn_adopted"] is False
    assert answers[1]["error"]["code"] == "bad_params"
    assert json.loads(service_state.read_text())["status"] == "declined"
