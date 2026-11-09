# SPDX-License-Identifier: GPL-3.0-or-later
"""The GlobalShortcuts client against a fake portal on a private session bus.

Needs PyGObject (Gio only) and dbus-daemon; skipped where they are missing.
"""

import os
import shutil
import subprocess
import sys
import textwrap

import pytest

gi = pytest.importorskip("gi")

FAKE_PORTAL = textwrap.dedent(
    """
    import sys
    from gi.repository import Gio, GLib

    XML = '''<node><interface name="org.freedesktop.portal.GlobalShortcuts">
      <method name="CreateSession"><arg type="a{sv}" direction="in"/>
        <arg type="o" direction="out"/></method>
      <method name="BindShortcuts"><arg type="o" direction="in"/>
        <arg type="a(sa{sv})" direction="in"/><arg type="s" direction="in"/>
        <arg type="a{sv}" direction="in"/><arg type="o" direction="out"/></method>
      <signal name="Activated"><arg type="o"/><arg type="s"/><arg type="t"/>
        <arg type="a{sv}"/></signal>
    </interface></node>'''
    SESSION = "/org/freedesktop/portal/desktop/session/x/s1"

    def request(sender, token):
        s = sender.lstrip(":").replace(".", "_")
        return f"/org/freedesktop/portal/desktop/request/{s}/{token}"

    def respond(conn, path, sender, results):
        def emit():
            conn.emit_signal(sender, path, "org.freedesktop.portal.Request", "Response",
                             GLib.Variant("(ua{sv})", (0, results)))
            return False
        GLib.timeout_add(50, emit)

    def call(conn, sender, path, iface, method, params, inv):
        args = params.unpack()
        if method == "CreateSession":
            p = request(sender, args[0]["handle_token"])
            inv.return_value(GLib.Variant("(o)", (p,)))
            respond(conn, p, sender, {"session_handle": GLib.Variant("s", SESSION)})
        else:
            print("BIND", [(i, o["preferred_trigger"]) for i, o in args[1]], flush=True)
            p = request(sender, args[3]["handle_token"])
            inv.return_value(GLib.Variant("(o)", (p,)))
            described = {"trigger_description": GLib.Variant("s", "Super+G")}
            bound = [(i, described) for i, _o in args[1]]
            respond(conn, p, sender, {"shortcuts": GLib.Variant("a(sa{sv})", bound)})
            def press():
                conn.emit_signal(None, "/org/freedesktop/portal/desktop",
                    "org.freedesktop.portal.GlobalShortcuts", "Activated",
                    GLib.Variant("(osta{sv})", (SESSION, "toggle", 1, {})))
                return False
            GLib.timeout_add(200, press)

    def acquired(conn, name):
        info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
        conn.register_object("/org/freedesktop/portal/desktop", info, call, None, None)
        print("READY", flush=True)

    Gio.bus_own_name(Gio.BusType.SESSION, "org.freedesktop.portal.Desktop",
                     Gio.BusNameOwnerFlags.NONE, acquired, None, None)
    GLib.MainLoop().run()
    """
)

CLIENT = textwrap.dedent(
    """
    import sys, time
    from gi.repository import GLib
    from gloxi.gui.shortcuts import GlobalShortcut
    loop = GLib.MainLoop()
    def pressed():
        print("TOGGLED", flush=True); loop.quit()
    def missing():
        print("UNAVAILABLE", flush=True); loop.quit()
    GlobalShortcut(pressed, missing).start()
    GLib.timeout_add_seconds(10, loop.quit)
    loop.run()
    """
)


@pytest.mark.skipif(shutil.which("dbus-run-session") is None, reason="no dbus-daemon")
def test_binds_and_toggles_through_the_portal(tmp_path):
    (tmp_path / "portal.py").write_text(FAKE_PORTAL)
    (tmp_path / "client.py").write_text(CLIENT)
    script = (
        f"{sys.executable} {tmp_path / 'portal.py'} & sleep 1; "
        f"{sys.executable} {tmp_path / 'client.py'}; kill %1"
    )
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    env = dict(os.environ, PYTHONPATH=src)
    out = subprocess.run(
        ["dbus-run-session", "--", "sh", "-c", script],
        capture_output=True, text=True, timeout=60, env=env,
    ).stdout  # fmt: skip
    assert "BIND [('toggle', 'LOGO+g'), ('toggle-assistant-key', 'LOGO+SHIFT+F23')]" in out
    assert "TOGGLED" in out


@pytest.mark.skipif(shutil.which("dbus-run-session") is None, reason="no dbus-daemon")
def test_no_portal_means_unavailable(tmp_path):
    (tmp_path / "client.py").write_text(CLIENT)
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    out = subprocess.run(
        ["dbus-run-session", "--", sys.executable, str(tmp_path / "client.py")],
        capture_output=True, text=True, timeout=60, env=dict(os.environ, PYTHONPATH=src),
    ).stdout  # fmt: skip
    assert "UNAVAILABLE" in out


def test_request_path_and_token():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from gloxi.gui.shortcuts import request_path, token

    t = token()
    assert t.startswith("gloxi_") and t.replace("_", "").isalnum()
    assert request_path(":1.42", "abc") == "/org/freedesktop/portal/desktop/request/1_42/abc"
