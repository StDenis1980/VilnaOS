# SPDX-License-Identifier: GPL-3.0-or-later
"""``/feedback``: report a problem or suggest something (ADR 0018, Gloxi 0.1).

A fixed script, not the model, like the backup talk: Gloxi asks what
happened (or what the idea is), offers to add the hardware block of
``gloxi check`` and the latest local crash report, hides personal data with
the privacy gateway, shows the whole draft and, only after "open", gives the
front end the address of the public feedback form with the fields filled in.
Nothing leaves the computer from here: the person reads the form in the
browser, can still change it there, and presses "Submit" themselves.

The form is public (GitHub Issues), and the talk says so before anything is
opened. Vulnerabilities go privately, as SECURITY.md says. From Gloxi 0.2
the draft goes to our own server instead (reports.vilnaos.com).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from gettext import gettext as _
from urllib.parse import urlencode

from privacy_gateway import Masker, detect

from gloxi import __version__, check, crash
from gloxi.backup_intro import Turn

FORM_URL = "https://github.com/StDenis1980/VilnaOS/issues/new"
TEMPLATE = "gloxi-feedback.yml"
SECURITY_URL = "https://github.com/StDenis1980/VilnaOS/security/advisories/new"
# Each answer is cut to this many characters, so the address stays well under
# the length browsers and GitHub accept.
MAX_TEXT = 1500
TITLE_WORDS = 10

# Words that suggest a vulnerability: the talk then points to the private channel.
SECURITY_HINTS = re.compile(
    r"vulnerab|exploit|bypass|вразлив|обійти|обхід|vulnerabil|sicherheitsl|"
    r"podatno|zranitel|pažeidžiam|faille|vulnerabilit",
    re.IGNORECASE,
)

# "Report a bug", "I have a suggestion"… in the ADR 0009 languages: such a
# message starts /feedback instead of going to the model.
WANTS_FEEDBACK = re.compile(
    r"^\W*(?:"
    r"(?:повідом\w*|зголос\w*) про (?:помилку|проблему|баг)|(?:маю|є) пропозиці\w*|"
    r"(?:залишити|надіслати|надішли) відгук|"
    r"report (?:a |this )?(?:bug|problem|issue)|(?:send|leave|give) feedback|"
    r"i have (?:a suggestion|an idea)|"
    r"reportar (?:un )?(?:error|fallo|problema)|tengo una sugerencia|enviar (?:un )?comentario|"
    r"zgło(?:ś|sić) (?:błąd|problem)|mam (?:sugestię|pomysł)|"
    r"(?:einen )?fehler melden|ich habe einen vorschlag|"
    r"signaler un (?:bug|problème)|j'ai une suggestion|"
    r"segnala(?:re)? un (?:bug|errore|problema)|ho un suggerimento|"
    r"raportea(?:ză|za) (?:o eroare|o problemă)|am o sugestie|"
    r"nahlásit (?:chybu|problém)|mám návrh|"
    r"pranešti apie (?:klaidą|problemą)|turiu pasiūlymą"
    r")",
    re.IGNORECASE,
)
MAX_TRIGGER = 120


def wants_feedback(text: str) -> bool:
    """A short message that asks to report something, in any main language."""
    return len(text) <= MAX_TRIGGER and WANTS_FEEDBACK.search(text) is not None


def kind_name(kind: str) -> str:
    return {
        "SECRET": _("a password or key"),
        "EMAIL": _("an e-mail address"),
        "PHONE": _("a phone number"),
        "IBAN": _("an account number"),
        "CARD": _("a card number"),
        "TAX_ID": _("a tax number"),
        "NATIONAL_ID": _("a personal ID number"),
        "PASSPORT": _("a passport number"),
        "IP": _("an IP address"),
        "PROFILE": _("a profile link"),
        "NAME": _("a name"),
        "BIRTH_DATE": _("a date of birth"),
        "ADDRESS": _("an address"),
    }.get(kind, kind)


def _yes(text: str) -> bool | None:
    """1/yes or 2/no, in any of the words people type; None if unclear."""
    value = text.strip().lower().rstrip(".!")
    if value in ("1", "y", "yes", "так", "т", "да", "sí", "si", "tak", "ja", "oui", "ano", "taip"):
        return True
    if value in ("2", "n", "no", "ні", "н", "nie", "nein", "non", "ne", "nu"):
        return False
    return None


@dataclass
class Draft:
    kind: str = "bug"  # "bug" or "idea"
    what: str = ""
    expected: str = ""
    system: list[str] = field(default_factory=list)  # gloxi check lines
    crash: str = ""
    hidden: list[str] = field(default_factory=list)  # kinds of data the gateway hid

    def title(self) -> str:
        words = self.what.split()
        short = " ".join(words[:TITLE_WORDS]) + (" …" if len(words) > TITLE_WORDS else "")
        tag = "bug" if self.kind == "bug" else "idea"
        return f"[{tag}] {short}"

    def fields(self) -> dict[str, str]:
        """Issue form field id → value (.github/ISSUE_TEMPLATE/gloxi-feedback.yml)."""
        values = {"title": self.title(), "version": __version__}
        if self.kind == "bug":
            failed = self.what
            if self.expected:
                failed += "\n\n" + _("Expected:") + " " + self.expected
            values["failed"] = failed
        else:
            values["idea"] = self.what
        info = list(self.system)
        if self.crash:
            info += ["", self.crash.rstrip()]
        if info:
            values["check"] = "\n".join(info)
        system = next((line for line in self.system if line.startswith("System: ")), "")
        if system:
            values["system"] = system.removeprefix("System: ")
        return values

    def url(self) -> str:
        return f"{FORM_URL}?{urlencode({'template': TEMPLATE, **self.fields()})}"

    def render(self) -> str:
        """The draft as the person sees it before anything is opened."""
        lines = [_("Title:") + " " + self.title()]
        if self.kind == "bug":
            lines += [_("What happened:") + " " + self.what]
            if self.expected:
                lines += [_("Expected:") + " " + self.expected]
        else:
            lines += [_("Suggestion:") + " " + self.what]
        if self.system:
            lines += ["", _("About this computer:"), *self.system]
        if self.crash:
            lines += ["", _("Crash report:"), self.crash.rstrip()]
        if self.hidden:
            lines += [
                "",
                _("Hidden before showing it here: {what}.").format(what=", ".join(self.hidden)),
            ]
        return "\n".join(lines)


@dataclass
class FeedbackTalk:
    """Turn by turn, like :class:`gloxi.backup_intro.BackupIntro`."""

    system_lines: Callable[[], list[str]] = lambda: check.collect().lines()
    latest_crash: Callable[[], crash.Crash | None] = crash.latest
    step: str = "idle"
    draft: Draft = field(default_factory=Draft)
    url: str | None = None
    _crash: crash.Crash | None = None

    @property
    def active(self) -> bool:
        return self.step not in ("idle", "done")

    def start(self, text: str = "") -> Turn:
        """Begin; ``/feedback <text>`` already says what happened."""
        self.draft = Draft()
        self.url = None
        self._crash = self.latest_crash()
        if text.strip():
            self.draft.what = text.strip()
            return self._after_what()
        self.step = "kind"
        return Turn(
            _("What would you like to tell us?"),
            (_("Something went wrong"), _("A suggestion"), _("Never mind")),
        )

    def answer(self, text: str) -> Turn:
        text = text.strip()
        step = getattr(self, f"_on_{self.step}", None)
        if step is None:
            return self.start(text)
        return step(text)

    # -- steps ---------------------------------------------------------------

    def _on_kind(self, text: str) -> Turn:
        choice = text.lower()
        if choice in ("1",) or choice.startswith(("something", "помил", "щось")):
            self.draft.kind = "bug"
            self.step = "what"
            return Turn(_("What did you do or ask, and what happened?"))
        if choice in ("2",) or choice.startswith(("a sugg", "sugg", "idea", "пропоз", "ідея")):
            self.draft.kind = "idea"
            self.step = "what"
            return Turn(_("What would you suggest? A sentence or two is enough."))
        if choice in ("3",) or choice.startswith(("never", "cancel", "ні", "скас")):
            return self._cancel()
        return Turn(
            _("Please answer 1, 2 or 3."),
            (_("Something went wrong"), _("A suggestion"), _("Never mind")),
        )

    def _on_what(self, text: str) -> Turn:
        if not text:
            return Turn(_("Please write a few words, or /cancel to stop."))
        self.draft.what = text
        return self._after_what()

    def _after_what(self) -> Turn:
        if SECURITY_HINTS.search(self.draft.what):
            self.step = "done"
            return Turn(
                _(
                    "This sounds like a security problem. Please do not post it publicly: "
                    "report it privately at {url}"
                ).format(url=SECURITY_URL),
                done=True,
            )
        if self.draft.kind == "bug" and not self.draft.expected:
            self.step = "expected"
            return Turn(_("What did you expect instead? (Enter to skip)"))
        return self._ask_system()

    def _on_expected(self, text: str) -> Turn:
        self.draft.expected = text
        return self._ask_system()

    def _ask_system(self) -> Turn:
        self.step = "system"
        return Turn(
            _(
                "May I add what this computer is (system, processor, memory, graphics, "
                "models), as gloxi check shows it? Nothing personal."
            ),
            (_("Yes"), _("No")),
        )

    def _on_system(self, text: str) -> Turn:
        answer = _yes(text)
        if answer is None:
            return Turn(_("Please answer 1 or 2."), (_("Yes"), _("No")))
        if answer:
            try:
                self.draft.system = self.system_lines()
            except OSError:
                self.draft.system = []
        if self._crash is not None:
            self.step = "crash"
            return Turn(
                _("Gloxi stopped with an error recently. Add that crash report?")
                + "\n\n"
                + self._crash.text.rstrip(),
                (_("Yes"), _("No")),
            )
        return self._show()

    def _on_crash(self, text: str) -> Turn:
        answer = _yes(text)
        if answer is None:
            return Turn(_("Please answer 1 or 2."), (_("Yes"), _("No")))
        if answer and self._crash is not None:
            self.draft.crash = self._crash.text
        return self._show()

    def _show(self) -> Turn:
        self._hide_personal_data()
        self.step = "confirm"
        return Turn(
            self.draft.render()
            + "\n\n"
            + _(
                "This is public: anyone can read it on GitHub, and you need a GitHub account "
                "to send it. The form opens in your browser, where you can still change "
                "everything; it is sent only when you press Create there."
            ),
            (_("Open the form"), _("Cancel")),
        )

    def _hide_personal_data(self) -> None:
        # One for the whole draft: the same value, the same mark. Strict: a report
        # leaves the computer, so every number that passes its check is hidden.
        masker = Masker(strict=True)
        hidden: list[str] = []
        for name in ("what", "expected"):
            value = getattr(self.draft, name)[:MAX_TEXT]
            for finding in detect(value):
                label = kind_name(str(finding.kind))
                if label not in hidden:
                    hidden.append(label)
            setattr(self.draft, name, masker.mask(value))
        self.draft.hidden = hidden

    def _on_confirm(self, text: str) -> Turn:
        answer = _yes(text)
        if answer is None and text.lower().startswith(("open", "відкр")):
            answer = True
        if answer is None:
            return Turn(_("Please answer 1 or 2."), (_("Open the form"), _("Cancel")))
        if not answer:
            return self._cancel()
        self.step = "done"
        self.url = self.draft.url()
        return Turn(
            _("Opening the form in your browser. Thank you! If it does not open: {url}").format(
                url=self.url
            ),
            done=True,
        )

    def _cancel(self) -> Turn:
        self.step = "done"
        self.url = None
        return Turn(_("All right, nothing was sent."), done=True)

    def cancel(self) -> Turn:
        return self._cancel()
