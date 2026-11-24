# SPDX-License-Identifier: GPL-3.0-or-later
"""Milestone В3 as fixed steps: "read the email, add the event, prepare the answer".

Left to call the functions itself, a small local model did not get through
the scenario: Qwen3 4B 2 of 20 requests, Ministral 3 8B 9 of 20 (gloxi-bench
--tasks mail, 2026-11-24). It skipped reading the email, guessed the date,
forgot the draft or answered the person in English. So for such a message
Gloxi takes the steps itself, as it does for /feedback, and asks the model
only what needs a model, each time as one JSON object that llama-server holds
to a schema:

1. the latest emails in the inbox (``list_messages``), and which of them the
   person means (the model, unless there is one);
2. that email (``read_message``);
3. the meeting's day, time and place and the text of the answer (the model);
4. the event (``create_event``) and the draft (``save_draft``), with the
   account, the address, the subject and ``in_reply_to`` from what the mail
   program returned, never from the email's text or the model.

Every step goes through Herkos like any other call, so the person is asked
where the levels say so (R3 for the event and the draft after an email), and
nothing is sent. The email reaches the model only inside Keros boundaries
and stays out of the history. What Gloxi tells the person at the end is a
fixed sentence in the language of their message, followed by the answer.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from gloxi.llm import Message, ModelError, ModelUnavailable
from gloxi.tools import (
    CALENDAR_TOOL,
    MAIL_TOOL,
    FileTools,
    ToolOutcome,
    _folded_words,
    _has,
    _stems,
)

# How many of the latest emails the model chooses from.
LATEST = 10
# The email as the model reads it for step 3: enough for a letter, not a thread.
MAX_EMAIL_CHARS = 4000
MAX_TITLE = 120
MAX_REPLY = 2000
EVENT_HOURS = 1

# The operations the steps need, by program.
NEEDED = {
    MAIL_TOOL: ("list_messages", "read_message", "save_draft"),
    CALENDAR_TOOL: ("create_event",),
}

Complete = Callable[[list[Message], dict], str]

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

CHOOSE_PROMPT = (
    "You pick the email the user means from a list of their latest emails. "
    'Answer with JSON only: {"uid": <the uid of that email>}, or {"uid": 0} '
    "when none of them fits."
)

EXTRACT_PROMPT = (
    "The user asked you to add a meeting from an email to their calendar and to "
    "prepare an answer to the email. Answer with one JSON object only:\n"
    "- title: a short name for the meeting, in the language of the user's message;\n"
    "- weekday: the day of the week of the meeting in English (Monday…Sunday), empty "
    "when the email names no day;\n"
    "- day_of_month: the number of that day in its month, 0 when the email does not say;\n"
    "- date: the day of the meeting as YYYY-MM-DD, counted from the day the email "
    "was sent when it says 'tomorrow' or 'on Friday'; empty when the email names no day;\n"
    "- start_time: when it starts, HH:MM on a 24-hour clock; empty when not said;\n"
    "- end_time: when it ends, HH:MM; empty when not said;\n"
    "- location: where, empty when not said;\n"
    "- reply: the answer to the email, written as the user to the sender, in the "
    "language of the email, short and friendly, saying yes unless the user said "
    "otherwise; no subject line, no placeholders like [Name]."
)

CHOOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"uid": {"type": "integer"}},
    "required": ["uid"],
}

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        # Before the date: a small model writes "Friday" and "27" right more
        # often than "2026-11-27" (Qwen3 4B put 3 of 20 events on the 25th or
        # 26th, 2026-11-24). Gloxi fixes the date by them (``_fixed_day``).
        "weekday": {"type": "string", "enum": ["", *_WEEKDAYS]},
        "day_of_month": {"type": "integer"},
        "date": {"type": "string"},
        "start_time": {"type": "string"},
        "end_time": {"type": "string"},
        "location": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": [
        "title",
        "weekday",
        "day_of_month",
        "date",
        "start_time",
        "end_time",
        "location",
        "reply",
    ],
}

# What Gloxi says at the end, in the language of the person's message.
SAY: dict[str, dict[str, str]] = {
    "uk": {
        "event": "Подію «{title}» додано в календар: {when}.",
        "event_failed": "Подію в календар не додано.",
        "no_time": "У листі немає дня й часу зустрічі, тож подію не додано.",
        "draft": "Відповідь для {to} збережено в чернетках і не надіслано: надішлю її, "
        "лише коли попросиш. Ось вона:",
        "draft_failed": "Чернетку відповіді не збережено.",
        "no_mail": "Серед останніх листів у вхідних немає того, про який ти пишеш.",
    },
    "en": {
        "event": "I added “{title}” to your calendar: {when}.",
        "event_failed": "The event was not added to your calendar.",
        "no_time": "The email does not name a day and time for the meeting, so I did not "
        "add an event.",
        "draft": "The reply to {to} is in your drafts and has not been sent: I will send it "
        "only when you ask. Here it is:",
        "draft_failed": "The reply was not saved as a draft.",
        "no_mail": "I did not find the email you mean among the latest ones in your inbox.",
    },
    "es": {
        "event": "He añadido «{title}» a tu calendario: {when}.",
        "event_failed": "El evento no se ha añadido a tu calendario.",
        "no_time": "El correo no dice el día y la hora de la reunión, así que no he añadido "
        "ningún evento.",
        "draft": "La respuesta para {to} está en tus borradores y no se ha enviado: la "
        "enviaré solo cuando me lo pidas. Aquí está:",
        "draft_failed": "La respuesta no se ha guardado como borrador.",
        "no_mail": "No he encontrado el correo que dices entre los últimos de tu bandeja de "
        "entrada.",
    },
    "pl": {
        "event": "Wydarzenie „{title}” jest w twoim kalendarzu: {when}.",
        "event_failed": "Wydarzenie nie zostało dodane do kalendarza.",
        "no_time": "W wiadomości nie ma dnia i godziny spotkania, więc nie dodano wydarzenia.",
        "draft": "Odpowiedź do {to} jest w wersjach roboczych i nie została wysłana: wyślę "
        "ją tylko wtedy, gdy o to poprosisz. Oto ona:",
        "draft_failed": "Odpowiedź nie została zapisana jako wersja robocza.",
        "no_mail": "Wśród ostatnich wiadomości w skrzynce odbiorczej nie ma tej, o której piszesz.",
    },
    "de": {
        "event": "Der Termin „{title}“ ist in deinem Kalender: {when}.",
        "event_failed": "Der Termin ist nicht in deinem Kalender.",
        "no_time": "Die E-Mail nennt keinen Tag und keine Uhrzeit für das Treffen, deshalb "
        "habe ich keinen Termin eingetragen.",
        "draft": "Die Antwort an {to} liegt in deinen Entwürfen und ist nicht gesendet: Ich "
        "sende sie nur, wenn du mich darum bittest. Hier ist sie:",
        "draft_failed": "Die Antwort ist nicht als Entwurf gespeichert.",
        "no_mail": "Ich habe die E-Mail, die du meinst, unter den letzten im Posteingang "
        "nicht gefunden.",
    },
    "fr": {
        "event": "C'est fait, « {title} » est dans ton agenda : {when}.",
        "event_failed": "Je n'ai pas ajouté l'événement à ton agenda.",
        "no_time": "Le message ne donne pas le jour et l'heure de la réunion, donc je n'ai "
        "pas ajouté d'événement.",
        "draft": "La réponse pour {to} est dans tes brouillons et n'a pas été envoyée : je "
        "l'enverrai seulement quand tu me le demanderas. La voici :",
        "draft_failed": "La réponse n'a pas été enregistrée comme brouillon.",
        "no_mail": "Je n'ai pas trouvé le message dont tu parles parmi les derniers de ta "
        "boîte de réception.",
    },
    "it": {
        "event": "Ho aggiunto «{title}» al tuo calendario: {when}.",
        "event_failed": "L'evento non è stato aggiunto al tuo calendario.",
        "no_time": "L'email non indica il giorno e l'ora dell'incontro, quindi non ho "
        "aggiunto un evento.",
        "draft": "La risposta per {to} è nelle bozze e non è stata inviata: la invierò solo "
        "quando me lo chiedi. Eccola:",
        "draft_failed": "Non ho salvato la risposta nelle bozze.",
        "no_mail": "Non ho trovato l'email che dici tra le ultime della posta in arrivo.",
    },
    "ro": {
        "event": "Am adăugat „{title}” în calendarul tău: {when}.",
        "event_failed": "Evenimentul nu a fost adăugat în calendar.",
        "no_time": "Mesajul nu spune ziua și ora întâlnirii, așa că nu am adăugat un eveniment.",
        "draft": "Răspunsul pentru {to} este în ciorne și nu a fost trimis: îl trimit doar "
        "când îmi ceri. Iată-l:",
        "draft_failed": "Răspunsul nu a fost salvat în ciorne.",
        "no_mail": "Nu am găsit mesajul despre care vorbești printre ultimele din căsuța de "
        "intrare.",
    },
    "cs": {
        "event": "Událost „{title}“ je v kalendáři: {when}.",
        "event_failed": "Událost se do kalendáře nepřidala.",
        "no_time": "Zpráva neuvádí den a čas schůzky, takže událost není v kalendáři.",
        "draft": "Odpověď pro {to} je v konceptech a není odeslaná: pošlu ji, jen když o to "
        "požádáš. Tady je:",
        "draft_failed": "Odpověď se neuložila jako koncept.",
        "no_mail": "Mezi posledními zprávami v doručené poště ta, o které píšeš, není.",
    },
    "lt": {
        "event": "„{title}“ įtrauktas į tavo kalendorių: {when}.",
        "event_failed": "Įvykis į kalendorių neįtrauktas.",
        "no_time": "Laiške nėra susitikimo dienos ir laiko, todėl įvykis neįtrauktas.",
        "draft": "Atsakymas {to} yra juodraščiuose ir neišsiųstas: išsiųsiu jį tik tada, "
        "kai paprašysi. Štai jis:",
        "draft_failed": "Atsakymas neišsaugotas kaip juodraštis.",
        "no_mail": "Tarp paskutinių gautų laiškų nėra to, apie kurį rašai.",
    },
}


def wants(tools: FileTools, text: str) -> bool:
    """A message that asks for the whole scenario, in any of the ten languages,
    with the functions it needs listed: an email ("лист", "Марта написала"),
    the calendar ("додай у календар", "зустріч") and an answer ("підготуй
    відповідь"), by the stems of ``program_words.toml``."""
    if not all(tools.function_for(t, op) for t, ops in NEEDED.items() for op in ops):
        return False
    folded = _folded_words(text)

    def said(*stems: tuple[str, ...]) -> bool:
        return any(_has(folded, s) for group in stems for s in group)

    mail = said(_stems(MAIL_TOOL, ""), _stems(MAIL_TOOL, "read_message"))
    calendar = said(_stems(CALENDAR_TOOL, ""), _stems(CALENDAR_TOOL, "create_event"))
    answer = said(_stems(MAIL_TOOL, "save_draft"))
    return mail and calendar and answer


def language(text: str) -> str:
    """The language of the person's message, for what Gloxi says at the end.

    A rough guess from common words (``gloxi.bench.guess_language``); when it
    has nothing to go by, "â" and "î" without French letters mean Romanian,
    and otherwise the interface language (ADR 0009) is used.
    """
    from gloxi.bench import guess_language

    found = guess_language(text)
    if found is None and re.search("[âîÂÎ]", text) and not re.search("[çœêèûÇŒÊÈÛ]", text):
        found = "ro"
    if found is None:
        for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            value = os.environ.get(name, "")
            if value:
                found = value.split(":")[0][:2].lower()
                break
    return found if found in SAY else "en"


@dataclass
class Facts:
    """What the model took from the email (step 3), checked."""

    title: str
    start: str  # YYYY-MM-DDTHH:MM, or "" when the email gives no day and time
    end: str
    location: str
    reply: str


class MailFlow:
    def __init__(
        self,
        tools: FileTools,
        complete: Complete,
        *,
        today: date,
        user_turn: int,
        on_tool: Callable[[ToolOutcome], None] | None = None,
    ) -> None:
        self.tools = tools
        self.complete = complete
        self.today = today
        self.user_turn = user_turn
        self.on_tool = on_tool

    def run(self, text: str) -> Iterator[str]:
        say = SAY[language(text)]
        read = self._find(text)
        if read is None:
            yield say["no_mail"]
            return
        facts = self._facts(text, read)
        lines = []
        if facts is None or not facts.start:
            lines.append(say["no_time"])
        else:
            event = self._event(facts)
            if event.status == "done":
                lines.append(say["event"].format(title=facts.title, when=_when(facts.start)))
            else:
                lines.append(say["event_failed"])
        reply = facts.reply if facts is not None else ""
        draft = self._draft(read.result, reply) if reply else None
        if draft is not None and draft.status == "done":
            to = _reply_address(read.result)
            lines.append(say["draft"].format(to=to) + "\n\n" + reply)
        else:
            lines.append(say["draft_failed"])
        yield "\n\n".join(lines)

    # -- steps ----------------------------------------------------------------

    def _call(self, tool: str, operation: str, arguments: dict[str, Any], reason: str):
        name = self.tools.function_for(tool, operation) or ""
        raw = json.dumps({**arguments, "reason": reason}, ensure_ascii=False)
        outcome = self.tools.call(name, raw, user_turn=self.user_turn)
        if self.on_tool:
            self.on_tool(outcome)
        return outcome

    def _find(self, text: str) -> ToolOutcome | None:
        """Steps 1–2: the email the person means, read."""
        listing = self._call(
            MAIL_TOOL, "list_messages", {"limit": LATEST}, "Find the email you asked about"
        )
        result = listing.result if isinstance(listing.result, dict) else {}
        found = [m for m in result.get("messages") or () if isinstance(m, dict)]
        uids = [m["uid"] for m in found if isinstance(m.get("uid"), int) and m["uid"] > 0]
        if listing.status != "done" or not uids:
            return None
        uid = uids[0] if len(uids) == 1 else self._choose(text, listing, uids)
        if uid is None:
            return None
        arguments: dict[str, Any] = {"uid": uid}
        if isinstance(result.get("account"), str):
            arguments["account"] = result["account"]
        read = self._call(MAIL_TOOL, "read_message", arguments, "Read the email you asked about")
        if read.status != "done" or not isinstance(read.result, dict):
            return None
        return read

    def _choose(self, text: str, listing: ToolOutcome, uids: list[int]) -> int | None:
        messages: list[Message] = [
            {"role": "system", "content": CHOOSE_PROMPT},
            {
                "role": "user",
                "content": f"The user wrote: {text}\n\nTheir latest emails:\n{listing.for_model}",
            },
        ]
        answer = self._json(messages, {**CHOOSE_SCHEMA, "properties": {"uid": {"enum": uids}}})
        uid = answer.get("uid") if answer else None
        return uid if isinstance(uid, int) and uid in uids else None

    def _facts(self, text: str, read: ToolOutcome) -> Facts | None:
        """Step 3: the meeting and the answer, from the email."""
        email = read.result if isinstance(read.result, dict) else {}
        sent = _sent(email.get("date")) or self.today
        letter = read.for_model
        if len(letter) > MAX_EMAIL_CHARS + 1000:
            letter = letter[: MAX_EMAIL_CHARS + 1000]
        messages: list[Message] = [
            {
                "role": "system",
                "content": f"{EXTRACT_PROMPT}\n\nToday is {_day(self.today)}. "
                f"The email was sent on {_day(sent)}.",
            },
            {"role": "user", "content": f"The user asked: {text}\n\nThe email:\n{letter}"},
        ]
        answer = self._json(messages, EXTRACT_SCHEMA)
        if answer is None:
            return None
        return _checked(answer, fallback_title=str(email.get("subject") or ""))

    def _event(self, facts: Facts) -> ToolOutcome:
        arguments = {"title": facts.title, "start": facts.start, "end": facts.end}
        if facts.location:
            arguments["location"] = facts.location
        return self._call(
            CALENDAR_TOOL, "create_event", arguments, "Add the meeting from the email"
        )

    def _draft(self, email: dict[str, Any], reply: str) -> ToolOutcome | None:
        to = _reply_address(email)
        account = email.get("account")
        if not account:
            got = getaddresses([str(email.get("to") or "")])
            account = got[0][1] if got else ""
        if not to or not isinstance(account, str) or not account:
            return None
        subject = str(email.get("subject") or "").strip()
        if not re.match(r"(?i)re\s*:", subject):
            subject = f"Re: {subject}".strip()
        arguments: dict[str, Any] = {
            "account": account,
            "to": [to],
            "subject": subject,
            "text": reply,
        }
        if isinstance(email.get("message_id"), str) and email["message_id"]:
            arguments["in_reply_to"] = email["message_id"]
        return self._call(MAIL_TOOL, "save_draft", arguments, "Save the answer as a draft")

    def _json(self, messages: list[Message], schema: dict) -> dict | None:
        try:
            text = self.complete(messages, schema)
        except ModelUnavailable:
            raise  # said like any other message the model could not answer
        except ModelError:
            return None
        try:
            value = json.loads(text.strip().removeprefix("```json").removesuffix("```"))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None


def _checked(answer: dict[str, Any], *, fallback_title: str) -> Facts:
    def text(key: str, limit: int) -> str:
        value = answer.get(key)
        return value.strip()[:limit] if isinstance(value, str) else ""

    title = text("title", MAX_TITLE) or fallback_title[:MAX_TITLE]
    day, start_time, end_time = text("date", 10), text("start_time", 5), text("end_time", 5)
    day = _fixed_day(day, text("weekday", 10), answer.get("day_of_month"))
    start = end = ""
    if _DAY.match(day) and _TIME.match(start_time):
        try:
            begins = datetime.fromisoformat(f"{day}T{start_time}")
        except ValueError:
            begins = None
        if begins is not None:
            ends = begins + timedelta(hours=EVENT_HOURS)
            if _TIME.match(end_time):
                later = datetime.fromisoformat(f"{day}T{end_time}")
                if later > begins:
                    ends = later
            start, end = begins.strftime("%Y-%m-%dT%H:%M"), ends.strftime("%Y-%m-%dT%H:%M")
    return Facts(title, start, end, text("location", MAX_TITLE), text("reply", MAX_REPLY))


# How far from the model's date the day it named may be: a week each way.
MAX_SHIFT = 7


def _fixed_day(day: str, weekday: str, day_of_month: Any) -> str:
    """The model's date, moved to the nearest day that is the weekday and the
    day of the month it also gave: "Friday", 27 and "2026-11-25" make
    2026-11-27. Unchanged when they do not fit any day near it."""
    if not _DAY.match(day):
        return day
    try:
        given = date.fromisoformat(day)
    except ValueError:
        return day
    wanted = _WEEKDAYS.index(weekday) if weekday in _WEEKDAYS else None
    number = day_of_month if isinstance(day_of_month, int) and 1 <= day_of_month <= 31 else None
    if wanted is None and number is None:
        return day

    def fits(d: date) -> bool:
        return (wanted is None or d.weekday() == wanted) and (number is None or d.day == number)

    for shift in range(MAX_SHIFT + 1):
        for d in (given + timedelta(days=shift), given - timedelta(days=shift)):
            if fits(d):
                return d.isoformat()
    return day


def _reply_address(email: dict[str, Any]) -> str:
    """Where an answer goes: Reply-To when the email has one, else its sender."""
    for key in ("reply_to", "from"):
        got = [a for _, a in getaddresses([str(email.get(key) or "")]) if "@" in a]
        if got:
            return got[0]
    return ""


def _sent(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError, IndexError):
        return None


def _day(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]}, {day.isoformat()}"


def _when(start: str) -> str:
    """27.11.2026, 15:00"""
    begins = datetime.fromisoformat(start)
    return f"{begins.day:02d}.{begins.month:02d}.{begins.year}, {begins:%H:%M}"
