# SPDX-License-Identifier: GPL-3.0-or-later
"""The message field of the Gloxi window: grows with the text.

A Gtk.Entry is one line, and in the narrow side panel it was a few words
wide (Build12.29). This field wraps the text, grows line by line up to
MAX_LINES and then scrolls. Enter sends (the "activate" signal, as the
entry had), Shift+Enter starts a new line. The methods the window used on
the entry are kept, so the window code reads the same.
"""

from __future__ import annotations

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, GObject, Gtk, Pango  # noqa: E402

MAX_LINES = 8


def sends(keyval: int, state: int) -> bool:
    """Whether a key press sends the message: Enter without Shift."""
    enter = keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter)
    return enter and not state & Gdk.ModifierType.SHIFT_MASK


class MessageInput(Gtk.Overlay):
    __gtype_name__ = "GloxiMessageInput"
    __gsignals__ = {"activate": (GObject.SignalFlags.RUN_LAST, None, ())}

    def __init__(self, placeholder: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.add_css_class("gloxi-input")
        self.view = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            accepts_tab=False,
            top_margin=8,
            bottom_margin=8,
            left_margin=10,
            right_margin=10,
        )
        self.view.update_property([Gtk.AccessibleProperty.LABEL], [placeholder])
        self.scroller = Gtk.ScrolledWindow(
            child=self.view,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,
            hexpand=True,
        )
        self.set_child(self.scroller)
        # No placeholder in a text view: a label over it while it is empty.
        self.placeholder = Gtk.Label(
            label=placeholder,
            xalign=0,
            halign=Gtk.Align.FILL,
            valign=Gtk.Align.START,
            margin_start=10,
            margin_end=10,
            margin_top=8,
            ellipsize=Pango.EllipsizeMode.END,
            can_target=False,
        )
        self.placeholder.add_css_class("dim-label")
        self.add_overlay(self.placeholder)
        self.buffer = self.view.get_buffer()
        self.buffer.connect("changed", lambda *_a: self._sync())
        # Capture phase: before the text view inserts a new line.
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.view.add_controller(keys)
        self.view.connect("notify::root", lambda *_a: self._limit())
        self.view.connect("map", lambda *_a: GLib.idle_add(self._remeasure))
        self._sync()

    def _on_key(self, _controller, keyval: int, _keycode: int, state) -> bool:
        if sends(keyval, state):
            self.emit("activate")
            return True
        return False

    def _limit(self) -> None:
        # MAX_LINES lines of the view's font, then it scrolls.
        metrics = self.view.get_pango_context().get_metrics(None, None)
        line = (metrics.get_ascent() + metrics.get_descent()) / Pango.SCALE
        self.scroller.set_max_content_height(int(line * MAX_LINES * 1.25) + 16)

    def _sync(self) -> None:
        self.placeholder.set_visible(not self.get_text())

    def get_text(self) -> str:
        start, end = self.buffer.get_bounds()
        return self.buffer.get_text(start, end, False)

    def set_text(self, text: str) -> None:
        self.buffer.set_text(text)
        # Text set before the field is shown is measured before the view has
        # laid it out (a line short): measure again once it has.
        GLib.idle_add(self._remeasure)

    def _remeasure(self) -> bool:
        self.view.queue_resize()
        return False

    def grab_focus(self) -> bool:
        return self.view.grab_focus()

    def grab_focus_without_selecting(self) -> bool:
        return self.view.grab_focus()

    def set_position(self, position: int) -> None:
        """Moves the text cursor; -1 is the end, as for Gtk.Entry."""
        if position < 0:
            where = self.buffer.get_end_iter()
        else:
            where = self.buffer.get_iter_at_offset(position)
        self.buffer.place_cursor(where)
        self.view.scroll_mark_onscreen(self.buffer.get_insert())
