# SPDX-License-Identifier: GPL-3.0-or-later
"""gloxi check: plain words about the hardware, and nothing personal in the report."""

import getpass
import json
import socket
import subprocess
from pathlib import Path

from gloxi import check
from gloxi.check import Gpu, Report, cpu_info, parse_devices
from gloxi.cli import main
from gloxi.engine import DEFAULT_MODEL, LARGE_MODEL

GIB = 1024**3

DEVICES = """\
ggml_vulkan: Found 1 Vulkan devices:
Available devices:
  Vulkan0: Intel(R) Iris(R) Xe Graphics (RPL-P) (15903 MiB, 14311 MiB free)
"""


def report(**kw):
    base = dict(ram=16 * GIB, cpu_flag=True, free_disk=50 * GIB, gpus_known=True)
    return Report(**{**base, **kw})


def test_parse_devices_reads_llama_server_output():
    assert parse_devices(DEVICES) == [
        Gpu("Vulkan0", "Intel(R) Iris(R) Xe Graphics (RPL-P)", 15903 * 1024**2)
    ]
    assert parse_devices("Available devices:\n") == []


def test_cpu_info_finds_model_and_avx2(tmp_path):
    info = tmp_path / "cpuinfo"
    info.write_text(
        "processor\t: 0\nmodel name\t: 13th Gen Intel(R) Core(TM) i5-1340P\n"
        "flags\t\t: fpu sse2 avx avx2 fma\n\nprocessor\t: 1\n"
        "model name\t: 13th Gen Intel(R) Core(TM) i5-1340P\nflags\t\t: fpu\n"
    )
    assert cpu_info(info) == ("13th Gen Intel(R) Core(TM) i5-1340P", True)
    info.write_text("model name\t: Old CPU\nflags\t\t: fpu sse2 avx\n")
    assert cpu_info(info) == ("Old CPU", False)
    assert cpu_info(tmp_path / "missing")[1] is None


def test_small_memory_old_processor_and_full_disk_are_problems():
    r = report(ram=int(3.8 * GIB), cpu_flag=False, free_disk=1 * GIB)
    problems = r.problems()
    assert len(problems) == 3
    assert "cloud" in problems[0]
    assert "AVX2" in problems[1]
    assert "below the minimum" in r.verdict()


def test_disk_space_does_not_matter_once_the_model_is_there():
    assert report(free_disk=GIB, models=[DEFAULT_MODEL.filename]).problems() == []


def test_eight_gb_laptop_runs_the_small_model():
    r = report(ram=int(7.6 * GIB))
    assert r.problems() == []
    assert r.verdict() == f"This computer can run {DEFAULT_MODEL.name}."


def test_sixteen_gb_without_a_test_says_gloxi_will_measure():
    assert "measure once" in report().verdict()


def test_speed_test_result_decides_the_larger_model():
    assert LARGE_MODEL.name in report(speed=20.0).verdict()
    assert report(speed=9.39).verdict() == f"This computer can run {DEFAULT_MODEL.name}."


def test_lines_name_the_hardware():
    text = "\n".join(
        report(
            cpu="i5-1340P",
            cores=16,
            gpus=parse_devices(DEVICES),
            speed=9.39,
            flatpak=True,
        ).lines()
    )
    assert "(Flatpak)" in text
    assert "AVX2: yes" in text
    assert "Vulkan0 Intel(R) Iris(R) Xe Graphics (RPL-P) (15.5 GiB)" in text
    assert "9.39 tokens/s" in text
    assert "GPU: none for llama.cpp" in "\n".join(report(gpus=[]).lines())
    assert "GPU: unknown" in "\n".join(report(gpus_known=False).lines())


def test_collect_reads_models_and_speed_test(tmp_path):
    (tmp_path / DEFAULT_MODEL.filename).write_bytes(b"")  # wrong size: not counted
    (tmp_path / "speed-test.json").write_text(json.dumps({"tokens_per_second": 9.39}))
    r = check.collect(folder=tmp_path, devices=lambda: None, env={})
    assert r.models == []
    assert r.speed == 9.39
    assert r.gpus_known is False
    assert r.free_disk and r.free_disk > 0


def test_report_has_nothing_personal(tmp_path):
    text = check.collect(folder=tmp_path, devices=lambda: [], env={}).render()
    assert str(Path.home()) not in text
    assert str(tmp_path) not in text
    assert socket.gethostname() not in text
    assert getpass.getuser() not in text.replace("Gloxi", "")


def test_list_devices_without_llama_server(monkeypatch):
    monkeypatch.setattr(check, "server_binary", lambda: None)
    assert check.list_devices() is None


def test_list_devices_runs_llama_server(monkeypatch):
    monkeypatch.setattr(check, "server_binary", lambda: "/app/bin/llama-server")
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=DEVICES, stderr="")

    assert check.list_devices(run)[0].backend == "Vulkan0"
    assert calls == [["/app/bin/llama-server", "--list-devices"]]


def test_cli_check_prints_report(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(check, "server_binary", lambda: None)
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "To paste into the feedback form:" in out
    assert "Speed test: not run" in out
