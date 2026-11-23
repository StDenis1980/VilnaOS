# SPDX-License-Identifier: GPL-3.0-or-later
"""Herkos's windows as Orca reads them, over a real accessibility bus (alpha item 34).

The permission dialog and the «Permissions» window run on a display and a
session bus of their own; ``tools/a11y_audit.py`` reads their AT-SPI tree.
Each assertion is a gap that was found this way.

Needs Xvfb, dbus-daemon, at-spi2-core and PyGObject with GTK 4, libadwaita
and Atspi; skipped without them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AUDIT = HERE.parent.parent / "tools" / "a11y_audit.py"


def _have_gtk_and_atspi() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        gi.require_version("Atspi", "2.0")
    except (ImportError, ValueError):
        return False
    return True


pytestmark = pytest.mark.skipif(
    not all(shutil.which(p) for p in ("Xvfb", "dbus-daemon")) or not _have_gtk_and_atspi(),
    reason="needs Xvfb, dbus-daemon, GTK 4, libadwaita and Atspi",
)

ACTIVATE_REGISTRY = """
import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi
Atspi.get_desktop(0).get_child_count()
"""

QUESTION = {
    "summary": "Delete the folder Old photos",
    "level": "R4",
    "reason": "You asked to free space",
    "resources": ["file:///home/olena/Old photos"],
    "undoable": False,
    "choices": ["once"],
    "content": {"path": "/home/olena/Old photos"},
}


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("a11y")
    runtime = tmp / "run"
    runtime.mkdir(mode=0o700)
    bus = tmp / "bus"
    display = f":{50 + os.getpid() % 100}"
    env = dict(
        os.environ,
        DISPLAY=display,
        DBUS_SESSION_BUS_ADDRESS=f"unix:path={bus}",
        XDG_RUNTIME_DIR=str(runtime),
        GTK_A11Y="atspi",
        GDK_BACKEND="x11",
        LANGUAGE="en",
    )
    env.pop("WAYLAND_DISPLAY", None)
    procs = []
    try:
        procs.append(
            subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1280x900x24"], stderr=subprocess.DEVNULL
            )
        )
        procs.append(
            subprocess.Popen(
                ["dbus-daemon", "--session", "--nofork", f"--address=unix:path={bus}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        deadline = time.monotonic() + 20
        while not bus.exists():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        time.sleep(0.5)
        subprocess.run([sys.executable, "-c", ACTIVATE_REGISTRY], env=env, check=True, timeout=30)
        yield env, tmp, procs
    finally:
        for p in reversed(procs):
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


def audit(env, name="*") -> dict:
    done = subprocess.run(
        [sys.executable, str(AUDIT), name],
        input="audit\n",
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(done.stdout)


def test_the_permission_dialog_is_read_as_a_dialog(session):
    """It was a plain window: Orca said its title and «Deny», not the question."""
    env, _tmp, procs = session
    dialog = subprocess.Popen(
        [sys.executable, "-m", "herkos.dialog"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        text=True,
    )
    procs.append(dialog)
    assert dialog.stdin is not None
    dialog.stdin.write(json.dumps(QUESTION))
    dialog.stdin.close()
    time.sleep(3)
    seen = audit(env)
    dialog.terminate()
    assert seen["nodes"][0]["role"] == "dialog"
    labels = [n["name"] for n in seen["nodes"] if n["role"] == "label"]
    assert "Delete the folder Old photos" in labels
    assert seen["focus"]["name"] == "Deny"  # Enter is the safe answer at R4
    assert seen["problems"] == []


@pytest.fixture(scope="module")
def pages(session):
    env, tmp, _procs = session
    state = tmp / "state"
    state.mkdir()
    out = tmp / "pages.jsonl"
    subprocess.run(
        [sys.executable, str(HERE / "a11y_tour.py"), str(out), str(state)],
        env=env,
        check=True,
        timeout=300,
        stderr=subprocess.DEVNULL,
    )
    return {p["screen"]: p for p in map(json.loads, out.read_text(encoding="utf-8").splitlines())}


def names(page: dict, role: str) -> list[str]:
    return [n["name"] for n in page["nodes"] if n["role"] == role]


def test_undo_and_revoke_say_what_they_act_on(pages):
    """Every row had the same «Undo» or «Revoke»."""
    assert "Undo: Gloxi changed a file: zvit.odt" in names(pages["journal"], "push button")
    assert "Revoke: Change a file" in names(pages["permissions"], "push button")


def test_the_risk_level_is_said_in_words(pages):
    assert "Risk level R2" in names(pages["permissions"], "label")


def test_a_permission_reads_how_long_and_where(pages):
    rows = [n for n in pages["permissions"]["nodes"] if n["name"] == "Change a file"]
    assert any("Documents" in n["description"] for n in rows)


def test_every_control_has_a_name(pages):
    assert {k: p["problems"] for k, p in pages.items() if p["problems"]} == {}
