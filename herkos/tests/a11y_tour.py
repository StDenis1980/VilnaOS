# SPDX-License-Identifier: GPL-3.0-or-later
"""The «Permissions» window as the screen reader sees it: one JSON line per page.

    python3 tests/a11y_tour.py OUT.jsonl STATE_DIR

Run by ``test_a11y_live.py`` inside a display and an accessibility bus of its
own; ``tools/a11y_audit.py`` (a process of its own) reads the AT-SPI tree.
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
from gi.repository import Adw, GLib  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
AUDIT = HERE.parent.parent / "tools" / "a11y_audit.py"
NAME = "herkos-a11y-tour"


def spin(seconds: float) -> None:
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.005)


def ask(proc: subprocess.Popen, command: str) -> dict:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(command + "\n")
    proc.stdin.flush()
    context = GLib.MainContext.default()
    end = time.monotonic() + 60
    while time.monotonic() < end:
        ready, _w, _x = select.select([proc.stdout], [], [], 0.005)
        if ready:
            return json.loads(proc.stdout.readline())
        context.iteration(False)  # the window answers the auditor meanwhile
    raise TimeoutError(command)


def main() -> int:
    out = Path(sys.argv[1])
    lines: list[str] = []
    state = Path(sys.argv[2])
    GLib.set_prgname(NAME)
    Adw.init()
    from test_permissions_view import FILES, change

    from herkos import permissions_view as view
    from herkos import permissions_window as pw
    from herkos.grants import GrantStore
    from herkos.mitos import Journal
    from herkos.risk import Level

    GrantStore(state / "permissions.json").add(
        FILES, "write", [state / "Documents"], Level.R2, "permanent"
    )
    journal = Journal(state / "journal.jsonl")
    for r in change("a1", "zvit.odt"):
        journal.append(r["kind"], r["data"], r["action_id"])
    app = Adw.Application()
    app.register(None)
    win = pw.PermissionsWindow(app, view.LiteHerkos(state_dir=state, socket=state / "none.sock"))
    win.present()
    spin(1.5)
    auditor = subprocess.Popen(
        [sys.executable, str(AUDIT), NAME], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    for page in ("journal", "permissions"):
        win.stack.set_visible_child_name(page)
        spin(0.5)
        result = ask(auditor, "audit")
        result["screen"] = page
        lines.append(json.dumps(result, ensure_ascii=False))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    auditor.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
