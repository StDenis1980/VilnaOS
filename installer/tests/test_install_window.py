# SPDX-License-Identifier: GPL-3.0-or-later
"""Step 13 on a display: the engine's progress, the end, and the secrets it is handed.

Needs GTK 4, libadwaita and a display (Xvfb is enough); skipped without them.
The engine runs in its --demo mode: nothing is written, no root.
"""

import os
import time

import pytest

# "gi.repository", not "gi": a package can leave gi/overrides without
# PyGObject, and that bare "gi" has no require_version.
pytest.importorskip("gi.repository", exc_type=ImportError)
import gi  # noqa: E402

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk
except (ImportError, ValueError):
    pytest.skip("GTK 4 or libadwaita is missing", allow_module_level=True)
if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) or not Gtk.init_check():
    pytest.skip("no display", allow_module_level=True)

from vilna_installer import confirm, install_window, start  # noqa: E402
from vilna_installer import engine as eng  # noqa: E402
from vilna_installer.vault import Vault  # noqa: E402

Adw.init()


def spin(until, seconds=30.0):
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.01)
    return until()


def test_the_demo_installation_ends_with_restart(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text("{}")
    app = Adw.Application()
    app.register(None)
    parent = Gtk.Window(application=app)
    vault = Vault()
    vault.put("recovery_key", "secret-key")
    ended = []
    win = install_window.start(
        parent, plan, confirm.Outcome(True, "a" * 64, "secret-key"), vault,
        texts=start.Texts("uk"), demo=True, on_end=ended.append,
    )  # fmt: skip
    assert not vault.has("recovery_key")  # handed over once
    assert not parent.get_visible()
    assert spin(lambda: win.ended)
    assert win.restart.get_visible() and win.title.get_label() == "Vilna встановлено"
    assert all(r.get_label().startswith("✓") for r in win.rows.values())


def test_a_failure_says_whether_the_disk_was_touched(tmp_path):
    app = Adw.Application()
    app.register(None)
    win = install_window.InstallWindow(app, tmp_path / "plan.json", texts=start.Texts("uk"))
    win.show_state({"state": "running", "phase": "model", "percent": 60})
    assert win.rows["layout"].get_label().startswith("✓")
    assert win.rows["model"].get_label().startswith("…")
    assert win.rows["boot"].get_label().startswith("·")
    win.show_state({"state": "failed", "phase": "boot", "error": "sdbootutil failed",
                    "disk_changed": True})  # fmt: skip
    assert win.ended and not win.restart.get_visible()
    assert "частково" in win.lead.get_label() and "sdbootutil" in win.error.get_label()


def test_an_engine_that_dies_silently_is_a_failure(tmp_path):
    app = Adw.Application()
    app.register(None)
    win = install_window.InstallWindow(app, tmp_path / "plan.json", texts=start.Texts("en"))
    win.engine_ended(False)
    assert win.ended and win.title.get_label() == eng.FAILED
