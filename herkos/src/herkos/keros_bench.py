# SPDX-License-Identifier: GPL-3.0-or-later
"""``herkos keros-bench``: how well the rules and the local model catch injections.

The corpus (``data/keros-corpus.jsonl``) holds short made-up texts in the ten
languages of ADR 0009 and Russian: injections (direct, paraphrased, hidden,
inside an e-mail) and clean texts, many of them on purpose close to an
injection (manuals that say "ignore the beep", articles about attacks, a
colleague asking to forward a file). No real people or data are in it.

Each text is checked by the rules and, separately, by the model, so the two
can be compared; in Herkos the model only runs when the rules saw nothing.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TextIO

from herkos.keros import scan
from herkos.keros_model import ModelCheck


@dataclass(frozen=True)
class Sample:
    id: str
    lang: str
    label: str  # "injection" | "clean"
    kind: str
    text: str


def load_corpus(path: Path | None = None) -> list[Sample]:
    if path is None:
        raw = resources.files("herkos").joinpath("data/keros-corpus.jsonl").read_text("utf-8")
    else:
        raw = path.read_text("utf-8")
    out = []
    for n, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("label") not in ("injection", "clean"):
            raise ValueError(f"line {n}: label must be injection or clean")
        out.append(Sample(d["id"], d["lang"], d["label"], d.get("kind", ""), d["text"]))
    return out


@dataclass
class Row:
    sample: Sample
    rules: bool
    model: bool | None  # None: model off or unavailable
    seconds: float = 0.0

    @property
    def combined(self) -> bool:
        return self.rules or bool(self.model)


def run(samples: list[Sample], model: ModelCheck | None) -> list[Row]:
    rows = []
    for s in samples:
        rules = bool(scan(s.text))
        verdict: bool | None = None
        seconds = 0.0
        if model is not None:
            report = model.check(s.text)
            seconds = report.seconds
            if report.status in ("injection", "clean"):
                verdict = report.status == "injection"
        rows.append(Row(s, rules, verdict, seconds))
    return rows


def _rate(hits: int, n: int) -> str:
    return f"{hits}/{n}" if n else "-"


def report(rows: list[Row], out: TextIO = sys.stdout) -> None:
    by_lang: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_lang[r.sample.lang].append(r)
    with_model = any(r.model is not None for r in rows)

    def line(name: str, group: list[Row]) -> str:
        inj = [r for r in group if r.sample.label == "injection"]
        cln = [r for r in group if r.sample.label == "clean"]
        cols = [name, _rate(sum(r.rules for r in inj), len(inj))]
        if with_model:
            cols += [
                _rate(sum(bool(r.model) for r in inj), len(inj)),
                _rate(sum(r.combined for r in inj), len(inj)),
            ]
        cols.append(_rate(sum(r.rules for r in cln), len(cln)))
        if with_model:
            cols += [
                _rate(sum(bool(r.model) for r in cln), len(cln)),
                _rate(sum(r.combined for r in cln), len(cln)),
            ]
        return "| " + " | ".join(cols) + " |"

    head = ["lang", "caught: rules"]
    if with_model:
        head += ["model", "together"]
    head.append("false alarms: rules")
    if with_model:
        head += ["model", "together"]
    print("| " + " | ".join(head) + " |", file=out)
    print("|" + " --- |" * len(head), file=out)
    for lang in sorted(by_lang):
        print(line(lang, by_lang[lang]), file=out)
    print(line("all", rows), file=out)

    if with_model:
        times = [r.seconds for r in rows if r.model is not None]
        down = sum(r.model is None for r in rows)
        if times:
            print(
                f"\nmodel time per text: median {statistics.median(times):.1f} s, "
                f"max {max(times):.1f} s",
                file=out,
            )
        if down:
            print(f"model gave no answer for {down} texts", file=out)

    missed = [r.sample.id for r in rows if r.sample.label == "injection" and not r.combined]
    alarms = [r.sample.id for r in rows if r.sample.label == "clean" and r.combined]
    print(f"\nmissed: {', '.join(missed) or 'none'}", file=out)
    print(f"false alarms: {', '.join(alarms) or 'none'}", file=out)
