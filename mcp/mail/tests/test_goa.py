# SPDX-License-Identifier: Apache-2.0
"""Accounts from GNOME Online Accounts, with GOA itself stood in for."""

import sys
import types
from types import SimpleNamespace

import pytest


class _GLibError(Exception):
    message = "no GOA"


class _Obj:
    def __init__(self, n):
        self.account = SimpleNamespace(
            props=SimpleNamespace(
                id=f"goa{n}", mail_disabled=False, attention_needed=False, provider_type="imap_smtp"
            )
        )
        self.mail = SimpleNamespace(
            props=SimpleNamespace(
                email_address=f"user{n}@example.org",
                name="",
                imap_supported=True,
                smtp_supported=True,
            )
        )

    def get_account(self):
        return self.account

    def get_mail(self):
        return self.mail


# What GOA holds now. A real Goa.Client made earlier keeps the list it had,
# because nothing runs the GLib main loop that would tell it of new ones.
ACCOUNTS: list = []


class _Client:
    def __init__(self):
        self._seen = list(ACCOUNTS)

    @classmethod
    def new_sync(cls, cancellable):
        return cls()

    def get_accounts(self):
        return list(self._seen)


@pytest.fixture
def goa(monkeypatch):
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a: None
    repo = types.ModuleType("gi.repository")
    repo.GLib = SimpleNamespace(Error=_GLibError)
    repo.Goa = SimpleNamespace(Client=_Client)
    gi.repository = repo
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repo)
    monkeypatch.delitem(sys.modules, "vilna_mcp_mail.goa", raising=False)
    ACCOUNTS.clear()
    import vilna_mcp_mail.goa as module

    yield module
    sys.modules.pop("vilna_mcp_mail.goa", None)


def test_an_account_added_after_start_is_seen(goa):
    ACCOUNTS.append(_Obj(1))
    directory = goa.GoaDirectory()
    assert [a.email for a in directory.accounts()] == ["user1@example.org"]
    ACCOUNTS.append(_Obj(2))  # added in Settings while the tool runs
    assert [a.email for a in directory.accounts()] == ["user1@example.org", "user2@example.org"]
