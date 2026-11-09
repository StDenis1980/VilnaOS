# SPDX-License-Identifier: Apache-2.0
"""Which placeholder format do cloud models give back unchanged?

Each synthetic task is masked with :class:`privacy_gateway.Masker`, its
placeholders are rewritten into one of several formats, and the text is sent
to each model with and without a short instruction about placeholders. For
every placeholder in the answer we record whether it came back exactly, came
back changed but was still restored by ``unmask``, came back beyond repair, or
did not come back at all.
"""

from __future__ import annotations

import re
import time
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from importlib import resources

from pg_bench.providers import Chat, ProviderError
from privacy_gateway import PLACEHOLDER_HINT, Masker
from privacy_gateway.masking import PLACEHOLDER_RE


@dataclass(frozen=True)
class Format:
    name: str
    open: str
    close: str

    def write(self, kind: str, n: int | str) -> str:
        return f"{self.open}{kind}_{n}{self.close}"

    def loose(self) -> re.Pattern[str]:
        """This format with the small changes models make: case, separator, spaces."""
        return re.compile(
            re.escape(self.open)
            + r"\s*(?P<kind>[^\W\d_]{2,}(?:[ _-][^\W\d_]+)*)[ _-]*(?P<n>\d{1,4})\s*"
            + re.escape(self.close)
        )


FORMATS = {
    f.name: f
    for f in (
        Format("square", "[", "]"),  # what the gateway uses today: [EMAIL_1]
        Format("double", "[[", "]]"),
        Format("curly", "{{", "}}"),
        Format("angle", "<", ">"),
        Format("white", "⟦", "⟧"),
    )
}

PROMPTS = {
    "none": "You are a helpful assistant.",
    "hint": "You are a helpful assistant. " + PLACEHOLDER_HINT,
}


@dataclass(frozen=True)
class Task:
    id: str
    lang: str
    keep: str
    instruction: str
    text: str


def load_tasks(only: str | None = None) -> list[Task]:
    data = resources.files("pg_bench").joinpath("data/placeholder_tasks.toml").read_bytes()
    tasks = [Task(**t) for t in tomllib.loads(data.decode())["task"]]
    return [t for t in tasks if only is None or only in t.id]


@dataclass
class Outcome:
    """What happened to one placeholder."""

    placeholder: str
    exact: int = 0  # times it came back exactly as sent
    restored: bool = False  # its real value is in the unmasked answer
    status: str = "missing"  # exact | changed | broken | missing


@dataclass
class Result:
    target: str
    task: str
    lang: str
    format: str
    prompt: str
    keep: str
    seconds: float = 0.0
    error: str | None = None
    outcomes: list[Outcome] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    answer: str = ""

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        bad = {"broken"} | ({"missing"} if self.keep == "all" else set())
        return not self.unresolved and all(o.status not in bad for o in self.outcomes)


def prepare(task: Task, fmt: Format) -> tuple[Masker, str, dict[str, str]]:
    """Mask ``task`` and rewrite its placeholders into ``fmt``.

    Returns the masker, the message to send, and the sent form of each of the
    masker's own placeholders.
    """
    masker = Masker()
    masked = masker.mask(task.text)
    sent: dict[str, str] = {}

    def rewrite(m: re.Match) -> str:
        sent[m.group(0)] = fmt.write(m.group(1), m.group(2))
        return sent[m.group(0)]

    body = PLACEHOLDER_RE.sub(rewrite, masked)
    return masker, f"{task.instruction}\n\n{body}", sent


def _exactly(fmt: Format, form: str) -> re.Pattern[str]:
    """``form`` itself, not inside a longer bracket: [EMAIL_1] is not in [[EMAIL_1]]."""
    return re.compile(
        f"(?<!{re.escape(fmt.open[0])}){re.escape(form)}(?!{re.escape(fmt.close[-1])})"
    )


def score(masker: Masker, fmt: Format, sent: dict[str, str], answer: str) -> tuple:
    """Outcomes per placeholder, and what ``unresolved`` reports after unmasking."""
    # Bring this format back to the gateway's own form, then use its usual unmask,
    # which also forgives small changes ([Email_1], [EMIAL_1], EMAIL_1).
    canonical = answer
    if fmt.name not in ("square", "double"):
        canonical = fmt.loose().sub(lambda m: f"[{m.group('kind')}_{m.group('n')}]", answer)
    unmasked = masker.unmask(canonical)
    values = masker._value_of  # noqa: SLF001 - the measurement needs to look inside
    outcomes = []
    for ours, form in sent.items():
        exact = len(_exactly(fmt, form).findall(answer))
        o = Outcome(ours, exact=exact, restored=values[ours] in unmasked)
        if o.exact and o.restored:
            o.status = "exact"
        elif o.restored:
            o.status = "changed"
        outcomes.append(o)
    unresolved = masker.unresolved(unmasked)
    for o in outcomes:
        if o.status == "missing" and unresolved:
            o.status = "broken"  # something placeholder-like came back but matched nothing
    return outcomes, unresolved


def run(
    targets: Iterable[tuple[str, Chat]],
    tasks: list[Task],
    formats: Iterable[str] = FORMATS,
    prompts: Iterable[str] = PROMPTS,
    repeat: int = 1,
    progress: Callable[[Result], None] | None = None,
) -> list[Result]:
    formats, prompts = list(formats), list(prompts)
    results = []
    for name, chat in targets:
        for task in tasks:
            for fmt_name in formats:
                fmt = FORMATS[fmt_name]
                for prompt in prompts:
                    for _ in range(repeat):
                        masker, message, sent = prepare(task, fmt)
                        system = PROMPTS[prompt].format(example=fmt.write("EMAIL", 1))
                        r = Result(name, task.id, task.lang, fmt.name, prompt, task.keep)
                        start = time.monotonic()
                        try:
                            r.answer = chat(system, message)
                        except ProviderError as e:
                            r.error = str(e)
                        r.seconds = round(time.monotonic() - start, 2)
                        if r.error is None:
                            r.outcomes, r.unresolved = score(masker, fmt, sent, r.answer)
                        results.append(r)
                        if progress:
                            progress(r)
    return results


def summary(results: list[Result]) -> list[dict]:
    """One row per model, format and prompt."""
    rows: dict[tuple[str, str, str], dict] = {}
    for r in results:
        row = rows.setdefault(
            (r.target, r.format, r.prompt),
            {
                "target": r.target,
                "format": r.format,
                "prompt": r.prompt,
                "runs": 0,
                "passed": 0,
                "errors": 0,
                "exact": 0,
                "changed": 0,
                "broken": 0,
                "missing": 0,
            },
        )
        row["runs"] += 1
        row["passed"] += r.passed
        row["errors"] += r.error is not None
        for o in r.outcomes:
            row[o.status] += 1
    return list(rows.values())


def markdown(results: list[Result], meta: dict) -> str:
    lines = [
        "# Замір формату замінників",
        "",
        *(f"- {k}: {v}" for k, v in meta.items()),
        "",
        "Точно — замінник повернувся без змін; змінено — модель його змінила, але значення "
        "відновилося; зламано — щось схоже на замінник, але значення не відновилося; "
        "немає — замінника у відповіді немає (для завдань «some» це не провал).",
        "",
        "| Модель | Формат | Підказка | Пройдено | Точно | Змінено | Зламано | Немає | Помилки |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary(results):
        lines.append(
            f"| {row['target']} | {row['format']} | {row['prompt']} | "
            f"{row['passed']}/{row['runs']} | {row['exact']} | {row['changed']} | "
            f"{row['broken']} | {row['missing']} | {row['errors']} |"
        )
    lines += ["", "## Що пішло не так", ""]
    for r in results:
        if r.passed:
            continue
        what = r.error or ", ".join(
            f"{o.placeholder}: {o.status}" for o in r.outcomes if o.status != "exact"
        )
        if r.unresolved:
            what += f"; не відновилося: {', '.join(r.unresolved)}"
        lines += [f"### {r.target} · {r.task} · {r.format} · {r.prompt}", "", what, ""]
        if r.answer:
            lines += ["```text", r.answer.strip(), "```", ""]
    return "\n".join(lines)


def to_json(results: list[Result]) -> list[dict]:
    out = []
    for r in results:
        d = asdict(r)
        d["passed"] = r.passed
        out.append(d)
    return out
