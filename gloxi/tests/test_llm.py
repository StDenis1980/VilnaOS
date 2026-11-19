# SPDX-License-Identifier: GPL-3.0-or-later
import httpx
import pytest
from fake_server import FakeLlamaServer

from gloxi import localkey
from gloxi.config import ConfigError, ModelEndpoint
from gloxi.llm import ChatClient, ModelError, ModelUnavailable

ENDPOINT = ModelEndpoint(url="http://127.0.0.1:8080/v1", model="local")
MESSAGES = [{"role": "user", "content": "привіт"}]


def test_complete_returns_reply_and_sends_model():
    server = FakeLlamaServer("Добрий вечір")
    with ChatClient(ENDPOINT, transport=server.transport()) as client:
        assert client.complete(MESSAGES) == "Добрий вечір"
    assert server.requests[0]["model"] == "local"
    assert server.requests[0]["stream"] is False


def test_stream_joins_to_full_reply():
    server = FakeLlamaServer("Це довша відповідь з кількох шматків.")
    with ChatClient(ENDPOINT, transport=server.transport()) as client:
        pieces = list(client.stream(MESSAGES))
    assert len(pieces) > 1
    assert "".join(pieces) == "Це довша відповідь з кількох шматків."


def test_http_error_is_reported_with_server_message():
    server = FakeLlamaServer(status=503)
    with (
        ChatClient(ENDPOINT, transport=server.transport()) as client,
        pytest.raises(ModelError, match="503: model not loaded"),
    ):
        list(client.stream(MESSAGES))


def test_unreachable_server_raises_model_unavailable():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    with ChatClient(ENDPOINT, transport=httpx.MockTransport(refuse)) as client:
        with pytest.raises(ModelUnavailable):
            list(client.stream(MESSAGES))
        with pytest.raises(ModelUnavailable):
            client.complete(MESSAGES)
        assert client.is_available() is False


def test_api_key_is_sent_only_when_set():
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"data": []})

    cloud = ModelEndpoint(url="https://cloud.example/v1", model="m")
    keyed = ModelEndpoint(url="https://cloud.example/v1", model="m", api_key="sk-test")
    for endpoint in (cloud, keyed, ENDPOINT):
        with ChatClient(endpoint, transport=httpx.MockTransport(handler)) as client:
            client.is_available()
    # The local model gets the key Gloxi gave the server it started.
    assert seen == [None, "Bearer sk-test", f"Bearer {localkey.KEY}"]


def test_api_key_is_not_in_repr():
    assert "sk-test" not in repr(ModelEndpoint(url="u", model="m", api_key="sk-test"))


def test_the_local_conversation_keeps_its_slot():
    # Build14.23: llama-server gave turns an idle slot without the cached
    # prompt, and choosing an action took up to 61 s.
    from pathlib import Path

    from gloxi.config import load_settings

    none = Path("/nonexistent/gloxi.toml")
    settings = load_settings(none, env={})
    assert settings.local.slot == 0
    assert settings.cloud is None or settings.cloud.slot is None
    server = FakeLlamaServer("ok")
    with ChatClient(settings.local, transport=server.transport()) as client:
        client.complete(MESSAGES)
        client.prefill(MESSAGES)
        list(client.stream_events(MESSAGES))
    assert [r["id_slot"] for r in server.requests] == [0, 0, 0]

    # The cloud and a local "-1" get no slot field.
    with ChatClient(ENDPOINT, transport=server.transport()) as client:
        client.complete(MESSAGES)
    assert "id_slot" not in server.requests[-1]
    assert load_settings(none, env={"GLOXI_LOCAL_SLOT": "-1"}).local.slot is None
    with pytest.raises(ConfigError, match="slot"):
        load_settings(none, env={"GLOXI_LOCAL_SLOT": "two"})


def test_the_local_model_is_kept_from_repeating_itself():
    # Build15.6: a long answer repeated one sentence over and over; the
    # llama-server default penalty is 1, off.
    from pathlib import Path

    import pytest

    from gloxi.config import ConfigError, load_settings

    none = Path("/nonexistent/gloxi.toml")
    settings = load_settings(none, env={})
    assert settings.local.repeat_penalty == 1.1
    server = FakeLlamaServer("ok")
    with ChatClient(settings.local, transport=server.transport()) as client:
        list(client.stream_events(MESSAGES))
    assert server.requests[0]["repeat_penalty"] == 1.1

    off = load_settings(none, env={"GLOXI_LOCAL_REPEAT_PENALTY": "1"})
    assert off.local.repeat_penalty is None
    with ChatClient(ENDPOINT, transport=server.transport()) as client:
        client.complete(MESSAGES)
    assert "repeat_penalty" not in server.requests[-1]
    for bad in ("x", "nan", "5"):
        with pytest.raises(ConfigError):
            load_settings(none, env={"GLOXI_LOCAL_REPEAT_PENALTY": bad})
