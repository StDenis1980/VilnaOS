# SPDX-License-Identifier: GPL-3.0-or-later
"""The installer as Orca reads it: every screen over a real accessibility bus.

Alpha item 34. ``a11y_tour.py`` opens each screen (first screen, every step
of the classic wizard, the partition editor, the chat, the Herkos screen) on
a display and a session bus of their own; ``tools/a11y_audit.py`` reads the
AT-SPI tree the way Orca does. Each assertion is a gap that was found this way.

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


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 20
    while not path.exists():
        assert time.monotonic() < deadline, f"no {path}"
        time.sleep(0.05)


@pytest.fixture(scope="module")
def screens(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("a11y")
    runtime = tmp / "run"
    runtime.mkdir(mode=0o700)
    bus = tmp / "bus"
    display = f":{150 + os.getpid() % 100}"
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
                ["Xvfb", display, "-screen", "0", "1400x950x24"], stderr=subprocess.DEVNULL
            )
        )
        procs.append(
            subprocess.Popen(
                ["dbus-daemon", "--session", "--nofork", f"--address=unix:path={bus}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        _wait_for(bus)
        time.sleep(0.5)
        subprocess.run([sys.executable, "-c", ACTIVATE_REGISTRY], env=env, check=True, timeout=30)
        out = tmp / "screens.jsonl"
        subprocess.run(
            [sys.executable, str(HERE / "a11y_tour.py"), str(out), str(tmp)],
            env=env,
            check=True,
            timeout=600,
            stderr=subprocess.DEVNULL,
        )
        lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
        yield {s["screen"]: s for s in lines}
    finally:
        for p in reversed(procs):
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


def nodes(screen: dict, role: str) -> list[dict]:
    return [n for n in screen["nodes"] if n["role"] == role]


def named(screen: dict, role: str, name: str) -> dict:
    hits = [n for n in nodes(screen, role) if n["name"] == name]
    assert hits, f"no {role} {name!r} in {screen['screen']}"
    return hits[0]


def test_every_screen_was_read(screens):
    from vilna_installer import wizard as wiz

    expected = {"welcome", "editor", "chat", "confirm", *(f"wizard:{k}" for k in wiz.STEPS)}
    assert expected <= set(screens)


def test_every_control_has_a_name(screens):
    problems = {name: s["problems"] for name, s in screens.items() if s["problems"]}
    assert problems == {}


def test_the_screen_reader_button_says_what_it_is(screens):
    """It had an icon and a label inside, and no name: Orca said «toggle button»."""
    named(screens["welcome"], "toggle button", "Screen reader")


def test_a_choice_of_one_reads_as_radio_buttons(screens):
    for step in ("network", "disk", "encryption", "privacy", "vpn"):
        screen = screens[f"wizard:{step}"]
        assert nodes(screen, "radio button"), step
        assert not nodes(screen, "check box"), step  # read «check box» before


def test_the_subtitle_of_a_choice_is_read_with_it(screens):
    """«Erase the whole disk» without what it erases told the person nothing."""
    disk = screens["wizard:disk"]
    erase = named(disk, "radio button", "Erase the whole disk and install Vilna")
    assert "will be erased" in erase["description"]
    assert "Windows" in erase["description"]
    tpm = named(screens["wizard:encryption"], "radio button", "Encrypted, with a recovery key")
    assert "recovery key" in tpm["description"]
    assert all(n["description"] for n in nodes(screens["wizard:privacy"], "radio button"))


def test_a_protected_wifi_network_says_so(screens):
    """The lock was only an icon."""
    home = named(screens["wizard:network"], "radio button", "DemoHome")
    assert "Password-protected" in home["description"]


def test_wifi_networks_are_in_the_tab_order(screens):
    """In one group with «Without a network», chosen in advance, they were unreachable."""
    order = [t and t["name"] for t in screens["wizard:network"]["tabs"]]
    assert "DemoHome" in order and "Without a network" in order


def test_every_way_of_encryption_is_in_the_tab_order(screens):
    """With one chosen in advance, Tab skipped the other three (VM, Build12.19)."""
    order = [t and t["name"] for t in screens["wizard:encryption"]["tabs"]]
    for way in (
        "Encrypted, with a recovery key",
        "Encrypted, with a PIN at every start",
        "Encrypted with a passphrase",
        "Not encrypted",
    ):
        assert way in order, way


def test_the_summary_reads_the_value_of_each_line(screens):
    lines = [n for n in nodes(screens["wizard:summary"], "list item") if n["description"]]
    assert any(n["name"] == "Languages and keyboard" for n in lines)


def test_the_current_step_is_said(screens):
    named(screens["wizard:disk"], "list item", "Step 3 of 9: Disk")


def test_the_account_password_field_has_a_whole_name(screens):
    """«Password for {name}» with no name read «Password for»."""
    texts = [n["name"] for n in nodes(screens["wizard:user"], "text")]
    assert "Password" in texts
    assert "Password for" not in texts


def test_one_tab_stop_per_language(screens):
    order = [t and t["name"] for t in screens["wizard:languages"]["tabs"]]
    assert order.count("Español") == 1


def test_new_partitions_say_which_one(screens):
    """The size read as its number («0.5»), and «Use» and «Remove» repeated for each."""
    editor = screens["editor"]
    named(editor, "spin button", "New partition 1: Size, GiB")
    named(editor, "combo box", "New partition 1: Use")
    named(editor, "push button", "Remove: New partition 1")


def test_the_chat_and_the_plan_have_names(screens):
    groups = {n["name"] for n in nodes(screens["chat"], "panel")}
    assert {"Conversation with Gloxi", "Plan"} <= groups


def test_the_herkos_screen_is_read_as_a_dialog(screens):
    """A dialog: Orca reads what will happen, not only the field with the focus."""
    confirm = screens["confirm"]
    assert confirm["nodes"][0]["role"] == "dialog"
    plan_lines = [n for n in nodes(confirm, "list item") if n["description"]]
    assert plan_lines, "the plan's values are not read"
