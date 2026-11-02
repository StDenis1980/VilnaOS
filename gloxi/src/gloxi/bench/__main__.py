# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line for the model measurements: ``gloxi-bench`` or ``python -m gloxi.bench``."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from gloxi.bench import (
    load_tasks,
    machine_info,
    markdown_report,
    peak_rss_mb,
    run_task,
    summarise,
    write_results,
)
from gloxi.config import DEFAULT_LOCAL_URL, ModelEndpoint
from gloxi.llm import ChatClient


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gloxi-bench", description="Measure a local model on Gloxi's test commands."
    )
    p.add_argument("--url", default=DEFAULT_LOCAL_URL, help="OpenAI-compatible endpoint")
    p.add_argument("--model", default="local", help="model name (also names the report)")
    p.add_argument("--tasks", type=Path, help="tasks file (default: the built-in set)")
    p.add_argument("--only", help="run only tasks whose id or category contains this")
    p.add_argument("--server-pid", type=int, help="PID of llama-server, to report its peak RAM")
    p.add_argument("--timeout", type=float, default=300, help="seconds per answer")
    p.add_argument("--out", type=Path, default=Path("bench-results"), help="where to write")
    p.add_argument("--no-warmup", action="store_true", help="skip the first, untimed request")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tasks = load_tasks(args.tasks)
    if args.only:
        tasks = [t for t in tasks if args.only in t.id or args.only in t.category]
    if not tasks:
        print("gloxi-bench: no tasks to run", file=sys.stderr)
        return 2

    endpoint = ModelEndpoint(url=args.url.rstrip("/"), model=args.model, timeout=args.timeout)
    with ChatClient(endpoint) as client:
        if not client.is_available():
            print(f"gloxi-bench: no model answers at {endpoint.url}", file=sys.stderr)
            return 1
        if not args.no_warmup:
            # The first request also loads the model; do not count it.
            run_task(client, tasks[0])
        results = []
        for n, task in enumerate(tasks, 1):
            r = run_task(client, task)
            mark = "ok" if r.passed else "FAIL " + ",".join(r.failed_checks)
            print(f"[{n}/{len(tasks)}] {task.id}: {mark} ({r.total_s} s)", file=sys.stderr)
            results.append(r)

    meta = {
        "model": args.model,
        "url": endpoint.url,
        "date": datetime.now().strftime("%Y-%m-%d_%H%M"),
        **machine_info(),
    }
    if args.server_pid:
        meta["server_peak_mb"] = peak_rss_mb(args.server_pid)
    summary = summarise(results)
    report = write_results(args.out, meta, summary, results)
    print(markdown_report(meta, summary, results))
    print(f"Report: {report}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
