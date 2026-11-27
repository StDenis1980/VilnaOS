# SPDX-License-Identifier: GPL-3.0-or-later
"""Hostile and odd input at the installer's text entry points, without GTK and without a disk.

What a person (or a model speaking for them) can type is checked at the places where it is
turned into something else: a login and a computer name (``wizard.suggest_username``,
``wizard.suggest_hostname``, ``conversation.latin_name``), a person's name and short answers
(``conversation._person_name``, ``_agrees``, ``_says``), text typed in the wrong keyboard
layout (``conversation.wrong_layout``), the recovery key typed back (``encryption.normalize``,
``check_group``, ``groups``, ``spoken``) and the manual layout (``layout.items``, ``target``,
``problems``, the wizard's report).

Everything is plain strings, dicts and temporary directories. The "computer" is the synthetic
``DemoMachine``: no real disk, partition, user or system file is read or written.

Run (from ``installer/``): ``uv run pytest tests/test_hostile_input.py``.
"""

from __future__ import annotations

import random
import re
import unicodedata

import pytest
from vilna_mcp_installer import plan as plans
from vilna_mcp_installer.machine import DemoMachine

from vilna_installer import conversation as conv
from vilna_installer import encryption, layout, start
from vilna_installer import wizard as wiz

# A Linux login name: a lower-case letter or "_" first, then at most 31 more (``useradd``).
LOGIN = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
# A host name label of RFC 1123: letters, digits and "-", 1 to 63, no "-" at either end.
LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")

# Names in the installer's languages (ca, ca@valencia, cs, de, es, eu, fr, gl, it, lt, pl, pt,
# ro, uk and en), each one starting with a Latin or Ukrainian letter.
NAMES = [
    "Łukasz Wójcik",
    "Ñúñez",
    "Žemaitė Šarūnė",
    "Ștefan Țepeș",
    "Øystein",
    "Ğ",
    "O'Brien",
    "Anne-Marie Smith-Jones",
    "Олена Коваль",
    "Іван Шевченко-Мазепа",
    "Ґанна Їжак",
    "Євгеній Щербак",
    "Jürgen Müller-Weiß",
    "François Lefèvre",
    "Iñaki Etxeberria",
    "João Conceição",
    "Jiří Šťastný",
    "Núria Çelma",
    "Xoán Pérez",
    "Niccolò Rossi",
    "Małgorzata Żak",
    "Vicent Ferrer",
    "Zoé Cœur",
    "Ąžuolas Čiurlionis",
]

# What ``suggest_username`` makes of some of them (checked by hand).
SUGGESTED = {
    "Łukasz Wójcik": "lukasz",
    "Ñúñez": "nunez",
    "Žemaitė Šarūnė": "zemaite",
    "Ștefan Țepeș": "stefan",
    "Øystein": "oystein",
    "Ğ": "g",
    "O'Brien": "obrien",
    "Олена Коваль": "olena",
    "Ґанна Їжак": "ganna",
    "Євгеній Щербак": "ievhenii",
    "Jürgen Müller-Weiß": "jurgen",
    "João Conceição": "joao",
}

# Scripts with no transliteration here, and strings made to break things.
HOSTILE = [
    "Ελένη Παπαδοπούλου",
    "გიორგი ბერიძე",
    "李小龍",
    "山田太郎",
    "محمد علي",
    "שלום",
    "😀",
    "😀😀 👨‍👩‍👧",
    "",
    " ",
    "   \t\n  ",
    "a" * 10_000,
    "ab " * 5_000,
    "\0",
    "\0abc",
    "ab\0c",
    "abc\0",
    "\x07\x1b[31mred\x1b[0m",
    "\u202eevil",
    "\u200b",
    "\ufeffAnna",
    "1234 Bob",
    "_under_score",
    "-dash",
    "Robert'); DROP TABLE users;--",
    "$(reboot)",
    "`id`",
    "../../etc/passwd",
    "; rm -rf /",
    "a:b,c\nd",
    "root",
    "ROOT",
    " root ",
    "r\u200boot",
    "ｒｏｏｔ",  # full-width
    "Rооt",  # Cyrillic "о" twice
    "gloxi-admin",
    "Gloxi",
    "\ud800",  # a lone surrogate
]


def sweep() -> list[str]:
    """Every Unicode scalar value once (surrogates are not text)."""
    return [chr(cp) for cp in range(0x110000) if not 0xD800 <= cp < 0xE000]


def sweep_chunks(size: int = 24) -> list[str]:
    """The same code points in runs of ``size``, so a function sees each of them in company."""
    every = sweep()
    return ["".join(every[i : i + size]) for i in range(0, len(every), size)]


def fuzz(seed: int, count: int) -> list[str]:
    """Reproducible random text from letters, marks, punctuation, controls and emoji."""
    rng = random.Random(seed)
    pools = [
        "abcdefghijklmnopqrstuvwxyz ABCXYZ0123456789",
        "łŁñÑžŽșȘøØğĞéèüöäßæœþđçįųėū",
        "абвгґдеєжзиіїйклмнопрстуфхцчшщьюяАБВГҐЄІЇЩЬЮЯ'’ʼ",
        "ΑΒΓαβγ გიორგი 李小龍 محمد שלום",
        ".,;:!?-–—_/\\'\"`<>[]{}()|&$%#@*+=~^",
        "\0\t\n\r\x07\x1b\x7f\u200b\u200d\u202e\ufeff\u0301\u030c\u00a0\u2003",
        "😀🙂👍🇺🇦👨‍👩‍👧",
    ]
    out = []
    for _ in range(count):
        picked = [rng.choice(pools) for _ in range(rng.randint(1, 3))]
        out.append("".join(rng.choice(rng.choice(picked)) for _ in range(rng.randint(0, 40))))
    return out


EVERYTHING = HOSTILE + NAMES + fuzz(20261130, 1500)


def new_wizard(tmp_path, kind="empty") -> wiz.Wizard:
    path = tmp_path / "plan.json"
    start.write_plan(start.start_plan("uk", False, False), path)
    return wiz.Wizard(path, DemoMachine(kind))


# --- login and computer names -----------------------------------------------------------


@pytest.mark.parametrize("name", EVERYTHING)
def test_a_suggested_login_is_valid_free_or_nothing(name):
    login = wiz.suggest_username(name)
    if login == "":
        return
    assert LOGIN.fullmatch(login), login
    assert login != "root"
    assert login not in plans.RESERVED_USERS
    assert not login.startswith("gloxi-")
    assert plans.USERNAME_RE.match(login)


@pytest.mark.parametrize("name", NAMES)
def test_a_name_in_a_latin_or_ukrainian_script_always_gets_a_suggestion(name):
    assert wiz.suggest_username(name) != ""


@pytest.mark.parametrize(("name", "login"), SUGGESTED.items())
def test_names_of_the_installers_languages_lose_only_their_marks(name, login):
    assert wiz.suggest_username(name) == login


def test_every_code_point_alone_gives_a_valid_login_or_nothing():
    for ch in sweep():
        for text in (ch, "a" + ch):
            login = wiz.suggest_username(text)
            assert login == "" or LOGIN.fullmatch(login), (hex(ord(ch)), login)
            assert login not in plans.RESERVED_USERS


@pytest.mark.parametrize("reserved", sorted(plans.RESERVED_USERS))
def test_a_reserved_name_is_never_suggested_however_it_is_written(reserved):
    for text in (
        reserved,
        reserved.upper(),
        reserved.title(),
        f" {reserved}  ",
        "\u200b".join(reserved),
        reserved.replace("o", "о"),  # Cyrillic "о"
        reserved.replace("a", "а"),  # Cyrillic "а"
        "".join(chr(0xFF00 + ord(c) - 0x20) if c.isalpha() else c for c in reserved),  # full-width
    ):
        login = wiz.suggest_username(text)
        assert login not in plans.RESERVED_USERS, (text, login)
        assert not login.startswith("gloxi-")


def test_a_login_cannot_start_with_a_digit_and_only_the_first_word_counts():
    assert wiz.suggest_username("1234 Bob") == ""
    assert wiz.suggest_username("Bob 1234") == "bob"
    assert wiz.suggest_username("  Anna   Maria  ") == "anna"


def test_a_very_long_name_is_cut_to_a_valid_login():
    login = wiz.suggest_username("a" * 10_000)
    assert login == "a" * 31
    assert LOGIN.fullmatch(login)
    assert wiz.suggest_username("Ab" * 5_000 + " x") == ("ab" * 16)[:31]


@pytest.mark.parametrize("name", NAMES + HOSTILE)
def test_a_suggested_host_name_is_a_rfc_1123_label_up_to_63_characters(name):
    login = wiz.suggest_username(name)
    host = wiz.suggest_hostname(login)
    assert LABEL.fullmatch(host), host
    assert len(host) <= 63
    assert plans.HOSTNAME_RE.match(host)


def test_the_host_name_of_no_login_is_a_fixed_valid_one():
    assert wiz.suggest_hostname("") == "vilna"
    assert LABEL.fullmatch(wiz.suggest_hostname(""))


def test_the_longest_valid_login_still_makes_a_valid_host_name():
    longest = "a" * 31
    assert plans.USERNAME_RE.match(longest)
    host = wiz.suggest_hostname(longest)
    assert len(host) <= 63 and LABEL.fullmatch(host)


@pytest.mark.parametrize("text", EVERYTHING)
def test_latin_is_small_latin_letters_digits_and_single_spaces(text):
    out = wiz.latin(text)
    assert re.fullmatch(r"[a-z0-9]+( [a-z0-9]+)*|", out), out


def test_latin_loses_no_letter_of_the_installers_languages():
    assert wiz.latin("Łukasz") == "lukasz"
    assert wiz.latin("Ñúñez") == "nunez"
    assert wiz.latin("Ğ") == "g"
    assert wiz.latin("Straße") == "strasse"
    assert wiz.latin("Æsa Œuvre Þór Đorđe") == "aesa oeuvre thor dorde"
    assert wiz.latin("Ｒｏｏｔ") == "root"


def test_latin_and_the_suggestions_survive_every_code_point():
    for text in sweep_chunks():
        assert isinstance(wiz.latin(text), str)
        wiz.suggest_hostname(wiz.suggest_username(text))


@pytest.mark.parametrize("name", EVERYTHING)
def test_the_conversation_login_is_a_letter_then_letters_digits_and_dashes(name):
    out = conv.latin_name(name)
    assert re.fullmatch(r"[a-z][a-z0-9-]{0,30}|", out), out
    assert not out.endswith("-")
    assert "--" not in out


def test_the_conversation_login_of_a_typical_name():
    assert conv.latin_name("Олена Коваль") == "olena-koval"
    assert conv.latin_name("Łukasz") == "lukasz"
    assert conv.latin_name("1234") == ""
    assert conv.latin_name("") == ""
    assert conv.latin_name("   ") == ""
    assert conv.latin_name("a" * 10_000) == "a" * 31
    assert conv.latin_name("\0") == ""


def test_the_conversation_login_survives_every_code_point():
    for text in sweep_chunks():
        conv.latin_name(text)
        conv.latin_name("a" + text + "b")


def test_a_login_taken_by_the_system_is_not_free():
    for taken in sorted(plans.RESERVED_USERS) + ["gloxi-x", "gloxi-"]:
        assert not conv._free_login(taken), taken
    assert conv._free_login("olena")


def test_the_models_names_are_made_latin_and_nothing_else_is_touched():
    args = {"full_name": "Тест", "username": "Тестер", "hostname": "Тестер-pc", "x": ["y"]}
    before = dict(args)
    out = conv._latin_names(args)
    assert args == before
    assert out["username"] == "tester" and out["hostname"] == "tester-pc"
    assert out["full_name"] == "Тест" and out["x"] == ["y"]
    for odd in (None, 5, ["a"], {"a": 1}, "", "ok-name_1"):
        kept = conv._latin_names({"username": odd, "hostname": odd})
        assert kept == {"username": odd, "hostname": odd}
    assert conv._latin_names({}) == {}


# --- the classic wizard saving a name, hostile ------------------------------------------


@pytest.mark.parametrize(
    "login",
    [
        "",
        " ",
        "Root",
        "root",
        "admin",
        "live",
        "gloxi-bot",
        "1abc",
        "-abc",
        "ab c",
        " abc",
        "abc ",
        "ABC",
        "a" * 32,
        "a" * 10_000,
        "ґанна",
        "олена",
        "a/b",
        "../x",
        "a;b",
        "a$(id)",
        "a`id`",
        "a\0",
        "\0a",
        "a:b",
        "a,b",
        "ａｂｃ",
        "😀",
    ],
)
def test_the_wizard_refuses_a_hostile_login_and_keeps_the_plan(tmp_path, login):
    w = new_wizard(tmp_path)
    assert w.set_user("Olena", login, "olena-vilna") in (wiz.BAD_USERNAME, wiz.TAKEN_USERNAME)
    assert "user" not in w.plan


@pytest.mark.parametrize(
    "host",
    [
        "",
        "-a",
        "a-",
        "A",
        "Olena-PC",
        "a_b",
        "a b",
        "a.b",
        "a..b",
        ".a",
        "a" * 64,
        "a" * 10_000,
        "олена",
        "a/b",
        "../x",
        "a\0",
        "a;b",
        "😀",
    ],
)
def test_the_wizard_refuses_a_hostile_computer_name_and_keeps_the_plan(tmp_path, host):
    w = new_wizard(tmp_path)
    assert w.set_user("Olena", "olena", host) == wiz.BAD_HOSTNAME
    assert "user" not in w.plan


@pytest.mark.parametrize("full_name", ["", " ", "\t\n ", "\u00a0"])
def test_the_wizard_wants_a_name_even_if_it_is_only_blanks(tmp_path, full_name):
    w = new_wizard(tmp_path)
    assert w.set_user(full_name, "olena", "olena-vilna") == wiz.NAME_NEEDED
    assert "user" not in w.plan


def test_the_wizard_refuses_a_name_over_the_limit_and_takes_the_longest_one(tmp_path):
    w = new_wizard(tmp_path)
    assert w.set_user("a" * 129, "olena", "olena-vilna") is not None
    assert "user" not in w.plan
    assert w.set_user("a" * 128, "olena", "olena-vilna") is None
    assert w.plan["user"]["full_name"] == "a" * 128


@pytest.mark.parametrize("name", NAMES)
def test_the_wizard_takes_a_name_with_its_suggested_login_and_computer_name(tmp_path, name):
    w = new_wizard(tmp_path)
    login = wiz.suggest_username(name)
    assert w.set_user(name, login, wiz.suggest_hostname(login)) is None
    user = w.plan["user"]
    assert user["full_name"] == name and user["username"] == login


# --- a person's name in the conversation ------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_a_real_name_is_taken_as_it_is(name):
    assert conv._person_name(name) == name


def test_the_words_before_the_name_are_left_out():
    assert conv._person_name("Мене звати Олена") == "Олена"
    assert conv._person_name("my name is Łukasz Wójcik") == "Łukasz Wójcik"
    assert conv._person_name("I'm O'Brien.") == "O'Brien"
    assert conv._person_name("  Олена   Коваль!  ") == "Олена Коваль"
    assert conv._person_name("Anna\nKowalska") == "Anna Kowalska"


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "\t\n",
        "\0",
        "Anna\0",
        "\0Anna",
        "An\x07na",
        "An\x1bna",
        "😀",
        "Anna 😀",
        "12345",
        "Anna 2",
        "R2D2",
        "Anna;Bob",
        "Anna, Bob",
        "<script>alert(1)</script>",
        "Robert'); DROP TABLE users;--",
        "$(reboot)",
        "../../etc/passwd",
        "Anna?",
        "Хто ти?",
        "¿Quién?",
        "Anna？",
        "One Two Three Four Five",
        "a" * 61,
        "a" * 10_000,
        "ab " * 5_000,
        "так",
        "Так.",
        "Yes",
        "OK",
        "ні",
        "no",
        "skip",
        "Далі",
        "later",
        "Why",
        "а навіщо?",
        "не знаю",
        "-",
        "'",
        "'-'",
        "Anna\u200bKowalska",
        "Anna\u202e",
    ],
)
def test_what_is_not_a_name_is_not_taken_as_one(text):
    assert conv._person_name(text) is None


def test_a_name_stops_at_sixty_characters_and_four_words():
    assert conv._person_name("a" * 60) == "a" * 60
    assert conv._person_name("ab ab ab ab") == "ab ab ab ab"
    assert conv._person_name("ab ab ab ab ab") is None


def test_scripts_without_a_latin_form_are_still_names_for_the_plan():
    for name in ("Ελένη Παπαδοπούλου", "გიორგი ბერიძე", "李小龍", "محمد علي"):
        assert conv._person_name(name) == name


@pytest.mark.parametrize("text", EVERYTHING)
def test_a_taken_name_is_short_plain_letters(text):
    name = conv._person_name(text)
    if name is None:
        return
    assert 1 <= len(name.split()) <= 4 and len(name) <= 60
    assert not any(unicodedata.category(c).startswith("C") for c in name)
    assert "?" not in name and "\0" not in name
    assert all(c.isalpha() or c in " -'’" for c in name)


def test_a_name_survives_every_code_point():
    for text in sweep_chunks(4):
        conv._person_name(text)
        conv._person_name("Anna " + text)


# --- «так» and what is said about the whole disk ----------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "так",
        "Так!",
        "ТАК.",
        "ok",
        "OK.",
        " yes ",
        "yes please",
        "sí",
        "SÍ",
        "d'accord",
        "Так, добре",
    ],
)
def test_a_plain_yes_after_the_question_is_agreement(text):
    assert conv._agrees(text, True)


@pytest.mark.parametrize("text", ["так", "yes", "ok", "sí", "Так, добре"])
def test_a_yes_that_does_not_follow_the_question_is_not_agreement(text):
    assert not conv._agrees(text, False)


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "\0",
        "так\0",
        "\0так",
        "так?",
        "yes?",
        "¿sí?",
        "так не",
        "не так",
        "yes no",
        "ok, no",
        "yes not",
        "ok don't",
        "ja nein",
        "ні",
        "ні, так",
        "no",
        "yes yes yes yes",
        "так " * 10,
        "yes" * 1_000,
        "ok " * 5_000,
        "a" * 10_000,
        "😀",
        "disk",
        "erase everything",
        "Sure, go ahead and do it",
    ],
)
def test_anything_else_after_the_question_is_not_agreement(text):
    assert not conv._agrees(text, True)


@pytest.mark.parametrize("text", EVERYTHING)
def test_agreement_is_only_ever_a_short_yes_after_the_question_with_no_no(text):
    assert conv._agrees(text, False) is False
    if conv._agrees(text, True):
        words = " ".join(text.lower().split()).strip(".,!").replace(",", " ").split()
        assert 1 <= len(words) <= 3 and words[0] in conv.YES_WORDS
        assert not set(words) & conv.NEGATIONS
        assert not any(m in text for m in conv.QUESTION_MARKS)


@pytest.mark.parametrize(
    "text",
    [
        "Erase the whole disk",
        "erase",
        "ERASE THE WHOLE DISK",
        "please  erase\nthe   whole\tdisk.",
        "встанови на весь диск",
        "Стирай",
        "cały dysk",
        "Ganze Festplatte bitte",
        "todo el disco",
        "tutto il disco",
        "visą diską",
    ],
)
def test_a_statement_about_the_whole_disk_is_heard(text):
    assert conv._says(text, conv.WHOLE_DISK_WORDS)


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "\0",
        "erase\0 not",
        "erase the whole disk?",
        "erase the whole disk？",
        "¿todo el disco",
        "do not erase the whole disk",
        "Don't erase the whole disk",
        "no erase",
        "не стирай весь диск",
        "не чіпай, не стирай весь диск",
        "no borres todo el disco",
        "nie kasuj, cały dysk",
        "nein, ganze festplatte",
        "Whole disk? no",
        "a" * 10_000,
        "keep windows",
        "😀",
    ],
)
def test_a_question_a_denial_or_nothing_is_not_a_statement_about_the_whole_disk(text):
    assert not conv._says(text, conv.WHOLE_DISK_WORDS)


def test_no_phrases_say_nothing():
    for text in EVERYTHING:
        assert not conv._says(text, ())


@pytest.mark.parametrize("text", EVERYTHING)
def test_a_statement_never_holds_a_question_or_a_denial(text):
    if conv._says(text, conv.WHOLE_DISK_WORDS):
        low = " ".join(text.lower().split())
        assert not any(m in low for m in conv.QUESTION_MARKS)
        assert not set(low.replace(",", " ").replace(".", " ").split()) & conv.NEGATIONS


def test_short_answers_survive_every_code_point():
    for text in sweep_chunks(4):
        conv._agrees(text, True)
        conv._agrees("так " + text, True)
        conv._says(text, conv.WHOLE_DISK_WORDS)
        conv._says("erase " + text, conv.WHOLE_DISK_WORDS)


# --- text typed in the wrong keyboard layout --------------------------------------------

TO_ENGLISH_LAYOUT = {ua: us for us, ua in zip(conv._US, conv._UA, strict=True)}


@pytest.mark.parametrize("word", sorted(conv._UA_WORDS))
def test_every_word_of_the_conversation_typed_in_the_english_layout_is_read_back(word):
    typed = "".join(TO_ENGLISH_LAYOUT[c] for c in word)
    assert conv.wrong_layout(typed) == word
    assert conv.wrong_layout(typed + "!") == word + "!"


@pytest.mark.parametrize(
    ("typed", "read"),
    [
        ("ghjljd;e'vj", "продовжуємо"),
        ("ghbdsn", "привіт"),
        ("Ghbdsn!", "Привіт!"),
        ("ghbdsn ,hfn", "привіт брат"),
        ("jr", "ок"),
        ("nfr", "так"),
        ("jr jr jr", "ок ок ок"),
    ],
)
def test_text_typed_in_the_wrong_layout_is_read_in_the_right_one(typed, read):
    assert conv.wrong_layout(typed) == read


@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "Hello, my name is Anna",
        "I want to use the whole disk",
        "go forward",
        "don't",
        "it's fine, ok.",
        "yes",
        "next",
        "Cafe Guest",
        "skynet",
        "rhythm",
        "lynx",
        "sync",
        "my",
        "try",
        "Wrzesień",
        "Strzępiński",
        "Szczęście, proszę",
        "Wszystko dobrze",
        "Krzysztof Wójcik",
        "Zdzisław",
        "Žemaitė",
        "Ștefan",
        "Ñúñez",
        "O'Brien",
        "Müller",
        "Dr. Schmidt-Müller",
        "Nguyen Thi",
        "Ich möchte die ganze Festplatte benutzen",
        "Quiero usar todo el disco",
        "Je voudrais utiliser tout le disque",
        "Voglio usare tutto il disco",
        "Quero usar o disco todo",
        "Vreau să folosesc tot discul",
        "Chci použít celý disk",
        "Noriu naudoti visą diską",
        "Nire diskoa osoa erabili nahi dut",
        "N/A",
        "12345",
        "wifi-5G",
    ],
)
def test_ordinary_text_is_not_taken_for_the_wrong_layout(text):
    assert conv.wrong_layout(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "\n",
        "\0",
        "ПРИВІТ",
        "привіт",
        "так",
        "Олена Коваль",
        "ghbdsn привіт",
        "😀",
        "李小龍",
    ],
)
def test_empty_cyrillic_and_non_latin_text_is_never_read_as_the_wrong_layout(text):
    assert conv.wrong_layout(text) is None


@pytest.mark.parametrize("text", EVERYTHING)
def test_a_reading_is_the_same_text_letter_for_letter_and_never_for_cyrillic(text):
    read = conv.wrong_layout(text)
    if any("\u0400" <= ch <= "\u04ff" for ch in text):
        assert read is None
    if read is not None:
        assert isinstance(read, str)
        assert len(read) == len(text)
        assert read == text.translate(conv._TO_UA)


def test_a_huge_text_in_the_wrong_layout_is_read_without_trouble():
    assert conv.wrong_layout("ghbdsn " * 20_000) == "привіт " * 20_000
    assert conv.wrong_layout("hello " * 20_000) is None
    assert conv.wrong_layout("a" * 1_000_000) is None


def test_the_wrong_layout_survives_every_code_point():
    for text in sweep_chunks(4):
        conv.wrong_layout(text)
        conv.wrong_layout("ghbdsn " + text)
        conv.wrong_layout(text + ";" + text[:1] + text[-1:])


# --- the recovery key typed back --------------------------------------------------------

KEY = encryption.recovery_key(lambda n: bytes(range(n)))
PARTS = encryption.groups(KEY)
HOMOGLYPHS = {"a": "а", "c": "с", "e": "е", "o": "о", "i": "і", "p": "р", "x": "х", "y": "у"}
DASHES = ["-", "‐", "‑", "–", "—", " - ", "\t", "  ", "\u00a0"]


def test_the_test_key_is_a_real_key():
    assert encryption.RECOVERY_RE.match(KEY)
    assert len(PARTS) == 8 and all(len(p) == encryption.GROUP for p in PARTS)


@pytest.mark.parametrize("index", range(8))
@pytest.mark.parametrize("dash", DASHES)
def test_a_group_typed_with_the_usual_slips_is_right(index, dash):
    group = PARTS[index]
    typed = [
        group,
        group.upper(),
        f" {group} ",
        f"\n{group}\t",
        dash.join(group),
        f"{group[:4]}{dash}{group[4:]}",
        group.title(),
    ]
    for text in typed:
        assert encryption.check_group(KEY, text, index), repr(text)


@pytest.mark.parametrize("index", [-1, -8, 8, 9, 100, 10**9, 2**64])
def test_a_group_index_outside_the_key_is_never_right(index):
    for text in PARTS + ["", KEY, " ".join(PARTS)]:
        assert encryption.check_group(KEY, text, index) is False


@pytest.mark.parametrize("index", range(8))
def test_the_wrong_group_is_never_right(index):
    for other, text in enumerate(PARTS):
        assert encryption.check_group(KEY, text, index) is (other == index)


@pytest.mark.parametrize("index", range(8))
def test_a_group_with_one_letter_wrong_missing_or_added_is_never_right(index):
    group = PARTS[index]
    for pos in range(len(group)):
        for letter in encryption.MODHEX:
            if letter != group[pos]:
                assert not encryption.check_group(
                    KEY, group[:pos] + letter + group[pos + 1 :], index
                )
        assert not encryption.check_group(KEY, group[:pos] + group[pos + 1 :], index)
        assert not encryption.check_group(
            KEY, group[:pos] + group[pos] * 2 + group[pos + 1 :], index
        )
    assert not encryption.check_group(KEY, group + group[0], index)
    assert not encryption.check_group(KEY, group + group, index)
    assert not encryption.check_group(KEY, group[0] + group, index)
    assert not encryption.check_group(KEY, group[::-1] if group[::-1] != group else "x", index)


@pytest.mark.parametrize(
    "typed",
    [
        "",
        " ",
        "-",
        "\0",
        "\n",
        "a",
        "ccccccc",
        "x" * 8,
        "z" * 8,
        "0" * 8,
        "<script>",
        "../../..",
        "a" * 10_000,
        "-" * 10_000,
        " " * 10_000,
        "- " * 500_000,
        KEY,
        KEY.replace("-", ""),
    ],
)
def test_nonsense_in_the_field_is_never_the_group(typed):
    for index in range(8):
        assert encryption.check_group(KEY, typed, index) is False


def test_the_whole_key_is_not_one_group():
    for index in range(8):
        assert not encryption.check_group(KEY, KEY, index)
        assert not encryption.check_group(KEY, KEY.replace("-", ""), index)


@pytest.mark.parametrize("group", PARTS)
def test_letters_that_only_look_latin_are_not_the_letters_of_the_key(group):
    look_alike = "".join(HOMOGLYPHS.get(c, c) for c in group)
    if look_alike == group:
        return
    assert encryption.normalize(look_alike) != group
    assert encryption.normalize(look_alike) == look_alike.lower()


def test_normalize_drops_blanks_and_dashes_and_lower_cases():
    assert encryption.normalize(" AB-cd ‐‑–— EF\t\n gh ") == "abcdefgh"
    assert encryption.normalize("") == ""
    assert encryption.normalize("- -\u2014 ") == ""
    assert encryption.normalize(KEY.upper()) == KEY.replace("-", "")


@pytest.mark.parametrize("text", EVERYTHING)
def test_normalize_gives_lower_case_without_blanks_and_dashes_and_is_stable(text):
    out = encryption.normalize(text)
    assert out == out.lower()
    assert not re.search(r"[\s\-‐‑–—]", out)
    assert encryption.normalize(out) == out


def test_normalize_survives_every_code_point_and_a_huge_text():
    for text in sweep_chunks(64):
        encryption.normalize(text)
    assert encryption.normalize("a -" * 1_000_000) == "a" * 1_000_000


def test_a_cyrillic_a_does_not_become_a_latin_a():
    assert encryption.normalize("А") == "а"
    assert encryption.normalize("А") != "a"
    assert encryption.normalize("ОO") != "oo"


@pytest.mark.parametrize(
    "key", ["", "-", "--", "a--b", "x", "a-" * 1_000, "😀-😀", "\0", "a" * 10_000]
)
def test_groups_and_the_spoken_key_of_any_text_do_not_fail(key):
    parts = encryption.groups(key)
    assert isinstance(parts, list) and "-".join(parts) == key
    spoken = encryption.spoken(key)
    assert isinstance(spoken, str)


def test_the_spoken_key_is_letter_by_letter_with_a_pause_between_groups():
    spoken = encryption.spoken(KEY)
    sentences = spoken.split(". ")
    assert len(sentences) == 8
    for sentence, group in zip(sentences, PARTS, strict=True):
        assert sentence.split(" ") == list(group)
    assert "-" not in spoken


def test_a_key_from_a_short_random_source_is_refused():
    for size in (0, 1, 31, 33, 64):
        with pytest.raises(ValueError, match="too few bytes"):
            encryption.recovery_key(lambda n, size=size: bytes(size))


# --- the manual layout: plans and surveys that are wrong ---------------------------------

MANUAL = {"layout": "manual", "target": "d1"}


def disk_plan(**disk) -> dict:
    return {"disk": {**MANUAL, **disk}}


def survey_with(*partitions) -> dict:
    return {"disks": [{"id": "d1", "partitions": list(partitions)}]}


@pytest.mark.parametrize(
    "plan",
    [
        {},
        {"disk": None},
        {"disk": {}},
        {"disk": []},
        {"disk": 0},
        {"disk": {"partitions": None}},
        {"disk": {"partitions": "abc"}},
        {"disk": {"partitions": {"use": "home"}}},
        {"disk": {"partitions": 5}},
        {"disk": {"partitions": [None, 1, "x", [], (), {}]}},
        {"disk": {"layout": ["manual"], "partitions": [{"use": "home"}]}},
        {"disk": {"layout": "replace", "partitions": [{"use": "home"}]}},
        {"disk": {"layout": None}},
    ],
)
def test_a_plan_with_missing_or_wrong_parts_has_no_items_and_no_problems(plan):
    assert layout.problems(plan, {}) == []
    assert layout.problems(plan, {"disks": []}) == []
    assert all(isinstance(i, dict) for i in layout.items(plan))


def test_items_keep_only_the_dicts_of_a_list():
    rows = [{"use": "efi"}, None, 3, "x", [], {"use": "system", "size": -1}, {}]
    assert layout.items({"disk": {"partitions": rows}}) == [
        {"use": "efi"},
        {"use": "system", "size": -1},
        {},
    ]
    assert layout.items({"disk": {"partitions": []}}) == []


@pytest.mark.parametrize(
    "survey",
    [{}, {"disks": []}, {"disks": [{}]}, {"disks": [{"id": "other"}]}, {"disks": [{"id": ["d1"]}]}],
)
def test_a_survey_without_the_target_disk_finds_nothing(survey):
    plan = disk_plan(partitions=[{"use": "efi"}])
    assert layout.target(plan, survey) is None
    assert layout.problems(plan, survey) == []


def test_the_target_is_the_disk_with_the_planned_id_even_among_odd_disks():
    other = {"id": 7, "partitions": []}
    disk = {"id": "d1", "partitions": []}
    assert layout.target(disk_plan(), {"disks": [other, disk]}) is disk
    assert layout.target({"disk": {"target": ["d1"]}}, {"disks": [other, disk]}) is None


def test_home_or_swap_is_a_problem_however_many_odd_rows_surround_it():
    rows = [None, "x", {"use": "efi", "size": 10**40}, {"use": "swap", "size": -5}, {"use": 5}]
    out = layout.problems(disk_plan(partitions=rows), survey_with())
    assert [p["code"] for p in out] == [layout.LATER_HOME_SWAP]
    assert out[0] == {
        "code": layout.LATER_HOME_SWAP,
        "field": "disk.partitions",
        "message": layout.MESSAGES[layout.LATER_HOME_SWAP],
    }


@pytest.mark.parametrize("use", ["home", "swap"])
def test_each_of_home_and_swap_is_a_problem(use):
    rows = [{"use": "system", "size": 1}, {"use": use, "size": 1}]
    codes = [p["code"] for p in layout.problems(disk_plan(partitions=rows), survey_with())]
    assert codes == [layout.LATER_HOME_SWAP]


def test_a_new_efi_beside_an_efi_is_a_problem_and_a_chosen_one_is_not():
    survey = survey_with({"id": "p1", "kind": "efi"}, {"id": "p2", "kind": "data"})
    new = disk_plan(partitions=[{"use": "efi", "size": 512}])
    chosen = disk_plan(partitions=[{"use": "efi", "partition": "p1", "format": False}])
    assert [p["code"] for p in layout.problems(new, survey)] == [layout.NEW_EFI_BESIDE_EFI]
    assert layout.problems(chosen, survey) == []
    assert layout.problems(new, survey_with({"id": "p2", "kind": "data"})) == []
    assert layout.problems(new, survey_with()) == []


@pytest.mark.parametrize("size", [None, 0, -1, -(10**30), 10**30, 10**400, "x", 1.5, True, [], {}])
def test_whatever_the_size_of_a_new_partition_the_layout_checks_do_not_fail(size):
    rows = [{"use": "efi", "size": size}, {"use": "system", "size": size}]
    survey = survey_with({"id": "p1", "kind": "efi"})
    out = layout.problems(disk_plan(partitions=rows), survey)
    assert [p["code"] for p in out] == [layout.NEW_EFI_BESIDE_EFI]
    assert layout.items(disk_plan(partitions=rows)) == rows


def test_a_survey_partition_without_a_kind_is_not_an_efi():
    survey = survey_with({"id": "p1"}, {"id": "p2", "kind": None}, {"id": "p3", "kind": ["efi"]})
    new = disk_plan(partitions=[{"use": "efi", "size": 512}])
    assert layout.problems(new, survey) == []


def manual_wizard(tmp_path, kind="windows") -> wiz.Wizard:
    w = new_wizard(tmp_path, kind)
    assert w.set_disk(w.disks()[0]["id"], "manual") is None
    return w


def codes_of(w: wiz.Wizard) -> list[str]:
    return [p["code"] for p in w.report()["problems"]]


@pytest.mark.parametrize("size", [0, -1, -(10**30)])
def test_the_editor_leaves_out_rows_with_no_or_a_negative_size(tmp_path, size):
    w = manual_wizard(tmp_path)
    rows = [wiz.EditorRow("system", None, False, size), wiz.EditorRow(None, "p-efi")]
    assert wiz.editor_partitions(rows) == []
    assert w.set_partitions(rows) is not None
    assert "manual_needs_editor" in codes_of(w)


@pytest.mark.parametrize("size", [1, 10**9, 10**30])
def test_a_new_partition_too_small_or_too_big_for_the_disk_is_a_problem(tmp_path, size):
    w = manual_wizard(tmp_path)
    assert w.set_partitions([wiz.EditorRow("system", None, False, size)]) is None
    codes = codes_of(w)
    assert "too_small" in codes or "not_enough_space" in codes
    assert "needs_one_efi" in codes


@pytest.mark.parametrize(
    "rows",
    [
        [1],
        "abc",
        [None],
        [[]],
        {"a": 1},
        [{"use": "system", "size": "x"}],
        [{"use": "system", "size": -5}],
        [{"use": "system", "size": None}],
        [{"use": "system", "size": True}],
        [{"use": "system", "size": 1.5}],
        [{"use": None}],
        [{"use": ["system"]}],
        [{"use": "boot", "size": 10**9}],
        [{"use": "efi", "partition": 5}],
        [{"use": "efi", "partition": "nope"}],
        [{"use": "system", "partition": ""}],
        [{"use": "system", "partition": "p-win", "size": 5}],
    ],
)
def test_a_plan_with_wrong_partition_rows_gets_problems_not_a_crash(tmp_path, rows):
    w = manual_wizard(tmp_path)
    plan = w.plan
    plan["disk"]["partitions"] = rows
    plans.save(plan, w.plan_path)
    report = w.report()
    assert report["problems"], rows
    assert "valid" not in report or report["valid"] is False
    assert {"bad_partitions", "no_partition", "needs_one_efi", "needs_one_system"} & set(
        codes_of(w)
    )


def test_a_plan_with_a_huge_new_partition_says_there_is_no_space(tmp_path):
    w = manual_wizard(tmp_path)
    plan = w.plan
    plan["disk"]["partitions"] = [{"use": "system", "size": 10**30}]
    plans.save(plan, w.plan_path)
    assert "not_enough_space" in codes_of(w)


@pytest.mark.parametrize("n", [-(10**30), -1, 0, 1, 1023, 2**20, 2**30 - 1, 2**30, 10**15, 10**30])
def test_a_size_of_any_whole_number_has_words(n):
    text = wiz.size_text(n)
    assert isinstance(text, str) and text.endswith(("MiB", "GiB", "TiB"))


@pytest.mark.parametrize(
    "disk",
    [{}, {"size": -5}, {"size": 10**30}, {"model": None, "id": 3}, {"model": "", "name": "n"}],
)
def test_a_disk_with_missing_or_odd_numbers_still_has_a_title(disk):
    assert isinstance(wiz.disk_title(disk), str)


def test_a_disk_title_shows_the_model_as_text_and_never_as_markup():
    title = wiz.disk_title({"model": "<b>x</b>\0&amp;", "size": 2**30})
    assert title == "<b>x</b>\0&amp; · 1 GiB"


@pytest.mark.parametrize("change", [{}, {"action": "nope"}, {"action": None}, {"action": 5}])
def test_an_unknown_change_is_said_as_choosing_in_the_editor(change):
    assert wiz.change_text(change, lambda s: s) == wiz.CHANGE_MANUAL


def test_the_editor_rows_with_odd_uses_are_passed_on_for_the_validator_to_refuse(tmp_path):
    w = manual_wizard(tmp_path)
    rows = [wiz.EditorRow("bogus", "p-win"), wiz.EditorRow("efi", "nope")]
    assert w.set_partitions(rows) is not None


def test_the_synthetic_computers_here_are_not_the_real_one(tmp_path):
    w = new_wizard(tmp_path)
    assert isinstance(w.machine, DemoMachine)
    assert all(d["id"].startswith(("nvme-Demo", "usb-Demo", "ata-Demo")) for d in w.disks())
