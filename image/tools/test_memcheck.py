#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for image/tools/memcheck.py on a made-up /proc and /sys.

Run: python3 image/tools/test_memcheck.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import memcheck  # after sys.path.insert above

MEMINFO = """MemTotal:        7990000 kB
MemFree:          200000 kB
MemAvailable:    1500000 kB
Cached:          2000000 kB
SwapTotal:       3990000 kB
SwapFree:        3000000 kB
Shmem:            100000 kB
AnonPages:       4000000 kB
HugePages_Total:       0
"""

PRESSURE = """some avg10=12.50 avg60=3.00 avg300=1.00 total=123456
full avg10=1.25 avg60=0.10 avg300=0.00 total=2345
"""

VMSTAT = """nr_free_pages 50000
pgmajfault 1000
pswpin 10
pswpout 20
oom_kill 0
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def fake_process(
    proc: Path, pid: int, comm: str, cmdline: str, rss_kb: int, anon_kb: int, pss_kb: int | None
) -> None:
    d = proc / str(pid)
    write(d / "comm", comm + "\n")
    write(d / "cmdline", cmdline.replace(" ", "\0") + "\0")
    write(d / "status", f"Name:\t{comm}\nVmRSS:\t{rss_kb} kB\nRssAnon:\t{anon_kb} kB\n")
    if pss_kb is not None:
        write(d / "smaps_rollup", f"Rss: {rss_kb} kB\nPss: {pss_kb} kB\n")


def fake_system(root: Path) -> tuple[Path, Path]:
    proc, sys_root = root / "proc", root / "sys"
    write(proc / "meminfo", MEMINFO)
    write(proc / "pressure" / "memory", PRESSURE)
    write(proc / "vmstat", VMSTAT)
    write(proc / "cmdline", "BOOT_IMAGE=/vmlinuz root=UUID=abcd rd.luks.uuid=ef01 mem=8G quiet\n")
    write(proc / "cpuinfo", "processor\t: 0\nmodel name\t: Intel(R) Core(TM) i5-8250U\n")
    write(
        proc / "swaps",
        "Filename Type Size Used Priority\n/dev/zram0 partition 3990000 990000 100\n",
    )
    fake_process(
        proc,
        100,
        "llama-server",
        "/usr/bin/llama-server --model default.gguf",
        4_900_000,
        900_000,
        4_800_000,
    )
    fake_process(proc, 200, "gnome-shell", "/usr/bin/gnome-shell", 400_000, 300_000, 350_000)
    fake_process(proc, 301, "firefox", "/usr/lib64/firefox/firefox", 300_000, 200_000, None)
    fake_process(
        proc,
        302,
        "Isolated Web Co",
        "/usr/lib64/firefox/firefox -contentproc",
        200_000,
        150_000,
        None,
    )
    fake_process(proc, 400, "python3", "/usr/bin/python3 -m gloxi.service", 90_000, 60_000, 80_000)
    # A kernel thread has no VmRSS and is skipped.
    write(proc / "2" / "status", "Name:\tkthreadd\n")
    write(proc / "2" / "comm", "kthreadd\n")
    zram = sys_root / "block" / "zram0"
    write(zram / "disksize", "4085760000\n")
    write(zram / "comp_algorithm", "lzo lzo-rle [zstd]\n")
    write(zram / "mm_stat", "1013760000 300000000 310000000 0 320000000 0 0 0 0\n")
    write(sys_root / "block" / "sda" / "size", "1\n")
    return proc, sys_root


def sample_line(
    label: str, t: float, avail_mib: int, some: float, full: float, majfault: int, oom: int
) -> str:
    return json.dumps(
        {
            "type": "sample",
            "label": label,
            "time": t,
            "mem": {
                "MemAvailable": avail_mib * memcheck.MIB,
                "SwapTotal": 4 * 1024 * memcheck.MIB,
                "SwapFree": 3 * 1024 * memcheck.MIB,
            },
            "psi": {"some": {"avg10": some}, "full": {"avg10": full}},
            "vmstat": {"pgmajfault": majfault, "oom_kill": oom},
            "zram": [{"name": "zram0", "mem_used_total": 300 * memcheck.MIB}],
            "groups": {
                "llama-server": {
                    "pss": 4000 * memcheck.MIB,
                    "anon": 900 * memcheck.MIB,
                    "procs": 1,
                    "no_pss": 0,
                }
            },
        }
    )


class Parsing(unittest.TestCase):
    def test_meminfo_is_in_bytes_and_skips_counts(self) -> None:
        info = memcheck.parse_meminfo(MEMINFO)
        self.assertEqual(info["MemTotal"], 7_990_000 * 1024)
        self.assertEqual(info["HugePages_Total"], 0)

    def test_pressure(self) -> None:
        psi = memcheck.parse_pressure(PRESSURE)
        self.assertEqual(psi["some"]["avg10"], 12.5)
        self.assertEqual(psi["full"]["total"], 2345)

    def test_vmstat_keeps_only_known_counters(self) -> None:
        self.assertEqual(
            memcheck.parse_vmstat(VMSTAT),
            {"pgmajfault": 1000, "pswpin": 10, "pswpout": 20, "oom_kill": 0},
        )

    def test_groups(self) -> None:
        self.assertEqual(memcheck.group_of("llama-server", ""), "llama-server")
        self.assertEqual(
            memcheck.group_of("Isolated Web Co", "/usr/lib64/firefox/firefox -contentproc"),
            "firefox",
        )
        self.assertEqual(memcheck.group_of("python3", "/usr/bin/python3 -m gloxi.service"), "gloxi")
        self.assertEqual(memcheck.group_of("herkosd", "/usr/libexec/herkosd"), "herkos")
        self.assertEqual(memcheck.group_of("nautilus", "/usr/bin/nautilus"), "nautilus")

    def test_kernel_arguments_keep_only_memory_ones(self) -> None:
        args = memcheck.memory_args(
            "root=UUID=abcd rd.luks.uuid=ef01 mem=8G maxcpus=4 zswap.enabled=0 quiet"
        )
        self.assertEqual(args, "mem=8G maxcpus=4 zswap.enabled=0")


class Sampling(unittest.TestCase):
    def test_sample_of_a_fake_system(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc, sys_root = fake_system(Path(tmp))
            s = memcheck.sample(proc, sys_root)
        self.assertEqual(s["mem"]["MemAvailable"], 1_500_000 * 1024)
        self.assertEqual(s["psi"]["full"]["avg10"], 1.25)
        self.assertEqual(
            s["zram"],
            [
                {
                    "name": "zram0",
                    "disksize": 4085760000,
                    "algorithm": "zstd",
                    "orig_data_size": 1013760000,
                    "compr_data_size": 300000000,
                    "mem_used_total": 310000000,
                }
            ],
        )
        groups = s["groups"]
        self.assertNotIn("kthreadd", groups)
        self.assertEqual(groups["llama-server"]["pss"], 4_800_000 * 1024)
        # Firefox's two processes are one group; without smaps_rollup the RSS counts.
        self.assertEqual(groups["firefox"]["procs"], 2)
        self.assertEqual(groups["firefox"]["pss"], 500_000 * 1024)
        self.assertEqual(groups["firefox"]["no_pss"], 2)
        self.assertEqual(groups["gloxi"]["anon"], 60_000 * 1024)

    def test_machine_leaves_disk_ids_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc, sys_root = fake_system(Path(tmp))
            m = memcheck.machine(proc, sys_root)
        self.assertEqual(m["cpu"], "Intel(R) Core(TM) i5-8250U")
        self.assertEqual(m["cmdline"], "mem=8G")
        self.assertNotIn("UUID", json.dumps(m))
        self.assertEqual(m["swaps"], ["/dev/zram0 partition 3990000"])

    def test_missing_files_give_empty_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = memcheck.sample(Path(tmp) / "nope", Path(tmp) / "nosys")
        self.assertEqual(
            (s["mem"], s["psi"], s["vmstat"], s["zram"], s["groups"]), ({}, {}, {}, [], {})
        )


class Verdicts(unittest.TestCase):
    def scenario(self, *lines: str) -> memcheck.Scenario:
        _, scenarios = memcheck.load(lines)
        self.assertEqual(len(scenarios), 1)
        return scenarios[0]

    def test_calm_scenario_is_ok(self) -> None:
        sc = self.scenario(
            sample_line("idle", 0, 2000, 0.5, 0.0, 100, 0),
            sample_line("idle", 60, 1900, 1.0, 0.0, 200, 0),
        )
        self.assertEqual(sc.verdict(), ("ok", []))
        self.assertEqual(sc.seconds, 60)

    def test_oom_kill_fails(self) -> None:
        sc = self.scenario(
            sample_line("tabs", 0, 2000, 0, 0, 0, 3), sample_line("tabs", 10, 100, 0, 0, 0, 4)
        )
        verdict, reasons = sc.verdict()
        self.assertEqual(verdict, "fail")
        self.assertIn("OOM killer: 1", reasons)
        self.assertTrue(any("MemAvailable" in r for r in reasons))

    def test_full_pressure_fails_and_some_pressure_needs_a_look(self) -> None:
        sc = self.scenario(
            sample_line("gloxi", 0, 2000, 30, 25, 0, 0), sample_line("gloxi", 10, 2000, 0, 0, 0, 0)
        )
        self.assertEqual(sc.verdict()[0], "fail")
        sc = self.scenario(
            sample_line("gloxi", 0, 2000, 30, 1, 0, 0), sample_line("gloxi", 10, 2000, 0, 0, 0, 0)
        )
        self.assertEqual(sc.verdict(), ("look", ["some pressure 30.0% ≥ 25%"]))

    def test_major_fault_rate(self) -> None:
        sc = self.scenario(
            sample_line("gloxi", 0, 2000, 0, 0, 0, 0), sample_line("gloxi", 10, 2000, 0, 0, 5000, 0)
        )
        self.assertEqual(sc.verdict(), ("look", ["major faults 500/s ≥ 200/s"]))


class Report(unittest.TestCase):
    def test_labels_split_scenarios_and_bad_lines_are_skipped(self) -> None:
        lines = [
            json.dumps(
                {
                    "type": "machine",
                    "os": "Vilna",
                    "cpu": "i5-8250U",
                    "cpus": 8,
                    "mem_total": 7990000 * 1024,
                    "swaps": [],
                    "zram": [],
                    "cmdline": "mem=8G",
                }
            ),
            sample_line("idle", 0, 2000, 0, 0, 0, 0),
            sample_line("idle", 5, 2000, 0, 0, 0, 0),
            '{"type": "sample", "label": "gl',  # cut by Ctrl-C
            "[1, 2]",
            sample_line("gloxi", 6, 900, 3, 0, 0, 0),
            sample_line("gloxi", 16, 800, 4, 0, 0, 0),
        ]
        machine_info, scenarios = memcheck.load(lines)
        self.assertEqual([s.label for s in scenarios], ["idle", "gloxi"])
        text = memcheck.report(machine_info, scenarios)
        self.assertIn("| gloxi | 10 s | ✅ ok | 800 MiB |", text)
        self.assertIn("| llama-server | 4 000 MiB | 900 MiB | 1 |", text)
        self.assertIn("`mem=8G`", text)

    def test_record_then_report_on_this_machine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.jsonl"
            self.assertEqual(
                memcheck.main(
                    ["record", str(path), "--label", "a", "--duration", "0", "--interval", "0.1"]
                ),
                0,
            )
            self.assertEqual(
                memcheck.main(
                    ["record", str(path), "--label", "b", "--duration", "0", "--interval", "0.1"]
                ),
                0,
            )
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[0])["type"], "machine")
        self.assertEqual(len(lines), 3)  # one machine line, then one sample per run
        _, scenarios = memcheck.load(lines)
        self.assertEqual([s.label for s in scenarios], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
