# SPDX-License-Identifier: GPL-3.0-or-later
"""Milestone В3 with a real model: "read the email, add the event, prepare
the answer" in the ten languages of ADR 0009 (alpha item 31).

``gloxi-bench --tasks mail`` runs Gloxi's own conversation loop
(``gloxi.chat.Conversation`` and ``gloxi.tools.FileTools``) against a running
local model. In place of Herkos stands ``Mailbox``: it lists Vilna's
programs as the system Herkos does, answers as the mail and calendar servers
would for one made-up email, says no to every send, and records each call.
Nothing reaches a real mailbox or calendar.

For each language the person asks once, and the checks are:

- ``find``: the model looked for the email (list or search);
- ``read``: it read it;
- ``event``: it added the event on the day and at the time the email names;
- ``draft``: it saved a draft to the sender, as an answer to that email,
  in the language of the email;
- ``not_sent``: it did not try to send;
- ``lang``: its last words to the person are in the person's language.

All names, addresses and dates are made up.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from gloxi.bench import LANGUAGES, _lang_ok, guess_language

MAIL = "org.vilna.mcp.mail"
CALENDAR = "org.vilna.mcp.calendar"
TODAY = date(2026, 11, 17)  # a Tuesday
START = "2026-11-27T15:00"  # what the email asks for
ACCOUNT = "ola@example.org"
SENDER = "marta@example.com"
MESSAGE_ID = "<m41@example.com>"

# What the person asks, as people write it; the second line of each language
# says it another way.
REQUESTS: dict[str, list[str]] = {
    "uk": [
        "Прочитай останній лист від Марти, додай зустріч у календар і підготуй відповідь.",
        "Марта написала мені про зустріч. Запиши її в календар і напиши їй відповідь.",
    ],
    "en": [
        "Read the latest email from Marta, add the meeting to my calendar and prepare a reply.",
        "Marta wrote to me about a meeting: put it in my calendar and draft an answer.",
    ],
    "es": [
        "Lee el último correo de Marta, añade la reunión al calendario y prepara una respuesta.",
        "Marta me escribió sobre una reunión: apúntala en mi agenda y redacta una respuesta.",
    ],
    "pl": [
        "Przeczytaj ostatni mail od Marty, dodaj spotkanie do kalendarza i przygotuj odpowiedź.",
        "Marta napisała do mnie wiadomość o spotkaniu. Zapisz je w kalendarzu i odpisz jej.",
    ],
    "de": [
        "Lies die letzte Mail von Marta, trag das Treffen in meinen Kalender ein und bereite "
        "eine Antwort vor.",
        "Marta hat mir wegen eines Termins geschrieben. Erstelle ihn im Kalender und schreib "
        "eine Antwort.",
    ],
    "fr": [
        "Lis le dernier mail de Marta, ajoute la réunion à mon agenda et prépare une réponse.",
        "Marta m'a écrit un courriel pour un rendez-vous : note-le dans mon calendrier et "
        "rédige une réponse.",
    ],
    "it": [
        "Leggi l'ultima mail di Marta, aggiungi la riunione al calendario e prepara una risposta.",
        "Marta mi ha scritto un messaggio per un appuntamento: segnalo in agenda e scrivi una "
        "risposta.",
    ],
    "ro": [
        "Citește ultimul e-mail de la Marta, adaugă întâlnirea în calendar și pregătește un "
        "răspuns.",
        "Marta mi-a trimis un mesaj despre o întâlnire. Creeaza evenimentul in calendar si "
        "scrie-i un raspuns.",
    ],
    "cs": [
        "Přečti poslední e-mail od Marty, přidej schůzku do kalendáře a připrav odpověď.",
        "Marta mi psala kvůli schůzce. Zapiš ji do kalendáře a napiš jí odpověď.",
    ],
    "lt": [
        "Perskaityk paskutinį Martos laišką, įrašyk susitikimą į kalendorių ir paruošk atsakymą.",
        "Marta parašė man laišką dėl susitikimo. Pridėk jį į kalendorių ir parašyk atsakymą.",
    ],
    "ca": [
        "Llegeix l'últim correu de la Marta, afegeix la reunió al calendari i prepara una "
        "resposta.",
        "La Marta m'ha escrit sobre una reunió: apunta-la a la meva agenda i redacta una resposta.",
    ],
    "ca@valencia": [
        "Llig l'últim correu de Marta, afig la reunió al calendari i prepara una resposta.",
        "Marta m'ha escrit sobre una reunió: apunta-la en la meua agenda i redacta una resposta.",
    ],
    "gl": [
        "Le o último correo de Marta, engade a reunión ao calendario e prepara unha resposta.",
        "Marta escribiume sobre unha reunión: apúntaa na miña axenda e redacta unha resposta.",
    ],
    "eu": [
        "Irakurri Martaren azken mezua, gehitu bilera egutegian eta prestatu erantzun bat.",
        "Martak bilera bati buruz idatzi dit: jarri egutegian eta idatzi erantzun bat.",
    ],
    "pt": [
        "Leia o último e-mail da Marta, adicione a reunião ao calendário e prepare uma resposta.",
        "A Marta escreveu-me sobre uma reunião: marque-a na minha agenda e redija uma resposta.",
    ],
}

# Marta's email in the same language: Friday, 27 November 2026, 15:00, an hour.
LETTERS: dict[str, tuple[str, str]] = {
    "uk": (
        "Зустріч у п'ятницю",
        "Привіт! Чи можемо ми зустрітися в п'ятницю, 27 листопада, о 15:00 у кав'ярні "
        "«Люмен»? Це займе десь годину. Напиши, чи тобі зручно. Марта",
    ),
    "en": (
        "Meeting on Friday",
        "Hi! Can we meet on Friday, 27 November, at 15:00 at Café Lumen? It will take "
        "about an hour. Let me know if that works for you. Marta",
    ),
    "es": (
        "Reunión el viernes",
        "¡Hola! ¿Podemos vernos el viernes 27 de noviembre a las 15:00 en el Café Lumen? "
        "Será cerca de una hora. Dime si te viene bien. Marta",
    ),
    "pl": (
        "Spotkanie w piątek",
        "Cześć! Czy możemy się spotkać w piątek 27 listopada o 15:00 w kawiarni Lumen? "
        "To zajmie około godziny. Daj znać, czy ci pasuje. Marta",
    ),
    "de": (
        "Treffen am Freitag",
        "Hallo! Können wir uns am Freitag, dem 27. November, um 15:00 im Café Lumen "
        "treffen? Es dauert etwa eine Stunde. Sag mir, ob es dir passt. Marta",
    ),
    "fr": (
        "Réunion vendredi",
        "Bonjour ! Pouvons-nous nous voir vendredi 27 novembre à 15h00 au Café Lumen ? "
        "Cela prendra environ une heure. Dis-moi si cela te convient. Marta",
    ),
    "it": (
        "Riunione venerdì",
        "Ciao! Possiamo vederci venerdì 27 novembre alle 15:00 al Café Lumen? Ci vorrà "
        "circa un'ora. Fammi sapere se ti va bene. Marta",
    ),
    "ro": (
        "Întâlnire vineri",
        "Bună! Ne putem vedea vineri, 27 noiembrie, la ora 15:00 la Café Lumen? Va dura "
        "cam o oră. Spune-mi dacă îți convine. Marta",
    ),
    "cs": (
        "Schůzka v pátek",
        "Ahoj! Můžeme se sejít v pátek 27. listopadu v 15:00 v kavárně Lumen? Zabere to "
        "asi hodinu. Dej mi vědět, jestli ti to vyhovuje. Marta",
    ),
    "lt": (
        "Susitikimas penktadienį",
        "Labas! Ar galime susitikti penktadienį, lapkričio 27 d., 15:00 kavinėje „Lumen“? "
        "Tai užtruks apie valandą. Parašyk, ar tau tinka. Marta",
    ),
    "ca": (
        "Reunió divendres",
        "Hola! Ens podem veure divendres 27 de novembre a les 15:00 al Café Lumen? Serà "
        "cosa d'una hora. Digues-me si et va bé. Marta",
    ),
    "ca@valencia": (
        "Reunió divendres",
        "Hola! Ens podem vore divendres 27 de novembre a les 15:00 en el Café Lumen? Serà "
        "cosa d'una hora. Digues-me si et va bé. Marta",
    ),
    "gl": (
        "Reunión o venres",
        "Ola! Podemos vernos o venres 27 de novembro ás 15:00 no Café Lumen? Será arredor "
        "dunha hora. Dime se che vai ben. Marta",
    ),
    "eu": (
        "Bilera ostiralean",
        "Kaixo! Ostiralean, azaroaren 27an, 15:00etan elkar ikus dezakegu Café Lumenen? "
        "Ordubete inguru izango da. Esan iezadazu ondo datorkizun. Marta",
    ),
    "pt": (
        "Reunião na sexta-feira",
        "Olá! Podemos encontrar-nos na sexta-feira, 27 de novembro, às 15:00 no Café Lumen? "
        "Deve demorar cerca de uma hora. Diz-me se te dá jeito. Marta",
    ),
}

# The parameters the model sees, as the mail and calendar servers give them
# (mcp/mail, mcp/calendar), cut to what the scenario uses.
_ACCOUNT = {"type": "string", "description": "The account's address from list_accounts"}
_DATE = {"type": "string", "description": "ISO 8601 date, e.g. 2027-03-05"}
_ADDRESSES = {"type": "array", "items": {"type": "string"}}
_MESSAGE = {
    "type": "object",
    "properties": {
        "account": _ACCOUNT,
        "to": _ADDRESSES,
        "cc": _ADDRESSES,
        "subject": {"type": "string"},
        "text": {"type": "string"},
        "in_reply_to": {
            "type": "string",
            "description": "message_id of the message this answers (from read_message)",
        },
    },
    "required": ["account", "to", "subject", "text"],
}
_EVENT_TIME = {
    "type": "string",
    "description": "ISO 8601 date-time such as 2027-03-05T14:00, or a date for all day",
}
SCHEMAS: dict[tuple[str, str], dict[str, Any]] = {
    (MAIL, "list_accounts"): {"type": "object", "properties": {}},
    (MAIL, "list_folders"): {"type": "object", "properties": {"account": _ACCOUNT}},
    (MAIL, "list_messages"): {
        "type": "object",
        "properties": {
            "account": _ACCOUNT,
            "folder": {"type": "string"},
            "unread_only": {"type": "boolean"},
            "limit": {"type": "integer"},
        },
    },
    (MAIL, "search_messages"): {
        "type": "object",
        "properties": {
            "account": _ACCOUNT,
            "query": {"type": "string"},
            "from": {"type": "string", "description": "Part of the sender's name or address"},
            "since": _DATE,
            "until": _DATE,
        },
    },
    (MAIL, "read_message"): {
        "type": "object",
        "properties": {"uid": {"type": "integer"}, "account": _ACCOUNT},
        "required": ["uid"],
    },
    (MAIL, "save_draft"): _MESSAGE,
    (MAIL, "send"): _MESSAGE,
    (CALENDAR, "list_events"): {
        "type": "object",
        "properties": {
            "period": {"type": "string", "enum": ["today", "tomorrow", "this_week", "next_week"]},
            "start": _DATE,
            "end": _DATE,
        },
    },
    (CALENDAR, "search_events"): {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    (CALENDAR, "create_event"): {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "start": _EVENT_TIME,
            "end": _EVENT_TIME,
            "location": {"type": "string"},
            "description": {"type": "string"},
        },
        "required": ["title", "start"],
    },
}
EMPTY = {"type": "object", "properties": {}}
# Built in, never offered as program functions (gloxi.tools.NOT_GENERIC).
BUILT_IN = {"org.vilna.mcp.files", "org.vilna.mcp.backup", "org.vilna.gloxi.network"}


def vilna_programs(only: set[str] | None = None) -> list[dict[str, Any]]:
    """What the system Herkos lists: Vilna's programs from their real
    manifests (the ``tools`` extra), or only the tools in ``only``."""
    import tomllib

    import herkos

    listed = []
    for path in sorted((Path(herkos.__file__).parent / "manifests").glob("*.toml")):
        m = tomllib.loads(path.read_text(encoding="utf-8"))
        if m["tool"] in BUILT_IN or (only and m["tool"] not in only):
            continue
        operations = [
            {
                "name": name,
                "summary": op.get("summary", name),
                "level": op.get("level"),
                "returns_external": op.get("returns_external", False),
                "input_schema": SCHEMAS.get((m["tool"], name), EMPTY),
            }
            for name, op in m.get("operations", {}).items()
        ]
        tool = {"tool": m["tool"], "description": m.get("description", "")}
        listed.append({**tool, "operations": operations})
    return listed


# Two other emails in the inbox: a newsletter and an old receipt.
OTHERS = (
    {"uid": 42, "from": "Lumen Weekly <news@lumen.example>", "subject": "Lumen Weekly #47"},
    {"uid": 40, "from": "Shop <orders@shop.example>", "subject": "Order 5531"},
)


class Mailbox:
    """Stands in for Herkos (``gloxi.tools.HerkosLike``): one mailbox with
    Marta's email, a calendar, and a person who says no to every send."""

    def __init__(self, lang: str, listed: list[dict[str, Any]]) -> None:
        self.lang = lang
        self.listed = listed
        self.calls: list[dict[str, Any]] = []
        # For tests: a Reply-To on Marta's email, and operations the person refuses.
        self.reply_to = ""
        self.refuse: set[str] = set()
        self.refuse_code = "denied_user"  # or denied_timeout, denied_error

    def tools(self) -> list[dict[str, Any]]:
        return self.listed

    def close(self) -> None:
        pass

    def request_action(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        subject, text = LETTERS[self.lang]
        header = {"uid": 41, "from": f"Marta <{SENDER}>", "subject": subject}
        op = kwargs.get("operation")
        results: dict[str, Any] = {
            "list_accounts": {"accounts": [{"address": ACCOUNT, "name": "Ola"}]},
            "list_folders": {"folders": [{"name": "INBOX", "unread": 1}]},
            # As vilna_mcp_mail: newest first, with the account. Marta's is
            # not the newest, so the model has to choose (gloxi.mail_flow).
            "list_messages": {
                "account": ACCOUNT,
                "messages": [
                    {**OTHERS[0], "date": "2026-11-16", "unread": True},
                    {**header, "date": "2026-11-16", "unread": True},
                    {**OTHERS[1], "date": "2026-11-15", "unread": False},
                ],
            },
            "search_messages": {"messages": [{**header, "date": "2026-11-16"}]},
            "read_message": {
                **header,
                "to": [ACCOUNT],
                # As vilna_mcp_mail.mail.Mail.read_message: the account it read.
                "account": ACCOUNT,
                "date": "2026-11-16T09:12:00+01:00",
                "message_id": MESSAGE_ID,
                "text": text,
                "attachments": [],
            },
            "list_events": {"events": []},
            "search_events": {"events": []},
            "create_event": {"id": "e7", "calendar": "personal"},
            "save_draft": {"folder": "Drafts", "message_id": "<d1.gloxi@example.org>"},
        }
        if op in self.refuse:
            return {"decision": "denied", "code": self.refuse_code, "message": "not done"}
        if op == "send":
            return {
                "decision": "denied",
                "code": "denied_user",
                "message": "the person said no",
            }
        if self.reply_to:
            results["read_message"]["reply_to"] = self.reply_to
        if op not in results:
            return {"decision": "denied", "code": "tool_error", "message": f"no {op} here"}
        external = op in ("list_messages", "search_messages", "read_message")
        answer: dict[str, Any] = {
            "decision": "allowed",
            "code": "allowed",
            "message": "allowed",
            "result": results[op],
        }
        if external:
            answer["keros"] = {"source": f"{kwargs.get('tool')}.{op}", "findings": []}
        return answer


@dataclass
class Outcome:
    lang: str
    request: str
    answer: str
    seconds: float
    calls: list[dict[str, Any]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return not self.failed and self.error is None


def check(lang: str, calls: list[dict[str, Any]], answer: str) -> list[str]:
    """The checks of the module docstring that failed."""
    ops = [(c.get("tool"), c.get("operation")) for c in calls]

    def args_of(tool: str, op: str) -> list[dict[str, Any]]:
        return [
            c.get("arguments") or {}
            for c in calls
            if (c.get("tool"), c.get("operation")) == (tool, op)
        ]

    failed = []
    if (MAIL, "list_messages") not in ops and (MAIL, "search_messages") not in ops:
        failed.append("find")
    if not any(a.get("uid") == 41 for a in args_of(MAIL, "read_message")):
        failed.append("read")
    if not any(
        str(a.get("start", "")).startswith(START) for a in args_of(CALENDAR, "create_event")
    ):
        failed.append("event")
    drafts = args_of(MAIL, "save_draft")
    if not any(_good_draft(lang, d) for d in drafts):
        failed.append("draft")
    if (MAIL, "send") in ops:
        failed.append("not_sent")
    if not _lang_ok(answer, lang):
        failed.append("lang")
    return failed


def _good_draft(lang: str, draft: dict[str, Any]) -> bool:
    to = draft.get("to")
    to = to if isinstance(to, list) else [to]
    text = str(draft.get("text") or "")
    return (
        any(SENDER in str(t) for t in to)
        and draft.get("in_reply_to") == MESSAGE_ID
        and draft.get("account") == ACCOUNT
        and (_lang_ok(text, lang) or (lang != "uk" and guess_language(text) is None))
    )


def run_one(client: Any, lang: str, request: str, listed: list[dict[str, Any]]) -> Outcome:
    """One request through Gloxi's own conversation loop."""
    from gloxi.chat import Conversation
    from gloxi.config import DEFAULT_SYSTEM_PROMPT, RouterPolicy
    from gloxi.router import Router
    from gloxi.tools import FileTools

    mailbox = Mailbox(lang, listed)
    tools = FileTools(lambda: mailbox, [])
    conv = Conversation(
        Router(client, None, RouterPolicy()),
        DEFAULT_SYSTEM_PROMPT,
        tools=tools,
        today=lambda: TODAY,
    )
    tools.definitions()  # as warm() does before the first message
    started = time.monotonic()
    try:
        answer = "".join(conv.ask(request, conv.route(request)))
    except Exception as exc:  # report it and go on with the next language
        took = round(time.monotonic() - started, 1)
        return Outcome(lang, request, "", took, mailbox.calls, ["error"], repr(exc))
    took = round(time.monotonic() - started, 1)
    return Outcome(lang, request, answer, took, mailbox.calls, check(lang, mailbox.calls, answer))


def run(client: Any, langs: list[str] | None = None, *, both: bool = False) -> list[Outcome]:
    listed = vilna_programs()
    out = []
    for lang in langs or list(LANGUAGES):
        for request in REQUESTS[lang][: 2 if both else 1]:
            r = run_one(client, lang, request, listed)
            mark = "ok" if r.passed else "FAIL " + ",".join(r.failed)
            print(f"mail {lang}: {mark} ({r.seconds} s)", file=sys.stderr)
            out.append(r)
    return out


def report(results: list[Outcome]) -> str:
    """A Markdown table, then each language's calls and answer."""
    passed = sum(r.passed for r in results)
    lines = [
        f"# Mail scenario: {passed} of {len(results)}",
        "",
        "| Language | Result | Seconds | Calls |",
        "| --- | --- | --- | --- |",
    ]
    for r in results:
        result = "ok" if r.passed else "fail: " + ", ".join(r.failed)
        calls = ", ".join(str(c.get("operation")) for c in r.calls)
        lines.append(f"| {r.lang} | {result} | {r.seconds} | {calls} |")
    for r in results:
        lines += ["", f"## {r.lang}", "", f"> {r.request}", ""]
        for c in r.calls:
            arguments = json.dumps(c.get("arguments"), ensure_ascii=False)
            lines.append(f"- `{c.get('operation')}` {arguments}")
        if r.error:
            lines.append(f"- error: {r.error}")
        lines += ["", r.answer.strip() or "(no answer)"]
    return "\n".join(lines) + "\n"


def main(client: Any, out_dir: Path, model: str, only: str | None, both: bool) -> int:
    langs = [lang for lang in LANGUAGES if not only or lang in only.split(",")]
    results = run(client, langs, both=both)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M")
    path = out_dir / f"{model}-mail-{stamp}.md"
    path.write_text(report(results), encoding="utf-8")
    passed = sum(r.passed for r in results)
    print(f"mail scenario: {passed} of {len(results)}; report: {path}")
    return 0 if passed == len(results) else 1
