# SPDX-License-Identifier: GPL-3.0-or-later
import httpx
import pytest
from fake_server import FakeLlamaServer

from gloxi import localkey
from gloxi.config import ModelEndpoint
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
