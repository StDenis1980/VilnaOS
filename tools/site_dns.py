#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The vilnaos.com zone at two DNS providers, and which host serves the site.

    python3 tools/site_dns.py check  [zone.toml]
    python3 tools/site_dns.py diff   [--provider desec|bunny] [--with-site]
    python3 tools/site_dns.py apply  [--provider desec|bunny] [--with-site] [--yes]
    python3 tools/site_dns.py failover statichost|ionos [--yes]
    python3 tools/site_dns.py probe  [--json]

ADR 0032: deSEC and bunny DNS serve the same zone, and neither can copy it
from the other, so site/dns/vilnaos.com.toml is the source and this script
writes it to both. `check` needs no network and runs in CI. `diff`, `apply`
and `failover` need DESEC_TOKEN and BUNNY_API_KEY (GitHub environment `dns`,
or Denys's password manager). `apply` and `failover` only print what they
would change unless they get --yes.

The apex and www (the `site` table) are left alone by `diff` and `apply`
unless --with-site is given, because `failover` may have moved them on
purpose. `probe` asks each site host directly, by address, whether it
serves vilnaos.com properly; site-watch.yml runs it.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZONE_FILE = ROOT / "site" / "dns" / "vilnaos.com.toml"
TYPES = {"A", "AAAA", "CNAME", "MX", "TXT", "SRV", "CAA", "NS"}
HOST_TYPES = {"CNAME", "NS"}  # the value is one host name
SITE_TYPES = {"A", "AAAA", "CNAME"}  # which host serves a site name
DESEC_API = "https://desec.io/api/v1"
BUNNY_API = "https://api.bunny.net"
# bunny DNS record types (API enum). Others (redirect, pull zone, script…)
# are bunny's own and never written by this script.
BUNNY_TYPES = {
    0: "A",
    1: "AAAA",
    2: "CNAME",
    3: "TXT",
    4: "MX",
    8: "SRV",
    9: "CAA",
    12: "NS",
}
BUNNY_TYPE_IDS = {v: k for k, v in BUNNY_TYPES.items()}
PROBE_PATHS = ("/", "/.well-known/security.txt")


class ZoneError(Exception):
    pass


@dataclass(frozen=True, order=True)
class Record:
    """One DNS record in a provider-neutral, comparable form."""

    name: str  # relative, "" is the apex, lower case
    type: str
    ttl: int
    value: str  # zone-file form; host names lower case with a final dot

    def __str__(self) -> str:
        name = self.name or "@"
        return f"{name} {self.ttl} {self.type} {self.value}"


@dataclass
class Zone:
    zone: str
    ttl: int
    records: list[Record]
    site_names: list[str]
    site_ttl: int
    site_hosts: dict[str, dict[str, list[str]]]
    active: str
    problems: list[str] = field(default_factory=list)

    def site_records(self, host: str | None = None) -> list[Record]:
        chosen = self.site_hosts[host or self.active]
        out = []
        for name in self.site_names:
            for rtype in ("A", "AAAA"):
                for value in chosen.get(rtype, []):
                    out.append(Record(name, rtype, self.site_ttl, normalise(rtype, value)))
        return out

    def is_site(self, r: Record) -> bool:
        """The apex and www addresses, which `failover` moves."""
        return r.name in self.site_names and r.type in SITE_TYPES

    def wanted(self, with_site: bool, host: str | None = None) -> list[Record]:
        recs = list(self.records)
        if with_site:
            recs += self.site_records(host)
        return sorted(recs)


def fqdn(host: str) -> str:
    host = host.strip().lower()
    return host if host.endswith(".") else host + "."


def normalise(rtype: str, value: str) -> str:
    """The comparable form of one record value."""
    value = value.strip()
    if rtype in HOST_TYPES:
        return fqdn(value)
    if rtype == "MX":
        prio, host = value.split()
        return f"{int(prio)} {fqdn(host)}"
    if rtype == "SRV":
        prio, weight, port, host = value.split()
        return f"{int(prio)} {int(weight)} {int(port)} {fqdn(host)}"
    if rtype == "CAA":
        flags, tag, rest = value.split(None, 2)
        return f'{int(flags)} {tag.lower()} "{rest.strip().strip(chr(34))}"'
    if rtype == "AAAA":
        import ipaddress

        return str(ipaddress.IPv6Address(value))
    if rtype == "A":
        import ipaddress

        return str(ipaddress.IPv4Address(value))
    return value  # TXT: the text itself, no quotes


def load(path: Path = ZONE_FILE) -> Zone:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    zone = data.get("zone", "")
    ttl = int(data.get("ttl", 3600))
    site = data.get("site", {})
    hosts = site.get("hosts", {})
    active = site.get("active", "")
    records: list[Record] = []
    seen: set[tuple[str, str]] = set()
    for i, rr in enumerate(data.get("rrset", [])):
        where = f"rrset {i + 1} ({rr.get('name', '?')!r} {rr.get('type', '?')})"
        name = str(rr.get("name", "")).lower().rstrip(".")
        rtype = str(rr.get("type", "")).upper()
        if rtype not in TYPES:
            problems.append(f"{where}: unknown type {rtype!r}")
            continue
        if (name, rtype) in seen:
            problems.append(f"{where}: listed twice; put all values in one rrset")
        seen.add((name, rtype))
        values = rr.get("values", [])
        if not values:
            problems.append(f"{where}: no values")
        for v in values:
            try:
                records.append(Record(name, rtype, int(rr.get("ttl", ttl)), normalise(rtype, v)))
            except ValueError as e:
                problems.append(f"{where}: bad value {v!r} ({e})")
    z = Zone(
        zone=zone,
        ttl=ttl,
        records=records,
        site_names=[str(n).lower() for n in site.get("names", [])],
        site_ttl=int(site.get("ttl", ttl)),
        site_hosts=hosts,
        active=active,
        problems=problems,
    )
    return z


def check(z: Zone) -> list[str]:
    """Problems that would break the zone at either provider."""
    problems = list(z.problems)
    if not z.zone or "." not in z.zone:
        problems.append("zone name missing")
    if z.active not in z.site_hosts:
        problems.append(f"site.active {z.active!r} is not one of site.hosts")
    if not z.site_names:
        problems.append("site.names is empty")
    for name, host in z.site_hosts.items():
        if not host.get("A"):
            problems.append(f"site host {name!r} has no A address")
        for rtype, values in host.items():
            if rtype not in ("A", "AAAA"):
                problems.append(f"site host {name!r}: only A and AAAA, not {rtype}")
                continue
            for v in values:
                try:
                    normalise(rtype, v)
                except ValueError:
                    problems.append(f"site host {name!r}: bad {rtype} {v!r}")
    names_types: dict[str, set[str]] = {}
    for r in z.records + (z.site_records() if z.active in z.site_hosts else []):
        names_types.setdefault(r.name, set()).add(r.type)
    for name, types in names_types.items():
        if "CNAME" in types and len(types) > 1:
            problems.append(f"{name or '@'}: CNAME cannot sit beside {sorted(types - {'CNAME'})}")
    for r in z.records:
        if z.is_site(r):
            problems.append(f"{r}: the site's address; set it under [site], not as an rrset")
    if "CNAME" in names_types.get("", set()):
        problems.append("the apex cannot be a CNAME")
    # Both providers must name all four servers at the apex: a resolver
    # trusts the zone's own NS set over the .com delegation, so a provider
    # that lists only itself would pull every resolver it answers onto it.
    apex_ns = [r for r in z.records if r.name == "" and r.type == "NS"]
    if len(apex_ns) < 2:
        problems.append("the apex NS must list the servers of both providers")
    spf = [
        r for r in z.records if r.name == "" and r.type == "TXT" and r.value.startswith("v=spf1")
    ]
    if len(spf) > 1:
        problems.append("more than one SPF record at the apex")
    for r in z.records:
        if r.type == "TXT" and len(r.value.encode()) > 255:
            problems.append(f"{r}: TXT longer than 255 bytes; split it")
        if r.ttl < 60:
            problems.append(f"{r}: TTL below 60 s")
    return problems


# --- providers --------------------------------------------------------------


def _request(method: str, url: str, headers: dict[str, str], body: object = None) -> object:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise ZoneError(f"{method} {url}: HTTP {e.code} {detail}") from None
    return json.loads(raw) if raw else None


def _rel(name: str, zone: str) -> str:
    """deSEC subname or bunny Name → our relative name."""
    name = name.lower().rstrip(".")
    zone = zone.lower().rstrip(".")
    if name in ("", "@", zone):
        return ""
    if name.endswith("." + zone):
        return name[: -len(zone) - 1]
    return name


def _txt_unquote(value: str) -> str:
    """deSEC TXT form ("part" "part") → the text."""
    parts, cur, inside, esc = [], [], False, False
    for ch in value:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            if inside:
                parts.append("".join(cur))
                cur = []
            inside = not inside
        elif inside:
            cur.append(ch)
    return "".join(parts) if parts or not value else value


def _txt_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Desec:
    """deSEC: whole rrsets, written in one bulk PATCH."""

    label = "deSEC"
    skip = {("", "SOA"), ("", "DNSKEY"), ("", "CDS"), ("", "CDNSKEY")}

    def __init__(self, zone: str, token: str, request=_request) -> None:
        self.zone = zone
        self.headers = {"Authorization": f"Token {token}"}
        self.request = request

    def records(self) -> list[Record]:
        url = f"{DESEC_API}/domains/{self.zone}/rrsets/"
        out = []
        for rr in self.request("GET", url, self.headers) or []:
            name, rtype = _rel(rr["subname"], self.zone), rr["type"]
            if (name, rtype) in self.skip:
                continue
            for v in rr["records"]:
                text = _txt_unquote(v) if rtype == "TXT" else v
                out.append(Record(name, rtype, int(rr["ttl"]), normalise(rtype, text)))
        return sorted(out)

    def write(self, want: list[Record], have: list[Record]) -> None:
        keys = {(r.name, r.type) for r in want} | {(r.name, r.type) for r in have}
        bulk = []
        for name, rtype in sorted(keys):
            w = [r for r in want if (r.name, r.type) == (name, rtype)]
            h = [r for r in have if (r.name, r.type) == (name, rtype)]
            if sorted(w) == sorted(h):
                continue
            values = [_txt_quote(r.value) if rtype == "TXT" else r.value for r in w]
            ttl = w[0].ttl if w else (h[0].ttl if h else 3600)
            bulk.append({"subname": name, "type": rtype, "ttl": ttl, "records": values})
        if bulk:
            self.request("PATCH", f"{DESEC_API}/domains/{self.zone}/rrsets/", self.headers, bulk)


class Bunny:
    """bunny DNS: single records, added and deleted one by one."""

    label = "bunny DNS"

    def __init__(self, zone: str, key: str, request=_request) -> None:
        self.zone = zone
        self.headers = {"AccessKey": key}
        self.request = request
        self._zone_id: int | None = None
        self._ids: dict[Record, list[int]] = {}

    def zone_id(self) -> int:
        if self._zone_id is None:
            found = self.request("GET", f"{BUNNY_API}/dnszone?search={self.zone}", self.headers)
            items = [z for z in (found or {}).get("Items", []) if z.get("Domain") == self.zone]
            if not items:
                raise ZoneError(f"bunny DNS has no zone {self.zone}")
            self._zone_id = int(items[0]["Id"])
        return self._zone_id

    @staticmethod
    def to_record(rec: dict, zone: str) -> Record | None:
        rtype = BUNNY_TYPES.get(int(rec["Type"]))
        if rtype is None or rec.get("Disabled"):
            return None
        name = _rel(rec.get("Name") or "", zone)
        v = rec["Value"]
        if rtype == "MX":
            v = f"{rec.get('Priority', 0)} {v}"
        elif rtype == "SRV":
            v = f"{rec.get('Priority', 0)} {rec.get('Weight', 0)} {rec.get('Port', 0)} {v}"
        elif rtype == "CAA":
            v = f'{rec.get("Flags", 0)} {rec.get("Tag", "issue")} "{v}"'
        return Record(name, rtype, int(rec["Ttl"]), normalise(rtype, v))

    @staticmethod
    def to_body(r: Record) -> dict:
        body: dict[str, object] = {"Type": BUNNY_TYPE_IDS[r.type], "Ttl": r.ttl, "Name": r.name}
        parts = r.value.split()
        if r.type == "MX":
            body["Priority"], body["Value"] = int(parts[0]), parts[1].rstrip(".")
        elif r.type == "SRV":
            body["Priority"], body["Weight"], body["Port"] = map(int, parts[:3])
            body["Value"] = parts[3].rstrip(".")
        elif r.type == "CAA":
            body["Flags"], body["Tag"] = int(parts[0]), parts[1]
            body["Value"] = r.value.split(None, 2)[2].strip('"')
        elif r.type in HOST_TYPES:
            body["Value"] = r.value.rstrip(".")
        else:
            body["Value"] = r.value
        return body

    def records(self) -> list[Record]:
        data = self.request("GET", f"{BUNNY_API}/dnszone/{self.zone_id()}", self.headers)
        self._ids = {}
        out = []
        for rec in (data or {}).get("Records", []):
            r = self.to_record(rec, self.zone)
            if r is not None:
                self._ids.setdefault(r, []).append(int(rec["Id"]))
                out.append(r)
        return sorted(out)

    def write(self, want: list[Record], have: list[Record]) -> None:
        zid = self.zone_id()
        if not self._ids:
            self.records()
        # Deletes first: a CNAME cannot sit beside the A that replaces it.
        for r in sorted(set(have) - set(want)):
            for rid in self._ids.get(r, []):
                self.request("DELETE", f"{BUNNY_API}/dnszone/{zid}/records/{rid}", self.headers)
        for r in sorted(set(want) - set(have)):
            self.request("PUT", f"{BUNNY_API}/dnszone/{zid}/records", self.headers, self.to_body(r))


def providers(zone: str, which: str | None) -> list[Desec | Bunny]:
    out: list[Desec | Bunny] = []
    if which in (None, "desec"):
        token = os.environ.get("DESEC_TOKEN")
        if not token:
            raise ZoneError("DESEC_TOKEN is not set")
        out.append(Desec(zone, token))
    if which in (None, "bunny"):
        key = os.environ.get("BUNNY_API_KEY")
        if not key:
            raise ZoneError("BUNNY_API_KEY is not set")
        out.append(Bunny(zone, key))
    return out


def plan(want: list[Record], have: list[Record]) -> tuple[list[Record], list[Record]]:
    return sorted(set(want) - set(have)), sorted(set(have) - set(want))


def sync(
    z: Zone, which: str | None, with_site: bool, yes: bool | None, host: str | None = None
) -> int:
    """diff (yes=None), apply or failover, at one or both providers.

    Returns how many records differ. With `host`, only the site names are
    compared, and only their A, AAAA and CNAME records."""
    want_all = z.wanted(with_site=with_site or bool(host), host=host)
    changed = 0
    for p in providers(z.zone, which):
        have_all = p.records()
        if host:
            want = [r for r in want_all if z.is_site(r)]
            have = [r for r in have_all if z.is_site(r)]
        elif with_site:
            want, have = want_all, have_all
        else:
            want = [r for r in want_all if not z.is_site(r)]
            have = [r for r in have_all if not z.is_site(r)]
        add, remove = plan(want, have)
        print(f"{p.label}: {len(add)} to add, {len(remove)} to remove")
        for r in add:
            print(f"  + {r}")
        for r in remove:
            print(f"  - {r}")
        changed += len(add) + len(remove)
        if yes and (add or remove):
            p.write(want, have)
            print(f"  {p.label}: written")
    if changed and yes is False:
        print("Nothing written. Add --yes to write.")
    return changed


# --- probe -------------------------------------------------------------------


def probe_host(zone: str, address: str, curl: str = "curl") -> list[str]:
    """Problems with one host serving the site, asked by its address."""
    problems = []
    host = zone
    target = f"[{address}]" if ":" in address else address
    for path in PROBE_PATHS:
        cmd = [
            curl,
            "--silent",
            "--show-error",
            "--max-time",
            "20",
            "--output",
            "/dev/null",
            "--dump-header",
            "-",
            "--resolve",
            f"{host}:443:{target}",
            f"https://{host}{path}",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            problems.append(f"{path}: {res.stderr.strip() or f'curl exit {res.returncode}'}")
            continue
        lines = res.stdout.splitlines()
        status = lines[0].split()[1] if lines and len(lines[0].split()) > 1 else "?"
        if status != "200":
            problems.append(f"{path}: HTTP {status}")
        if path == "/" and not any(
            line.lower().startswith("content-security-policy:") for line in lines
        ):
            problems.append("/: no Content-Security-Policy header")
    return problems


def probe(z: Zone, ipv6: bool) -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {}
    for name, host in z.site_hosts.items():
        out[name] = {}
        addrs = list(host.get("A", [])) + (list(host.get("AAAA", [])) if ipv6 else [])
        for addr in addrs:
            out[name][addr] = probe_host(z.zone, addr)
    return out


# --- command line ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--zone-file", type=Path, default=ZONE_FILE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    for name in ("diff", "apply"):
        s = sub.add_parser(name)
        s.add_argument("--provider", choices=("desec", "bunny"))
        s.add_argument("--with-site", action="store_true")
        if name == "apply":
            s.add_argument("--yes", action="store_true")
    f = sub.add_parser("failover")
    f.add_argument("host")
    f.add_argument("--provider", choices=("desec", "bunny"))
    f.add_argument("--yes", action="store_true")
    p = sub.add_parser("probe")
    p.add_argument("--json", action="store_true")
    p.add_argument("--ipv6", action="store_true", help="also probe AAAA addresses")
    args = ap.parse_args(argv)

    z = load(args.zone_file)
    problems = check(z)
    if problems:
        for line in problems:
            print(f"{args.zone_file.name}: {line}", file=sys.stderr)
        return 1
    try:
        if args.cmd == "check":
            n = len(z.records) + len(z.site_records())
            print(f"{z.zone}: {n} records, site on {z.active}")
            return 0
        if args.cmd == "diff":
            return 1 if sync(z, args.provider, args.with_site, yes=None) else 0  # type: ignore[arg-type]
        if args.cmd == "apply":
            sync(z, args.provider, args.with_site, yes=args.yes)
            return 0
        if args.cmd == "failover":
            if args.host not in z.site_hosts:
                print(f"unknown host {args.host!r}; one of {sorted(z.site_hosts)}", file=sys.stderr)
                return 2
            sync(z, args.provider, False, yes=args.yes, host=args.host)
            return 0
        if args.cmd == "probe":
            result = probe(z, args.ipv6)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                for name, addrs in result.items():
                    for addr, probs in addrs.items():
                        state = "ok" if not probs else "; ".join(probs)
                        print(f"{name} {addr}: {state}")
            return 0 if all(not p for a in result.values() for p in a.values()) else 1
    except ZoneError as e:
        print(e, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
