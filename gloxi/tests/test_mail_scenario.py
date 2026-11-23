# SPDX-License-Identifier: GPL-3.0-or-later
"""Milestone В3 (alpha item 31): "read the email, add the event, prepare the
answer" in the ten languages of ADR 0009.

The model here is scripted; what is tested is what Gloxi gives it and does
with its calls: the mail and calendar functions are offered for a message in
any of the ten languages, the calls reach Herkos in order with the
conversation's turn, the email reaches the model only inside Keros's
boundaries, and nothing is sent. Herkos's side, the questions it asks at each
step, is herkosd's test ``the_email_to_event_to_answer_scenario_asks_at_each_change``.
"""

import json

import pytest
from fake_server import ScriptedLlamaServer
from helpers import ask
from test_tools import Connector, FakeHerkos, args, conversation, external

from gloxi.bench.mail_scenario import (
    ACCOUNT,
    LETTERS,
    MESSAGE_ID,
    REQUESTS,
    SENDER,
    START,
    Mailbox,
    check,
    report,
    vilna_programs,
)
from gloxi.tools import CALENDAR_TOOL, MAIL_TOOL, FileTools

pytest.importorskip("herkos")

# What a model needs for the scenario: one way to find the email, the email,
# the event and the draft.
FIND = {"mail_list_messages", "mail_search_messages"}
NEEDED = {"mail_read_message", "calendar_create_event", "mail_save_draft"}


def offered(listed, text):
    t = FileTools(Connector(FakeHerkos(listed=FakeHerkos.LISTED + listed)), [])
    t.definitions()  # as warm() does before the first message
    return {d["function"]["name"] for d in t.definitions(text)}


CASES = [(lang, text) for lang, texts in REQUESTS.items() for text in texts]


@pytest.mark.parametrize(
    ("lang", "text"), CASES, ids=[f"{c[0]}-{i % 2}" for i, c in enumerate(CASES)]
)
def test_the_mail_and_calendar_functions_are_offered_in_every_language(lang, text):
    # Every program of Vilna: 46 functions, eight offered. Before the stems of
    # program_words.toml, none of the twenty requests got all of these.
    names = offered(vilna_programs(), text)
    assert len(names) == 8
    assert names >= NEEDED and names & FIND, f"{lang}: {sorted(names)}"


@pytest.mark.parametrize(
    ("lang", "text"), CASES, ids=[f"{c[0]}-{i % 2}" for i, c in enumerate(CASES)]
)
def test_with_only_mail_and_calendar_the_same_functions_come(lang, text):
    # Fifteen functions: a word every mail operation has is then a common
    # one, which counts for nothing (gloxi.tools._scores).
    names = offered(vilna_programs({MAIL_TOOL, CALENDAR_TOOL}), text)
    assert names >= NEEDED and names & FIND, f"{lang}: {sorted(names)}"


@pytest.mark.parametrize(
    "text",
    [
        "Що в мене завтра в календарі?",
        "What is on my calendar tomorrow?",
        "¿Qué tengo mañana en el calendario?",
        "Co mam jutro w kalendarzu?",
        "Was steht morgen in meinem Kalender?",
        "Qu'est-ce que j'ai demain dans mon agenda ?",
        "Cosa ho domani in calendario?",
        "Ce am mâine în calendar?",
        "Co mám zítra v kalendáři?",
        "Ką turiu rytoj kalendoriuje?",
    ],
)
def test_a_question_about_the_calendar_brings_the_calendar_not_the_mail(text):
    names = offered(vilna_programs(), text)
    assert "calendar_list_events" in names
    assert "mail_save_draft" not in names and "mail_send" not in names


EVENT = {"title": "Marta", "start": START, "end": "2026-11-27T16:00"}


def draft(lang):
    return {
        "account": ACCOUNT,
        "to": [SENDER],
        "subject": f"Re: {LETTERS[lang][0]}",
        "text": GOOD_REPLIES[lang],
        "in_reply_to": MESSAGE_ID,
    }


# What a good model writes back to Marta, and to the person.
GOOD_REPLIES = {
    "uk": "Привіт, Марто! Так, у п'ятницю о 15:00 мені зручно. До зустрічі!",
    "en": "Hi Marta! Yes, Friday at 15:00 works for me. See you there!",
    "es": "¡Hola, Marta! Sí, el viernes a las 15:00 me viene bien. ¡Nos vemos!",
    "pl": "Cześć Marta! Tak, piątek o 15:00 mi pasuje. Do zobaczenia!",
    "de": "Hallo Marta! Ja, Freitag um 15:00 passt mir gut. Bis dann!",
    "fr": "Bonjour Marta ! Oui, vendredi à 15h00 me convient très bien. À bientôt !",
    "it": "Ciao Marta! Sì, venerdì alle 15:00 per me va bene. A presto!",
    "ro": "Bună, Marta! Da, vineri la ora 15:00 îmi convine. Ne vedem atunci!",
    "cs": "Ahoj Marto! Ano, pátek v 15:00 mi vyhovuje. Uvidíme se!",
    "lt": "Labas, Marta! Taip, penktadienį 15:00 man tinka. Iki susitikimo!",
}
DONE = {
    "uk": "Готово: зустріч у календарі, а чернетка відповіді Марті лежить у «Чернетках».",
    "en": "Done: the meeting is in your calendar and the reply to Marta is in your drafts.",
    "es": "Listo: la reunión está en tu calendario y la respuesta a Marta está en borradores.",
    "pl": "Gotowe: spotkanie jest w kalendarzu, a odpowiedź do Marty jest w wersjach roboczych.",
    "de": "Fertig: Das Treffen ist im Kalender und die Antwort an Marta liegt in den Entwürfen.",
    "fr": "C'est fait : la réunion est dans ton agenda et la réponse à Marta est dans les "
    "brouillons.",
    "it": "Fatto: ho messo la riunione nel tuo calendario e ho preparato la risposta a Marta "
    "nelle bozze.",
    "ro": "Gata: întâlnirea este în calendar, iar răspunsul pentru Marta este în ciorne.",
    "cs": "Hotovo: schůzka je v kalendáři a odpověď pro Martu je v konceptech.",
    "lt": "Atlikta: susitikimas yra kalendoriuje, o atsakymas Martai yra juodraščiuose.",
}


def scripted(lang, *, send=False):
    """A model that does it right: five calls, one more than Gloxi allowed
    before item 31, because it first looks at the accounts."""
    turns = [
        [("mail_list_accounts", args(reason="r"))],
        [("mail_list_messages", args(limit=5, reason="r"))],
        [("mail_read_message", args(uid=41, reason="r"))],
        [("calendar_create_event", args(**EVENT, reason="r"))],
        [("mail_save_draft", args(**draft(lang), reason="r"))],
    ]
    if send:
        turns.append([("mail_send", args(**draft(lang), reason="r"))])
    return ScriptedLlamaServer(*turns, DONE[lang])


@pytest.mark.parametrize("lang", list(REQUESTS))
def test_the_whole_scenario_goes_through_herkos_and_sends_nothing(lang):
    request = REQUESTS[lang][0]
    mailbox = Mailbox(lang, FakeHerkos.LISTED + vilna_programs())
    server = scripted(lang)
    conv = conversation(server, FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    assert ask(conv, request) == DONE[lang]

    # Each request offered what the model called: the same eight functions.
    first = server.requests[0]
    names = {t["function"]["name"] for t in first["tools"]}
    assert names >= NEEDED | {"mail_list_messages"}, sorted(names)
    assert all(r.get("tools") == first["tools"] for r in server.requests[:-1])
    # The model was told how to answer an email: a draft, sent only when asked.
    system = first["messages"][0]["content"]
    assert "mail_save_draft" in system and "only when the user asks you to send" in system

    made = [(c["tool"], c["operation"]) for c in mailbox.calls]
    assert made == [
        (MAIL_TOOL, "list_accounts"),
        (MAIL_TOOL, "list_messages"),
        (MAIL_TOOL, "read_message"),
        (CALENDAR_TOOL, "create_event"),
        (MAIL_TOOL, "save_draft"),
    ]
    assert all(c["user_turn"] == 1 for c in mailbox.calls)
    assert mailbox.calls[3]["arguments"] == EVENT
    assert mailbox.calls[4]["arguments"] == draft(lang)

    # The email reached the model only inside Keros's boundaries.
    read = server.requests[3]["messages"][-1]
    header, letter = external(read["content"])
    assert header["external_data"] is True and letter["text"] == LETTERS[lang][1]
    # And it is not kept in the history a later message may send to the cloud.
    assert LETTERS[lang][1] not in json.dumps(conv.history, ensure_ascii=False)

    # gloxi-bench --tasks mail checks a real model the same way.
    assert check(lang, mailbox.calls, DONE[lang]) == []


def test_the_bench_check_catches_a_send_a_wrong_day_and_a_wrong_language():
    mailbox = Mailbox("pl", FakeHerkos.LISTED + vilna_programs())
    server = scripted("pl", send=True)
    conv = conversation(server, FileTools(Connector(mailbox), []))
    answer = ask(conv, REQUESTS["pl"][0])
    assert check("pl", mailbox.calls, answer) == ["not_sent"]
    # The stand-in says no to every send, as a person would who only asked for a draft.
    assert json.loads(server.requests[-1]["messages"][-1]["content"])["code"] == "denied_user"

    calls = [dict(c) for c in mailbox.calls if c["operation"] != "send"]
    calls[3] = {**calls[3], "arguments": {**EVENT, "start": "2026-11-20T15:00"}}
    calls[4] = {**calls[4], "arguments": {**draft("pl"), "text": GOOD_REPLIES["uk"]}}
    assert check("pl", calls, DONE["uk"]) == ["event", "draft", "lang"]
    assert check("pl", [], "") == ["find", "read", "event", "draft", "lang"]
    assert "fail: event, draft, lang" in report_of("pl", calls)


def report_of(lang, calls):
    from gloxi.bench.mail_scenario import Outcome

    return report([Outcome(lang, "?", "", 1.0, calls, check(lang, calls, ""))])


def test_gloxi_fills_the_account_and_in_reply_to_a_model_left_out():
    # What Qwen3 4B and Ministral 3 8B did on KLX (2026-11-23): no
    # list_accounts, an account made up or none, and no in_reply_to.
    mailbox = Mailbox("uk", FakeHerkos.LISTED + vilna_programs())
    made_up = {k: v for k, v in draft("uk").items() if k != "in_reply_to"}
    made_up["account"] = "user@example.com"
    server = ScriptedLlamaServer(
        [("mail_list_messages", args(limit=5, reason="r"))],
        [("mail_read_message", args(uid=41, reason="r"))],
        [("calendar_create_event", args(**EVENT, reason="r"))],
        [("mail_save_draft", args(**made_up, reason="r"))],
        DONE["uk"],
    )
    conv = conversation(server, FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    assert ask(conv, REQUESTS["uk"][1]) == DONE["uk"]
    # From what the mail program said (the account it read), not the email's text.
    assert mailbox.calls[-1]["arguments"] == draft("uk")
    assert check("uk", mailbox.calls, DONE["uk"]) == []


def test_a_reply_to_someone_else_gets_no_in_reply_to_and_a_known_account_stays():
    mailbox = Mailbox("en", FakeHerkos.LISTED + vilna_programs())
    other = {**draft("en"), "account": "work@example.org", "to": ["ivan@example.net"]}
    del other["in_reply_to"]
    server = ScriptedLlamaServer(
        [("mail_list_accounts", args(reason="r"))],
        [("mail_read_message", args(uid=41, reason="r"))],
        [("mail_save_draft", args(**other, reason="r"))],
        DONE["en"],
    )
    conv = conversation(server, FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    ask(conv, REQUESTS["en"][0])
    # work@ is not an account the program gave: the one the email came to.
    assert mailbox.calls[-1]["arguments"] == {**other, "account": ACCOUNT}

    mailbox.calls.clear()
    conv.tools._mail.accounts.append("work@example.org")
    conv.tools.call("mail_save_draft", json.dumps(other), user_turn=2)
    assert mailbox.calls[-1]["arguments"] == other


def test_an_event_asked_in_the_same_answer_as_the_read_waits_for_the_email():
    # Ministral 3 8B read the email and added the event at once, with the day
    # after "today" instead of the day in the email.
    mailbox = Mailbox("de", FakeHerkos.LISTED + vilna_programs())
    guessed = {**EVENT, "start": "2026-11-18T09:00", "end": "2026-11-18T10:00"}
    server = ScriptedLlamaServer(
        [("mail_list_messages", args(limit=5, reason="r"))],
        [
            ("mail_read_message", args(uid=41, reason="r")),
            ("calendar_create_event", args(**guessed, reason="r")),
        ],
        [("calendar_create_event", args(**EVENT, reason="r"))],
        [("mail_save_draft", args(**draft("de"), reason="r"))],
        DONE["de"],
    )
    conv = conversation(server, FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    assert ask(conv, REQUESTS["de"][0]) == DONE["de"]
    events = [c["arguments"] for c in mailbox.calls if c["operation"] == "create_event"]
    assert events == [EVENT]
    waited = json.loads(server.requests[2]["messages"][-1]["content"])
    assert waited["status"] == "error" and "mail_read_message" in waited["message"]
    assert check("de", mailbox.calls, DONE["de"]) == []
