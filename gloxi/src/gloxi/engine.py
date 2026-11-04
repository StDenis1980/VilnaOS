# SPDX-License-Identifier: GPL-3.0-or-later
"""The local model engine that Gloxi starts itself (ADR 0004, ADR 0008).

In the Flatpak, llama.cpp's ``llama-server`` ships inside the package and the
model weights do not: they are about 2.5 GB, so Gloxi downloads them on the
first start, and only after the person agrees. Running from source, a
``llama-server`` that is already listening is simply used.

Rules:

- Nothing is downloaded without an explicit yes. The question names the
  model, the size, where it comes from and where it will be kept.
- The file is checked against the SHA-256 pinned here before it is used;
  a file that does not match is deleted.
- The server listens only on 127.0.0.1.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from gettext import gettext as _
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from gloxi import localkey
from gloxi.i18n import N_

CHUNK = 1 << 20


@dataclass(frozen=True)
class ModelFile:
    name: str  # shown to the person
    filename: str
    url: str
    sha256: str
    size: int  # bytes
    source: str  # who publishes it, shown to the person
    licence: str


# ADR 0008: Qwen3 4B, Q4_K_M, without "thinking". tools/check_model.py checks
# these values against Hugging Face in CI.
DEFAULT_MODEL = ModelFile(
    name="Qwen3 4B (Q4_K_M)",
    filename="Qwen3-4B-Q4_K_M.gguf",
    url="https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf",
    sha256="7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5",
    size=2_497_280_256,
    source=N_("Qwen (Alibaba Cloud) on Hugging Face"),
    licence="Apache-2.0",
)

# Decisions of 2026-11-03 and 2026-11-04 (ADR 0008, docs/benchmarks.md): the best
# of seven candidates (56 of 63 tasks, Ukrainian 24 of 27). Gloxi offers it on
# computers with 16 GB of RAM or more, and only after a speed test shows that it
# will be fast enough there (gloxi.upgrade).
LARGE_MODEL = ModelFile(
    name="Ministral 3 8B (Q4_K_M)",
    filename="Ministral-3-8B-Instruct-2512-Q4_K_M.gguf",
    url=(
        "https://huggingface.co/mistralai/Ministral-3-8B-Instruct-2512-GGUF/resolve/main/"
        "Ministral-3-8B-Instruct-2512-Q4_K_M.gguf"
    ),
    sha256="33e7a72cf5e6e2cfc2f2847075acc013d68bba023e35310cef86b5cf8fdca761",
    size=5_198_911_904,
    source=N_("Mistral AI on Hugging Face"),
    licence="Apache-2.0",
)

MODELS = (DEFAULT_MODEL, LARGE_MODEL)

# "16 GB" computers report a little less (15.3 GiB on the test laptop), and the
# 8B model needs ~7 GB of its own: 14 GiB keeps 8 GB machines out of the test.
LARGE_MODEL_MIN_RAM = 14 * 1024**3


def total_ram(meminfo: Path = Path("/proc/meminfo")) -> int | None:
    """MemTotal in bytes, or None if it cannot be read."""
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


SERVER_ARGS = (
    "--ctx-size",
    "4096",
    "--jinja",
    # ADR 0008: no thinking; never --reasoning-budget 0 (breaks parsing in b12048).
    "--chat-template-kwargs",
    '{"enable_thinking":false}',
)


class EngineError(Exception):
    """The engine cannot be prepared or started; the message is for the person."""


class DownloadCancelled(EngineError):
    pass


def models_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "gloxi" / "models"


def server_binary() -> str | None:
    return shutil.which("llama-server")


def human_size(size: int) -> str:
    if size >= 1e9:
        # The decimal separator of the interface language ("2,5 ГБ").
        value = f"{size / 1e9:.1f}".replace(".", _(DECIMAL_POINT))
        return _("{value} GB").format(value=value)
    return _("{value} MB").format(value=f"{size / 1e6:.0f}")


# Translators: the decimal separator, e.g. "," in Ukrainian.
DECIMAL_POINT = N_(".")


# -- download ---------------------------------------------------------------------

Progress = Callable[[int, int], None]  # done, total


def download(
    model: ModelFile,
    dest_dir: Path,
    *,
    progress: Progress = lambda done, total: None,
    cancelled: Callable[[], bool] = lambda: False,
    client: httpx.Client | None = None,
) -> Path:
    """Fetch ``model`` into ``dest_dir``, resuming a partial file, and verify it."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / model.filename
    part = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    done = 0
    if part.exists():
        with part.open("rb") as f:
            while block := f.read(CHUNK):
                digest.update(block)
                done += len(block)
        if done > model.size:
            part.unlink()
            digest, done = hashlib.sha256(), 0

    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0))
    try:
        headers = {"Range": f"bytes={done}-"} if done else {}
        with client.stream("GET", model.url, headers=headers) as response:
            if done and response.status_code == 200:
                # The server ignored the range: start again.
                digest, done = hashlib.sha256(), 0
                mode = "wb"
            elif response.status_code in (200, 206):
                mode = "ab" if done else "wb"
            else:
                raise EngineError(f"the download failed: HTTP {response.status_code}")
            with part.open(mode) as f:
                for block in response.iter_bytes(CHUNK):
                    if cancelled():
                        raise DownloadCancelled("the download was cancelled")
                    f.write(block)
                    digest.update(block)
                    done += len(block)
                    progress(done, model.size)
    except httpx.HTTPError as exc:
        raise EngineError(f"the download failed: {exc}") from exc
    finally:
        if own:
            client.close()

    if done != model.size or digest.hexdigest() != model.sha256:
        part.unlink(missing_ok=True)
        raise EngineError("the downloaded file is damaged or not the expected one")
    part.replace(target)
    return target


def verified(model: ModelFile, dest_dir: Path) -> Path | None:
    """The model file if it is present with the right size (the hash was checked on download)."""
    path = dest_dir / model.filename
    if path.is_file() and path.stat().st_size == model.size:
        return path
    return None


# -- server -----------------------------------------------------------------------


def _host_port(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    return parts.hostname or "127.0.0.1", parts.port or 80


def is_local_url(url: str) -> bool:
    return localkey.is_local_url(url)


def listening(url: str, client: httpx.Client | None = None, timeout: float = 2.0) -> bool:
    """True if an OpenAI-compatible server answers at ``url`` (…/v1)."""
    base = url.rstrip("/").removesuffix("/v1")
    try:
        if client is not None:
            return client.get(f"{base}/health", timeout=timeout).status_code == 200
        return httpx.get(f"{base}/health", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


class Engine:
    """A ``llama-server`` child process on 127.0.0.1, stopped with Gloxi."""

    def __init__(self, binary: str, model_path: Path, url: str) -> None:
        host, port = _host_port(url)
        if host not in ("127.0.0.1", "localhost"):
            raise EngineError("Gloxi starts the model only on 127.0.0.1")
        self.argv = [
            binary,
            "--model",
            str(model_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            *SERVER_ARGS,
        ]
        self.url = url
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def start(self, timeout: float = 180.0) -> None:
        # The key goes in a file, not on the command line, where other
        # processes could read it; the file is gone once the server runs.
        with localkey.key_file() as key_path:
            self._start(key_path, timeout)

    def _start(self, key_path: Path, timeout: float) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            # Run from the server's own folder: llama.cpp also loads backends
            # from the working directory, which must not be the person's.
            self._proc = subprocess.Popen(
                [*self.argv, "--api-key-file", str(key_path)],
                cwd=Path(self.argv[0]).resolve().parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                code = self._proc.returncode
                raise EngineError(f"the model stopped while starting (exit {code})")
            if listening(self.url, timeout=1.0):
                return
            time.sleep(0.5)
        self.stop()
        raise EngineError("the model did not start in time")

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# -- what to do at start-up ---------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """What start-up needs: ``ready`` (a server answers), ``start`` (model present),
    ``download`` (ask the person first) or ``none`` (no engine here)."""

    action: str
    model: ModelFile = DEFAULT_MODEL
    model_path: Path | None = None
    binary: str | None = None
    folder: Path | None = None


def plan(
    url: str,
    *,
    start_server: bool = True,
    env: dict[str, str] | None = None,
) -> Plan:
    """What start-up needs. The larger model is used if the person downloaded
    it; otherwise Qwen3 4B, which is the first model offered everywhere."""
    if listening(url):
        return Plan("ready")
    binary = server_binary()
    if not start_server or binary is None or not is_local_url(url):
        return Plan("none")
    folder = models_dir(env)
    for model in (LARGE_MODEL, DEFAULT_MODEL):
        path = verified(model, folder)
        if path is not None:
            return Plan("start", model=model, model_path=path, binary=binary, folder=folder)
    return Plan("download", model=DEFAULT_MODEL, binary=binary, folder=folder)
