# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from vilna_backup import keys
from vilna_backup.plan import Layout
from vilna_backup.service import Backup
from vilna_backup.settings import load_settings, parse_destination, save_settings

needs_restic = pytest.mark.skipif(shutil.which("restic") is None, reason="restic not installed")


class FakeSystemd:
    """Records systemctl calls instead of running them."""

    def __init__(self, ok: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.ok = ok

    def __call__(self, cmd, **kwargs):
        if cmd[0] != "systemctl":
            raise FileNotFoundError(cmd[0])  # flatpak, dconf: not on the test machine
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0 if self.ok else 1, "", "no bus")


def make_home(root: Path) -> Path:
    home = root / "home"
    for rel, text in {
        ".config/dconf/user": "dconf",
        ".config/gloxi/config.toml": "[router]\n",
        ".local/share/gloxi/memory.json": "{}",
        ".local/share/gloxi/history/2026-11-02.jsonl": "chat",
        ".local/state/herkos/journal.jsonl": "{}\n",
        ".local/state/herkos/backups/files/x/meta.json": "{}",
        ".var/app/org.keepassxc.KeePassXC/config/keepassxc/keepassxc.ini": "",
        ".var/app/org.gnome.Maps/cache/tiles.bin": "x" * 100,
        ".cache/big.bin": "x" * 1000,
        ".local/share/keyrings/login.keyring": "secret",
        ".ssh/id_ed25519": "secret",
        "Documents/zvit.txt": "звіт",
        "Documents/models/qwen.gguf": "w" * 500,
        "Pictures/2019/a.jpg": "p" * 3000,
        "Pictures/2024/b.jpg": "p" * 200,
        "Videos/film.mkv": "v" * 5000,
        "Downloads/setup.iso": "d" * 4000,
        "Passwords.kdbx": "kdbx",
    }.items():
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return home


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return make_home(tmp_path)


@pytest.fixture
def layout(home: Path) -> Layout:
    return Layout.from_env({}, home=home)


@pytest.fixture
def systemd() -> FakeSystemd:
    return FakeSystemd()


@pytest.fixture
def backup(tmp_path: Path, layout: Layout, systemd: FakeSystemd, monkeypatch) -> Backup:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(layout.config))
    monkeypatch.setenv("HOME", str(layout.home))
    monkeypatch.setenv("RESTIC_CACHE_DIR", str(tmp_path / "restic-cache"))
    return Backup(
        layout=layout,
        store=keys.FileStore(tmp_path / "keys"),
        settings_file=layout.config / "vilna-backup" / "settings.json",
        state=layout.state / "vilna-backup",
        run_cmd=systemd,
    )


@pytest.fixture(scope="session")
def template(tmp_path_factory) -> tuple[Path, str]:
    """One repository set up once (restic's key derivation is slow on purpose)."""
    root = tmp_path_factory.mktemp("template")
    layout = Layout.from_env({}, home=make_home(root))
    b = Backup(
        layout=layout,
        store=keys.FileStore(root / "keys"),
        settings_file=root / "settings.json",
        state=root / "state",
        run_cmd=FakeSystemd(),
    )
    recovery = b.set_up(parse_destination(str(root / "usb" / "vilna")))
    b.confirm_recovery(recovery)
    return root, recovery


@pytest.fixture
def ready(backup: Backup, tmp_path: Path, template) -> tuple[Backup, str]:
    """Backups set up on a local folder, the recovery sheet confirmed."""
    root, recovery = template
    shutil.copytree(root / "usb", tmp_path / "usb")
    shutil.copytree(root / "keys", tmp_path / "keys")
    s = load_settings(root / "settings.json")
    save_settings(
        s.with_changes(destination=parse_destination(str(tmp_path / "usb" / "vilna"))),
        backup.settings_file,
    )
    return backup, recovery
