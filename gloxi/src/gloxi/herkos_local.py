# SPDX-License-Identifier: GPL-3.0-or-later
"""Herkos-lite inside the Gloxi Flatpak (decision of 2.11.2026).

On an ordinary Linux there is no system Herkos, so the Flatpak carries
Herkos-lite and the files tool, and Gloxi starts Herkos as a separate process
in the same sandbox. The socket stays private to the sandbox.

The sandbox sees no files of its own accord. The person chooses the folders
Gloxi may work in with the system folder chooser (the document portal); only
those folders become visible, and they are Herkos's ``allowed_roots``.

This is weaker than Vilna, where Herkos is a system service apart from the
user's session: here Gloxi and Herkos share one sandbox. The app description
says so.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path


class HerkosStartError(Exception):
    pass


def in_flatpak(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("FLATPAK_ID")) or Path("/.flatpak-info").exists()


def config_file(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "herkos" / "config.toml"


def socket_file(env: dict[str, str] | None = None) -> Path | None:
    env = os.environ if env is None else env
    runtime = env.get("XDG_RUNTIME_DIR")
    return Path(runtime) / "vilna" / "herkos.sock" if runtime else None


# -- allowed folders ----------------------------------------------------------------


def _load(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {value!r} to the Herkos config")


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Written by Gloxi: folders you chose for Gloxi, and Herkos settings.",
        *(f"{key} = {_toml_value(value)}" for key, value in data.items()),
    ]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def allowed_folders(path: Path | None = None) -> list[Path]:
    """The folders the person chose; none until they choose (not Herkos's default)."""
    data = _load(path or config_file())
    return [Path(p) for p in data.get("allowed_roots", [])]


def ensure_config(path: Path | None = None) -> None:
    """In the sandbox, start with no folders rather than Herkos's ``~/Documents``."""
    path = path or config_file()
    if not path.exists():
        _save(path, {"allowed_roots": []})


def set_allowed_folders(folders: list[Path], path: Path | None = None) -> None:
    path = path or config_file()
    data = _load(path)
    unique: list[str] = []
    for folder in folders:
        text = str(folder)
        if text not in unique:
            unique.append(text)
    data["allowed_roots"] = unique
    _save(path, data)


def add_folder(folder: Path, path: Path | None = None) -> list[Path]:
    folders = allowed_folders(path)
    if folder not in folders:
        folders.append(folder)
    set_allowed_folders(folders, path)
    return folders


def remove_folder(folder: Path, path: Path | None = None) -> list[Path]:
    folders = [f for f in allowed_folders(path) if f != folder]
    set_allowed_folders(folders, path)
    return folders


# -- the process --------------------------------------------------------------------


def alive(sock: Path) -> bool:
    """True if something accepts connections on the socket."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(str(sock))
        return True
    except OSError:
        return False


class LocalHerkos:
    """``herkos serve`` as a child of Gloxi, restarted when the folders change."""

    def __init__(self, command: list[str] | None = None, env: dict[str, str] | None = None):
        self.command = command or [sys.executable, "-m", "herkos", "serve", "--confirm", "auto"]
        self.env = env
        self._proc: subprocess.Popen | None = None

    @property
    def socket(self) -> Path | None:
        return socket_file(self.env if self.env is not None else None)

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, timeout: float = 15.0) -> None:
        if self.running():
            return
        sock = self.socket
        if sock is None:
            raise HerkosStartError("XDG_RUNTIME_DIR is not set")
        if alive(sock):
            return  # another Gloxi in this sandbox already started it
        sock.unlink(missing_ok=True)  # a stale socket from a crash
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self.env,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                code = self._proc.returncode
                raise HerkosStartError(f"Herkos stopped while starting (exit {code})")
            if alive(sock):
                return
            time.sleep(0.1)
        self.stop()
        raise HerkosStartError("Herkos did not start in time")

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def restart(self) -> None:
        self.stop()
        self.start()


def start_if_needed(tools_enabled: bool) -> LocalHerkos | None:
    """In the Flatpak, start the bundled Herkos unless one already listens."""
    if not tools_enabled or not in_flatpak():
        return None
    sock = socket_file()
    if sock is None:
        return None
    ensure_config()
    herkos = LocalHerkos()
    try:
        herkos.start()
    except HerkosStartError as exc:
        print(f"gloxi: {exc}", file=sys.stderr)
        return None
    return herkos
