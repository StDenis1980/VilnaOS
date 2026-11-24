# SPDX-License-Identifier: GPL-3.0-or-later
"""Herkos's confirmation window (GTK 4 + libadwaita), run as its own process.

    python -m herkos.dialog < question.json   →   prints the answer

Herkos starts this process for each question (``confirm.Dialog``), so the
window belongs to Herkos and not to Gloxi: the model can neither change its
text nor press its buttons through Gloxi. Enter presses the safest button
for the level (:func:`default_choice`); closing the window or the timeout
means "deny".

Every program of the same user can reach the window through accessibility
(AT-SPI), which Orca needs. So the allow buttons count only a real press,
from the keyboard or the pointer (``real_input``, docs/adr/0029); an AT-SPI
«click» on them does nothing. "Deny" needs no such care.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys

from herkos.confirm import content_lines, readable_resource
from herkos.i18n import _

CONSENT_SOUND = "x-vilna-herkos-consent"


def play_consent_sound(which=shutil.which, popen=subprocess.Popen, enabled=None) -> bool:
    """The Vilna sound theme's signal that Herkos is asking (sounds/).

    Played once through libcanberra's canberra-gtk-play, which follows the
    chosen theme and its switched-off sounds; GNOME's switch for all event
    sounds is checked here because that program ignores it. Without the
    program (outside Vilna) nothing plays. Never blocks the window and
    never touches stdout, where the answer goes. Returns whether it played.
    """
    program = which("canberra-gtk-play")
    if program is None:
        return False
    try:
        on = event_sounds_on() if enabled is None else enabled()
    except Exception:  # noqa: BLE001 - a sound must never stop the question
        on = True
    if not on:
        return False
    with contextlib.suppress(OSError):
        popen(
            [program, "-i", CONSENT_SOUND, "-d", "Herkos"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    return False


def event_sounds_on() -> bool:
    from gi.repository import Gio

    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup("org.gnome.desktop.sound", True) is None:
        return True
    return Gio.Settings.new("org.gnome.desktop.sound").get_boolean("event-sounds")


def translated_raises(question: dict) -> list[str]:
    """Reasons for the higher level, in the window's language.

    herkosd sends them untranslated with their message ids (``raises_i18n``);
    Herkos-lite translates them itself and sends only ``raises``.
    """
    structured = question.get("raises_i18n")
    if not isinstance(structured, list):
        return [str(r) for r in question.get("raises", [])]
    out = []
    for r in structured:
        args = {
            k: "; ".join(_(str(x)) for x in v) if isinstance(v, list) else v
            for k, v in (r.get("args") or {}).items()
        }
        try:
            out.append(_(r["msgid"]).format(**args))
        except (KeyError, IndexError, ValueError):
            out.append(str(r.get("msgid", "")))
    return out


MAX_CONTENT_LINES = 40


def content_text(question: dict) -> str | None:
    """The exact arguments of an R4 action, as the person will read them.

    The system ``herkosd`` sends them as ``content`` for every R4 question:
    "allow" there means "allow exactly this", so the text must be visible.
    """
    if "content" not in question or question["content"] is None:
        return None
    content = question["content"]
    if isinstance(content, dict):
        lines = content_lines(content)
    else:
        lines = json.dumps(content, ensure_ascii=False, indent=2).splitlines()
    if len(lines) > MAX_CONTENT_LINES:
        lines = [*lines[:MAX_CONTENT_LINES], "…"]
    return "\n".join(lines)


DIALOG_ID = "io.github.stdenis1980.Herkos.Dialog"


def application_id(env) -> str:
    """A Flatpak may only use names under its own id (Gloxi bundles Herkos-lite)."""
    flatpak = env.get("FLATPAK_ID")
    return f"{flatpak}.HerkosDialog" if flatpak else DIALOG_ID


NOT_REAL = (
    "Press the button yourself, with the keyboard or the mouse: Herkos does not take presses "
    "from other programs."
)

LABELS = {
    "once": "Allow once",
    "session": "Allow for this session",
    "always": "Always allow",
}  # translated when shown

# The level as a chip: text and colour together (brief, scheme 10.6).
RISK_LABELS = {
    "R0": "R0 · no questions",
    "R1": "R1 · reads your data",
    "R2": "R2 · can be undone",
    "R3": "R3 · hard to undo",
    "R4": "R4 · cannot be undone",
}  # translated when shown

# A shape for each level, so the chip reads without colour: the Herkos shield
# with no, one, two or three dots, and a full shield for R4. The Vilna icons
# (docs/design/icons/system), shipped in data/icons/hicolor (style.icon_dir).
RISK_ICONS = {
    "R0": "vilna-risk-0-symbolic",
    "R1": "vilna-risk-1-symbolic",
    "R2": "vilna-risk-2-symbolic",
    "R3": "vilna-risk-3-symbolic",
    "R4": "vilna-risk-4-symbolic",
}


def risk_level(question: dict) -> str:
    """The level as shown; anything unknown is shown as the strictest, R4."""
    level = str(question.get("level", "")).strip().upper()
    return level if level in RISK_LABELS else "R4"


def suspicious(question: dict) -> bool:
    """Untrusted content took part in this request (Keros, brief scheme 10.7)."""
    return bool(question.get("taint_sources"))


def shown_choices(question: dict) -> list[str]:
    """The allow buttons, in order. "Deny" is always there.

    Herkos already limits the choices by level; the window may only narrow
    them further: with suspicious content or at R4 only "Allow once" stays,
    so a single click never grants more than this one action. "Always allow"
    is offered only up to R2.
    """
    choices = [c for c in question.get("choices", []) if c in LABELS]
    if suspicious(question) or risk_level(question) == "R4":
        choices = [c for c in choices if c == "once"]
    if risk_level(question) not in ("R0", "R1", "R2"):
        choices = [c for c in choices if c != "always"]
    return choices


def default_choice(question: dict) -> str:
    """The button Enter presses: the safest one for the level (design wave 2).

    Up to R2 the change can be undone, so Enter allows it once; from R3, with
    suspicious content or without "Allow once" Enter denies. The irreversible
    allow button of R4 is never the default.
    """
    if (
        risk_level(question) in ("R0", "R1", "R2")
        and not suspicious(question)
        and "once" in shown_choices(question)
    ):
        return "once"
    return "deny"


def needs_acknowledgement(question: dict) -> bool:
    """R4: "allow" stays off until the person ticks that they read the plan."""
    return risk_level(question) == "R4"


def main() -> int:
    try:
        question = json.loads(sys.stdin.read())
    except ValueError:
        print("failed")
        return 1
    if not isinstance(question, dict):
        print("failed")
        return 1
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk, Pango

    from herkos import real_input, style

    # Until the person answers: a window that never showed is no "no" of
    # theirs (VM check 2026-11-24: it was logged as denied_user).
    answer = ["failed"]
    from gi.repository import Gio

    # One window per question: never ask the bus for a name (NON_UNIQUE).
    # The id must still sit under the app's own id inside a Flatpak.
    app = Adw.Application(
        application_id=application_id(os.environ),
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )

    def finish(value: str) -> None:
        answer[0] = value
        app.quit()

    def narrow(label: Gtk.Label) -> Gtk.Label:
        # Long paths and lines wrap anywhere instead of widening the window.
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_max_width_chars(60)
        return label

    def text(value: str, *classes: str, selectable: bool = False) -> Gtk.Label:
        label = Gtk.Label(label=value, xalign=0, wrap=True, selectable=selectable)
        for c in classes:
            label.add_css_class(c)
        return label

    def build(app: Adw.Application) -> None:
        style.install()
        level = risk_level(question)
        # A dialog to the screen reader: Orca reads the question when the window
        # opens, not only the button that has the focus.
        win = Adw.Window(
            application=app,
            title=_("Herkos: permission"),
            modal=True,
            accessible_role=Gtk.AccessibleRole.DIALOG,
        )
        win.set_default_size(480, -1)
        win.connect("close-request", lambda *_: finish("deny") or False)
        gate = real_input.RealInput()
        real_input.watch_window(win, gate)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.add_css_class("vilna-sheet")
        box.add_css_class(level.lower())
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(inner, f"set_margin_{side}")(20)
        box.append(inner)

        # 1. Who asks.  2. What and how risky: the chip has text, not only colour.
        inner.append(text(_("Herkos: Gloxi asks for permission"), "vilna-requester"))
        head = Gtk.Box(spacing=10)
        title = text(_(str(question.get("summary", ""))), "title-3")
        title.set_hexpand(True)
        head.append(title)
        chip = Gtk.Box(spacing=6, valign=Gtk.Align.START)
        shape = Gtk.Image.new_from_icon_name(RISK_ICONS[level])
        shape.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)  # the words say it
        chip.append(shape)
        chip.append(Gtk.Label(label=_(RISK_LABELS[level])))
        chip.add_css_class("vilna-risk")
        chip.add_css_class(level.lower())
        head.append(chip)
        inner.append(head)

        # 3. Why: the model's words, shown as such.
        inner.append(text(_("Gloxi's reason: ") + str(question.get("reason", "")), selectable=True))

        # 4. What changes and whether it can be undone.
        facts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        facts.add_css_class("vilna-facts")
        resources = question.get("resources") or []
        if resources:
            listing = "\n".join(f"• {readable_resource(str(r))}" for r in resources[:10])
            if len(resources) > 10:
                listing += "\n" + _("… and {n} more").format(n=len(resources) - 10)
            facts.append(narrow(text(listing, selectable=True)))
        notes = [_("Can be undone.") if question.get("undoable") else _("Cannot be undone.")]
        riskier = _("Riskier than usual: {why}.")
        notes += [riskier.format(why=w) for w in translated_raises(question)]
        facts.append(text("\n".join(notes), "dim-label"))
        content = content_text(question)
        if content is not None:
            facts.append(text(_("Exactly this will be done:"), "heading"))
            facts.append(narrow(text(content, "monospace", selectable=True)))
        inner.append(facts)

        # 5. Suspicious content (Keros): its own block, noticeable, no panic.
        if suspicious(question):
            warning = Gtk.Box(spacing=10)
            warning.add_css_class("vilna-warning")
            icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
            icon.set_valign(Gtk.Align.START)
            warning.append(icon)
            words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            words.append(text(_("Suspicious content"), "title"))
            sources = ", ".join(str(s) for s in question["taint_sources"])
            words.append(narrow(text(_("Untrusted content came from: {src}").format(src=sources))))
            words.append(text(_("Allow only if you asked for exactly this yourself.")))
            warning.append(words)
            inner.append(warning)

        # 6. Buttons: Enter presses the safest one for the level (default_choice).
        allow_buttons: list[Gtk.Button] = []
        not_real = text(_(NOT_REAL), "dim-label")
        not_real.set_visible(False)

        def allow(button: Gtk.Button, choice: str) -> None:
            # A press sent by a program over AT-SPI is not the person's answer.
            if gate.allows(button):
                finish(choice)
            else:
                not_real.set_visible(True)
                if hasattr(not_real, "announce"):  # GTK 4.14+
                    not_real.announce(
                        not_real.get_label(), Gtk.AccessibleAnnouncementPriority.MEDIUM
                    )

        default = deny = Gtk.Button(label=_("Deny"))
        if needs_acknowledgement(question):
            ack = Gtk.CheckButton(label=_("I have read exactly what will be done"))
            ack.connect(
                "toggled",
                lambda b: [x.set_sensitive(b.get_active()) for x in allow_buttons],
            )
            inner.append(ack)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        deny.connect("clicked", lambda *_: finish("deny"))
        buttons.append(deny)
        for choice in shown_choices(question):
            b = Gtk.Button(label=_(LABELS[choice]))
            b.connect("clicked", lambda b, c=choice: allow(b, c))
            real_input.guard(b, gate)
            if choice == default_choice(question):
                default = b
            if level == "R4":
                b.add_css_class("vilna-danger")
            if needs_acknowledgement(question):
                b.set_sensitive(False)
            allow_buttons.append(b)
            buttons.append(b)
        inner.append(not_real)
        inner.append(buttons)

        win.set_content(box)
        default.add_css_class("suggested-action")
        win.set_default_widget(default)
        default.grab_focus()
        win.present()
        play_consent_sound()

    app.connect("activate", build)
    app.run([])
    print(answer[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
