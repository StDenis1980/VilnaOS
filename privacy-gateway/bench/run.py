# SPDX-License-Identifier: Apache-2.0
"""Run the privacy gateway measurements. See bench/README.md.

uv run python bench/run.py placeholders --model anthropic:claude-haiku-4-5
uv run python bench/run.py names --backend regex --backend llm:qwen3-4b
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pg_bench import names, placeholders  # noqa: E402
from pg_bench.providers import ProviderError, Target, client  # noqa: E402


def _out(directory: str, stem: str) -> Path:
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{stem}-{dt.datetime.now():%Y-%m-%d_%H%M}"


def cmd_placeholders(args: argparse.Namespace) -> int:
    targets = [Target.parse(spec) for spec in args.model]
    try:
        chats = [(str(t), client(t, args.local_url)) for t in targets]
    except ProviderError as e:
        print(e, file=sys.stderr)
        return 2
    tasks = placeholders.load_tasks(args.only)

    def progress(r: placeholders.Result) -> None:
        mark = "ok" if r.passed else ("ERR" if r.error else "--")
        print(f"{mark:3} {r.target} {r.task} {r.format} {r.prompt} {r.seconds}s", flush=True)

    results = placeholders.run(
        chats,
        tasks,
        args.format or placeholders.FORMATS,
        args.prompt or placeholders.PROMPTS,
        args.repeat,
        progress,
    )
    meta = {
        "Дата": f"{dt.datetime.now():%Y-%m-%d %H:%M}",
        "Моделі": ", ".join(str(t) for t in targets),
        "Завдань": len(tasks),
        "Повторів": args.repeat,
    }
    stem = _out(args.out, "placeholders")
    stem.with_suffix(".md").write_text(placeholders.markdown(results, meta), encoding="utf-8")
    stem.with_suffix(".json").write_text(
        json.dumps(
            {"meta": meta, "results": placeholders.to_json(results)}, ensure_ascii=False, indent=1
        ),
        encoding="utf-8",
    )
    print(f"\n{stem}.md")
    return 0


def _backend(spec: str, args: argparse.Namespace) -> names.Backend:
    kind, _, arg = spec.partition(":")
    if kind == "regex":
        return names.regex_backend()
    if kind == "llm":
        url = names.require_local(args.local_url)
        return names.llm_backend(client(Target("local", arg or "local"), url))
    if kind == "gliner":
        return names.gliner_backend(arg or "urchade/gliner_multi_pii-v1", args.threshold)
    if kind == "spacy":
        return names.spacy_backend(arg or "xx_ent_wiki_sm")
    raise SystemExit(f"unknown backend {spec!r}: regex, llm:<model>, gliner:<id>, spacy:<model>")


def cmd_names(args: argparse.Namespace) -> int:
    sentences = names.load_sentences(args.lang)
    for spec in args.backend:
        rows = names.evaluate(_backend(spec, args), sentences)
        meta = {
            "Дата": f"{dt.datetime.now():%Y-%m-%d %H:%M}",
            "Модель": spec,
            "Речень": len(sentences),
            "Машина": f"{platform.node()} · {platform.processor() or platform.machine()}",
        }
        report = names.markdown(rows, meta)
        stem = _out(args.out, "names-" + spec.replace(":", "-").replace("/", "_"))
        stem.with_suffix(".md").write_text(report, encoding="utf-8")
        print(report.split("\n## ")[0], f"\n{stem}.md\n", sep="\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="bench-results")
    parser.add_argument("--local-url", default="http://127.0.0.1:8080/v1")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("placeholders", help="which placeholder format models keep")
    p.add_argument("--model", action="append", required=True, help="provider:model, repeatable")
    p.add_argument("--format", action="append", choices=list(placeholders.FORMATS))
    p.add_argument("--prompt", action="append", choices=list(placeholders.PROMPTS))
    p.add_argument("--only", help="only tasks whose id contains this")
    p.add_argument("--repeat", type=int, default=1)
    p.set_defaults(func=cmd_placeholders)

    n = sub.add_parser("names", help="how well a model finds names in free text")
    n.add_argument("--backend", action="append", required=True)
    n.add_argument("--lang", action="append")
    n.add_argument("--threshold", type=float, default=0.5)
    n.set_defaults(func=cmd_names)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
