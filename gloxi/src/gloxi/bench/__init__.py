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
import threading
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


# Built-in sets: the ten-language commands, and Ukrainian quality (2026-11-03:
# grammar, translation, explanations, facts, and whether a password is repeated).
BUILTIN_SETS = {"default": "tasks.toml", "uk-quality": "uk_quality.toml"}


def load_tasks(path: Path | str | None = None) -> list[Task]:
    """A tasks file, or a built-in set by name (``BUILTIN_SETS``)."""
    name = "default" if path is None else str(path)
    if name in BUILTIN_SETS and not Path(name).exists():
        res = resources.files("gloxi.bench").joinpath(BUILTIN_SETS[name])
        text = res.read_text(encoding="utf-8")
    else:
        text = Path(path).read_text(encoding="utf-8")
    tasks = [Task(**raw) for raw in tomllib.loads(text)["task"]]
    ids = [t.id for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("task ids must be unique")
    for t in tasks:
        unknown = set(t.checks) - set(CHECK_NAMES)
        if unknown:
            raise ValueError(f"{t.id}: unknown checks {sorted(unknown)}")
        if t.checks.get("lang", "uk") not in LANGUAGES:
            raise ValueError(f"{t.id}: unknown language {t.checks['lang']!r}")
    return tasks


# --- checks ---------------------------------------------------------------------

_CYRILLIC = re.compile(r"[а-яА-ЯіІїЇєЄґҐ]")
_LETTER = re.compile(r"[^\W\d_]")


def cyrillic_share(text: str) -> float:
    letters = _LETTER.findall(text)
    if not letters:
        return 0.0
    return len(_CYRILLIC.findall(text)) / len(letters)


# The ten main languages (ADR 0009). Only Ukrainian is written in Cyrillic, so
# for the others a rough guess from common words and letters is enough to tell
# "answered in Polish" from "fell back to English".
LANGUAGES = ("uk", "en", "es", "pl", "de", "fr", "it", "ro", "cs", "lt")

_WORDS = {
    "en": "yes the and is are you your to of it that with this can for not all would will",
    "es": "sí el la los las que es y en un una para por con tu tus puedes no del "
    "se todos puedo pero como esto",
    "pl": "i w nie się jest to na z że do jak czy możesz jeśli lub dla wszystkie "
    "twoje mogę ale co tak być",
    "de": "ja der die das und ist nicht ich sie du mit ein eine zu auf kann den "
    "alle deine dein aber wie auch",
    "fr": "oui le la les et est vous de des un une pas pour que dans avec je tous "
    "vos votre mais comme cela",
    "it": "sì il la che di e è non per un una sono con puoi del della gli tutti "
    "tuoi tuo posso ma come questo",
    "ro": "da și este în nu de la cu un o sunt pentru că care poți să sau toate "
    "tale pot dar cum acest",
    "cs": "ano a i je se na v že to není jsem pro jak můžete s by nebo všechny vaše "
    "mohu ale co tak být opravdu jsi už jen ještě",
    "lt": "ir yra kad ne su į tai kaip jūs galite man aš bet iš arba visi jūsų "
    "galiu nes taip būti kuris kuri gali jei bus tik dar šis šį šiuo norint",
}
_LETTERS = {  # letters that (among these ten) mostly belong to one language
    "es": "ñ¿¡",
    "pl": "łśźżń",
    "de": "äöüß",
    "fr": "çœêèû",
    "it": "ìò",
    "ro": "ășțşţ",
    "cs": "řůěť",
    "lt": "ėįųū",
}
_TOKEN = re.compile(r"[^\W\d_]+")


def guess_language(text: str) -> str | None:
    """One of LANGUAGES, or None when there is nothing to go by."""
    if cyrillic_share(text) >= 0.6:
        return "uk"
    low = text.lower()
    words = _TOKEN.findall(low)
    scores = {}
    for lang, common in _WORDS.items():
        vocab = set(common.split())
        scores[lang] = sum(w in vocab for w in words)
        scores[lang] += 2 * sum(low.count(ch) for ch in _LETTERS.get(lang, ""))
    best = max(scores, key=scores.get)
    if scores[best] == 0 or list(scores.values()).count(scores[best]) > 1:
        return None  # nothing to go by, or a tie
    return best


def _lang_ok(answer: str, lang: str) -> bool:
    if lang == "uk":
        # Russian letters in a "Ukrainian" answer mean the model mixed them up.
        return cyrillic_share(answer) >= 0.6 and not re.search(r"[ыэъёЫЭЪЁ]", answer)
    return cyrillic_share(answer) <= 0.1 and guess_language(answer) == lang


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
    if lang is not None and not _lang_ok(answer, lang):
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


class AnonPeak:
    """Samples a process's ``RssAnon`` (its own memory, without the mapped model
    file) while the tasks run, and keeps the highest value in MB.

    ``VmHWM`` also counts the model file mapped into memory, which the kernel
    can drop and re-read under pressure, so it overstates what the model
    needs: on the November run VmHWM said 6.5 GB where systemd's cgroup peak
    was about 4 GB.
    """

    def __init__(self, pid: int, *, proc: Path = Path("/proc"), interval: float = 0.5) -> None:
        self._path = proc / str(pid) / "status"
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.peak_mb: float | None = None

    def sample(self) -> None:
        try:
            status = self._path.read_text()
        except OSError:
            return
        m = re.search(r"^RssAnon:\s+(\d+) kB", status, re.M)
        if m:
            mb = round(int(m.group(1)) / 1024, 1)
            self.peak_mb = mb if self.peak_mb is None else max(self.peak_mb, mb)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self._interval)

    def __enter__(self) -> AnonPeak:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()
        self.sample()


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
        f"- Пам'ять сервера моделі (пік VmHWM, разом із файлом моделі): "
        f"{meta.get('server_peak_mb') or 'не виміряно'} МБ",
        f"- Власна пам'ять сервера (пік RssAnon, без файлу моделі): "
        f"{meta.get('server_anon_peak_mb') or 'не виміряно'} МБ",
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
