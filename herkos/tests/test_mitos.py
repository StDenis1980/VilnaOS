# SPDX-License-Identifier: GPL-3.0-or-later
import json

import pytest

from herkos.mitos import Journal, JournalError, verify


def lines(path):
    return path.read_text().splitlines()


def test_chain_and_verify(tmp_path):
    p = tmp_path / "j.jsonl"
    j = Journal(p)
    for i in range(3):
        j.append("request", {"n": i}, action_id=f"a{i}")
    assert verify(p).ok and verify(p).records == 3
    # A new instance continues the chain.
    Journal(p).append("result", {"ok": True}, action_id="a2")
    assert verify(p).records == 4
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_changed_record_is_found(tmp_path):
    p = tmp_path / "j.jsonl"
    j = Journal(p)
    for i in range(3):
        j.append("request", {"n": i})
    recs = lines(p)
    rec = json.loads(recs[1])
    rec["data"]["n"] = 99
    recs[1] = json.dumps(rec)
    p.write_text("\n".join(recs) + "\n")
    result = verify(p)
    assert not result.ok and result.line == 2


def test_removed_record_is_found(tmp_path):
    p = tmp_path / "j.jsonl"
    j = Journal(p)
    for i in range(3):
        j.append("request", {"n": i})
    recs = lines(p)
    p.write_text("\n".join([recs[0], recs[2]]) + "\n")
    assert not verify(p).ok


def test_truncated_tail_is_not_found_but_rewrite_is(tmp_path):
    # Cutting off the last records keeps a valid chain: that is a known limit
    # of a local hash chain (documented in herkos/docs/protocol.md).
    p = tmp_path / "j.jsonl"
    j = Journal(p)
    for i in range(3):
        j.append("request", {"n": i})
    p.write_text("\n".join(lines(p)[:2]) + "\n")
    assert verify(p).ok
    # ...but the running journal notices that its file changed under it.
    with pytest.raises(JournalError):
        j.append("request", {"n": 3})


def test_broken_journal_refuses_to_open(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_text("not json\n")
    with pytest.raises(JournalError):
        Journal(p)


def test_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        Journal(tmp_path / "j.jsonl").append("note", {})
