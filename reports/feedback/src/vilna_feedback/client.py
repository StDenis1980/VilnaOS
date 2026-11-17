# SPDX-License-Identifier: GPL-3.0-or-later
"""The control socket of ``vilna-reportd`` (``reports/client/src/control.rs``).

One JSON request per line, one answer per line. The service knows who is
asking from the kernel (``SO_PEERCRED``): this person sees and sends only
their own crash reports and the system's. Standard library only.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

SOCKET = Path(os.environ.get("VILNA_REPORTD_SOCKET", "/run/vilna-reportd/control"))
TIMEOUT = 60.0  # sending waits for the server, at most 30 s in curl
MAX_ANSWER = 4 << 20


class ReportdError(Exception):
    """The service is not there, or it refused; ``str()`` says why in plain words."""

    def __init__(self, message: str, *, offline: bool = False) -> None:
        super().__init__(message)
        self.offline = offline


class Reportd:
    def __init__(self, path: Path = SOCKET, timeout: float = TIMEOUT) -> None:
        self.path = path
        self.timeout = timeout

    def ask(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(str(self.path))
                s.sendall(json.dumps(request, ensure_ascii=False).encode() + b"\n")
                data = b""
                while not data.endswith(b"\n") and len(data) < MAX_ANSWER:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    data += chunk
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise ReportdError(f"vilna-reportd is not running: {e}", offline=True) from e
        except OSError as e:
            raise ReportdError(f"vilna-reportd did not answer: {e}", offline=True) from e
        try:
            answer = json.loads(data.decode("utf-8"))
        except ValueError as e:
            raise ReportdError("vilna-reportd answered nonsense") from e
        if not isinstance(answer, dict):
            raise ReportdError("vilna-reportd answered nonsense")
        if "error" in answer:
            raise ReportdError(str(answer["error"]))
        return answer

    def status(self) -> dict[str, Any]:
        return self.ask({"op": "status"})

    def reports(self) -> list[dict[str, Any]]:
        return list(self.ask({"op": "list"}).get("reports") or [])

    def show(self, report_id: str) -> dict[str, Any]:
        return self.ask({"op": "show", "id": report_id})

    def send(self, report_id: str) -> dict[str, Any]:
        return self.ask({"op": "send", "id": report_id})

    def discard(self, report_id: str) -> bool:
        return bool(self.ask({"op": "discard", "id": report_id}).get("discarded"))

    def feedback(self, body: dict[str, Any]) -> str:
        return str(self.ask({"op": "feedback", "feedback": body}).get("id", ""))

    def preview(self, body: dict[str, Any]) -> dict[str, Any]:
        """What ``feedback`` would send, checked and scrubbed by the service; nothing is sent."""
        return dict(self.ask({"op": "preview", "feedback": body}).get("feedback") or {})

    def forget(self) -> int:
        return int(self.ask({"op": "forget"}).get("deleted") or 0)

    def system(self) -> str:
        return str(self.ask({"op": "system"}).get("system") or "")
