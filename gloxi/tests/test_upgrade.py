# SPDX-License-Identifier: GPL-3.0-or-later
"""The larger model is offered only where the speed test says it is fast enough."""

import io
from dataclasses import replace

import httpx

from gloxi import upgrade
from gloxi.cli import prepare_engine
from gloxi.config import load_settings
from gloxi.engine import BEST_MODEL, DEFAULT_MODEL, GPU_MODEL, LARGE_MODEL, Plan
from gloxi.upgrade import Machine, SpeedResult, measure_speed

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
        clock.now += 8.0 if clock.now < 30 else 16.0
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
    assert not upgrade.should_offer(tmp_path, Machine(16 * GIB))  # 4.6 * 0.48 < 6


class FakeEngine:
    def __init__(self, *args):
        pass

    def start(self):
        pass

    def stop(self):
        pass


def run_cli(tmp_path, answer, speed, fetched, ram=16 * GIB, gpu=None):
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
        ram=ram,
        measure=lambda url: SpeedResult(speed),
        machine=lambda folder, ram: Machine(ram, gpu, 100 * GIB),
    )
    return out.getvalue()


def test_a_fast_computer_is_offered_the_large_model_once(tmp_path):
    fetched = []
    text = run_cli(tmp_path, "n", 14.0, fetched)
    assert "Checking how fast" in text and LARGE_MODEL.name in text and "6.7" in text
    assert fetched == [] and "will not ask again" in text
    assert "Checking how fast" not in run_cli(tmp_path, "y", 14.0, fetched)
    assert fetched == []  # declined once: never asked again


def test_yes_downloads_the_large_model_for_the_next_start(tmp_path):
    fetched = []
    text = run_cli(tmp_path, "y", 14.0, fetched)
    assert fetched == [LARGE_MODEL.filename] and "next start" in text


def test_a_slow_computer_is_not_asked(tmp_path):
    fetched = []
    # The test laptop: Qwen3 4B 9.4 tokens/s on Vulkan, Ministral would give ~4.5.
    text = run_cli(tmp_path, "y", 9.4, fetched)
    assert "Checking how fast" in text and LARGE_MODEL.name not in text and fetched == []
    assert upgrade.load_state(tmp_path)["tokens_per_second"] == 9.4


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
        machine=lambda folder, ram: Machine(ram),
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
        measure=lambda url: SpeedResult(14.0),
        machine=lambda folder, ram: Machine(ram, None, 100 * GIB),
    )
    setup.begin()
    assert any(e[0] == "progress" and "Checking how fast" in e[1] for e in events)
    assert any(e[0] == "ask" and LARGE_MODEL.name in e[1] for e in events)
    assert fetched == [LARGE_MODEL.filename] and events[-1][0] == "note"


# -- the tiers of 2026-11-13: the best model that fits and is fast enough ----------


def offer(speed, ram, gpu=None, disk=100 * GIB, folder=None):
    return upgrade.best_offer(speed, Machine(ram, gpu, disk), folder)


def test_32_gb_without_graphics_gets_gemma(tmp_path):
    # Processor only: Qwen3 4B ~20 tokens/s is enough for the mixture of experts.
    got = offer(20.0, 31 * GIB, folder=tmp_path)
    assert got.model is BEST_MODEL


def test_12_gb_card_with_16_gb_gets_ministral_14b(tmp_path):
    # RTX 3060: Qwen3 4B ~89 tokens/s on the card.
    got = offer(89.0, 15 * GIB, gpu=12 * GIB, folder=tmp_path)
    assert got.model is GPU_MODEL and got.expected > 30


def test_32_gb_and_a_card_get_the_best_model(tmp_path):
    assert offer(89.0, 31 * GIB, gpu=12 * GIB, folder=tmp_path).model is BEST_MODEL


def test_integrated_graphics_with_shared_memory_is_not_offered_the_14b(tmp_path):
    # Iris Xe reports the shared 15.5 GiB, but the speed test says no.
    got = offer(9.39, 15 * GIB, gpu=int(15.5 * GIB), folder=tmp_path)
    assert got is None


def test_16_gb_without_a_card_still_gets_ministral_8b(tmp_path):
    assert offer(14.0, 15 * GIB, folder=tmp_path).model is LARGE_MODEL


def test_a_full_disk_falls_back_to_a_smaller_model(tmp_path):
    disk = GPU_MODEL.size + upgrade.DISK_MARGIN + 1
    assert offer(89.0, 31 * GIB, gpu=12 * GIB, disk=disk, folder=tmp_path).model is GPU_MODEL
    assert offer(89.0, 31 * GIB, gpu=12 * GIB, disk=GIB, folder=tmp_path) is None


def present(folder, model):
    with (folder / model.filename).open("wb") as f:
        f.truncate(model.size)  # sparse: the size is what counts as downloaded


def test_nothing_is_offered_when_a_model_as_good_is_there(tmp_path):
    (tmp_path / GPU_MODEL.filename).write_bytes(b"")  # a broken file does not count
    assert offer(89.0, 15 * GIB, gpu=12 * GIB, folder=tmp_path).model is GPU_MODEL
    present(tmp_path, GPU_MODEL)
    assert offer(89.0, 15 * GIB, gpu=12 * GIB, folder=tmp_path) is None
    # A better model is still offered over a smaller one that is there.
    assert offer(89.0, 31 * GIB, gpu=12 * GIB, folder=tmp_path).model is BEST_MODEL


def test_the_best_model_present_is_started(tmp_path, monkeypatch):
    from gloxi import engine

    monkeypatch.setattr(engine, "listening", lambda url: False)
    monkeypatch.setattr(engine, "server_binary", lambda: "/bin/llama-server")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    folder = engine.models_dir()
    folder.mkdir(parents=True)
    for model in (DEFAULT_MODEL, LARGE_MODEL, GPU_MODEL):
        present(folder, model)
    assert engine.plan("http://127.0.0.1:8080/v1").model is GPU_MODEL


def test_cli_offers_the_best_model_and_downloads_it(tmp_path):
    fetched = []
    text = run_cli(tmp_path, "y", 89.0, fetched, ram=31 * GIB, gpu=12 * GIB)
    assert BEST_MODEL.name in text and fetched == [BEST_MODEL.filename]
    state = upgrade.load_state(tmp_path)
    assert state["answer"] == "yes" and state["offered"] == BEST_MODEL.filename


def test_an_old_answer_is_not_asked_again_for_a_bigger_model(tmp_path):
    upgrade.save_state(tmp_path, {"tokens_per_second": 89.0, "answer": "no"})
    assert upgrade.should_offer(tmp_path, Machine(31 * GIB, 12 * GIB, 100 * GIB)) is None
