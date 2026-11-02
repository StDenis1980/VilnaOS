# SPDX-License-Identifier: GPL-3.0-or-later
"""Measure a local model on Gloxi's test commands (roadmap 2.4).

Runs every task in ``tasks.toml`` against an OpenAI-compatible endpoint
(llama-server by default), times each answer, checks it automatically and
writes a report. Nothing is sent anywhere else: point it at a model on this
computer.

    gloxi-bench --model qwen3-1.7b --server-pid $(pgrep -f llama-server)

With ``--server-pid`` the report also has the peak memory of the model
server (``VmHWM`` from ``/proc``), which is what decides 8 vs 16 GB.
"""

from __future__ import annotations

import json
import re
import time
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from statistics import median

from gloxi.config import DEFAULT_SYSTEM_PROMPT
from gloxi.llm import ChatClient, Message, ModelError

CHECK_NAMES = (
    "lang",
    "contains_any",
    "contains_all",
    "not_contains",
    "regex",
    "json_keys",
    "max_chars",
)


@dataclass(frozen=True)
class Task:
    id: str
    category: str
    prompt: str
    system: str | None = None
    checks: dict = field(default_factory=dict)


@dataclass
class Result:
    id: str
    category: str
    answer: str
    passed: bool
    failed_checks: list[str]
    first_token_s: float | None
    total_s: float
    chars_per_s: float
    error: str | None = None


def load_tasks(path: Path | None = None) -> list[Task]:
    if path is None:
        text = resources.files("gloxi.bench").joinpath("tasks.toml").read_text(encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    tasks = [Task(**raw) for raw in tomllib.loads(text)["task"]]
    ids = [t.id for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("task ids must be unique")
    for t in tasks:
        unknown = set(t.checks) - set(CHECK_NAMES)
        if unknown:
            raise ValueError(f"{t.id}: unknown checks {sorted(unknown)}")
    return tasks


# --- checks ---------------------------------------------------------------------

_CYRILLIC = re.compile(r"[а-яА-ЯіІїЇєЄґҐ]")
_LETTER = re.compile(r"[^\W\d_]")


def cyrillic_share(text: str) -> float:
    letters = _LETTER.findall(text)
    if not letters:
        return 0.0
    return len(_CYRILLIC.findall(text)) / len(letters)


def _json_object(text: str) -> dict | None:
    """The JSON object in an answer, tolerating a ```json fence around it."""
    stripped = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.S)
    if fence:
        stripped = fence.group(1)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def run_checks(answer: str, checks: dict) -> list[str]:
    """Names of the checks that failed (empty list: the answer passed)."""
    low = answer.lower()
    failed = []
    lang = checks.get("lang")
    if lang == "uk" and cyrillic_share(answer) < 0.6:
        failed.append("lang")
    if lang == "en" and cyrillic_share(answer) > 0.1:
        failed.append("lang")
    if "contains_any" in checks and not any(s.lower() in low for s in checks["contains_any"]):
        failed.append("contains_any")
    if "contains_all" in checks and not all(s.lower() in low for s in checks["contains_all"]):
        failed.append("contains_all")
    if "not_contains" in checks and any(s.lower() in low for s in checks["not_contains"]):
        failed.append("not_contains")
    if "regex" in checks and not re.search(checks["regex"], answer):
        failed.append("regex")
    if "json_keys" in checks:
        obj = _json_object(answer)
        if obj is None or not all(k in obj for k in checks["json_keys"]):
            failed.append("json_keys")
    if "max_chars" in checks and len(answer.strip()) > checks["max_chars"]:
        failed.append("max_chars")
    return failed


# --- running ----------------------------------------------------------------------


def run_task(
    client: ChatClient, task: Task, *, clock: Callable[[], float] = time.monotonic
) -> Result:
    messages: list[Message] = [
        {"role": "system", "content": task.system or DEFAULT_SYSTEM_PROMPT},
        {"role": "user", "content": task.prompt},
    ]
    start = clock()
    first: float | None = None
    parts: list[str] = []
    error = None
    try:
        for piece in client.stream(messages):
            if first is None:
                first = clock() - start
            parts.append(piece)
    except ModelError as exc:
        error = str(exc)
    total = clock() - start
    answer = "".join(parts)
    failed = ["error"] if error else run_checks(answer, task.checks)
    gen_time = total - (first or 0.0)
    return Result(
        id=task.id,
        category=task.category,
        answer=answer,
        passed=not failed,
        failed_checks=failed,
        first_token_s=round(first, 3) if first is not None else None,
        total_s=round(total, 3),
        chars_per_s=round(len(answer) / gen_time, 1) if gen_time > 0 else 0.0,
        error=error,
    )


def peak_rss_mb(pid: int, proc: Path = Path("/proc")) -> float | None:
    """Peak resident memory of a process in MB (Linux ``VmHWM``)."""
    try:
        status = (proc / str(pid) / "status").read_text()
    except OSError:
        return None
    m = re.search(r"^VmHWM:\s+(\d+) kB", status, re.M)
    return round(int(m.group(1)) / 1024, 1) if m else None


def machine_info(proc: Path = Path("/proc")) -> dict:
    info: dict = {}
    try:
        meminfo = (proc / "meminfo").read_text()
        m = re.search(r"^MemTotal:\s+(\d+) kB", meminfo, re.M)
        if m:
            info["ram_gb"] = round(int(m.group(1)) / 1024 / 1024, 1)
    except OSError:
        pass
    try:
        cpuinfo = (proc / "cpuinfo").read_text()
        m = re.search(r"^model name\s*:\s*(.+)$", cpuinfo, re.M)
        if m:
            info["cpu"] = m.group(1).strip()
        info["cpu_threads"] = len(re.findall(r"^processor\s*:", cpuinfo, re.M))
    except OSError:
        pass
    return info


# --- report -----------------------------------------------------------------------


def summarise(results: Iterable[Result]) -> dict:
    results = list(results)
    by_cat: dict[str, list[Result]] = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r)
    ok = [r for r in results if r.error is None]
    firsts = [r.first_token_s for r in ok if r.first_token_s is not None]
    return {
        "tasks": len(results),
        "passed": sum(r.passed for r in results),
        "errors": len(results) - len(ok),
        "median_first_token_s": round(median(firsts), 2) if firsts else None,
        "median_total_s": round(median(r.total_s for r in ok), 2) if ok else None,
        "median_chars_per_s": round(median(r.chars_per_s for r in ok), 1) if ok else None,
        "by_category": {c: [sum(r.passed for r in rs), len(rs)] for c, rs in by_cat.items()},
    }


def markdown_report(meta: dict, summary: dict, results: list[Result]) -> str:
    lines = [
        f"# Заміри: {meta.get('model', '?')}",
        "",
        f"- Дата: {meta.get('date', '?')}",
        f"- Машина: {meta.get('cpu', '?')}, {meta.get('cpu_threads', '?')} потоків, "
        f"{meta.get('ram_gb', '?')} ГБ RAM",
        f"- Пам'ять сервера моделі (пік): {meta.get('server_peak_mb') or 'не виміряно'} МБ",
        f"- Пройдено: **{summary['passed']} з {summary['tasks']}**, помилок: {summary['errors']}",
        f"- Медіана: перший токен {summary['median_first_token_s']} с, уся відповідь "
        f"{summary['median_total_s']} с, {summary['median_chars_per_s']} символів/с",
        "",
        "| Категорія | Пройдено |",
        "| --- | --- |",
    ]
    for cat, (passed, total) in summary["by_category"].items():
        lines.append(f"| {cat} | {passed} з {total} |")
    lines += [
        "",
        "| Завдання | Результат | Перший токен, с | Усього, с | Відповідь |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in results:
        verdict = "так" if r.passed else "ні: " + ", ".join(r.failed_checks)
        answer = (r.error or r.answer).replace("\n", " ").replace("|", "\\|").strip()
        if len(answer) > 120:
            answer = answer[:117] + "..."
        lines.append(
            f"| {r.id} | {verdict} | {r.first_token_s if r.first_token_s is not None else '—'} "
            f"| {r.total_s} | {answer} |"
        )
    return "\n".join(lines) + "\n"


def write_results(out_dir: Path, meta: dict, summary: dict, results: list[Result]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w.-]+", "_", f"{meta.get('model', 'model')}-{meta.get('date', '')}")
    data = {"meta": meta, "summary": summary, "results": [asdict(r) for r in results]}
    (out_dir / f"{stem}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = out_dir / f"{stem}.md"
    report.write_text(markdown_report(meta, summary, results), encoding="utf-8")
    return report
