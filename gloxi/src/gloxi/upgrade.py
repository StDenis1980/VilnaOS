# SPDX-License-Identifier: GPL-3.0-or-later
"""Offer the larger model only where it will be fast enough.

Decision of 2026-11-04 (ADR 0008): the speed of Ministral 3 8B depends on the
processor and its power limit, not on the amount of RAM. On the test laptop
(i5-1340P, 15 GB, 20 W) it ran at 6 tokens per second for 20 seconds and then
at 4.6, so a RAM threshold alone would offer it where it is slow.

So every computer starts with Qwen3 4B. On computers with 16 GB or more,
Gloxi then measures once, for about 30 seconds, how fast Qwen3 4B answers,
estimates the speed of Ministral 3 8B from it, and offers that model only
if the estimate reaches ``LARGE_MIN_SPEED``. The person decides; the answer
is remembered and the question is not repeated. Nothing about the person is
measured or sent: the test asks the local model a fixed question.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from gloxi.engine import DEFAULT_MODEL, LARGE_MODEL, LARGE_MODEL_MIN_RAM, Plan, verified

# ADR 0008: below this the larger model feels too slow in a chat.
LARGE_MIN_SPEED = 6.0  # tokens per second
# How much slower Ministral 3 8B is than Qwen3 4B on the same computer, from
# the measurements of 2026-11-03 (3.8 / 5.8 tokens per second on the CPU).
# The size ratio of the files (0.48) would be the pessimistic bound.
LARGE_SPEED_FACTOR = 0.65
# Long enough for a laptop to reach its sustained power limit.
TEST_SECONDS = 30.0
WARMUP_SECONDS = 10.0

TEST_PROMPT = "Describe in five sentences how a computer stores files on a disk."
STATE_FILE = "speed-test.json"


@dataclass(frozen=True)
class SpeedResult:
    tokens_per_second: float  # sustained, after the warm-up

    @property
    def large_expected(self) -> float:
        return self.tokens_per_second * LARGE_SPEED_FACTOR

    @property
    def large_is_fast_enough(self) -> bool:
        return self.large_expected >= LARGE_MIN_SPEED


def measure_speed(
    url: str,
    *,
    seconds: float = TEST_SECONDS,
    warmup: float = WARMUP_SECONDS,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> SpeedResult:
    """Ask the local model the same short question until ``seconds`` have passed.

    The speed counts only the answers that started after ``warmup``: laptops
    run fast for the first seconds and then lower their clock.
    """
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=5.0))
    body = {
        "messages": [{"role": "user", "content": TEST_PROMPT}],
        "max_tokens": 64,
        "temperature": 0,
        "stream": False,
    }
    start = clock()
    tokens = spent = 0.0
    all_tokens = all_spent = 0.0
    try:
        while clock() - start < seconds:
            began = clock()
            response = client.post(url.rstrip("/") + "/chat/completions", json=body)
            response.raise_for_status()
            took = clock() - began
            count = _completion_tokens(response.json())
            all_tokens, all_spent = all_tokens + count, all_spent + took
            if began - start >= warmup:
                tokens, spent = tokens + count, spent + took
    finally:
        if own:
            client.close()
    if spent <= 0:  # a very slow computer: no answer started after the warm-up
        tokens, spent = all_tokens, all_spent
    return SpeedResult(tokens / spent if spent > 0 else 0.0)


def _completion_tokens(data: dict) -> int:
    usage = data.get("usage") or {}
    if usage.get("completion_tokens"):
        return int(usage["completion_tokens"])
    return int((data.get("timings") or {}).get("predicted_n") or 0)


# -- what was measured and answered, kept next to the models ------------------------


def state_path(models_folder: Path) -> Path:
    return models_folder / STATE_FILE


def load_state(models_folder: Path) -> dict:
    try:
        return json.loads(state_path(models_folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(models_folder: Path, state: dict) -> None:
    models_folder.mkdir(parents=True, exist_ok=True)
    path = state_path(models_folder)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def needs_test(p: Plan, ram: int | None) -> bool:
    """Run the test once, on 16 GB computers that use Qwen3 4B and have no answer yet."""
    if p.folder is None or p.model is not DEFAULT_MODEL or ram is None:
        return False
    if ram < LARGE_MODEL_MIN_RAM or verified(LARGE_MODEL, p.folder) is not None:
        return False
    return "tokens_per_second" not in load_state(p.folder)


def record_result(models_folder: Path, result: SpeedResult) -> dict:
    state = load_state(models_folder)
    state.update(
        model=DEFAULT_MODEL.filename,
        tokens_per_second=round(result.tokens_per_second, 2),
        large_expected=round(result.large_expected, 2),
        offer_large=result.large_is_fast_enough,
    )
    save_state(models_folder, state)
    return state


def should_offer(models_folder: Path) -> bool:
    """The test said yes and the person has not answered yet."""
    state = load_state(models_folder)
    return (
        bool(state.get("offer_large"))
        and "answer" not in state
        and verified(LARGE_MODEL, models_folder) is None
    )


def record_answer(models_folder: Path, yes: bool) -> None:
    state = load_state(models_folder)
    state["answer"] = "yes" if yes else "no"
    save_state(models_folder, state)


def record_answer_reset(models_folder: Path) -> None:
    """The download failed or was stopped: ask again next time."""
    state = load_state(models_folder)
    state.pop("answer", None)
    save_state(models_folder, state)
