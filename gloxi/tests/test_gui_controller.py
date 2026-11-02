# SPDX-License-Identifier: GPL-3.0-or-later
from fake_server import FakeLlamaServer
from helpers import make_conversation

from gloxi.config import RouterPolicy
from gloxi.gui.controller import ChatController, cloud_status


class RecordingView:
    def __init__(self):
        self.events = []

    def show_user_message(self, text):
        self.events.append(("user", text))

    def start_answer(self, label):
        self.events.append(("start", label))

    def append_answer(self, piece):
        self.events.append(("piece", piece))

    def finish_answer(self):
        self.events.append(("finish",))

    def show_error(self, text):
        self.events.append(("error", text))

    def set_busy(self, busy):
        self.events.append(("busy", busy))

    def answer(self):
        return "".join(e[1] for e in self.events if e[0] == "piece")

    def kinds(self):
        return [e[0] for e in self.events]


def sync(job):
    job()


def make(local, **kwargs):
    view = RecordingView()
    conv = make_conversation(local, **kwargs)
    return ChatController(conv, view, runner=sync), view, conv


def test_submit_streams_answer_in_order():
    ctl, view, conv = make(FakeLlamaServer("Привіт, я Gloxi."))
    assert ctl.submit("  привіт  ")
    assert view.events[:3] == [("busy", True), ("user", "привіт"), ("start", "On this computer")]
    assert view.answer() == "Привіт, я Gloxi."
    assert view.kinds()[-2:] == ["finish", "busy"]
    assert view.events[-1] == ("busy", False)
    assert ctl.busy is False
    assert len(conv.history) == 2


def test_empty_and_busy_input_is_ignored():
    ctl, view, _ = make(FakeLlamaServer())
    assert not ctl.submit("   ")
    assert not ctl.submit("/cloud")
    ctl.busy = True
    assert not ctl.submit("привіт")
    assert view.events == []


def test_cloud_command_refused_when_cloud_off():
    ctl, view, _ = make(FakeLlamaServer())
    assert not ctl.submit("/cloud поясни")
    assert view.kinds() == ["error"]


def test_cloud_route_label_says_not_masked():
    cloud = FakeLlamaServer("хмара")
    ctl, view, _ = make(FakeLlamaServer(), cloud=cloud, policy=RouterPolicy(cloud_allowed=True))
    ctl.submit("/cloud поясни")
    assert ("start", "Cloud: api.example.eu, not masked") in view.events
    assert view.answer() == "хмара"


def test_model_error_is_shown_and_busy_cleared():
    ctl, view, conv = make(FakeLlamaServer(status=500))
    ctl.submit("привіт")
    errors = [e for e in view.events if e[0] == "error"]
    assert errors and "HTTP 500" in errors[0][1]
    assert view.events[-1] == ("busy", False)
    assert conv.history == []


def test_cloud_status_text():
    _, _, conv = make(FakeLlamaServer())
    assert cloud_status(conv).startswith("Cloud off")
    _, _, conv = make(
        FakeLlamaServer(), cloud=FakeLlamaServer(), policy=RouterPolicy(cloud_allowed=True)
    )
    assert "NOT masked" in cloud_status(conv)
