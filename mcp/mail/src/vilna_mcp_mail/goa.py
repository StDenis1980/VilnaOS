# SPDX-License-Identifier: Apache-2.0
"""Mail accounts from GNOME Online Accounts (ADR 0030).

The person adds an account once, in Settings → Online Accounts: IMAP and
SMTP, Google, Microsoft 365. GOA keeps the password or the sign-in token in
the keyring; we ask for it each time we connect (``GetPassword`` or
``GetAccessToken``) and keep it only for that connection. PyGObject and the
Goa typelib come from the system, so this module is imported only when the
server really starts.
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Goa", "1.0")
from gi.repository import GLib, Goa  # noqa: E402

from vilna_mcp_mail.imap import Login, Server, security_of, split_host  # noqa: E402
from vilna_mcp_mail.mail import Account, MailError  # noqa: E402

# Providers whose SMTP server puts a copy of what it sends into Sent itself.
SAVES_SENT = {"google", "ms365", "windows_live", "exchange"}


def _out(answer: Any) -> Any:
    """PyGObject gives an out value alone or after the boolean result."""
    if isinstance(answer, tuple):
        return answer[-1] if len(answer) else None
    return answer


def _client() -> Any:
    try:
        return Goa.Client.new_sync(None)
    except GLib.Error as e:
        raise MailError(f"GNOME Online Accounts is not reachable: {e.message}") from e


class GoaDirectory:
    def __init__(self) -> None:
        self._client = _client()  # say at start when GOA is not there at all
        self._objects: dict[str, Any] = {}

    def accounts(self) -> list[Account]:
        # A fresh client each time: the tool runs no GLib main loop, so one
        # client made at start never learns of an account added after it.
        client = self._client = _client()  # kept alive with its accounts
        out = []
        self._objects.clear()
        for obj in client.get_accounts():
            account, mail = obj.get_account(), obj.get_mail()
            if account is None or mail is None or account.props.mail_disabled:
                continue
            email = mail.props.email_address or ""
            if not email:
                continue
            problem = ""
            if account.props.attention_needed:
                problem = "GNOME Online Accounts needs you to sign in to it again"
            out.append(
                Account(
                    id=account.props.id,
                    email=email,
                    name=mail.props.name or "",
                    provider=account.props.provider_type or "",
                    can_read=bool(mail.props.imap_supported),
                    can_send=bool(mail.props.smtp_supported),
                    saves_sent=(account.props.provider_type or "") in SAVES_SENT,
                    problem=problem,
                )
            )
            self._objects[email.lower()] = obj
        return out

    def _object(self, account: Account) -> Any:
        if account.email.lower() not in self._objects:
            self.accounts()
        obj = self._objects.get(account.email.lower())
        if obj is None:
            raise MailError(f"the account {account.email} is gone from GNOME Online Accounts")
        return obj

    def _login(self, obj: Any, password_id: str) -> Login:
        try:
            obj.get_account().call_ensure_credentials_sync(None)
            oauth2 = obj.get_oauth2_based()
            if oauth2 is not None:
                answer = oauth2.call_get_access_token_sync(None)
                token = answer[1] if isinstance(answer, tuple) and len(answer) >= 2 else answer
                return Login("xoauth2", str(token))
            password = obj.get_password_based()
            if password is not None:
                return Login(
                    "password", str(_out(password.call_get_password_sync(password_id, None)))
                )
        except GLib.Error:
            raise MailError(
                "GNOME Online Accounts could not give the sign-in for this account; "
                "open Settings → Online Accounts"
            ) from None
        raise MailError("this account signs in in a way Gloxi does not know yet")

    def imap(self, account: Account) -> tuple[Server, Login]:
        obj = self._object(account)
        mail = obj.get_mail()
        security = security_of(mail.props.imap_use_ssl, mail.props.imap_use_tls)
        host, port = split_host(mail.props.imap_host or "", security, "imap")
        server = Server(
            host,
            port,
            security,
            mail.props.imap_user_name or account.email,
            bool(mail.props.imap_accept_ssl_errors),
        )
        return server, self._login(obj, "imap-password")

    def smtp(self, account: Account) -> tuple[Server, Login | None]:
        obj = self._object(account)
        mail = obj.get_mail()
        security = security_of(mail.props.smtp_use_ssl, mail.props.smtp_use_tls)
        host, port = split_host(mail.props.smtp_host or "", security, "smtp")
        server = Server(
            host,
            port,
            security,
            mail.props.smtp_user_name or account.email,
            bool(mail.props.smtp_accept_ssl_errors),
        )
        if not mail.props.smtp_use_auth and obj.get_oauth2_based() is None:
            return server, None
        return server, self._login(obj, "smtp-password")
