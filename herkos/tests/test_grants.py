# SPDX-License-Identifier: GPL-3.0-or-later
import json

import pytest

from herkos.grants import GrantStore
from herkos.risk import Level

T, OP = "org.vilna.mcp.files", "read"


def test_permanent_grant_survives_restart(tmp_path):
    p = tmp_path / "permissions.json"
    docs = tmp_path / "Documents"
    g = GrantStore(p).add(T, OP, [docs], Level.R1, "permanent")
    again = GrantStore(p)
    assert again.find(T, OP, [docs], Level.R1).id == g.id
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_grant_does_not_cover_other_roots_or_higher_levels(tmp_path):
    store = GrantStore(tmp_path / "p.json")
    docs, other = tmp_path / "Documents", tmp_path / "Other"
    store.add(T, "write", [docs], Level.R2, "permanent")
    assert store.find(T, "write", [other], Level.R2) is None
    assert store.find(T, "write", [None], Level.R2) is None
    assert store.find(T, "write", [docs], Level.R3) is None
    assert store.find(T, "read", [docs], Level.R1) is None


def test_no_permanent_grant_above_r2(tmp_path):
    with pytest.raises(ValueError):
        GrantStore(tmp_path / "p.json").add(T, "delete", [], Level.R3, "permanent")


def test_session_grant_expires_when_idle(tmp_path):
    now = [1000.0]
    store = GrantStore(tmp_path / "p.json", clock=lambda: now[0])
    store.add(T, OP, [tmp_path], Level.R1, "session")
    now[0] += 29 * 60
    assert store.find(T, OP, [tmp_path], Level.R1) is not None
    now[0] += 29 * 60  # used a minute ago, so still alive
    assert store.find(T, OP, [tmp_path], Level.R1) is not None
    now[0] += 31 * 60
    assert store.find(T, OP, [tmp_path], Level.R1) is None
    assert not (tmp_path / "p.json").exists()


def test_revoke_from_another_process_is_seen(tmp_path):
    p = tmp_path / "p.json"
    running = GrantStore(p)
    g = running.add(T, OP, [tmp_path], Level.R1, "permanent")
    assert GrantStore(p).revoke(g.id)  # e.g. `herkos permissions revoke`
    assert running.find(T, OP, [tmp_path], Level.R1) is None


def test_unreadable_file_means_no_permissions(tmp_path):
    p = tmp_path / "p.json"
    p.write_text("{broken")
    store = GrantStore(p)
    assert store.load_error and store.all() == []


def test_hand_edited_r3_permanent_grant_is_ignored(tmp_path):
    p = tmp_path / "p.json"
    item = {
        "id": "x",
        "tool": T,
        "operation": "delete",
        "roots": [str(tmp_path)],
        "level": "R3",
        "duration": "permanent",
        "created": 0,
    }
    p.write_text(json.dumps({"version": 1, "permissions": [item]}))
    assert GrantStore(p).find(T, "delete", [tmp_path], Level.R3) is None
