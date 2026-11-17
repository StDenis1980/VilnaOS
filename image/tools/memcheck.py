#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Measure how Vilna fits in memory: the 8 GB check before the alpha.

Vilna's minimum is 8 GB of RAM (roadmap, stage 2), and the local model of
Gloxi lives in the same memory as GNOME and the person's programs. This tool
records what the kernel says while someone runs the scenarios of
``image/docs/low-memory-check.md`` and writes a report with a verdict per
scenario. It needs only the Python standard library, so it runs as is in the
installed image, in the live ISO and in a VM.

    memcheck.py record run.jsonl --label idle --duration 300
    memcheck.py record run.jsonl --label gloxi        # Ctrl-C to stop
    memcheck.py report run.jsonl > REPORT.md
    memcheck.py now                                   # one look, no file

Run ``record`` with sudo to see the PSS of every person's and service's
processes; without it, other users' processes show only RSS.

Every sample holds: /proc/meminfo, memory pressure (/proc/pressure/memory),
the counters of /proc/vmstat that matter (major faults, swap in/out, OOM
kills), each zram device, and the memory of process groups (the model, GNOME
Shell, Firefox, Gloxi, Herkos...). The thresholds in ``THRESHOLDS`` are a
first guess, to be calibrated on the first real 8 GB machine.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

KIB = 1024
MIB = 1024 * KIB

# Process groups, matched in order on the process name (comm) or the command
# line. Firefox runs as many processes with different names; the Python
# programs all have comm "python3".
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("llama-server", "comm", "llama-server"),
    ("gnome-shell", "comm", "gnome-shell"),
    ("Xwayland", "comm", "Xwayland"),
    ("firefox", "cmdline", "firefox"),
    ("herkos", "comm", "herkosd"),
    ("herkos", "cmdline", "herkos"),
    ("gloxi", "cmdline", "gloxi"),
    ("installer", "cmdline", "vilna-installer"),
    ("tinta", "cmdline", "tinta"),
    ("tracker", "comm", "tracker-miner"),
    ("packagekit", "comm", "packagekitd"),
    ("zypper", "comm", "zypper"),
    ("flatpak", "comm", "flatpak"),
)

VMSTAT_KEYS = ("pgmajfault", "pswpin", "pswpout", "oom_kill")
MEMINFO_KEYS = (
    "MemTotal",
    "MemAvailable",
    "MemFree",
    "Cached",
    "Shmem",
    "SwapTotal",
    "SwapFree",
    "AnonPages",
)


@dataclass(frozen=True)
class Thresholds:
    """When a scenario fails or needs a look. A first guess (see module doc)."""

    # Any OOM kill loses someone's work or the model.
    oom_kills: int = 0
    # "full" pressure: every task waits for memory, the desktop freezes.
    fail_full_avg10: float = 20.0
    warn_full_avg10: float = 5.0
    # "some" pressure: at least one task waits; the person notices stutter.
    warn_some_avg10: float = 25.0
    # Less than this left before the kernel starts reclaiming hard.
    warn_min_available: int = 300 * MIB
    # Reading pages back from disk (model weights, programs) all the time.
    warn_majfault_per_s: float = 200.0


THRESHOLDS = Thresholds()


# ---------------------------------------------------------------- reading /proc


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def parse_meminfo(text: str) -> dict[str, int]:
    """/proc/meminfo in bytes (the file says kB, which is KiB)."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if not parts or not parts[0].isdigit():
            continue
        value = int(parts[0])
        if len(parts) > 1 and parts[1] == "kB":
            value *= KIB
        out[name.strip()] = value
    return out


def parse_pressure(text: str) -> dict[str, dict[str, float]]:
    """/proc/pressure/memory: {"some": {"avg10": .., "total": ..}, "full": ..}."""
    out: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        kind, *fields = line.split()
        values: dict[str, float] = {}
        for field in fields:
            key, _, value = field.partition("=")
            try:
                values[key] = float(value)
            except ValueError:
                continue
        out[kind] = values
    return out


def parse_vmstat(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in VMSTAT_KEYS and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def read_zram(sys_root: Path) -> list[dict[str, object]]:
    """Each zram device: size, algorithm and how much it holds (mm_stat)."""
    devices: list[dict[str, object]] = []
    block = sys_root / "block"
    try:
        names = sorted(p.name for p in block.iterdir() if p.name.startswith("zram"))
    except OSError:
        return devices
    for name in names:
        dev = block / name
        info: dict[str, object] = {"name": name}
        size = read_text(dev / "disksize")
        if size and size.strip().isdigit():
            info["disksize"] = int(size)
        algo = read_text(dev / "comp_algorithm")
        if algo:
            # "lzo [lz4] zstd": the one in brackets is in use.
            chosen = [a.strip("[]") for a in algo.split() if a.startswith("[")]
            info["algorithm"] = chosen[0] if chosen else algo.strip()
        mm = read_text(dev / "mm_stat")
        if mm:
            fields = mm.split()
            names_mm = ("orig_data_size", "compr_data_size", "mem_used_total")
            for key, value in zip(names_mm, fields):
                if value.isdigit():
                    info[key] = int(value)
        devices.append(info)
    return devices


def group_of(comm: str, cmdline: str) -> str:
    for group, where, needle in GROUPS:
        haystack = comm if where == "comm" else cmdline
        if needle in haystack:
            return group
    return comm or "?"


def read_process(pid_dir: Path) -> tuple[str, dict[str, int]] | None:
    """(group, {"rss", "anon", "pss"}) of one process, or None if it is gone."""
    status = read_text(pid_dir / "status")
    if status is None:
        return None
    fields = parse_meminfo(status)
    if "VmRSS" not in fields:
        return None  # a kernel thread
    comm = (read_text(pid_dir / "comm") or "").strip()
    raw = read_text(pid_dir / "cmdline") or ""
    cmdline = raw.replace("\0", " ").strip()
    mem = {"rss": fields.get("VmRSS", 0), "anon": fields.get("RssAnon", 0)}
    rollup = read_text(pid_dir / "smaps_rollup")
    if rollup:
        pss = parse_meminfo(rollup).get("Pss")
        if pss is not None:
            mem["pss"] = pss
    return group_of(comm, cmdline), mem


def read_groups(proc_root: Path) -> dict[str, dict[str, int]]:
    groups: dict[str, dict[str, int]] = {}
    try:
        pids = [p for p in proc_root.iterdir() if p.name.isdigit()]
    except OSError:
        return groups
    for pid_dir in pids:
        found = read_process(pid_dir)
        if found is None:
            continue
        group, mem = found
        total = groups.setdefault(group, {"rss": 0, "anon": 0, "pss": 0, "procs": 0, "no_pss": 0})
        total["rss"] += mem["rss"]
        total["anon"] += mem["anon"]
        total["procs"] += 1
        if "pss" in mem:
            total["pss"] += mem["pss"]
        else:
            # Not ours to read (no root): count the RSS so the sum is not low.
            total["pss"] += mem["rss"]
            total["no_pss"] += 1
    return groups


def sample(proc_root: Path = Path("/proc"), sys_root: Path = Path("/sys")) -> dict[str, object]:
    meminfo = parse_meminfo(read_text(proc_root / "meminfo") or "")
    return {
        "type": "sample",
        "time": time.time(),
        "mem": {k: meminfo[k] for k in MEMINFO_KEYS if k in meminfo},
        "psi": parse_pressure(read_text(proc_root / "pressure" / "memory") or ""),
        "vmstat": parse_vmstat(read_text(proc_root / "vmstat") or ""),
        "zram": read_zram(sys_root),
        "groups": read_groups(proc_root),
    }


def machine(proc_root: Path = Path("/proc"), sys_root: Path = Path("/sys")) -> dict[str, object]:
    cpu = "?"
    for line in (read_text(proc_root / "cpuinfo") or "").splitlines():
        if line.startswith("model name"):
            cpu = line.partition(":")[2].strip()
            break
    os_name = "?"
    for line in (read_text(Path("/etc/os-release")) or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            os_name = line.partition("=")[2].strip().strip('"')
    swaps = (read_text(proc_root / "swaps") or "").splitlines()[1:]
    return {
        "type": "machine",
        "time": time.time(),
        "cpu": cpu,
        "cpus": os.cpu_count(),
        "kernel": platform.release(),
        "os": os_name,
        "mem_total": parse_meminfo(read_text(proc_root / "meminfo") or "").get("MemTotal"),
        "swaps": [" ".join(s.split()[:3]) for s in swaps],
        "zram": read_zram(sys_root),
        "cmdline": memory_args(read_text(proc_root / "cmdline") or ""),
    }


# Kernel arguments that change the memory picture. The rest (root=UUID,
# LUKS ids) says nothing about memory and stays out of a report people share.
MEMORY_ARGS = ("mem=", "maxcpus=", "nr_cpus=", "zswap.", "transparent_hugepage", "psi=")


def memory_args(cmdline: str) -> str:
    return " ".join(a for a in cmdline.split() if a.startswith(MEMORY_ARGS))


# ---------------------------------------------------------------- the report


@dataclass
class Scenario:
    label: str
    samples: list[dict]

    @property
    def seconds(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        return float(self.samples[-1]["time"]) - float(self.samples[0]["time"])

    def mem(self, key: str) -> list[int]:
        return [s["mem"][key] for s in self.samples if key in s.get("mem", {})]

    def psi(self, kind: str, key: str = "avg10") -> list[float]:
        return [s["psi"][kind][key] for s in self.samples if key in s.get("psi", {}).get(kind, {})]

    def counter(self, key: str) -> int | None:
        values = [s["vmstat"][key] for s in self.samples if key in s.get("vmstat", {})]
        if len(values) < 2:
            return None
        return max(0, values[-1] - values[0])

    def swap_used(self) -> list[int]:
        out = []
        for s in self.samples:
            mem = s.get("mem", {})
            if "SwapTotal" in mem and "SwapFree" in mem:
                out.append(mem["SwapTotal"] - mem["SwapFree"])
        return out

    def zram_used(self) -> list[int]:
        return [
            sum(int(d.get("mem_used_total", 0)) for d in s.get("zram", [])) for s in self.samples
        ]

    def peak_groups(self) -> dict[str, dict[str, int]]:
        peaks: dict[str, dict[str, int]] = {}
        for s in self.samples:
            for name, mem in s.get("groups", {}).items():
                peak = peaks.setdefault(name, {"pss": 0, "anon": 0, "procs": 0, "no_pss": 0})
                for key in peak:
                    peak[key] = max(peak[key], int(mem.get(key, 0)))
        return peaks

    def verdict(self, t: Thresholds = THRESHOLDS) -> tuple[str, list[str]]:
        """("ok" | "look" | "fail", reasons)."""
        fails: list[str] = []
        looks: list[str] = []
        kills = self.counter("oom_kill")
        if kills is not None and kills > t.oom_kills:
            fails.append(f"OOM killer: {kills}")
        full = max(self.psi("full"), default=0.0)
        some = max(self.psi("some"), default=0.0)
        if full >= t.fail_full_avg10:
            fails.append(f"full pressure {full:.1f}% ≥ {t.fail_full_avg10:g}%")
        elif full >= t.warn_full_avg10:
            looks.append(f"full pressure {full:.1f}% ≥ {t.warn_full_avg10:g}%")
        if some >= t.warn_some_avg10:
            looks.append(f"some pressure {some:.1f}% ≥ {t.warn_some_avg10:g}%")
        available = self.mem("MemAvailable")
        if available and min(available) < t.warn_min_available:
            looks.append(f"MemAvailable {mib(min(available))} < {mib(t.warn_min_available)}")
        faults = self.counter("pgmajfault")
        if faults is not None and self.seconds > 0:
            rate = faults / self.seconds
            if rate >= t.warn_majfault_per_s:
                looks.append(f"major faults {rate:.0f}/s ≥ {t.warn_majfault_per_s:g}/s")
        if fails:
            return "fail", fails + looks
        if looks:
            return "look", looks
        return "ok", []


def mib(value: float) -> str:
    return f"{value / MIB:,.0f} MiB".replace(",", " ")


def load(lines: Iterable[str]) -> tuple[dict | None, list[Scenario]]:
    """The machine line and the samples split by label, in recorded order."""
    machine_info: dict | None = None
    scenarios: list[Scenario] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a line cut by Ctrl-C or a full disk
        if not isinstance(record, dict):
            continue
        if record.get("type") == "machine":
            machine_info = machine_info or record
        elif record.get("type") == "sample":
            label = str(record.get("label", "-"))
            if not scenarios or scenarios[-1].label != label:
                scenarios.append(Scenario(label, []))
            scenarios[-1].samples.append(record)
    return machine_info, scenarios


VERDICT_MARK = {"ok": "✅ ok", "look": "⚠️ look", "fail": "❌ fail"}


def report(machine_info: dict | None, scenarios: list[Scenario], top: int = 8) -> str:
    out: list[str] = ["# Memory check", ""]
    if machine_info:
        out += [
            f"- OS: {machine_info.get('os', '?')}, kernel {machine_info.get('kernel', '?')}",
            f"- CPU: {machine_info.get('cpu', '?')} ({machine_info.get('cpus', '?')} threads)",
            f"- RAM: {mib(int(machine_info.get('mem_total') or 0))}",
            f"- Swap: {'; '.join(machine_info.get('swaps') or []) or 'none'}",
        ]
        for dev in machine_info.get("zram") or []:
            out.append(
                f"- {dev.get('name')}: {mib(int(dev.get('disksize', 0)))}, {dev.get('algorithm', '?')}"
            )
        out += [f"- Memory arguments of the kernel: `{machine_info.get('cmdline') or '-'}`", ""]
    out += [
        "| Scenario | Time | Verdict | Min available | Max swap used | Max zram RAM | PSI some / full (max avg10) | Major faults/s | OOM kills |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for sc in scenarios:
        verdict, _ = sc.verdict()
        available = sc.mem("MemAvailable")
        faults = sc.counter("pgmajfault")
        rate = f"{faults / sc.seconds:.0f}" if faults is not None and sc.seconds else "–"
        kills = sc.counter("oom_kill")
        out.append(
            "| {label} | {secs:.0f} s | {verdict} | {avail} | {swap} | {zram} | {some:.1f} / {full:.1f} | {rate} | {kills} |".format(
                label=sc.label,
                secs=sc.seconds,
                verdict=VERDICT_MARK[verdict],
                avail=mib(min(available)) if available else "–",
                swap=mib(max(sc.swap_used(), default=0)),
                zram=mib(max(sc.zram_used(), default=0)),
                some=max(sc.psi("some"), default=0.0),
                full=max(sc.psi("full"), default=0.0),
                rate=rate,
                kills="–" if kills is None else kills,
            )
        )
    for sc in scenarios:
        verdict, reasons = sc.verdict()
        out += ["", f"## {sc.label}", ""]
        if reasons:
            out += [f"- {r}" for r in reasons] + [""]
        out += [
            "| Group | Peak PSS | Peak private (anon) | Processes |",
            "| --- | --- | --- | --- |",
        ]
        peaks = sorted(sc.peak_groups().items(), key=lambda kv: kv[1]["pss"], reverse=True)
        for name, peak in peaks[:top]:
            mark = " (RSS)" if peak["no_pss"] else ""
            out.append(
                f"| {name} | {mib(peak['pss'])}{mark} | {mib(peak['anon'])} | {peak['procs']} |"
            )
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- commands


def record(path: Path, label: str, interval: float, duration: float | None) -> int:
    stop = False

    def on_signal(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    new_file = not path.exists() or path.stat().st_size == 0
    start = time.monotonic()
    count = 0
    with path.open("a", encoding="utf-8") as out:
        if new_file:
            out.write(json.dumps(machine(), ensure_ascii=False) + "\n")
        while not stop:
            s = sample()
            s["label"] = label
            out.write(json.dumps(s, ensure_ascii=False) + "\n")
            out.flush()
            count += 1
            if count == 1 or count % 15 == 0:
                avail = s["mem"].get("MemAvailable", 0)
                psi = s["psi"].get("some", {}).get("avg10", 0.0)
                print(f"[{label}] available {mib(avail)}, pressure {psi:.1f}%", file=sys.stderr)
            if duration is not None and time.monotonic() - start >= duration:
                break
            deadline = time.monotonic() + interval
            while not stop and time.monotonic() < deadline:
                time.sleep(min(0.2, interval))
    print(f"[{label}] {count} samples → {path}", file=sys.stderr)
    return 0


def iter_lines(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8", errors="replace") as f:
        yield from f


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="sample memory into a JSON Lines file")
    rec.add_argument("file", type=Path)
    rec.add_argument("--label", default="run", help="scenario name (see low-memory-check.md)")
    rec.add_argument("--interval", type=float, default=2.0, help="seconds between samples")
    rec.add_argument("--duration", type=float, help="stop after this many seconds")
    rep = sub.add_parser("report", help="Markdown report of a recorded file")
    rep.add_argument("file", type=Path)
    rep.add_argument("--top", type=int, default=8, help="process groups per scenario")
    sub.add_parser("now", help="print one sample as a report")
    args = parser.parse_args(argv)

    if args.command == "record":
        if args.interval <= 0:
            parser.error("--interval must be above 0")
        return record(args.file, args.label, args.interval, args.duration)
    if args.command == "report":
        if not args.file.exists():
            parser.error(f"{args.file} does not exist")
        machine_info, scenarios = load(iter_lines(args.file))
        print(report(machine_info, scenarios, top=max(1, args.top)))
        return 0
    s = sample()
    s["label"] = "now"
    print(report(machine(), [Scenario("now", [s])]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
