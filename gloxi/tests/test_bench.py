# SPDX-License-Identifier: GPL-3.0-or-later
import json
from collections import Counter

import httpx
import pytest
from fake_server import FakeLlamaServer
from helpers import LOCAL

from gloxi.bench import (
    Task,
    cyrillic_share,
    load_tasks,
    markdown_report,
    peak_rss_mb,
    run_checks,
    run_task,
    summarise,
    write_results,
)
from gloxi.bench.__main__ import main
from gloxi.llm import ChatClient


def test_builtin_set_has_about_thirty_tasks_in_all_categories():
    tasks = load_tasks()
    assert 28 <= len(tasks) <= 32
    cats = Counter(t.category for t in tasks)
    assert set(cats) == {
        "розмова",
        "довідка",
        "текст",
        "структура",
        "маршрут",
        "встановлення",
        "безпека",
    }
    assert all(t.checks for t in tasks)


def test_unknown_check_is_rejected(tmp_path):
    path = tmp_path / "t.toml"
    path.write_text('[[task]]\nid="a"\ncategory="x"\nprompt="p"\nchecks={ langg = "uk" }\n')
    with pytest.raises(ValueError, match="langg"):
        load_tasks(path)


@pytest.mark.parametrize(
    ("answer", "checks", "failed"),
    [
        ("Вільнюс", {"contains_any": ["Вільнюс", "Vilnius"], "max_chars": 60}, []),
        ("Ризька", {"contains_any": ["Вільнюс"]}, ["contains_any"]),
        ("Привіт, я Gloxi, асистент.", {"lang": "uk"}, []),
        ("Hello, I am Gloxi.", {"lang": "uk"}, ["lang"]),
        ("Привіт", {"lang": "en"}, ["lang"]),
        ('```json\n{"tool": "files.search", "args": {}}\n```', {"json_keys": ["tool", "args"]}, []),
        ('Ось: {"tool": "x"}', {"json_keys": ["tool"]}, ["json_keys"]),
        ("Відповідь: 36.", {"regex": r"\b36\b"}, []),
        ("ЗЛАМАНО", {"not_contains": ["зламано"]}, ["not_contains"]),
        ("a" * 61, {"max_chars": 60}, ["max_chars"]),
        ("save to Documents", {"contains_all": ["save", "documents"]}, []),
    ],
)
def test_checks(answer, checks, failed):
    assert run_checks(answer, checks) == failed


def test_cyrillic_share_ignores_digits_and_punctuation():
    assert cyrillic_share("Gloxi — асистент 2026!") == pytest.approx(8 / 13)
    assert cyrillic_share("123") == 0.0


def test_run_task_times_and_checks():
    server = FakeLlamaServer("Вільнюс")
    client = ChatClient(LOCAL, transport=server.transport())
    ticks = iter([0.0, 0.5, 0.7])
    task = Task("cap", "розмова", "Столиця Литви?", checks={"contains_any": ["Вільнюс"]})
    r = run_task(client, task, clock=lambda: next(ticks))
    assert r.passed and r.answer == "Вільнюс"
    assert r.first_token_s == 0.5
    assert r.total_s == 0.7
    sent = server.requests[0]["messages"]
    assert sent[0]["role"] == "system" and sent[1]["content"] == "Столиця Литви?"


def test_run_task_uses_its_own_system_prompt():
    server = FakeLlamaServer('{"tool": null, "args": {}}')
    client = ChatClient(LOCAL, transport=server.transport())
    task = Task("j", "структура", "анекдот", system="JSON only", checks={"json_keys": ["tool"]})
    assert run_task(client, task).passed
    assert server.requests[0]["messages"][0]["content"] == "JSON only"


def test_run_task_records_model_errors():
    client = ChatClient(LOCAL, transport=FakeLlamaServer(status=500).transport())
    r = run_task(client, Task("x", "розмова", "hi", checks={"lang": "en"}))
    assert not r.passed and r.failed_checks == ["error"] and "HTTP 500" in r.error


def test_peak_rss_reads_vmhwm(tmp_path):
    (tmp_path / "42").mkdir()
    (tmp_path / "42" / "status").write_text("Name:\tllama-server\nVmHWM:\t 2097152 kB\n")
    assert peak_rss_mb(42, proc=tmp_path) == 2048.0
    assert peak_rss_mb(7, proc=tmp_path) is None


def test_report_and_files(tmp_path):
    client = ChatClient(LOCAL, transport=FakeLlamaServer("Вільнюс | ok").transport())
    tasks = [
        Task("a", "розмова", "?", checks={"contains_any": ["Вільнюс"]}),
        Task("b", "текст", "?", checks={"lang": "en"}),
    ]
    results = [run_task(client, t) for t in tasks]
    summary = summarise(results)
    assert summary["passed"] == 1
    assert summary["by_category"] == {"розмова": [1, 1], "текст": [0, 1]}
    meta = {"model": "test/model:1b", "date": "2026-11-02_1700", "ram_gb": 8.0}
    report = write_results(tmp_path, meta, summary, results)
    text = report.read_text()
    assert "Пройдено: **1 з 2**" in text
    assert "Вільнюс \\| ok" in text  # table cells stay intact
    data = json.loads(report.with_suffix(".json").read_text())
    assert data["results"][1]["failed_checks"] == ["lang"]
    assert "8.0 ГБ RAM" in markdown_report(meta, summary, results)


def test_cli_runs_selected_tasks(tmp_path, monkeypatch, capsys):
    server = FakeLlamaServer("Вільнюс")
    real_client = httpx.Client

    def client_with_fake(*args, **kwargs):
        kwargs["transport"] = server.transport()
        return real_client(*args, **kwargs)

    monkeypatch.setattr("gloxi.llm.httpx.Client", client_with_fake)
    code = main(["--only", "fact-uk-capital", "--out", str(tmp_path), "--model", "fake"])
    assert code == 0
    assert "Пройдено: **1 з 1**" in capsys.readouterr().out
    assert len(server.requests) == 2  # warm-up plus the task
    assert list(tmp_path.glob("fake-*.md"))
