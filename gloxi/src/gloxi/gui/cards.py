# SPDX-License-Identifier: GPL-3.0-or-later
"""Action cards: what Gloxi did through Herkos, as a card in the conversation.

A card is plain data (``ToolOutcome.card()``), so the system service gloxid
can send it to the window as JSON. This module turns it into what the
window shows; the window only draws it (styles: docs/design/style/vilna.css,
``.vilna-action-card``). Preliminary design of wave 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from gettext import gettext as _

from gloxi.i18n import N_

# What Keros found, by rule id (herkos.keros and herkos-core keros.rs use the
# same ids), as the person reads it. An unknown rule shows its English text.
KEROS_RULES = {
    "override": N_("It asks Gloxi to ignore its instructions."),
    "role": N_("It tries to give Gloxi a new role or new instructions."),
    "template": N_("It contains markers that imitate system messages."),
    "hidden_html": N_("It contains text hidden from people."),
    "exfil_link": N_("It contains a link that could carry your data away."),
    "invisible": N_("It contains invisible or direction-changing characters."),
    "model": N_("The check on this computer found text addressed to Gloxi."),
}

# What Keros found, by rule id (herkos.keros and herkos-core keros.rs use the
# same ids), as the person reads it. An unknown rule shows its English text.
KEROS_RULES = {
    "override": N_("It asks Gloxi to ignore its instructions."),
    "role": N_("It tries to give Gloxi a new role or new instructions."),
    "template": N_("It contains markers that imitate system messages."),
    "hidden_html": N_("It contains text hidden from people."),
    "exfil_link": N_("It contains a link that could carry your data away."),
    "invisible": N_("It contains invisible or direction-changing characters."),
    "model": N_("The check on this computer found text addressed to Gloxi."),
}

STATUSES = {"done": N_("Done"), "denied": N_("Denied"), "error": N_("Failed")}
MAX_FIELD = 300  # a card is a summary; long text stays in the answer


@dataclass(frozen=True)
class CardView:
    status: str  # done | denied | error: also the CSS class
    status_label: str
    title: str
    target: str
    detail: str  # why it was denied or failed
    warnings: tuple[str, ...]  # Keros findings on the content


def _text(value: object) -> str:
    text = value if isinstance(value, str) else ""
    return text if len(text) <= MAX_FIELD else text[: MAX_FIELD - 1] + "…"


def _warnings(descriptions: object, rules: object) -> tuple[str, ...]:
    """Keros findings in the window's language; duplicates once, at most five."""
    texts = descriptions if isinstance(descriptions, list) else []
    ids = rules if isinstance(rules, list) else []
    out: list[str] = []
    for i, text in enumerate(texts[:20]):
        rule = ids[i] if i < len(ids) and isinstance(ids[i], str) else ""
        if rule in KEROS_RULES:
            shown = _(KEROS_RULES[rule])
        elif isinstance(text, str) and text:
            shown = _("The content {what}.").format(what=_text(text))
        else:
            continue
        if shown not in out:
            out.append(shown)
    return tuple(out[:5])


def card_view(card: object) -> CardView:
    """What to show for a card; anything malformed is shown as a failure."""
    data = card if isinstance(card, dict) else {}
    status = data.get("status")
    if status not in STATUSES:
        status = "error"
    program, operation = _text(data.get("program")), _text(data.get("operation"))
    title = f"{program}: {operation}" if program and operation else program or operation
    warnings = _warnings(data.get("warnings"), data.get("rules"))
    return CardView(
        status=status,
        status_label=_(STATUSES[status]),
        title=title or _("Action"),
        target=_text(data.get("target")),
        detail="" if status == "done" else _text(data.get("message")),
        warnings=warnings,
    )


# The empty panel's starts (brief 10.4): things Gloxi can do with its own tools.
STARTERS = (
    N_("Tidy up Downloads"),
    N_("What do I have today?"),
    N_("Turn on the dark theme"),
)


def starter_prompts() -> list[str]:
    return [_(s) for s in STARTERS]
