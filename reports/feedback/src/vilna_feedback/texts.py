# SPDX-License-Identifier: GPL-3.0-or-later
"""The window's words: English msgids, translations in ``locale/<code>.po``.

All ten languages of ADR 0009. English is the msgid; a text without a
translation stays English. Translations other than Ukrainian were made with
AI help and are worth a native speaker's look (Weblate).
"""

from __future__ import annotations

import ast
import os
from importlib import resources

CODES = ("uk", "en", "es", "pl", "de", "fr", "it", "ro", "cs", "lt")

TITLE = "Feedback about Vilna"
TAB_FEEDBACK = "Feedback"
TAB_REPORTS = "Crash reports"
LEAD = (
    "Tell us what went wrong, what surprised you or what you would like. Only the Vilna team "
    "reads it; nothing is published without your permission."
)
KIND = "What is it about?"
KIND_BUG = "Something does not work"
KIND_IDEA = "An idea or a wish"
KIND_OTHER = "Something else"
MESSAGE = "Your message"
ADD_SYSTEM = "Add facts about this computer"
REPLY = "You may answer me by e-mail"
EMAIL = "E-mail address"
PUBLISH = "You may publish it, without my address, after reading it"
PREVIEW = "What will be sent"
SEND = "Send"
SENDING = "Sending…"
SENT = "Thank you! Your feedback is sent."
EMPTY_TEXT = "Write what happened or what you suggest."
BAD_EMAIL = "This does not look like an e-mail address."
TOO_LONG = "The message is longer than 8000 characters."
NO_SERVICE = (
    "The report service is not running on this computer, so nothing can be sent now. "
    "Your text stays here."
)
NOT_SENT = "Not sent: {reason}"
REPORTS_LEAD = (
    "Crash reports stay on this computer until they are sent. Each one has only what you "
    "see here: which program crashed and where in its code."
)
NO_REPORTS = "No crash reports."
ANSWER = "If a program crashes, send us a crash report?"
ANSWER_AUTO = "Send automatically"
ANSWER_ASK = "Ask me every time"
ANSWER_NEVER = "Never"
ANSWER_NONE = "No answer yet: nothing is sent."
ANSWER_NOT_CHANGED = "The answer was not changed."
STATE_WAITING = "Not sent"
STATE_SENT = "Sent"
STATE_REFUSED = "Not accepted by the server"
STATUS_NEW = "First report of this crash"
STATUS_KNOWN = "We already know about this crash"
STATUS_FIXED = "Fixed in {version}"
VIEW = "View"
SEND_REPORT = "Send"
DISCARD = "Do not send"
REPORT = "Crash report"
CLOSE = "Close"
FORGET = "Delete everything I sent from the server"
FORGET_DONE = "Deleted from the server: {count}"
NOTIFY_TITLE = "{program} crashed"
NOTIFY_BODY = "Send us a crash report? It has only which program crashed and where in its code."

KIND_TEXT = {"bug": KIND_BUG, "idea": KIND_IDEA, "other": KIND_OTHER}
ANSWER_TEXT = {"auto": ANSWER_AUTO, "ask": ANSWER_ASK, "never": ANSWER_NEVER}
STATE_TEXT = {"waiting": STATE_WAITING, "sent": STATE_SENT, "refused": STATE_REFUSED}

TEXTS = (
    TITLE,
    TAB_FEEDBACK,
    TAB_REPORTS,
    LEAD,
    KIND,
    KIND_BUG,
    KIND_IDEA,
    KIND_OTHER,
    MESSAGE,
    ADD_SYSTEM,
    REPLY,
    EMAIL,
    PUBLISH,
    PREVIEW,
    SEND,
    SENDING,
    SENT,
    EMPTY_TEXT,
    BAD_EMAIL,
    TOO_LONG,
    NO_SERVICE,
    NOT_SENT,
    REPORTS_LEAD,
    NO_REPORTS,
    ANSWER,
    ANSWER_AUTO,
    ANSWER_ASK,
    ANSWER_NEVER,
    ANSWER_NONE,
    ANSWER_NOT_CHANGED,
    STATE_WAITING,
    STATE_SENT,
    STATE_REFUSED,
    STATUS_NEW,
    STATUS_KNOWN,
    STATUS_FIXED,
    VIEW,
    REPORT,
    CLOSE,
    DISCARD,
    FORGET,
    FORGET_DONE,
    NOTIFY_TITLE,
    NOTIFY_BODY,
)


def parse_po(text: str) -> dict[str, str]:
    """msgid → msgstr for every translated, non-fuzzy entry."""
    catalog: dict[str, str] = {}
    msgid: list[str] | None = None
    msgstr: list[str] | None = None
    current: list[str] | None = None
    fuzzy = False
    for raw in text.splitlines() + [""]:
        line = raw.strip()
        if not line:
            if msgid is not None and msgstr is not None and not fuzzy:
                key, value = "".join(msgid), "".join(msgstr)
                if key and value:
                    catalog[key] = value
            msgid = msgstr = current = None
            fuzzy = False
        elif line.startswith("#,") and "fuzzy" in line:
            fuzzy = True
        elif line.startswith("#"):
            continue
        elif line.startswith("msgid "):
            msgid = [ast.literal_eval(line[6:])]
            current = msgid
        elif line.startswith("msgstr "):
            msgstr = [ast.literal_eval(line[7:])]
            current = msgstr
        elif line.startswith('"') and current is not None:
            current.append(ast.literal_eval(line))
    return catalog


def catalog(code: str) -> dict[str, str]:
    po = resources.files("vilna_feedback.locale") / f"{code}.po"
    return parse_po(po.read_text(encoding="utf-8")) if po.is_file() else {}


def language(environ: dict[str, str] | None = None) -> str:
    """The person's language from the session (LANGUAGE, LC_ALL, LC_MESSAGES, LANG)."""
    env = os.environ if environ is None else environ
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        for part in (env.get(var) or "").split(":"):
            code = part.split(".")[0].split("@")[0].split("_")[0]
            if code in CODES:
                return code
    return "en"


class Texts:
    def __init__(self, code: str | None = None) -> None:
        self.code = code or language()
        self._catalog = catalog(self.code)

    def __call__(self, message: str) -> str:
        return self._catalog.get(message, message)
