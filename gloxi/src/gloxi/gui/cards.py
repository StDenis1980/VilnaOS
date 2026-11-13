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


def card_view(card: object) -> CardView:
    """What to show for a card; anything malformed is shown as a failure."""
    data = card if isinstance(card, dict) else {}
    status = data.get("status")
    if status not in STATUSES:
        status = "error"
    program, operation = _text(data.get("program")), _text(data.get("operation"))
    title = f"{program}: {operation}" if program and operation else program or operation
    raw = data.get("warnings")
    warnings = (
        tuple(_text(w) for w in raw[:5] if isinstance(w, str)) if isinstance(raw, list) else ()
    )
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
