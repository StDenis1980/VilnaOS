# SPDX-License-Identifier: GPL-3.0-or-later
"""The model switcher's list (brief, scheme 10.3), apart from GTK.

Grouped by where the messages go: "Auto" first, then this computer, then
the cloud, each with a tag, so the person sees before choosing whether the
words leave the computer. Preliminary design of wave 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from gettext import gettext as _

from gloxi.gui.controller import ModelChoice
from gloxi.i18n import N_

SECTIONS = (
    ("auto", None),
    ("local", N_("On this computer")),
    ("cloud", N_("In the cloud")),
)
TAGS = {"auto": N_("recommended"), "local": N_("local"), "cloud": N_("cloud")}


@dataclass(frozen=True)
class Section:
    title: str | None
    choices: tuple[ModelChoice, ...]
    note: str | None = None  # instead of choices: why the section is empty


def sections(choices: list[ModelChoice]) -> list[Section]:
    """The switcher's sections; with the cloud off it says so instead of hiding it."""
    out = []
    for mode, title in SECTIONS:
        found = tuple(c for c in choices if c.mode == mode)
        if found:
            out.append(Section(_(title) if title else None, found))
        elif mode == "cloud":
            out.append(Section(_(title), (), _("Turned off in the settings")))
    return out


def tag(mode: str) -> str:
    return _(TAGS.get(mode, "local"))


def switch_note(choice: ModelChoice) -> str:
    """The line in the conversation after a switch: from here on, another model answers."""
    return _("Model changed to «{model}» · it answers from the next message").format(
        model=choice.label
    )


def button_label(choice: ModelChoice) -> str:
    """The switcher's own label: the model and its tag, without saying a word twice."""
    tagged = tag(choice.mode)
    if tagged.casefold() == choice.label.casefold():
        return choice.label
    return f"{choice.label} · {tagged}"
