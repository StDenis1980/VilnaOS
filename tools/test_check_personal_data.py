#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/check_personal_data.py (personal data in the tracked files of the repo).

Run: python3 tools/test_check_personal_data.py
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_personal_data as cp  # noqa: E402  (after sys.path.insert above)


def kinds(text: str) -> list[tuple[str, str]]:
    """(kind, normalised value) of every finding of a text."""
    return [(kind, value) for _, kind, value, _ in cp.scan_text(text)]


def values(text: str, kind: str) -> list[str]:
    return [v for k, v in kinds(text) if k == kind]


def check(
    files: dict[str, bytes | str],
    exceptions: tuple[cp.AllowedRow, ...] = (),
    legacy: frozenset[tuple[str, str, str]] = frozenset(),
    only: tuple[str, ...] = (),
    max_bytes: int = cp.MAX_BYTES,
) -> cp.Result:
    """Write the files to a temporary folder and check them; every key is 'tracked'."""
    with tempfile.TemporaryDirectory() as tmp:
        for rel, data in files.items():
            path = Path(tmp) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return cp.check_repo(Path(tmp), list(files), only, exceptions, legacy, max_bytes)


def errors(files: dict[str, bytes | str], **kw: object) -> list[str]:
    return [str(p) for p in check(files, **kw).problems if not p.warning]  # type: ignore[arg-type]


class Email(unittest.TestCase):
    def test_real_looking_address_is_found(self) -> None:
        self.assertEqual(values("write to olena.k@gmail.com now", "email"), ["olena.k@gmail.com"])

    def test_documentation_domains_pass(self) -> None:
        text = "a@example.org b@example.com c@example.net d@mail.example.org e@vilnaos.com"
        self.assertEqual(values(text, "email"), [])

    def test_vilnaos_subdomain_passes_but_a_lookalike_does_not(self) -> None:
        self.assertEqual(values("x@reports.vilnaos.com", "email"), [])
        self.assertEqual(values("x@vilnaos.com.evil.net", "email"), ["x@vilnaos.com.evil.net"])
        self.assertEqual(values("x@notvilnaos.com", "email"), ["x@notvilnaos.com"])
        self.assertEqual(values("x@example.org.evil.com", "email"), ["x@example.org.evil.com"])
        self.assertEqual(values("x@myexample.org", "email"), ["x@myexample.org"])

    def test_reserved_top_level_domains_pass(self) -> None:
        text = "a@host.example b@host.invalid c@host.test d@host.localhost"
        self.assertEqual(values(text, "email"), [])

    def test_noreply_passes(self) -> None:
        text = "noreply@bank.eu no-reply@shop.de 1+User@users.noreply.github.com"
        self.assertEqual(values(text, "email"), [])

    def test_noreply_is_not_a_substring_match(self) -> None:
        self.assertEqual(values("john.noreply@gmail.com", "email"), ["john.noreply@gmail.com"])
        self.assertEqual(values("noreply-x@gmail.com", "email"), ["noreply-x@gmail.com"])

    def test_noreply_must_be_a_whole_domain_label(self) -> None:
        self.assertEqual(values("x@mynoreply.com", "email"), ["x@mynoreply.com"])
        self.assertEqual(values("x@noreply-mail.com", "email"), ["x@noreply-mail.com"])
        self.assertEqual(values("x@noreply.shop.eu", "email"), [])

    def test_case_is_folded(self) -> None:
        self.assertEqual(values("OLA@EXAMPLE.ORG", "email"), [])
        self.assertEqual(values("Ola@Gmail.COM", "email"), ["ola@gmail.com"])

    def test_systemd_instance_names_are_not_addresses(self) -> None:
        text = "gloxi@1001.service getty@tty1.service user@1000.socket a@b.timer ca@valencia.po"
        self.assertEqual(values(text, "email"), [])

    def test_text_around_the_address(self) -> None:
        self.assertEqual(values("<ola@gmail.com>", "email"), ["ola@gmail.com"])
        self.assertEqual(values("mailto:ola@gmail.com?subject=hi", "email"), ["ola@gmail.com"])
        self.assertEqual(values("(ola@gmail.com).", "email"), ["ola@gmail.com"])
        self.assertEqual(values('"ola@gmail.com",', "email"), ["ola@gmail.com"])

    def test_not_an_address(self) -> None:
        for text in ("user@localhost", "p@127.0.0.1", "@handle", "foo@", "a @ b.com", "x@y"):
            self.assertEqual(values(text, "email"), [], text)

    def test_two_addresses_in_a_line(self) -> None:
        self.assertEqual(
            values("a@gmail.com, b@gmail.com", "email"), ["a@gmail.com", "b@gmail.com"]
        )

    def test_unicode_address(self) -> None:
        self.assertEqual(values("олена@приклад.укр", "email"), ["олена@приклад.укр"])

    def test_punycode_domain(self) -> None:
        self.assertEqual(
            values("ola@xn--80aaxitdbjk.xn--p1ai", "email"), ["ola@xn--80aaxitdbjk.xn--p1ai"]
        )
        self.assertEqual(values("ola@host.xn--example", "email"), ["ola@host.xn--example"])

    def test_zero_width_and_fullwidth_do_not_hide_an_address(self) -> None:
        self.assertEqual(values("ola\u200b@gmail.com", "email"), ["ola@gmail.com"])
        self.assertEqual(values("ola@gma\u00adil.com", "email"), ["ola@gmail.com"])
        self.assertEqual(values("ola\uff20gmail.com", "email"), ["ola@gmail.com"])
        self.assertEqual(values("ola@\ufeffgmail.com", "email"), ["ola@gmail.com"])

    def test_masked_value_does_not_contain_the_address(self) -> None:
        (_, _, value, shown) = cp.scan_text("ola.k@gmail.com")[0]
        self.assertEqual(value, "ola.k@gmail.com")
        self.assertNotIn("ola.k", shown)
        self.assertTrue(shown.endswith("@gmail.com"))


class Phone(unittest.TestCase):
    def test_international_forms(self) -> None:
        for text in (
            "+380 67 123 45 67",
            "+380-67-123-45-67",
            "+380671234567",
            "+38 (067) 123-45-67",
            "+380.67.123.45.67",
            "call +49 30 12345678 now",
        ):
            self.assertEqual(len(values(text, "phone")), 1, text)

    def test_digits_are_normalised(self) -> None:
        self.assertEqual(values("+380 67 123 45 67", "phone"), ["380671234567"])
        self.assertEqual(values("+38 (067) 123-45-67", "phone"), ["380671234567"])

    def test_arabic_indic_digits(self) -> None:
        self.assertEqual(values("+٣٨٠ ٦٧ ١٢٣ ٤٥ ٦٧", "phone"), ["380671234567"])

    def test_fullwidth_digits(self) -> None:
        self.assertEqual(values("+３８０ ６７ １２３ ４５ ６７", "phone"), ["380671234567"])

    def test_text_after_the_number_is_not_part_of_it(self) -> None:
        self.assertEqual(values("+380 67 123 45 67 2026", "phone"), ["380671234567"])
        self.assertEqual(values("+380 67 123 45 67, 2026", "phone"), ["380671234567"])

    def test_fictional_north_american_range_passes(self) -> None:
        for text in ("+1 202 555 0143", "+1.202.555.0143", "(202) 555-0143", "202-555-0199"):
            self.assertEqual(values(text, "phone"), [], text)

    def test_only_the_reserved_555_range_passes(self) -> None:
        self.assertEqual(values("+1 202 555 0243", "phone"), ["12025550243"])
        self.assertEqual(values("202-555-0200", "phone"), ["2025550200"])
        self.assertEqual(values("202-555-0099", "phone"), ["2025550099"])
        self.assertEqual(values("202-555-0100", "phone"), [])
        self.assertEqual(values("202-555-0199", "phone"), [])

    def test_north_american_forms(self) -> None:
        self.assertEqual(values("(415) 867-5309", "phone"), ["4158675309"])
        self.assertEqual(values("415-867-5309", "phone"), ["4158675309"])
        self.assertEqual(values("415.867.5309", "phone"), ["4158675309"])

    def test_numbers_that_are_not_phones(self) -> None:
        for text in (
            'viewBox="933 266 1229 1229"',
            "x: 1920 + 1280 - 8 - 320",
            "86400000 + 5",
            "ms = +86400000",
            "a+380671234567",
            "1.0.0+20261127",
            "+12",
            "2026-11-27",
            "+1.5",
            "id 380671234567",
            "ISBN 978-3-16-148410-0",
        ):
            self.assertEqual(values(text, "phone"), [], text)

    def test_masked_value_hides_the_middle(self) -> None:
        (_, _, _, shown) = cp.scan_text("+380 67 123 45 67")[0]
        self.assertEqual(shown, "+38...67")


class Iban(unittest.TestCase):
    DE = "DE89 3704 0044 0532 0130 00"

    def test_spaced_and_compact(self) -> None:
        self.assertEqual(values(self.DE, "iban"), ["DE89370400440532013000"])
        self.assertEqual(
            values("iban: DE89370400440532013000.", "iban"), ["DE89370400440532013000"]
        )

    def test_lower_case(self) -> None:
        self.assertEqual(values(self.DE.lower(), "iban"), ["DE89370400440532013000"])

    def test_word_after_the_number_is_not_part_of_it(self) -> None:
        self.assertEqual(values("LT12 1000 0111 0100 1000 OK", "iban"), ["LT121000011101001000"])
        self.assertEqual(
            values("PL61 1090 1014 0000 0712 1981 2874 PLN", "iban"),
            ["PL61109010140000071219812874"],
        )

    def test_wrong_check_digits_are_found_when_written_in_groups(self) -> None:
        self.assertEqual(values("DE00 3704 0044 0532 0130 00", "iban"), ["DE00370400440532013000"])

    def test_compact_with_wrong_check_digits_is_not_an_iban(self) -> None:
        self.assertEqual(values("DE00370400440532013000", "iban"), [])

    def test_hyphenated_guid_is_not_an_iban(self) -> None:
        self.assertEqual(values('ID = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"', "iban"), [])

    def test_wrong_length_or_country(self) -> None:
        self.assertEqual(values("DE89 3704 0044 0532 01", "iban"), [])
        self.assertEqual(values("ZZ89 3704 0044 0532 0130 00", "iban"), [])

    def test_letters_stuck_to_a_spaced_number_are_not_an_iban(self) -> None:
        self.assertEqual(values("DE89 3704 0044 0532 0130 00x", "iban"), [])
        self.assertEqual(
            values("DE89 3704 0044 0532 0130 00 x", "iban"), ["DE89370400440532013000"]
        )

    def test_gpg_fingerprint_is_not_an_iban(self) -> None:
        self.assertEqual(values("DF20 38AD A1C1 ABFD 2A67 D5CA DF01 0638", "iban"), [])

    def test_inside_a_longer_word(self) -> None:
        self.assertEqual(values("xDE89370400440532013000", "iban"), [])
        self.assertEqual(values("DE89370400440532013000x", "iban"), [])

    def test_checksum_function(self) -> None:
        self.assertTrue(cp.iban_checksum_ok("GB82WEST12345698765432"))
        self.assertFalse(cp.iban_checksum_ok("GB83WEST12345698765432"))


class HomeFolders(unittest.TestCase):
    def test_named_users_are_found(self) -> None:
        self.assertEqual(values("cd /home/olena/docs", "home"), ["olena"])
        self.assertEqual(values("/Users/Olena/Desktop", "home"), ["olena"])
        self.assertEqual(values(r"C:\Users\Olena\Desktop", "home"), ["olena"])
        self.assertEqual(values("C:\\\\Users\\\\olena\\\\x", "home"), ["olena"])
        self.assertEqual(values("c:/Users/olena/x", "home"), ["olena"])

    def test_var_home_of_immutable_systems(self) -> None:
        self.assertEqual(values("cd /var/home/olena", "home"), ["olena"])
        self.assertEqual(values("/var/home/user/x", "home"), [])

    def test_name_with_a_space_on_windows(self) -> None:
        self.assertEqual(values(r"C:\Users\Anna Kowalska\notes.txt", "home"), ["anna kowalska"])

    def test_text_after_the_name_is_not_part_of_it(self) -> None:
        self.assertEqual(values(r"(C:\Users\anna)", "home"), ["anna"])
        self.assertEqual(values(r"C:\Users\anna id 5", "home"), ["anna"])
        self.assertEqual(values("/home/anna:/bin/sh", "home"), ["anna"])
        self.assertEqual(values("'/home/anna'", "home"), ["anna"])

    def test_placeholders_pass(self) -> None:
        for text in (
            "/home/user/x",
            "/home/username",
            "/home/<user>/x",
            "/home/$USER/x",
            "/home/${USER}/x",
            "/home/%USER%",
            "/home/{name}",
            "/home/a/b",
            "/home/x",
            "/Users/you/x",
            "/home/~",
            "/home/",
            "/home",
            r"C:\Users\<name>\x",
            r"C:\Users\%USERNAME%\x",
            r"C:\Users\Public\x",
            r"C:\Users\Default\x",
            r"C:\Users\All Users\x",
            r"C:\Users\x",
            "/home/User/x",
        ):
            self.assertEqual(values(text, "home"), [], text)

    def test_not_a_home_folder(self) -> None:
        for text in (
            "/usr/home/olena",
            "src/home/olena.py",
            "/tmp/Users/olena",
            "./home/olena",
            "http://example.org/home/olena",
        ):
            self.assertEqual(values(text, "home"), [], text)

    def test_two_in_a_line(self) -> None:
        self.assertEqual(values("/home/ola and /home/ivan", "home"), ["ola", "ivan"])

    def test_masked_value_hides_the_name(self) -> None:
        (_, _, _, shown) = cp.scan_text("/home/olena/x")[0]
        self.assertEqual(shown, "/home/o...")


class IpAddresses(unittest.TestCase):
    def test_public_ipv4_is_found(self) -> None:
        self.assertEqual(values("server 93.184.216.34 up", "ip"), ["93.184.216.34"])
        self.assertEqual(values("8.8.8.8", "ip"), ["8.8.8.8"])
        self.assertEqual(values("1.1.1.1:53", "ip"), ["1.1.1.1"])
        self.assertEqual(values("http://93.184.216.34/x", "ip"), ["93.184.216.34"])

    def test_allowed_ipv4(self) -> None:
        for addr in (
            "10.1.2.3",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.1.5",
            "127.0.0.1",
            "0.0.0.0",
            "169.254.1.1",
            "192.0.2.7",
            "198.51.100.7",
            "203.0.113.7",
            "255.255.255.0",
            "255.255.255.255",
            "224.0.0.1",
        ):
            self.assertEqual(values(f"ip {addr} ok", "ip"), [], addr)

    def test_edges_of_the_private_ranges(self) -> None:
        self.assertEqual(values("172.32.0.1", "ip"), ["172.32.0.1"])
        self.assertEqual(values("172.15.255.255", "ip"), ["172.15.255.255"])
        self.assertEqual(values("11.0.0.1", "ip"), ["11.0.0.1"])
        self.assertEqual(values("192.169.0.1", "ip"), ["192.169.0.1"])
        self.assertEqual(values("203.0.114.1", "ip"), ["203.0.114.1"])

    def test_not_an_address(self) -> None:
        for text in (
            "version 1.4.335.0",
            "999.1.1.1",
            "256.1.1.1",
            "1.2.3",
            "1.2.3.4.5",
            "v1.2.3.4x",
            "pkg@1.2.3.4",
            "1.2.3.4-rc1",
            "a1.2.3.4",
            "1.2.3.4a",
        ):
            self.assertEqual(values(text, "ip"), [], text)

    def test_leading_zeros_are_read_as_decimal(self) -> None:
        self.assertEqual(values("050.123.45.67", "ip"), ["50.123.45.67"])

    def test_sentence_end_after_the_address(self) -> None:
        self.assertEqual(values("see 93.184.216.34.", "ip"), ["93.184.216.34"])

    def test_public_ipv6_is_found(self) -> None:
        self.assertEqual(values("addr 2a01:4f9:c01f:8002::", "ip"), ["2a01:4f9:c01f:8002::"])
        self.assertEqual(values("[2606:4700::1111]:53", "ip"), ["2606:4700::1111"])
        self.assertEqual(
            values("2001:4860:4860:0000:0000:0000:0000:8888", "ip"), ["2001:4860:4860::8888"]
        )

    def test_allowed_ipv6(self) -> None:
        for addr in (
            "::1",
            "::",
            "2001:db8::1",
            "2001:DB8:0:0:0:0:0:1",
            "fd12:3456:789a::1",
            "fe80::1",
            "fe80::1%eth0",
            "ff02::1",
            "::ffff:192.168.1.5",
        ):
            self.assertEqual(values(f"ip {addr} ok", "ip"), [], addr)

    def test_documentation_prefix_edge(self) -> None:
        self.assertEqual(values("2001:db9::1", "ip"), ["2001:db9::1"])
        self.assertEqual(values("2001:db7::1", "ip"), ["2001:db7::1"])

    def test_mapped_public_ipv4(self) -> None:
        self.assertEqual(values("::ffff:8.8.8.8", "ip"), ["::ffff:8.8.8.8"])

    def test_times_macs_and_paths_are_not_addresses(self) -> None:
        for text in (
            "at 18:00:15",
            "aa:bb:cc:dd:ee:ff",
            "std::fmt::Display",
            "Foo::bar",
            "a::b",
            "key: value",
            "12:30",
            "ratio 1:2:3",
            "http://host:8080/x",
        ):
            self.assertEqual(values(text, "ip"), [], text)

    def test_masked_value_hides_the_host_part(self) -> None:
        (_, _, _, shown) = cp.scan_text("93.184.216.34")[0]
        self.assertEqual(shown, "93.184.*.*")


class Normalisation(unittest.TestCase):
    def test_ascii_line_is_unchanged(self) -> None:
        self.assertEqual(cp.normalise_line("a@b.c"), "a@b.c")

    def test_format_characters_are_removed(self) -> None:
        self.assertEqual(cp.normalise_line("a\u200bb\u2060c\u00add\ufeff"), "abcd")

    def test_nfkc(self) -> None:
        self.assertEqual(cp.normalise_line("\uff21\uff20"), "A@")

    def test_line_numbers_and_crlf(self) -> None:
        found = cp.scan_text("ok\r\nolena@gmail.com\r\n\r\n/home/ivan/x\r\n")
        self.assertEqual([(n, k) for n, k, _, _ in found], [(2, "email"), (4, "home")])

    def test_no_trailing_newline(self) -> None:
        self.assertEqual([k for _, k, _, _ in cp.scan_text("olena@gmail.com")], ["email"])

    def test_empty_text(self) -> None:
        self.assertEqual(cp.scan_text(""), [])


class Decoding(unittest.TestCase):
    def test_utf8_and_bom(self) -> None:
        self.assertEqual(cp.decode(b"a@b.c"), "a@b.c")
        self.assertEqual(cp.decode(b"\xef\xbb\xbfa@b.c"), "a@b.c")

    def test_utf16_with_bom(self) -> None:
        self.assertEqual(cp.decode("a@b.c".encode("utf-16")), "a@b.c")
        self.assertEqual(cp.decode(b"\xfe\xff" + "a@b.c".encode("utf-16-be")), "a@b.c")

    def test_utf32_with_bom(self) -> None:
        self.assertEqual(cp.decode("a@b.c".encode("utf-32")), "a@b.c")

    def test_utf16_without_bom(self) -> None:
        self.assertEqual(cp.decode("olena@gmail.com\n".encode("utf-16-le")), "olena@gmail.com\n")
        self.assertEqual(cp.decode("olena@gmail.com\n".encode("utf-16-be")), "olena@gmail.com\n")

    def test_binary_is_none(self) -> None:
        self.assertIsNone(cp.decode(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00\x01\x02" * 50))
        self.assertIsNone(cp.decode(b"MZ\x00\x00\x03\x00\x00\x00"))

    def test_invalid_utf8_is_replaced(self) -> None:
        self.assertEqual(cp.decode(b"a\xff\xfeb")[0], "a")  # type: ignore[index]

    def test_empty(self) -> None:
        self.assertEqual(cp.decode(b""), "")


class Repo(unittest.TestCase):
    def test_clean_repo(self) -> None:
        result = check({"a.md": "mail a@example.org, host 10.0.0.1, /home/user/x\n"})
        self.assertEqual(result.problems, [])
        self.assertEqual(result.files, 1)

    def test_error_message_has_path_line_and_masked_value(self) -> None:
        msgs = errors({"docs/a.md": "ok\nwrite olena.k@gmail.com\n"})
        self.assertEqual(len(msgs), 1)
        self.assertTrue(msgs[0].startswith("docs/a.md: line 2: "), msgs[0])
        self.assertNotIn("olena.k", msgs[0])

    def test_every_kind_is_an_error(self) -> None:
        text = (
            "olena@gmail.com\n+380 67 123 45 67\nDE89 3704 0044 0532 0130 00\n/home/olena\n"
            "93.184.216.34\n"
        )
        self.assertEqual(len(errors({"a.txt": text})), 5)

    def test_utf16_file_with_bom_is_searched(self) -> None:
        data = "x\nolena@gmail.com\n".encode("utf-16")
        self.assertEqual(len(errors({"a.txt": data})), 1)

    def test_utf16_file_without_bom_is_searched(self) -> None:
        data = "x\nolena@gmail.com\n".encode("utf-16-le")
        self.assertEqual(len(errors({"a.txt": data})), 1)

    def test_utf8_bom_file_is_searched(self) -> None:
        self.assertEqual(len(errors({"a.txt": b"\xef\xbb\xbfolena@gmail.com"})), 1)

    def test_binary_file_under_a_text_name_is_skipped(self) -> None:
        data = b"\x00\x01\x02olena@gmail.com\x00\x00" * 10
        result = check({"a.txt": data})
        self.assertEqual(result.problems, [])
        self.assertEqual(result.skipped, 1)

    def test_known_binary_suffix_is_skipped(self) -> None:
        result = check({"a.png": "olena@gmail.com"})
        self.assertEqual((result.problems, result.skipped), ([], 1))

    def test_crlf_and_no_final_newline(self) -> None:
        self.assertEqual(len(errors({"a.txt": "x\r\nolena@gmail.com"})), 1)

    def test_names_with_spaces_and_unicode(self) -> None:
        msgs = errors({"docs/мій файл.md": "olena@gmail.com", "a b/c d.txt": "/home/olena"})
        self.assertEqual(len(msgs), 2)
        self.assertTrue(any(m.startswith("docs/мій файл.md: ") for m in msgs))

    def test_symlink_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.txt"
            outside.write_text("olena@gmail.com\n")
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "out.txt").symlink_to(outside)
            (repo / "self.txt").symlink_to("self.txt")
            (repo / "dangling.txt").symlink_to("nowhere.txt")
            result = cp.check_repo(
                repo, ["out.txt", "self.txt", "dangling.txt"], (), (), frozenset()
            )
        self.assertEqual(result.problems, [])
        self.assertEqual(result.skipped, 3)

    def test_missing_file_and_folder_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "d").mkdir()
            result = cp.check_repo(Path(tmp), ["gone.txt", "d"], (), (), frozenset())
        self.assertEqual((result.errors, result.skipped), (0, 2))

    def test_unreadable_file_is_an_error_not_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.txt").write_text("x")
            with mock.patch.object(Path, "read_bytes", side_effect=PermissionError(13, "denied")):
                result = cp.check_repo(Path(tmp), ["a.txt"], (), (), frozenset())
        self.assertEqual(result.errors, 1)
        self.assertEqual(str(result.problems[0]), "a.txt: cannot read the file: denied")

    def test_file_over_the_limit_is_an_error(self) -> None:
        msgs = errors({"big.txt": "x" * 100}, max_bytes=50)
        self.assertEqual(len(msgs), 1)
        self.assertIn("over the limit", msgs[0])

    def test_big_file_is_fast(self) -> None:
        line = "a normal line of text with no personal data in it at all\n"
        data = line * (2_000_000 // len(line))
        start = time.monotonic()
        result = check({"big.txt": data})
        self.assertEqual(result.problems, [])
        self.assertLess(time.monotonic() - start, 20)

    def test_only_limits_the_search(self) -> None:
        files = {"a.txt": "olena@gmail.com", "b.txt": "ivan@gmail.com"}
        msgs = errors(files, only=("b.txt",))
        self.assertEqual(len(msgs), 1)
        self.assertTrue(msgs[0].startswith("b.txt: "))


class Pathological(unittest.TestCase):
    """Long lines of the shapes that make a careless regex slow must stay fast."""

    def test_long_lines(self) -> None:
        n = 200_000
        shapes = [
            "a" * n,
            "a@" * n,
            "a@" + "b" * n,
            "1." * n,
            "1.1.1." * (n // 6),
            "a:" * n,
            "ab:" * n,
            "::" * n,
            "+1 " * n,
            "+1" + "0" * n,
            "AB12 " * n,
            "DE89 " + "0000 " * n,
            "/home/" * n,
            "C:\\Users\\" + "a " * n,
            "(123)." * n,
            "\u200b" * n,
            "a." * n + "@b",
        ]
        start = time.monotonic()
        for shape in shapes:
            cp.scan_text(shape)
        self.assertLess(time.monotonic() - start, 30)


class Exceptions(unittest.TestCase):
    ROW = cp.AllowedRow("email", "docs/*", "*@gmail.com", "an invented address in the docs")

    def test_row_allows_a_match(self) -> None:
        result = check({"docs/a.md": "olena@gmail.com"}, exceptions=(self.ROW,))
        self.assertEqual((result.problems, result.allowed), ([], 1))

    def test_row_is_limited_to_its_path(self) -> None:
        msgs = errors({"src/a.py": "olena@gmail.com"}, exceptions=(self.ROW,))
        self.assertEqual(len(msgs), 1)

    def test_row_is_limited_to_its_value(self) -> None:
        msgs = errors({"docs/a.md": "olena@yahoo.com"}, exceptions=(self.ROW,))
        self.assertEqual(len(msgs), 1)

    def test_row_is_limited_to_its_kind(self) -> None:
        msgs = errors({"docs/a.md": "/home/olena"}, exceptions=(self.ROW,))
        self.assertEqual(len(msgs), 1)

    def test_row_of_one_kind_does_not_allow_another_with_the_same_value(self) -> None:
        row = cp.AllowedRow("home", "*", "olena*", "an invented name")
        self.assertEqual(len(errors({"a.txt": "olena@gmail.com"}, exceptions=(row,))), 1)

    def test_alternatives(self) -> None:
        row = cp.AllowedRow("home", "a/*|b/*", "ola|ivan", "invented names")
        files = {"a/x.txt": "/home/ola", "b/x.txt": "/home/ivan", "c/x.txt": "/home/ola"}
        self.assertEqual(len(errors(files, exceptions=(row,))), 1)

    def test_iban_value_is_compared_without_case(self) -> None:
        row = cp.AllowedRow("iban", "*", "DE89370400440532013000", "a specimen IBAN")
        result = check({"a.txt": "DE89 3704 0044 0532 0130 00"}, exceptions=(row,))
        self.assertEqual(result.problems, [])

    def test_unused_row_is_a_warning(self) -> None:
        result = check({"docs/a.md": "nothing"}, exceptions=(self.ROW,))
        self.assertEqual((result.errors, result.warnings), (0, 1))
        self.assertIn("matches nothing", str(result.problems[0]))

    def test_unused_row_is_not_reported_for_a_partial_run(self) -> None:
        result = check({"docs/a.md": "nothing"}, exceptions=(self.ROW,), only=("docs/a.md",))
        self.assertEqual(result.problems, [])

    def test_every_row_of_the_repo_has_a_reason(self) -> None:
        for row in cp.EXCEPTIONS:
            self.assertIn(row.kind, cp.KINDS)
            self.assertGreater(len(row.reason.split()), 4, row)
            self.assertTrue(row.path and row.value, row)

    def test_no_row_of_the_repo_is_a_catch_all(self) -> None:
        for row in cp.EXCEPTIONS:
            if "test_check_personal_data" not in row.path:  # its input is the data itself
                self.assertNotEqual(row.value, "*", row)


class Legacy(unittest.TestCase):
    def test_legacy_is_a_warning(self) -> None:
        legacy = frozenset({("a.txt", "email", cp.legacy_id("olena@gmail.com"))})
        result = check({"a.txt": "olena@gmail.com"}, legacy=legacy)
        self.assertEqual((result.errors, result.warnings), (0, 1))
        self.assertIn("warning: ", str(result.problems[0]))

    def test_a_new_finding_in_a_legacy_file_is_an_error(self) -> None:
        legacy = frozenset({("a.txt", "email", cp.legacy_id("olena@gmail.com"))})
        result = check({"a.txt": "olena@gmail.com\nivan@gmail.com\n"}, legacy=legacy)
        self.assertEqual((result.errors, result.warnings), (1, 1))

    def test_same_value_in_another_file_is_an_error(self) -> None:
        legacy = frozenset({("a.txt", "email", cp.legacy_id("olena@gmail.com"))})
        result = check({"a.txt": "olena@gmail.com", "b.txt": "olena@gmail.com"}, legacy=legacy)
        self.assertEqual((result.errors, result.warnings), (1, 1))

    def test_a_fixed_legacy_row_is_a_warning(self) -> None:
        legacy = frozenset({("a.txt", "email", cp.legacy_id("olena@gmail.com"))})
        result = check({"a.txt": "clean"}, legacy=legacy)
        self.assertEqual((result.errors, result.warnings), (0, 1))
        self.assertIn("delete it", str(result.problems[0]))


class RealRepo(unittest.TestCase):
    def test_tables_use_known_kinds(self) -> None:
        for path, kind, value in cp.LEGACY:
            self.assertIn(kind, cp.KINDS)
            self.assertTrue(path and value)

    def test_no_errors_on_the_repo(self) -> None:
        tracked = cp.tracked_files(ROOT)
        result = cp.check_repo(ROOT, tracked)
        self.assertEqual([str(p) for p in result.problems if not p.warning], [])
        self.assertGreater(result.files, 100)

    def test_the_checker_passes_its_own_files(self) -> None:
        own = ("tools/check_personal_data.py", "tools/test_check_personal_data.py")
        tracked = [*cp.tracked_files(ROOT), *own]
        result = cp.check_repo(ROOT, tracked, own)
        self.assertEqual([str(p) for p in result.problems if not p.warning], [])


class Main(unittest.TestCase):
    def run_main(self, args: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cp.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_clean_run_returns_0(self) -> None:
        patches = (
            mock.patch.object(cp, "tracked_files", return_value=["a.txt"]),
            mock.patch.object(cp, "check_repo", return_value=cp.Result(files=1)),
        )
        with patches[0], patches[1]:
            code, out, _ = self.run_main([])
        self.assertEqual(code, 0)
        self.assertIn("1 text files searched", out)

    def test_error_returns_1_and_prints_to_stderr(self) -> None:
        result = cp.Result(problems=[cp.Problem("a.txt", 3, "phone number: +38...67")])
        patches = (
            mock.patch.object(cp, "tracked_files", return_value=["a.txt"]),
            mock.patch.object(cp, "check_repo", return_value=result),
        )
        with patches[0], patches[1]:
            code, out, err = self.run_main([])
        self.assertEqual(code, 1)
        self.assertIn("a.txt: line 3: phone number", err)
        self.assertIn("1 errors", out)

    def test_warnings_alone_return_0(self) -> None:
        result = cp.Result(problems=[cp.Problem("a.txt", 3, "x", warning=True)])
        patches = (
            mock.patch.object(cp, "tracked_files", return_value=["a.txt"]),
            mock.patch.object(cp, "check_repo", return_value=result),
        )
        with patches[0], patches[1]:
            code, out, _ = self.run_main([])
        self.assertEqual(code, 0)
        self.assertIn("1 warnings", out)

    def test_unknown_option_returns_2(self) -> None:
        code, _, err = self.run_main(["--nope"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)

    def test_help_returns_0(self) -> None:
        self.assertEqual(self.run_main(["-h"])[0], 0)

    def test_untracked_file_returns_2(self) -> None:
        with mock.patch.object(cp, "tracked_files", return_value=["a.txt"]):
            code, _, err = self.run_main(["b.txt"])
        self.assertEqual(code, 2)
        self.assertIn("not a tracked file: b.txt", err)

    def test_git_failure_returns_2(self) -> None:
        with mock.patch.object(cp, "tracked_files", side_effect=OSError("no git")):
            code, _, err = self.run_main([])
        self.assertEqual(code, 2)
        self.assertIn("git ls-files", err)

    def test_empty_repo_returns_2(self) -> None:
        with mock.patch.object(cp, "tracked_files", return_value=[]):
            self.assertEqual(self.run_main([])[0], 2)

    def test_real_run_on_the_repo_returns_0(self) -> None:
        code, out, _ = self.run_main([])
        self.assertEqual(code, 0)
        self.assertIn("0 errors", out)

    def test_only_git_ls_files_is_run(self) -> None:
        calls: list[list[str]] = []
        real = cp.subprocess.run

        def spy(cmd: list[str], *a: object, **k: object) -> object:
            calls.append(list(cmd))
            return real(cmd, *a, **k)  # type: ignore[call-overload]

        with mock.patch.object(cp.subprocess, "run", spy):
            self.run_main([])
        self.assertEqual([c[:2] for c in calls], [["git", "ls-files"]])


if __name__ == "__main__":
    unittest.main()
