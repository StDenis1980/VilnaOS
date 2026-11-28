# SPDX-License-Identifier: Apache-2.0
"""The person's mail for Gloxi (ADR 0030): reading, drafts and sending.

Everything Gloxi can do with mail is here, whatever carries it (IMAP and
SMTP for real accounts, ``MemoryStore`` for tests and the demo):

- Reading never changes the mailbox: messages are fetched with ``PEEK`` and
  stay unread. All a message says is someone else's words, which Herkos marks
  for Keros (``returns_external`` in the manifest).
- ``send`` builds the message from its arguments alone. The arguments are
  what the Herkos dialog shows for R4 (permissions.md, 5.2), so the person
  sees exactly what goes out: sender, every recipient, subject and text.
  Nothing else is added except technical headers (date, message id, the
  message it answers). There are no attachments yet.
- The sender is the account the person chose in GNOME Online Accounts and
  is named by its address in ``account``; Gloxi cannot write another From.
- Every number that comes from a request or a server is bounded.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email import policy
from email.headerregistry import Address as HeaderAddress
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import format_datetime, formataddr, getaddresses, make_msgid, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Protocol

MAX_LIST = 50  # messages per list or search
DEFAULT_LIST = 20
MAX_TEXT = 20_000  # characters of a message's text given to Gloxi
MAX_HEADER = 1_000  # characters of one header (subject, recipients...)
MAX_ATTACHMENTS = 50  # attachments listed for one message
MAX_FOLDERS = 200
MAX_QUERY = 200
MAX_SEARCH_DAYS = 3660
MAX_RAW = 4 * 1024 * 1024  # bytes of one message read from a server

MAX_RECIPIENTS = 50  # to, cc and bcc together
MAX_SUBJECT = 255
MAX_BODY = 50_000  # characters Gloxi may write in one message
MAX_ADDRESS = 254

# Folder roles: special-use attributes (RFC 6154) and INBOX.
ROLES = ("inbox", "sent", "drafts", "trash", "junk", "archive", "all", "flagged")

_ADDRESS = re.compile(r"^[^@\s<>(),;:\"\[\]\\]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_NAMED = re.compile(r"^[^<>]*<[^<>]+>$")
_MESSAGE_ID = re.compile(r"^<[^<>\s]{1,994}>$")
# Message ids of drafts this server wrote (make_msgid with idstring "gloxi"):
# only these may be removed again by ``restore``.
_OWN_DRAFT_ID = re.compile(r"^<[^<>@\s]+\.gloxi@[^<>\s]+>$")
# AI Act, Art. 50(2): what Gloxi writes is marked, machine-readably, as made
# by an AI (ADR 0033, Denys's decision of 2026-11-26). The person does not see
# these in their mail program; a recipient may, in the raw message. The
# digital source type is IPTC's, the one C2PA and image metadata use.
AI_HEADERS = (
    ("X-AI-Generated", "yes"),
    (
        "X-Digital-Source-Type",
        "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia",
    ),
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Signs that change how text looks without being seen: direction overrides,
# zero-width spaces, and line breaks that only some readers break on.
_INVISIBLE = re.compile("[\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_UNICODE_BREAKS = re.compile("[\u0085\u2028\u2029]")
# Bytes of one header read before it is parsed: parsing many encoded words is
# quadratic, and nothing past this is shown anyway (MAX_HEADER characters).
MAX_RAW_HEADER = 8 * 1024
# Years the clock can show; a later one ("9999") cannot be moved to a time zone.
MAX_YEAR = 9000


class MailError(Exception):
    """Something the caller can fix: an unknown account, a bad address, a folder."""


@dataclass(frozen=True)
class Account:
    id: str  # GNOME Online Accounts id
    email: str
    name: str = ""
    provider: str = ""  # GOA provider type: "imap_smtp", "google", "ms365"...
    can_read: bool = True
    can_send: bool = True
    # The provider keeps a copy of what goes out through SMTP itself (Gmail,
    # Microsoft): then we do not add one to Sent, or it would be there twice.
    saves_sent: bool = False
    problem: str = ""  # why the account cannot be used now, in plain words

    def public(self) -> dict:
        out: dict[str, Any] = {
            "account": self.email,
            "name": self.name,
            "provider": self.provider,
            "can_read": self.can_read and not self.problem,
            "can_send": self.can_send and not self.problem,
        }
        if self.problem:
            out["problem"] = self.problem
        return out


@dataclass(frozen=True)
class Folder:
    name: str  # as people read it (decoded from IMAP's modified UTF-7)
    role: str = ""
    messages: int | None = None
    unread: int | None = None


@dataclass(frozen=True)
class Criteria:
    text: str = ""
    sender: str = ""
    since: date | None = None
    before: date | None = None  # exclusive
    unread: bool = False


@dataclass(frozen=True)
class Summary:
    uid: int
    headers: bytes  # From, To, Cc, Subject, Date, Message-ID
    seen: bool = False
    flagged: bool = False
    size: int = 0


class Store(Protocol):
    def accounts(self) -> list[Account]: ...

    def folders(self, account: Account) -> list[Folder]: ...

    def search(self, account: Account, folder: str, criteria: Criteria) -> list[int]:
        """UIDs of the matching messages, in any order."""
        ...

    def summaries(self, account: Account, folder: str, uids: list[int]) -> list[Summary]: ...

    def fetch(self, account: Account, folder: str, uid: int) -> tuple[bytes, Summary] | None:
        """The message (at most MAX_RAW bytes) without marking it read; None if gone."""
        ...

    def append(self, account: Account, role: str, raw: bytes, flags: tuple[str, ...]) -> str:
        """Store a message in the folder with that role; returns the folder's name."""
        ...

    def remove_draft(self, account: Account, folder: str, message_id: str) -> bool: ...

    def send(self, account: Account, raw: bytes, recipients: list[str]) -> list[str]:
        """Send; returns the recipients the server refused while taking the others."""
        ...


# --- What the person and Gloxi see --------------------------------------------------


def clip(text: str, limit: int) -> str:
    text = _INVISIBLE.sub("", _CONTROL.sub("", _UNICODE_BREAKS.sub(" ", text)))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def one_line(text: str, limit: int) -> str:
    """A name from a message (a header, a file name) as one plain line."""
    return clip(" ".join(_UNICODE_BREAKS.sub(" ", text).split()), limit)


def cap_headers(raw: bytes) -> bytes:
    """The message with every header cut to MAX_RAW_HEADER bytes before parsing."""
    end = len(raw)
    for sep in (b"\r\n\r\n", b"\n\n"):
        i = raw.find(sep)
        if i != -1:
            end = min(end, i)
    head, body = raw[:end], raw[end:]
    if len(head) <= MAX_RAW_HEADER:
        return raw
    out: list[bytes] = []
    used = 0
    for line in head.splitlines(keepends=True):
        folded = line[:1] in (b" ", b"\t")
        if not folded:
            used = 0
        if used >= MAX_RAW_HEADER:
            continue  # the rest of a header that is too long
        keep = line[: MAX_RAW_HEADER - used]
        if len(keep) < len(line) and not keep.endswith(b"\n"):
            keep += b"\r\n"
        used += len(keep)
        out.append(keep)
    return b"".join(out) + body


def parse_headers(raw: bytes) -> Message:
    return BytesParser(policy=policy.default).parsebytes(cap_headers(raw), headersonly=True)


ADDRESS_HEADERS = ("from", "to", "cc", "reply-to", "sender", "bcc")


def header(msg: Message, name: str) -> str:
    try:
        value = msg.get(name)
        # An address list the parser had to guess at ("boss@example.com
        # <x@example.org>" without quotes) may drop the real address: show it
        # as written instead.
        if name.lower() in ADDRESS_HEADERS and value is not None and value.defects:
            value = next((v for k, v in msg.raw_items() if k.lower() == name.lower()), value)
    except MemoryError:
        raise
    except Exception:  # a header the parser cannot read: leave it out
        value = ""
    return one_line(str(value or ""), MAX_HEADER)


def when(msg: Message) -> str:
    raw = header(msg, "Date")
    try:
        moment = parsedate_to_datetime(raw)
        if moment.year > MAX_YEAR:
            return raw
        if moment.tzinfo is None:
            return moment.isoformat(timespec="minutes")
        return moment.astimezone().isoformat(timespec="minutes")
    except (TypeError, ValueError, IndexError, OverflowError, OSError):
        return raw


def summary_dict(s: Summary) -> dict:
    msg = parse_headers(s.headers)
    return {
        "uid": s.uid,
        "from": header(msg, "From"),
        "to": header(msg, "To"),
        "subject": header(msg, "Subject"),
        "date": when(msg),
        "unread": not s.seen,
        "flagged": s.flagged,
        "size": max(0, int(s.size)),
    }


_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0+(\.0+)?\s*(px|pt|em|rem|%)?\s*(;|$)"
    r"|opacity\s*:\s*0+(\.0+)?\s*(;|$)|(left|top|text-indent)\s*:\s*-\d{3,}",
    re.IGNORECASE,
)
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source"}
    | {"track", "wbr"}
)
_BODY_START = frozenset({"body", "p", "div", "table", "span", "a", "h1", "h2", "h3", "ul", "ol"})


class _Text(HTMLParser):
    """HTML to plain text: what a person would read, without scripts, styles or hidden text.

    Text the person cannot see (display:none, zero size, far off screen) is
    left out too: it would reach the model only, a channel for instructions
    the person never sees.
    """

    BREAK = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
    SKIP = {"script", "style", "head", "title", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        # Open elements, each with whether it hides what is inside.
        self._open: list[tuple[str, bool]] = []

    def _hidden(self) -> bool:
        return any(h for _, h in self._open)

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _BODY_START:
            # An unclosed <head> must not hide the whole message.
            while self._open and self._open[-1][0] in ("head", "title"):
                self._open.pop()
        if tag in self.BREAK and not self._hidden():
            self.parts.append("\n")
        if tag in _VOID:
            return
        values = {k: (v or "") for k, v in attrs}
        hides = (
            tag in self.SKIP
            or "hidden" in values
            or values.get("aria-hidden", "").lower() == "true"
            or bool(_HIDDEN_STYLE.search(values.get("style", "")))
        )
        self._open.append((tag, hides))

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        if tag in self.BREAK and not self._hidden():
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i][0] == tag:
                del self._open[i:]
                break
        if tag in self.BREAK and not self._hidden():
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden():
            self.parts.append(data)


def html_text(html: str) -> str:
    p = _Text()
    try:
        p.feed(html)
        p.close()
    except Exception:  # a broken page: what was read so far
        pass
    lines = [" ".join(line.split()) for line in "".join(p.parts).splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def _content(part: Message) -> str:
    try:
        text = part.get_content()
    except (LookupError, ValueError, AssertionError):
        payload = part.get_payload(decode=True) or b""
        text = payload.decode("utf-8", errors="replace")
    return text if isinstance(text, str) else ""


def message_dict(raw: bytes, s: Summary, folder: str) -> dict:
    try:
        return _message_dict(raw, s, folder)
    except RecursionError:
        # Parts nested a thousand deep: still say who wrote it and what about.
        msg = parse_headers(raw)
        return {
            "uid": s.uid,
            "folder": folder,
            "from": header(msg, "From"),
            "to": header(msg, "To"),
            "cc": header(msg, "Cc"),
            "reply_to": header(msg, "Reply-To"),
            "subject": header(msg, "Subject"),
            "date": when(msg),
            "message_id": header(msg, "Message-ID"),
            "unread": not s.seen,
            "text": "[the message is built too deeply to read; open it in the mail program]",
            "text_truncated": True,
            "attachments": [],
        }


def _message_dict(raw: bytes, s: Summary, folder: str) -> dict:
    msg = BytesParser(policy=policy.default).parsebytes(cap_headers(raw))
    body = None
    try:
        body = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        body = None
    text = ""
    if body is not None:
        text = _content(body)
        if body.get_content_type() == "text/html":
            text = html_text(text)
    text = _CONTROL.sub("", text.replace("\r\n", "\n"))
    attachments = []
    try:
        parts = list(msg.iter_attachments()) if msg.is_multipart() else []
    except Exception:
        parts = []
    for part in parts[:MAX_ATTACHMENTS]:
        payload = part.get_payload(decode=True)
        attachments.append(
            {
                "name": one_line(part.get_filename() or "", 255),
                "type": part.get_content_type(),
                "size": len(payload) if isinstance(payload, bytes) else 0,
            }
        )
    out = {
        "uid": s.uid,
        "folder": folder,
        "from": header(msg, "From"),
        "to": header(msg, "To"),
        "cc": header(msg, "Cc"),
        "reply_to": header(msg, "Reply-To"),
        "subject": header(msg, "Subject"),
        "date": when(msg),
        "message_id": header(msg, "Message-ID"),
        "unread": not s.seen,
        "text": text[:MAX_TEXT],
        "attachments": attachments,
    }
    if len(text) > MAX_TEXT or len(raw) >= MAX_RAW:
        out["text_truncated"] = True
    if len(parts) > MAX_ATTACHMENTS:
        out["attachments_truncated"] = True
    return out


# --- What Gloxi writes ----------------------------------------------------------------


def address(raw: Any) -> str:
    """``ola@example.org`` or ``Ola Nowak <ola@example.org>``, checked, as a header value."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_ADDRESS + 200:
        raise MailError("each recipient is an address such as ola@example.org")
    if "\n" in raw or "\r" in raw or _CONTROL.search(raw) or _UNICODE_BREAKS.search(raw):
        raise MailError("an address cannot hold line breaks or control characters")
    if _INVISIBLE.search(raw):
        raise MailError("an address cannot hold invisible or direction-changing signs")
    if ("<" in raw or ">" in raw) and not _NAMED.fullmatch(raw.strip()):
        raise MailError(f"{raw!r} is not an email address")
    found = getaddresses([raw])
    if len(found) != 1:
        raise MailError(f"{raw!r} must be one address")
    name, addr = found[0]
    if not addr or len(addr) > MAX_ADDRESS or not _ADDRESS.fullmatch(addr):
        raise MailError(f"{raw!r} is not an email address")
    if not name:
        return addr
    if "@" in name:
        # "boss@example.com" <evil@example.net> looks like one address and goes to another.
        raise MailError(f"{raw!r}: the name before the address cannot look like an address")
    try:
        return str(HeaderAddress(display_name=name, addr_spec=addr))
    except (ValueError, IndexError) as e:
        raise MailError(f"{raw!r} is not an email address") from e


def bare(addr: str) -> str:
    return getaddresses([addr])[0][1]


@dataclass(frozen=True)
class Draft:
    to: tuple[str, ...]
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    subject: str = ""
    text: str = ""
    in_reply_to: str = ""

    @classmethod
    def check(
        cls,
        to: Any,
        subject: Any,
        text: Any,
        cc: Any = None,
        bcc: Any = None,
        in_reply_to: Any = None,
    ) -> Draft:
        lists = []
        for name, value in (("to", to), ("cc", cc or []), ("bcc", bcc or [])):
            if not isinstance(value, list):
                raise MailError(f"'{name}' must be a list of addresses")
            lists.append(tuple(address(a) for a in value))
        if not lists[0] and not lists[1] and not lists[2]:
            raise MailError("a message needs at least one recipient")
        if sum(len(x) for x in lists) > MAX_RECIPIENTS:
            raise MailError(f"at most {MAX_RECIPIENTS} recipients in one message")
        if not isinstance(subject, str) or len(subject) > MAX_SUBJECT:
            raise MailError(f"'subject' must be text of at most {MAX_SUBJECT} characters")
        if (
            "\n" in subject
            or "\r" in subject
            or _CONTROL.search(subject)
            or _UNICODE_BREAKS.search(subject)
        ):
            raise MailError("the subject is one line, without control characters")
        if not isinstance(text, str) or len(text) > MAX_BODY:
            raise MailError(f"'text' must be text of at most {MAX_BODY} characters")
        if _CONTROL.search(text.replace("\t", "")):
            raise MailError("the text cannot hold control characters")
        reply = in_reply_to or ""
        if reply and (not isinstance(reply, str) or not _MESSAGE_ID.fullmatch(reply)):
            raise MailError(
                "'in_reply_to' is the message_id of a message, such as <a1@example.org>"
            )
        return cls(lists[0], lists[1], lists[2], subject, text, reply)

    def recipients(self) -> list[str]:
        seen: dict[str, None] = {}
        for a in (*self.to, *self.cc, *self.bcc):
            seen.setdefault(bare(a).lower(), None)
        return list(seen)

    def message(self, account: Account, now: datetime | None = None) -> EmailMessage:
        msg = EmailMessage(policy=policy.SMTP)
        msg["From"] = formataddr((account.name, account.email)) if account.name else account.email
        if self.to:
            msg["To"] = ", ".join(self.to)
        if self.cc:
            msg["Cc"] = ", ".join(self.cc)
        msg["Subject"] = self.subject
        msg["Date"] = format_datetime(now or datetime.now().astimezone())
        domain = account.email.rpartition("@")[2] or "localhost"
        msg["Message-ID"] = make_msgid(idstring="gloxi", domain=domain)
        if self.in_reply_to:
            msg["In-Reply-To"] = self.in_reply_to
            msg["References"] = self.in_reply_to
        for name, value in AI_HEADERS:
            msg[name] = value
        text = self.text.replace("\r\n", "\n")
        if not text.endswith("\n"):
            text += "\n"
        msg.set_content(text, charset="utf-8", cte="quoted-printable")
        return msg


# --- The operations -------------------------------------------------------------------


def _date(name: str, value: Any) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MailError(f"'{name}' must be a date such as 2027-03-05")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as e:
        raise MailError(f"'{name}' must be a date such as 2027-03-05") from e


def _limit(value: Any) -> int:
    if value is None:
        return DEFAULT_LIST
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_LIST:
        raise MailError(f"'limit' must be a number from 1 to {MAX_LIST}")
    return value


def _folder(value: Any) -> str:
    if value is None:
        return "INBOX"
    if not isinstance(value, str) or not value or len(value) > 255 or _CONTROL.search(value):
        raise MailError("'folder' is a folder name from list_folders, such as INBOX")
    return value


class Mailbox:
    def __init__(self, store: Store) -> None:
        self.store = store

    def account(self, wanted: Any, *, required: bool = False) -> Account:
        accounts = self.store.accounts()
        if wanted is None:
            if required:
                raise MailError("say which account: 'account' is its address from list_accounts")
            usable = [a for a in accounts if a.can_read and not a.problem]
            if len(usable) == 1:
                return usable[0]
            if not usable:
                raise MailError("no mail account is set up in GNOME Online Accounts")
            raise MailError("there are several mail accounts: say which one in 'account'")
        if not isinstance(wanted, str):
            raise MailError("'account' is an address from list_accounts")
        for a in accounts:
            if a.email.lower() == wanted.strip().lower():
                if a.problem:
                    raise MailError(f"{a.email}: {a.problem}")
                return a
        known = ", ".join(a.email for a in accounts) or "none"
        raise MailError(f"unknown account {wanted!r}; the accounts are: {known}")

    def list_accounts(self) -> dict:
        return {"accounts": [a.public() for a in self.store.accounts()]}

    def list_folders(self, account: Any = None) -> dict:
        a = self.account(account)
        folders = self.store.folders(a)[:MAX_FOLDERS]
        out = []
        for f in folders:
            item: dict[str, Any] = {"folder": f.name}
            if f.role:
                item["role"] = f.role
            if f.messages is not None:
                item["messages"] = f.messages
            if f.unread is not None:
                item["unread"] = f.unread
            out.append(item)
        return {"account": a.email, "folders": out}

    def _listing(self, a: Account, folder: str, criteria: Criteria, limit: int) -> dict:
        uids = sorted({u for u in self.store.search(a, folder, criteria) if u > 0}, reverse=True)
        picked = uids[:limit]
        summaries = {s.uid: s for s in self.store.summaries(a, folder, picked)}
        messages = [summary_dict(summaries[u]) for u in picked if u in summaries]
        return {
            "account": a.email,
            "folder": folder,
            "messages": messages,
            "total": len(uids),
            "truncated": len(uids) > len(picked),
        }

    def list_messages(
        self, account: Any = None, folder: Any = None, unread_only: Any = False, limit: Any = None
    ) -> dict:
        a = self.account(account)
        criteria = Criteria(unread=bool(unread_only))
        return self._listing(a, _folder(folder), criteria, _limit(limit))

    def search_messages(
        self,
        account: Any = None,
        query: Any = None,
        sender: Any = None,
        since: Any = None,
        until: Any = None,
        folder: Any = None,
        unread_only: Any = False,
        limit: Any = None,
    ) -> dict:
        a = self.account(account)
        for name, value in (("query", query), ("from", sender)):
            if value is not None and (not isinstance(value, str) or len(value) > MAX_QUERY):
                raise MailError(f"'{name}' must be text of at most {MAX_QUERY} characters")
            if isinstance(value, str) and ("\n" in value or "\r" in value or '"' in value):
                raise MailError(f"'{name}' is one line without quotes")
        start, end = _date("since", since), _date("until", until)
        before = end + timedelta(days=1) if end else None
        if start and before and (before - start).days > MAX_SEARCH_DAYS:
            raise MailError(f"search at most {MAX_SEARCH_DAYS} days at once")
        if start and before and before <= start:
            raise MailError("'until' must not be before 'since'")
        criteria = Criteria(
            text=(query or "").strip(),
            sender=(sender or "").strip(),
            since=start,
            before=before,
            unread=bool(unread_only),
        )
        if not (criteria.text or criteria.sender or start or before or criteria.unread):
            raise MailError("say what to look for: query, from, since, until or unread_only")
        return self._listing(a, _folder(folder), criteria, _limit(limit))

    def read_message(self, uid: Any, account: Any = None, folder: Any = None) -> dict:
        a = self.account(account)
        if not isinstance(uid, int) or isinstance(uid, bool) or not 1 <= uid < 2**32:
            raise MailError("'uid' is a message's uid from list_messages")
        name = _folder(folder)
        got = self.store.fetch(a, name, uid)
        if got is None:
            raise MailError(f"there is no message {uid} in {name}")
        raw, s = got
        out = message_dict(raw, s, name)
        out["account"] = a.email
        return out

    def _writable(self, account: Any, *, send: bool) -> Account:
        a = self.account(account, required=True)
        if send and not a.can_send:
            raise MailError(f"{a.email} cannot send mail (no SMTP in GNOME Online Accounts)")
        return a

    def save_draft(self, account: Any = None, **message: Any) -> dict:
        a = self._writable(account, send=False)
        draft = Draft.check(**message)
        msg = draft.message(a)
        folder = self.store.append(a, "drafts", msg.as_bytes(), ("\\Draft", "\\Seen"))
        message_id = str(msg["Message-ID"])
        return {
            "account": a.email,
            "saved": True,
            "folder": folder,
            "message_id": message_id,
            # Herkos undoes it with restore (ADR 0023, section 3).
            "undo_arguments": {"account": a.email, "folder": folder, "message_id": message_id},
        }

    def send(self, account: Any = None, replaces_draft: Any = None, **message: Any) -> dict:
        a = self._writable(account, send=True)
        draft = Draft.check(**message)
        if replaces_draft is not None and (
            not isinstance(replaces_draft, str) or not _OWN_DRAFT_ID.fullmatch(replaces_draft)
        ):
            raise MailError("'replaces_draft' is the message_id save_draft gave")
        msg = draft.message(a)
        raw = msg.as_bytes()
        refused = self.store.send(a, raw, draft.recipients())
        out: dict[str, Any] = {
            "account": a.email,
            "sent": True,
            "message_id": str(msg["Message-ID"]),
            "recipients": len(draft.recipients()) - len(refused),
        }
        if refused:
            out["refused"] = refused
        if replaces_draft:
            # What is sent is still built from these arguments alone, the ones
            # the person saw; the draft they answer only goes from Drafts, so
            # the same reply does not stay there as if unsent.
            out["draft_removed"] = self._remove_own_draft(a, replaces_draft)
        if not a.saves_sent:
            # Bcc stays out of the copy too: it is the message as sent.
            try:
                out["saved_to"] = self.store.append(a, "sent", raw, ("\\Seen",))
            except MailError as e:
                out["saved_to"] = None
                out["note"] = f"sent, but no copy was kept in Sent: {e}"
        return out

    def _remove_own_draft(self, a: Account, message_id: str) -> bool:
        try:
            folder = next((f.name for f in self.store.folders(a) if f.role == "drafts"), "")
            return bool(folder) and self.store.remove_draft(a, folder, message_id)
        except MailError:
            return False  # sent all the same; the draft stays

    def restore(self, account: Any = None, folder: Any = None, message_id: Any = None) -> dict:
        """Undo ``save_draft``: remove that draft again, and only a draft Gloxi wrote."""
        a = self.account(account, required=True)
        name = _folder(folder)
        if not isinstance(message_id, str) or not _OWN_DRAFT_ID.fullmatch(message_id):
            raise MailError("restore only removes a draft that save_draft wrote")
        removed = self.store.remove_draft(a, name, message_id)
        return {"account": a.email, "folder": name, "removed": removed}


# --- A mailbox in memory: tests and the demo ------------------------------------------


@dataclass
class _Stored:
    uid: int
    raw: bytes
    flags: set[str] = field(default_factory=set)


class MemoryStore:
    def __init__(self, accounts: Iterable[Account]) -> None:
        self._accounts = list(accounts)
        self.boxes: dict[str, dict[str, list[_Stored]]] = {}
        self.roles: dict[str, dict[str, str]] = {}
        self.outbox: list[tuple[str, bytes, list[str]]] = []  # what "send" got
        for a in self._accounts:
            self.boxes[a.email] = {"INBOX": [], "Sent": [], "Drafts": [], "Trash": []}
            self.roles[a.email] = {
                "INBOX": "inbox",
                "Sent": "sent",
                "Drafts": "drafts",
                "Trash": "trash",
            }

    def add(self, account: str, folder: str, raw: bytes, seen: bool = False) -> int:
        box = self.boxes[account].setdefault(folder, [])
        uid = max((m.uid for m in box), default=0) + 1
        box.append(_Stored(uid, raw, {"\\Seen"} if seen else set()))
        return uid

    def accounts(self) -> list[Account]:
        return list(self._accounts)

    def _box(self, account: Account, folder: str) -> list[_Stored]:
        for name, box in self.boxes[account.email].items():
            if name == folder or (folder.upper() == "INBOX" and name == "INBOX"):
                return box
        raise MailError(f"there is no folder {folder!r}")

    def folders(self, account: Account) -> list[Folder]:
        return [
            Folder(
                name,
                self.roles[account.email].get(name, ""),
                len(box),
                sum("\\Seen" not in m.flags for m in box),
            )
            for name, box in self.boxes[account.email].items()
        ]

    def search(self, account: Account, folder: str, criteria: Criteria) -> list[int]:
        out = []
        for m in self._box(account, folder):
            msg = BytesParser(policy=policy.default).parsebytes(m.raw)
            if criteria.unread and "\\Seen" in m.flags:
                continue
            if criteria.sender and criteria.sender.casefold() not in header(msg, "From").casefold():
                continue
            if criteria.text:
                body = msg.get_body(preferencelist=("plain", "html"))
                blob = " ".join(
                    [header(msg, "Subject"), header(msg, "From"), _content(body) if body else ""]
                )
                if criteria.text.casefold() not in blob.casefold():
                    continue
            if criteria.since or criteria.before:
                try:
                    day = parsedate_to_datetime(header(msg, "Date")).date()
                except (TypeError, ValueError, IndexError):
                    continue
                if criteria.since and day < criteria.since:
                    continue
                if criteria.before and day >= criteria.before:
                    continue
            out.append(m.uid)
        return out

    def summaries(self, account: Account, folder: str, uids: list[int]) -> list[Summary]:
        wanted = set(uids)
        return [
            Summary(
                m.uid,
                m.raw.split(b"\r\n\r\n")[0].split(b"\n\n")[0] + b"\r\n\r\n",
                "\\Seen" in m.flags,
                "\\Flagged" in m.flags,
                len(m.raw),
            )
            for m in self._box(account, folder)
            if m.uid in wanted
        ]

    def fetch(self, account: Account, folder: str, uid: int) -> tuple[bytes, Summary] | None:
        for m in self._box(account, folder):
            if m.uid == uid:
                return m.raw[:MAX_RAW], Summary(
                    m.uid, b"", "\\Seen" in m.flags, "\\Flagged" in m.flags, len(m.raw)
                )
        return None

    def append(self, account: Account, role: str, raw: bytes, flags: tuple[str, ...]) -> str:
        for name, r in self.roles[account.email].items():
            if r == role:
                box = self.boxes[account.email][name]
                uid = max((m.uid for m in box), default=0) + 1
                box.append(_Stored(uid, raw, set(flags)))
                return name
        raise MailError(f"the account has no {role} folder")

    def remove_draft(self, account: Account, folder: str, message_id: str) -> bool:
        box = self._box(account, folder)
        for m in list(box):
            msg = parse_headers(m.raw)
            if "\\Draft" in m.flags and header(msg, "Message-ID") == message_id:
                box.remove(m)
                return True
        return False

    def send(self, account: Account, raw: bytes, recipients: list[str]) -> list[str]:
        self.outbox.append((account.email, raw, recipients))
        return []
