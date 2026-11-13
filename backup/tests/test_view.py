# SPDX-License-Identifier: GPL-3.0-or-later
"""The Backups window's texts and form, without GTK."""

from __future__ import annotations

import ast
import time
from importlib import resources
from pathlib import Path

import pytest

from vilna_backup import i18n, view
from vilna_backup.settings import Destination


@pytest.fixture(autouse=True)
def english():
    i18n.use(["en"])
    yield
    i18n.use(["en"])


NOW = time.mktime((2026, 11, 9, 18, 0, 0, 0, 0, -1))


def test_sizes():
    assert view.human_size(0) == "0 B"
    assert view.human_size(999) == "999 B"
    assert view.human_size(3_200_000_000) == "3.2 GB"
    assert view.human_size(86_000_000_000) == "86 GB"
    assert view.human_size(None) == "—"


def test_preset_sizes_add_documents_to_settings():
    groups = [
        {"group": "settings", "bytes": 50},
        {"group": "documents", "bytes": 1000},
        {"group": "desktop", "bytes": 10},
        {"group": "pictures", "bytes": 200},
        {"group": "videos", "bytes": 9000},
        {"group": "home", "bytes": 20000},
    ]
    assert view.preset_sizes(groups) == {"settings": 50, "documents": 1260, "home": 20000}


def test_when():
    assert view.when(NOW - 3600, NOW) == "today at 17:00"
    assert view.when(NOW - 86400, NOW) == "yesterday at 18:00"
    assert view.when(time.mktime((2026, 11, 3, 9, 5, 0, 0, 0, -1)), NOW) == "3 November at 09:05"
    assert view.when(None, NOW) == "—"
    assert view.when("not a time", NOW) == "—"


def test_when_reads_restic_times():
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW - 60)) + ".123456789"
    assert view.when(stamp + time.strftime("%z")[:3] + ":" + time.strftime("%z")[3:], NOW) == (
        "today at 17:59"
    )


def test_summary():
    off = view.summary({"enabled": False})
    assert (off.title, off.ok) == ("Backups are off", False)
    first = view.summary({"enabled": True, "schedule": "daily", "last_backup": None})
    assert first.detail == "No backup yet · every day"
    good = view.summary(
        {
            "enabled": True,
            "schedule": "hourly",
            "last_backup": {"ok": True, "time": NOW - 120, "bytes": 3_200_000_000},
        },
        NOW,
    )
    assert good.ok and good.detail == "Last backup today at 17:58 · 3.2 GB · every hour"
    bad = view.summary(
        {"enabled": True, "last_backup": {"ok": False, "time": NOW, "message": "disk full"}}, NOW
    )
    assert not bad.ok and bad.detail == "today at 18:00: disk full"
    running = view.summary({"enabled": True, "running": {"percent": 41.6}})
    assert running.percent == 41.6 and running.detail == "42 % done"


def test_snapshot_rows_newest_first():
    rows = view.snapshot_rows(
        [
            {"id": "aaaa1111", "time": "2026-11-08T10:00:00+00:00", "bytes": 10, "files": 2},
            {"id": "bbbb2222", "time": "2026-11-09T10:00:00+00:00", "bytes": 5000, "files": 1204},
        ],
        NOW,
    )
    assert [r[0] for r in rows] == ["bbbb2222", "aaaa1111"]
    assert rows[0][2] == "5.0 kB · 1 204 files"
    assert rows[0][1][0].isupper()


def test_place_text():
    assert view.place_text("disk: /run/media/me/USB/vilna") == "Disk: /run/media/me/USB/vilna"
    assert view.place_text("SFTP: me@nas:/b") == "SFTP: me@nas:/b"
    assert view.place_text(None) == "—"
    i18n.use(["uk"])
    assert view.place_text("Vilna server: https://x/") == "Сервер Vilna: https://x/"


def test_recovery_key_for_reading():
    key = "ABCD-EFGH-JKMN-PQRS-TVWX-YZ01-2345-6789"
    assert view.key_for_reading(key) == "ABCD  EFGH  JKMN  PQRS\nTVWX  YZ01  2345  6789"
    assert view.key_looks_complete(key.lower().replace("-", " "))
    assert not view.key_looks_complete("ABCD-EFGH")


def test_form_disk(tmp_path):
    dest, creds = view.form_destination("disk", folder=str(tmp_path))
    assert dest == Destination("local", str(tmp_path)) and creds == {}
    with pytest.raises(view.FormError):
        view.form_destination("disk")


def test_form_server():
    dest, creds = view.form_destination("server", address="sftp:me@nas:/backups")
    assert dest.kind == "sftp" and creds == {}
    dest, creds = view.form_destination(
        "server", address="s3:https://s3.example.eu/bucket", login="id", password="secret"
    )
    assert dest.kind == "s3"
    assert creds == {"AWS_ACCESS_KEY_ID": "id", "AWS_SECRET_ACCESS_KEY": "secret"}
    for bad in ("", "/home/me", "s3:http://example.eu/b", "ftp:x"):
        with pytest.raises(view.FormError):
            view.form_destination("server", address=bad, login="a", password="b")
    with pytest.raises(view.FormError, match="login"):
        view.form_destination("server", address="rest:https://vilna.example/me/")
    with pytest.raises(view.FormError):
        view.form_destination("cloud")


def _msgids() -> set[str]:
    src = Path(view.__file__).parent
    ids = set(view.MONTHS)
    for name in ("view.py", "gui.py"):
        for node in ast.walk(ast.parse((src / name).read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "_"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                ids.add(node.args[0].value)
    return ids


def test_every_window_text_has_a_ukrainian_translation():
    po = resources.files("vilna_backup.locale") / "uk.po"
    catalog = i18n.parse_po(po.read_text(encoding="utf-8"))
    missing = sorted(_msgids() - catalog.keys())
    assert not missing, missing


def test_ukrainian():
    i18n.use(["uk"])
    assert view.human_size(3_200_000_000) == "3,2 ГБ"
    assert view.when(time.mktime((2026, 11, 3, 9, 5, 0, 0, 0, -1)), NOW) == "3 листопада о 09:05"
    assert view.preset_title("documents") == "Налаштування і документи"


def test_replaced_files_read_inside_the_home_folder():
    replaced = [f"/home/a/Documents/{i}.odt" for i in range(15)]
    text = view.replaced_for_reading(replaced, "/home/a", limit=3)
    assert text.splitlines() == [
        "Documents/0.odt",
        "Documents/1.odt",
        "Documents/2.odt",
        "…and 12 more",
    ]
    assert view.replaced_for_reading(["/elsewhere/x"], "/home/a") == "/elsewhere/x"


def test_in_place_texts():
    assert "Replace 2 files" in view.in_place_heading(2, 5)
    assert view.in_place_heading(0, 5) == "Bring back 5 files?"
    assert "already" in view.in_place_heading(0, 0)
    assert "Nothing is replaced" in view.in_place_body(0, 3)
    assert "old version" in view.in_place_body(1, 0)
    assert "nothing was replaced" in view.in_place_done({"restored": False})
    done = view.in_place_done({"restored": True, "files": 4, "replaced": 1, "keyrings": ["x"]})
    assert "4 files back, 1 replaced" in done and "key ring" in done
