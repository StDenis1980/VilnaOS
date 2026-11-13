# SPDX-License-Identifier: GPL-3.0-or-later
"""The first-run talk about a VPN (docs/adr/0027-vpn-proton-mullvad.md, section 2)."""

from __future__ import annotations

import io
import json

import pytest

from gloxi.cli import run_intro
from gloxi.vpn_intro import (
    LAUNCHABLE,
    MULLVAD_APP,
    PROTON_APP,
    PROTON_SIGNUP,
    REMIND_AFTER,
    VPN_TOOL,
    VpnIntro,
)


class FakeHerkos:
    """Answers like Herkos with the VPN tool (vilna-netguard) behind it."""

    def __init__(self, enabled=False, tunnels=(0,), deny=()):
        self.calls = []
        self.enabled = enabled
        self.tunnels = list(tunnels)
        self.deny = set(deny)

    def __call__(self, operation, arguments, reason, resources):
        self.calls.append(operation)
        assert reason  # always a reason for the Herkos window
        if operation in self.deny:
            return {"decision": "denied", "code": "denied_user", "message": "the user said no"}
        result = {"ok": True}
        if operation == "status":
            tunnels = self.tunnels.pop(0) if len(self.tunnels) > 1 else self.tunnels[0]
            result = {"enabled": self.enabled, "tunnels": tunnels}
        elif operation == "killswitch_on":
            self.enabled = True
        return {"decision": "allowed", "code": "allowed", "result": result}


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def make(tmp_path):
    def make(**kw):
        herkos = FakeHerkos(**kw)
        clock = Clock()
        return VpnIntro(herkos, path=tmp_path / "vpn.json", clock=clock), herkos, clock

    return make


def talk(intro, *answers):
    turns = [intro.start()]
    for a in answers:
        turns.append(intro.answer(a))
    return turns


def test_proton_sign_in_then_kill_switch(make):
    intro, herkos, _ = make(tunnels=(0, 1))
    first, opened, connected, end = talk(intro, "1", "1", "1")
    assert "already protected" in first.text and "even more protection" in first.text
    assert len(first.choices) == 4 and "Proton" in first.choices[0]
    assert opened.launch == PROTON_APP and opened.open_uri is None
    assert "not here" in opened.text  # the password goes only to Proton's app
    assert "kill switch" in connected.text
    assert end.done and "kill switch is on" in end.text
    assert "Connect at startup" in end.text
    assert herkos.calls == ["status", "status", "killswitch_on"]
    assert not intro.active and not intro.due()


def test_free_proton_account_opens_the_sign_up_page(make):
    intro, _, _ = make()
    turn = talk(intro, "2")[-1]
    assert turn.open_uri == PROTON_SIGNUP and turn.launch == PROTON_APP
    assert "Proton Drive" not in turn.text  # ADR 0027: Drive is never offered


def test_mullvad(make):
    intro, herkos, _ = make(tunnels=(0, 1))
    _, opened, _, end = talk(intro, "3", "1", "2")
    assert opened.launch == MULLVAD_APP and opened.open_uri is None
    assert "account number" in opened.text
    assert "stays off" in end.text and "Auto-connect" in end.text
    assert "killswitch_on" not in herkos.calls
    assert herkos.calls[:2] == ["status", "start_provider"]


def test_mullvad_service_refused(make):
    intro, _, _ = make(deny={"start_provider"})
    end = talk(intro, "3")[-1]
    assert end.done and end.launch is None and "the user said no" in end.text


def test_not_connected_yet_asks_again(make):
    intro, _, _ = make(tunnels=(0, 0, 1))
    turns = talk(intro, "1", "1", "1")
    assert "do not see a VPN connection" in turns[2].text
    assert turns[2].choices and intro.active
    assert "kill switch" in turns[3].text


def test_already_connected_goes_straight_to_the_kill_switch(make):
    intro, _, _ = make(tunnels=(2,))
    first, end = talk(intro, "1")
    assert "Shall I turn on the kill switch" in first.text
    # Gloxi does not know the provider, so no app-specific advice.
    assert end.done and "Connect at startup" not in end.text and "Auto-connect" not in end.text


def test_kill_switch_already_on_asks_nothing(make):
    intro, herkos, _ = make(enabled=True, tunnels=(1,))
    assert intro.start() is None
    assert not intro.due()
    assert herkos.calls == ["status"]


def test_later_asks_once_more_after_a_week(make, tmp_path):
    intro, _, clock = make()
    assert talk(intro, "4")[-1].done
    assert not intro.due()
    clock.now += REMIND_AFTER
    assert intro.due()
    assert talk(intro, "4")[-1].done
    clock.now += 10 * REMIND_AFTER
    assert not intro.due()  # the reminder was the last time
    assert json.loads((tmp_path / "vpn.json").read_text())["status"] == "declined"
    assert intro.start() is not None  # /vpn still works


def test_later_while_waiting_for_the_app(make):
    intro, _, clock = make()
    end = talk(intro, "1", "2")[-1]
    assert end.done and "/vpn" in end.text
    clock.now += REMIND_AFTER
    assert intro.due()


def test_a_denial_ends_the_talk_politely(make):
    intro, _, _ = make(tunnels=(0, 1), deny={"killswitch_on"})
    end = talk(intro, "1", "1", "1")[-1]
    assert end.done and "the user said no" in end.text and "/vpn" in end.text
    assert not intro.active


def test_herkos_not_running_does_not_count_as_asked(tmp_path):
    def broken(*a):
        raise OSError("no socket")

    intro = VpnIntro(broken, path=tmp_path / "vpn.json")
    turn = intro.start()
    assert turn.done and "Herkos" in turn.text
    assert intro.due()


def test_bad_answers_ask_again(make):
    intro, _, _ = make()
    intro.start()
    assert "1 to 4" in intro.answer("maybe").text
    assert intro.active


def test_only_the_providers_apps_can_be_launched():
    assert {PROTON_APP, MULLVAD_APP} == LAUNCHABLE
    assert PROTON_SIGNUP.startswith("https://")


def test_every_operation_is_in_the_herkos_manifest():
    pytest.importorskip("herkos")
    from herkos.manifest import ManifestRegistry

    manifest = ManifestRegistry.builtin().get(VPN_TOOL)
    assert manifest is not None
    for operation in ("status", "killswitch_on", "start_provider"):
        assert manifest.operation(operation) is not None


def test_terminal(make):
    intro, _, _ = make()
    answers = iter(["2", "2"])
    opened = []
    out = io.StringIO()
    run_intro(intro, lambda prompt: next(answers), out, opened.append)
    text = out.getvalue()
    assert "Gloxi: Your connection is already protected" in text
    assert opened == [PROTON_SIGNUP]
    assert PROTON_APP in text
