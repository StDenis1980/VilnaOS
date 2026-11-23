# SPDX-License-Identifier: GPL-3.0-or-later
"""What a screen reader sees of one program: its AT-SPI tree, read like Orca reads it.

    python3 tools/a11y_audit.py NAME   (then one command per line on stdin)

``NAME`` is the application name on the accessibility bus (``*`` for the last
one). Each line ``audit`` prints one line of JSON: the shown objects (role,
name, description, focusable) and the problems found — a control with no
name. ``focus`` prints only the object that has the focus. The installer's and
Herkos's live tests run it in a process of its own: a program cannot answer
AT-SPI calls about itself while it waits for them.

Needs PyGObject with Atspi and a running accessibility bus.
"""

from __future__ import annotations

import json
import sys
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

# Roles a person acts on: each needs a name of its own.
CONTROLS = frozenset(
    {
        "push button",
        "toggle button",
        "check box",
        "radio button",
        "combo box",
        "text",
        "password text",
        "entry",
        "spin button",
        "slider",
        "switch",
        "link",
        "menu item",
        "page tab",
    }
)
WINDOWS = frozenset({"frame", "dialog", "alert", "window"})
MAX_DEPTH = 80


def has(obj, state: str) -> bool:
    return obj.get_state_set().contains(getattr(Atspi.StateType, state))


def find_app(name: str):
    found = None
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is not None and (name == "*" or name in (app.get_name() or "")):
            found = app
    return found


def where(obj) -> str:
    parts = []
    while obj is not None and obj.get_role_name() != "application" and len(parts) < 3:
        parts.append(f"{obj.get_role_name()}:{obj.get_name()!r}")
        obj = obj.get_parent()
    return " < ".join(parts)


def inside_combo(obj) -> bool:
    """The list inside a combo box belongs to it: the combo box has the name."""
    for _ in range(4):
        obj = obj.get_parent()
        if obj is None:
            return False
        if obj.get_role_name() == "combo box":
            return True
    return False


def audit(app) -> dict:
    nodes, problems = [], []

    def walk(obj, depth: int) -> None:
        role = obj.get_role_name()
        if depth > MAX_DEPTH or (not has(obj, "SHOWING") and role not in WINDOWS):
            return
        name = (obj.get_name() or "").strip()
        nodes.append(
            {
                "role": role,
                "name": name,
                "description": obj.get_description() or "",
                "focusable": has(obj, "FOCUSABLE"),
                "sensitive": has(obj, "SENSITIVE"),
                "depth": depth,
            }
        )
        if role in CONTROLS and not name and not inside_combo(obj):
            problems.append(f"no name: {where(obj)}")
        for i in range(obj.get_child_count()):
            child = obj.get_child_at_index(i)
            if child is not None:
                walk(child, depth + 1)

    for i in range(app.get_child_count()):
        window = app.get_child_at_index(i)
        if window is not None:
            walk(window, 0)
    return {"nodes": nodes, "problems": problems}


def focused(app):
    def walk(obj, depth: int):
        role = obj.get_role_name()
        if depth > MAX_DEPTH:
            return None
        if has(obj, "FOCUSED") and role not in WINDOWS:
            return obj
        if not has(obj, "SHOWING") and role not in WINDOWS:
            return None
        for i in range(obj.get_child_count()):
            child = obj.get_child_at_index(i)
            hit = walk(child, depth + 1) if child is not None else None
            if hit is not None:
                return hit
        return None

    for i in range(app.get_child_count()):
        window = app.get_child_at_index(i)
        hit = walk(window, 0) if window is not None else None
        if hit is not None:
            return {
                "role": hit.get_role_name(),
                "name": (hit.get_name() or "").strip(),
                "description": hit.get_description() or "",
            }
    return None


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "*"
    app = None
    for line in sys.stdin:
        for _ in range(100):
            app = app or find_app(name)
            if app is not None:
                break
            time.sleep(0.1)
        if app is None:
            print(json.dumps({"error": f"no application {name!r} on the bus"}), flush=True)
            continue
        result = audit(app) if line.strip() == "audit" else {}
        result["focus"] = focused(app)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
