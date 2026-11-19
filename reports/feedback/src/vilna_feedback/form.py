# SPDX-License-Identifier: GPL-3.0-or-later
"""The feedback form and the crash notifications without GTK.

What the person sees under «What will be sent» is built here from the same
dictionary that goes to ``vilna-reportd``, so the preview is the request.
The service checks it again, and the server a third time.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vilna_feedback import texts as t

MAX_TEXT = 8000
# The same shape the server accepts (reports/protocol/src/lib.rs, EMAIL).
EMAIL_RE = re.compile(r"^[^\s@<>]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
KINDS = ("bug", "idea", "other")
# reports/protocol/src/lib.rs, MAX_CONVERSATION_CHARS: the end is kept.
MAX_CONVERSATION = 16000
MAX_CONVERSATION_FILE = 4 << 20


@dataclass
class Form:
    kind: str = "bug"
    text: str = ""
    add_system: bool = False
    system: str = ""
    reply: bool = False
    contact: str = ""
    add_conversation: bool = False
    conversation: str = ""
    may_publish: bool = False
    locale: str = "en"

    def problem(self) -> str | None:
        """What keeps the form from being sent (a msgid), or None."""
        if not self.text.strip():
            return t.EMPTY_TEXT
        if len(self.text) > MAX_TEXT:
            return t.TOO_LONG
        if self.reply and not EMAIL_RE.match(self.contact.strip()):
            return t.BAD_EMAIL
        return None

    def body(self) -> dict[str, Any]:
        """Exactly what goes to vilna-reportd (and on to the server)."""
        body: dict[str, Any] = {
            "kind": self.kind if self.kind in KINDS else "other",
            "channel": "window",
            "text": self.text.strip(),
            "locale": self.locale,
            "program": "vilna-feedback",
            "may_publish": self.may_publish,
        }
        if self.add_system and self.system:
            body["system"] = self.system
        if self.reply and self.contact.strip():
            body["contact"] = self.contact.strip()
        if self.add_conversation and self.conversation.strip():
            body["conversation"] = self.conversation.strip()
        return body


def preview(body: dict[str, Any]) -> str:
    """The request as the person reads it before sending: plain JSON."""
    return json.dumps(body, ensure_ascii=False, indent=2)


def status_text(report: dict[str, Any], _: t.Texts) -> str:
    """«Sent · We already know about this crash» for a row of the list."""
    parts = [_(t.STATE_TEXT.get(str(report.get("state")), t.STATE_WAITING))]
    status = report.get("status")
    if status == "fixed" and report.get("fixed_in"):
        parts.append(_(t.STATUS_FIXED).format(version=report["fixed_in"]))
    elif status == "known":
        parts.append(_(t.STATUS_KNOWN))
    elif status == "new":
        parts.append(_(t.STATUS_NEW))
    return " · ".join(parts)


# --- The installer's conversation with Gloxi ------------------------------------------


def conversation_path(environ: dict[str, str] | None = None) -> Path:
    """Where the installer keeps it: next to its plan (installer/.../start.py)."""
    env = os.environ if environ is None else environ
    base = env.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / "vilna-installer" / "conversation.jsonl"


def read_conversation(path: Path) -> str:
    """The conversation as plain lines, «[step] who: text», its end if it is long.

    The installer writes one JSON object a line (time, step, who, text), with
    passwords never in it: they go into protected fields. It may still have a
    name, the computer's name or a Wi-Fi network, so it is only ever added
    after the person read it and chose to.
    """
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - MAX_CONVERSATION_FILE))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or not str(entry.get("text") or "").strip():
            continue
        who = str(entry.get("who") or "?")
        step = str(entry.get("step") or "")
        text = " ".join(str(entry["text"]).split())
        lines.append(f"[{step}] {who}: {text}" if step else f"{who}: {text}")
    joined = "\n".join(lines)
    return joined[-MAX_CONVERSATION:]


# --- «Ask me every time»: one notification per waiting report -------------------


def state_dir() -> Path:
    value = os.environ.get("XDG_STATE_HOME")
    base = Path(value) if value and os.path.isabs(value) else Path.home() / ".local/state"
    return base / "vilna-feedback"


def load_notified(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(x) for x in data} if isinstance(data, list) else set()


def save_notified(path: Path, ids: set[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(ids)), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # at worst a notification shows twice


def to_notify(consent: Any, reports: list[dict[str, Any]], notified: set[str]) -> list[dict]:
    """Waiting reports to ask about: only with the answer «ask», each once."""
    if consent != "ask":
        return []
    return [r for r in reports if r.get("state") == "waiting" and r.get("id") not in notified]


def still_waiting(reports: list[dict[str, Any]], notified: set[str]) -> set[str]:
    """Forget ids whose reports are gone, so the file does not grow."""
    return notified & {str(r.get("id")) for r in reports}
