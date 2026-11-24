# SPDX-License-Identifier: Apache-2.0
from email import policy
from email.parser import BytesParser

import pytest

from vilna_mcp_mail.mail import (
    MAX_LIST,
    MAX_RECIPIENTS,
    MAX_TEXT,
    Account,
    Draft,
    Mailbox,
    MailError,
    MemoryStore,
    address,
    html_text,
)
from vilna_mcp_mail.server import DEMO_ACCOUNT, demo_store


@pytest.fixture
def box():
    return Mailbox(demo_store())


def parsed(raw: bytes):
    return BytesParser(policy=policy.default).parsebytes(raw)


def test_one_account_is_the_default_for_reading(box):
    assert box.list_accounts()["accounts"][0]["account"] == DEMO_ACCOUNT
    assert box.list_messages()["account"] == DEMO_ACCOUNT


def test_listing_is_newest_first_and_bounded(box):
    out = box.list_messages()
    assert [m["subject"] for m in out["messages"]] == [
        "This week",
        "Lunch on Friday?",
        "Invoice 2027-014",
    ]
    assert out["total"] == 3 and not out["truncated"]
    assert out["messages"][2]["unread"] is False
    assert box.list_messages(limit=1)["truncated"] is True
    assert [m["subject"] for m in box.list_messages(unread_only=True)["messages"]] == [
        "This week",
        "Lunch on Friday?",
    ]
    with pytest.raises(MailError, match="limit"):
        box.list_messages(limit=MAX_LIST + 1)


def test_reading_leaves_a_message_unread(box):
    uid = box.search_messages(query="lunch")["messages"][0]["uid"]
    msg = box.read_message(uid)
    assert msg["from"] == "Marta Kowalczyk <marta@example.com>"
    assert "Friday at 13:00" in msg["text"]
    assert msg["message_id"] == "<demo2@example.net>"
    assert box.read_message(uid)["unread"] is True
    with pytest.raises(MailError, match="no message 99"):
        box.read_message(99)


def test_search_by_sender_and_dates(box):
    assert [m["subject"] for m in box.search_messages(sender="billing")["messages"]] == [
        "Invoice 2027-014"
    ]
    with pytest.raises(MailError, match="what to look for"):
        box.search_messages()
    with pytest.raises(MailError, match="until"):
        box.search_messages(since="2027-03-05", until="2027-03-01")
    with pytest.raises(MailError, match="quotes"):
        box.search_messages(query='a" OR ALL "')


def test_send_builds_the_message_from_its_arguments_only(box):
    out = box.send(
        account=DEMO_ACCOUNT,
        to=["Marta Kowalczyk <marta@example.com>"],
        bcc=["boss@example.com"],
        subject="Re: Lunch on Friday?",
        text="Yes, see you at 13:00.\n\nOla",
        in_reply_to="<demo2@example.net>",
    )
    assert out["sent"] is True and out["recipients"] == 2
    sent_from, raw, rcpts = box.store.outbox[-1]
    assert sent_from == DEMO_ACCOUNT
    assert rcpts == ["marta@example.com", "boss@example.com"]
    msg = parsed(raw)
    assert msg["From"] == f"Ola Nowak <{DEMO_ACCOUNT}>"
    assert msg["To"] == "Marta Kowalczyk <marta@example.com>"
    assert msg["Bcc"] is None  # never in the message itself
    assert msg["In-Reply-To"] == "<demo2@example.net>"
    assert msg.get_body().get_content().replace("\r\n", "\n") == "Yes, see you at 13:00.\n\nOla\n"
    # Exactly these headers, nothing Gloxi did not give or the dialog did not show.
    assert sorted(k.lower() for k in msg) == sorted(
        [
            "from",
            "to",
            "subject",
            "date",
            "message-id",
            "in-reply-to",
            "references",
            "content-type",
            "content-transfer-encoding",
            "mime-version",
        ]
    )
    # A copy in Sent, without Bcc.
    sent = box.list_messages(folder="Sent")["messages"]
    assert [m["subject"] for m in sent] == ["Re: Lunch on Friday?"]


def test_send_needs_the_sender_named(box):
    with pytest.raises(MailError, match="which account"):
        box.send(to=["a@example.com"], subject="Hi", text="Hi")
    with pytest.raises(MailError, match="unknown account"):
        box.send(account="other@example.org", to=["a@example.com"], subject="Hi", text="Hi")
    assert box.store.outbox == []


@pytest.mark.parametrize(
    "to",
    [
        ["not an address"],
        ["a@example.com\r\nBcc: x@evil.example"],
        ["a@example.com, b@example.com"],
        ["<a@example.com"],
        [],
        "a@example.com",
    ],
)
def test_bad_recipients_are_refused(box, to):
    with pytest.raises(MailError):
        box.send(account=DEMO_ACCOUNT, to=to, subject="Hi", text="Hi")
    assert box.store.outbox == []


def test_headers_cannot_be_injected():
    with pytest.raises(MailError, match="one line"):
        Draft.check(to=["a@example.com"], subject="Hi\r\nBcc: x@evil.example", text="")
    with pytest.raises(MailError, match="message_id"):
        Draft.check(to=["a@example.com"], subject="Hi", text="", in_reply_to="<a@b>\r\nBcc: x@y.z")
    with pytest.raises(MailError, match="at most"):
        Draft.check(
            to=[f"a{i}@example.com" for i in range(MAX_RECIPIENTS + 1)], subject="", text=""
        )


def test_address_forms():
    assert address("ola@example.org") == "ola@example.org"
    assert address("Ola Nowak <ola@example.org>") == "Ola Nowak <ola@example.org>"
    assert address("Ковальчук Марта <marta@example.com>") == "Ковальчук Марта <marta@example.com>"


def test_unicode_survives_sending(box):
    box.send(
        account=DEMO_ACCOUNT, to=["марта <m@example.com>"], subject="Привіт", text="Як ти? ї є ґ"
    )
    msg = parsed(box.store.outbox[-1][1])
    assert msg["Subject"] == "Привіт"
    assert msg.get_body().get_content().strip() == "Як ти? ї є ґ"
    assert box.store.outbox[-1][1].isascii()  # encoded for any SMTP server


def test_a_draft_is_saved_and_undone(box):
    out = box.save_draft(account=DEMO_ACCOUNT, to=["marta@example.com"], subject="Plan", text="…")
    assert out["folder"] == "Drafts" and ".gloxi@example.org>" in out["message_id"]
    assert box.list_folders()["folders"][2] == {
        "folder": "Drafts",
        "role": "drafts",
        "messages": 1,
        "unread": 0,
    }
    assert box.store.outbox == []  # a draft is not sent
    undone = box.restore(**out["undo_arguments"])
    assert undone["removed"] is True
    assert box.list_messages(folder="Drafts")["total"] == 0


def test_restore_removes_only_drafts_gloxi_wrote(box):
    with pytest.raises(MailError, match="save_draft"):
        box.restore(account=DEMO_ACCOUNT, folder="INBOX", message_id="<demo2@example.net>")


def test_an_account_with_a_problem_is_listed_but_not_used():
    store = MemoryStore([Account("a", "a@example.org", problem="sign in again")])
    box = Mailbox(store)
    assert box.list_accounts()["accounts"][0]["can_send"] is False
    with pytest.raises(MailError, match="sign in again"):
        box.send(account="a@example.org", to=["b@example.org"], subject="", text="")
    with pytest.raises(MailError, match="no mail account"):
        box.list_messages()


def test_html_mail_is_read_as_text_and_long_text_is_cut():
    store = MemoryStore([Account("a", "a@example.org")])
    page = (
        "<html><head><style>p{color:red}</style></head><body><p>Hello&nbsp;Ola</p>"
        "<script>steal()</script><div>Second<br>line</div></body></html>"
    )
    raw = (
        "From: x@example.com\r\nSubject: S\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + page
    ).encode()
    long = ("From: x@example.com\r\nSubject: L\r\n\r\n" + "a" * (MAX_TEXT + 10)).encode()
    uid = store.add("a@example.org", "INBOX", raw)
    uid2 = store.add("a@example.org", "INBOX", long)
    box = Mailbox(store)
    assert box.read_message(uid)["text"] == "Hello Ola\n\nSecond\nline"
    cut = box.read_message(uid2)
    assert len(cut["text"]) == MAX_TEXT and cut["text_truncated"] is True


def test_attachments_are_named_not_given():
    store = MemoryStore([Account("a", "a@example.org")])
    raw = (
        b"From: x@example.com\r\nSubject: Invoice\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/plain\r\n\r\nSee attached.\r\n"
        b"--b\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="inv.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n--b--\r\n"
    )
    uid = store.add("a@example.org", "INBOX", raw)
    msg = Mailbox(store).read_message(uid)
    assert msg["text"].strip() == "See attached."
    assert msg["attachments"] == [{"name": "inv.pdf", "type": "application/pdf", "size": 9}]
    assert "JVBER" not in str(msg)


def test_broken_mail_does_not_break_reading():
    store = MemoryStore([Account("a", "a@example.org")])
    uid = store.add("a@example.org", "INBOX", b"\xff\xfe\x00garbage without headers")
    out = Mailbox(store).read_message(uid)
    assert out["uid"] == uid and isinstance(out["text"], str)


def test_html_text_ignores_broken_markup():
    assert html_text("<p>a<p>b</div><").startswith("a\nb")


def test_sending_a_reply_takes_its_own_draft_out_of_drafts(box):
    reply = {
        "account": DEMO_ACCOUNT,
        "to": ["marta@example.com"],
        "subject": "Re: Lunch on Friday?",
        "text": "Yes.",
    }
    saved = box.save_draft(**reply)
    assert [m["subject"] for m in box.list_messages(folder="Drafts")["messages"]] == [
        "Re: Lunch on Friday?"
    ]
    out = box.send(**reply, replaces_draft=saved["message_id"])
    assert out["sent"] is True and out["draft_removed"] is True
    assert box.list_messages(folder="Drafts")["messages"] == []
    # Only a draft Gloxi wrote; what is sent is built from the arguments alone.
    with pytest.raises(MailError, match="replaces_draft"):
        box.send(**reply, replaces_draft="<demo2@example.net>")
    assert box.send(**reply, replaces_draft=saved["message_id"])["draft_removed"] is False
