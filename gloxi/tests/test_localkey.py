# SPDX-License-Identifier: GPL-3.0-or-later
import stat

from gloxi import localkey
from gloxi.config import ModelEndpoint
from gloxi.llm import ChatClient


def test_local_endpoints_get_gloxis_key_and_the_cloud_its_own():
    assert localkey.headers_for("http://127.0.0.1:8080/v1") == {
        "Authorization": f"Bearer {localkey.KEY}"
    }
    assert localkey.headers_for("http://localhost:8080/v1")
    assert localkey.headers_for("https://api.mistral.ai/v1") == {}
    assert localkey.headers_for("https://api.mistral.ai/v1", "k") == {"Authorization": "Bearer k"}


def test_the_chat_client_sends_the_key_to_the_local_model():
    with ChatClient(ModelEndpoint(url="http://127.0.0.1:8080/v1", model="local")) as c:
        assert c._http.headers["Authorization"] == f"Bearer {localkey.KEY}"


def test_the_key_is_long_and_random():
    assert len(localkey.KEY) >= 40


def test_the_key_file_is_private_and_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    with localkey.key_file() as path:
        assert path.parent == tmp_path
        assert path.read_text().strip() == localkey.KEY
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.exists()
