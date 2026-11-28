# SPDX-License-Identifier: Apache-2.0
"""The "other apps" tool (org.vilna.mcp.atspi, ADR 0031, permissions.md 9.4):
reads the windows of apps that have no tool of their own and presses their
buttons or fills their fields, through the accessibility bus (AT-SPI).

- Protected apps (protect.py) are never listed, read or touched.
- A password field's text is never read or changed.
- An element is named by the id ``read_window`` gave and the name Gloxi saw
  there; if the window changed and the name is another, nothing is done.
- No synthetic keys or mouse, no moving the focus.

    vilna-mcp-atspi
"""

from __future__ import annotations

import re
import sys
import unicodedata
from collections.abc import Callable
from typing import Protocol

from vilna_mcp_desktop.bus import text
from vilna_mcp_desktop.mcp import CHANGE, READ, Server, ToolError, tool
from vilna_mcp_desktop.protect import Process, identify, protected

MAX_APPS = 64
MAX_WINDOWS = 32
MAX_DEPTH = 40
MAX_NODES = 1500
MAX_TEXT_PER_NODE = 2000
MAX_TEXT_TOTAL = 64 * 1024
MAX_SET_TEXT = 10_000
MAX_VISITED = 10 * MAX_NODES  # nodes looked at, written or not
MAX_NAME_SHOWN = 300
MAX_WORD = 64  # an action or role name
PASSWORD_ROLES = {"password text"}
# A field named like a secret is a password field too, whatever its role says
# (a "show password" toggle turns it into plain text; codes, PINs, tokens).
_SECRET_NAME = re.compile(
    r"pass(word|phrase|code)|пароль|парол|contrase|hasło|haslo|mot de passe|passwort"
    r"|\bpin\b|\btoken\b|secret|секрет|\botp\b|one[- ]time|2fa|код підтвердження",
    re.IGNORECASE,
)
# States worth telling the model about.
STATES = ("focused", "checked", "selected", "pressed", "expanded", "editable")
# Which action "activate" uses when Gloxi does not name one.
PREFERRED_ACTIONS = ("click", "press", "activate", "toggle", "jump", "open")
# Actions that move the clipboard: pasting could carry a password the person
# copied into a web form, copying could hand Gloxi's text to another app.
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
# Letters of other scripts that look like the Latin ones of copy, cut, paste.
_LOOKALIKE = str.maketrans("орсаеухіОРСАЕТХ", "opcaeyxiOPCAETX")


def moves_clipboard(action: str) -> bool:
    """Whether an action name means copy, cut or paste, however it is written."""
    plain = unicodedata.normalize("NFKC", action)
    plain = "".join(c for c in plain if unicodedata.category(c) != "Cf").translate(_LOOKALIKE)
    words = re.split(r"[^a-z0-9+]+", _CAMEL.sub(" ", plain).lower())
    if "clipboard" in plain.lower() or any(re.fullmatch(r"ctrl\+[cvx]", w) for w in words):
        return True
    return any(w.startswith(("paste", "copy", "cut")) for w in words)


def is_secret(role: str, name: str) -> bool:
    return role in PASSWORD_ROLES or bool(_SECRET_NAME.search(name))


_ID = re.compile(r"([0-9]{1,9}):([0-9]{1,4}(?:\.[0-9]{1,4}){1,41})")


class Node(Protocol):
    """One accessible object; atspi_node() wraps the real ones."""

    def role(self) -> str: ...
    def name(self) -> str: ...
    def pid(self) -> int: ...
    def child_count(self) -> int: ...
    def child(self, i: int) -> Node | None: ...
    def states(self) -> set[str]: ...
    def text(self) -> str | None: ...
    def editable(self) -> bool: ...
    def actions(self) -> list[str]: ...
    def do_action(self, i: int) -> bool: ...
    def set_text(self, value: str) -> bool: ...


_ELEMENT = {"type": "string", "maxLength": 256, "description": "id from read_window"}
_NAME = {"type": "string", "maxLength": 1000, "description": "its name as read_window gave it"}

TOOLS = [
    tool(
        "list_windows", "Open windows of other apps (protected apps are left out)", {}, [], **READ
    ),
    tool(
        "read_window",
        "The buttons, fields and texts of one window, with ids for activate and set_text",
        {"window": {"type": "string", "maxLength": 64, "description": "id from list_windows"}},
        ["window"],
        **READ,
    ),
    tool(
        "describe",
        "Which app, window and control an element is (what Herkos shows the person)",
        {"element": _ELEMENT, "name": _NAME},
        ["element", "name"],
        **READ,
    ),
    tool(
        "activate",
        "Press a button, menu item, link or switch",
        {
            "element": _ELEMENT,
            "name": _NAME,
            "action": {"type": "string", "maxLength": 64, "description": "default: click"},
        },
        ["element", "name"],
        readOnlyHint=False,
        destructiveHint=True,
    ),
    tool(
        "set_text",
        "Replace the text of a field (not a password field)",
        {"element": _ELEMENT, "name": _NAME, "text": {"type": "string", "maxLength": MAX_SET_TEXT}},
        ["element", "name", "text"],
        **CHANGE,
    ),
    tool(
        "restore",
        "Put back the text a field had (undo_arguments of set_text)",
        {"element": _ELEMENT, "name": _NAME, "text": {"type": "string", "maxLength": MAX_SET_TEXT}},
        ["element", "name", "text"],
        **CHANGE,
    ),
]


class Apps:
    def __init__(
        self,
        desktop: Callable[[], Node],
        who: Callable[[int], Process | None] = identify,
        why_protected: Callable[[Process | None], str | None] = protected,
    ) -> None:
        self._desktop = desktop
        self._who = who
        self._why = why_protected

    # --- finding things -------------------------------------------------

    def _open_app(self, node: Node | None) -> tuple[Node, Process] | None:
        """The app and its process, or None if protected or gone."""
        if node is None:
            return None
        try:
            pid = node.pid()
        except Exception:
            return None
        process = self._who(pid)
        if self._why(process) is not None or process is None:
            return None
        return node, process

    def _resolve(self, element: str) -> tuple[Process, Node, Node]:
        """(process, window, element) for an id, after every check."""
        m = _ID.fullmatch(element)
        if not m:
            raise ToolError(
                f"{element!r} is not an id from list_windows or read_window; call "
                "list_windows, then read_window, and use an id they gave"
            )
        pid, path = int(m.group(1)), [int(i) for i in m.group(2).split(".")]
        desktop = self._desktop()
        app = desktop.child(path[0]) if path[0] < desktop.child_count() else None
        opened = self._open_app(app)
        if opened is None or opened[1].pid != pid:
            raise ToolError(
                "that app is closed, changed or not open to Gloxi; list the windows again"
            )
        _, process = opened
        node: Node | None = app
        window: Node | None = None
        for depth, i in enumerate(path[1:]):
            if node is None or i >= node.child_count():
                node = None
                break
            node = node.child(i)
            if depth == 0:
                window = node
        if node is None or window is None:
            raise ToolError("the window changed; read it again")
        return process, window, node

    def _checked(self, element: str, name: str) -> tuple[Process, Node, Node]:
        process, window, node = self._resolve(element)
        now = node.name()
        if name not in (now, text(now, MAX_NAME_SHOWN)):  # read_window shows it cut
            raise ToolError(
                f"the window changed: that element is now {text(node.name(), 80)!r}, "
                f"not {text(name, 80)!r}; read it again"
            )
        if node is not window and "showing" not in node.states():
            # read_window never shows it, and the person does not see it.
            raise ToolError("that element is not shown in the window now; read it again")
        return process, window, node

    # --- operations -----------------------------------------------------

    def list_windows(self) -> dict:
        desktop = self._desktop()
        windows = []
        for a in range(min(desktop.child_count(), MAX_APPS)):
            try:
                opened = self._open_app(desktop.child(a))
                if opened is None:
                    continue
                app, process = opened
                count = min(app.child_count(), MAX_WINDOWS)
            except Exception:  # noqa: BLE001 - a hung or broken app hides only itself
                continue
            for w in range(count):
                try:
                    win = app.child(w)
                    if win is None:
                        continue
                    entry = {
                        "window": f"{process.pid}:{a}.{w}",
                        "app": process.label,
                        "title": text(win.name()),
                        "active": "active" in win.states(),
                    }
                except Exception:  # noqa: BLE001
                    continue
                windows.append(entry)
        return {"windows": windows}

    def read_window(self, window: str) -> dict:
        process, win, node = self._resolve(window)
        if node is not win:
            raise ToolError("that is an element, not a window; use an id from list_windows")
        out: list[dict] = []
        walk = _Walk()
        self._walk(win, window, 0, out, walk)
        return {
            "app": process.label,
            "title": text(win.name()),
            "elements": out,
            "complete": len(out) < MAX_NODES and walk.text > 0 and not walk.cut,
        }

    def _walk(self, node: Node, id_: str, depth: int, out: list[dict], walk: _Walk) -> None:
        if len(out) >= MAX_NODES or depth > MAX_DEPTH or walk.visited >= MAX_VISITED:
            walk.cut = True
            return
        walk.visited += 1
        states = node.states()
        if "showing" not in states and depth > 0:
            return
        role, name = node.role(), node.name()
        entry: dict = {"id": id_, "role": text(role, MAX_WORD), "depth": depth}
        if name:
            entry["name"] = text(name, MAX_NAME_SHOWN)
        if is_secret(role, name):
            entry["hidden"] = True  # never read
        else:
            value = node.text()
            if value and value != name and walk.text > 0:
                value = text(value, min(MAX_TEXT_PER_NODE, walk.text))
                walk.text -= len(value)
                entry["text"] = value
        told = [s for s in STATES if s in states]
        if "enabled" not in states and "sensitive" not in states:
            told.append("disabled")
        if told:
            entry["states"] = told
        actions = [text(a, MAX_WORD) for a in node.actions() if a and not moves_clipboard(a)]
        if actions:
            entry["actions"] = actions[:8]
        if depth == 0 or len(entry) > 3 or actions:
            out.append(entry)
        for i in range(node.child_count()):
            child = node.child(i)
            if child is not None:
                self._walk(child, f"{id_}.{i}", depth + 1, out, walk)

    def describe(self, element: str, name: str) -> dict:
        process, window, node = self._checked(element, name)
        facts = {
            "App": process.label,
            "Window": text(window.name(), 120) or "(no title)",
            "Control": f"{node.role()} «{text(node.name(), 120)}»",
        }
        if node.editable() and not is_secret(node.role(), node.name()):
            facts["Text now"] = text(node.text() or "", 200) or "(empty)"
        return {"facts": facts}

    def activate(self, element: str, name: str, action: str | None = None) -> dict:
        _, _, node = self._checked(element, name)
        actions = [a if not moves_clipboard(a) else "" for a in node.actions()]
        if action is not None:
            if not action or action not in actions:
                has = [text(a, MAX_WORD) for a in actions if a][:8]
                raise ToolError(f"it has no action {text(action, MAX_WORD)!r}; it has {has}")
            index = actions.index(action)
        else:
            usable = [i for i, a in enumerate(actions) if a]
            if not usable:
                raise ToolError("it cannot be pressed")
            index = next((actions.index(a) for a in PREFERRED_ACTIONS if a in actions), usable[0])
        done = text(actions[index], MAX_WORD)
        if not node.do_action(index):
            raise ToolError(f"{done!r} did not work")
        return {"done": done, "element": element, "name": text(node.name(), MAX_NAME_SHOWN)}

    def set_text(self, element: str, name: str, text: str) -> dict:
        _, _, node = self._checked(element, name)
        if is_secret(node.role(), node.name()):
            raise ToolError("a password field: Gloxi never reads or fills it")
        if not node.editable():
            raise ToolError("that is not a field one can type in")
        before = node.text() or ""
        if len(before) > MAX_TEXT_PER_NODE:
            # Only the start of it could be put back: an undo would lose the rest.
            raise ToolError(
                "the field holds more text than Gloxi can put back; the person can change it"
            )
        if not node.set_text(text):
            raise ToolError("the app did not take the text")
        return {
            "element": element,
            "name": name,
            "undo_arguments": {"element": element, "name": node.name(), "text": before},
        }

    def restore(self, element: str, name: str, text: str) -> dict:
        return self.set_text(element, name, text)


class _Walk:
    """What one read_window may still spend, and whether it stopped early."""

    def __init__(self) -> None:
        self.text = MAX_TEXT_TOTAL
        self.visited = 0
        self.cut = False


class AtspiNode:
    """Node over Atspi.Accessible. The interface functions are called through
    their interfaces (Atspi.Text.get_text): the Accessible has methods of the
    same names that mean something else."""

    def __init__(self, acc, Atspi) -> None:
        self._acc = acc
        self._A = Atspi

    def role(self) -> str:
        return self._acc.get_role_name() or ""

    def name(self) -> str:
        return self._acc.get_name() or ""

    def pid(self) -> int:
        return int(self._acc.get_process_id())

    def child_count(self) -> int:
        return max(0, min(int(self._acc.get_child_count()), 10_000))

    def child(self, i: int) -> AtspiNode | None:
        c = self._acc.get_child_at_index(i)
        return AtspiNode(c, self._A) if c is not None else None

    def states(self) -> set[str]:
        return {s.value_nick for s in self._acc.get_state_set().get_states()}

    def text(self) -> str | None:
        t = self._acc.get_text_iface()
        if t is None:
            return None
        n = min(self._A.Text.get_character_count(t), MAX_TEXT_PER_NODE + 1)
        return self._A.Text.get_text(t, 0, n)

    def editable(self) -> bool:
        return self._acc.get_editable_text_iface() is not None and "editable" in self.states()

    def actions(self) -> list[str]:
        a = self._acc.get_action_iface()
        if a is None:
            return []
        n = min(self._A.Action.get_n_actions(a), 16)
        return [self._A.Action.get_action_name(a, i) or "" for i in range(n)]

    def do_action(self, i: int) -> bool:
        a = self._acc.get_action_iface()
        return bool(a is not None and self._A.Action.do_action(a, i))

    def set_text(self, value: str) -> bool:
        e = self._acc.get_editable_text_iface()
        return bool(e is not None and self._A.EditableText.set_text_contents(e, value))


def atspi_desktop() -> Callable[[], Node]:
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    Atspi.init()
    # A hung app must not hang Herkos: give up on it after 2 s.
    Atspi.set_timeout(2000, 5000)
    return lambda: AtspiNode(Atspi.get_desktop(0), Atspi)


def server(apps: Apps) -> Server:
    return Server(
        "vilna-mcp-atspi",
        TOOLS,
        {
            "list_windows": apps.list_windows,
            "read_window": apps.read_window,
            "describe": apps.describe,
            "activate": apps.activate,
            "set_text": apps.set_text,
            "restore": apps.restore,
        },
    )


def main(argv: list[str] | None = None) -> int:
    try:
        desktop = atspi_desktop()
    except (ImportError, ValueError) as e:
        print(f"vilna-mcp-atspi: AT-SPI is not available ({e})", file=sys.stderr)
        return 2
    server(Apps(desktop)).serve(sys.stdin, sys.stdout)
    return 0
