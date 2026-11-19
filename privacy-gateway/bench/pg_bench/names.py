# SPDX-License-Identifier: Apache-2.0
"""How well do small on-device models find names in free text?

Today the gateway hides a name only in a labelled field ("Ім'я: ..."). This
measures candidates for free text on synthetic sentences in ten languages
(ADR 0009), with the forms names take there (Ірині Бондар, Tomaszem, Jonui).

A name counts as found only when the predicted spans cover all of it: hiding
"Олені" but not "Коваль" still leaks the surname, so that is "partial".
Everything runs on the device; the LLM backend talks only to a local server.
"""

from __future__ import annotations

import json
import re
import statistics
import time
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib import resources
from urllib.parse import urlparse

from pg_bench.providers import Chat, ProviderError
from privacy_gateway import Kind, detect

Span = tuple[int, int]
Backend = Callable[[str], list[Span]]


@dataclass(frozen=True)
class Sentence:
    lang: str
    text: str
    names: tuple[str, ...]

    def gold(self) -> list[Span]:
        spans = []
        for name in self.names:
            start = self.text.find(name)
            if start < 0:
                raise ValueError(f"{name!r} is not in {self.text!r}")
            spans.append((start, start + len(name)))
        return spans


def load_sentences(langs: Iterable[str] | None = None) -> list[Sentence]:
    data = resources.files("pg_bench").joinpath("data/names.toml").read_bytes()
    wanted = set(langs) if langs else None
    return [
        Sentence(s["lang"], s["text"], tuple(s["names"]))
        for s in tomllib.loads(data.decode())["sentence"]
        if wanted is None or s["lang"] in wanted
    ]


# --- backends ----------------------------------------------------------------


def regex_backend() -> Backend:
    """The gateway as it is: names in labelled fields only. The baseline."""
    return lambda text: [(f.start, f.end) for f in detect(text, {Kind.NAME})]


LLM_PROMPT = (
    "List every person's name in the user's text exactly as it is written there, "
    "in the same form and case, including a first name or a surname alone. "
    "Do not list places, companies, rivers or ordinary words. "
    'Answer with a JSON array of strings only, for example ["Anna Nowak"], or [] if there are none.'
)


def spans_of(text: str, names: Iterable[str]) -> list[Span]:
    """Where each listed name occurs in ``text``; names that do not occur are dropped."""
    spans = []
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        spans += [(m.start(), m.end()) for m in re.finditer(re.escape(name.strip()), text)]
    return spans


def parse_names(answer: str) -> list[str]:
    """The JSON array in a model's answer, forgiving code fences and chatter."""
    start, end = answer.find("["), answer.rfind("]")
    if start < 0 or end < start:
        return []
    try:
        value = json.loads(answer[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def llm_backend(chat: Chat) -> Backend:
    return lambda text: spans_of(text, parse_names(chat(LLM_PROMPT, text)))


def require_local(url: str) -> str:
    host = urlparse(url).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(f"the name model must run on this device, not at {host!r}")
    return url


def gliner_backend(model_id: str, threshold: float = 0.5) -> Backend:  # pragma: no cover
    from gliner import GLiNER  # optional: uv pip install gliner

    model = GLiNER.from_pretrained(model_id)
    labels = ["person"]
    return lambda text: [
        (e["start"], e["end"]) for e in model.predict_entities(text, labels, threshold=threshold)
    ]


def spacy_backend(name: str) -> Backend:  # pragma: no cover
    import spacy  # optional: uv pip install spacy && python -m spacy download <name>

    nlp = spacy.load(name)
    return lambda text: [
        (e.start_char, e.end_char) for e in nlp(text).ents if e.label_ in ("PER", "PERSON")
    ]


# --- scoring -----------------------------------------------------------------


def _covered(gold: Span, predicted: list[Span], text: str) -> bool:
    """Do the predicted spans cover all of ``gold``? Spaces between them do not count."""
    start, end = gold
    for p_start, p_end in sorted(predicted):
        if p_start <= start < p_end:
            start = p_end
            while start < end and text[start].isspace():
                start += 1
        if start >= end:
            return True
    return start >= end


def _overlaps(a: Span, b: Span) -> bool:
    return a[0] < b[1] and b[0] < a[1]


@dataclass
class Row:
    lang: str
    names: int = 0
    found: int = 0
    partial: int = 0
    false_alarms: int = 0
    predicted: int = 0
    errors: int = 0
    seconds: list[float] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.found / self.names if self.names else 1.0

    @property
    def precision(self) -> float:
        good = self.predicted - self.false_alarms
        return good / self.predicted if self.predicted else 1.0


def evaluate(backend: Backend, sentences: list[Sentence]) -> dict[str, Row]:
    rows: dict[str, Row] = {}
    for s in sentences:
        row = rows.setdefault(s.lang, Row(s.lang))
        gold = s.gold()
        start = time.monotonic()
        try:
            predicted = [p for p in backend(s.text) if p[1] > p[0]]
        except ProviderError as e:
            row.errors += 1
            row.misses.append(f"error: {e}")
            predicted = []
        row.seconds.append(time.monotonic() - start)
        row.names += len(gold)
        row.predicted += len(predicted)
        for g, name in zip(gold, s.names, strict=True):
            if _covered(g, predicted, s.text):
                row.found += 1
            else:
                row.partial += any(_overlaps(g, p) for p in predicted)
                row.misses.append(f"пропущено «{name}»: {s.text}")
        for p in predicted:
            if not any(_overlaps(p, g) for g in gold):
                row.false_alarms += 1
                row.misses.append(f"зайве «{s.text[p[0] : p[1]]}»: {s.text}")
    return rows


def markdown(rows: dict[str, Row], meta: dict) -> str:
    lines = [
        "# Замір моделі імен",
        "",
        *(f"- {k}: {v}" for k, v in meta.items()),
        "",
        "| Мова | Імен | Знайдено повністю | Частково | Зайві | Повнота | Точність "
        "| Час (медіана) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    total = Row("усі")
    for row in rows.values():
        for attr in ("names", "found", "partial", "false_alarms", "predicted", "errors"):
            setattr(total, attr, getattr(total, attr) + getattr(row, attr))
        total.seconds += row.seconds
    for row in [*rows.values(), total]:
        median = statistics.median(row.seconds) if row.seconds else 0.0
        lines.append(
            f"| {row.lang} | {row.names} | {row.found} | {row.partial} | {row.false_alarms} | "
            f"{row.recall:.0%} | {row.precision:.0%} | {median:.2f} с |"
        )
    lines += ["", "## Помилки", ""]
    for row in rows.values():
        lines += [f"- {row.lang}: {m}" for m in row.misses]
    return "\n".join(lines)
