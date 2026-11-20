# SPDX-License-Identifier: Apache-2.0
"""vilna-mcp-atspi against real GTK 4 windows over a real accessibility bus,
behind xdg-dbus-proxy with the manifest's single session-bus call (ADR 0031).

Needs Xvfb, dbus-daemon, xdg-dbus-proxy, at-spi2-core and PyGObject with
GTK 4 and Atspi; skipped without them.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_proxy import proxy_args, wait_for

HERE = Path(__file__).parent


def _have_gtk_and_atspi() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Atspi", "2.0")
    except (ImportError, ValueError):
        return False
    return True


# CI's live job sets VILNA_MCP_DESKTOP_LIVE=1: there a missing piece fails, not skips.
pytestmark = pytest.mark.skipif(
    not os.environ.get("VILNA_MCP_DESKTOP_LIVE")
    and (
        not all(shutil.which(p) for p in ("Xvfb", "dbus-daemon", "xdg-dbus-proxy"))
        or not _have_gtk_and_atspi()
    ),
    reason="needs Xvfb, dbus-daemon, xdg-dbus-proxy, GTK 4 and Atspi",
)


def wait_line(path: Path, word: str) -> None:
    deadline = time.monotonic() + 20
    while word not in path.read_text():
        assert time.monotonic() < deadline, f"no {word!r} in {path}"
        time.sleep(0.1)


@pytest.fixture
def session(tmp_path):
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    display = f":{90 + os.getpid() % 100}"
    procs = []
    bus = tmp_path / "bus"
    env = dict(
        os.environ,
        DISPLAY=display,
        DBUS_SESSION_BUS_ADDRESS=f"unix:path={bus}",
        XDG_RUNTIME_DIR=str(runtime),
        GTK_A11Y="atspi",
        GDK_BACKEND="x11",
    )
    env.pop("WAYLAND_DISPLAY", None)
    try:
        procs.append(
            subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "800x600x24"], stderr=subprocess.DEVNULL
            )
        )
        procs.append(
            subprocess.Popen(
                ["dbus-daemon", "--session", "--nofork", f"--address=unix:path={bus}"],
                stdout=subprocess.DEVNULL,
            )
        )
        wait_for(bus)
        time.sleep(0.5)
        logs = {}
        # An ordinary app, and the same window run as if it were Herkos's.
        for name, script in (("editor", "gtk_app.py"), ("herkos", "herkos-fake-dialog.py")):
            path = tmp_path / script
            shutil.copy(HERE / "gtk_app.py", path)
            logs[name] = tmp_path / f"{name}.log"
            procs.append(
                subprocess.Popen(
                    [sys.executable, str(path), f"{name} window"],
                    env=env,
                    stdout=logs[name].open("w"),
                    stderr=subprocess.DEVNULL,
                )
            )
            wait_line(logs[name], "ready")
        args = proxy_args("org.vilna.mcp.atspi", env["DBUS_SESSION_BUS_ADDRESS"], "", tmp_path)
        procs.append(subprocess.Popen(["xdg-dbus-proxy", *args]))
        wait_for(tmp_path / "org.vilna.mcp.atspi.session.bus")
        tool_env = {
            "PATH": os.environ.get("PATH", "/usr/bin"),
            "HOME": str(tmp_path),
            "XDG_RUNTIME_DIR": str(runtime),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={tmp_path / 'org.vilna.mcp.atspi.session.bus'}",
        }
        tool = subprocess.Popen(
            [sys.executable, "-c", "from vilna_mcp_desktop.atspi import main; main()"],
            env=tool_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        procs.append(tool)
        yield tool, logs
    finally:
        for p in reversed(procs):
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


def call(tool, operation, **arguments):
    call.n = getattr(call, "n", 0) + 1
    msg = {
        "jsonrpc": "2.0",
        "id": call.n,
        "method": "tools/call",
        "params": {"name": operation, "arguments": arguments},
    }
    tool.stdin.write(json.dumps(msg) + "\n")
    tool.stdin.flush()
    result = json.loads(tool.stdout.readline())["result"]
    return result


def test_reads_fills_and_presses_but_not_protected_or_passwords(session):
    tool, logs = session
    windows = call(tool, "list_windows")["structuredContent"]["windows"]
    titles = [w["title"] for w in windows]
    assert "editor window" in titles
    assert "herkos window" not in titles  # Vilna's own window: never listed
    window = next(w["window"] for w in windows if w["title"] == "editor window")
    read = call(tool, "read_window", window=window)
    assert "secret123" not in json.dumps(read)
    els = {}
    for e in read["structuredContent"]["elements"]:
        els.setdefault(e.get("name"), e)  # the button before its own label
    assert els["Password"]["hidden"] is True

    filled = call(tool, "set_text", element=els["Note"]["id"], name="Note", text="new text")
    assert filled["structuredContent"]["undo_arguments"]["text"] == "old text"
    facts = call(tool, "describe", element=els["Send"]["id"], name="Send")["structuredContent"][
        "facts"
    ]
    assert facts["Window"] == "editor window" and facts["Control"] == "push button «Send»"
    assert not call(tool, "activate", element=els["Send"]["id"], name="Send")["isError"]
    wait_line(logs["editor"], "CLICKED new text")

    refused = call(tool, "set_text", element=els["Password"]["id"], name="Password", text="x")
    assert refused["isError"]
    # The protected window's place in the tree, guessed: still refused.
    pid = int(window.split(":")[0])
    for a in range(4):
        guess = call(tool, "read_window", window=f"{pid + 1}:{a}.0")
        assert guess["isError"] or "herkos window" not in json.dumps(guess)
