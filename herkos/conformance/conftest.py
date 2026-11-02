# SPDX-License-Identifier: GPL-3.0-or-later
"""Conformance tests for any Herkos: they start it as a program and talk to it
only through its socket, the way Gloxi does (ADR 0003, ADR 0005).

    uv run pytest conformance                                  # Herkos-lite
    HERKOS_SERVE="rs/target/debug/herkosd serve --confirm deny" \\
        uv run pytest conformance                              # Herkos in Rust

The program gets private XDG folders and a config that allows one folder and
turns the Keros model off. It must answer "no" to every confirmation
(``--confirm deny``): nothing here may depend on a person.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

PROTOCOL = "0.1"
DEFAULT_SERVE = [sys.executable, "-m", "herkos", "serve", "--confirm", "deny"]


def serve_command() -> list[str]:
    value = os.environ.get("HERKOS_SERVE")
    return shlex.split(value) if value else list(DEFAULT_SERVE)


@dataclass
class Herkos:
    socket: Path
    docs: Path
    state: Path
    process: subprocess.Popen
    env: dict[str, str]


class Client:
    def __init__(self, path: Path) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(str(path))
        self.file = self.sock.makefile("rwb")
        self.next_id = 0

    def send_raw(self, data: bytes) -> None:
        self.file.write(data)
        self.file.flush()

    def recv(self):
        line = self.file.readline()
        return json.loads(line) if line else None

    def call(self, method: str, params=None):
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg).encode() + b"\n")
        return self.recv()

    def hello(self):
        return self.call("herkos.hello", {"protocol": [PROTOCOL], "client": "conformance"})

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def herkos(tmp_path: Path):
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    docs = tmp_path / "home" / "Documents"
    docs.mkdir(parents=True)
    config = tmp_path / "config" / "herkos"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(
        f'allowed_roots = ["{docs}"]\nkeros_model = "off"\n', encoding="utf-8"
    )
    env = dict(os.environ)
    env.update(
        XDG_RUNTIME_DIR=str(runtime),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        HOME=str(tmp_path / "home"),
        LANG="C.UTF-8",
        LANGUAGE="",
        LC_ALL="",
        LC_MESSAGES="",
    )
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    path = runtime / "vilna" / "herkos.sock"
    proc = subprocess.Popen(
        serve_command(),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 20
    while not path.exists():
        if proc.poll() is not None:
            err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            pytest.fail(f"Herkos exited with {proc.returncode}: {err}")
        if time.monotonic() > deadline:
            proc.kill()
            pytest.fail("Herkos did not create its socket in 20 s")
        time.sleep(0.05)
    yield Herkos(path, docs, tmp_path / "state", proc, env)
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5)
    if proc.stderr:
        proc.stderr.close()


@pytest.fixture
def client(herkos: Herkos):
    c = Client(herkos.socket)
    yield c
    c.close()
