# SPDX-License-Identifier: GPL-3.0-or-later
"""A fake ``vilna-reportd``: a real Unix socket that answers like control.rs."""

import json
import socket
import threading

import pytest

from vilna_feedback.client import Reportd


class FakeReportd:
    def __init__(self, path):
        self.path = path
        self.requests = []
        self.answers = {}
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(8)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                request = json.loads(data)
                self.requests.append(request)
                answer = self.answers.get(request.get("op"), {"error": "unknown op"})
                if callable(answer):
                    answer = answer(request)
                raw = answer if isinstance(answer, bytes) else json.dumps(answer).encode() + b"\n"
                conn.sendall(raw)

    def close(self):
        self.server.close()


@pytest.fixture
def fake(tmp_path):
    server = FakeReportd(tmp_path / "control")
    yield server
    server.close()


@pytest.fixture
def reportd(fake):
    return Reportd(fake.path, timeout=5)
