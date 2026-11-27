# SPDX-License-Identifier: Apache-2.0
"""Hostile input for the privacy gateway: nothing it promises to hide may stay, nothing may break.

All data here is synthetic: example.org addresses, the published test card numbers, the
specimen IBANs and ID numbers that the other tests of this package already use, and
obviously fake keys. Only forms that the gateway handles are asserted; no test is skipped
or expected to fail.

Four groups:

* round trip: ``unmask(mask(x)) == x`` on empty, huge, control, RTL, emoji and mixed text;
* nothing found survives ``mask`` in unusual surroundings (quotes, brackets, NBSP, RTL marks,
  Unicode digits, other separators);
* a placeholder-like string written by a person or a model does not pull in other values;
* time: a megabyte of hostile text is processed well inside ten seconds.
"""

from __future__ import annotations

import random
import re
import signal
import threading
import time
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from privacy_gateway import Kind, Masker, detect

LIMIT_SECONDS = 10.0
MEGABYTE = 1 << 20

EMAIL = "olena.test@example.org"
PHONE = "+380 67 123 45 67"
CARD = "4111 1111 1111 1111"
IBAN = "UA21 3223 1300 0002 6007 2335 6600 1"


# --- helpers ---------------------------------------------------------------------


def flat(text: str) -> str:
    """Letters and digits only, one alphabet, one digit system: what a reader would see."""
    out = []
    for ch in unicodedata.normalize("NFKC", text).casefold():
        if ch.isdecimal():
            out.append(str(unicodedata.decimal(ch)))
        elif ch.isalnum():
            out.append(ch)
    return "".join(out)


def leaked(masked: str, secret: str, run: int = 6) -> bool:
    """Does any ``run`` characters in a row of ``secret`` still show in ``masked``?"""
    left, right = flat(masked), flat(secret)
    if len(right) < run:
        return right in left
    return any(right[i : i + run] in left for i in range(len(right) - run + 1))


def assert_hidden(text: str, secret: str, kind: Kind, restores: bool = True) -> None:
    masker = Masker()
    masked = masker.mask(text)
    # The domain of an address may stay in a URL around it; the person's part may not.
    assert not leaked(masked, secret.split("@")[0] if kind is Kind.EMAIL else secret), masked
    assert f"[{kind.value}_1]" in masked, masked
    if restores:
        assert masker.unmask(masked) == text


@contextmanager
def within_limit() -> Iterator[None]:
    """Fail, not hang, when a regular expression backtracks for good."""
    armed = hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
    if armed:

        def on_alarm(signum, frame):
            raise TimeoutError(f"no result after {LIMIT_SECONDS:.0f} seconds")

        previous = signal.signal(signal.SIGALRM, on_alarm)
        signal.setitimer(signal.ITIMER_REAL, LIMIT_SECONDS)
    started = time.perf_counter()
    try:
        yield
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    assert time.perf_counter() - started < LIMIT_SECONDS


def fullwidth(text: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if c.isascii() and c.isalnum() else c for c in text)


def arabic_indic(text: str) -> str:
    return text.translate({ord(str(d)): 0x0660 + d for d in range(10)})


CONTEXTS = {
    "alone": "{}",
    "ascii-quotes": '"{}"',
    "single-quotes": "'{}'",
    "curly-quotes": "\u201c{}\u201d",
    "guillemets": "\u00ab{}\u00bb",
    "parentheses": "({})",
    "square-brackets": "[{}]",
    "braces": "{{{}}}",
    "angle-brackets": "<{}>",
    "commas": "a,{},b",
    "semicolons": "x;{};y",
    "colon-label": "k:{}",
    "question": "{}?",
    "newlines": "a\n{}\nb",
    "tabs": "a\t{}\tb",
    "nbsp": "a\u00a0{}\u00a0b",
    "narrow-nbsp": "a\u202f{}\u202fb",
    "rtl-marks": "\u200f{}\u200f",
    "rtl-override": "\u202e{}\u202c",
    "emoji": "\U0001f4e7{}\U0001f4e7",
    "cyrillic-dashes": "Дані\u2014{}\u2014дякую",
    "combining": "a\u0301 {} b\u0301",
    "url-query": "https://example.org/x?v={}&z=1",
    "markdown-bold": "**{}**",
    "html": "<b>{}</b>",
    "json": '{{"v": "{}"}}',
    "prose": "Привіт! Це {} \u2014 і ще трохи тексту.",
}


# Contexts that put the value inside a pair of brackets of their own.
BRACKETED = frozenset({"parentheses", "square-brackets", "braces", "markdown-link"})


def combos(kind: Kind, values: dict[str, str], contexts: dict[str, str]) -> list:
    return [
        pytest.param(
            kind,
            value,
            template.format(value),
            cname not in BRACKETED,
            id=f"{kind.value}-{vname}-{cname}",
        )
        for vname, value in values.items()
        for cname, template in contexts.items()
    ]


# --- 1. round trip -----------------------------------------------------------------

HOSTILE_TEXTS = {
    "empty": "",
    "one-space": " ",
    "only-whitespace": " \t\r\n\u00a0\u2003\u3000\u2028\u2029\u000b\u000c",
    "nul": "\0",
    "nul-inside": "ab\0cd\0\0",
    "c0-controls": "".join(chr(i) for i in range(32)),
    "del-and-c1-controls": "\x7f" + "".join(chr(i) for i in range(0x80, 0xA0)),
    "ansi-escape": "\x1b[31mred\x1b[0m \x1b]0;title\x07",
    "emoji": "\U0001f600\U0001f4a9\U0001f680",
    "emoji-zwj-family": "\U0001f469\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466",
    "emoji-flags-and-skin": "\U0001f1fa\U0001f1e6 \U0001f44d\U0001f3fd",
    "arabic-rtl": "\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645",
    "hebrew-rtl": "\u05e9\u05dc\u05d5\u05dd \u05e2\u05d5\u05dc\u05dd",
    "bidi-controls": "abc \u202e\u2066def\u2069 \u202c\u200f\u200e\u061c",
    "combining-marks": "e\u0301\u0302\u0303\u0304 a" + "\u0300" * 200,
    "zalgo": "Z" + "".join(chr(0x300 + i % 112) for i in range(300)),
    "zero-width": "a\u200bb\u200cc\u200dd\u2060e\ufeff",
    "variation-selectors": "\u2764\ufe0f \u2764\ufe0e",
    "astral-and-noncharacters": "\U00010000\U0010ffff\uffff\ufffe",
    "lone-surrogates": "a\ud800b\udfffc",
    "mixed-scripts": "Hello привіт 你好 \u0645\u0631\u062d\u0628\u0627 γεια გამარჯობა",
    "only-brackets": "[[[]]] {{{}}} ((()))",
    "only-at-and-dots": "@@@ ... @.@.@",
    "placeholder-lookalikes": "[EMAIL_9] {PHONE_3} [[NAME_2]] EMAIL_4 [ ] [_1] [A_] [1]",
    "very-long-line": "word " * 20000,
}


@pytest.mark.parametrize("text", list(HOSTILE_TEXTS.values()), ids=list(HOSTILE_TEXTS))
def test_text_without_findings_comes_back_unchanged(text):
    masker = Masker()
    masked = masker.mask(text)
    assert masked == text
    assert masker.unmask(masked) == text
    assert "".join(masker.unmask_stream([text])) == text
    assert len(masker) == 0


SENTENCES = {
    "uk": "Напиши Олені на",
    "en": "Write to Olena at",
    "es": "Escribe a Elena a",
    "pl": "Napisz do Eleny na",
    "de": "Schreib an Elena an",
    "fr": "Écris à Hélène à",
    "it": "Scrivi a Elena a",
    "ro": "Scrie-i Elenei la",
    "cs": "Napiš Eleně na",
    "sk": "Napíš Elene na",
    "lt": "Parašyk Elenai adresu",
    "ca": "Escriu a l'Elena a",
    "gl": "Escribe a Elena a",
    "eu": "Idatzi Elenari helbide honetara:",
    "pt": "Escreve à Helena para",
}


def test_the_fifteen_languages_are_all_here():
    assert len(SENTENCES) == 15


@pytest.mark.parametrize("lang", list(SENTENCES))
def test_round_trip_in_each_language(lang):
    text = f"{SENTENCES[lang]} {EMAIL}, tel. {PHONE}. \u2014 {SENTENCES[lang]} {CARD}!"
    masker = Masker()
    masked = masker.mask(text)
    assert (
        masked == f"{SENTENCES[lang]} [EMAIL_1], tel. [PHONE_1]. \u2014 {SENTENCES[lang]} [CARD_1]!"
    )
    assert masker.unmask(masked) == text
    assert (
        "".join(masker.unmask_stream(masked[i : i + 3] for i in range(0, len(masked), 3))) == text
    )


@pytest.mark.parametrize("lang", list(SENTENCES))
def test_one_message_with_all_languages_round_trips(lang):
    text = "\n".join(f"{s} user{i}@example.org" for i, s in enumerate(SENTENCES.values()))
    masker = Masker()
    masked = masker.mask(text)
    assert "@" not in masked
    assert len(masker) == 15
    assert masker.unmask(masked) == text
    assert SENTENCES[lang] in masked


@pytest.mark.parametrize("name", list(HOSTILE_TEXTS))
def test_value_among_hostile_neighbours_round_trips(name):
    hostile = HOSTILE_TEXTS[name]
    if len(hostile) > 1000:
        hostile = hostile[:1000]
    text = f"{hostile} {EMAIL} {hostile}"
    masker = Masker()
    masked = masker.mask(text)
    assert EMAIL not in masked
    assert masker.unmask(masked) == text


def test_megabyte_round_trip():
    filler = "Привіт, світе! Hello, world! 你好 \U0001f30d \u0645\u0631\u062d\u0628\u0627 \n"
    pieces = []
    size = 0
    index = 0
    while size < MEGABYTE:
        piece = filler * 400
        if index % 2 == 0:
            piece += f"mail user{index}@example.org, card {CARD}\n"
        pieces.append(piece)
        size += len(piece)
        index += 1
    text = "".join(pieces)
    assert len(text) >= MEGABYTE
    masker = Masker()
    with within_limit():
        masked = masker.mask(text)
        restored = masker.unmask(masked)
        streamed = "".join(
            masker.unmask_stream(masked[i : i + 4096] for i in range(0, len(masked), 4096))
        )
    assert "@example.org" not in masked
    assert CARD not in masked
    assert restored == text
    assert streamed == text
    assert len(masker) == index // 2 + (index % 2) + 1


def test_megabyte_without_findings_is_returned_as_it_is():
    text = ("Привіт\0 світе \u202e\u200b " * 40000)[:MEGABYTE]
    masker = Masker()
    with within_limit():
        assert masker.mask(text) == text
        assert masker.unmask(text) == text


def test_random_text_never_raises_and_round_trips():
    rng = random.Random(20261127)
    pools = [
        "abcxyz ABCXYZ 0123456789 .,;:!?-_/\\@#$%^&*()[]{}<>'\"`~+=|",
        "абвгґдеєжії ŁÑŽȘØĞ",
        "\0\x01\x07\x1b\x7f\u200b\u200d\u202e\u2066\u2069\ufeff\u00a0\u3000",
        "\U0001f600\U0001f4a9\u0301\u0302\u0645\u05e9你好γა",
    ]
    for _ in range(300):
        pool = "".join(rng.sample(pools, rng.randint(1, len(pools))))
        text = "".join(rng.choice(pool) for _ in range(rng.randint(0, 400)))
        masker = Masker()
        masked = masker.mask(text)
        assert masker.unmask(masked) == text
        assert masker.mask(masked) == masked


def test_mixed_values_round_trip_in_one_text():
    text = (
        f"{EMAIL}\u00a0|\u00a0{PHONE}\n"
        f"\u202e{CARD}\u202c; IBAN: {IBAN}.\n"
        "Name: Łukasz Żemaitis\n"
        "PESEL 90010112349, DNI 12345678Z\n"
        "Authorization: Bearer FAKETOKENFAKETOKEN0123456789\n"
    )
    masker = Masker()
    masked = masker.mask(text)
    for secret in (
        EMAIL,
        PHONE,
        CARD,
        IBAN,
        "Łukasz",
        "90010112349",
        "12345678Z",
        "FAKETOKEN",
    ):
        assert not leaked(masked, secret), secret
    assert masker.unmask(masked) == text


def test_mask_messages_survives_odd_messages_and_keeps_the_input():
    messages = [
        {"role": "system", "content": "\0 \u202e system"},
        {"role": "user", "content": f"hello {EMAIL}"},
        {"role": "user", "content": ""},
        {"role": "assistant", "content": None},
        {"role": "user", "content": [{"type": "text", "text": f"keep {EMAIL}"}]},
        {"role": "tool"},
        {"content": "no role"},
    ]
    before = repr(messages)
    masker = Masker()
    out = masker.mask_messages(messages)
    assert repr(messages) == before
    assert EMAIL not in repr([m["content"] for m in out if isinstance(m.get("content"), str)])
    assert out[1]["content"].startswith("hello [EMAIL_1]")
    assert out[3]["content"] is None
    assert out[4]["content"] == [{"type": "text", "text": f"keep {EMAIL}"}]
    assert "content" not in out[5]
    assert masker.mask_messages([]) == []


def test_repr_never_shows_hostile_values():
    masker = Masker()
    masker.mask(f"Name: Łukasz\n{EMAIL}\n{CARD}\nPassword: Fake-pass-7731")
    shown = repr(masker) + str(masker)
    for secret in ("ukasz", "olena", "4111", "Fake-pass"):
        assert secret not in shown


# --- 2. nothing found survives in unusual forms ------------------------------------

EMAILS = {
    "plain": EMAIL,
    "upper-case": EMAIL.upper(),
    "mixed-case": "Olena.Test@Example.Org",
    "plus-tag": "olena+news@example.org",
    "subdomain": "olena.test@mail.eu.example.org",
    "hyphenated-domain": "olena-test@my-host.example.org",
    "digits": "o1234@example.org",
    "percent-and-underscore": "o_l%e@example.org",
}


@pytest.mark.parametrize(
    ("kind", "secret", "text", "restores"), combos(Kind.EMAIL, EMAILS, CONTEXTS)
)
def test_email_is_hidden_in_unusual_surroundings(kind, secret, text, restores):
    assert_hidden(text, secret, kind, restores)


@pytest.mark.parametrize(
    "text",
    [
        f"mailto:{EMAIL}",
        f"MAILTO:{EMAIL}",
        f"[write](mailto:{EMAIL})",
        f'<a href="mailto:{EMAIL}">{EMAIL}</a>',
        f"a,{EMAIL},b,{EMAIL.upper()};c",
        f"x{EMAIL}",
        f"{EMAIL}.",
        f"{EMAIL}...",
        f"{EMAIL}\u200b",
        f"\u200b{EMAIL}",
        f"{EMAIL}\u0301",
        f"{EMAIL}%0a",
        f"{EMAIL}\\n",
        f"From: {EMAIL}\r\nTo: {EMAIL}\r\n",
        f"user={EMAIL}&pass=1",
    ],
)
def test_email_with_odd_glue_leaves_nothing(text):
    masker = Masker()
    masked = masker.mask(text)
    assert not leaked(masked, "olena.test"), masked
    assert "@" not in masked.replace("[EMAIL_1]", "")


PHONES = {
    "spaces": PHONE,
    "dashes": "+380-67-123-45-67",
    "compact": "+380671234567",
    "double-zero-spaced": "00380 67 123 45 67",
    "national-spaces": "067 123 45 67",
    "national-dashes": "067-123-45-67",
    "parentheses": "+38 (067) 123-45-67",
    "us-style": "+1 (202) 555-0143",
    "us-dots": "+1.202.555.0143",
    "dots-4-3-3": "067.123.4567",
    "german": "+49 30 12345678",
    "nbsp": "+380\u00a067\u00a0123\u00a045\u00a067",
    "thin-spaces": "+380\u200967\u2009123\u200945\u200967",
    "narrow-nbsp": "+380\u202f67\u202f123\u202f45\u202f67",
    "fullwidth-digits": fullwidth("+380 67 123 45 67"),
    "arabic-indic-digits": arabic_indic("+380 67 123 45 67"),
}


@pytest.mark.parametrize(
    ("kind", "secret", "text", "restores"), combos(Kind.PHONE, PHONES, CONTEXTS)
)
def test_phone_is_hidden_in_unusual_surroundings(kind, secret, text, restores):
    masker = Masker()
    masked = masker.mask(text)
    assert not leaked(masked, secret), masked
    assert "[PHONE_" in masked


@pytest.mark.parametrize(
    "context", ["alone", "ascii-quotes", "parentheses", "commas", "nbsp", "newlines", "rtl-marks"]
)
def test_bare_number_with_a_key_word_is_hidden(context):
    text = CONTEXTS[context].format("тел. 0671234567")
    masked = Masker().mask(text)
    assert "0671234567" not in masked
    assert "[PHONE_1]" in masked


CARDS = {
    "visa-spaces": CARD,
    "visa-dashes": "4111-1111-1111-1111",
    "visa-compact": "4111111111111111",
    "mastercard": "5555 5555 5555 4444",
    "mastercard-new-range": "2223 0000 4840 0011",
    "amex": "3782 822463 10005",
    "discover": "6011 0000 0000 0004",
    "mixed-separators": "4111 1111-1111 1111",
}
CARD_CONTEXTS = {**CONTEXTS, "label-glued": "card:{}", "label-upper": "CARD {}"}


@pytest.mark.parametrize(
    ("kind", "secret", "text", "restores"), combos(Kind.CARD, CARDS, CARD_CONTEXTS)
)
def test_card_is_hidden_in_unusual_surroundings(kind, secret, text, restores):
    assert_hidden(text, secret, kind, restores)


IBANS = {
    "ua-spaces": IBAN,
    "ua-compact": "UA213223130000026007233566001",
    "de-spaces": "DE89 3704 0044 0532 0130 00",
    "de-compact": "DE89370400440532013000",
    "gb": "GB82 WEST 1234 5698 7654 32",
    "pl": "PL61 1090 1014 0000 0712 1981 2874",
    "fr-with-letter": "FR14 2004 1010 0505 0001 3M02 606",
    "es": "ES91 2100 0418 4502 0005 1332",
}
IBAN_CONTEXTS = {**CONTEXTS, "label-glued": "IBAN:{}", "label-upper": "IBAN {}."}


@pytest.mark.parametrize(
    ("kind", "secret", "text", "restores"), combos(Kind.IBAN, IBANS, IBAN_CONTEXTS)
)
def test_iban_is_hidden_in_unusual_surroundings(kind, secret, text, restores):
    assert_hidden(text, secret, kind, restores)


NATIONAL_IDS = {
    "dni-label": ("DNI ", "12345678Z"),
    "dni-dash": ("DNI ", "12345678-Z"),
    "nie": ("NIE ", "Y1234567X"),
    "pesel": ("PESEL ", "90010112349"),
    "pesel-2000s": ("PESEL ", "02210312342"),
    "steuer-id": ("Steuer-ID ", "86095742719"),
    "lithuanian": ("asmens kodas ", "39001011237"),
    "cnp": ("CNP ", "1900101123457"),
    "nir-spaced": ("NIR ", "1 85 05 78 006 084 91"),
    "nir-compact": ("numéro ", "185057800608491"),
    "codice-fiscale": ("codice fiscale ", "RSSMRA85T10A562S"),
    "rodne-cislo": ("rodné číslo ", "905101/1002"),
    "nip": ("NIP: ", "123-456-32-18"),
    "ua-tax-id": ("РНОКПП ", "3000000008"),
}
ID_CONTEXTS = {
    "alone": "{}",
    "ascii-quotes": '"{}"',
    "curly-quotes": "\u201c{}\u201d",
    "parentheses": "({})",
    "square-brackets": "[{}]",
    "commas": "a, {}, b",
    "semicolon": "{}; next",
    "sentence-end": "Ось: {}.",
    "newlines": "a\n{}\nb",
    "tabs": "a\t{}\tb",
    "nbsp": "a\u00a0{}\u00a0b",
    "rtl-marks": "\u200f{}\u200f",
    "emoji": "\U0001f4ce{}\U0001f4ce",
    "cyrillic-dashes": "Дані\u2014{}\u2014дякую",
    "markdown-bold": "**{}**",
    "json": '{{"id": "{}"}}',
    "prose": "Привіт! Це {} \u2014 і ще трохи тексту.",
    "list": "- a\n- {}\n- b",
}


@pytest.mark.parametrize(
    ("label", "number", "template", "restores"),
    [
        pytest.param(label, number, template, cname not in BRACKETED, id=f"{name}-{cname}")
        for name, (label, number) in NATIONAL_IDS.items()
        for cname, template in ID_CONTEXTS.items()
    ],
)
def test_national_numbers_are_hidden_next_to_other_text(label, number, template, restores):
    text = template.format(label + number)
    masker = Masker()
    masked = masker.mask(text)
    assert not leaked(masked, number), masked
    if restores:
        assert masker.unmask(masked) == text


@pytest.mark.parametrize(
    "number", ["90010112349", "86095742719", "39001011237", "1900101123457", "3000000008"]
)
@pytest.mark.parametrize("digits", [fullwidth, arabic_indic], ids=["fullwidth", "arabic-indic"])
def test_unicode_digit_national_numbers_leave_no_digits(number, digits):
    text = f"PESEL {digits(number)} i ID {digits(number)}"
    masked = Masker().mask(text)
    assert not leaked(masked, number), masked


@pytest.mark.parametrize(
    "text",
    [
        "PESEL 90010112349 Steuer-ID 86095742719\nCNP 1900101123457",
        "PESEL 90010112349, 86095742719, 39001011237",
        "90010112349\n86095742719\n39001011237",
        "Klient: Jan Kowalski; PESEL: 90010112349; NIP: 123-456-32-18; DNI 12345678Z.",
    ],
)
def test_several_numbers_in_one_text_all_go(text):
    masker = Masker()
    masked = masker.mask(text)
    for number in re.findall(r"\d{3}-\d{3}-\d{2}-\d{2}|\d{10,13}|\d{8}[A-Z]", text):
        assert not leaked(masked, number), masked
    assert masker.unmask(masked) == text


IPS = {"private": "192.168.10.25", "public": "203.0.113.7", "loopback": "127.0.0.1"}
IP_CONTEXTS = {
    "alone": "{}",
    "ascii-quotes": '"{}"',
    "parentheses": "({})",
    "square-brackets": "[{}]",
    "comma": "{}, ok",
    "port": "{}:8080",
    "url": "http://{}:8080/x",
    "url-path": "https://{}/a/b",
    "colon-label": "IP:{}",
    "newlines": "a\n{}\nb",
    "nbsp": "a\u00a0{}\u00a0b",
    "rtl-marks": "\u200f{}\u200f",
    "cyrillic-dashes": "Сервер\u2014{}\u2014працює",
    "semicolon": "{};",
}


@pytest.mark.parametrize(("kind", "secret", "text", "restores"), combos(Kind.IP, IPS, IP_CONTEXTS))
def test_ip_is_hidden_in_unusual_surroundings(kind, secret, text, restores):
    assert_hidden(text, secret, kind, restores)


SECRETS = {
    "openai-style": "sk-FAKEKEYFAKEKEYFAKEKEY0123",
    "anthropic-style": "sk-ant-FAKEKEYFAKEKEYFAKEKEY0123",
    "github-token": "ghp_" + "FAKEKEY0123" * 4,
    "github-pat": "github_pat_" + "FAKEKEY0123" * 5,
    "slack-token": "xoxb-FAKEKEY0123-FAKEKEY0123",
    "aws-key-id": "AKIA" + "FAKEKEY012345678",
    "google-key": "AIza" + "FAKEKEY0123FAKEKEY0123FAKEKEY0123XY",
}
SECRET_CONTEXTS = {
    "alone": "{}",
    "ascii-quotes": '"{}"',
    "single-quotes": "'{}'",
    "backticks": "`{}`",
    "parentheses": "({})",
    "equals": "key={}",
    "env-file": "API_KEY={}\n",
    "json": '{{"key": "{}"}}',
    "commas": "a,{},b",
    "newlines": "a\n{}\nb",
    "nbsp": "a\u00a0{}\u00a0b",
    "rtl-marks": "\u200f{}\u200f",
    "emoji": "\U0001f511{}\U0001f511",
    "url-query": "https://example.org/x?token={}&z=1",
    "sentence-end": "My key is {}.",
    "cyrillic": "Мій ключ \u2014 {} \u2014 не показуй.",
}


@pytest.mark.parametrize(
    ("kind", "secret", "text", "restores"), combos(Kind.SECRET, SECRETS, SECRET_CONTEXTS)
)
def test_key_is_hidden_in_unusual_surroundings(kind, secret, text, restores):
    assert_hidden(text, secret, kind, restores)


PASSWORD_LINES = {
    "label-uk": "Пароль: FakePass-7731",
    "label-uk-upper": "ПАРОЛЬ: FakePass-7731",
    "label-en": "password: FakePass-7731",
    "label-dash": "Password - FakePass-7731",
    "label-bullet": "- Password: FakePass-7731",
    "label-tab-indent": "\t* Пароль: FakePass-7731",
    "label-quote": "> Пароль: FakePass-7731",
    "label-bold": "**Пароль:** FakePass-7731",
    "label-nbsp": "пароль:\u00a0FakePass-7731",
    "label-crlf": "Пароль: FakePass-7731\r\nдалі",
    "label-zero-width-inside": "Пароль: Fake\u200bPass-7731",
    "label-rtl-mark": "Пароль: \u200fFakePass-7731",
    "label-pin": "PIN: 482915",
    "label-pin-code": "PIN-код: 482915",
    "json": '{"password": "FakePass-7731"}',
    "sentence-uk": "мій пароль від пошти FakePass-7731",
    "sentence-en": "my password is FakePass-7731!",
    "sentence-de": "mein Passwort ist FakePass-7731",
    "sentence-fr": "mon mot de passe est FakePass-7731",
    "sentence-pl": "moje hasło to FakePass-7731",
    "sentence-quoted": "my password is \u201cFakePass-7731\u201d.",
}


@pytest.mark.parametrize(("name", "text"), list(PASSWORD_LINES.items()))
def test_password_is_hidden_in_unusual_forms(name, text):
    masker = Masker()
    masked = masker.mask(text)
    secret = "482915" if "PIN" in text else "FakePass-7731"
    assert not leaked(masked, secret), masked
    assert "[SECRET_1]" in masked
    assert masker.unmask(masked) == text


@pytest.mark.parametrize(
    "token",
    ["FAKETOKENFAKETOKEN0123456789", "FAKE.TOKEN-FAKE_TOKEN~0123456789+/="],
)
@pytest.mark.parametrize("context", ["{}", "x: {}.", "{}\n"])
def test_bearer_token_is_hidden(token, context):
    text = context.format(f"Authorization: Bearer {token}")
    masked = Masker().mask(text)
    assert "FAKETOKEN" not in masked.replace("FAKE.TOKEN", "FAKETOKEN") or not leaked(masked, token)
    assert not leaked(masked, token), masked


def test_password_in_brackets_is_hidden():
    masked = Masker().mask("(password FakePass-7731) [pin 482915]")
    assert not leaked(masked, "FakePass-7731")
    assert "482915" not in masked


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\u2028"])
def test_private_key_block_is_hidden_whatever_the_newlines(newline):
    body = newline.join(
        ["-----BEGIN PRIVATE KEY-----", "FAKEFAKE0123", "FAKEFAKE4567", "-----END PRIVATE KEY-----"]
    )
    text = f"before{newline}{body}{newline}after"
    masker = Masker()
    masked = masker.mask(text)
    assert "FAKEFAKE" not in masked
    assert masked.startswith("before") and masked.endswith("after")
    assert masker.unmask(masked) == text


PROFILES = {
    "telegram": "t.me/olena_test",
    "telegram-https": "https://t.me/olena_test",
    "linkedin": "https://www.linkedin.com/in/olena-test",
    "github": "github.com/olena-test",
    "instagram": "https://instagram.com/olena.test/",
}
PROFILE_CONTEXTS = {
    "alone": "{}",
    "ascii-quotes": '"{}"',
    "parentheses": "({})",
    "angle-brackets": "<{}>",
    "commas": "a,{},b",
    "nbsp": "a\u00a0{}\u00a0b",
    "rtl-marks": "\u200f{}\u200f",
    "markdown-link": "[me]({})",
    "cyrillic-dashes": "Профіль\u2014{}\u2014ось",
}


@pytest.mark.parametrize(
    ("kind", "secret", "text", "restores"), combos(Kind.PROFILE, PROFILES, PROFILE_CONTEXTS)
)
def test_profile_link_is_hidden_in_unusual_surroundings(kind, secret, text, restores):
    masker = Masker()
    masked = masker.mask(text)
    assert not leaked(masked, secret.split("/")[-1].strip("/") or secret, run=5), masked
    if restores:
        assert masker.unmask(masked) == text


LABELLED = {
    "name-uk": ("Ім'я: ", "Тестенко Олена-Марія"),
    "name-pl": ("Name: ", "Łukasz Żukowski"),
    "name-lt": ("Name: ", "Žemaitė O'Brien"),
    "name-ro": ("Name: ", "Ștefan Țărănescu"),
    "name-rtl": (
        "Name: ",
        "\u0645\u062d\u0645\u062f \u0627\u0644\u062a\u062c\u0631\u064a\u0628\u064a",
    ),
    "name-cjk": ("Name: ", "山田太郎"),
    "name-emoji": ("Name: ", "Test \U0001f600 Person"),
    "name-zero-width": ("Name: ", "Test\u200bPerson Zalgo\u0301\u0302"),
    "birth": ("Дата народження: ", "01.01.1990"),
    "address": ("Адреса: ", "вул. Тестова 1, кв. 2, м. Тестове"),
}
LABELLED_FORMS = {
    "plain": "{label}{value}",
    "upper-label": "{label_upper}{value}",
    "bullet": "- {label}{value}",
    "indented": "  \t{label}{value}",
    "crlf": "{label}{value}\r\nnext line",
    "after-lines": "first\nsecond\n{label}{value}\nlast",
}


@pytest.mark.parametrize(
    ("value", "text"),
    [
        pytest.param(
            value,
            form.format(label=label, label_upper=label.upper(), value=value),
            id=f"{name}-{fname}",
        )
        for name, (label, value) in LABELLED.items()
        for fname, form in LABELLED_FORMS.items()
    ],
)
def test_labelled_values_are_hidden_with_any_script(value, text):
    masker = Masker()
    masked = masker.mask(text)
    assert not leaked(masked, value, run=4), masked
    assert masker.unmask(masked) == text


def test_every_kind_is_hidden_in_one_hostile_document():
    document = (
        f"\u202e{EMAIL}\u202c {PHONE}, {CARD}\n"
        f"IBAN:{IBAN};\n"
        "Name: Łukasz\nПароль: FakePass-7731\nPESEL 90010112349\n"
        "IP 192.168.10.25 t.me/olena_test\n"
        "Authorization: Bearer FAKETOKENFAKETOKEN0123456789\n"
    )
    found = {f.kind for f in detect(document)}
    assert found >= {
        Kind.EMAIL,
        Kind.PHONE,
        Kind.CARD,
        Kind.IBAN,
        Kind.NAME,
        Kind.SECRET,
        Kind.NATIONAL_ID,
        Kind.IP,
        Kind.PROFILE,
    }
    masker = Masker()
    masked = masker.mask(document)
    for secret in (EMAIL, PHONE, CARD, IBAN, "ukasz", "FakePass", "90010112349", "192.168.10.25"):
        assert not leaked(masked, secret), secret
    assert masker.unmask(masked) == document


def test_strict_mode_hides_every_checked_number_in_odd_surroundings():
    masker = Masker(strict=True)
    for template in CONTEXTS.values():
        text = template.format("замовлення 90010112349 відправлено")
        masked = masker.mask(text)
        assert "90010112349" not in masked, masked
        assert masker.unmask(masked) == text


# --- 3. injection ---------------------------------------------------------------------

LOOKALIKES = [
    "[EMAIL_9]",
    "[EMAIL_10]",
    "[NAME_2]",
    "[PHONE_7]",
    "[PHONE_1]",
    "[SECRET_3]",
    "[UNKNOWN_1]",
    "{PHONE_1}",
    "{EMAIL_9}",
    "[[CARD_4]]",
    "[Email_9]",
    "[ПОШТА_1]",
    "(IBAN_2)",
    "EMAIL_9",
    "PHONE_1",
]


@pytest.mark.parametrize("lookalike", LOOKALIKES)
def test_person_written_placeholder_for_another_value_is_not_filled_in(lookalike):
    masker = Masker()
    text = f"Лист для {EMAIL}. У тексті є {lookalike}, не чіпати."
    masked = masker.mask(text)
    assert masked == f"Лист для [EMAIL_1]. У тексті є {lookalike}, не чіпати."
    assert masker.unmask(masked) == text
    assert masker.unmask(lookalike) == lookalike
    assert "".join(masker.unmask_stream([lookalike])) == lookalike


@pytest.mark.parametrize("lookalike", LOOKALIKES)
def test_masker_without_values_changes_nothing(lookalike):
    masker = Masker()
    assert masker.mask(lookalike) == lookalike
    assert masker.unmask(lookalike) == lookalike


def test_lookalike_does_not_pull_in_a_value_of_another_kind():
    masker = Masker()
    masker.mask(f"{EMAIL} {PHONE} Password: FakePass-7731")
    answer = "[NAME_1] [CARD_1] [IP_1] [EMAIL_2] [PHONE_2] [SECRET_2] [IBAN_1]"
    restored = masker.unmask(answer)
    assert restored == answer
    for secret in (EMAIL, PHONE, "FakePass"):
        assert secret not in restored
    assert len(masker.unresolved(answer)) == 7


def test_another_masker_cannot_restore_these_placeholders():
    mine, other = Masker(), Masker()
    masked = mine.mask(f"{EMAIL} {PHONE}")
    assert other.unmask(masked) == masked
    other.mask("ivan@example.org")
    assert other.unmask(masked) == masked.replace("[EMAIL_1]", "ivan@example.org")
    assert EMAIL not in other.unmask(masked)


def test_placeholders_do_not_cross_conversations():
    first, second = Masker(), Masker()
    first.mask(f"{EMAIL}")
    second.mask("ivan@example.org")
    answer = first.mask(f"{EMAIL}")
    assert second.unmask(answer) == "ivan@example.org"
    assert EMAIL not in second.unmask(answer)


def test_masked_text_masked_again_is_stable():
    masker = Masker()
    text = f"{EMAIL}, {PHONE}, {CARD}\nName: Łukasz"
    once = masker.mask(text)
    assert masker.mask(once) == once
    assert masker.mask(masker.mask(once)) == once
    assert len(masker) == 4


def test_value_that_looks_like_a_changed_placeholder_comes_back_as_written():
    masker = Masker()
    text = f"{EMAIL}\nName: {{Email_1}}\nAddress: (phone 1)"
    masked = masker.mask(text)
    assert masked == "[EMAIL_1]\nName: [NAME_1]\nAddress: [ADDRESS_1]"
    assert masker.unmask(masked) == text


def test_model_answer_inside_markup_still_restores_only_ours():
    masker = Masker()
    masker.mask(EMAIL)
    answer = '<a href="x">[EMAIL_1]</a> `[EMAIL_2]` **[EMAIL_1]** "{PHONE_1}"'
    assert masker.unmask(answer) == f'<a href="x">{EMAIL}</a> `[EMAIL_2]` **{EMAIL}** "{{PHONE_1}}"'


@pytest.mark.parametrize("seed", range(5))
def test_stream_and_whole_agree_on_exact_placeholders(seed):
    rng = random.Random(seed)
    masker = Masker()
    masker.mask(f"{EMAIL} {PHONE} ivan@example.org")
    parts = ["Лист для ", "[EMAIL_1]", "[PHONE_1]", "[EMAIL_2]", ", ", ". ", " ", "\n", "[ok]"]
    parts += [
        "\U0001f600",
        "text",
        "[EMAIL_9]",
        "(x)",
        "{y}",
        "\u2014",
        "[UNKNOWN_3]",
        "\u200b",
        "і ",
    ]
    for _ in range(300):
        answer = "".join(rng.choice(parts) for _ in range(rng.randint(0, 12)))
        chunks, pos = [], 0
        while pos < len(answer):
            step = rng.randint(1, 6)
            chunks.append(answer[pos : pos + step])
            pos += step
        assert "".join(masker.unmask_stream(chunks)) == masker.unmask(answer), chunks


@pytest.mark.parametrize("seed", range(3))
def test_concealed_answer_never_shows_a_real_value(seed):
    rng = random.Random(100 + seed)
    masker = Masker()
    masker.mask(f"{EMAIL} {PHONE} Password: FakePass-7731")
    garbage = list("[]{}()_ -\n") + [
        "EMAIL",
        "PHONE",
        "SECRET",
        "1",
        "2",
        "email",
        "Email_1",
        "\u200b",
    ]
    for _ in range(500):
        answer = "".join(rng.choice(garbage) for _ in range(rng.randint(0, 30)))
        chunks, pos = [], 0
        while pos < len(answer):
            step = rng.randint(1, 5)
            chunks.append(answer[pos : pos + step])
            pos += step
        for shown in (masker.conceal(answer), "".join(masker.conceal_stream(chunks))):
            for secret in ("olena", "+380", "FakePass"):
                assert secret not in shown


# --- 4. time ---------------------------------------------------------------------------

HOSTILE_MEGABYTES = {
    "at-signs": "@" * MEGABYTE,
    "at-signs-100k": "@" * 100_000,
    "a-at-pairs": "a@" * (MEGABYTE // 2),
    "dotted-local-then-at": "a." * (MEGABYTE // 2) + "@",
    "dots": "." * MEGABYTE,
    "digit-dot-pairs": "1." * (MEGABYTE // 2),
    "spaced-digits": "1 " * (MEGABYTE // 2),
    "spaced-digit-groups": "1234567890 " * (MEGABYTE // 11),
    "plus-signs": "+" * MEGABYTE,
    "parenthesised-digits": "(1" * (MEGABYTE // 2),
    "opening-brackets": "[" * MEGABYTE,
    "unclosed-placeholders": "[A_" * (MEGABYTE // 3),
    "placeholder-heads": "[EMAIL_" * (MEGABYTE // 7),
    "slashes": "/" * MEGABYTE,
    "newlines": "\n" * MEGABYTE,
    "tabs": "\t" * MEGABYTE,
    "label-then-spaces": "name" + " " * MEGABYTE,
    "password-words": "password " * (MEGABYTE // 9),
    "http-prefixes": "http://" * (MEGABYTE // 7),
    "domain-prefixes": "github.com/" * (MEGABYTE // 11),
    "thousand-groups": "1,000" * (MEGABYTE // 5),
    "date-like": "1/1/1 " * (MEGABYTE // 6),
    "nbsp-digits": "1\u00a0" * (MEGABYTE // 2),
    "zero-width": "\u200b" * MEGABYTE,
    "combining": "a" + "\u0301" * MEGABYTE,
    "rtl-overrides": "\u202e" * MEGABYTE,
    "emoji": "\U0001f600" * (MEGABYTE // 2),
    "nul-bytes": "\0" * MEGABYTE,
    "bearer-prefixes": "Bearer " * (MEGABYTE // 7),
}


@pytest.mark.parametrize("name", list(HOSTILE_MEGABYTES))
def test_a_megabyte_of_hostile_text_is_processed_in_time(name):
    text = HOSTILE_MEGABYTES[name]
    masker = Masker()
    with within_limit():
        masked = masker.mask(text)
        restored = masker.unmask(masked)
    assert restored == text


@pytest.mark.parametrize("name", ["opening-brackets", "placeholder-heads", "at-signs-100k"])
def test_hostile_stream_is_processed_in_time(name):
    text = HOSTILE_MEGABYTES[name]
    masker = Masker()
    masker.mask(EMAIL)
    with within_limit():
        big = "".join(masker.unmask_stream([text]))
        small = "".join(masker.unmask_stream(text[i : i + 997] for i in range(0, len(text), 997)))
        tiny = "".join(masker.conceal_stream(text[i : i + 3] for i in range(0, 60_000, 3)))
    assert big == text
    assert small == text
    assert tiny == masker.conceal(text[:60_000])


def test_long_runs_of_letters_and_digits_are_processed_in_time():
    # Long unbroken runs of one character must neither raise nor change.
    masker = Masker()
    with within_limit():
        for run in ("x", "1", "д", "a-", "1-"):
            text = run * 5000
            assert masker.unmask(masker.mask(text)) == text


def test_many_values_in_a_big_text_are_processed_in_time():
    text = "\n".join(f"user{i}@example.org" for i in range(5000)) + "\n" + "tail " * 100_000
    assert len(text) > MEGABYTE // 2
    masker = Masker()
    with within_limit():
        masked = masker.mask(text)
        restored = masker.unmask(masked)
    assert "@" not in masked
    assert len(masker) == 5000
    assert restored == text


def test_changed_placeholders_in_a_megabyte_answer_are_restored_in_time():
    masker = Masker()
    masker.mask(" ".join(f"user{i}@example.org" for i in range(10)))
    answer = "[Emial_1] [EMAIL_3] {Email 7} EMAIL_9 [EMAIL_77] " * 20000
    with within_limit():
        restored = masker.unmask(answer)
        left = masker.unresolved(answer)
    assert "@example.org" in restored
    assert "[EMAIL_77]" in restored
    assert left
