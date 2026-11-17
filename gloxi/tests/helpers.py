# SPDX-License-Identifier: GPL-3.0-or-later
from datetime import date

from fake_server import FakeLlamaServer

from gloxi.chat import Conversation
from gloxi.config import ModelEndpoint, RouterPolicy
from gloxi.llm import ChatClient
from gloxi.router import Router

LOCAL = ModelEndpoint(url="http://127.0.0.1:8080/v1", model="local")
CLOUD = ModelEndpoint(url="https://api.example.eu/v1", model="big", api_key="sk-test")

# A fixed day, so requests do not depend on when the tests run.
SYSTEM = "SYS\n\nToday is Tuesday, 2026-11-17."


def TODAY() -> date:  # noqa: N802
    return date(2026, 11, 17)


def make_router(
    local: FakeLlamaServer,
    cloud: FakeLlamaServer | None = None,
    policy: RouterPolicy | None = None,
    **kwargs,
) -> Router:
    cloud_client = ChatClient(CLOUD, transport=cloud.transport()) if cloud else None
    return Router(
        ChatClient(LOCAL, transport=local.transport()),
        cloud_client,
        policy or RouterPolicy(),
        **kwargs,
    )


def make_conversation(local: FakeLlamaServer, **kwargs) -> Conversation:
    max_turns = kwargs.pop("max_turns", 20)
    return Conversation(make_router(local, **kwargs), "SYS", max_turns=max_turns, today=TODAY)


def ask(conv: Conversation, text: str, force=None) -> str:
    return "".join(conv.ask(text, conv.route(text, force=force)))
