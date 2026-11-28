# SPDX-License-Identifier: Apache-2.0
"""Real mailboxes: IMAP for reading and drafts, SMTP for sending (ADR 0030).

Only the standard library talks to the servers. Where the accounts and their
secrets come from is someone else's job (``goa.py``: GNOME Online Accounts);
this module asks for them each time it connects and keeps no secret in a
file, a log or an answer.

- Mail goes only over TLS (implicit TLS or STARTTLS) with the certificate
  checked. A plain connection, or one that accepts certificate errors, is
  allowed only to this computer itself (127.0.0.1, ::1, localhost), where
  bridges such as Proton Mail Bridge listen: nothing leaves the machine there.
- Folders are opened read-only (EXAMINE) and messages fetched with
  ``BODY.PEEK``, so reading leaves every message as it was.
- One connection per operation: slower, but nothing stays logged in while
  Gloxi is idle.
"""

from __future__ import annotations

import base64
import imaplib
import re
import smtplib
import socket
import ssl
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from vilna_mcp_mail.mail import (
    _CONTROL,
    MAX_FOLDERS,
    MAX_RAW,
    Account,
    Criteria,
    Folder,
    MailError,
    Summary,
)

TIMEOUT = 30  # seconds for one server to answer
MAX_UIDS = 100_000  # UIDs considered from one search
MAX_STATUS = 50  # folders whose counts are asked
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_SPECIAL = {
    "\\sent": "sent",
    "\\drafts": "drafts",
    "\\trash": "trash",
    "\\junk": "junk",
    "\\archive": "archive",
    "\\all": "all",
    "\\flagged": "flagged",
}
# When a server does not mark its folders (no SPECIAL-USE), the usual names.
_BY_NAME = {
    "sent": "sent",
    "sent items": "sent",
    "sent messages": "sent",
    "sent mail": "sent",
    "drafts": "drafts",
    "draft": "drafts",
    "trash": "trash",
    "deleted items": "trash",
    "deleted messages": "trash",
    "junk": "junk",
    "spam": "junk",
    "archive": "archive",
}
_HEADER_FIELDS = "BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)]"


@dataclass(frozen=True)
class Server:
    host: str
    port: int
    security: str  # "ssl" (implicit TLS), "starttls" or "none"
    user: str
    accept_ssl_errors: bool = False


@dataclass(frozen=True)
class Login:
    kind: str  # "password" or "xoauth2"
    secret: str = field(repr=False)


class Directory(Protocol):
    """Where accounts, servers and secrets come from (GNOME Online Accounts)."""

    def accounts(self) -> list[Account]: ...

    def imap(self, account: Account) -> tuple[Server, Login]: ...

    def smtp(self, account: Account) -> tuple[Server, Login | None]: ...


def split_host(raw: str, security: str, kind: str) -> tuple[str, int]:
    """``mail.example.org`` or ``mail.example.org:1993`` (GOA keeps the port in the host)."""
    host, port = raw.strip(), 0
    if host.startswith("["):  # [::1]:1143
        inside, _, rest = host[1:].partition("]")
        host = inside
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
    elif host.count(":") == 1:
        name, _, number = host.partition(":")
        if number.isdigit():
            host, port = name, int(number)
    if not port:
        if kind == "imap":
            port = 993 if security == "ssl" else 143
        else:
            port = 465 if security == "ssl" else 587 if security == "starttls" else 25
    return host, port


def security_of(use_ssl: bool, use_tls: bool) -> str:
    return "ssl" if use_ssl else "starttls" if use_tls else "none"


def checked(server: Server) -> Server:
    """Refuse what would put mail or a password on the network in the clear."""
    local = server.host.strip("[]").lower() in LOOPBACK
    if server.security not in ("ssl", "starttls", "none"):
        raise MailError(f"unknown connection security {server.security!r}")
    if not local and server.security == "none":
        raise MailError(
            f"{server.host} is set up without encryption; Gloxi only uses encrypted "
            "connections (turn on SSL/TLS or STARTTLS in the account settings)"
        )
    if not local and server.accept_ssl_errors:
        raise MailError(
            f"the account accepts certificate errors from {server.host}; Gloxi does not "
            "(fix the server's certificate or turn that option off)"
        )
    if not 0 < server.port < 65536:
        raise MailError(f"bad port {server.port}")
    return server


def _tls(server: Server) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if server.accept_ssl_errors:  # loopback only, see checked()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --- Modified UTF-7 (RFC 3501, 5.1.3) for folder names ---------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").rstrip("=").replace("/", ",")


def utf7_encode(name: str) -> str:
    out, pending = [], []

    def flush() -> None:
        if pending:
            out.append("&" + _b64("".join(pending).encode("utf-16-be")) + "-")
            pending.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(out)


def utf7_decode(name: str) -> str:
    def one(m: re.Match) -> str:
        body = m.group(1)
        if not body:
            return "&"
        b = body.replace(",", "/")
        b += "=" * (-len(b) % 4)
        try:
            return base64.b64decode(b).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return m.group(0)

    return re.sub(r"&([A-Za-z0-9+,]*)-", one, name)


# --- Parsing what IMAP says -----------------------------------------------------------

_LIST = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"(?:[^"\\]|\\.)*"|NIL) (?P<name>.*)$')


def _unquote(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = re.sub(r"\\(.)", r"\1", text[1:-1])
    return text


def parse_list(data: list) -> list[tuple[str, set[str]]]:
    """``(\\HasNoChildren \\Sent) "/" "Sent"`` lines → [(encoded name, flags)]."""
    out = []
    for item in data:
        if isinstance(item, tuple) and len(item) == 2:  # the name came as a literal
            line, name = item[0], item[1]
            m = _LIST.match(re.sub(rb" \{\d+\}$", b" X", line))
            raw_name = name
        elif isinstance(item, bytes):
            m = _LIST.match(item)
            raw_name = m.group("name") if m else b""
        else:
            continue
        if not m:
            continue
        flags = {f.lower() for f in m.group("flags").decode("ascii", "replace").split()}
        out.append((_unquote(raw_name), flags))
    return out


def parse_fetch(data: list) -> dict[int, tuple[bytes, set[str], int]]:
    """FETCH answers → {uid: (literal, flags, size)}."""
    out: dict[int, tuple[bytes, set[str], int]] = {}
    for i, item in enumerate(data):
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        meta = item[0] if isinstance(item[0], bytes) else b""
        after = data[i + 1] if i + 1 < len(data) and isinstance(data[i + 1], bytes) else b""
        meta += b" " + after
        uid = re.search(rb"\bUID (\d+)", meta)
        if not uid:
            continue
        flags = re.search(rb"\bFLAGS \(([^)]*)\)", meta)
        size = re.search(rb"\bRFC822\.SIZE (\d+)", meta)
        literal = item[1] if isinstance(item[1], bytes) else b""
        out[int(uid.group(1))] = (
            literal,
            set(flags.group(1).decode("ascii", "replace").lower().split()) if flags else set(),
            int(size.group(1)) if size else len(literal),
        )
    return out


def imap_date(d: date) -> str:
    return f"{d.day}-{_MONTHS[d.month - 1]}-{d.year}"


def search_terms(criteria: Criteria) -> tuple[list[str], bytes | None]:
    """IMAP SEARCH keys and at most one UTF-8 literal, which goes last."""
    keys: list[str] = []
    literal: bytes | None = None
    pending: list[tuple[str, str]] = []
    if criteria.unread:
        keys.append("UNSEEN")
    if criteria.since:
        keys += ["SENTSINCE", imap_date(criteria.since)]
    if criteria.before:
        keys += ["SENTBEFORE", imap_date(criteria.before)]
    for value in (criteria.sender, criteria.text):
        if value and _CONTROL.search(value):
            raise MailError("search words cannot hold control characters")
    if criteria.sender:
        pending.append(("FROM", criteria.sender))
    if criteria.text:
        pending.append(("TEXT", criteria.text))
    wide = [(k, v) for k, v in pending if not v.isascii()]
    if len(wide) > 1:
        raise MailError("search with one non-Latin word at a time (query or from)")
    for k, v in pending:
        if v.isascii():
            keys += [k, quote(v)]
    if wide:
        keys.append(wide[0][0])
        literal = wide[0][1].encode("utf-8")
    if not keys:
        keys.append("ALL")
    return keys, literal


def _ok(answer: tuple, what: str) -> list:
    typ, data = answer
    if typ != "OK":
        said = b" ".join(d for d in data if isinstance(d, bytes)).decode("utf-8", "replace")
        raise MailError(f"the mail server refused to {what}: {said[:200]}")
    return data


class ImapSmtpStore:
    def __init__(
        self,
        directory: Directory,
        imap_factory: Callable[..., imaplib.IMAP4] | None = None,
    ) -> None:
        self.directory = directory
        self._imap_factory = imap_factory
        self._roles: dict[str, dict[str, str]] = {}  # account → role → encoded name

    def accounts(self) -> list[Account]:
        return self.directory.accounts()

    # -- IMAP --

    @contextmanager
    def _imap(self, account: Account) -> Iterator[imaplib.IMAP4]:
        if not account.can_read:
            raise MailError(f"{account.email} has no IMAP in GNOME Online Accounts")
        server, login = self.directory.imap(account)
        server = checked(server)
        try:
            if self._imap_factory is not None:
                conn = self._imap_factory(server)
            elif server.security == "ssl":
                conn = imaplib.IMAP4_SSL(
                    server.host, server.port, ssl_context=_tls(server), timeout=TIMEOUT
                )
            else:
                conn = imaplib.IMAP4(server.host, server.port, timeout=TIMEOUT)
                if server.security == "starttls":
                    conn.starttls(ssl_context=_tls(server))
        except (OSError, imaplib.IMAP4.error, ssl.SSLError) as e:
            raise MailError(f"cannot reach {server.host}:{server.port}: {_said(e)}") from None
        try:
            self._login(conn, server, login)
            yield conn
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as e:
            raise MailError(f"{server.host}: {_said(e)}") from None
        finally:
            with suppress(Exception):
                conn.logout()

    @staticmethod
    def _login(conn: imaplib.IMAP4, server: Server, login: Login) -> None:
        try:
            if login.kind == "xoauth2":
                token = f"user={server.user}\x01auth=Bearer {login.secret}\x01\x01".encode()
                conn.authenticate("XOAUTH2", lambda _: token)
            elif "AUTH=PLAIN" in conn.capabilities and not (server.user + login.secret).isascii():
                plain = f"\0{server.user}\0{login.secret}".encode()
                conn.authenticate("PLAIN", lambda _: plain)
            else:
                conn.login(server.user, login.secret)
        except imaplib.IMAP4.error:
            # Never the server's words here: some echo what they were sent.
            raise MailError(
                f"{server.host} did not accept the sign-in; check the account in "
                "Settings → Online Accounts"
            ) from None

    def _list(self, conn: imaplib.IMAP4) -> list[tuple[str, set[str]]]:
        found = parse_list(_ok(conn.list(), "list the folders"))
        return [(n, f) for n, f in found if "\\noselect" not in f and "\\nonexistent" not in f]

    @staticmethod
    def _role(name: str, flags: set[str]) -> str:
        if name.upper() == "INBOX":
            return "inbox"
        for f in flags:
            if f in _SPECIAL:
                return _SPECIAL[f]
        return ""

    def folders(self, account: Account) -> list[Folder]:
        out = []
        with self._imap(account) as conn:
            listed = self._list(conn)[:MAX_FOLDERS]
            roles = self._remember(account, listed)
            for i, (name, _flags) in enumerate(listed):
                messages = unread = None
                if i < MAX_STATUS:
                    try:
                        typ, data = conn.status(quote(name), "(MESSAGES UNSEEN)")
                    except UnicodeEncodeError:  # a server name that is not 7-bit
                        typ, data = "NO", []
                    line = data[0] if typ == "OK" and data and isinstance(data[0], bytes) else b""
                    m = re.search(rb"MESSAGES (\d+)", line)
                    u = re.search(rb"UNSEEN (\d+)", line)
                    messages = int(m.group(1)) if m else None
                    unread = int(u.group(1)) if u else None
                role = next((r for r, n in roles.items() if n == name), "")
                out.append(Folder(utf7_decode(name), role, messages, unread))
        return out

    def _remember(self, account: Account, listed: list[tuple[str, set[str]]]) -> dict[str, str]:
        roles: dict[str, str] = {}
        for name, flags in listed:
            role = self._role(name, flags)
            if role and role not in roles:
                roles[role] = name
        for name, _flags in listed:  # servers without SPECIAL-USE
            leaf = re.split(r"[./]", utf7_decode(name))[-1].lower()
            role = _BY_NAME.get(leaf)
            if role and role not in roles:
                roles[role] = name
        self._roles[account.email] = roles
        return roles

    def _examine(self, conn: imaplib.IMAP4, folder: str, *, write: bool = False) -> None:
        name = "INBOX" if folder.upper() == "INBOX" else utf7_encode(folder)
        typ, data = conn.select(quote(name), readonly=not write)
        if typ != "OK":
            raise MailError(f"there is no folder {folder!r}; see list_folders")

    def search(self, account: Account, folder: str, criteria: Criteria) -> list[int]:
        keys, literal = search_terms(criteria)
        with self._imap(account) as conn:
            self._examine(conn, folder)
            if literal is not None:
                conn.literal = literal
                data = _ok(conn.uid("SEARCH", "CHARSET", "UTF-8", *keys), "search")
            else:
                data = _ok(conn.uid("SEARCH", *keys), "search")
        found = b" ".join(d for d in data if isinstance(d, bytes)).split()
        return [int(u) for u in found[-MAX_UIDS:] if u.isdigit()]

    def summaries(self, account: Account, folder: str, uids: list[int]) -> list[Summary]:
        if not uids:
            return []
        with self._imap(account) as conn:
            self._examine(conn, folder)
            what = f"(UID FLAGS RFC822.SIZE {_HEADER_FIELDS})"
            data = _ok(conn.uid("FETCH", ",".join(str(u) for u in uids), what), "read")
        got = parse_fetch(data)
        return [
            Summary(uid, lit, "\\seen" in flags, "\\flagged" in flags, size)
            for uid, (lit, flags, size) in got.items()
            if uid in set(uids)
        ]

    def fetch(self, account: Account, folder: str, uid: int) -> tuple[bytes, Summary] | None:
        with self._imap(account) as conn:
            self._examine(conn, folder)
            what = f"(UID FLAGS RFC822.SIZE BODY.PEEK[]<0.{MAX_RAW}>)"
            data = _ok(conn.uid("FETCH", str(uid), what), "read the message")
        got = parse_fetch(data).get(uid)
        if got is None:
            return None
        raw, flags, size = got
        return raw[:MAX_RAW], Summary(uid, b"", "\\seen" in flags, "\\flagged" in flags, size)

    def _folder_for(self, conn: imaplib.IMAP4, account: Account, role: str) -> str:
        roles = self._remember(account, self._list(conn))
        if role not in roles:
            raise MailError(f"the account has no {role} folder")
        return roles[role]

    def append(self, account: Account, role: str, raw: bytes, flags: tuple[str, ...]) -> str:
        with self._imap(account) as conn:
            name = self._folder_for(conn, account, role)
            stamp = imaplib.Time2Internaldate(time.time())
            _ok(conn.append(quote(name), "(" + " ".join(flags) + ")", stamp, raw), "save")
        return utf7_decode(name)

    def remove_draft(self, account: Account, folder: str, message_id: str) -> bool:
        with self._imap(account) as conn:
            self._examine(conn, folder, write=True)
            data = _ok(
                conn.uid("SEARCH", "DRAFT", "HEADER", "Message-ID", quote(message_id)), "search"
            )
            uids = [u.decode() for u in b" ".join(data).split() if u.isdigit()]
            if not uids:
                return False
            _ok(conn.uid("STORE", ",".join(uids), "+FLAGS.SILENT", "(\\Deleted)"), "remove")
            # Only these messages: EXPUNGE alone would also remove whatever
            # else the person marked as deleted.
            if "UIDPLUS" in conn.capabilities:
                _ok(conn.uid("EXPUNGE", ",".join(uids)), "remove")
        return True

    # -- SMTP --

    def send(self, account: Account, raw: bytes, recipients: list[str]) -> list[str]:
        if not account.can_send:
            raise MailError(f"{account.email} has no SMTP in GNOME Online Accounts")
        server, login = self.directory.smtp(account)
        server = checked(server)
        try:
            if server.security == "ssl":
                smtp = smtplib.SMTP_SSL(
                    server.host, server.port, context=_tls(server), timeout=TIMEOUT
                )
            else:
                smtp = smtplib.SMTP(server.host, server.port, timeout=TIMEOUT)
                smtp.ehlo()
                if server.security == "starttls":
                    smtp.starttls(context=_tls(server))
                    smtp.ehlo()
        except (OSError, smtplib.SMTPException, ssl.SSLError) as e:
            raise MailError(f"cannot reach {server.host}:{server.port}: {_said(e)}") from None
        try:
            if login is not None:
                try:
                    if login.kind == "xoauth2":
                        token = f"user={server.user}\x01auth=Bearer {login.secret}\x01\x01"
                        smtp.auth("XOAUTH2", lambda challenge=None: token)
                    elif (server.user + login.secret).isascii():
                        smtp.login(server.user, login.secret)
                    else:
                        # smtplib sends only ASCII; RFC 4616 PLAIN is UTF-8.
                        plain = f"\0{server.user}\0{login.secret}".encode()
                        code, _said_back = smtp.docmd(
                            "AUTH", "PLAIN " + base64.b64encode(plain).decode("ascii")
                        )
                        if code != 235:
                            raise smtplib.SMTPAuthenticationError(code, b"")
                except smtplib.SMTPException:
                    raise MailError(
                        f"{server.host} did not accept the sign-in for sending; check the "
                        "account in Settings → Online Accounts"
                    ) from None
            refused = smtp.sendmail(account.email, recipients, raw)
        except smtplib.SMTPRecipientsRefused as e:
            raise MailError(f"no recipient was accepted: {', '.join(e.recipients)}") from None
        except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
            raise MailError(f"{server.host} did not take the message: {_said(e)}") from None
        finally:
            with suppress(Exception):
                smtp.quit()
        return sorted(refused)  # sent to the others; these did not get it


def _said(e: BaseException) -> str:
    if isinstance(e, socket.timeout):
        return "no answer"
    text = " ".join(str(e).split())
    return text[:200] or type(e).__name__
