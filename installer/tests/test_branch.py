# SPDX-License-Identifier: GPL-3.0-or-later
"""The branch that grows with the installation and the release sign at its end.

Needs GTK 4, libadwaita and a display (Xvfb is enough); skipped without them.
"""

import os
import struct
import time
import zlib

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

from vilna_installer import branch, release  # noqa: E402

Adw.init()


def spin(until, seconds=10.0):
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.01)
    return until()


@pytest.fixture
def animations():
    settings = Gtk.Settings.get_default()
    before = settings.get_property("gtk-enable-animations")
    yield settings
    settings.set_property("gtk-enable-animations", before)


def shown(widget: Gtk.Widget) -> Gtk.Window:
    win = Gtk.Window()
    win.set_child(widget)
    win.present()
    return win


def png(path, width=4, height=2):
    """A tiny valid RGB PNG."""

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\0" + b"\x80\x40\x20" * width for _ in range(height))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def test_frames_are_ordered_by_the_number_in_their_names(tmp_path):
    for name in ("branch-10.png", "branch-2.png", "branch-1.png", "notes.png"):
        png(tmp_path / name)
    assert [p.name for p in branch.frames(tmp_path)] == [
        "branch-1.png",
        "branch-2.png",
        "branch-10.png",
    ]
    assert branch.frames(tmp_path / "missing") == []


def test_leaves_open_only_after_the_stem_passes_them():
    assert branch.opened(0.5, 0.4) == 0
    assert branch.opened(0.5, 0.53) == pytest.approx(0.5)
    assert branch.opened(0.5, 0.9) == 1


def test_without_animations_the_branch_jumps_and_the_sign_appears_at_once(animations):
    animations.set_property("gtk-enable-animations", False)
    b = branch.Branch()
    win = shown(b)
    b.set_fraction(0.4)
    assert b.shown == pytest.approx(0.4)
    b.set_fraction(7)  # out of range from a bad progress file
    assert b.shown == 1.0
    b.finish()
    assert b.mark_alpha == 1.0
    win.destroy()


def test_with_animations_the_branch_grows_to_the_percent_and_the_sign_lands(animations):
    animations.set_property("gtk-enable-animations", True)
    b = branch.Branch()
    win = shown(b)
    b.set_fraction(0.3)
    assert b.shown < 0.3
    assert spin(lambda: b.shown == pytest.approx(0.3))
    assert b.mark_alpha == 0
    b.finish()
    assert spin(lambda: b.mark_alpha == 1.0)
    win.destroy()


def test_the_release_sign_exists_for_both_backgrounds():
    assert release.NAME
    for dark in (False, True):
        assert release.mark(dark).is_file()


def test_designer_frames_replace_the_drawing(tmp_path, monkeypatch, animations):
    animations.set_property("gtk-enable-animations", False)
    (tmp_path / "dark").mkdir()
    for i in range(1, 4):
        png(tmp_path / f"branch-{i:02}.png")
    for i in range(1, 3):
        png(tmp_path / "dark" / f"branch-{i:02}.png")
    monkeypatch.setattr(branch, "FRAMES", tmp_path)
    b = branch.Branch()
    assert len(b.frames_for(False)) == 3  # no light/: the shared frames
    assert len(b.frames_for(True)) == 2
    win = shown(b)
    b.set_fraction(0.5)
    assert spin(lambda: b.get_mapped())
    win.destroy()
