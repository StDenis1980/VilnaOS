# SPDX-License-Identifier: GPL-3.0-or-later
"""Offer a larger model only where it will be fast enough.

Decision of 2026-11-04 (ADR 0008): the speed of Ministral 3 8B depends on the
processor and its power limit, not on the amount of RAM. On the test laptop
(i5-1340P, 15 GB, 20 W) it ran at 6 tokens per second for 20 seconds and then
at 4.6, so a RAM threshold alone would offer it where it is slow.

So every computer starts with Qwen3 4B. On computers with 16 GB or more,
Gloxi then measures once, for about a minute, how fast Qwen3 4B answers.
Decision of 2026-11-13 (ADR 0008): from that speed it estimates how fast
each larger model would be and offers the best one (``TIERS``) that fits in
the memory and on the disk and reaches ``LARGE_MIN_SPEED``. The person
decides; the answer is remembered and the question is not repeated. Nothing
about the person is measured or sent: the test asks the local model a fixed
question.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from gloxi import localkey
from gloxi.engine import (
    BEST_MODEL,
    DEFAULT_MODEL,
    GPU_MODEL,
    LARGE_MODEL,
    LARGE_MODEL_MIN_RAM,
    ModelFile,
    Plan,
    verified,
)

# ADR 0008: below this the larger model feels too slow in a chat.
LARGE_MIN_SPEED = 6.0  # tokens per second
# How much slower Ministral 3 8B is than Qwen3 4B on the same computer:
# 4.51 / 9.39 tokens per second, both measured with measure_speed in the
# Flatpak on Vulkan on 2026-11-04 (i5-1340P, Iris Xe). It equals the ratio of
# the file sizes (0.48): the speed follows the memory traffic.
LARGE_SPEED_FACTOR = 0.48
# Laptops run fast for the first ~25 seconds and then lower the clock to their
# sustained power limit; only answers that start after the warm-up count.
TEST_SECONDS = 60.0
WARMUP_SECONDS = 30.0

TEST_PROMPT = "Describe in five sentences how a computer stores files on a disk."
STATE_FILE = "speed-test.json"

GIB = 1024**3
# What the system and llama.cpp need beside a model file while it downloads.
DISK_MARGIN = 2 * GIB


@dataclass(frozen=True)
class Tier:
    """A larger model and what a computer needs for it."""

    model: ModelFile
    min_ram: int  # MemTotal
    min_gpu_memory: int  # the largest graphics llama.cpp sees; 0: none needed
    speed_factor: float  # its speed / the speed of Qwen3 4B on the same computer


# The best first; Gloxi offers the first one this computer passes.
# Speed factors: gloxi.upgrade.measure_speed on 2026-11-13 (i5-12600, 32 GB,
# RTX 3060 12 GB, llama.cpp b12359 Vulkan, started as Gloxi starts it).
TIERS = (
    # Gemma 4 on the processor alone: 16.4 tokens/s, Qwen3 4B 16.3 there (1.0);
    # with the card llama.cpp moves its experts to the processor by itself
    # (--fit): 30 tokens/s against 85 for Qwen3 4B, still far above the minimum.
    # 0.9 leaves a margin. On the processor it holds ~21 GB: "32 GB" computers
    # report ~31 GiB, and the desktop and the programs need the rest.
    Tier(BEST_MODEL, min_ram=28 * GIB, min_gpu_memory=0, speed_factor=0.9),
    # Ministral 3 14B is dense, too slow on the processor: only with a card that
    # holds it ("12 GB" cards report 11.6-12 GiB). 32.3 / 85.7 tokens/s.
    Tier(GPU_MODEL, min_ram=LARGE_MODEL_MIN_RAM, min_gpu_memory=11 * GIB, speed_factor=0.37),
    Tier(
        LARGE_MODEL, min_ram=LARGE_MODEL_MIN_RAM, min_gpu_memory=0, speed_factor=LARGE_SPEED_FACTOR
    ),
)
UPGRADE_MODELS = tuple(t.model for t in TIERS)


@dataclass(frozen=True)
class Machine:
    """What decides which model fits; None where it could not be read."""

    ram: int | None
    gpu_memory: int | None = None
    free_disk: int | None = None


@dataclass(frozen=True)
class Offer:
    tier: Tier
    expected: float  # tokens per second

    @property
    def model(self) -> ModelFile:
        return self.tier.model


def best_offer(tokens_per_second: float, machine: Machine, folder: Path) -> Offer | None:
    """The best larger model for this computer, or None.

    None also when a model at least as good is already in ``folder``.
    """
    return choose(tokens_per_second, machine, lambda m: verified(m, folder) is not None)


def choose(
    tokens_per_second: float, machine: Machine, present: Callable[[ModelFile], bool]
) -> Offer | None:
    if machine.ram is None:
        return None
    for tier in TIERS:
        if not passes(tier, tokens_per_second, machine):
            continue
        if present(tier.model):
            return None
        if machine.free_disk is not None and machine.free_disk < tier.model.size + DISK_MARGIN:
            continue
        return Offer(tier, tokens_per_second * tier.speed_factor)
    return None


def passes(tier: Tier, tokens_per_second: float | None, machine: Machine) -> bool:
    """The memory holds the model and the estimated speed is high enough."""
    if machine.ram is None or machine.ram < tier.min_ram or tokens_per_second is None:
        return False
    if tier.min_gpu_memory and (machine.gpu_memory or 0) < tier.min_gpu_memory:
        return False
    return tokens_per_second * tier.speed_factor >= LARGE_MIN_SPEED


def tier_of(model: ModelFile) -> Tier | None:
    return next((t for t in TIERS if t.model == model), None)


def gpu_memory() -> int | None:
    """The memory of the largest graphics llama.cpp can use; None if unknown."""
    from gloxi.check import list_devices  # gloxi.check imports this module

    gpus = list_devices()
    if not gpus:
        return None
    return max(g.memory for g in gpus)


def this_machine(folder: Path, ram: int | None) -> Machine:
    from gloxi.check import free_space

    return Machine(ram=ram, gpu_memory=gpu_memory(), free_disk=free_space(folder))


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
    client = client or httpx.Client(
        timeout=httpx.Timeout(120.0, connect=5.0), headers=localkey.headers_for(url)
    )
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
    if ram < LARGE_MODEL_MIN_RAM:
        return False
    if any(verified(m, p.folder) is not None for m in UPGRADE_MODELS):
        return False
    return "tokens_per_second" not in load_state(p.folder)


def record_result(models_folder: Path, result: SpeedResult) -> dict:
    state = load_state(models_folder)
    state.update(
        model=DEFAULT_MODEL.filename,
        tokens_per_second=round(result.tokens_per_second, 2),
    )
    save_state(models_folder, state)
    return state


def should_offer(models_folder: Path, machine: Machine) -> Offer | None:
    """What to offer: the test has run, the person has not answered yet and a
    larger model fits this computer."""
    state = load_state(models_folder)
    speed = state.get("tokens_per_second")
    if "answer" in state or not isinstance(speed, (int, float)):
        return None
    return best_offer(float(speed), machine, models_folder)


def record_answer(models_folder: Path, yes: bool, model: ModelFile | None = None) -> None:
    state = load_state(models_folder)
    state["answer"] = "yes" if yes else "no"
    if model is not None:
        state["offered"] = model.filename
    save_state(models_folder, state)


def record_answer_reset(models_folder: Path) -> None:
    """The download failed or was stopped: ask again next time."""
    state = load_state(models_folder)
    state.pop("answer", None)
    save_state(models_folder, state)
