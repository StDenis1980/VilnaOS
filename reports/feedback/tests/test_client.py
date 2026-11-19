# SPDX-License-Identifier: GPL-3.0-or-later
"""The control socket client: requests as control.rs reads them, errors in plain words."""

import pytest

from vilna_feedback.client import Reportd, ReportdError


def test_each_operation_sends_one_request(fake, reportd):
    fake.answers = {
        "status": {"consent": "ask", "waiting": 1},
        "list": {"reports": [{"id": "a", "state": "waiting"}]},
        "show": lambda r: {"id": r["id"], "report": {"kind": "crash"}},
        "send": {"id": "a", "state": "sent"},
        "discard": {"discarded": True},
        "feedback": {"id": "f-1"},
        "preview": lambda r: {"feedback": {**r["feedback"], "channel": "window"}},
        "forget": {"deleted": 3},
        "system": {"system": "Vilna 0.1 · 16 GB"},
    }
    assert reportd.status()["consent"] == "ask"
    assert reportd.reports() == [{"id": "a", "state": "waiting"}]
    assert reportd.show("a")["report"] == {"kind": "crash"}
    assert reportd.send("a")["state"] == "sent"
    assert reportd.discard("a") is True
    assert reportd.feedback({"text": "hi"}) == "f-1"
    assert reportd.preview({"text": "hi"}) == {"text": "hi", "channel": "window"}
    assert reportd.forget() == 3
    assert reportd.system() == "Vilna 0.1 · 16 GB"
    assert [r["op"] for r in fake.requests] == [
        "status",
        "list",
        "show",
        "send",
        "discard",
        "feedback",
        "preview",
        "forget",
        "system",
    ]
    assert fake.requests[2] == {"op": "show", "id": "a"}
    assert fake.requests[5] == {"op": "feedback", "feedback": {"text": "hi"}}


def test_a_refusal_carries_the_services_reason(fake, reportd):
    fake.answers = {"send": {"error": "this report was sent already"}}
    with pytest.raises(ReportdError, match="sent already") as e:
        reportd.send("a")
    assert not e.value.offline


def test_no_service_is_offline(tmp_path):
    with pytest.raises(ReportdError) as e:
        Reportd(tmp_path / "missing", timeout=1).status()
    assert e.value.offline


def test_nonsense_is_an_error_not_a_crash(fake, reportd):
    fake.answers = {"status": b"[1, 2]\n", "list": b"not json\n"}
    with pytest.raises(ReportdError, match="nonsense"):
        reportd.status()
    with pytest.raises(ReportdError, match="nonsense"):
        reportd.reports()
