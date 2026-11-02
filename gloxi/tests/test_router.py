# SPDX-License-Identifier: GPL-3.0-or-later
import pytest
from fake_server import FakeLlamaServer
from helpers import ask, make_conversation, make_router

from gloxi.config import RouterPolicy
from gloxi.llm import ModelError
from gloxi.router import CloudNotAllowed

ON = RouterPolicy(cloud_allowed=True, long_message_chars=50)
MSG = [{"role": "user", "content": "привіт"}]


def test_cloud_off_by_default_keeps_everything_local():
    local, cloud = FakeLlamaServer("l"), FakeLlamaServer("c")
    router = make_router(local, cloud)
    assert router.decide("x" * 10_000).target == "local"
    with pytest.raises(CloudNotAllowed):
        router.decide("hi", force="cloud")


def test_mode_cloud_without_permission_is_refused():
    router = make_router(FakeLlamaServer(), FakeLlamaServer(), RouterPolicy(mode="cloud"))
    with pytest.raises(CloudNotAllowed):
        router.decide("hi")


def test_cloud_allowed_but_not_configured_stays_local():
    router = make_router(FakeLlamaServer(), None, ON)
    assert router.decide("x" * 1000).target == "local"


def test_auto_sends_short_local_and_long_to_cloud():
    router = make_router(FakeLlamaServer(), FakeLlamaServer(), ON)
    assert router.decide("коротко").target == "local"
    route = router.decide("x" * 51)
    assert route.target == "cloud"
    assert route.destination == "api.example.eu"


def test_force_wins_over_mode():
    router = make_router(
        FakeLlamaServer(), FakeLlamaServer(), RouterPolicy(mode="local", cloud_allowed=True)
    )
    assert router.decide("x" * 9999).target == "local"
    assert router.decide("hi", force="cloud").target == "cloud"
    router = make_router(
        FakeLlamaServer(), FakeLlamaServer(), RouterPolicy(mode="cloud", cloud_allowed=True)
    )
    assert router.decide("hi").target == "cloud"
    assert router.decide("hi", force="local").target == "local"


def test_messages_reach_only_the_chosen_model():
    local, cloud = FakeLlamaServer("локальна"), FakeLlamaServer("хмарна")
    conv = make_conversation(local, cloud=cloud, policy=ON)
    assert ask(conv, "коротко") == "локальна"
    assert ask(conv, "x" * 60) == "хмарна"
    assert len(local.requests) == 1
    assert len(cloud.requests) == 1
    assert conv.last_route.target == "cloud"


def test_outbound_filter_applies_only_to_cloud():
    seen = []

    def mask(messages):
        seen.append(len(messages))
        return [{**m, "content": m["content"].replace("Олена", "[NAME_1]")} for m in messages]

    local, cloud = FakeLlamaServer(), FakeLlamaServer()
    router = make_router(local, cloud, ON, outbound_filter=mask)
    assert router.masks_cloud
    list(router.stream([{"role": "user", "content": "Олена"}], router.decide("Олена")))
    list(router.stream([{"role": "user", "content": "Олена"}], router.decide("hi", force="cloud")))
    assert local.requests[0]["messages"][0]["content"] == "Олена"
    assert cloud.requests[0]["messages"][0]["content"] == "[NAME_1]"
    assert seen == [1]


def test_no_silent_fallback_to_cloud_when_local_fails():
    local, cloud = FakeLlamaServer(status=500), FakeLlamaServer()
    conv = make_conversation(local, cloud=cloud, policy=ON)
    with pytest.raises(ModelError):
        ask(conv, "коротко")
    assert cloud.requests == []
