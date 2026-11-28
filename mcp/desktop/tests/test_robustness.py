# SPDX-License-Identifier: Apache-2.0
"""Windows, processes and permissions that once hid, lost or showed more than they should."""

from __future__ import annotations

import json
import os

import pytest
from fakes import FakeBus, FakeNode
from test_protect import proc

from vilna_mcp_desktop.atspi import Apps
from vilna_mcp_desktop.mcp import ToolError
from vilna_mcp_desktop.portals import Portals
from vilna_mcp_desktop.protect import Process, protected

SH = {"showing", "enabled"}
STORE = "org.freedesktop.impl.portal.PermissionStore"


def who(pid):
    return Process(
        pid=pid, uid=os.getuid(), exe="editor", argv=("editor",), names=frozenset({"editor"})
    )


def apps_for(*kids, title="W"):
    win = FakeNode("frame", title, states_={"showing"}, kids=list(kids))
    root = FakeNode("desktop frame", "d", kids=[FakeNode("application", "a", pid_=5, kids=[win])])
    return Apps(lambda: root, who)


def test_a_window_that_contains_itself_is_read_to_an_end():
    loop = FakeNode("panel", "", states_=set(SH))
    loop.kids = [loop, loop]
    out = apps_for(loop).read_window("5:0.0")
    assert out["complete"] is False


def test_a_tree_cut_by_depth_is_not_complete():
    n = FakeNode("push button", "deep", states_=set(SH), actions_=["click"])
    for _ in range(100):
        n = FakeNode("panel", "", states_=set(SH), kids=[n])
    assert apps_for(n).read_window("5:0.0")["complete"] is False


def test_a_long_field_is_not_replaced_with_an_undo_that_loses_text():
    field = FakeNode("text", "Doc", text_="L" * 50_000, editable_=True, states_=SH | {"editable"})
    with pytest.raises(ToolError):
        apps_for(field).set_text("5:0.0.0", "Doc", "new")
    assert field.text_ == "L" * 50_000


def test_action_names_are_short():
    buttons = [
        FakeNode("push button", f"b{i}", states_=set(SH), actions_=["x" * 100_000])
        for i in range(20)
    ]
    assert len(json.dumps(apps_for(*buttons).read_window("5:0.0"))) < 100_000


def test_one_broken_app_leaves_the_other_windows_listed():
    class Bad(FakeNode):
        def child_count(self):
            raise RuntimeError("timeout")

    good = FakeNode("application", "g", pid_=6, kids=[FakeNode("frame", "G")])
    root = FakeNode("desktop frame", kids=[Bad("application", "b", pid_=7), good])
    assert [w["title"] for w in Apps(lambda: root, who).list_windows()["windows"]] == ["G"]


def test_an_element_with_a_long_name_can_be_pressed_by_the_name_shown():
    button = FakeNode("push button", "N" * 500, states_=set(SH), actions_=["click"])
    a = apps_for(button)
    e = next(x for x in a.read_window("5:0.0")["elements"] if x.get("name", "").startswith("N"))
    a.activate(e["id"], e["name"])
    assert button.pressed == ["click"]


@pytest.mark.parametrize("name", ["Password", "PIN", "One-time code", "Пароль"])
def test_a_field_named_like_a_secret_is_never_read_or_filled(name):
    field = FakeNode("text", name, text_="s3cret", editable_=True, states_=SH | {"editable"})
    a = apps_for(field)
    assert "s3cret" not in json.dumps(a.read_window("5:0.0"))
    with pytest.raises(ToolError):
        a.set_text("5:0.0.0", name, "x")


@pytest.mark.parametrize("action", ["copyLink", "pasteAndGo", "ctrl+c", "co​py", "cоpy"])
def test_clipboard_actions_are_never_offered_however_written(action):
    button = FakeNode("push button", "B", states_=set(SH), actions_=[action])
    a = apps_for(button)
    assert action not in json.dumps(a.read_window("5:0.0"), ensure_ascii=False)
    with pytest.raises(ToolError):
        a.activate("5:0.0.0", "B")


def test_a_hidden_element_cannot_be_pressed():
    hidden = FakeNode("push button", "Delete", states_={"enabled"}, actions_=["click"])
    a = apps_for(hidden, FakeNode("push button", "Ok", states_=set(SH), actions_=["click"]))
    with pytest.raises(ToolError):
        a.activate("5:0.0.0", "Delete")
    assert hidden.pressed == []


@pytest.mark.parametrize(
    "argv",
    [
        ["python3", "-W", "ignore", "-m", "herkos.dialog"],
        ["python3", "-X", "dev", "-m", "herkos.dialog"],
        ["python3", "-u", "-u", "-u", "-u", "-u", "-u", "-u", "-u", "-m", "herkos.dialog"],
    ],
)
def test_a_protected_module_is_seen_behind_python_options(tmp_path, argv):
    assert protected(proc(tmp_path, 1, "/usr/bin/python3", argv)) is not None


@pytest.mark.parametrize("exe", ["nm-connection-editor", "gnome-system-monitor", "xfce4-terminal"])
def test_more_apps_that_hold_secrets_or_run_things_are_protected(tmp_path, exe):
    assert protected(proc(tmp_path, 1, f"/usr/bin/{exe}", [exe])) is not None


def test_an_escaped_flatpak_scope_is_still_known(tmp_path):
    cgroup = "0::/user.slice/app\\x2dflatpak\\x2dorg.gnome.Settings\\x2d1234.scope\n"
    p = proc(tmp_path, 1, "/app/bin/x", ["x"], cgroup=cgroup)
    assert p.flatpak == "org.gnome.Settings"


@pytest.mark.parametrize("value", [["CITY", "²"], ["maybe"], ["x" * 1_000_000]])
def test_a_permission_that_could_not_be_put_back_is_not_changed(value):
    written = []
    permission = "location" if value[0] == "CITY" else "camera"
    bus = FakeBus(
        {
            f"{STORE}.Lookup": lambda a: ({"org.gnome.Maps": value}, False),
            f"{STORE}.SetPermission": lambda a: written.append(a) or (),
        }
    )
    with pytest.raises(ToolError):
        Portals(bus).revoke("org.gnome.Maps", permission)
    assert written == []
