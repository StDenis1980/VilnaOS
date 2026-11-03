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
import threading
import time
from pathlib import Path

import pytest

HERKOSD = os.environ.get("HERKOSD")
SRC = Path(__file__).resolve().parents[1] / "src" / "herkos"
MCP_FILES = Path(__file__).resolve().parents[2] / "mcp" / "files" / "src" / "vilna_mcp_files"
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


def exists(path: str) -> bool:
    """Whether the path exists, seen as root: the test may run as a user without access."""
    return subprocess.run([*(SUDO or []), "test", "-e", path], check=False).returncode == 0


def as_user(name: str, argv: list[str], **kw) -> subprocess.Popen:
    pw = pwd.getpwnam(name)
    cmd = [*(SUDO or []), "setpriv", f"--reuid={pw.pw_uid}", f"--regid={pw.pw_gid}"]
    # The environment is set after sudo, which would drop PYTHONPATH.
    env = ["env", "-i", "PATH=/usr/bin:/bin", f"HOME={pw.pw_dir}"]
    env.append(f"PYTHONPATH={kw.pop('pythonpath', '')}")
    cmd += ["--clear-groups", "--", *env, *argv]
    return subprocess.Popen(cmd, **kw)


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


# Stands in for systemctl: herkosd asks it to start a tool unit.
FAKE_SYSTEMCTL = """\
#!/bin/sh
case "$3" in
    vilna-mcp-files@*.service) echo "$3" > "{spool}/$3" ;;
    *) exit 1 ;;
esac
"""

# What systemd does for vilna-mcp-files@.service: connect stdin and stdout
# to herkosd's socket as root, then become the person and run the tool.
LAUNCH = """\
import os, pwd, socket, sys
uid, path, cmd = int(sys.argv[1]), sys.argv[2], sys.argv[3:]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(path)
os.dup2(s.fileno(), 0)
os.dup2(s.fileno(), 1)
pw = pwd.getpwuid(uid)
os.setgroups([])
os.setgid(pw.pw_gid)
os.setuid(uid)
os.environ["HOME"] = pw.pw_dir
os.execv(cmd[0], cmd)
"""

# The confirmation window of the agent: logs the question, gives the answer
# written in the file "answer" next to it.
WINDOW = """\
#!{python}
import json, pathlib, sys
here = pathlib.Path(__file__).parent / "agent"
with (here / "questions.jsonl").open("a") as f:
    f.write(json.dumps(json.load(sys.stdin)) + "\\n")
answer = here / "answer"
print(answer.read_text().strip() if answer.exists() else "deny")
"""


def fake_systemd(base: Path, stop: threading.Event) -> None:
    spool = base / "spool"
    while not stop.is_set():
        for req in sorted(spool.iterdir()) if spool.exists() else []:
            unit = req.name
            uid = unit.split("@")[1].split(".")[0]
            subprocess.run([*(SUDO or []), "rm", "-f", str(req)], check=False)
            sock = base / "tools" / uid / "files.sock"
            args = base / "tools" / uid / "files.args"
            cmd = [*(SUDO or []), "env", f"PYTHONPATH={base / 'py'}", PYTHON]
            cmd += [str(base / "launch.py"), uid, str(sock), PYTHON, "-m", "vilna_mcp_files"]
            subprocess.Popen([*cmd, f"@{args}"], stderr=subprocess.DEVNULL)
        stop.wait(0.05)


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
        shutil.copytree(
            MCP_FILES, base / "py" / "vilna_mcp_files", ignore=shutil.ignore_patterns("__pycache__")
        )
        (base / "rpc.py").write_text(RPC, encoding="utf-8")
        (base / "launch.py").write_text(LAUNCH, encoding="utf-8")
        (base / "fake-systemctl").write_text(
            FAKE_SYSTEMCTL.format(spool=base / "spool"), encoding="utf-8"
        )
        (base / "window").write_text(WINDOW.format(python=PYTHON), encoding="utf-8")
        (base / "fake-systemctl").chmod(0o755)
        (base / "window").chmod(0o755)
        (base / "system.toml").write_text(
            '[[person]]\nuser = "hkt-anna"\ngloxi = "hkt-gloxi-a"\n'
            '[[person]]\nuser = "hkt-bohdan"\ngloxi = "hkt-gloxi-b"\n',
            encoding="utf-8",
        )
        (base / "config.toml").write_text('keros_model = "off"\n', encoding="utf-8")
        subprocess.run(["chmod", "-R", "a+rX", str(base)], check=True)
        for d in ("run", "state", "spool", "tools"):
            (base / d).mkdir()
            root("chown", "hkt-herkos:hkt-herkos", str(base / d))
        (base / "agent").mkdir()
        root("chown", "hkt-anna:hkt-anna", str(base / "agent"))
        root("chmod", "0700", str(base / "state"))
        args = [str(base / "herkosd"), "system", "--socket", str(base / "run" / "herkos.sock")]
        args += [
            "--system-config",
            str(base / "system.toml"),
            "--config",
            str(base / "config.toml"),
        ]
        args += ["--state", str(base / "state"), "--tools-dir", str(base / "tools")]
        args += ["--start-command", str(base / "fake-systemctl")]
        stop = threading.Event()
        threading.Thread(target=fake_systemd, args=(base, stop), daemon=True).start()
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
        if "stop" in locals():
            stop.set()
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
    assert hello["result"]["max_level"] == "R4"  # the system herkosd does R3–R4 (step 7)
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
        try:
            c.sendall(json.dumps(HELLO).encode() + b"\n")
            answer = c.recv(100)
        except (BrokenPipeError, ConnectionResetError):
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


def read_request(path: str) -> dict:
    return {
        "tool": "org.vilna.mcp.files",
        "operation": "read",
        "reason": "conformance",
        "resources": [Path(path).as_uri()],
        "arguments": {"path": path},
    }


def test_tools_act_as_the_person(system):
    home = pwd.getpwnam("hkt-anna").pw_dir
    note = f"{home}/Documents/note.txt"
    root(
        "sh",
        "-c",
        f"echo 'Buy milk.' > {note} && chown hkt-anna:hkt-anna {note} && chmod 600 {note}",
    )
    agent = as_user(
        "hkt-anna",
        [
            PYTHON,
            "-m",
            "herkos.agent",
            "--socket",
            str(system.socket),
            "--window-command",
            str(system.base / "window"),
        ],
        pythonpath=str(system.base / "py"),
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert "registered" in agent.stderr.readline()
        root("sh", "-c", f"echo once > {system.base / 'agent' / 'answer'}")
        _, r = system.rpc("hkt-gloxi-a", HELLO, msg(2, "herkos.request_action", read_request(note)))
        r = r["result"]
        log = (system.base / "herkosd.log").read_text()
        assert (r["decision"], r["code"]) == ("allowed", "allowed"), (r, log)
        assert "Buy milk." in json.dumps(r["result"])
        questions = (system.base / "agent" / "questions.jsonl").read_text().splitlines()
        assert json.loads(questions[-1])["resources"] == [Path(note).as_uri()]

        # Bohdan's Gloxi cannot read Anna's file: nobody of Bohdan's answers,
        # and his tool would run as Bohdan anyway.
        _, r = system.rpc("hkt-gloxi-b", HELLO, msg(2, "herkos.request_action", read_request(note)))
        assert r["result"]["decision"] == "denied" and "result" not in r["result"]
    finally:
        subprocess.run([*(SUDO or []), "kill", str(agent.pid)], check=False)
        agent.wait(timeout=10)


def files_request(operation: str, path: str) -> dict:
    return {**read_request(path), "operation": operation}


def test_system_herkos_asks_for_r3_and_r4(system):
    home = pwd.getpwnam("hkt-anna").pw_dir
    docs = f"{home}/Documents"
    names = ("a.txt", "b.txt", "c.txt")
    script = " && ".join(f"echo {n} > {docs}/{n}" for n in names)
    root("sh", "-c", f"{script} && chown hkt-anna:hkt-anna {docs}/*.txt")
    agent = as_user(
        "hkt-anna",
        [
            PYTHON,
            "-m",
            "herkos.agent",
            "--socket",
            str(system.socket),
            "--window-command",
            str(system.base / "window"),
        ],
        pythonpath=str(system.base / "py"),
        stderr=subprocess.PIPE,
        text=True,
    )
    answer = system.base / "agent" / "answer"
    questions = system.base / "agent" / "questions.jsonl"
    try:
        assert "registered" in agent.stderr.readline()
        # R3: Anna allows for the session, the second delete needs no question.
        root("sh", "-c", f"echo session > {answer}")
        _, a, b = system.rpc(
            "hkt-gloxi-a",
            HELLO,
            msg(2, "herkos.request_action", files_request("delete", f"{docs}/a.txt")),
            msg(3, "herkos.request_action", files_request("delete", f"{docs}/b.txt")),
        )
        log = (system.base / "herkosd.log").read_text()
        assert (a["result"]["code"], a["result"]["level"]) == ("allowed", "R3"), (a, log)
        assert b["result"]["decision"] == "allowed", b
        asked = [json.loads(q) for q in questions.read_text().splitlines()]
        assert len(asked) == 1 and asked[0]["choices"] == ["once", "session"]
        assert asked[0].get("content") is None
        assert not exists(f"{docs}/a.txt") and not exists(f"{docs}/b.txt")

        # After a read, the session holds untrusted content: a delete is R4,
        # "session" is not on offer (so the window's "session" is a no), and
        # the question shows exactly what will be done.
        _, read, denied = system.rpc(
            "hkt-gloxi-a",
            HELLO,
            msg(2, "herkos.request_action", read_request(f"{docs}/c.txt")),
            msg(3, "herkos.request_action", files_request("delete", f"{docs}/c.txt")),
        )
        assert read["result"]["decision"] == "allowed", read
        assert (denied["result"]["code"], denied["result"]["level"]) == ("denied_user", "R4")
        q = json.loads(questions.read_text().splitlines()[-1])
        assert q["level"] == "R4" and q["choices"] == ["once"]
        assert q["content"] == {"path": f"{docs}/c.txt"}
        assert exists(f"{docs}/c.txt")
    finally:
        subprocess.run([*(SUDO or []), "kill", str(agent.pid)], check=False)
        agent.wait(timeout=10)


def test_a_tool_that_stopped_is_started_again(system):
    home = pwd.getpwnam("hkt-anna").pw_dir
    note = f"{home}/Documents/again.txt"
    root("sh", "-c", f"echo again > {note} && chown hkt-anna:hkt-anna {note}")
    agent = as_user(
        "hkt-anna",
        [
            PYTHON,
            "-m",
            "herkos.agent",
            "--socket",
            str(system.socket),
            "--window-command",
            str(system.base / "window"),
        ],
        pythonpath=str(system.base / "py"),
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert "registered" in agent.stderr.readline()
        root("sh", "-c", f"echo session > {system.base / 'agent' / 'answer'}")
        read = msg(2, "herkos.request_action", read_request(note))
        _, r = system.rpc("hkt-gloxi-a", HELLO, read)
        assert r["result"]["decision"] == "allowed", r
        # The tool dies (out of memory, killed): the call that finds out fails,
        # the next one gets a fresh tool, still running as Anna.
        subprocess.run([*(SUDO or []), "pkill", "-u", "hkt-anna", "-f", "vilna_mcp_files"])
        time.sleep(0.5)
        _, broken, again = system.rpc("hkt-gloxi-a", HELLO, read, {**read, "id": 3})
        log = (system.base / "herkosd.log").read_text()
        assert broken["result"]["code"] == "tool_error", broken
        assert again["result"]["code"] == "allowed", (again, log)
        assert "again" in json.dumps(again["result"]["result"])
        assert "started again" in log
    finally:
        subprocess.run([*(SUDO or []), "kill", str(agent.pid)], check=False)
        agent.wait(timeout=10)
