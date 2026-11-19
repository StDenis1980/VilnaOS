# SPDX-License-Identifier: Apache-2.0
"""IMAP and SMTP: the parsing on its own, SMTP against a tiny local server,
and everything against a real Dovecot when one is installed (skipped otherwise)."""

import base64
import os
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from vilna_mcp_mail.imap import (
    ImapSmtpStore,
    Login,
    Server,
    checked,
    parse_fetch,
    parse_list,
    search_terms,
    split_host,
    utf7_decode,
    utf7_encode,
)
from vilna_mcp_mail.mail import Account, Criteria, Mailbox, MailError

PASSWORD = "пароль-secret"


def test_folder_names_in_modified_utf7():
    for name in ["INBOX", "Надіслані", "A&B", "Ärger/Büro", "日本語"]:
        assert utf7_decode(utf7_encode(name)) == name
    assert utf7_encode("A&B") == "A&-B"
    assert utf7_decode("&BB0EMAQ0BFYEQQQ7BDAEPQRW-") == "Надіслані"


def test_parse_list_with_special_use_and_literals():
    data = [
        b'(\\HasNoChildren) "/" INBOX',
        b'(\\HasNoChildren \\Sent) "/" "Sent Items"',
        b'(\\Noselect \\HasChildren) "/" "[Gmail]"',
        (b'(\\HasNoChildren \\Drafts) "/" {6}', b"Drafts"),
        None,
        b"garbage",
    ]
    assert parse_list(data) == [
        ("INBOX", {"\\hasnochildren"}),
        ("Sent Items", {"\\hasnochildren", "\\sent"}),
        ("[Gmail]", {"\\noselect", "\\haschildren"}),
        ("Drafts", {"\\hasnochildren", "\\drafts"}),
    ]


def test_parse_fetch_reads_flags_after_the_literal():
    data = [
        (b"1 (UID 7 RFC822.SIZE 120 BODY[HEADER.FIELDS (FROM)] {20}", b"From: a@example.org\r\n"),
        b" FLAGS (\\Seen \\Flagged))",
        (b"2 (UID 9 FLAGS () BODY[] {3}", b"abc"),
        b")",
    ]
    assert parse_fetch(data) == {
        7: (b"From: a@example.org\r\n", {"\\seen", "\\flagged"}, 120),
        9: (b"abc", set(), 3),
    }


def test_search_terms_put_one_utf8_literal_last():
    keys, literal = search_terms(Criteria(text="рахунок", sender="bank", unread=True))
    assert keys == ["UNSEEN", "FROM", '"bank"', "TEXT"] and literal == "рахунок".encode()
    keys, literal = search_terms(
        Criteria(text="invoice", since=date(2027, 3, 5), before=date(2027, 4, 1))
    )
    assert keys == ["SENTSINCE", "5-Mar-2027", "SENTBEFORE", "1-Apr-2027", "TEXT", '"invoice"']
    assert literal is None
    assert search_terms(Criteria()) == (["ALL"], None)
    with pytest.raises(MailError, match="one non-Latin"):
        search_terms(Criteria(text="рахунок", sender="Марта"))


def test_only_encrypted_connections_leave_the_computer():
    assert checked(Server("imap.example.org", 993, "ssl", "u")).host == "imap.example.org"
    with pytest.raises(MailError, match="without encryption"):
        checked(Server("imap.example.org", 143, "none", "u"))
    with pytest.raises(MailError, match="certificate errors"):
        checked(Server("imap.example.org", 993, "ssl", "u", accept_ssl_errors=True))
    # A bridge on this computer (Proton Mail Bridge): nothing leaves the machine.
    for host in ("127.0.0.1", "localhost", "[::1]"):
        checked(Server(host, 1143, "none", "u"))
        checked(Server(host, 1143, "starttls", "u", accept_ssl_errors=True))


def test_ports_from_goa_hosts():
    assert split_host("imap.example.org", "ssl", "imap") == ("imap.example.org", 993)
    assert split_host("imap.example.org", "starttls", "imap") == ("imap.example.org", 143)
    assert split_host("smtp.example.org", "ssl", "smtp") == ("smtp.example.org", 465)
    assert split_host("smtp.example.org", "starttls", "smtp") == ("smtp.example.org", 587)
    assert split_host("127.0.0.1:1025", "none", "smtp") == ("127.0.0.1", 1025)
    assert split_host("[::1]:1143", "starttls", "imap") == ("::1", 1143)


# --- A tiny SMTP server -----------------------------------------------------------------


class _Smtp(socketserver.StreamRequestHandler):
    def handle(self):
        got = self.server.got
        self.wfile.write(b"220 test ESMTP\r\n")
        data = None
        while line := self.rfile.readline():
            cmd = line.decode().strip()
            word = cmd.split(" ")[0].upper()
            if data is not None:
                if cmd == ".":
                    got["data"] = b"".join(data)
                    data = None
                    self.wfile.write(b"250 queued\r\n")
                else:
                    data.append(line[1:] if line.startswith(b"..") else line)
                continue
            if word == "EHLO":
                self.wfile.write(b"250-test\r\n250 AUTH PLAIN LOGIN\r\n")
            elif word == "AUTH":
                got["auth"] = base64.b64decode(cmd.split(" ")[2]).decode()
                self.wfile.write(b"235 ok\r\n")
            elif word == "MAIL":
                got["from"] = cmd
                self.wfile.write(b"250 ok\r\n")
            elif word == "RCPT":
                if "nobody@" in cmd:
                    self.wfile.write(b"550 no such user\r\n")
                else:
                    got.setdefault("rcpt", []).append(cmd)
                    self.wfile.write(b"250 ok\r\n")
            elif word == "DATA":
                data = []
                self.wfile.write(b"354 go\r\n")
            elif word == "QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 ok\r\n")


@pytest.fixture
def smtp():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Smtp)
    srv.daemon_threads = True
    srv.got = {}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


class Accounts:
    """GNOME Online Accounts stand-in: one account, servers on this computer."""

    def __init__(self, imap_port=1, smtp_port=1, can_read=True):
        self.account = Account("goa1", "ola@example.org", "Ola Nowak", "imap_smtp", can_read)
        self.imap_port, self.smtp_port = imap_port, smtp_port

    def accounts(self):
        return [self.account]

    def imap(self, account):
        return Server("127.0.0.1", self.imap_port, "none", account.email), Login(
            "password", PASSWORD
        )

    def smtp(self, account):
        return Server("127.0.0.1", self.smtp_port, "none", account.email), Login(
            "password", PASSWORD
        )


def test_sending_over_smtp(smtp):
    store = ImapSmtpStore(Accounts(smtp_port=smtp.server_address[1], can_read=False))
    out = Mailbox(store).send(
        account="ola@example.org",
        to=["marta@example.com", "nobody@example.com"],
        subject="Привіт",
        text="Line\n.\nafter a dot",
    )
    got = smtp.got
    assert got["auth"] == f"\0ola@example.org\0{PASSWORD}"
    assert got["from"].upper() == "MAIL FROM:<OLA@EXAMPLE.ORG>"
    assert [r.lower() for r in got["rcpt"]] == ["rcpt to:<marta@example.com>"]
    assert out["refused"] == ["nobody@example.com"] and out["recipients"] == 1
    assert b"Subject: =?utf-8?b?" in got["data"]
    # No IMAP here, so no copy in Sent, and the person is told.
    assert out["saved_to"] is None and "Sent" in out["note"]


def test_an_unreachable_server_is_a_plain_error():
    port = socket.socket()
    port.bind(("127.0.0.1", 0))
    free = port.getsockname()[1]
    port.close()
    box = Mailbox(ImapSmtpStore(Accounts(imap_port=free, smtp_port=free)))
    with pytest.raises(MailError, match="cannot reach 127.0.0.1"):
        box.send(account="ola@example.org", to=["a@example.com"], subject="", text="")
    with pytest.raises(MailError, match="cannot reach 127.0.0.1"):
        box.list_messages()


# --- A real IMAP server -----------------------------------------------------------------

DOVECOT = shutil.which("dovecot") or (
    "/usr/sbin/dovecot" if Path("/usr/sbin/dovecot").exists() else None
)


@pytest.fixture
def dovecot(tmp_path):
    if DOVECOT is None:
        pytest.skip("Dovecot is not installed")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    uid, gid = os.getuid(), os.getgid()
    import grp
    import pwd

    user, group = pwd.getpwuid(uid).pw_name, grp.getgrgid(gid).gr_name
    if uid == 0:
        # Dovecot runs neither logins nor mail as root (a container, CI): all
        # as nobody, in a folder nobody can reach (pytest's own is 0700).
        nobody = pwd.getpwnam("nobody")
        uid, gid = nobody.pw_uid, nobody.pw_gid
        user, group = "nobody", grp.getgrgid(gid).gr_name
        tmp_path = Path(tempfile.mkdtemp(prefix="vilna-mail-"))
        tmp_path.chmod(0o755)
    (tmp_path / "users").write_text(f"ola@example.org:{{PLAIN}}{PASSWORD}\n", encoding="utf-8")
    conf = tmp_path / "dovecot.conf"
    conf.write_text(
        f"""protocols = imap
listen = 127.0.0.1
base_dir = {tmp_path}/run
state_dir = {tmp_path}/state
log_path = {tmp_path}/dovecot.log
ssl = no
disable_plaintext_auth = no
auth_mechanisms = plain login
default_internal_user = {user}
default_login_user = {user}
default_internal_group = {group}
first_valid_uid = 0
service imap-login {{
  inet_listener imap {{
    port = {port}
  }}
}}
passdb {{
  driver = passwd-file
  args = scheme=PLAIN {tmp_path}/users
}}
userdb {{
  driver = static
  args = uid={uid} gid={gid} home={tmp_path}/home/%u
}}
mail_location = maildir:~/Maildir
namespace inbox {{
  inbox = yes
  separator = /
  mailbox Sent {{
    special_use = \\Sent
    auto = subscribe
  }}
  mailbox Drafts {{
    special_use = \\Drafts
    auto = subscribe
  }}
  mailbox Надіслане {{
    auto = subscribe
  }}
}}
""",
        encoding="utf-8",
    )
    for d in ("run", "state", "home"):
        (tmp_path / d).mkdir()
    if os.getuid() == 0:
        os.chown(tmp_path / "home", uid, gid)
    # The daemon keeps its standard streams: a pipe here would never close.
    errors = tmp_path / "start.err"
    with errors.open("wb") as err:
        started = subprocess.run(
            [DOVECOT, "-c", str(conf)], stdin=subprocess.DEVNULL, stdout=err, stderr=err
        )
    if started.returncode != 0:
        pytest.skip(f"Dovecot did not start: {errors.read_text()[:200]}")
    for _ in range(50):
        try:
            _deliver(port, None)  # signs in, and nothing more
            break
        except Exception:
            time.sleep(0.1)
    else:
        log = tmp_path / "dovecot.log"
        tail = log.read_text(errors="replace")[-300:] if log.exists() else ""
        _stop(conf)
        pytest.skip(f"Dovecot does not answer here: {tail}")
    yield port
    _stop(conf)


def _stop(conf: Path) -> None:
    subprocess.run(
        [DOVECOT, "-c", str(conf), "stop"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _deliver(port: int, raw: bytes | None, folder: str = "INBOX", flags: str = "()") -> None:
    import imaplib

    conn = imaplib.IMAP4("127.0.0.1", port, timeout=5)
    try:
        conn.authenticate("PLAIN", lambda _: f"\0ola@example.org\0{PASSWORD}".encode())
        if raw is not None:
            conn.append(folder, flags, None, raw)
    finally:
        conn.logout()


def test_everything_against_dovecot(dovecot, smtp):
    _deliver(
        dovecot,
        "From: Банк <bank@example.com>\r\nTo: ola@example.org\r\nSubject: Рахунок за березень\r\n"
        "Date: Fri, 05 Mar 2027 10:00:00 +0100\r\nMessage-ID: <inv1@example.com>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\nДо сплати 120 євро.\r\n".encode(),
    )
    _deliver(
        dovecot,
        b"From: marta@example.com\r\nSubject: Lunch\r\nDate: Sat, 06 Mar 2027 10:00:00 +0100\r\n"
        b"\r\nFriday?\r\n",
        flags="(\\Seen)",
    )
    box = Mailbox(ImapSmtpStore(Accounts(imap_port=dovecot, smtp_port=smtp.server_address[1])))
    folders = {f["folder"]: f for f in box.list_folders()["folders"]}
    assert folders["INBOX"] == {"folder": "INBOX", "role": "inbox", "messages": 2, "unread": 1}
    assert folders["Sent"]["role"] == "sent" and folders["Drafts"]["role"] == "drafts"
    assert "Надіслане" in folders

    listed = box.list_messages()
    assert [m["subject"] for m in listed["messages"]] == ["Lunch", "Рахунок за березень"]
    assert [m["unread"] for m in listed["messages"]] == [False, True]

    found = box.search_messages(query="сплати")["messages"]
    assert [m["subject"] for m in found] == ["Рахунок за березень"]
    assert [m["subject"] for m in box.search_messages(sender="marta")["messages"]] == ["Lunch"]
    in_march = box.search_messages(since="2027-03-06", until="2027-03-06")["messages"]
    assert [m["subject"] for m in in_march] == ["Lunch"]

    uid = found[0]["uid"]
    msg = box.read_message(uid)
    assert msg["text"].strip() == "До сплати 120 євро." and msg["from"] == "Банк <bank@example.com>"
    # Still unread on the server after reading.
    assert box.list_messages(unread_only=True)["total"] == 1

    draft = box.save_draft(
        account="ola@example.org",
        to=["bank@example.com"],
        subject="Re: Рахунок",
        text="Дякую",
        in_reply_to=msg["message_id"],
    )
    assert draft["folder"] == "Drafts"
    assert box.list_messages(folder="Drafts")["total"] == 1
    assert box.restore(**draft["undo_arguments"])["removed"] is True
    assert box.list_messages(folder="Drafts")["total"] == 0

    sent = box.send(
        account="ola@example.org",
        to=["bank@example.com"],
        subject="Re: Рахунок",
        text="Дякую, оплачу.",
        in_reply_to=msg["message_id"],
    )
    assert sent["saved_to"] == "Sent" and [r.lower() for r in smtp.got["rcpt"]] == [
        "rcpt to:<bank@example.com>"
    ]
    copy = box.list_messages(folder="Sent")["messages"]
    assert [m["subject"] for m in copy] == ["Re: Рахунок"] and copy[0]["unread"] is False

    with pytest.raises(MailError, match="no folder"):
        box.list_messages(folder="Nope")
