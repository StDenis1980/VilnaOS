# SPDX-License-Identifier: GPL-3.0-or-later
"""Every installer screen, as the screen reader sees it: one JSON line per screen.

    python3 tests/a11y_tour.py OUT.jsonl

Run by ``test_a11y_live.py`` inside a display and an accessibility bus of its
own. It opens the screens one after another with synthetic computers, and for
each asks ``tools/a11y_audit.py`` (a process of its own) what is on the bus,
and walks the Tab order with the window's own «move-focus», as Tab does.
"""

from __future__ import annotations

import json
import select
import subprocess
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
AUDIT = HERE.parent.parent / "tools" / "a11y_audit.py"
NAME = "vilna-a11y-tour"
TABS = 40


def spin(seconds: float) -> None:
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.005)


class Auditor:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(AUDIT), NAME],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

    def ask(self, command: str) -> dict:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()
        context = GLib.MainContext.default()
        end = time.monotonic() + 60
        while time.monotonic() < end:
            ready, _w, _x = select.select([self.proc.stdout], [], [], 0.005)
            if ready:
                return json.loads(self.proc.stdout.readline())
            context.iteration(False)  # the window answers the auditor meanwhile
        raise TimeoutError(command)


def tabs(window: Gtk.Window, auditor: Auditor) -> list[dict | None]:
    """The Tab order from where the focus is, once round."""
    seen: list[dict | None] = []
    for _ in range(TABS):
        window.emit("move-focus", Gtk.DirectionType.TAB_FORWARD)
        spin(0.05)
        focus = auditor.ask("focus")["focus"]
        if seen and focus == seen[0]:
            break
        seen.append(focus)
    return seen


def main() -> int:
    out = Path(sys.argv[1])
    lines: list[str] = []
    GLib.set_prgname(NAME)
    Adw.init()
    from test_confirm import full_plan
    from vilna_mcp_installer import machine as machines

    from vilna_installer import chat, confirm_window, encryption, start, welcome, wizard_window
    from vilna_installer import wizard as wiz

    tmp = Path(sys.argv[2])
    app = Adw.Application()
    app.register(None)
    auditor = Auditor()

    def record(screen: str, window: Gtk.Window, *, tab: bool = False, **extra) -> None:
        result = auditor.ask("audit")
        result["screen"] = screen
        # Walking Tab asks the bus once a stop: only where a test reads the order.
        result["tabs"] = tabs(window, auditor) if tab else []
        result.update(extra)
        lines.append(json.dumps(result, ensure_ascii=False))

    first = welcome.Welcome(app, tmp / "welcome.json", "en")
    first.present()
    spin(1.0)
    record("welcome", first)
    first.close()

    plan = tmp / "wizard.json"
    start.write_plan(start.start_plan("en", False, False), plan)
    win = wizard_window.WizardWindow(app, plan, machines.DemoMachine("windows"), demo="windows")
    win.present()
    spin(1.0)
    for key in wiz.STEPS:
        win.next.grab_focus()  # where the focus is after «Next»
        win.go(key, strict=False)
        spin(0.8)
        record(f"wizard:{key}", win, tab=key in ("languages", "network"))
    w = win.wizard
    w.set_disk(w.disks()[0]["id"], "manual")
    editor = wizard_window.PartitionEditor(w, win._, lambda: None)
    editor.present(win)
    spin(0.3)
    editor._on_add(None)
    spin(0.5)
    record("editor", win)
    editor.close()
    win.close()

    talk = tmp / "chat.json"
    start.write_plan(start.start_plan("en", False, True), talk)
    chat_window = chat.ChatWindow(app, None, talk, demo=True)
    chat_window.present()
    spin(1.0)
    record("chat", chat_window)
    chat_window.close()

    confirmed = tmp / "confirm.json"
    full_plan(confirmed)
    herkos = confirm_window.ConfirmWindow(
        app,
        confirmed,
        machines.DemoMachine("windows"),
        key=encryption.recovery_key(),
        stage="alpha",
        tpm=True,
        finish=lambda _outcome: None,
    )
    herkos.present()
    spin(1.0)
    record("confirm", herkos)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
