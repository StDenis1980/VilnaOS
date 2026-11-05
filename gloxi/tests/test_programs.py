# SPDX-License-Identifier: GPL-3.0-or-later
"""«Programs for Gloxi» (ADR 0017, step 6): the switches live in Herkos."""

import pytest

from gloxi.programs import Change, Hidden, Program, Programs

herkos_client = pytest.importorskip("herkos.client")
HerkosError = herkos_client.HerkosError
HerkosUnavailable = herkos_client.HerkosUnavailable

LISTED = [
    {
        "tool": "org.example.notes",
        "description": "Notes",
        "enabled": True,
        "running": True,
        "hidden": [
            {"operation": "erase", "why": ["asks the assistant to ignore its instructions"]}
        ],
    },
    {"tool": "org.example.mail", "description": "Mail", "enabled": False, "running": False},
    "junk",
    {"description": "no tool"},
]


class FakeHerkos:
    def __init__(self, listed=None, answer=None, fail=None):
        self.listed = LISTED if listed is None else listed
        self.answer = answer
        self.fail = fail
        self.sets = []
        self.closed = 0

    def programs(self):
        if self.fail:
            raise self.fail
        return self.listed

    def set_program(self, tool, enabled, reason=""):
        if self.fail:
            raise self.fail
        self.sets.append((tool, enabled, reason))
        return self.answer or {"tool": tool, "enabled": enabled, "code": "done"}

    def close(self):
        self.closed += 1


def test_programs_are_listed_with_their_hidden_actions():
    fake = FakeHerkos()
    notes, mail = Programs(lambda: fake).list()
    assert notes == Program(
        "org.example.notes",
        "Notes",
        True,
        True,
        (Hidden("erase", ("asks the assistant to ignore its instructions",)),),
    )
    assert (mail.enabled, mail.hidden) == (False, ())
    assert notes.status() == "On" and "Off" in mail.status()
    assert fake.closed == 1


def test_a_program_that_is_on_but_not_running_says_when_it_starts():
    p = Program("org.example.notes", "Notes", enabled=True, running=False)
    assert "starts when Herkos starts again" in p.status()


@pytest.mark.parametrize(
    "fail", [HerkosUnavailable("gone"), HerkosError(-32601, "unknown method herkos.programs")]
)
def test_no_herkos_or_an_old_herkos_lists_nothing(fail):
    assert Programs(lambda: FakeHerkos(fail=fail)).list() == []


def test_no_connection_lists_nothing():
    def refuse():
        raise HerkosUnavailable("cannot reach Herkos")

    assert Programs(refuse).list() == []


def test_turning_on_goes_to_herkos_with_gloxi_s_reason():
    fake = FakeHerkos(answer={"enabled": True, "code": "done", "restart_needed": True})
    change = Programs(lambda: fake).set("org.example.mail", True)
    assert change == Change(enabled=True, restart_needed=True)
    ((tool, on, reason),) = fake.sets
    assert (tool, on) == ("org.example.mail", True) and "settings" in reason


def test_a_no_in_the_herkos_window_keeps_it_off():
    fake = FakeHerkos(answer={"enabled": False, "code": "denied_user"})
    change = Programs(lambda: fake).set("org.example.mail", True)
    assert (change.enabled, change.refused) == (False, True)


def test_turning_off_sends_no_reason():
    fake = FakeHerkos()
    assert Programs(lambda: fake).set("org.example.notes", False).enabled is False
    assert fake.sets == [("org.example.notes", False, "")]


def test_an_error_leaves_the_switch_where_it_was():
    fake = FakeHerkos(fail=HerkosError(-32602, "not a program"))
    change = Programs(lambda: fake).set("org.example.notes", True)
    assert change.enabled is False and "not a program" in change.error
