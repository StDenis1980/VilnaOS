# SPDX-License-Identifier: Apache-2.0
"""Rules that find personal and secret data in text.

Every rule returns spans ``(start, end)`` of the value to hide, never the label
around it: in ``Email: a@b.eu`` only ``a@b.eu`` is hidden, so the model still
sees what kind of value was there.

The rules are regular expressions plus checksums where a format has one
(payment cards, IBAN, the Ukrainian taxpayer number, the personal and tax
numbers of the EU countries in ADR 0009), which keeps false alarms down. They
do not find names or places in free text: that needs a language model on the
device and comes later. Until then a name is hidden only when it
is labelled (``Ім'я: ...``, ``Name: ...``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    """What a value is. The name is shown to the model in the placeholder."""

    SECRET = "SECRET"
    EMAIL = "EMAIL"
    IBAN = "IBAN"
    CARD = "CARD"
    TAX_ID = "TAX_ID"
    NATIONAL_ID = "NATIONAL_ID"
    PASSPORT = "PASSPORT"
    PHONE = "PHONE"
    IP = "IP"
    PROFILE = "PROFILE"
    NAME = "NAME"
    BIRTH_DATE = "BIRTH_DATE"
    ADDRESS = "ADDRESS"


Span = tuple[int, int]


@dataclass(frozen=True)
class Finding:
    kind: Kind
    start: int
    end: int
    value: str


# --- contacts -----------------------------------------------------------------

EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)

PHONE_CANDIDATE_RE = re.compile(r"(?<![\w+])(?:\+|00)?(?:\(?\d{1,4}\)?[\s.\-]*){3,6}\d{2,4}(?!\w)")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{2,4}")

# A profile link: the site and a user name, nothing after it. Longer links
# (a repository, a post) are left alone: they are usually what the user asks about.
PROFILE_RE = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)?"
    r"(?:linkedin\.com/in|t\.me|telegram\.me|github\.com|facebook\.com|fb\.com"
    r"|instagram\.com|twitter\.com|x\.com|threads\.net|tiktok\.com|mastodon\.social)"
    r"/@?[\w.%+-]++/?+(?![\w/])",
    re.I,
)

IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")

# --- identifiers and money ----------------------------------------------------

IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
CARD_RE = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
TAX_ID_RE = re.compile(r"(?<!\d)\d{10}(?!\d)")
# Ukrainian passport book: two Cyrillic capitals and six digits.
PASSPORT_RE = re.compile(r"(?<![\w])[А-ЯІЇЄҐ]{2} ?\d{6}(?!\d)")

# --- secrets ------------------------------------------------------------------

SECRET_RES = (
    re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}"),  # OpenAI, Anthropic style keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    re.compile(r"(?<=Bearer )[A-Za-z0-9._~+/-]{20,}=*"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
)

# --- labelled fields: ``Label: value`` on one line ------------------------------


def _labelled(labels: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?im)^[ \t]*(?:[-*•][ \t]*)?(?:{labels})[ \t]*[:\-–—][ \t]*(?P<value>\S[^\n]*?)[ \t]*$"
    )


NAME_LABEL_RE = _labelled(
    r"ПІБ|П\.І\.Б\.|ім.?я(?:[ \t]+(?:та|і)[ \t]+прізвище)?|прізвище|по[ \t]+батькові"
    r"|full[ \t]*name|first[ \t]*name|last[ \t]*name|surname|name"
)
BIRTH_LABEL_RE = _labelled(
    r"дата[ \t]+народження|народи(?:вся|лася)|date[ \t]+of[ \t]+birth|d\.?o\.?b\.?|birthday|born"
)
ADDRESS_LABEL_RE = _labelled(
    r"адреса(?:[ \t]+проживання|[ \t]+реєстрації)?|address|home[ \t]+address|місце[ \t]+проживання"
)
PASSPORT_LABEL_RE = _labelled(r"паспорт|passport(?:[ \t]+(?:no\.?|number))?|ID[ \t]+card|ID-картка")
SECRET_LABEL_RE = _labelled(r"пароль|password|passwd|pin|PIN-код|api[ _-]?key|token|secret")

# --- passwords in a sentence: ``мій пароль від пошти Kvitka-7731`` ---------------
#
# A password word, then within a few words on the same line, a token that looks
# like a password: letters with a digit or a symbol, or a PIN of digits. The
# words cover the ten languages of ADR 0009. Ordinary words ("пароль від Wi-Fi
# сусіда") are left alone, so the model can still talk about passwords.

PASSWORD_WORD_RE = re.compile(
    r"(?<!\w)(?:парол\w*|пін(?:-код\w*)?|pin(?:-код\w*|-code)?|password\w*|passwd|passphrase"
    r"|passcode|pwd|contraseñ\w*|clave|hasł\w*|hasl\w*|passwort\w*|kennwort\w*|mot de passe"
    r"|mots de passe|parol\w*|hesl\w*|slaptažod\w*)(?!\w)",
    re.I,
)
PIN_WORD_RE = re.compile(r"(?i)pin|пін")
_TOKEN_RE = re.compile(r"[^\s]+")
_WRAPPERS = "\"'«»“”„‘’`()[]{}<>"
# Symbols that ordinary words and site names do not have (unlike ``Wi-Fi``, ``gov.ua``).
_PASSWORD_SYMBOLS = frozenset("!@#$%^&*_+=?~/\\|<>")
_WORDS_AFTER = 10
# A quoted password may hold spaces: «Sonyah@ 12».
_QUOTED_RE = re.compile(r"[«\"“„'‘]([^«»\"“”„'‘’\n]{4,64})[»\"”“'’]")


def _strip_token(raw: str, start: int) -> tuple[str, int]:
    """Drop quotes and brackets around a token and a full stop or comma after it."""
    left = len(raw) - len(raw.lstrip(_WRAPPERS))
    token = raw[left:].rstrip(_WRAPPERS).rstrip(".,;:?").rstrip(_WRAPPERS)
    return token, start + left


def looks_like_password(token: str, *, pin: bool = False) -> bool:
    """Letters with a digit or a symbol (``Kvitka-7731``, ``P@ss!word``), or a PIN."""
    if "://" in token or token.startswith("[") or EMAIL_RE.fullmatch(token):
        return False
    if token.isdigit():
        if not pin and re.fullmatch(r"(?:19|20)\d\d", token):
            return False  # a year
        return 4 <= len(token) <= 12
    if not 6 <= len(token) <= 64 or not any(c.isalpha() for c in token):
        return False
    return any(c.isdigit() or c in _PASSWORD_SYMBOLS for c in token)


def _passwords_in_text(text: str) -> Iterator[Span]:
    for word in PASSWORD_WORD_RE.finditer(text):
        line_end = text.find("\n", word.end())
        line_end = len(text) if line_end == -1 else line_end
        pin = bool(PIN_WORD_RE.match(word.group(0)))
        for n, m in enumerate(_TOKEN_RE.finditer(text, word.end(), line_end)):
            if n >= _WORDS_AFTER:
                break
            quoted = _QUOTED_RE.match(text, m.start(), line_end)
            if (
                quoted
                and " " in quoted.group(1)
                and looks_like_password(quoted.group(1).replace(" ", ""), pin=pin)
            ):
                yield quoted.span(1)
                break
            token, start = _strip_token(m.group(0), m.start())
            is_word = PASSWORD_WORD_RE.fullmatch(token)
            if token and not is_word and looks_like_password(token, pin=pin):
                yield start, start + len(token)
                break
            if m.group(0).rstrip(_WRAPPERS)[-1:] in (".", "?", "!"):
                break  # the sentence is over


# --- checks ---------------------------------------------------------------------


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def luhn_ok(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def iban_ok(raw: str) -> bool:
    iban = raw.replace(" ", "")
    if not 15 <= len(iban) <= 34:
        return False
    moved = iban[4:] + iban[:4]
    as_digits = "".join(str(int(c, 36)) for c in moved)
    return int(as_digits) % 97 == 1


_TAX_WEIGHTS = (-1, 5, 7, 9, 4, 6, 10, 5, 7)


def tax_id_ok(number: str) -> bool:
    """Checksum of the Ukrainian taxpayer number (РНОКПП)."""
    if len(number) != 10 or not number.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(number, _TAX_WEIGHTS, strict=False))
    return total % 11 % 10 == int(number[9])


# --- national identity and tax numbers of the EU countries (ADR 0009) ----------
#
# Each format is found by its shape and kept only if its check digit (and, where
# the number holds a birth date, the date) is right. Formats that are just a run
# of digits with a weak check (Polish NIP, Czech rodné číslo without the slash)
# are taken only when labelled or written in their usual form, so ordinary
# numbers in the text are left alone.

_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
# Spain: DNI 12345678Z and NIE X1234567L.
DNI_RE = re.compile(r"(?<![\w])(?P<num>[XYZ]?\d{7,8})[- ]?(?P<letter>[A-Z])(?![\w])")


def dni_ok(num: str, letter: str) -> bool:
    if not num.isascii() or not num[1:].isdigit() or not num[:1].isalnum():
        return False
    if num[0] in "XYZ":
        if len(num) != 8:
            return False
        num = str("XYZ".index(num[0])) + num[1:]
    elif len(num) != 8:
        return False
    return _DNI_LETTERS[int(num) % 23] == letter


def _dnis(text: str) -> Iterator[Span]:
    for m in DNI_RE.finditer(text):
        if dni_ok(m.group("num"), m.group("letter")):
            yield m.span()


ELEVEN_DIGITS_RE = re.compile(r"(?<![\w+.,/-])\d{11}(?!\w)(?![.,/-]\d)")


def _date_ok(yy: int, mm: int, dd: int) -> bool:
    return 1 <= mm <= 12 and 1 <= dd <= 31 and 0 <= yy <= 99


def pesel_ok(number: str) -> bool:
    """Polish PESEL: weights 1 3 7 9, month carries the century."""
    if len(number) != 11 or not number.isascii() or not number.isdigit():
        return False
    d = [int(c) for c in number]
    weights = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)
    if (10 - sum(a * w for a, w in zip(d, weights, strict=False)) % 10) % 10 != d[10]:
        return False
    month = (d[2] * 10 + d[3]) % 20
    return _date_ok(d[0] * 10 + d[1], month, d[4] * 10 + d[5])


def steuer_id_ok(number: str) -> bool:
    """German tax identification number: ISO 7064 MOD 11,10 and the digit rule."""
    if len(number) != 11 or not number.isascii() or not number.isdigit() or number[0] == "0":
        return False
    counts = sorted((number[:10].count(c) for c in set(number[:10])), reverse=True)
    if counts[0] not in (2, 3) or any(c != 1 for c in counts[1:]):
        return False
    product = 10
    for c in number[:10]:
        total = (int(c) + product) % 10 or 10
        product = total * 2 % 11
    check = 11 - product
    return (0 if check == 10 else check) == int(number[10])


def lt_personal_code_ok(number: str) -> bool:
    """Lithuanian asmens kodas (the same scheme as the Estonian isikukood)."""
    if len(number) != 11 or not number.isascii() or not number.isdigit():
        return False
    if number[0] not in "123456":
        return False
    d = [int(c) for c in number]
    if not _date_ok(d[1] * 10 + d[2], d[3] * 10 + d[4], d[5] * 10 + d[6]):
        return False
    for weights in ((1, 2, 3, 4, 5, 6, 7, 8, 9, 1), (3, 4, 5, 6, 7, 8, 9, 1, 2, 3)):
        check = sum(a * w for a, w in zip(d, weights, strict=False)) % 11
        if check != 10:
            return check == d[10]
    return d[10] == 0


def _eleven_digit_ids(text: str) -> Iterator[Span]:
    for m in ELEVEN_DIGITS_RE.finditer(text):
        n = m.group(0)
        if pesel_ok(n) or steuer_id_ok(n) or lt_personal_code_ok(n):
            yield m.span()


# Romania: CNP, 13 digits.
CNP_RE = re.compile(r"(?<![\w+.,/-])[1-9]\d{12}(?!\w)(?![.,/-]\d)")


def cnp_ok(number: str) -> bool:
    if len(number) != 13 or not number.isascii() or not number.isdigit():
        return False
    d = [int(c) for c in number]
    if not _date_ok(d[1] * 10 + d[2], d[3] * 10 + d[4], d[5] * 10 + d[6]):
        return False
    check = sum(a * int(w) for a, w in zip(d, "279146358279", strict=False)) % 11
    return (1 if check == 10 else check) == d[12]


# France: NIR (numéro de sécurité sociale), 13 characters and a two-digit key.
NIR_RE = re.compile(
    r"(?<![\w])[1-478] ?\d{2} ?(?:0[1-9]|1[0-2]|[2-9]\d) ?(?:\d{2}|2[AB])"
    r" ?\d{3} ?\d{3} ?\d{2}(?![\w])"
)


def nir_ok(raw: str) -> bool:
    nir = raw.replace(" ", "")
    if len(nir) != 15:
        return False
    body = nir[:13].replace("2A", "19").replace("2B", "18")
    if not body.isascii() or not body.isdigit() or not nir[13:].isdigit():
        return False
    return 97 - int(body) % 97 == int(nir[13:])


# Italy: codice fiscale, 16 characters (digits may be replaced by letters, "omocodia").
CODICE_FISCALE_RE = re.compile(
    r"(?<![\w])[A-Z]{6}[\dLMNPQRSTUV]{2}[ABCDEHLMPRST][\dLMNPQRSTUV]{2}[A-Z][\dLMNPQRSTUV]{3}[A-Z](?![\w])",
    re.I,
)
# Values of characters in odd positions: A-M, then N-Z; a digit counts as the letter at its index.
_CF_ODD_A_M = (1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 2, 4, 18)
_CF_ODD_N_Z = (20, 11, 3, 6, 8, 12, 14, 16, 10, 22, 25, 24, 23)
_CF_ODD = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", _CF_ODD_A_M + _CF_ODD_N_Z, strict=True))
_CF_ODD.update(zip("0123456789", _CF_ODD_A_M, strict=False))


def codice_fiscale_ok(raw: str) -> bool:
    cf = raw.upper()
    if len(cf) != 16 or not cf.isascii() or not cf.isalnum():
        return False
    total = 0
    for i, c in enumerate(cf[:15]):
        if i % 2 == 0:  # odd positions, counting from 1
            total += _CF_ODD[c]
        else:
            total += int(c) if c.isdigit() else ord(c) - ord("A")
    return chr(ord("A") + total % 26) == cf[15]


# Czech Republic and Slovakia: rodné číslo, written as YYMMDD/XXXX.
RODNE_CISLO_RE = re.compile(r"(?<![\w/])\d{6}/\d{3,4}(?![\w/])")


def rodne_cislo_ok(raw: str) -> bool:
    number = raw.replace("/", "")
    if not number.isascii() or not number.isdigit() or len(number) not in (9, 10):
        return False
    month = int(number[2:4])
    for offset in (0, 50, 20, 70):
        if 1 <= month - offset <= 12:
            break
    else:
        return False
    if not 1 <= int(number[4:6]) <= 31:
        return False
    if len(number) == 9:
        return int(number[:2]) < 54  # numbers before 1954 had no check digit
    return int(number) % 11 == 0 or (int(number[:9]) % 11 == 10 and number[9] == "0")


# Poland: NIP (tax number), only when labelled: a bare run of ten digits is too common.
NIP_RE = re.compile(
    r"(?i)(?<![\w])NIP[:\s]*"
    r"(?P<value>\d{3}-?\d{3}-?\d{2}-?\d{2}|\d{3}-?\d{2}-?\d{2}-?\d{3})(?![\w-])"
)


def nip_ok(raw: str) -> bool:
    d = [int(c) for c in _digits(raw)]
    if len(d) != 10:
        return False
    check = sum(a * w for a, w in zip(d, (6, 5, 7, 2, 3, 4, 5, 6, 7), strict=False)) % 11
    return check != 10 and check == d[9]


def _nips(text: str) -> Iterator[Span]:
    for m in NIP_RE.finditer(text):
        if nip_ok(m.group("value")):
            yield m.span("value")


# 1 250 000 000 or 6,500,000: an amount, not a phone.
AMOUNT_RE = re.compile(r"\d{1,3}(?:([ ,\u00a0])\d{3})(?:\1\d{3})+")


def looks_like_phone(raw: str) -> bool:
    digits = _digits(raw)
    if not 10 <= len(digits) <= 15:
        return False
    return not DATE_RE.search(raw) and not AMOUNT_RE.fullmatch(raw)


def ipv4_ok(raw: str) -> bool:
    parts = raw.split(".")
    return all(int(p) <= 255 and (p == "0" or not p.startswith("0")) for p in parts)


# --- finders --------------------------------------------------------------------

Finder = Callable[[str], Iterable[Span]]


def _spans(pattern: re.Pattern[str], check: Callable[[str], bool] | None = None) -> Finder:
    def find(text: str) -> Iterator[Span]:
        for m in pattern.finditer(text):
            if check is None or check(m.group(0)):
                yield m.span()

    return find


def _value_spans(pattern: re.Pattern[str]) -> Finder:
    def find(text: str) -> Iterator[Span]:
        for m in pattern.finditer(text):
            yield m.span("value")

    return find


def _secrets(text: str) -> Iterator[Span]:
    for pattern in SECRET_RES:
        for m in pattern.finditer(text):
            yield m.span()


def _cards(text: str) -> Iterator[Span]:
    for m in CARD_RE.finditer(text):
        digits = _digits(m.group(0))
        # Card numbers start with 2-6 (Mastercard, Amex, Visa, Discover, UnionPay).
        if 13 <= len(digits) <= 19 and digits[0] in "23456" and luhn_ok(digits):
            yield m.span()


def _tax_ids(text: str) -> Iterator[Span]:
    for m in TAX_ID_RE.finditer(text):
        number = m.group(0)
        # A ten-digit number starting with 0 is a Ukrainian phone, not a tax number.
        if not number.startswith("0") and tax_id_ok(number):
            yield m.span()


@dataclass(frozen=True)
class Rule:
    kind: Kind
    find: Finder


# Earlier rules win when two findings overlap (then the longer one wins).
RULES: tuple[Rule, ...] = (
    Rule(Kind.SECRET, _secrets),
    Rule(Kind.SECRET, _value_spans(SECRET_LABEL_RE)),
    Rule(Kind.SECRET, _passwords_in_text),
    Rule(Kind.NAME, _value_spans(NAME_LABEL_RE)),
    Rule(Kind.BIRTH_DATE, _value_spans(BIRTH_LABEL_RE)),
    Rule(Kind.ADDRESS, _value_spans(ADDRESS_LABEL_RE)),
    Rule(Kind.PASSPORT, _value_spans(PASSPORT_LABEL_RE)),
    Rule(Kind.EMAIL, _spans(EMAIL_RE)),
    Rule(Kind.PROFILE, _spans(PROFILE_RE)),
    Rule(Kind.IBAN, _spans(IBAN_RE, iban_ok)),
    Rule(Kind.CARD, _cards),
    Rule(Kind.TAX_ID, _tax_ids),
    Rule(Kind.NATIONAL_ID, _dnis),
    Rule(Kind.NATIONAL_ID, _eleven_digit_ids),
    Rule(Kind.NATIONAL_ID, _spans(CNP_RE, cnp_ok)),
    Rule(Kind.NATIONAL_ID, _spans(NIR_RE, nir_ok)),
    Rule(Kind.NATIONAL_ID, _spans(CODICE_FISCALE_RE, codice_fiscale_ok)),
    Rule(Kind.NATIONAL_ID, _spans(RODNE_CISLO_RE, rodne_cislo_ok)),
    Rule(Kind.TAX_ID, _nips),
    Rule(Kind.PASSPORT, _spans(PASSPORT_RE)),
    Rule(Kind.IP, _spans(IPV4_RE, ipv4_ok)),
    Rule(Kind.PHONE, _spans(PHONE_CANDIDATE_RE, looks_like_phone)),
)


# A bare run of digits next to an arithmetic sign and another number is part of a
# formula (``15! = 1307674368000``, ``90010112349 + 1``), not a phone or an ID:
# hiding it would leave the model nothing to compute. A label on the left
# (``PESEL = 90010112349``) keeps the number hidden, because a word is not a number.
_NUMBER_KINDS = frozenset({Kind.PHONE, Kind.CARD, Kind.TAX_ID, Kind.NATIONAL_ID})
# Signs, and the same operations in words in the languages of ADR 0009 (and Russian):
# "19 плюс 38290376543". Short words that also mean "on" or "for" ("на", "por")
# count only as part of a longer phrase ("помножити на").
_OPERATOR = (
    r"(?:[=+\-*/×÷^<>≈≤≥]|(?<!\w)(?:плюс|мінус|минус|помножити\s+на|помножене\s+на|поділити\s+на|поділене\s+на|разів|дорівнює"
    r"|умножить\s+на|разделить\s+на|равно"
    r"|plus|minus|times|multiplied\s+by|divided\s+by|equals|is\s+equal\s+to"
    r"|más|menos|multiplicado\s+por|dividido\s+(?:por|entre)|igual\s+a"
    r"|razy|podzielić\s+przez|podzielone\s+przez|równa\s+się|równe"
    r"|mal|geteilt\s+durch|gleich|ist\s+gleich"
    r"|moins|fois|divisé\s+par|multiplié\s+par|égal(?:e)?\s+à|égale?"
    r"|più|meno|moltiplicato\s+per|diviso\s+per|uguale\s+a"
    r"|ori|împărțit\s+la|egal\s+cu|egal"
    r"|mínus|krát|děleno|rovná\s+se"
    r"|plius|kart|padalinti\s+iš|lygu)(?!\w))"
)
_OPERATOR_LEFT_RE = re.compile(rf"(?i)[\d)\]!]\s*{_OPERATOR}\s*$")
_OPERATOR_RIGHT_RE = re.compile(rf"(?i)\s*{_OPERATOR}\s*[\d(\[]")
_DECIMAL_LEFT_RE = re.compile(r"\d[.,]$")
_LOOK = 40


def in_formula(text: str, start: int, end: int) -> bool:
    """True if ``text[start:end]`` is a bare number that is an operand of a formula."""
    if not text[start:end].isdigit():
        return False
    left = text[max(0, start - _LOOK) : start]
    if _DECIMAL_LEFT_RE.search(left) or _OPERATOR_LEFT_RE.search(left):
        return True
    return _OPERATOR_RIGHT_RE.match(text, end, min(len(text), end + _LOOK)) is not None


def detect(text: str, kinds: Iterable[Kind] | None = None) -> list[Finding]:
    """Find what to hide in ``text``, without overlaps, in text order."""
    wanted = set(kinds) if kinds is not None else set(Kind)
    candidates: list[tuple[int, int, int, Kind]] = []
    for rank, rule in enumerate(RULES):
        if rule.kind not in wanted:
            continue
        for start, end in rule.find(text):
            if rule.kind in _NUMBER_KINDS and in_formula(text, start, end):
                continue
            if end > start:
                candidates.append((rank, start, end, rule.kind))
    candidates.sort(key=lambda c: (c[0], -(c[2] - c[1]), c[1]))

    taken: list[Span] = []
    found: list[Finding] = []
    for _, start, end, kind in candidates:
        if any(start < t_end and t_start < end for t_start, t_end in taken):
            continue
        taken.append((start, end))
        found.append(Finding(kind, start, end, text[start:end]))
    found.sort(key=lambda f: f.start)
    return found
