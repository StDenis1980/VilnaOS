# SPDX-License-Identifier: Apache-2.0
"""Models change placeholders a little; the real values must still come back.

Cases from the cloud measurement of 2026-11-05 (Mistral Small 4 and Medium,
synthetic data): {PERSON_2} came back as {Person_2}, {ОСОБА_1} as {ОСАБА_1}.
"""

import pytest

from privacy_gateway import Masker

EMAIL = "olena.koval@example.com"
PHONE = "+380 67 123 45 67"


@pytest.fixture
def masker():
    m = Masker()
    masked = m.mask(f"Write to {EMAIL} or call {PHONE}.")
    assert masked == "Write to [EMAIL_1] or call [PHONE_1]."
    return m


@pytest.mark.parametrize(
    "answer",
    [
        "[EMAIL_1]",
        "[Email_1]",
        "[email_1]",
        "{EMAIL_1}",
        "[[EMAIL_1]]",
        "[EMAIL 1]",
        "[EMAIL-1]",
        "[ EMAIL_1 ]",
        "[EMIAL_1]",  # two letters swapped
        "[EMAL_1]",  # one letter lost
        "EMAIL_1",  # brackets lost
    ],
)
def test_changed_placeholder_still_comes_back(masker, answer):
    assert masker.unmask(f"Send it to {answer}, thanks.") == f"Send it to {EMAIL}, thanks."
    assert masker.unresolved(masker.unmask(answer)) == []


def test_only_our_own_placeholders_are_restored(masker):
    # PHONE_2 was never masked: nothing to put back, and it is reported.
    text = masker.unmask("Call [PHONE_2] or [PHONE_1].")
    assert text == f"Call [PHONE_2] or {PHONE}."
    assert masker.unresolved(text) == ["[PHONE_2]"]


def test_far_off_or_foreign_placeholders_are_reported(masker):
    for broken in ("[ОСАБА_1]", "{Person_2}", "[NAMEE_1]", "[ADRES_3]"):
        assert masker.unresolved(masker.unmask(f"Dear {broken},")) == [broken]


def test_ordinary_text_is_left_alone(masker):
    text = "See figure 1, chapter 2 (Table 3) and [Figure 4]; email 1 more time."
    assert masker.unmask(text) == text
    assert masker.unresolved(text) == []


def test_kind_names_with_underscores():
    m = Masker()
    masked = m.mask("Дата народження: 12.03.1985")
    assert "[BIRTH_DATE_1]" in masked
    assert m.unmask("[Birth date 1]") == "12.03.1985"
    assert m.unmask("[BIRTH-DATE_1]") == "12.03.1985"


@pytest.mark.parametrize(
    "chunks",
    [
        ["Send to {Ema", "il_1", "} now."],
        ["Send to [[EMAIL_1]", "] now."],
        ["Send to EMAIL", "_1 now."],
        ["Send to [EM", "IAL_", "1] now."],
    ],
)
def test_stream_with_split_changed_placeholders(masker, chunks):
    assert "".join(masker.unmask_stream(chunks)) == f"Send to {EMAIL} now."


def test_conceal_hides_changed_secret_placeholders():
    m = Masker()
    m.mask("password: Kv7!pQ2zR")
    assert m.conceal("It is [Secret_1] or {SECRET 1}.") == "It is •••••• or ••••••."
