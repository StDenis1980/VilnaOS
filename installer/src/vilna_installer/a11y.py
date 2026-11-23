# SPDX-License-Identifier: GPL-3.0-or-later
"""What the screen reader hears, in one place: radio buttons, descriptions, announcements.

Found by walking every installer screen over AT-SPI (GTK 4.14, libadwaita 1.5)
and pinned by ``tests/test_a11y_live.py``:

* a ``Gtk.CheckButton`` in a group still reads as a *check box*; a choice of
  one needs the radio role, given when the button is made (``radio``);
* ``Adw.ActionRow`` points its row and its activatable widget at the subtitle
  with *described-by*, and AT-SPI then gives an empty description, so Orca
  never reads the subtitle: «Erase the whole disk» without what it erases.
  ``follow_subtitle`` puts the subtitle into the description itself, now and
  whenever it changes;
* text that appears without focus (an error, Gloxi's reply) is said with
  ``announce`` (GTK 4.14+);
* once a choice of a group is made, GTK leaves only that button in the Tab
  order, and inside an ``Adw.PreferencesGroup`` the arrows did not move to the
  other buttons either: with «Encrypted, with a recovery key» chosen in
  advance, the keyboard could not reach the other three ways. ``radio`` gives
  every button of a group the arrow keys: they move the focus to the next or
  the previous choice, and Space chooses it (moving alone changes nothing, so
  arrowing past a Wi-Fi network or a disk has no effect of its own).
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

NEXT_KEYS = frozenset({Gdk.KEY_Down, Gdk.KEY_Right, Gdk.KEY_KP_Down, Gdk.KEY_KP_Right})
BACK_KEYS = frozenset({Gdk.KEY_Up, Gdk.KEY_Left, Gdk.KEY_KP_Up, Gdk.KEY_KP_Left})


def announce(widget: Gtk.Widget, text: str, *, high: bool = False) -> None:
    """Say it to the screen reader; the text is on the screen as well."""
    if text and hasattr(widget, "announce"):
        priority = (
            Gtk.AccessibleAnnouncementPriority.HIGH
            if high
            else Gtk.AccessibleAnnouncementPriority.MEDIUM
        )
        widget.announce(text, priority)


def describe(widget: Gtk.Widget, text: str | None) -> None:
    """``text`` as the accessible description, instead of a *described-by* that reads empty."""
    widget.reset_relation(Gtk.AccessibleRelation.DESCRIBED_BY)
    if text:
        widget.update_property([Gtk.AccessibleProperty.DESCRIPTION], [text])
    else:
        widget.reset_property(Gtk.AccessibleProperty.DESCRIPTION)


def label(widget: Gtk.Widget, text: str) -> None:
    """``text`` as the accessible name, also over a row's *labelled-by* title.

    A *labelled-by* relation wins over the label: ``Adw.ActionRow`` adds one
    to its activatable widget, so call this after ``set_activatable_widget``.
    """
    widget.reset_relation(Gtk.AccessibleRelation.LABELLED_BY)
    widget.update_property([Gtk.AccessibleProperty.LABEL], [text])


def radio(group: Gtk.CheckButton, **kwargs: Any) -> Gtk.CheckButton:
    """A check button that reads as a radio button, one choice of ``group``'s.

    The arrow keys move between the choices of the group that are on the screen.
    """
    check = Gtk.CheckButton(accessible_role=Gtk.AccessibleRole.RADIO, group=group, **kwargs)
    check.leader = group  # the leader is in no window: keep it alive with the group
    if not hasattr(group, "members"):
        group.members = []
    group.members.append(check)
    keys = Gtk.EventControllerKey()
    keys.connect("key-pressed", _arrow, check)
    check.add_controller(keys)
    return check


def _arrow(_controller, keyval: int, _code: int, state, check: Gtk.CheckButton) -> bool:
    if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
        return False
    step = 1 if keyval in NEXT_KEYS else -1 if keyval in BACK_KEYS else 0
    if not step:
        return False
    # Rows come and go (a new Wi-Fi scan, a hidden way): only those shown count,
    # and those no longer in a window are forgotten.
    members = check.leader.members
    members[:] = [m for m in members if m.get_root() is not None]
    shown = [m for m in members if m.get_mapped() and m.is_sensitive()]
    if check not in shown or len(shown) < 2:
        return False
    target = shown[(shown.index(check) + step) % len(shown)]
    return target.grab_focus()


def follow_subtitle(row: Adw.ActionRow, *widgets: Gtk.Widget) -> None:
    """The row's subtitle as the description of the row and of ``widgets``, kept in step.

    Call it after ``set_activatable_widget``: that is where libadwaita adds the
    relation this replaces.
    """

    def sync(*_args: Any) -> None:
        text = row.get_subtitle() or None
        for widget in (row, *widgets):
            describe(widget, text)

    row.connect("notify::subtitle", sync)
    sync()
