# SPDX-License-Identifier: GPL-3.0-or-later
from vilna_backup import schedule


def test_units(tmp_path, systemd):
    note = schedule.apply("daily", run=systemd, folder=tmp_path)
    assert note == "timer on: daily"
    timer = (tmp_path / "vilna-backup.timer").read_text()
    assert "OnCalendar=daily" in timer and "Persistent=true" in timer
    service = (tmp_path / "vilna-backup.service").read_text()
    assert "run --scheduled" in service and "Type=oneshot" in service
    assert "OnCalendar=weekly" in (tmp_path / "vilna-backup-check.timer").read_text()
    assert ["systemctl", "--user", "enable", "--now", "vilna-backup.timer"] in systemd.calls


def test_off_removes_the_timer(tmp_path, systemd):
    schedule.apply("hourly", run=systemd, folder=tmp_path)
    assert schedule.apply("off", run=systemd, folder=tmp_path) == "timer off"
    assert not (tmp_path / "vilna-backup.timer").exists()
    assert (tmp_path / "vilna-backup-check.timer").exists()


def test_without_systemd(tmp_path):
    def missing(cmd, **kw):
        raise FileNotFoundError("systemctl")

    assert "not reachable" in schedule.apply("weekly", run=missing, folder=tmp_path)
    assert not schedule.start_now(run=missing)
