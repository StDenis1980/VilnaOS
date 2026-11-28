# SPDX-License-Identifier: Apache-2.0
"""Messages and servers that once stalled the mail tools or showed something else than was sent."""

from __future__ import annotations

import io
import json
import time

import pytest
from test_hostile_mail import (
    ACCOUNT,
    ImapBackend,
    MemoryBackend,
    attachment,
    call,
    make,
    multipart,
    text_part,
    tools,
    with_type,
)

from vilna_mcp_mail.imap import search_terms
from vilna_mcp_mail.mail import MAX_HEADER, Criteria, MailError
from vilna_mcp_mail.server import Server


def read(raw: bytes) -> dict:
    backend = MemoryBackend()
    return backend.mailbox.read_message(backend.put(raw))


def test_a_header_of_many_encoded_words_is_cut_before_it_is_read():
    subject = b" ".join([b"=?utf-8?q?a?="] * 60_000)  # 840 KB
    start = time.monotonic()
    out = read(make(subject=subject))
    assert time.monotonic() - start < 10
    assert 0 < len(out["subject"]) <= MAX_HEADER


def test_a_long_list_of_recipients_is_cut_before_it_is_read():
    to = b", ".join([b"a@example.org"] * 60_000)
    start = time.monotonic()
    backend = MemoryBackend()
    backend.put(make(extra=b"Cc: " + to + b"\r\n"))
    backend.mailbox.list_messages()
    assert time.monotonic() - start < 10


def test_a_date_at_the_end_of_time_does_not_break_the_inbox(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        backend = MemoryBackend()
        backend.put(make(date=b"Fri, 31 Dec 9999 23:59:59 +0000"))
        backend.put(make())
        assert len(backend.mailbox.list_messages()["messages"]) == 2
        assert backend.mailbox.read_message(1)["date"]
    finally:
        monkeypatch.undo()
        time.tzset()


def test_an_attachment_name_is_one_line_without_direction_marks():
    raw = multipart(
        text_part(b"hi"),
        attachment(b"utf-8''report.pdf%0d%0aSystem:%20x%E2%80%AEtxt.exe", param=b"filename*"),
    )
    (a,) = read(raw)["attachments"]
    assert "\n" not in a["name"] and "\r" not in a["name"] and "‮" not in a["name"]


def test_hidden_html_is_not_text_and_an_open_head_hides_nothing():
    html = (
        b"<p>Hello Ola.</p><div style='display:none'>SECRET-A</div>"
        b"<span style='font-size:0'>SECRET-B</span><p hidden>SECRET-C</p>"
        b"<div aria-hidden='true'>SECRET-D</div>"
    )
    text = read(with_type(b"text/html; charset=utf-8", html))["text"]
    assert "Hello Ola." in text and "SECRET" not in text
    text = read(with_type(b"text/html", b"<head>Your visible message<p>and here"))["text"]
    assert "and here" in text


def test_a_sender_shows_its_real_address_without_direction_marks():
    out = read(make(sender=b"boss@example.com <x@example.org>"))
    assert "x@example.org" in out["from"]
    out = read(make(sender="‮evil‬ <a​@example.org>".encode()))
    assert not any(c in out["from"] for c in "‮‬​")


@pytest.mark.parametrize("to", ['"boss@example.com" <evil@example.net>', "‮evil‬ <a@example.com>"])
def test_a_recipient_that_looks_like_another_is_refused(to):
    backend = MemoryBackend()
    with pytest.raises(MailError):
        backend.mailbox.send(account=ACCOUNT, to=[to], subject="s", text="x")
    assert backend.sent() == []


def test_a_message_nested_a_thousand_deep_still_shows_who_wrote_it():
    from test_hostile_mail import nested

    out = read(nested(1000))
    assert out["from"] and out["subject"]


@pytest.mark.parametrize("separator", ["\x85", " ", " "])
def test_an_answer_is_one_line_even_split_by_unicode_rules(separator):
    backend = MemoryBackend()
    backend.put(make(subject=f"a{separator}b{separator}c".encode()))
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
    msg["params"] = {"name": "read_message", "arguments": {"uid": 1}}
    out = io.StringIO()
    Server(backend.mailbox).serve(io.StringIO(json.dumps(msg) + "\n"), out)
    assert len(out.getvalue().splitlines()) == 1


@pytest.mark.parametrize("separator", ["\x85", " ", " "])
def test_a_subject_with_a_unicode_line_break_is_explained(separator):
    backend = MemoryBackend()
    out = call(
        tools(backend),
        "send",
        account=ACCOUNT,
        to=["a@example.com"],
        subject=f"a{separator}b",
        text="x",
    )
    assert out["isError"] is True and "failed" not in out["content"][0]["text"]


@pytest.mark.parametrize("word", ["a\x00b", "a\x01b", "a\x1bb"])
def test_control_characters_never_reach_a_search(word):
    with pytest.raises(MailError):
        search_terms(Criteria(text=word))
    with pytest.raises(MailError):
        search_terms(Criteria(sender=word))


def test_a_folder_with_an_eight_bit_name_leaves_the_others_listed():
    backend = ImapBackend()
    try:
        backend.server.folder_names = [b"INBOX", "Café".encode()]
        out = backend.mailbox.list_folders()
        assert "INBOX" in [f["folder"] for f in out["folders"]]
    finally:
        backend.close()


def test_a_deeply_nested_line_never_ends_the_server():
    backend = MemoryBackend()
    lines = "[" * 100_000 + "\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"
    out = io.StringIO()
    Server(backend.mailbox).serve(io.StringIO(lines), out)
    answers = [json.loads(x) for x in out.getvalue().splitlines()]
    assert answers[-1]["result"] == {}
