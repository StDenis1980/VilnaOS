# SPDX-License-Identifier: GPL-3.0-or-later
"""The internet of the Gloxi service (ADR 0025, section 3)."""

import io
import json
import subprocess

import pytest
from fake_server import FakeLlamaServer
from helpers import make_router

from gloxi.config import RouterPolicy
from gloxi.network import CloudNetwork, Network, Server
from gloxi.router import NetworkRefused
from gloxi.tools import NOT_GENERIC

ON = RouterPolicy(cloud_allowed=True, long_message_chars=50)
MSG = [{"role": "user", "content": "привіт"}]


class Systemctl:
    """systemd's IPAddressAllow of one unit, as systemctl shows and sets it."""

    def __init__(self, allowed="127.0.0.0/8 ::1/128", fail=False):
        self.allowed = allowed
        self.fail = fail
        self.cmds = []

    def __call__(self, cmd):
        self.cmds.append(cmd)
        if self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "Access denied\n")
        if cmd[1] == "show":
            return subprocess.CompletedProcess(cmd, 0, self.allowed + "\n", "")
        values = [c.split("=", 1)[1] for c in cmd[3:]]
        if values == ["any"]:
            self.allowed = "::/0 0.0.0.0/0"
        elif values == ["", "localhost"]:
            self.allowed = "127.0.0.0/8 ::1/128"
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_the_tool_opens_and_closes_only_that_persons_gloxi():
    sc = Systemctl()
    net = Network(1001, runner=sc)
    assert net.status() == {"cloud": False}
    assert net.allow_cloud() == {"cloud": True}
    assert sc.cmds[1] == ["systemctl", "set-property", "gloxi@1001.service", "IPAddressAllow=any"]
    assert net.block_cloud() == {"cloud": False}
    assert sc.cmds[3] == [
        "systemctl",
        "set-property",
        "gloxi@1001.service",
        "IPAddressAllow=",
        "IPAddressAllow=localhost",
    ]
    assert {c[-1] for c in sc.cmds if c[1] == "show"} == {"gloxi@1001.service"}


@pytest.mark.parametrize("uid", [0, 999, 60000, 65534])
def test_the_tool_is_only_for_people(uid):
    with pytest.raises(ValueError):
        Network(uid, runner=Systemctl())


def call(server, name, arguments=None, id_=1):
    msg = {"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": {"name": name}}
    if arguments is not None:
        msg["params"]["arguments"] = arguments
    return server.handle(msg)["result"]


def test_the_server_speaks_mcp_and_takes_no_arguments():
    sc = Systemctl()
    server = Server(Network(1000, runner=sc))
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    out = io.StringIO()
    server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in lines) + "junk\n"), out)
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert answers[0]["result"]["serverInfo"]["name"] == "vilna-mcp-gloxi-network"
    assert [t["name"] for t in answers[1]["result"]["tools"]] == [
        "status",
        "allow_cloud",
        "block_cloud",
    ]
    assert answers[2]["error"]["code"] == -32700

    assert call(server, "allow_cloud")["structuredContent"] == {"cloud": True}
    # Nothing in a request can point the tool at another unit or value.
    refused = call(server, "allow_cloud", {"unit": "sshd.service"})
    assert refused["isError"]
    assert call(server, "reboot")["isError"]
    assert all(c[-1] != "sshd.service" for c in sc.cmds)


def test_a_failing_systemctl_is_a_tool_error():
    server = Server(Network(1000, runner=Systemctl(fail=True)))
    result = call(server, "allow_cloud")
    assert result["isError"]
    assert "Access denied" in result["content"][0]["text"]


class Herkos:
    def __init__(self, decision="allowed", cloud=False, down=False):
        self.decision = decision
        self.cloud = cloud
        self.down = down
        self.asked = []

    def __call__(self, operation, reason):
        self.asked.append(operation)
        if self.down:
            raise ConnectionRefusedError("no herkosd")
        if self.decision != "allowed":
            return {"decision": "denied", "code": self.decision, "message": "no"}
        if operation == "allow_cloud":
            self.cloud = True
        if operation == "block_cloud":
            self.cloud = False
        return {"decision": "allowed", "code": "allowed", "result": {"cloud": self.cloud}}


def test_gloxi_asks_herkos_once_before_the_first_cloud_message():
    herkos = Herkos()
    cloud = FakeLlamaServer("хмарна")
    router = make_router(FakeLlamaServer(), cloud, ON, network=CloudNetwork(herkos))
    for _ in range(2):
        route = router.decide("x" * 60)
        assert "".join(router.stream(MSG, route)) == "хмарна"
    assert herkos.asked == ["allow_cloud"]


def test_local_messages_never_ask_for_the_internet():
    herkos = Herkos()
    router = make_router(FakeLlamaServer("л"), FakeLlamaServer(), ON, network=CloudNetwork(herkos))
    assert "".join(router.stream(MSG, router.decide("коротко"))) == "л"
    assert herkos.asked == []


@pytest.mark.parametrize("herkos", [Herkos(decision="denied_user"), Herkos(down=True)])
def test_without_a_yes_nothing_goes_to_the_cloud(herkos):
    cloud = FakeLlamaServer()
    router = make_router(FakeLlamaServer(), cloud, ON, network=CloudNetwork(herkos))
    with pytest.raises(NetworkRefused) as exc:
        list(router.stream(MSG, router.decide("x" * 60)))
    assert str(exc.value)
    assert cloud.requests == []


def test_closing_asks_to_block_only_when_the_way_is_open():
    herkos = Herkos(cloud=False)
    assert CloudNetwork(herkos).close()
    assert herkos.asked == ["status"]
    herkos = Herkos(cloud=True)
    assert CloudNetwork(herkos).close()
    assert herkos.asked == ["status", "block_cloud"]
    assert herkos.cloud is False
    assert not CloudNetwork(Herkos(down=True)).close()


def test_the_model_is_never_offered_the_network_tool():
    assert "org.vilna.gloxi.network" in NOT_GENERIC


def test_the_window_hears_why_the_cloud_did_not_answer():
    from helpers import make_conversation
    from test_gui_controller import RecordingView

    from gloxi.gui.controller import ChatController

    view = RecordingView()
    conv = make_conversation(
        FakeLlamaServer(),
        cloud=FakeLlamaServer("хмарна"),
        policy=ON,
        network=CloudNetwork(Herkos(decision="denied_user")),
    )
    ctl = ChatController(conv, view, runner=lambda job: job())
    assert ctl.submit("/cloud привіт")
    errors = [e[1] for e in view.events if e[0] == "error"]
    assert errors == ["Gloxi may not connect to the internet, so the cloud model cannot answer."]
    assert view.answer() == ""
    assert ctl.busy is False
