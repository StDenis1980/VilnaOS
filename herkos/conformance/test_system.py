# SPDX-License-Identifier: GPL-3.0-or-later
"""The system service (``herkosd system``, ADR 0013 step 6), with real users.

herkosd runs as its own user and tells callers apart by the uid of the
socket: each person's Gloxi, the person's session agent, or nobody. These
tests create throwaway users, so they need root or passwordless sudo, and
the ``herkosd`` binary (``HERKOSD``). Everything they run as another user is
copied to a world-readable folder first: the checkout may be private.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

HERKOSD = os.environ.get("HERKOSD")
SRC = Path(__file__).resolve().parents[1] / "src" / "herkos"
# Run as other users: the system Python, not a virtualenv in a private checkout.
PYTHON = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
USERS = ["hkt-herkos", "hkt-gloxi-a", "hkt-gloxi-b", "hkt-anna", "hkt-bohdan", "hkt-other"]
PROTOCOL = "0.1"


def sudo() -> list[str] | None:
    if os.geteuid() == 0:
        return []
    if shutil.which("sudo") and subprocess.run(["sudo", "-n", "true"], check=False).returncode == 0:
        return ["sudo", "-n"]
    return None


SUDO = sudo()
pytestmark = pytest.mark.skipif(
    not HERKOSD or SUDO is None or not shutil.which("setpriv"),
    reason="needs HERKOSD, setpriv and root or passwordless sudo",
)


def root(*argv: str) -> None:
    subprocess.run([*(SUDO or []), *argv], check=True, capture_output=True)


def as_user(name: str, argv: list[str], **kw) -> subprocess.Popen:
    pw = pwd.getpwnam(name)
    cmd = [*(SUDO or []), "setpriv", f"--reuid={pw.pw_uid}", f"--regid={pw.pw_gid}"]
    cmd += ["--clear-groups", "--", *argv]
    env = {"PATH": "/usr/bin:/bin", "HOME": pw.pw_dir, "PYTHONPATH": kw.pop("pythonpath", "")}
    return subprocess.Popen(cmd, env=env, **kw)


@pytest.fixture(scope="module")
def users():
    made = []
    for name in USERS:
        try:
            pwd.getpwnam(name)
        except KeyError:
            home = ["--create-home"] if name in ("hkt-anna", "hkt-bohdan") else ["--no-create-home"]
            root("useradd", "--user-group", *home, "--shell", "/bin/sh", name)
            made.append(name)
    for person in ("hkt-anna", "hkt-bohdan"):
        docs = Path(pwd.getpwnam(person).pw_dir) / "Documents"
        root("install", "-d", "-o", person, "-g", person, "-m", "0700", str(docs))
    yield
    for name in made:
        subprocess.run(
            [*(SUDO or []), "userdel", "--remove", name], check=False, capture_output=True
        )


RPC = """\
import json, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(10)
s.connect(sys.argv[1])
f = s.makefile("rwb")
out = []
for line in sys.stdin:
    f.write(line.encode()); f.flush()
    try:
        reply = f.readline()
    except OSError:
        reply = b""
    out.append(json.loads(reply) if reply else None)
print(json.dumps(out))
"""


class System:
    def __init__(self, base: Path, process: subprocess.Popen) -> None:
        self.base = base
        self.socket = base / "run" / "herkos.sock"
        self.state = base / "state"
        self.process = process

    def rpc(self, user: str, *messages: dict) -> list:
        """Send these messages as `user`, one answer each (None: no answer)."""
        p = as_user(
            user,
            [PYTHON, str(self.base / "rpc.py"), str(self.socket)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = p.communicate("".join(json.dumps(m) + "\n" for m in messages), timeout=30)
        assert p.returncode == 0, err
        return json.loads(out)


def msg(i: int, method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}


HELLO = msg(1, "herkos.hello", {"protocol": [PROTOCOL], "client": "conformance"})


@pytest.fixture
def system(users):
    base = Path(tempfile.mkdtemp(prefix="herkos-system-"))
    try:
        base.chmod(0o755)
        shutil.copy2(HERKOSD, base / "herkosd")
        shutil.copytree(SRC, base / "py" / "herkos", ignore=shutil.ignore_patterns("__pycache__"))
        (base / "rpc.py").write_text(RPC, encoding="utf-8")
        (base / "system.toml").write_text(
            '[[person]]\nuser = "hkt-anna"\ngloxi = "hkt-gloxi-a"\n'
            '[[person]]\nuser = "hkt-bohdan"\ngloxi = "hkt-gloxi-b"\n',
            encoding="utf-8",
        )
        (base / "config.toml").write_text('keros_model = "off"\n', encoding="utf-8")
        subprocess.run(["chmod", "-R", "a+rX", str(base)], check=True)
        for d in ("run", "state"):
            (base / d).mkdir()
            root("chown", "hkt-herkos:hkt-herkos", str(base / d))
        root("chmod", "0700", str(base / "state"))
        args = [str(base / "herkosd"), "system", "--socket", str(base / "run" / "herkos.sock")]
        args += [
            "--system-config",
            str(base / "system.toml"),
            "--config",
            str(base / "config.toml"),
        ]
        args += ["--state", str(base / "state")]
        log = (base / "herkosd.log").open("wb")
        proc = as_user("hkt-herkos", args, stdout=log, stderr=subprocess.STDOUT)
        s = System(base, proc)
        deadline = time.monotonic() + 10
        while not s.socket.exists():
            assert proc.poll() is None, (base / "herkosd.log").read_text()
            assert time.monotonic() < deadline, "herkosd did not start"
            time.sleep(0.05)
        yield s
    finally:
        if "proc" in locals():
            subprocess.run([*(SUDO or []), "kill", str(proc.pid)], check=False)
            proc.wait(timeout=10)
        subprocess.run([*(SUDO or []), "rm", "-rf", str(base)], check=False)


def test_strangers_get_no_answer(system):
    # Not listed, herkosd's own user, and a person's Gloxi pretending to be root.
    for user in ("hkt-other", "hkt-herkos"):
        assert system.rpc(user, HELLO) == [None], user


def test_gloxi_speaks_for_its_person(system):
    hello, act = system.rpc(
        "hkt-gloxi-a",
        HELLO,
        msg(
            2,
            "herkos.request_action",
            {
                "tool": "org.vilna.mcp.files",
                "operation": "read",
                "reason": "conformance",
                "resources": ["file:///home/hkt-anna/Documents/a.txt"],
                "arguments": {"path": "/home/hkt-anna/Documents/a.txt"},
            },
        ),
    )
    assert hello["result"]["protocol"] == PROTOCOL
    assert hello["result"]["max_level"] == "R2"
    assert act["result"]["decision"] == "denied"
    # Each person has a journal of their own, in herkosd's state folder.
    anna = pwd.getpwnam("hkt-anna").pw_uid
    journal = system.state / str(anna) / "journal.jsonl"
    text = subprocess.run(
        [*(SUDO or []), "cat", str(journal)], check=True, capture_output=True, text=True
    ).stdout
    records = [json.loads(line) for line in text.splitlines()]
    assert records[0]["kind"] == "start" and records[0]["data"]["person"] == "hkt-anna"
    bohdan = pwd.getpwnam("hkt-bohdan").pw_uid
    assert (
        subprocess.run(
            [*(SUDO or []), "test", "-e", str(system.state / str(bohdan))], check=False
        ).returncode
        != 0
    )


def test_the_person_is_an_agent_not_a_gloxi(system):
    hello, act, reg = system.rpc(
        "hkt-anna",
        HELLO,
        msg(2, "herkos.request_action", {"tool": "x", "operation": "y", "reason": "z"}),
        msg(3, "herkos.agent.register"),
    )
    assert hello["result"]["role"] == "agent"
    assert act["error"]["code"] == -32601
    assert reg["result"] == {"registered": True}


def test_herkos_agent_registers(system):
    p = as_user(
        "hkt-anna",
        [PYTHON, "-m", "herkos.agent", "--socket", str(system.socket)],
        pythonpath=str(system.base / "py"),
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = p.stderr.readline()
        assert "registered" in line, line
    finally:
        subprocess.run([*(SUDO or []), "kill", str(p.pid)], check=False)
        p.wait(timeout=10)
    # The state folder is herkosd's alone.
    st = os.stat(system.state) if SUDO == [] else None
    if st is not None:
        assert st.st_mode & 0o077 == 0


def test_socket_from_systemd(users, tmp_path):
    """`StandardInput=socket`: the listening socket arrives as stdin."""
    path = tmp_path / "s.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen()
    empty = tmp_path / "system.toml"
    empty.write_text("", encoding="utf-8")
    proc = subprocess.Popen(
        [HERKOSD, "system", "--system-config", str(empty), "--state", str(tmp_path / "st")],
        stdin=listener.fileno(),
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = proc.stderr.readline()
        assert "system service for 0 people" in proc.stderr.readline() + line
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(str(path))
        c.sendall(json.dumps(HELLO).encode() + b"\n")
        try:
            answer = c.recv(100)
        except ConnectionResetError:
            answer = b""
        assert answer == b""  # nobody is listed: no answer
    finally:
        proc.kill()
        proc.wait()
    # Without a socket on stdin herkosd refuses to start.
    done = subprocess.run(
        [HERKOSD, "system", "--system-config", str(empty)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2 and "not a listening socket" in done.stderr
