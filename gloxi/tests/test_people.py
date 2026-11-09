# SPDX-License-Identifier: GPL-3.0-or-later
"""vilna-people-sync (ADR 0025): a Gloxi for every person, from /etc/passwd."""

import tomllib

from gloxi.people import Person, Sync, gloxi_users, people, system_toml

PASSWD = """\
root:x:0:0:root:/root:/bin/bash
herkos:x:476:476:Herkos permission gate:/var/lib/herkos:/usr/sbin/nologin
gdm:x:477:477:Gnome Display Manager daemon:/var/lib/gdm:/usr/sbin/nologin
denys:x:1000:100:Denys:/home/denys:/bin/bash
anna:x:1001:100:Anna:/home/anna:/usr/bin/zsh
service:x:1002:100::/home/service:/usr/sbin/nologin
nobody:x:65534:65534:nobody:/var/lib/nobody:/bin/bash
broken line
bad"name:x:1003:100::/home/x:/bin/bash
"""


class Recorder:
    def __init__(self, fail=()):
        self.cmds = []
        self.fail = set(fail)

    def __call__(self, cmd):
        self.cmds.append(" ".join(cmd))
        return 1 if cmd[0] in self.fail else 0


def make(tmp_path, passwd, *, live=True, fail=()):
    (tmp_path / "passwd").write_text(passwd)
    rec = Recorder(fail)
    sync = Sync(
        passwd=tmp_path / "passwd",
        config=tmp_path / "herkos" / "system.toml",
        runner=rec,
        systemd_running=lambda: live,
    )
    return sync, rec


def test_people_are_login_accounts_in_the_human_range():
    assert people(PASSWD) == [Person("denys", 1000), Person("anna", 1001)]


def test_system_toml_lists_each_person_and_their_gloxi():
    text = system_toml([Person("denys", 1000), Person("anna", 1001)])
    data = tomllib.loads(text)
    assert data["person"] == [
        {"user": "denys", "gloxi": "gloxi-1000"},
        {"user": "anna", "gloxi": "gloxi-1001"},
    ]


def test_first_run_creates_users_enables_sockets_and_restarts_herkos(tmp_path):
    sync, rec = make(tmp_path, PASSWD)
    assert sync.apply() == 0
    assert any(
        c.startswith("useradd --system --gid gloxi") and c.endswith(" gloxi-1000") for c in rec.cmds
    )
    assert any(c.endswith(" gloxi-1001") for c in rec.cmds if c.startswith("useradd"))
    assert "systemctl enable gloxi@1000.socket" in rec.cmds
    assert "systemctl start gloxi@1001.socket" in rec.cmds
    assert rec.cmds[-1] == "systemctl try-restart herkosd.service"
    data = tomllib.loads((tmp_path / "herkos" / "system.toml").read_text())
    assert [p["user"] for p in data["person"]] == ["denys", "anna"]
    # No personal names in the system users' comments.
    assert not any("Denys" in c for c in rec.cmds)


def test_nothing_changes_the_second_time(tmp_path):
    passwd = PASSWD + (
        "gloxi-1000:x:470:469:Gloxi for uid 1000:/var/lib/gloxi/1000:/usr/sbin/nologin\n"
        "gloxi-1001:x:471:469:Gloxi for uid 1001:/var/lib/gloxi/1001:/usr/sbin/nologin\n"
    )
    sync, rec = make(tmp_path, passwd)
    sync.apply()
    rec.cmds.clear()
    assert sync.apply() == 0
    assert not any(c.startswith(("useradd", "userdel")) for c in rec.cmds)
    assert "systemctl try-restart herkosd.service" not in rec.cmds


def test_a_person_who_left_loses_their_gloxi(tmp_path):
    passwd = PASSWD + "gloxi-1005:x:472:469::/var/lib/gloxi/1005:/usr/sbin/nologin\n"
    sync, rec = make(tmp_path, passwd)
    sync.apply()
    assert "systemctl disable gloxi@1005.socket" in rec.cmds
    assert "systemctl stop gloxi@1005.socket gloxi@1005.service" in rec.cmds
    assert "userdel gloxi-1005" in rec.cmds


def test_in_an_image_build_nothing_is_started(tmp_path):
    sync, rec = make(tmp_path, PASSWD, live=False)
    assert sync.apply() == 0
    assert "systemctl enable gloxi@1000.socket" in rec.cmds
    assert not any(
        c.startswith(("systemctl start", "systemctl stop", "systemctl try")) for c in rec.cmds
    )
    assert (tmp_path / "herkos" / "system.toml").exists()


def test_a_failed_command_is_reported_but_the_rest_still_runs(tmp_path):
    sync, rec = make(tmp_path, PASSWD, fail={"useradd"})
    assert sync.apply() == 1
    assert "systemctl enable gloxi@1001.socket" in rec.cmds
    assert (tmp_path / "herkos" / "system.toml").exists()


def test_gloxi_users_are_recognised_by_name_only():
    text = (
        "gloxi-1000:x:470:469::/:/sbin/nologin\ngloxi:x:1:1::/:/sbin/nologin\ngloxi-x:x:2:2::/:/x\n"
    )
    assert gloxi_users(text) == {1000}
