# SPDX-License-Identifier: GPL-3.0-or-later
"""The larger model is offered only where the speed test says it is fast enough."""

import io
from dataclasses import replace

import httpx

from gloxi import upgrade
from gloxi.cli import prepare_engine
from gloxi.config import load_settings
from gloxi.engine import DEFAULT_MODEL, LARGE_MODEL, Plan
from gloxi.upgrade import SpeedResult, measure_speed

GIB = 1024**3


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_measure_counts_only_answers_after_the_warm_up():
    clock = Clock()
    seen = []

    def handler(request):
        # Each answer: 64 tokens; fast at first, slower once the laptop throttles.
        seen.append(request.url.path)
        clock.now += 8.0 if clock.now < 10 else 16.0
        return httpx.Response(200, json={"usage": {"completion_tokens": 64}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = measure_speed("http://127.0.0.1:8080/v1", client=client, clock=clock)
    assert seen[0] == "/v1/chat/completions"
    assert result.tokens_per_second == 4.0  # 64 tokens / 16 s, not the early 8/s
    assert result.large_expected == 4.0 * upgrade.LARGE_SPEED_FACTOR
    assert not result.large_is_fast_enough


def test_measure_on_a_very_slow_computer_uses_every_answer():
    clock = Clock()

    def handler(request):
        clock.now += 40.0
        return httpx.Response(200, json={"timings": {"predicted_n": 80}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert measure_speed("http://x/v1", client=client, clock=clock).tokens_per_second == 2.0


def plan_in(tmp_path) -> Plan:
    return Plan("start", model=DEFAULT_MODEL, binary="/bin/llama-server", folder=tmp_path)


def test_the_test_runs_once_and_only_on_16_gb(tmp_path):
    p = plan_in(tmp_path)
    assert not upgrade.needs_test(p, 8 * GIB)
    assert not upgrade.needs_test(p, None)
    assert upgrade.needs_test(p, int(15.3 * GIB))
    upgrade.record_result(tmp_path, SpeedResult(4.6))
    assert not upgrade.needs_test(p, 16 * GIB)
    assert not upgrade.should_offer(tmp_path)  # 4.6 * 0.65 < 6


class FakeEngine:
    def __init__(self, *args):
        pass

    def start(self):
        pass

    def stop(self):
        pass


def run_cli(tmp_path, answer, speed, fetched):
    settings = replace(load_settings(tmp_path / "none.toml", env={}), start_server=True)
    out = io.StringIO()

    def fetch(model, folder, progress):
        fetched.append(model.filename)
        progress(model.size, model.size)
        return folder / model.filename

    prepare_engine(
        settings,
        read=lambda prompt: answer,
        out=out,
        planner=lambda url, **kw: plan_in(tmp_path),
        fetch=fetch,
        engine_type=FakeEngine,
        ram=16 * GIB,
        measure=lambda url: SpeedResult(speed),
    )
    return out.getvalue()


def test_a_fast_computer_is_offered_the_large_model_once(tmp_path):
    fetched = []
    text = run_cli(tmp_path, "n", 10.0, fetched)
    assert "Checking how fast" in text and LARGE_MODEL.name in text and "6.5" in text
    assert fetched == [] and "will not ask again" in text
    assert "Checking how fast" not in run_cli(tmp_path, "y", 10.0, fetched)
    assert fetched == []  # declined once: never asked again


def test_yes_downloads_the_large_model_for_the_next_start(tmp_path):
    fetched = []
    text = run_cli(tmp_path, "y", 10.0, fetched)
    assert fetched == [LARGE_MODEL.filename] and "next start" in text


def test_a_slow_computer_is_not_asked(tmp_path):
    fetched = []
    text = run_cli(tmp_path, "y", 4.6, fetched)
    assert "Checking how fast" in text and LARGE_MODEL.name not in text and fetched == []
    assert upgrade.load_state(tmp_path)["tokens_per_second"] == 4.6


def test_a_failed_test_does_not_stop_the_chat_and_is_tried_again(tmp_path):
    settings = replace(load_settings(tmp_path / "none.toml", env={}), start_server=True)

    def broken(url):
        raise httpx.ConnectError("no")

    engine = prepare_engine(
        settings,
        read=lambda prompt: "y",
        out=io.StringIO(),
        planner=lambda url, **kw: plan_in(tmp_path),
        engine_type=FakeEngine,
        ram=16 * GIB,
        measure=broken,
    )
    assert engine is not None and upgrade.needs_test(plan_in(tmp_path), 16 * GIB)


def test_the_window_asks_after_the_test(tmp_path):
    from gloxi.gui.controller import ModelSetup

    events, fetched = [], []

    class View:
        def ask_model_download(self, text, answer):
            events.append(("ask", text))
            answer(True)

        def show_model_progress(self, text, fraction):
            events.append(("progress", text))

        def hide_model_progress(self):
            pass

        def show_error(self, text):
            events.append(("error", text))

        def show_note(self, text):
            events.append(("note", text))

    def fetch(model, folder, progress, cancelled):
        fetched.append(model.filename)
        return folder / model.filename

    settings = replace(load_settings(tmp_path / "none.toml", env={}), start_server=True)
    setup = ModelSetup(
        settings,
        View(),
        runner=lambda job: job(),
        planner=lambda url, **kw: plan_in(tmp_path),
        fetch=fetch,
        engine_type=FakeEngine,
        ram=lambda: 16 * GIB,
        measure=lambda url: SpeedResult(12.0),
    )
    setup.begin()
    assert any(e[0] == "progress" and "Checking how fast" in e[1] for e in events)
    assert any(e[0] == "ask" and LARGE_MODEL.name in e[1] for e in events)
    assert fetched == [LARGE_MODEL.filename] and events[-1][0] == "note"
