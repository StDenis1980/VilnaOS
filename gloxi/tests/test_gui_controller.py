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

    def show_note(self, text):
        self.events.append(("note", text))

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


def test_cloud_status_when_masked():
    from gloxi.privacy import guard_factory

    on = RouterPolicy(cloud_allowed=True)
    _, _, conv = make(
        FakeLlamaServer(), cloud=FakeLlamaServer(), policy=on, guard_factory=guard_factory(on)
    )
    assert cloud_status(conv) == "Cloud on, private data is masked"


def test_tool_calls_are_shown_as_notes():
    import json

    from fake_server import ScriptedLlamaServer
    from helpers import LOCAL
    from test_tools import Connector, tools

    from gloxi.chat import Conversation
    from gloxi.llm import ChatClient
    from gloxi.router import Router

    call = [("files_read", json.dumps({"path": "/home/u/Documents/a", "reason": "r"}))]
    server = ScriptedLlamaServer(call, "Готово.")
    router = Router(ChatClient(LOCAL, transport=server.transport()), None, RouterPolicy())
    conv = Conversation(router, "SYS", tools=tools(Connector()))
    view = RecordingView()
    ctl = ChatController(conv, view, runner=sync)
    assert ctl.submit("прочитай a")
    assert ("note", "files: read /home/u/Documents/a (done)") in view.events
    assert view.answer() == "Готово."


def test_backup_talk_in_the_window(tmp_path):
    from test_backup_intro import FakeHerkos

    from gloxi.backup_intro import BackupIntro

    server = FakeLlamaServer()
    view = RecordingView()
    intro = BackupIntro(FakeHerkos(), path=tmp_path / "intro.json")
    controller = ChatController(make_conversation(server), view, runner=sync, intro=intro)
    assert controller.offer_intro()
    assert "Shall I keep backups" in view.answer()
    assert controller.submit("2")  # "Later": goes to the talk, not to the model
    assert "ask once more" in view.answer()
    assert ("user", "2") in view.events
    assert not controller.offer_intro()  # not due any more
    assert controller.submit("/backup")  # but always on request
    assert view.answer().count("Shall I keep backups") == 2
    assert server.requests == []


def test_backup_talk_without_herkos():
    controller, view, _ = make(FakeLlamaServer())
    assert not controller.submit("/backup")
    assert view.kinds() == ["error"]


class SetupView:
    def __init__(self, yes):
        self.yes = yes
        self.events = []

    def ask_model_download(self, text, answer):
        self.events.append(("ask", text))
        answer(self.yes)

    def show_model_progress(self, text, fraction):
        self.events.append(("progress", text, fraction))

    def hide_model_progress(self):
        self.events.append(("hide",))

    def show_error(self, text):
        self.events.append(("error", text))

    def show_note(self, text):
        self.events.append(("note", text))


class StubEngine:
    def __init__(self, binary, model_path, url):
        self.model_path = model_path
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


def _setup(tmp_path, yes, fetched):
    from dataclasses import replace

    from gloxi.config import load_settings
    from gloxi.engine import DEFAULT_MODEL, Plan
    from gloxi.gui.controller import ModelSetup

    view = SetupView(yes)
    p = Plan("download", model=DEFAULT_MODEL, binary="/bin/llama-server", folder=tmp_path)

    def fetch(model, folder, progress, cancelled):
        fetched.append(model.filename)
        progress(model.size // 2, model.size)
        return folder / model.filename

    settings = replace(load_settings(tmp_path / "none.toml", env={}), start_server=True)
    setup = ModelSetup(
        settings,
        view,
        runner=lambda job: job(),
        planner=lambda url, **kw: p,
        fetch=fetch,
        engine_type=StubEngine,
        ram=lambda: None,
    )
    return setup, view


def test_model_setup_asks_and_respects_no(tmp_path):
    fetched = []
    setup, view = _setup(tmp_path, False, fetched)
    setup.begin()
    assert view.events[0][0] == "ask" and "Qwen3 4B" in view.events[0][1]
    assert fetched == [] and setup.engine is None
    assert view.events[-1][0] == "note"


def test_model_setup_downloads_after_yes_and_starts(tmp_path):
    fetched = []
    setup, view = _setup(tmp_path, True, fetched)
    setup.begin()
    assert fetched == ["Qwen3-4B-Q4_K_M.gguf"]
    assert ("progress", "Downloading the model: 50%", 0.5) in view.events
    assert setup.engine is not None and setup.engine.running
    assert view.events[-1] == ("hide",)
    setup.stop()
    assert setup.engine is None


def test_model_switcher_choices():
    from gloxi.gui.controller import current_choice, model_choices

    _, _, conv = make(FakeLlamaServer())
    choices = model_choices(conv.router)
    assert [c.mode for c in choices] == ["local"]  # the cloud is off: nothing to switch to
    assert current_choice(conv.router) == "local"

    _, _, conv = make(
        FakeLlamaServer(), cloud=FakeLlamaServer(), policy=RouterPolicy(cloud_allowed=True)
    )
    choices = model_choices(conv.router)
    assert [c.mode for c in choices] == ["auto", "local", "cloud"]
    assert "NOT masked" in choices[-1].detail
    assert current_choice(conv.router) == "auto"
    conv.router.set_mode("cloud")
    assert current_choice(conv.router) == "cloud"
