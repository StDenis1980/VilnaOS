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

from gloxi import mail_flow
from gloxi.bench import _lang_ok
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


def mail_scenario_subject(lang):
    return LETTERS[lang][0]


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


# -- the fixed steps (gloxi.mail_flow) ---------------------------------------------


def facts(lang, **change):
    """What a good model takes from Marta's email (step 3)."""
    out = {
        "title": "Marta",
        "weekday": "Friday",
        "day_of_month": 27,
        "date": "2026-11-27",
        "start_time": "15:00",
        "end_time": "",
        "location": "",
        "reply": GOOD_REPLIES[lang],
    }
    return out | change


def flow(lang, *turns, listed=None):
    mailbox = Mailbox(lang, listed or FakeHerkos.LISTED + vilna_programs())
    server = ScriptedLlamaServer(*turns)
    conv = conversation(server, FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    return mailbox, server, conv


@pytest.mark.parametrize(
    ("lang", "text"), CASES, ids=[f"{c[0]}-{i % 2}" for i, c in enumerate(CASES)]
)
def test_every_request_takes_the_fixed_steps(lang, text):
    assert mail_flow.wants(listing(vilna_programs()), text)


@pytest.mark.parametrize(
    "text",
    [
        "Що в мене завтра в календарі?",
        "Прочитай останній лист",
        "Підготуй відповідь Марті",
        "Read the latest email and reply to it",
        "Add a meeting with Ivan on Monday at 10 to my calendar",
    ],
)
def test_other_requests_stay_with_the_model(text):
    assert not mail_flow.wants(listing(vilna_programs()), text)


def test_without_the_calendar_the_model_does_it_alone():
    assert not mail_flow.wants(listing(vilna_programs({MAIL_TOOL})), REQUESTS["uk"][0])


def listing(programs):
    t = FileTools(Connector(FakeHerkos(listed=FakeHerkos.LISTED + programs)), [])
    t.definitions()  # as warm() does before the first message
    return t


@pytest.mark.parametrize("lang", list(REQUESTS))
@pytest.mark.parametrize("phrasing", [0, 1])
def test_the_fixed_steps_go_through_herkos_and_send_nothing(lang, phrasing):
    request = REQUESTS[lang][phrasing]
    mailbox, server, conv = flow(lang, {"uid": 41}, facts(lang))
    answer = ask(conv, request)

    made = [(c["tool"], c["operation"]) for c in mailbox.calls]
    assert made == [
        (MAIL_TOOL, "list_messages"),
        (MAIL_TOOL, "read_message"),
        (CALENDAR_TOOL, "create_event"),
        (MAIL_TOOL, "save_draft"),
    ]
    assert all(c["user_turn"] == 1 and c["reason"] for c in mailbox.calls)
    assert mailbox.calls[1]["arguments"] == {"uid": 41, "account": ACCOUNT}
    assert mailbox.calls[2]["arguments"] == EVENT
    # The account, the address, the subject and in_reply_to come from the
    # mail program, not from the model.
    assert mailbox.calls[3]["arguments"] == draft(lang)

    # Two answers from the model, each a JSON object held to a schema.
    choose, extract = server.requests
    assert not choose["stream"] and not extract["stream"]
    uids = choose["response_format"]["json_schema"]["schema"]["properties"]["uid"]["enum"]
    assert uids == [42, 41, 40]
    assert "title" in extract["response_format"]["json_schema"]["schema"]["properties"]
    # The email reached the model only inside Keros's boundaries, with the
    # day it was sent to count "on Friday" from.
    header, letter = external(extract["messages"][-1]["content"].split("The email:\n", 1)[1])
    assert header["external_data"] is True and letter["text"] == LETTERS[lang][1]
    assert "The email was sent on Monday, 2026-11-16" in extract["messages"][0]["content"]
    assert LETTERS[lang][1] not in json.dumps(conv.history, ensure_ascii=False)

    # The person is told, in the language they wrote in, and sees the answer.
    assert GOOD_REPLIES[lang] in answer and "27.11.2026, 15:00" in answer
    assert conv.history[-1]["content"] == answer
    assert check(lang, mailbox.calls, answer) == []


def test_an_email_the_model_cannot_find_is_not_read():
    mailbox, server, conv = flow("de", {"uid": 0})
    assert ask(conv, REQUESTS["de"][0]) == mail_flow.SAY["de"]["no_mail"]
    assert [c["operation"] for c in mailbox.calls] == ["list_messages"]
    assert len(server.requests) == 1


def test_an_email_with_no_day_gets_a_draft_and_no_event():
    mailbox, _, conv = flow("fr", {"uid": 41}, facts("fr", date="", start_time=""))
    answer = ask(conv, REQUESTS["fr"][0])
    assert [c["operation"] for c in mailbox.calls] == [
        "list_messages",
        "read_message",
        "save_draft",
    ]
    assert answer.startswith(mail_flow.SAY["fr"]["no_time"])
    assert GOOD_REPLIES["fr"] in answer


def test_the_end_time_location_and_a_reply_to_address_are_used():
    mailbox, _, conv = flow("it", {"uid": 41}, facts("it", end_time="16:30", location="Café Lumen"))
    mailbox.reply_to = "Marta <marta.work@example.com>"
    ask(conv, REQUESTS["it"][0])
    event = mailbox.calls[2]["arguments"]
    assert event == {**EVENT, "end": "2026-11-27T16:30", "location": "Café Lumen"}
    assert mailbox.calls[3]["arguments"]["to"] == ["marta.work@example.com"]


@pytest.mark.parametrize(
    ("date_", "start", "end"),
    [("27.11.2026", "15:00", ""), ("2026-11-27", "3 pm", ""), ("2026-02-30", "15:00", "")],
)
def test_a_day_or_time_gloxi_cannot_read_adds_no_event(date_, start, end):
    mailbox, _, conv = flow(
        "en", {"uid": 41}, facts("en", date=date_, start_time=start, weekday="", day_of_month=0)
    )
    answer = ask(conv, REQUESTS["en"][0])
    assert "create_event" not in [c["operation"] for c in mailbox.calls]
    assert mail_flow.SAY["en"]["no_time"] in answer


def test_an_answer_that_is_not_json_saves_nothing():
    mailbox, _, conv = flow("pl", {"uid": 41}, "Spotkanie w piątek o 15:00.")
    answer = ask(conv, REQUESTS["pl"][0])
    assert [c["operation"] for c in mailbox.calls] == ["list_messages", "read_message"]
    assert (
        mail_flow.SAY["pl"]["no_time"] in answer and mail_flow.SAY["pl"]["draft_failed"] in answer
    )


def test_a_refused_event_still_gets_the_draft():
    mailbox, _, conv = flow("cs", {"uid": 41}, facts("cs"))
    mailbox.refuse = {"create_event"}
    answer = ask(conv, REQUESTS["cs"][0])
    assert [c["operation"] for c in mailbox.calls][-1] == "save_draft"
    assert answer.startswith(mail_flow.SAY["cs"]["event_failed"])


@pytest.mark.parametrize("code", ["denied_user", "denied_timeout", "denied_error"])
def test_a_read_herkos_did_not_run_is_not_no_email(code):
    # The VM check (2026-11-24): a "no", no answer in time, or a window that
    # never showed, and Gloxi said there was no such email.
    mailbox, _, conv = flow("uk", {"uid": 41})
    mailbox.refuse, mailbox.refuse_code = {"read_message"}, code
    say = mail_flow.SAY["uk"]
    assert ask(conv, REQUESTS["uk"][0]) == f"{say['not_read']} {say[code]}"
    mailbox, _, conv = flow("lt", {"uid": 41}, facts("lt"))
    mailbox.refuse, mailbox.refuse_code = {"create_event"}, code
    say = mail_flow.SAY["lt"]
    assert ask(conv, REQUESTS["lt"][0]).startswith(f"{say['event_failed']} {say[code]}")


def test_a_mail_program_error_is_not_no_email():
    # The VM check after PR #224: the program failed at the first step, and
    # Gloxi said there was no such email.
    mailbox, _, conv = flow("pl", {"uid": 41})
    mailbox.refuse, mailbox.refuse_code = {"list_messages"}, "tool_error"
    say = mail_flow.SAY["pl"]
    assert ask(conv, REQUESTS["pl"][0]) == f"{say['not_read']} {say['tool_error']}"


def test_after_no_answer_in_time_gloxi_does_not_offer_to_ask_again():
    # Decision of 2026-11-24: the person asks again if they want to.
    for lang, say in mail_flow.SAY.items():
        told = say["denied_timeout"]
        assert "?" not in told and told.count(".") == 1, (lang, told)


@pytest.mark.parametrize("lang", list(REQUESTS))
def test_what_gloxi_says_is_in_the_language_of_the_message(lang):
    say = mail_flow.SAY[lang]
    for key in ("no_mail", "no_time", "event_failed", "draft_failed"):
        assert _lang_ok(say[key], lang), (lang, key, say[key])
    for code in ("denied_user", "denied_timeout", "denied_error", "tool_error"):
        for first in ("not_read", "not_sent"):
            told = f"{say[first]} {say[code]}"
            assert _lang_ok(told, lang), (lang, code, told)
    told = say["event"].format(title="Marta", when="27.11.2026, 15:00")
    told += " " + say["draft"].format(to=SENDER)
    assert _lang_ok(told, lang), (lang, told)
    assert mail_flow.language(REQUESTS[lang][0]) == lang
    assert mail_flow.language(REQUESTS[lang][1]) == lang


def test_the_bench_check_catches_a_send_a_wrong_day_and_a_wrong_language():
    mailbox, _, conv = flow("pl", {"uid": 41}, facts("pl"))
    answer = ask(conv, REQUESTS["pl"][0])
    calls = [dict(c) for c in mailbox.calls]
    sent = {**calls[3], "operation": "send"}
    assert check("pl", [*calls, sent], answer) == ["not_sent"]

    calls[2] = {**calls[2], "arguments": {**EVENT, "start": "2026-11-20T15:00"}}
    calls[3] = {**calls[3], "arguments": {**draft("pl"), "text": GOOD_REPLIES["uk"]}}
    assert check("pl", calls, DONE["uk"]) == ["event", "draft", "lang"]
    assert check("pl", [], "") == ["find", "read", "event", "draft", "lang"]
    assert "fail: event, draft, lang" in report_of("pl", calls)


def report_of(lang, calls):
    from gloxi.bench.mail_scenario import Outcome

    return report([Outcome(lang, "?", "", 1.0, calls, check(lang, calls, ""))])


# -- the model on its own (other mail requests) ------------------------------------


def test_gloxi_fills_the_account_and_in_reply_to_a_model_left_out():
    # What Qwen3 4B and Ministral 3 8B did on KLX (2026-11-23): no
    # list_accounts, an account made up or none, and no in_reply_to.
    mailbox = Mailbox("uk", FakeHerkos.LISTED + vilna_programs())
    made_up = {k: v for k, v in draft("uk").items() if k != "in_reply_to"}
    made_up["account"] = "user@example.com"
    server = ScriptedLlamaServer(
        [("mail_list_messages", args(limit=5, reason="r"))],
        [("mail_read_message", args(uid=41, reason="r"))],
        [("mail_save_draft", args(**made_up, reason="r"))],
        "Готово.",
    )
    conv = conversation(server, FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    assert ask(conv, "Прочитай останній лист від Марти й підготуй відповідь") == "Готово."
    # From what the mail program said (the account it read), not the email's text.
    assert mailbox.calls[-1]["arguments"] == draft("uk")


def test_a_reply_to_someone_else_gets_no_in_reply_to_and_a_known_account_stays():
    mailbox = Mailbox("en", FakeHerkos.LISTED + vilna_programs())
    other = {**draft("en"), "account": "work@example.org", "to": ["ivan@example.net"]}
    del other["in_reply_to"]
    server = ScriptedLlamaServer(
        [("mail_list_accounts", args(reason="r"))],
        [("mail_read_message", args(uid=41, reason="r"))],
        [("mail_save_draft", args(**other, reason="r"))],
        "Done.",
    )
    conv = conversation(server, FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    ask(conv, "Read Marta's email and write a reply to Ivan")
    # work@ is not an account the program gave: the one the email came to.
    assert mailbox.calls[-1]["arguments"] == {**other, "account": ACCOUNT}

    mailbox.calls.clear()
    conv.tools._mail.accounts.append("work@example.org")
    conv.tools.call("mail_save_draft", json.dumps(other), user_turn=2)
    assert mailbox.calls[-1]["arguments"] == other


def test_a_send_after_the_draft_answers_the_email_and_takes_the_draft_out():
    # The R4 test in the VM (2026-11-24): an id of the right form but made up,
    # a subject of the model's own, and the saved draft left in Drafts.
    mailbox = Mailbox("en", FakeHerkos.LISTED + vilna_programs())
    conv = conversation(ScriptedLlamaServer("Done."), FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    conv.tools.call("mail_read_message", json.dumps({"uid": 41, "reason": "r"}), user_turn=1)
    conv.tools.call("mail_save_draft", json.dumps(draft("en")), user_turn=1)
    made_up = {**draft("en"), "subject": "Meeting", "in_reply_to": "<m41@example.org>"}
    conv.tools.call("mail_send", json.dumps(made_up), user_turn=2)
    assert mailbox.calls[-1]["operation"] == "send"
    assert mailbox.calls[-1]["arguments"] == {
        **draft("en"),
        "replaces_draft": "<d1.gloxi@example.org>",
    }
    # Another text is a new message: the draft stays, the answer is still one.
    changed = {**draft("en"), "text": "Sorry, Friday is out."}
    conv.tools.call("mail_send", json.dumps(changed), user_turn=3)
    assert mailbox.calls[-1]["arguments"] == changed
    # "Re: Re:" or "RE:" is the same subject and stays as written.
    again = {**draft("en"), "subject": "RE: " + draft("en")["subject"]}
    conv.tools.call("mail_save_draft", json.dumps(again), user_turn=3)
    assert mailbox.calls[-1]["arguments"]["subject"] == again["subject"]


@pytest.mark.parametrize("lang", sorted(REQUESTS))
def test_herkos_is_told_why_in_the_language_of_the_request(lang):
    mailbox, _, conv = flow(lang, {"uid": 41}, facts(lang))
    ask(conv, REQUESTS[lang][0])
    why = mail_flow.WHY[lang]
    assert [c["reason"] for c in mailbox.calls] == [
        why["find"],
        why["read"],
        why["event"],
        why["draft"],
    ]
    assert set(mail_flow.WHY) == set(mail_flow.SAY)


def test_a_made_up_domain_and_in_reply_to_are_put_right_before_the_send():
    # The send in the VM check (2026-11-24): the sender's name at another
    # domain and "..." for in_reply_to, shown to the person in the R4 window.
    mailbox = Mailbox("en", FakeHerkos.LISTED + vilna_programs())
    conv = conversation(ScriptedLlamaServer("Done."), FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    conv.tools.call("mail_read_message", json.dumps({"uid": 41, "reason": "r"}), user_turn=1)
    slip = {**draft("en"), "to": "marta@example.org", "in_reply_to": "..."}
    conv.tools.call("mail_save_draft", json.dumps(slip), user_turn=1)
    assert mailbox.calls[-1]["arguments"] == {**draft("en"), "to": SENDER}
    in_list = {**draft("en"), "to": ["Marta <marta@example.org>", "ivan@example.net"]}
    conv.tools.call("mail_save_draft", json.dumps(in_list), user_turn=1)
    assert mailbox.calls[-1]["arguments"]["to"] == [SENDER, "ivan@example.net"]
    # Someone else with the same name stays, and so does a real Message-ID.
    other = {**draft("en"), "to": ["ola@example.net"], "in_reply_to": "<x1@example.net>"}
    conv.tools.call("mail_save_draft", json.dumps(other), user_turn=1)
    assert mailbox.calls[-1]["arguments"] == other


def test_a_letter_more_in_the_senders_name_is_the_sender():
    # Qwen3 4B, lt, in the VM check after PR #224: martas@ for marta@. The R4
    # window would have shown another person's address with no in_reply_to.
    mailbox = Mailbox("lt", FakeHerkos.LISTED + vilna_programs())
    conv = conversation(ScriptedLlamaServer("Gerai."), FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    conv.tools.call("mail_read_message", json.dumps({"uid": 41, "reason": "r"}), user_turn=1)
    for slip in ("martas@example.com", "mart@example.com", "marte@example.com"):
        sent = {k: v for k, v in draft("lt").items() if k != "in_reply_to"}
        conv.tools.call("mail_save_draft", json.dumps({**sent, "to": [slip]}), user_turn=1)
        assert mailbox.calls[-1]["arguments"] == draft("lt"), slip
    # Someone on the email, or two letters away, is a person and stays.
    mailbox.reply_to = "Marto <marto@example.com>"
    conv.tools.call("mail_read_message", json.dumps({"uid": 41, "reason": "r"}), user_turn=2)
    for person in ("marto@example.com", "marina@example.com"):
        other = {**draft("lt"), "to": [person], "in_reply_to": "<x1@example.net>"}
        conv.tools.call("mail_save_draft", json.dumps(other), user_turn=2)
        assert mailbox.calls[-1]["arguments"] == other, person


def test_the_subject_without_re_becomes_the_answers():
    # The VM check after PR #224: the original subject without "Re:" stayed.
    mailbox = Mailbox("en", FakeHerkos.LISTED + vilna_programs())
    conv = conversation(ScriptedLlamaServer("Done."), FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    conv.tools.call("mail_read_message", json.dumps({"uid": 41, "reason": "r"}), user_turn=1)
    original = mail_scenario_subject("en")
    bare = {**draft("en"), "subject": original}
    conv.tools.call("mail_save_draft", json.dumps(bare), user_turn=1)
    assert mailbox.calls[-1]["arguments"]["subject"] == f"Re: {original}"


def test_a_send_with_the_quotes_changed_still_takes_the_draft_out():
    # Ministral 3 8B, 2 of 8 conversations in the VM check after PR #224:
    # «» became "" in the text sent, and the draft stayed in Drafts.
    mailbox = Mailbox("uk", FakeHerkos.LISTED + vilna_programs())
    conv = conversation(ScriptedLlamaServer("Готово."), FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    conv.tools.call("mail_read_message", json.dumps({"uid": 41, "reason": "r"}), user_turn=1)
    saved = {**draft("uk"), "text": "Добре, буду в «Каві» о 15:00.\n\nОля"}
    conv.tools.call("mail_save_draft", json.dumps(saved), user_turn=1)
    copied = {**saved, "text": 'Добре, буду в "Каві" о 15:00.\nОля'}
    conv.tools.call("mail_send", json.dumps(copied), user_turn=2)
    assert mailbox.calls[-1]["arguments"]["replaces_draft"] == "<d1.gloxi@example.org>"


SEND = {
    "uk": "Надішли Марті мою відповідь",
    "pl": "Wyślij Marcie moją odpowiedź",
    "lt": "Išsiųsk Martai mano atsakymą",
}


@pytest.mark.parametrize(
    ("lang", "code"),
    [("uk", "denied_user"), ("pl", "denied_timeout"), ("lt", "denied_error")],
)
def test_a_send_herkos_did_not_run_is_told_in_fixed_words(lang, code):
    # The VM check after PR #232: after a refused or unanswered send, 6 of 12
    # answers were right; Ministral 3 8B passed the note on and offered to
    # try again, and Qwen3 4B answered a Lithuanian request in English.
    mailbox = Mailbox(lang, FakeHerkos.LISTED + vilna_programs())
    mailbox.refuse, mailbox.refuse_code = {"send"}, code
    server = ScriptedLlamaServer(
        [("mail_read_message", args(uid=41, reason="r"))],
        [("mail_send", args(**draft(lang), reason="r"))],
        "Try again? I can send it once more.",
    )
    conv = conversation(server, FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    say = mail_flow.SAY[lang]
    asked = SEND[lang]
    assert not mail_flow.wants(conv.tools, asked)  # the model's own turn, not the fixed steps
    assert ask(conv, asked) == f"{say['not_sent']} {say[code]}"
    assert _lang_ok(f"{say['not_sent']} {say[code]}", lang)


def test_a_send_of_the_draft_goes_to_the_drafts_people():
    # Qwen3 4B, pl, both conversations of the VM check after PR #232: the
    # draft to marta@, the send of it to marcia@, two letters away.
    mailbox = Mailbox("pl", FakeHerkos.LISTED + vilna_programs())
    conv = conversation(ScriptedLlamaServer("Gotowe."), FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    conv.tools.call("mail_read_message", json.dumps({"uid": 41, "reason": "r"}), user_turn=1)
    conv.tools.call("mail_save_draft", json.dumps(draft("pl")), user_turn=1)
    slip = {k: v for k, v in draft("pl").items() if k != "in_reply_to"}
    conv.tools.call("mail_send", json.dumps({**slip, "to": ["marcia@example.com"]}), user_turn=2)
    assert mailbox.calls[-1]["arguments"] == {
        **draft("pl"),
        "replaces_draft": "<d1.gloxi@example.org>",
    }
    # Another text is another email: its address stays as written.
    other = {**slip, "to": ["marcia@example.com"], "text": "Zupełnie inna wiadomość."}
    conv.tools.call("mail_send", json.dumps(other), user_turn=3)
    assert mailbox.calls[-1]["arguments"]["to"] == ["marcia@example.com"]


def test_an_address_the_person_wrote_is_theirs():
    # The VM check after PR #232: "to martas@example.com, Marta's new
    # address", and Gloxi made it marta@ without a word.
    mailbox = Mailbox("uk", FakeHerkos.LISTED + vilna_programs())
    conv = conversation(ScriptedLlamaServer("Готово."), FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    conv.tools.call("mail_read_message", json.dumps({"uid": 41, "reason": "r"}), user_turn=1)
    conv.tools.call("mail_save_draft", json.dumps(draft("uk")), user_turn=1)
    conv.tools.heard("Надішли на martas@example.com, це нова адреса Марти")
    new = {**draft("uk"), "to": ["martas@example.com"]}
    conv.tools.call("mail_save_draft", json.dumps(new), user_turn=2)
    assert mailbox.calls[-1]["arguments"]["to"] == ["martas@example.com"]
    conv.tools.call("mail_send", json.dumps(new), user_turn=2)
    assert mailbox.calls[-1]["arguments"]["to"] == ["martas@example.com"]


NEW_ADDRESS = {
    "uk": "Надішли відповідь на martas@example.com, це нова адреса Марти",
    "pl": "Wyślij odpowiedź na martas@example.com, to nowy adres Marty",
}


def _read_and_draft(lang):
    mailbox = Mailbox(lang, FakeHerkos.LISTED + vilna_programs())
    conv = conversation(ScriptedLlamaServer("OK"), FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    conv.tools.call("mail_read_message", json.dumps({"uid": 41, "reason": "r"}), user_turn=1)
    conv.tools.call("mail_save_draft", json.dumps(draft(lang)), user_turn=1)
    conv.tools.heard(NEW_ADDRESS[lang])
    return mailbox, conv


@pytest.mark.parametrize("to", [[SENDER], None], ids=["old-address", "no-address"])
def test_the_address_the_person_gave_is_used_when_the_model_falls_back(to):
    # Ministral 3 8B, pl, VM check after PR #238: the person gave martas@,
    # the model wrote marta@ again, and the draft went to the old address.
    mailbox, conv = _read_and_draft("pl")
    sent = {k: v for k, v in draft("pl").items() if k != "to"}
    if to is not None:
        sent["to"] = to
    conv.tools.call("mail_send", json.dumps(sent), user_turn=2)
    assert mailbox.calls[-1]["arguments"] == {
        **draft("pl"),
        "to": ["martas@example.com"],
        "replaces_draft": "<d1.gloxi@example.org>",
    }


def test_a_reply_at_the_new_address_still_answers_the_email():
    # The VM check after PR #238, uk: martas@ was kept, but the email went
    # out as a new one, without in_reply_to or "Re:", and the draft stayed.
    mailbox, conv = _read_and_draft("uk")
    bare = {
        "account": ACCOUNT,
        "to": ["martas@example.com"],
        "subject": "Відповідь",
        "text": GOOD_REPLIES["uk"],
    }
    conv.tools.call("mail_send", json.dumps(bare), user_turn=2)
    assert mailbox.calls[-1]["arguments"] == {
        **draft("uk"),
        "to": ["martas@example.com"],
        "replaces_draft": "<d1.gloxi@example.org>",
    }


def test_an_address_the_model_chose_itself_is_not_replaced():
    # Only a fallback becomes the person's address: a third address the
    # model wrote is shown in the Herkos window as it is.
    mailbox, conv = _read_and_draft("uk")
    other = {**draft("uk"), "to": ["ivan@example.com"], "text": "Зовсім інший лист."}
    conv.tools.call("mail_send", json.dumps(other), user_turn=2)
    assert mailbox.calls[-1]["arguments"]["to"] == ["ivan@example.com"]
    assert "replaces_draft" not in mailbox.calls[-1]["arguments"]


def test_a_new_message_without_addresses_changes_nothing():
    mailbox, conv = _read_and_draft("uk")
    conv.tools.heard("Надішли Марті мою відповідь")
    conv.tools.call("mail_send", json.dumps(draft("uk")), user_turn=3)
    assert mailbox.calls[-1]["arguments"]["to"] == [SENDER]


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
        "Fertig.",
    )
    conv = conversation(server, FileTools(Connector(mailbox), []))
    conv.tools.definitions()
    assert ask(conv, "Lies Martas E-Mail und trag das Treffen in den Kalender ein") == "Fertig."
    events = [c["arguments"] for c in mailbox.calls if c["operation"] == "create_event"]
    assert events == [EVENT]
    waited = json.loads(server.requests[2]["messages"][-1]["content"])
    assert waited["status"] == "error" and "mail_read_message" in waited["message"]


def test_a_model_that_is_not_running_is_said_as_for_any_message():
    from gloxi.llm import ModelUnavailable

    mailbox, server, conv = flow("en")
    server.status = 503

    def down(*_):
        raise ModelUnavailable("cannot reach the model")

    conv.router.local.complete = down
    with pytest.raises(ModelUnavailable):
        ask(conv, REQUESTS["en"][0])
    assert [c["operation"] for c in mailbox.calls] == ["list_messages"]
    assert conv.history == []


@pytest.mark.parametrize(
    ("given", "weekday", "number", "fixed"),
    [
        # What Qwen3 4B wrote on KLX (2026-11-24) for "Friday 27 November".
        ("2026-11-25", "Friday", 27, "2026-11-27"),
        ("2026-11-26", "Friday", 27, "2026-11-27"),
        ("2026-11-25", "Friday", 0, "2026-11-27"),
        ("2026-11-25", "", 27, "2026-11-27"),
        ("2026-11-27", "Friday", 27, "2026-11-27"),
        ("2026-11-30", "Friday", 4, "2026-12-04"),
        # Nothing near fits both: the model's date stays.
        ("2026-11-25", "Friday", 3, "2026-11-25"),
        ("2026-11-25", "", 0, "2026-11-25"),
        ("", "Friday", 27, ""),
    ],
)
def test_the_weekday_and_the_day_of_month_fix_the_date(given, weekday, number, fixed):
    assert mail_flow._fixed_day(given, weekday, number) == fixed


def test_a_wrong_date_with_the_right_weekday_still_makes_the_right_event():
    mailbox, _, conv = flow("lt", {"uid": 41}, facts("lt", date="2026-11-25"))
    ask(conv, REQUESTS["lt"][0])
    assert mailbox.calls[2]["arguments"] == EVENT
