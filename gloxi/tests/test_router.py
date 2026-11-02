# SPDX-License-Identifier: GPL-3.0-or-later
import pytest
from fake_server import FakeLlamaServer
from helpers import ask, make_conversation, make_router

from gloxi.chat import Conversation
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


class FakeGuard:
    def __init__(self):
        self.masked = 0

    def mask_messages(self, messages):
        self.masked += 1
        return [{**m, "content": m["content"].replace("Олена", "[NAME_1]")} for m in messages]

    def unmask_stream(self, chunks):
        yield "".join(chunks).replace("[NAME_1]", "Олена")


def test_guard_applies_only_to_cloud():
    guards = []

    def factory():
        guards.append(FakeGuard())
        return guards[-1]

    local, cloud = FakeLlamaServer("Олена"), FakeLlamaServer("Привіт, [NAME_1]")
    router = make_router(local, cloud, ON, guard_factory=factory)
    assert router.masks_cloud
    msgs = [{"role": "user", "content": "Олена"}]
    assert "".join(router.stream(msgs, router.decide("Олена"))) == "Олена"
    assert "".join(router.stream(msgs, router.decide("hi", force="cloud"))) == "Привіт, Олена"
    assert local.requests[0]["messages"][0]["content"] == "Олена"
    assert cloud.requests[0]["messages"][0]["content"] == "[NAME_1]"
    assert guards[0].masked == 1


def test_new_conversation_gets_a_new_guard():
    made = []
    router = make_router(FakeLlamaServer(), guard_factory=lambda: made.append(1) or FakeGuard())
    conv = Conversation(router, "SYS")
    first = router.guard
    conv.reset()
    assert router.guard is not first
    assert len(made) == 2


def test_real_gateway_masks_and_restores():
    pytest.importorskip("privacy_gateway")
    from gloxi.privacy import guard_factory

    local = FakeLlamaServer()
    cloud = FakeLlamaServer("Готово, лист для [EMAIL_1].")
    conv = make_conversation(local, cloud=cloud, policy=ON, guard_factory=guard_factory(ON))
    answer = ask(conv, "Напиши на olena@example.com", force="cloud")
    assert answer == "Готово, лист для olena@example.com."
    sent = cloud.requests[0]["messages"][-1]["content"]
    assert sent == "Напиши на [EMAIL_1]"
    # The history keeps the real values; only the wire sees placeholders.
    assert conv.history[-1]["content"] == answer


def test_masking_off_sends_as_is():
    from gloxi.privacy import guard_factory

    assert guard_factory(RouterPolicy(mask_cloud=False)) is None


def test_no_silent_fallback_to_cloud_when_local_fails():
    local, cloud = FakeLlamaServer(status=500), FakeLlamaServer()
    conv = make_conversation(local, cloud=cloud, policy=ON)
    with pytest.raises(ModelError):
        ask(conv, "коротко")
    assert cloud.requests == []


def test_mode_can_change_in_the_middle_of_a_conversation():
    router = make_router(
        FakeLlamaServer(), cloud=FakeLlamaServer(), policy=RouterPolicy(cloud_allowed=True)
    )
    assert router.decide("hi").target == "local"
    router.set_mode("cloud")
    assert router.mode == "cloud"
    assert router.decide("hi").target == "cloud"
    assert router.decide("hi", force="local").target == "local"  # /local still wins
    router.set_mode("local")
    assert router.decide("x" * 5000).target == "local"
    with pytest.raises(ValueError):
        router.set_mode("fast")


def test_cloud_mode_needs_the_cloud_turned_on():
    router = make_router(FakeLlamaServer(), cloud=FakeLlamaServer())
    with pytest.raises(CloudNotAllowed):
        router.set_mode("cloud")
    assert router.mode == "auto"
