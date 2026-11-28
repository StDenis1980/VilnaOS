#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/site_dns.py (ADR 0032), with fake provider APIs.

Run: python3 tools/test_site_dns.py
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import site_dns as sd  # after sys.path.insert above


class FakeDesec:
    """Just enough of the deSEC rrset API."""

    def __init__(self, rrsets: list[dict]) -> None:
        self.rrsets = {(r["subname"], r["type"]): r for r in rrsets}

    def __call__(self, method, url, headers, body=None):
        if method == "GET":
            return list(self.rrsets.values())
        assert method == "PATCH"
        for rr in body:
            key = (rr["subname"], rr["type"])
            if rr["records"]:
                self.rrsets[key] = rr
            else:
                self.rrsets.pop(key, None)
        return None


class FakeBunny:
    """Just enough of the bunny DNS zone API."""

    def __init__(self, records: list[dict]) -> None:
        self.records = {r["Id"]: r for r in records}
        self.next_id = 1000

    def __call__(self, method, url, headers, body=None):
        if "dnszone?search=" in url:
            return {"Items": [{"Id": 7, "Domain": "vilnaos.com"}]}
        if method == "GET":
            return {"Records": list(self.records.values())}
        if method == "PUT":
            if body["Name"] == "" and body["Type"] == sd.BUNNY_TYPE_IDS["NS"]:
                raise sd.ZoneError("bunny: NS records are not supported on the root")
            names_types = {(r["Name"], r["Type"]) for r in self.records.values()}
            cname = sd.BUNNY_TYPE_IDS["CNAME"]
            if (body["Name"], cname) in names_types or (
                body["Type"] == cname and any(n == body["Name"] for n, _ in names_types)
            ):
                raise sd.ZoneError("bunny: CNAME conflict")
            self.next_id += 1
            self.records[self.next_id] = {"Id": self.next_id, **body}
            return None
        assert method == "DELETE"
        del self.records[int(url.rsplit("/", 1)[1])]
        return None


class RealZone(unittest.TestCase):
    def test_zone_file_is_valid(self) -> None:
        z = sd.load()
        self.assertEqual(sd.check(z), [])

    def test_every_site_host_serves_both_names(self) -> None:
        z = sd.load()
        for host in z.site_hosts:
            names = {r.name for r in z.site_records(host)}
            self.assertEqual(names, set(z.site_names), host)

    def test_mail_survives_failover(self) -> None:
        # The apex holds MX and SPF as well as the site's address: a failover
        # must never count them as site records.
        z = sd.load()
        apex_mail = [r for r in z.records if r.name == "" and r.type in ("MX", "TXT")]
        self.assertTrue(apex_mail)
        self.assertFalse(any(z.is_site(r) for r in apex_mail))


class Check(unittest.TestCase):
    def zone(self, extra: str) -> sd.Zone:
        base = (ROOT / "site" / "dns" / "vilnaos.com.toml").read_text(encoding="utf-8")
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(base + "\n" + extra)
        return sd.load(Path(f.name))

    def test_cname_beside_other_record_is_refused(self) -> None:
        z = self.zone('[[rrset]]\nname = "flatpak"\ntype = "TXT"\nvalues = ["x"]\n')
        self.assertTrue(any("CNAME cannot sit" in p for p in sd.check(z)))

    def test_second_spf_is_refused(self) -> None:
        z = self.zone('[[rrset]]\nname = ""\ntype = "TXT"\nvalues = ["v=spf1 -all"]\n')
        self.assertTrue(sd.check(z))

    def test_site_address_outside_site_table_is_refused(self) -> None:
        z = self.zone('[[rrset]]\nname = "www"\ntype = "A"\nvalues = ["192.0.2.1"]\n')
        self.assertTrue(any("[site]" in p for p in sd.check(z)))

    def test_zone_without_apex_ns_is_refused(self) -> None:
        # Without it apply would delete each provider's own apex NS.
        text = (ROOT / "site" / "dns" / "vilnaos.com.toml").read_text(encoding="utf-8")
        text = text.replace('type = "NS"', 'type = "TXT"', 1)
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(text)
        self.assertTrue(any("apex NS" in p for p in sd.check(sd.load(Path(f.name)))))

    def test_unknown_active_host_is_refused(self) -> None:
        text = (ROOT / "site" / "dns" / "vilnaos.com.toml").read_text(encoding="utf-8")
        text = text.replace('active = "statichost"', 'active = "nowhere"')
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(text)
        self.assertTrue(sd.check(sd.load(Path(f.name))))


class Providers(unittest.TestCase):
    def setUp(self) -> None:
        self.z = sd.load()

    def run_sync(self, p, **kw) -> int:
        orig = sd.providers
        sd.providers = lambda zone, which: [p]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return sd.sync(self.z, None, **kw)
        finally:
            sd.providers = orig

    def test_desec_apply_then_nothing_left(self) -> None:
        # An empty zone with only the provider's own apex NS.
        api = FakeDesec([{"subname": "", "type": "NS", "ttl": 3600, "records": ["ns1.desec.io."]}])
        p = sd.Desec("vilnaos.com", "t", request=api)
        self.assertGreater(self.run_sync(p, with_site=True, yes=True), 0)
        self.assertEqual(self.run_sync(p, with_site=True, yes=None), 0)
        self.assertIn(("", "NS"), api.rrsets)  # never touched
        spf = api.rrsets[("", "TXT")]["records"][0]
        self.assertTrue(spf.startswith('"') and spf.endswith('"'))

    def test_bunny_apply_then_nothing_left(self) -> None:
        api = FakeBunny([])
        p = sd.Bunny("vilnaos.com", "k", request=api)
        self.assertGreater(self.run_sync(p, with_site=True, yes=True), 0)
        p2 = sd.Bunny("vilnaos.com", "k", request=api)
        self.assertEqual(self.run_sync(p2, with_site=True, yes=None), 0)

    def test_apply_without_site_keeps_a_failover(self) -> None:
        api = FakeDesec([])
        p = sd.Desec("vilnaos.com", "t", request=api)
        self.run_sync(p, with_site=False, yes=True, host="ionos")
        self.run_sync(p, with_site=False, yes=True)
        self.assertEqual(api.rrsets[("", "A")]["records"], ["217.160.125.31"])

    def test_failover_moves_only_site_names(self) -> None:
        for make in (
            lambda: (FakeDesec([]), sd.Desec),
            lambda: (FakeBunny([]), sd.Bunny),
        ):
            api, cls = make()
            self.run_sync(cls("vilnaos.com", "x", request=api), with_site=True, yes=True)
            before = sd.Desec if cls is sd.Desec else sd.Bunny
            others = [r for r in before("vilnaos.com", "x", request=api).records()]
            others = [r for r in others if not self.z.is_site(r)]
            self.run_sync(
                cls("vilnaos.com", "x", request=api), with_site=False, yes=True, host="ionos"
            )
            after = cls("vilnaos.com", "x", request=api).records()
            self.assertEqual([r for r in after if not self.z.is_site(r)], others)
            site = {(r.name, r.type, r.value) for r in after if self.z.is_site(r)}
            self.assertEqual(site, {("", "A", "217.160.125.31"), ("www", "A", "217.160.125.31")})

    def test_bunny_replaces_www_cname_with_address(self) -> None:
        # Today www is a CNAME to statichost; the first failover must not
        # trip over it.
        api = FakeBunny(
            [{"Id": 1, "Type": 2, "Name": "www", "Value": "vilnaos.statichost.page", "Ttl": 3600}]
        )
        p = sd.Bunny("vilnaos.com", "k", request=api)
        self.run_sync(p, with_site=False, yes=True, host="ionos")
        types = {r["Type"] for r in api.records.values() if r["Name"] == "www"}
        self.assertEqual(types, {sd.BUNNY_TYPE_IDS["A"]})

    def test_values_compare_across_spellings(self) -> None:
        # The same MX as bunny and deSEC spell it.
        b = sd.Bunny.to_record(
            {"Type": 4, "Name": "", "Value": "MXEXT1.mailbox.org", "Priority": 10, "Ttl": 3600},
            "vilnaos.com",
        )
        d = sd.Record("", "MX", 3600, sd.normalise("MX", "10 mxext1.mailbox.org."))
        self.assertEqual(b, d)
        self.assertEqual(sd._txt_unquote('"v=DMARC1; " "p=none"'), "v=DMARC1; p=none")


class SpareHost(unittest.TestCase):
    """site/deploy/caddy_site.py: the spare host behaves like statichost."""

    def setUp(self) -> None:
        sys.path.insert(0, str(ROOT / "site" / "deploy"))
        import caddy_site

        self.cs = caddy_site
        self.text = caddy_site.render()

    def test_every_header_reaches_caddy(self) -> None:
        for name, value in self.cs.headers(ROOT / "site" / "public"):
            self.assertIn(f"{name} {self.cs.quote(value)}", self.text)

    def test_every_language_has_its_404(self) -> None:
        langs = [p.name for p in (ROOT / "site" / "public").iterdir() if (p / "404.html").exists()]
        self.assertTrue(langs)
        for lang in langs:
            self.assertIn(lang, self.text.split("@lang path_regexp", 1)[1].split("\n", 1)[0])

    def test_acme_challenge_reaches_the_spare_host(self) -> None:
        # statichost sends Let's Encrypt to ACME_HOST; that name must point at
        # the spare host, and the spare host must serve the challenge files
        # itself after a failover.
        z = sd.load()
        name = self.cs.ACME_HOST.removesuffix("." + z.zone)
        acme = {r.value for r in z.records if r.name == name and r.type == "A"}
        self.assertTrue(acme)
        self.assertEqual(acme, set(z.site_hosts["ionos"]["A"]))
        rules = (ROOT / "site" / "public" / "_redirects").read_text(encoding="utf-8")
        self.assertIn(f"http://{self.cs.ACME_HOST}{self.cs.ACME_PATH}", rules)
        self.assertIn(f"root * {self.cs.ACME_ROOT}", self.text)

    def test_unknown_redirect_rule_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            pub = Path(d)
            (pub / "_headers").write_text("*\n    X-Test: 1\n", encoding="utf-8")
            (pub / "_redirects").write_text("/old /new 302\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.cs.render(pub)


if __name__ == "__main__":
    unittest.main()
