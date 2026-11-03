# SPDX-License-Identifier: GPL-3.0-or-later
"""The bundled model engine: download only after a yes, verified, server on 127.0.0.1."""

from __future__ import annotations

import hashlib
import io
import socket
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from gloxi import engine
from gloxi.cli import NoModel, prepare_engine
from gloxi.config import load_settings
from gloxi.engine import DownloadCancelled, Engine, EngineError, ModelFile, Plan, download

DATA = b"GGUF" + bytes(range(256)) * 40


def model(data: bytes = DATA) -> ModelFile:
    return ModelFile(
        name="Test model",
        filename="test.gguf",
        url="https://models.example/test.gguf",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        source="a test",
        licence="CC0-1.0",
    )


def serving(data: bytes, *, ranges: bool = True, seen: list | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.headers.get("Range"))
        rng = request.headers.get("Range")
        if rng and ranges:
            start = int(rng.removeprefix("bytes=").removesuffix("-"))
            return httpx.Response(206, content=data[start:])
        return httpx.Response(200, content=data)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_download_verifies_and_renames(tmp_path):
    progress = []
    path = download(
        model(), tmp_path, client=serving(DATA), progress=lambda d, t: progress.append((d, t))
    )
    assert path == tmp_path / "test.gguf"
    assert path.read_bytes() == DATA
    assert not (tmp_path / "test.gguf.part").exists()
    assert progress[-1] == (len(DATA), len(DATA))


def test_download_resumes_a_partial_file(tmp_path):
    (tmp_path / "test.gguf.part").write_bytes(DATA[:1000])
    seen = []
    path = download(model(), tmp_path, client=serving(DATA, seen=seen))
    assert seen == ["bytes=1000-"]
    assert path.read_bytes() == DATA


def test_download_restarts_when_ranges_are_ignored(tmp_path):
    (tmp_path / "test.gguf.part").write_bytes(DATA[:1000])
    path = download(model(), tmp_path, client=serving(DATA, ranges=False))
    assert path.read_bytes() == DATA


def test_a_wrong_file_is_deleted(tmp_path):
    with pytest.raises(EngineError, match="damaged"):
        download(model(), tmp_path, client=serving(DATA[:-1] + b"X"))
    assert list(tmp_path.iterdir()) == []


def test_http_error_is_reported(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(EngineError, match="404"):
        download(model(), tmp_path, client=client)


def test_cancel_keeps_the_part_for_later(tmp_path):
    with pytest.raises(DownloadCancelled):
        download(model(), tmp_path, client=serving(DATA), cancelled=lambda: True)
    assert not (tmp_path / "test.gguf").exists()


def test_plan(monkeypatch, tmp_path):
    env = {"XDG_DATA_HOME": str(tmp_path)}
    monkeypatch.setattr(engine, "listening", lambda url, **kw: False)
    monkeypatch.setattr(engine, "server_binary", lambda: "/app/bin/llama-server")
    url = "http://127.0.0.1:8080/v1"
    p = engine.plan(url, env=env)
    assert p.action == "download"
    assert p.folder == tmp_path / "gloxi" / "models"

    p.folder.mkdir(parents=True)
    (p.folder / engine.DEFAULT_MODEL.filename).write_bytes(b"short")
    assert engine.plan(url, env=env).action == "download"  # wrong size

    assert engine.plan(url, env=env, start_server=False).action == "none"
    assert engine.plan("http://192.168.1.5:8080/v1", env=env).action == "none"
    monkeypatch.setattr(engine, "server_binary", lambda: None)
    assert engine.plan(url, env=env).action == "none"
    monkeypatch.setattr(engine, "listening", lambda url, **kw: True)
    assert engine.plan(url, env=env).action == "ready"


def test_the_pinned_model_is_the_adr_0008_one():
    m = engine.DEFAULT_MODEL
    assert m.url.startswith("https://huggingface.co/Qwen/Qwen3-4B-GGUF/")
    assert m.url.endswith("/" + m.filename)
    assert len(m.sha256) == 64 and m.size > 1_000_000_000


# -- the server --------------------------------------------------------------------

FAKE_SERVER = textwrap.dedent(
    """
    import sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    args = sys.argv[1:]
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert '{"enable_thinking":false}' in args
    port = int(args[args.index("--port") + 1])
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()
        def log_message(self, *a):
            pass
    HTTPServer(("127.0.0.1", port), H).serve_forever()
    """
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_binary(tmp_path) -> str:
    script = tmp_path / "llama-server"
    script.write_text(f"#!{sys.executable}\n{FAKE_SERVER}")
    script.chmod(0o755)
    return str(script)


def test_engine_starts_and_stops(fake_binary, tmp_path):
    url = f"http://127.0.0.1:{free_port()}/v1"
    e = Engine(fake_binary, tmp_path / "m.gguf", url)
    e.start(timeout=20)
    assert engine.listening(url)
    e.stop()
    assert not engine.listening(url, timeout=0.5)


def test_engine_reports_a_crash(tmp_path):
    script = tmp_path / "llama-server"
    script.write_text(f"#!{sys.executable}\nraise SystemExit(3)\n")
    script.chmod(0o755)
    e = Engine(str(script), tmp_path / "m.gguf", f"http://127.0.0.1:{free_port()}/v1")
    with pytest.raises(EngineError, match="exit 3"):
        e.start(timeout=20)


def test_engine_only_on_loopback(fake_binary, tmp_path):
    with pytest.raises(EngineError):
        Engine(fake_binary, tmp_path / "m.gguf", "http://0.0.0.0:8080/v1")


# -- the terminal asks first ----------------------------------------------------------


class FakeEngine:
    started = []

    def __init__(self, binary, model_path, url):
        self.args = (binary, model_path, url)

    def start(self):
        FakeEngine.started.append(self.args)

    def stop(self):
        pass


def settings(tmp_path):
    return replace(load_settings(tmp_path / "none.toml", env={}), start_server=True)


def download_plan(tmp_path):
    return Plan("download", model=model(), binary="/bin/llama-server", folder=tmp_path / "m")


def test_terminal_does_not_download_without_yes(tmp_path):
    fetched, out = [], io.StringIO()
    with pytest.raises(NoModel):
        prepare_engine(
            settings(tmp_path),
            read=lambda prompt: "",
            out=out,
            planner=lambda url, **kw: download_plan(tmp_path),
            fetch=lambda *a, **kw: fetched.append(a),
            engine_type=FakeEngine,
        )
    assert fetched == []
    assert "Test model" in out.getvalue() and "Not downloaded" in out.getvalue()


def test_terminal_downloads_after_yes_and_starts(tmp_path):
    FakeEngine.started.clear()
    out = io.StringIO()
    result = prepare_engine(
        settings(tmp_path),
        read=lambda prompt: "так",
        out=out,
        planner=lambda url, **kw: download_plan(tmp_path),
        fetch=lambda m, folder, progress: (progress(5, 10), Path(folder) / m.filename)[1],
        engine_type=FakeEngine,
    )
    assert isinstance(result, FakeEngine)
    assert FakeEngine.started == [
        ("/bin/llama-server", tmp_path / "m" / "test.gguf", result.args[2])
    ]
    assert "50%" in out.getvalue()


def test_not_interactive_never_asks(tmp_path):
    asked = []
    result = prepare_engine(
        settings(tmp_path),
        read=lambda prompt: asked.append(prompt) or "y",
        out=io.StringIO(),
        interactive=False,
        planner=lambda url, **kw: download_plan(tmp_path),
        engine_type=FakeEngine,
    )
    assert result is None and asked == []
