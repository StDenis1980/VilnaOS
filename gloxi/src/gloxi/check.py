# SPDX-License-Identifier: GPL-3.0-or-later
"""``gloxi check``: will this computer run Gloxi well?

For testers (roadmap 2.4, the minimum-hardware check): it reads the processor,
memory, free disk space and graphics, says in plain words what that means for
the local model, and prints a short block to paste into the feedback form.

Nothing about the person is read or sent: no user name, computer name or home
folder path, and nothing leaves the computer. Only the hardware, the system
name, the Gloxi version and the speed test result already kept next to the
models.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from gettext import gettext as _
from pathlib import Path

from gloxi import __version__, upgrade
from gloxi.engine import (
    DEFAULT_MODEL,
    LARGE_MODEL,
    LARGE_MODEL_MIN_RAM,
    MODELS,
    human_size,
    models_dir,
    server_binary,
    total_ram,
    verified,
)

GIB = 1024**3
# "8 GB" computers report 7.3-7.8 GiB. Below this the 4B model and the desktop
# do not fit together, and Gloxi can only use the cloud.
MIN_RAM = 7 * GIB
# Roadmap 2.4: Intel Core 8th generation / Ryzen 2000 have AVX2; without it
# llama.cpp on the processor is several times slower.
NEEDED_CPU_FLAG = "avx2"
# What the system and llama.cpp need beside the model file while it downloads.
DISK_MARGIN = 500 * 1024**2

DEVICE_RE = re.compile(r"^\s*(\w+):\s*(.+?)\s*\((\d+) MiB,\s*(\d+) MiB free\)\s*$")


@dataclass(frozen=True)
class Gpu:
    backend: str  # "Vulkan0"
    name: str
    memory: int  # bytes


@dataclass
class Report:
    version: str = __version__
    system: str = ""
    desktop: str = ""
    cpu: str = ""
    cores: int = 0
    cpu_flag: bool | None = None  # AVX2; None when it cannot be read
    ram: int | None = None
    free_disk: int | None = None
    gpus: list[Gpu] = field(default_factory=list)
    gpus_known: bool = False  # llama-server answered
    models: list[str] = field(default_factory=list)  # file names present
    speed: float | None = None  # tokens per second, from the speed test
    flatpak: bool = False

    # -- what it means ---------------------------------------------------------

    def problems(self) -> list[str]:
        """Things that stop the local model or make it slow; empty if all is well."""
        found = []
        if self.ram is not None and self.ram < MIN_RAM:
            found.append(
                _(
                    "Memory: {ram}. The local model needs at least 8 GB; "
                    "Gloxi can still answer through the cloud with your own key."
                ).format(ram=gib(self.ram))
            )
        if self.cpu_flag is False:
            found.append(
                _(
                    "The processor has no AVX2 (it is older than Intel Core 8th "
                    "generation or Ryzen 2000), so the local model will be slow."
                )
            )
        if (
            not self.models
            and self.free_disk is not None
            and self.free_disk < DEFAULT_MODEL.size + DISK_MARGIN
        ):
            found.append(
                _("Free disk space: {free}. The model needs {size}.").format(
                    free=human_size(self.free_disk), size=human_size(DEFAULT_MODEL.size)
                )
            )
        return found

    def verdict(self) -> str:
        if self.problems():
            return _("This computer is below the minimum for the local model.")
        if self.ram is None:
            return _("Could not read the memory size; the local model may still work.")
        if self.speed is not None and self.speed * upgrade.LARGE_SPEED_FACTOR >= (
            upgrade.LARGE_MIN_SPEED
        ):
            return _("This computer is fast enough for the larger model, {name}.").format(
                name=LARGE_MODEL.name
            )
        if self.ram >= LARGE_MODEL_MIN_RAM and self.speed is None:
            return _(
                "This computer can run {name}. Gloxi will measure once whether the "
                "larger model would be fast enough."
            ).format(name=DEFAULT_MODEL.name)
        return _("This computer can run {name}.").format(name=DEFAULT_MODEL.name)

    # -- what is printed -------------------------------------------------------

    def lines(self) -> list[str]:
        """The block for the feedback form: facts only, in English, as in a bug report."""
        cpu_flag = {True: "yes", False: "no", None: "unknown"}[self.cpu_flag]
        if self.gpus_known:
            gpus = "; ".join(f"{g.backend} {g.name} ({gib(g.memory)})" for g in self.gpus)
        else:
            gpus = "unknown"
        return [
            f"Gloxi: {self.version}" + (" (Flatpak)" if self.flatpak else ""),
            f"System: {self.system or 'unknown'}" + (f", {self.desktop}" if self.desktop else ""),
            f"CPU: {self.cpu or 'unknown'}, {self.cores} threads, AVX2: {cpu_flag}",
            f"RAM: {gib(self.ram) if self.ram else 'unknown'}",
            f"GPU: {gpus or 'none for llama.cpp (processor only)'}",
            f"Free disk for models: {gib(self.free_disk) if self.free_disk else 'unknown'}",
            f"Models: {', '.join(self.models) or 'none yet'}",
            "Speed test: "
            + (f"{self.speed:.2f} tokens/s with {DEFAULT_MODEL.name}" if self.speed else "not run"),
        ]

    def render(self) -> str:
        parts = [self.verdict()]
        parts += [f"- {p}" for p in self.problems()]
        parts.append("")
        parts.append(_("To paste into the feedback form:"))
        parts.append("```")
        parts += self.lines()
        parts.append("```")
        return "\n".join(parts)


def gib(size: int | None) -> str:
    return f"{size / GIB:.1f} GiB" if size else "?"


# -- reading the computer ----------------------------------------------------------


def os_name(os_release: Path = Path("/etc/os-release")) -> str:
    """PRETTY_NAME of the host; inside Flatpak the host file is in /run/host."""
    for path in (Path("/run/host/os-release"), os_release):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            continue
    return platform.system()


def cpu_info(cpuinfo: Path = Path("/proc/cpuinfo")) -> tuple[str, bool | None]:
    """The processor model and whether it has AVX2."""
    try:
        text = cpuinfo.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return platform.processor(), None
    model, flag = "", None
    for line in text.splitlines():
        key, _sep, value = line.partition(":")
        key = key.strip()
        if key == "model name" and not model:
            model = " ".join(value.split())
        elif key == "flags" and flag is None:
            flag = NEEDED_CPU_FLAG in value.split()
    return model, flag


def free_space(folder: Path) -> int | None:
    """Free space where the models are kept, or on the nearest folder that exists."""
    for path in (folder, *folder.parents):
        if path.exists():
            try:
                return shutil.disk_usage(path).free
            except OSError:
                return None
    return None


def parse_devices(text: str) -> list[Gpu]:
    """The output of ``llama-server --list-devices``."""
    gpus = []
    for line in text.splitlines():
        match = DEVICE_RE.match(line)
        if match:
            backend, name, total, _free = match.groups()
            gpus.append(Gpu(backend, name, int(total) * 1024**2))
    return gpus


def list_devices(
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[Gpu] | None:
    """The graphics llama.cpp can use; None when llama-server is not here."""
    binary = server_binary()
    if not binary:
        return None
    try:
        done = run(
            [binary, "--list-devices"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_devices(done.stdout + done.stderr)


def collect(
    *,
    folder: Path | None = None,
    devices: Callable[[], list[Gpu] | None] = list_devices,
    env: dict[str, str] | None = None,
) -> Report:
    env = os.environ if env is None else env
    folder = folder or models_dir()
    cpu, flag = cpu_info()
    gpus = devices()
    state = upgrade.load_state(folder)
    return Report(
        system=os_name(),
        desktop=env.get("XDG_CURRENT_DESKTOP", ""),
        cpu=cpu,
        cores=os.cpu_count() or 0,
        cpu_flag=flag,
        ram=total_ram(),
        free_disk=free_space(folder),
        gpus=gpus or [],
        gpus_known=gpus is not None,
        models=[m.filename for m in MODELS if verified(m, folder) is not None],
        speed=state.get("tokens_per_second"),
        flatpak=bool(env.get("FLATPAK_ID")) or Path("/.flatpak-info").exists(),
    )
