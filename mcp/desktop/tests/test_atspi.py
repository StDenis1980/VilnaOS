# SPDX-License-Identifier: Apache-2.0
import pytest
from fakes import FakeNode

from vilna_mcp_desktop.atspi import Apps
from vilna_mcp_desktop.mcp import ToolError
from vilna_mcp_desktop.protect import Process

EDITOR, KEYS = 100, 200


def tree():
    note = FakeNode(
        "text", "Note", text_="old text", editable_=True, states_={"showing", "enabled", "editable"}
    )
    password = FakeNode(
        "password text",
        "Password",
        text_="secret123",
        editable_=True,
        states_={"showing", "enabled", "editable"},
    )
    send = FakeNode("push button", "Send", actions_=["click"])
    label = FakeNode("label", "Hello", text_="Hello", actions_=["clipboard.copy", "link.open"])
    hidden = FakeNode("push button", "Hidden", states_={"enabled"}, actions_=["click"])
    window = FakeNode(
        "frame",
        "Draft – Editor",
        states_={"showing", "active"},
        kids=[FakeNode("panel", kids=[label, note, password, send, hidden])],
    )
    editor = FakeNode("application", "editor", pid_=EDITOR, kids=[window])
    keys_window = FakeNode(
        "frame", "Passwords", kids=[FakeNode("push button", "Copy", actions_=["click"])]
    )
    keyring = FakeNode("application", "keepassxc", pid_=KEYS, kids=[keys_window])
    return FakeNode("desktop frame", "main", kids=[keyring, editor]), note, password, send, label


def who(pid):
    names = {EDITOR: "gnome-text-editor", KEYS: "keepassxc"}
    if pid not in names:
        return None
    return Process(
        pid=pid, uid=1000, exe=names[pid], argv=(names[pid],), names=frozenset({names[pid]})
    )


def why(p):
    return None if p is not None and p.exe == "gnome-text-editor" else "a protected app"


@pytest.fixture
def apps():
    desktop, *_ = tree()
    a = Apps(lambda: desktop, who, why)
    a.nodes = _
    return a


def elements(apps):
    return {e.get("name"): e for e in apps.read_window(f"{EDITOR}:1.0")["elements"]}


def test_protected_apps_are_never_listed(apps):
    assert apps.list_windows() == {
        "windows": [
            {
                "window": f"{EDITOR}:1.0",
                "app": "gnome-text-editor",
                "title": "Draft – Editor",
                "active": True,
            }
        ]
    }
    for bad in (f"{KEYS}:0.0", f"{KEYS}:0.0.0"):
        with pytest.raises(ToolError, match="not open to Gloxi"):
            apps.read_window(bad)
        with pytest.raises(ToolError):
            apps.activate(bad, "Copy")


def test_ids_must_name_the_same_process(apps):
    # The app at that place is the editor, but the id says another process.
    with pytest.raises(ToolError, match="not open to Gloxi"):
        apps.read_window(f"{EDITOR + 1}:1.0")
    for bad in ("1.0", f"{EDITOR}:1", f"{EDITOR}:x.0", f"{EDITOR}:1.0;rm", f"{EDITOR}:9.0"):
        with pytest.raises(ToolError):
            apps.read_window(bad)


def test_read_window_hides_passwords_clipboard_and_what_is_not_shown(apps):
    els = elements(apps)
    assert els["Password"]["hidden"] is True and "text" not in els["Password"]
    assert els["Note"]["text"] == "old text" and "editable" in els["Note"]["states"]
    assert els["Send"]["actions"] == ["click"]
    assert els["Hello"]["actions"] == ["link.open"]  # no clipboard actions
    assert "Hidden" not in els  # not showing
    assert "secret123" not in repr(apps.read_window(f"{EDITOR}:1.0"))
    with pytest.raises(ToolError, match="not a window"):
        apps.read_window(els["Send"]["id"])


def test_describe_is_what_the_person_sees(apps):
    els = elements(apps)
    assert apps.describe(els["Send"]["id"], "Send") == {
        "facts": {
            "App": "gnome-text-editor",
            "Window": "Draft – Editor",
            "Control": "push button «Send»",
        }
    }
    assert apps.describe(els["Note"]["id"], "Note")["facts"]["Text now"] == "old text"
    assert "Text now" not in apps.describe(els["Password"]["id"], "Password")["facts"]


def test_activate_presses_the_element_gloxi_saw(apps):
    note, password, send, label = apps.nodes
    els = elements(apps)
    assert apps.activate(els["Send"]["id"], "Send")["done"] == "click"
    assert send.pressed == ["click"]
    # The window changed: another name at that place, nothing is pressed.
    send.name_ = "Delete everything"
    with pytest.raises(ToolError, match="window changed"):
        apps.activate(els["Send"]["id"], "Send")
    assert send.pressed == ["click"]
    # Clipboard actions are never done, named or not.
    with pytest.raises(ToolError):
        apps.activate(els["Hello"]["id"], "Hello", "clipboard.copy")
    assert apps.activate(els["Hello"]["id"], "Hello")["done"] == "link.open"
    with pytest.raises(ToolError, match="cannot be pressed"):
        apps.activate(els["Note"]["id"], "Note")


def test_set_text_returns_the_old_text_for_undo(apps):
    note, password, *_ = apps.nodes
    els = elements(apps)
    out = apps.set_text(els["Note"]["id"], "Note", "new text")
    assert note.text_ == "new text"
    assert out["undo_arguments"] == {
        "element": els["Note"]["id"],
        "name": "Note",
        "text": "old text",
    }
    apps.restore(**out["undo_arguments"])
    assert note.text_ == "old text"
    with pytest.raises(ToolError, match="password"):
        apps.set_text(els["Password"]["id"], "Password", "x")
    assert password.text_ == "secret123"
    with pytest.raises(ToolError, match="not a field"):
        apps.set_text(els["Send"]["id"], "Send", "x")


def test_read_window_is_bounded():
    from vilna_mcp_desktop import atspi

    deep = FakeNode("push button", "leaf", actions_=["click"])
    for _ in range(atspi.MAX_DEPTH + 10):
        deep = FakeNode("panel", "p", kids=[deep])
    wide = FakeNode(
        "panel", kids=[FakeNode("push button", str(i), actions_=["click"]) for i in range(5000)]
    )
    window = FakeNode("frame", "w", kids=[deep, wide])
    desktop = FakeNode("desktop frame", kids=[FakeNode("application", pid_=EDITOR, kids=[window])])
    out = Apps(lambda: desktop, who, why).read_window(f"{EDITOR}:0.0")
    assert len(out["elements"]) <= atspi.MAX_NODES and out["complete"] is False
    assert max(e["depth"] for e in out["elements"]) <= atspi.MAX_DEPTH
