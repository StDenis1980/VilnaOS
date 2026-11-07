# SPDX-License-Identifier: Apache-2.0
# All numbers in these tests are synthetic: built to pass the check digit, or
# the specimen numbers that the issuing authorities publish in their own docs.
import pytest

from privacy_gateway import Kind, Masker, detect
from privacy_gateway.detectors import (
    cnp_ok,
    codice_fiscale_ok,
    dni_ok,
    lt_personal_code_ok,
    nip_ok,
    nir_ok,
    pesel_ok,
    rodne_cislo_ok,
    steuer_id_ok,
)


def kinds(text: str) -> list[tuple[Kind, str]]:
    return [(f.kind, f.value) for f in detect(text)]


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("DNI 12345678Z, por favor", "12345678Z"),
        ("mi DNI es 12345678-Z", "12345678-Z"),
        ("NIE Y1234567X", "Y1234567X"),
        ("PESEL: 90010112349", "90010112349"),
        ("urodzona w 2002, PESEL 02210312342", "02210312342"),  # month + 20 = 2000s
        ("Steuer-ID 86095742719", "86095742719"),
        ("asmens kodas 39001011237", "39001011237"),
        ("CNP 1900101123457", "1900101123457"),
        ("NIR 1 85 05 78 006 084 91", "1 85 05 78 006 084 91"),
        ("numéro 185057800608491", "185057800608491"),
        ("codice fiscale RSSMRA85T10A562S", "RSSMRA85T10A562S"),
        ("rodné číslo 905101/1002", "905101/1002"),
    ],
)
def test_finds_national_ids(text, value):
    assert kinds(text) == [(Kind.NATIONAL_ID, value)]


def test_polish_nip_only_when_labelled():
    assert kinds("NIP: 123-456-32-18") == [(Kind.TAX_ID, "123-456-32-18")]
    assert kinds("NIP 1234563218") == [(Kind.TAX_ID, "1234563218")]
    # The same digits without the label are not taken for a NIP.
    assert all(f.kind is not Kind.TAX_ID for f in detect("zamówienie 123-456-32-18"))


@pytest.mark.parametrize(
    "text",
    [
        "DNI 12345678A",  # wrong letter
        "PESEL 90010112340",  # wrong check digit
        "PESEL 90130112347",  # month 13
        "Steuer-ID 86095742718",  # wrong check digit
        "Steuer-ID 12345678901",  # no repeated digit
        "kodas 39001011238",  # wrong check digit
        "CNP 1901301123452",  # month 13
        "NIR 1 85 05 78 006 084 92",  # wrong key
        "codice RSSMRA85T10A562T",  # wrong check letter
        "rodné číslo 905101/1003",  # not divisible by 11
        "rodné číslo 901301/1002",  # month 13
        "NIP 1234563219",  # wrong check digit
        "версія 1.12345678901.2",  # digits inside a version string
        "ID 9001011234900",  # thirteen digits, not a CNP date
    ],
)
def test_ignores_invalid_ids(text):
    assert all(f.kind is not Kind.NATIONAL_ID for f in detect(text))
    assert all(f.kind is not Kind.TAX_ID or "NIP" not in text for f in detect(text))


def test_checks_reject_malformed_input():
    for check in (pesel_ok, steuer_id_ok, lt_personal_code_ok, cnp_ok, nir_ok, nip_ok):
        assert not check("")
        assert not check("abc")
    assert not dni_ok("1234567", "Z")
    assert not codice_fiscale_ok("SHORT")
    assert not rodne_cislo_ok("12/34")


def test_masks_and_restores_ids():
    masker = Masker()
    masked = masker.mask("Klient PESEL 90010112349, DNI 12345678Z")
    assert masked == "Klient PESEL [NATIONAL_ID_1], DNI [NATIONAL_ID_2]"
    assert masker.unmask("Gotowe dla [NATIONAL_ID_1].") == "Gotowe dla 90010112349."


def test_checks_reject_non_ascii_digits():
    arabic = "٩٠٠١٠١١٢٣٤٩"  # PESEL digits written in Arabic-Indic numerals
    assert not pesel_ok(arabic)
    assert not dni_ok("١٢٣٤٥٦٧٨", "Z")
    assert not codice_fiscale_ok("RSSMRA85T10A562ß")


@pytest.mark.parametrize(
    "text",
    [
        "15! = 1307674368000",
        "2^40 = 1099511627776",
        "Скільки буде 90010112349 + 1?",
        "x = 98765432109 - 12345678901",
        "12345678901/7",
        "π ≈ 3,14159265358",
        "1e11 = 100000000000",
        "19 плюс 38290376543",
        "скільки буде 38290376543 помножити на 3",
        "38290376543 minus 7 equals what?",
        "19 más 38290376543",
        "3 razy 38290376543",
        "38290376543 geteilt durch 2",
        "19 fois 38290376543",
    ],
)
def test_leaves_numbers_in_formulas(text):
    assert detect(text) == []


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("PESEL = 90010112349", "90010112349"),
        ("тел. 0671234567, дзвоніть", "0671234567"),
        ("PESEL: 90010112349 + Steuer-ID", "90010112349"),
        ("дзвоніть на 0671234567 завтра", "0671234567"),
        ("PESEL plus adres: 90010112349", "90010112349"),
    ],
)
def test_still_hides_labelled_numbers(text, value):
    assert [f.value for f in detect(text)] == [value]
