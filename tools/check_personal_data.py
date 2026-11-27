#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check that the tracked text files of the repo carry no personal data.

Every tracked text file is read (UTF-8, or UTF-16 / UTF-32 with a byte order
mark, or UTF-16 without one when the NUL bytes show it; binary files and
symbolic links are skipped, a file over ``MAX_BYTES`` is an error) and each
line is searched for:

* **e-mail addresses** whose domain is not ``example.org``, ``example.com``,
  ``example.net`` or ``vilnaos.com`` (subdomains included), not in the
  reserved top-level domains of RFC 2606 (``.example``, ``.invalid``,
  ``.test``, ``.localhost``) and that are not ``noreply`` addresses (the local
  part is ``noreply`` / ``no-reply`` or a domain label is ``noreply``).
  Names of systemd instances (``name@1000.service``) are not addresses;
* **phone numbers** in the international form (``+<country code> <digits>``, 8-15
  digits, any digits of Unicode) and the North American form
  (``(NNN) NNN-NNNN``); numbers of the reserved fictional range 555-01XX pass;
* **IBANs**: two letters, two digits and the length that the country of the
  IBAN has (spaces and hyphens are allowed between the groups); the check
  digits are not verified, so a mistyped real IBAN is found too;
* **home folders** ``/home/<name>`` (``/var/home/<name>`` too), ``/Users/<name>`` and
  ``C:\\Users\\<name>`` (``C:/Users/..`` and the doubled backslash of source
  strings too) whose name is not a placeholder (``user``, ``username``, one
  letter, ``<user>``, ``$USER``, ``%USERNAME%``, ``Public``, ``Default`` ...);
* **IP addresses** that are not private, loopback, link-local, multicast,
  unspecified or reserved for documentation (IPv4: RFC 1918, RFC 5737 and the
  like; IPv6: ``::1``, ``::``, ``2001:db8::/32`` of RFC 3849, ``fc00::/7``,
  ``fe80::/10``, ``ff00::/8``). A part of a version number (an octet above
  255, a fifth group) is not an address.

Text is normalised before the search (NFKC, zero-width and other format
characters removed), so a full-width ``@`` or a soft hyphen in an address does
not hide it. Not found: obfuscated forms (``name [at] host``), percent-encoded
text, local phone numbers without ``+``, addresses in binary files, a number
written as one 32-bit integer.

A finding is allowed when it matches a row of ``EXCEPTIONS`` (kind, path glob,
value glob, **reason**). Findings that the repo already has and that are not
exceptions are listed in ``LEGACY`` and are warnings; every other finding is an
error. A row of either table that matches nothing is a warning: delete it.
The report prints a masked value (``o***@host``, ``+38...67``), never the whole
value, so a log does not repeat a leak. Only ``git ls-files`` is run; nothing
else is executed, read from the network or written.

Run: python3 tools/check_personal_data.py [FILE ...]
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MAX_BYTES = 8 * 1024 * 1024  # a bigger text file is an error: it cannot be searched cheaply
SAMPLE = 8192  # bytes looked at to tell text from binary

BINARY_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".icns", ".bmp", ".tif", ".tiff",
        ".oga", ".ogg", ".flac", ".wav", ".mp3", ".mp4", ".webm", ".opus",
        ".mo", ".gz", ".xz", ".zst", ".bz2", ".zip", ".tar", ".woff", ".woff2", ".ttf", ".otf",
        ".pdf", ".gpg", ".bin", ".so", ".pyc",
    }
)  # fmt: skip

# ---------------------------------------------------------------------------
# What is allowed without a row in EXCEPTIONS
# ---------------------------------------------------------------------------

ALLOWED_EMAIL_DOMAINS = ("example.org", "example.com", "example.net", "vilnaos.com")
RESERVED_TLDS = frozenset({"example", "invalid", "test", "localhost"})  # RFC 2606
NOREPLY_LOCALS = frozenset({"noreply", "no-reply", "no_reply", "donotreply", "do-not-reply"})
# ``name@1000.service`` is a systemd instance, ``ca@valencia.po`` a locale: not addresses.
NOT_A_TLD = frozenset(
    {
        "service", "socket", "timer", "target", "mount", "automount", "path", "slice",
        "scope", "swap", "device", "po", "pot", "mo", "conf", "d",
    }
)  # fmt: skip

PLACEHOLDER_NAMES = frozenset(
    {
        "user", "users", "username", "name", "yourname", "your-name", "you", "me", "my",
        "someone", "somebody", "example", "test", "foo", "bar", "baz", "home",
        # Windows system profiles that belong to nobody
        "public", "default", "default user", "all users", "shared", "administrator",
    }
)  # fmt: skip

IPV4_ALLOWED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",  # "this network", 0.0.0.0
        "10.0.0.0/8",  # RFC 1918
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local
        "172.16.0.0/12",  # RFC 1918
        "192.0.2.0/24",  # RFC 5737 TEST-NET-1
        "192.168.0.0/16",  # RFC 1918
        "198.51.100.0/24",  # RFC 5737 TEST-NET-2
        "203.0.113.0/24",  # RFC 5737 TEST-NET-3
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved; also the netmasks 255.255.255.0 and the like
    )
)
IPV6_ALLOWED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "::/128",  # unspecified
        "::1/128",  # loopback
        "2001:db8::/32",  # RFC 3849 documentation
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local
        "ff00::/8",  # multicast
    )
)

# Length of the IBAN of each country (ISO 13616 registry, the countries of Europe and the
# ones most often seen). A string that has the shape but another length is not an IBAN.
IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22,
    "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "DO": 28,
    "EE": 20, "EG": 29, "ES": 24, "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23,
    "GL": 18, "GR": 27, "GT": 28, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26,
    "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20,
    "LV": 21, "MC": 27, "MD": 24, "ME": 22, "MK": 19, "MR": 27, "MT": 31, "MU": 30, "NL": 18,
    "NO": 15, "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "SA": 24,
    "SC": 31, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28, "TL": 23, "TN": 24,
    "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
}  # fmt: skip


@dataclass(frozen=True)
class AllowedRow:
    """One allowed finding. ``value`` is matched against the normalised value."""

    kind: str  # "email", "phone", "iban", "home" or "ip"
    path: str  # globs joined by "|" on the path from the repo root (``*`` also matches ``/``)
    value: str  # globs joined by "|": lower-case e-mail, digits, compact IBAN, name, address
    reason: str


# Allowed findings with the reason. A real person's data never goes here.
EXCEPTIONS: tuple[AllowedRow, ...] = (
    # --- e-mail ---------------------------------------------------------------------------
    AllowedRow(
        "email",
        "*",
        "*@stdenis1980.github.io|dash-to-dock@micxgx.gmail.com",
        "UUIDs of GNOME Shell extensions (our own and the third-party Dash to Dock): they look "
        "like addresses but are extension ids, nobody reads mail there",
    ),
    AllowedRow(
        "email",
        "herkos/src/herkos/data/keros-corpus.jsonl",
        "*@example.??|*@example.???",
        "the corpus of the classifier has one invented address per language, with the "
        "country domain of that language on the reserved name example",
    ),
    AllowedRow(
        "email",
        "backup/tests/test_settings.py",
        "pw@example.eu",
        "an invented address in a test, on the reserved name example",
    ),
    AllowedRow(
        "email",
        "privacy-gateway/*|reports/feedback/tests/test_form.py",
        "a@b.??",
        "the shortest possible invented address, a test input of the address detector and of "
        "the feedback form; the domain b.xx is not a real mail domain",
    ),
    AllowedRow(
        "email",
        "reports/protocol/tests/hostile.rs",
        "олена@приклад.укр",
        "an invented address in Ukrainian script (a test of non-ASCII addresses)",
    ),
    AllowedRow(
        "email",
        "image/docs/composition.md",
        "board@lists.opensuse.org",
        "the public mailing list of the openSUSE board, named as the contact for permissions; "
        "a list address, not a person",
    ),
    AllowedRow(
        "email",
        "thirdparty/claude-desktop/README.md",
        "marketing@anthropic.com",
        "a role address of a company, quoted as the place to ask for brand permission",
    ),
    # --- phone ----------------------------------------------------------------------------
    AllowedRow(
        "phone",
        "privacy-gateway/*|gloxi/tests/test_feedback.py|reports/protocol/*",
        "380671234567|380509876543|380501234567|0671234567|5550100199|34612345678|37061234567"
        "|393123456789|420601123456|48601234567|48602345678|493012345678",
        "invented numbers (the sequence 123 45 67 and the like) of the tests, the examples and "
        "the benchmark data of the privacy gateway and of the feedback form",
    ),
    # --- IBAN -----------------------------------------------------------------------------
    AllowedRow(
        "iban",
        "privacy-gateway/*|reports/protocol/tests/hostile.rs",
        "DE89370400440532013000|GB82WEST12345698765432|ES9121000418450200051332"
        "|PL61109010140000071219812874|LT121000011101001000|FR1420041010050500013M02606"
        "|UA213223130000026007233566001|UA000000000000000000000000000",
        "specimen IBANs that banks and standards publish as examples, and one with wrong "
        "check digits, used as test input of the IBAN detector; none is a real account",
    ),
    # --- home folders ---------------------------------------------------------------------
    AllowedRow(
        "home",
        "herkos/*|reports/*|gloxi/tests/*|installer/tests/*",
        "anna|ana|ola|olah|olena|bohdan|hkt-anna|анна|anna kowalska",
        "invented first names of the test users (Anna, Ola, Olena, Bohdan) used in the "
        "fixtures of the safety layer, the reports and the installer; no such accounts exist",
    ),
    AllowedRow(
        "home",
        "gloxi/tests/test_people.py",
        "service",
        "the name of a service account in a fixture of /etc/passwd",
    ),
    AllowedRow(
        "home",
        "herkos/rs/crates/herkosd/src/passwd.rs",
        "odd",
        "a deliberately odd line of a fixture of /etc/passwd",
    ),
    AllowedRow(
        "home",
        "installer/tests/test_engine.py",
        "marker",
        "a marker file name in a test of the installer, not a user",
    ),
    AllowedRow(
        "home",
        "obs/images/Vilna-Live/config.sh",
        "live",
        "the account of the live image: it exists only in the image and belongs to nobody",
    ),
    AllowedRow(
        "home",
        "tools/test_check_svg.py",
        "alex",
        "an invented user name inside a test input of check_svg.py",
    ),
    # --- IP addresses ---------------------------------------------------------------------
    AllowedRow(
        "ip",
        "docs/adr/0032-site-hosts-dns.md|site/*|reports/client/data/vilna-reportd.service"
        "|tools/test_site_dns.py",
        "217.160.125.31|95.217.26.94|2a01:4f9:c01f:8002::",
        "the public addresses of the project's own site, published as the A and AAAA records "
        "of vilnaos.com; they are in the DNS for everyone, not private",
    ),
    AllowedRow(
        "ip",
        "obs/packages/vilna-branding/50-vilna-dns.conf",
        "9.9.9.9|149.112.112.112|2620:fe::fe|2620:fe::9",
        "the public resolvers of Quad9, which the image uses by name (DNS over TLS)",
    ),
    AllowedRow(
        "ip",
        "herkos/rs/crates/vilna-netguard/*",
        "194.242.2.4|2a07:e340::4|185.159.157.1|2a03:1b20:*",
        "public addresses of the Mullvad resolver and of a Mullvad VPN server, used as test "
        "input of the network rules; servers of a public service, not private",
    ),
    AllowedRow(
        "ip",
        "herkos/rs/*",
        "8.8.8.8|8.8.8.0|::ffff:8.8.8.0|1.2.3.4|172.32.0.0",
        "well-known public examples: Google's resolver, 1.2.3.4, and 172.32.0.0 that lies just "
        "outside the private range 172.16.0.0/12; test input of the network rules",
    ),
    AllowedRow(
        "ip",
        "reports/protocol/tests/hostile.rs",
        "50.123.45.67",
        "an invented address, written with a leading zero (050.) to test the masking of "
        "unusual forms",
    ),
    # --- this checker and its tests ---------------------------------------------------------
    AllowedRow(
        "email",
        "tools/check_personal_data.py|tools/test_check_personal_data.py",
        "*",
        "the tables of this checker and its tests hold, as data, the very values that it finds",
    ),
    AllowedRow(
        "phone",
        "tools/check_personal_data.py|tools/test_check_personal_data.py",
        "*",
        "the tables of this checker and its tests hold, as data, the very values that it finds",
    ),
    AllowedRow(
        "iban",
        "tools/check_personal_data.py|tools/test_check_personal_data.py",
        "*",
        "the tables of this checker and its tests hold, as data, the very values that it finds",
    ),
    AllowedRow(
        "home",
        "tools/check_personal_data.py|tools/test_check_personal_data.py",
        "*",
        "the tables of this checker and its tests hold, as data, the very values that it finds",
    ),
    AllowedRow(
        "ip",
        "tools/check_personal_data.py|tools/test_check_personal_data.py",
        "*",
        "the tables of this checker and its tests hold, as data, the very values that it finds",
    ),
)

# Findings that the repo has today and that are not exceptions: (path, kind, legacy_id(value)).
# They are warnings, so that the check can be switched on in CI; fix them, then delete the row.
# The value is kept as a short hash, so that this table does not repeat the data it points at.
LEGACY: frozenset[tuple[str, str, str]] = frozenset(
    {
        # the first name of a real person in a test fixture of /etc/passwd: use an invented one
        ("gloxi/tests/test_people.py", "home", "22c1ad192e49"),
        ("herkos/rs/crates/herkosd/src/passwd.rs", "home", "22c1ad192e49"),
        # a real-looking user name in the test paths of check_md_links.py
        ("tools/test_check_md_links.py", "home", "79aef7310914"),
    }
)

KINDS = ("email", "phone", "iban", "home", "ip")


# ---------------------------------------------------------------------------
# Reading a file
# ---------------------------------------------------------------------------


def decode(data: bytes) -> str | None:
    """Text of the bytes, or None for a binary file."""
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return data.decode("utf-32", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    sample = data[:SAMPLE]
    if b"\x00" in sample:
        even = sample[0::2].count(0)
        odd = sample[1::2].count(0)
        size = max(len(sample) // 2, 1)
        if even >= size * 0.3 and odd <= size * 0.02:
            return data.decode("utf-16-be", errors="replace")
        if odd >= size * 0.3 and even <= size * 0.02:
            return data.decode("utf-16-le", errors="replace")
        return None
    return data.decode("utf-8", errors="replace")


def normalise_line(line: str) -> str:
    """NFKC and no format characters (zero width space, soft hyphen, BOM, bidi marks)."""
    if line.isascii():
        return line
    line = unicodedata.normalize("NFKC", line)
    return "".join(ch for ch in line if unicodedata.category(ch) != "Cf")


# ---------------------------------------------------------------------------
# The detectors: each returns (kind, normalised value, masked value)
# ---------------------------------------------------------------------------

Hit = tuple[str, str, str]

EMAIL_RE = re.compile(
    r"(?<![\w.%+-])([\w.%+-]+)@((?:[^\W_][\w-]*\.)+(?:[^\W\d_]{2,}|xn--[\w-]{2,}))(?![\w-])",
    re.UNICODE,
)


def email_hits(line: str) -> list[Hit]:
    hits: list[Hit] = []
    if "@" not in line:
        return hits
    for m in EMAIL_RE.finditer(line):
        local, domain = m.group(1).lower(), m.group(2).lower().rstrip(".")
        labels = domain.split(".")
        if labels[-1] in NOT_A_TLD or labels[-1] in RESERVED_TLDS:
            continue
        if any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS):
            continue
        if local in NOREPLY_LOCALS or "noreply" in labels:
            continue
        hits.append(("email", f"{local}@{domain}", f"{local[0]}***@{domain}"))
    return hits


PHONE_INTL_RE = re.compile(r"(?<![\w+/=])\+\(?\d[\d \t().\-]{5,30}")
PHONE_NANP_RE = re.compile(r"(?<![\w+.\-])\(?(\d{3})\)?[ .\-](\d{3})[ .\-](\d{4})(?![\w\-])")
SEPARATORS = re.compile(r"[ \t().\-]+")
FICTIONAL_PHONE = re.compile(r"1?\d{3}55501\d\d")  # 555-0100 .. 555-0199 are never assigned


def decimal_digits(text: str) -> str:
    """The decimal digits of ``text`` as ASCII (Arabic-Indic and other digits included)."""
    out = []
    for ch in text:
        if ch.isdecimal():
            out.append(str(unicodedata.decimal(ch)))
    return "".join(out)


def phone_hits(line: str) -> list[Hit]:
    hits: list[Hit] = []
    if "+" in line:
        for m in PHONE_INTL_RE.finditer(line):
            taken: list[str] = []
            count = 0
            for group in SEPARATORS.split(m.group(0)[1:].strip(" \t(")):
                digits = decimal_digits(group) if group.isdecimal() else ""
                if not digits or count + len(digits) > 15:
                    break
                taken.append(digits)
                count += len(digits)
            digits = "".join(taken)
            # one run of digits is a phone only from 9 digits: "+86400000" is a day in ms
            need = 9 if len(taken) == 1 else 8
            if need <= len(digits) <= 15 and not FICTIONAL_PHONE.fullmatch(digits):
                hits.append(("phone", digits, f"+{digits[:2]}...{digits[-2:]}"))
    for m in PHONE_NANP_RE.finditer(line):
        digits = "".join(m.groups())
        spaces_only = "(" not in m.group(0) and not re.search(r"[.\-]", m.group(0))
        if spaces_only:
            continue  # "933 266 1229" is more often a viewBox or a table than a phone
        if not FICTIONAL_PHONE.fullmatch(digits):
            hits.append(("phone", digits, f"{digits[:3]}...{digits[-2:]}"))
    return hits


IBAN_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]{2}\d{2}(?:[ \-]?[A-Za-z0-9]{4}){2,7}(?:[ \-]?[A-Za-z0-9]{1,3})?"
    r"(?![A-Za-z0-9])"
)
IBAN_GROUPS = re.compile(r"[A-Za-z]{2}\d{2}|[A-Za-z0-9]{4}|[A-Za-z0-9]{1,3}")


def iban_checksum_ok(text: str) -> bool:
    """ISO 7064 mod 97-10: move the first four characters to the end, letters become 10..35."""
    moved = text[4:] + text[:4]
    return int("".join(str(int(ch, 36)) for ch in moved)) % 97 == 1


def iban_hits(line: str) -> list[Hit]:
    hits: list[Hit] = []
    for m in IBAN_RE.finditer(line):
        raw = m.group(0)
        tokens = [t for t in re.split(r"[ \-]", raw) if t]
        want = IBAN_LENGTHS.get(raw[:2].upper())
        if want is None:
            continue
        # The IBAN is the first tokens that add up to the length of the country: a word after
        # it ("LT12 1000 0111 0100 1000 OK") is not a part of it, and neither are the letters
        # that stick to a compact number ("DE89...00x").
        text, total = "", 0
        for token in tokens:
            text += token
            total += len(token)
            if total >= want:
                break
        if total != want:
            continue
        text = text.upper()
        # A compact string or one with hyphens ("de94bba4-06d1-4d40-...", a GUID) must pass the
        # check digits. Written in groups of four with spaces, as people write an IBAN, it is
        # reported even with wrong check digits: that is a mistyped real number.
        spaced = " " in raw and "-" not in raw
        if not spaced and not iban_checksum_ok(text):
            continue
        hits.append(("iban", text, f"{text[:4]}...{text[-2:]}"))
    return hits


NAME_CHARS = r"[^\s\"'`<>:;,)\]\[|{}*?\\/]"
HOME_POSIX_RE = re.compile(r"(?<![\w.~$%-])(?:/var)?/(?:home|Users)/(" + NAME_CHARS + r"+)")
HOME_WIN_RE = re.compile(
    r"(?<![\w])[A-Za-z]:(?:\\\\|\\|/)Users(?:\\\\|\\|/)"
    r"([^\s\"'`<>|:*?)\\/]+(?: [^\s\"'`<>|:*?)\\/]+(?=\\|/))?)"
)
PLACEHOLDER_START = ("<", "$", "%", "{", "~", "*", "[", "(", "@", "&")


def is_placeholder_name(name: str) -> bool:
    low = name.lower()
    return len(low) == 1 or low.startswith(PLACEHOLDER_START) or low in PLACEHOLDER_NAMES


def home_hits(line: str) -> list[Hit]:
    hits: list[Hit] = []
    if "/" not in line and "\\" not in line:
        return hits
    taken: list[tuple[int, int]] = []  # spans of Windows paths: c:/Users/x is not also /Users/x
    for regex, prefix in ((HOME_WIN_RE, "C:\\Users\\"), (HOME_POSIX_RE, "/home/")):
        for m in regex.finditer(line):
            if any(a <= m.start() < b for a, b in taken):
                continue
            if regex is HOME_WIN_RE:
                taken.append(m.span())
            name = m.group(1).rstrip(".")
            if name and not is_placeholder_name(name):
                hits.append(("home", name.lower(), f"{prefix}{name[0]}..."))
    return hits


IPV4_RE = re.compile(r"(?<![\w.@~^])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w-])(?!\.\d)")
IPV6_RE = re.compile(r"(?<![\w:.])[0-9A-Fa-f:.]{2,}(?:%\w+)?(?![\w:])")


def ip_hits(line: str) -> list[Hit]:
    hits: list[Hit] = []
    mapped_spans: list[tuple[int, int]] = []  # ::ffff:1.2.3.4 is one finding, not two
    if ":" in line:
        for m in IPV6_RE.finditer(line):
            text = m.group(0).split("%")[0].rstrip(".")
            if text.count(":") < 2 or not any(c.isdigit() for c in text):
                continue
            try:
                addr6 = ipaddress.IPv6Address(text)
            except ValueError:
                continue
            mapped = addr6.ipv4_mapped
            if mapped is not None:
                mapped_spans.append(m.span())
                if any(mapped in net for net in IPV4_ALLOWED):
                    continue
                hits.append(("ip", f"::ffff:{mapped}", f"::ffff:{str(mapped).split('.')[0]}.*.*"))
            elif not any(addr6 in net for net in IPV6_ALLOWED):
                hits.append(("ip", str(addr6), f"{addr6.exploded[:4]}:..."))
    if "." in line:
        for m in IPV4_RE.finditer(line):
            if any(a <= m.start() < b for a, b in mapped_spans):
                continue
            octets = [int(g) for g in m.groups()]
            if max(octets) > 255:
                continue  # a version number, not an address
            addr = ipaddress.IPv4Address(".".join(map(str, octets)))
            if any(addr in net for net in IPV4_ALLOWED) or int(addr) == 0xFFFFFFFF:
                continue
            hits.append(("ip", str(addr), f"{octets[0]}.{octets[1]}.*.*"))
    return hits


DETECTORS = (email_hits, phone_hits, iban_hits, home_hits, ip_hits)


def scan_text(text: str) -> list[tuple[int, str, str, str]]:
    """Findings of a text as (line, kind, value, masked value)."""
    found: list[tuple[int, str, str, str]] = []
    for number, raw in enumerate(text.split("\n"), 1):
        line = normalise_line(raw.rstrip("\r"))
        for detect in DETECTORS:
            for kind, value, shown in detect(line):
                found.append((number, kind, value, shown))
    return found


# ---------------------------------------------------------------------------
# The repo
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    path: str
    line: int
    message: str
    warning: bool = False

    def __str__(self) -> str:
        kind = "warning: " if self.warning else ""
        where = f"line {self.line}: " if self.line else ""
        return f"{self.path}: {where}{kind}{self.message}"


@dataclass
class Result:
    problems: list[Problem] = field(default_factory=list)
    files: int = 0  # text files searched
    skipped: int = 0  # binary files, links and the like
    allowed: int = 0  # findings that match an exception

    @property
    def errors(self) -> int:
        return sum(1 for p in self.problems if not p.warning)

    @property
    def warnings(self) -> int:
        return sum(1 for p in self.problems if p.warning)


MESSAGES = {
    "email": "e-mail address outside example.org/.com/.net, vilnaos.com and noreply: {}",
    "phone": "phone number: {}",
    "iban": "IBAN: {}",
    "home": "home folder of a named user: {}",
    "ip": "IP address that is not private or reserved for documentation: {}",
}


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    ).stdout
    return [p for p in out.decode("utf-8", errors="replace").split("\0") if p]


def legacy_id(value: str) -> str:
    """The short hash of a value, as LEGACY keeps it."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def matches(row: AllowedRow, kind: str, path: str, value: str) -> bool:
    return (
        row.kind == kind
        and any(fnmatch.fnmatchcase(path, alt) for alt in row.path.split("|"))
        and any(fnmatch.fnmatchcase(value.lower(), alt) for alt in row.value.lower().split("|"))
    )


def check_repo(
    root: Path,
    tracked: list[str],
    only: tuple[str, ...] = (),
    exceptions: tuple[AllowedRow, ...] | None = None,
    legacy: frozenset[tuple[str, str, str]] | None = None,
    max_bytes: int = MAX_BYTES,
) -> Result:
    """Search the tracked files; ``only`` limits the search to some of them."""
    exceptions = EXCEPTIONS if exceptions is None else exceptions
    legacy = LEGACY if legacy is None else legacy
    result = Result()
    used_rows: set[AllowedRow] = set()
    used_legacy: set[tuple[str, str, str]] = set()
    for rel in tracked:
        if only and rel not in only:
            continue
        path = root / rel
        try:
            if (
                path.is_symlink()
                or not path.is_file()
                or Path(rel).suffix.lower() in BINARY_SUFFIXES
            ):
                result.skipped += 1
                continue
            size = path.stat().st_size
            if size > max_bytes:
                result.problems.append(
                    Problem(rel, 0, f"text file of {size} bytes is over the limit of {max_bytes}")
                )
                continue
            data = path.read_bytes()
        except OSError as err:
            result.problems.append(Problem(rel, 0, f"cannot read the file: {err.strerror or err}"))
            continue
        text = decode(data)
        if text is None:
            result.skipped += 1
            continue
        result.files += 1
        for number, kind, value, shown in scan_text(text):
            row = next((r for r in exceptions if matches(r, kind, rel, value)), None)
            if row is not None:
                used_rows.add(row)
                result.allowed += 1
                continue
            key = (rel, kind, legacy_id(value))
            known = key in legacy
            if known:
                used_legacy.add(key)
            result.problems.append(Problem(rel, number, MESSAGES[kind].format(shown), known))
    if not only:
        for row in exceptions:
            if row not in used_rows:
                result.problems.append(
                    Problem(
                        "tools/check_personal_data.py",
                        0,
                        f"exception for {row.kind} {row.value!r} in {row.path!r} matches "
                        "nothing: delete the row",
                        True,
                    )
                )
        for item in sorted(legacy - used_legacy):
            result.problems.append(
                Problem(
                    item[0], 0, f"{item[1]} {item[2]!r} is in LEGACY but not found: delete it", True
                )
            )
    return result


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if any(a.startswith("-") for a in args):
        print("usage: python3 tools/check_personal_data.py [FILE ...]", file=sys.stderr)
        return 0 if args in (["-h"], ["--help"]) else 2
    try:
        tracked = tracked_files(ROOT)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    only = tuple(a.replace("\\", "/") for a in args)
    unknown = [a for a in only if a not in set(tracked)]
    if unknown:
        print(f"not a tracked file: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if not tracked:
        print("no tracked files found", file=sys.stderr)
        return 2
    result = check_repo(ROOT, tracked, only)
    for problem in result.problems:
        print(problem, file=sys.stderr)
    print(
        f"{result.files} text files searched ({result.skipped} binary or links skipped): "
        f"{result.allowed} allowed by exceptions, {result.errors} errors, "
        f"{result.warnings} warnings"
    )
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
