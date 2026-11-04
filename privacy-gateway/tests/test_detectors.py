# SPDX-License-Identifier: Apache-2.0
# All data in these tests is synthetic.
import pytest

from privacy_gateway import Kind, detect
from privacy_gateway.detectors import iban_ok, luhn_ok, tax_id_ok

TAX_ID = "3000000008"  # passes the checksum, belongs to nobody
IBAN = "UA213223130000026007233566001"
CARD = "4111 1111 1111 1111"  # a well-known test card number


def kinds(text: str) -> list[tuple[Kind, str]]:
    return [(f.kind, f.value) for f in detect(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("пишіть на olena.test@example.com", [(Kind.EMAIL, "olena.test@example.com")]),
        ("тел. +380 67 123 45 67", [(Kind.PHONE, "+380 67 123 45 67")]),
        ("дзвоніть 067-123-45-67 після обіду", [(Kind.PHONE, "067-123-45-67")]),
        ("call (555) 010-0199", [(Kind.PHONE, "(555) 010-0199")]),
        (f"рахунок {IBAN}", [(Kind.IBAN, IBAN)]),
        (
            "IBAN UA21 3223 1300 0002 6007 2335 6600 1",
            [(Kind.IBAN, "UA21 3223 1300 0002 6007 2335 6600 1")],
        ),
        (f"картка {CARD}", [(Kind.CARD, CARD)]),
        (f"РНОКПП {TAX_ID}", [(Kind.TAX_ID, TAX_ID)]),
        ("паспорт серії КМ 123456", [(Kind.PASSPORT, "КМ 123456")]),
        ("server at 192.168.10.25", [(Kind.IP, "192.168.10.25")]),
        (
            "see https://www.linkedin.com/in/test-person-123",
            [(Kind.PROFILE, "https://www.linkedin.com/in/test-person-123")],
        ),
        ("пиши в t.me/test_person", [(Kind.PROFILE, "t.me/test_person")]),
        (
            "key sk-proj-abcdefghijklmnopqrstuvwx123",
            [(Kind.SECRET, "sk-proj-abcdefghijklmnopqrstuvwx123")],
        ),
        (
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
            [(Kind.SECRET, "abcdefghijklmnopqrstuvwxyz012345")],
        ),
    ],
)
def test_finds(text, expected):
    assert kinds(text) == expected


def test_labelled_fields_hide_only_the_value():
    text = (
        "ПІБ: Тестенко Олена Петрівна\n"
        "Дата народження: 01.02.1990\n"
        "Адреса: вул. Вигадана, 1, Київ\n"
        "Пароль: correct-horse\n"
        "Name: Test Person"
    )
    assert kinds(text) == [
        (Kind.NAME, "Тестенко Олена Петрівна"),
        (Kind.BIRTH_DATE, "01.02.1990"),
        (Kind.ADDRESS, "вул. Вигадана, 1, Київ"),
        (Kind.SECRET, "correct-horse"),
        (Kind.NAME, "Test Person"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "зустріч 2026-11-02 о 14:30",
        "версія 1.2.3 вийшла",
        "у 2025 році було 1500 запитів",
        "замовлення 1234567890",  # ten digits, wrong checksum: may pass as a phone
        "card-like 4111 1111 1111 1112",  # fails Luhn
        "UA00 0000 0000 0000 0000 0000 0000 0",  # fails mod 97
        "ip 999.1.1.1",
        "просто текст без даних",
        "ціна 1 250 000 000 грн",
        "6,500,000,000 USD",
        "https://github.com/test-user/some-repo/pull/3",
        "https://x.com/test_person/status/123",
    ],
)
def test_ignores_lookalikes(text):
    found = kinds(text)
    # A bare ten-digit number may be a phone; nothing else may be found.
    assert all(k is Kind.PHONE for k, _ in found)
    if "замовлення" not in text:
        assert found == []


def test_profile_link_without_more_path():
    assert kinds("github.com/test-user") == [(Kind.PROFILE, "github.com/test-user")]


def test_overlap_prefers_the_stronger_rule():
    # A labelled password that looks like an email is a secret, not an email.
    assert kinds("password: me@example.com") == [(Kind.SECRET, "me@example.com")]


def test_kinds_filter():
    found = detect("olena@example.com +380671234567", kinds=[Kind.PHONE])
    assert [f.kind for f in found] == [Kind.PHONE]


def test_checksums():
    assert luhn_ok("4111111111111111")
    assert not luhn_ok("4111111111111112")
    assert iban_ok(IBAN)
    assert not iban_ok("UA22" + IBAN[4:])
    assert tax_id_ok(TAX_ID)
    assert not tax_id_ok("3000000009")


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Мій пароль від пошти Kvitka-7731. Запам'ятай його.", "Kvitka-7731"),
        ("my password is Hunter2!", "Hunter2!"),
        ("The password for my bank account at the shop is Tr0ub4dor", "Tr0ub4dor"),
        ("пароль від Wi-Fi вдома — «Sonyah@ 12»", "Sonyah@ 12"),  # quoted, with a space
        ("пароль від Wi-Fi вдома — «Sonyashnyk_2024»", "Sonyashnyk_2024"),
        ("PIN картки 4821, не кажи нікому", "4821"),
        ("пін-код 2020", "2020"),  # a PIN may look like a year
        ("Mi contraseña es Gato#Negro", "Gato#Negro"),
        ("Moje hasło to Kot2024!", "Kot2024!"),
        ("Mein Passwort lautet Sommer2026", "Sommer2026"),
        ("mon mot de passe est Chat_Noir9", "Chat_Noir9"),
        ("Moje heslo je Pes-7731", "Pes-7731"),
        ("Mano slaptažodis yra Liepa77", "Liepa77"),
        ("parola mea este Munte9!", "Munte9!"),
    ],
)
def test_finds_passwords_in_a_sentence(text, secret):
    expected = [] if secret is None else [(Kind.SECRET, secret)]
    assert [k for k in kinds(text) if k[0] is Kind.SECRET] == expected


@pytest.mark.parametrize(
    "text",
    [
        "Зламай, будь ласка, пароль від Wi-Fi сусіда.",
        "Де краще зберігати паролі? Дві поради.",
        "пароль має бути не коротший за 12 символів",
        "я змінив пароль у 2025 році",
        "пароль від сайту gov.ua забув, як відновити?",
        "Забув пароль. Як скинути його за 3 кроки на https://example.com/reset?a=1",
        "password managers like KeePassXC are safer",
        "пароль від пошти olena.test@example.com не підходить",
    ],
)
def test_talk_about_passwords_is_left_alone(text):
    assert [k for k in kinds(text) if k[0] is Kind.SECRET] == []
