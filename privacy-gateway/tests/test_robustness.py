# SPDX-License-Identifier: Apache-2.0
"""Values written in other ways, and answers that once came back changed or slowly.

All data is synthetic: example.org addresses, published test card numbers and the
specimen IBANs the other tests of this package use, and obviously fake keys.
"""

from __future__ import annotations

import time

import pytest

from privacy_gateway import Masker

EMAIL = "olena.test@example.org"


@pytest.mark.parametrize(
    "text,value",
    [
        ("067.123.45.67", "067.123.45.67"),
        ("+380.67.123.45.67", "+380.67.123.45.67"),
        ("UA21-3223-1300-0002-6007-2335-6600-1", "UA21-3223-1300-0002-6007-2335-6600-1"),
        ("ua21 3223 1300 0002 6007 2335 6600 1", "ua21 3223 1300 0002 6007 2335 6600 1"),
        ("4111.1111.1111.1111", "4111.1111.1111.1111"),
        ("4111  1111  1111  1111", "4111  1111  1111  1111"),
        ("4111\t1111\t1111\t1111", "1111\t1111"),
        ("authorization: bearer FAKETOKENFAKETOKEN0123456789", "FAKETOKENFAKETOKEN0123456789"),
        ("Authorization: Bearer  FAKETOKENFAKETOKEN0123456789", "FAKETOKENFAKETOKEN0123456789"),
        ("SK-FAKEKEYFAKEKEYFAKEKEY0123", "FAKEKEYFAKEKEYFAKEKEY0123"),
        ("tel+380 67 123 45 67", "123 45 67"),
        ("abcUA213223130000026007233566001def", "UA213223130000026007233566001"),
    ],
)
def test_a_value_written_another_way_is_hidden(text, value):
    m = Masker()
    masked = m.mask(text)
    assert value not in masked
    assert m.unmask(masked) == text


def test_a_password_after_an_equals_sign_keeps_its_label():
    m = Masker()
    masked = m.mask("PASSWORD=FakePass-7731")
    assert masked.startswith("PASSWORD=") and "FakePass" not in masked


def test_a_windows_line_end_after_a_field_survives_masking():
    text = "Name: Test Person\r\nnext"
    masked = Masker().mask(text)
    assert "\r\n" in masked and "Test Person" not in masked


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("([EMAIL_1])", f"({EMAIL})"),
        ("[write](mailto:[EMAIL_1])", f"[write](mailto:{EMAIL})"),
        ("{[EMAIL_1]}", f"{{{EMAIL}}}"),
        ("[EMAIL_1]] ok", f"{EMAIL}] ok"),
        ("[[EMAIL_1]]", EMAIL),
    ],
)
def test_brackets_around_a_placeholder_stay(answer, expected):
    m = Masker()
    m.mask(EMAIL)
    assert m.unmask(answer) == expected


def test_a_placeholder_written_by_the_person_is_not_replaced_by_a_value():
    m = Masker()
    text = f"literal [EMAIL_1] and {EMAIL}"
    masked = m.mask(text)
    assert masked.count("[EMAIL_1]") == 1
    assert m.unmask(masked) == text


def test_a_value_put_back_is_not_read_again():
    m = Masker()
    text = f"Email: {EMAIL}\nName: EMAIL_1"
    assert m.unmask(m.mask(text)) == text


def test_putting_values_back_does_not_slow_down_with_many_values():
    m = Masker()
    m.mask(" ".join(f"user{i}@example.org" for i in range(1000)))
    answer = "[Emial_1] " * 100_000  # about 1 MB of changed placeholders
    started = time.monotonic()
    m.unmask(answer)
    assert time.monotonic() - started < 10
