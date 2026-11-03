# SPDX-License-Identifier: Apache-2.0
# All data in these tests is synthetic.
from privacy_gateway import Kind, Masker


def test_round_trip():
    m = Masker()
    text = "Олена: olena@example.com, +380 67 123 45 67; копія на ivan@example.com"
    masked = m.mask(text)
    assert masked == "Олена: [EMAIL_1], [PHONE_1]; копія на [EMAIL_2]"
    assert m.unmask(masked) == text


def test_same_value_same_placeholder_across_turns():
    m = Masker()
    assert m.mask("пиши Olena@Example.com") == "пиши [EMAIL_1]"
    assert m.mask("ще раз olena@example.com і bob@example.com") == "ще раз [EMAIL_1] і [EMAIL_2]"
    assert len(m) == 2


def test_unknown_placeholder_is_left_alone():
    m = Masker()
    assert m.unmask("[EMAIL_7] і [NOT A PLACEHOLDER]") == "[EMAIL_7] і [NOT A PLACEHOLDER]"


def test_repr_hides_values():
    m = Masker()
    m.mask("olena@example.com")
    assert "olena" not in repr(m)


def test_mask_messages_copies():
    m = Masker()
    messages = [
        {"role": "system", "content": "Ти Gloxi."},
        {"role": "user", "content": "мій номер +380671234567"},
    ]
    out = m.mask_messages(messages)
    assert out[1] == {"role": "user", "content": "мій номер [PHONE_1]"}
    assert messages[1]["content"] == "мій номер +380671234567"


def test_unmask_stream_rejoins_split_placeholders():
    m = Masker()
    m.mask("a@example.com +380671234567")
    chunks = ["Лист для [EM", "AIL_", "1], дзвінок на [PHO", "NE_1]", ". [", "ok]"]
    out = list(m.unmask_stream(chunks))
    assert "".join(out) == "Лист для a@example.com, дзвінок на +380671234567. [ok]"
    # Text before a split placeholder is released at once, not held to the end.
    assert out[0] == "Лист для "


def test_unmask_stream_flushes_a_dangling_bracket():
    m = Masker()
    assert "".join(m.unmask_stream(["кінець [EMAIL"])) == "кінець [EMAIL"


def test_only_selected_kinds():
    m = Masker(kinds=[Kind.EMAIL])
    assert m.mask("a@example.com +380671234567") == "[EMAIL_1] +380671234567"


def test_conceal_never_puts_a_password_back():
    m = Masker(kinds=[Kind.SECRET])
    masked = m.mask("Мій пароль від пошти Kvitka-7731.")
    assert "Kvitka-7731" not in masked and "[SECRET_1]" in masked
    answer = ["Запам'ятав: [SEC", "RET_1]. А [EMAIL_9] не знаю."]
    shown = "".join(m.conceal_stream(answer, shown="***"))
    assert shown == "Запам'ятав: ***. А [EMAIL_9] не знаю."
    assert m.conceal("[SECRET_1]") == "••••••"
