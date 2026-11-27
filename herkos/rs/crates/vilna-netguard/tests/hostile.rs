// SPDX-License-Identifier: GPL-3.0-or-later
//! Hostile input for vilna-netguard (ADR 0027): the output of `wg`, `ip -j`
//! and `resolvectl`, the state file and the file of opened links are read as
//! if someone who wants to break the kill switch wrote them. Nothing here
//! runs a program or touches the network or the firewall: only the parsers
//! and the text they turn into nftables rules or `resolvectl` arguments.
//!
//! Every address is from a documentation or private range.

use std::collections::BTreeSet;
use std::fmt::Write as _;
use std::net::SocketAddr;

use vilna_netguard::dns::{OVERRIDE_FILE, Resolver};
use vilna_netguard::endpoints::{MAX_ENDPOINTS, parse_wg_endpoints};
use vilna_netguard::portal_dns::{
    AUTO_SECONDS, AutoPortal, Connectivity, MAX_DOMAINS, MAX_LINKS, Relaxed, changes,
    default_links, is_tunnel, network_of, parse_domains, parse_relaxed, render_relaxed,
};
use vilna_netguard::ruleset::{PROVIDER_SERVICES, TABLE, TUNNELS, is_service_name, render};
use vilna_netguard::state::{Lan, MAX_LAN, State};

/// A small deterministic generator, so a failure can be repeated.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }

    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }

    /// A string made of the pieces an attacker would try first.
    fn hostile(&mut self, max_pieces: usize) -> String {
        const PIECES: [&str; 40] = [
            "\t",
            "\n",
            "\r",
            " ",
            ";",
            "{",
            "}",
            "\"",
            "'",
            "#",
            "\\",
            "/",
            ":",
            ".",
            "..",
            "[",
            "]",
            "%",
            "@",
            "~",
            "~.",
            "-",
            "--",
            "=",
            "\0",
            "\u{202e}",
            "ї",
            "Ü",
            "wg0",
            "eth0",
            "1.2.3.4",
            "2001:db8::1",
            "51820",
            "0",
            "65536",
            "accept",
            "flush ruleset",
            "DNS=192.0.2.1",
            "[Resolve]",
            "):",
        ];
        let n = self.below(max_pieces + 1);
        (0..n).map(|_| PIECES[self.below(PIECES.len())]).collect()
    }
}

fn on() -> State {
    State {
        enabled: true,
        ..State::default()
    }
}

fn wg(endpoints: &[&str]) -> String {
    endpoints.iter().fold(String::new(), |mut s, e| {
        let _ = writeln!(s, "wg0\tAAAA=\t{e}");
        s
    })
}

fn set(names: &[&str]) -> BTreeSet<String> {
    names.iter().map(|s| (*s).to_owned()).collect()
}

fn routes(devs: &[&str]) -> String {
    let list: Vec<String> = devs
        .iter()
        .map(|d| serde_json::json!({ "dst": "default", "dev": d }).to_string())
        .collect();
    format!("[{}]", list.join(","))
}

// ---------------------------------------------------------------------------
// The rules: one fixed shape, whatever the data
// ---------------------------------------------------------------------------

/// Characters an element of the four sets may have once rendered: addresses,
/// ports, prefixes and the separators nft wants. No quote, brace, `;`, `#`.
fn element_char(c: char) -> bool {
    c.is_ascii_hexdigit() || " .:/,".contains(c)
}

/// The script with the elements of every set taken out, so two scripts can
/// be compared line by line; panics (fails the test) if an element has a
/// character that could end the set or start another statement.
fn shape(script: &str) -> Vec<String> {
    script
        .lines()
        .map(|line| match line.split_once(" elements = { ") {
            Some((head, rest)) => {
                let (items, tail) = rest
                    .split_once(" }; }")
                    .expect("a set with elements ends with ` }; }`");
                assert!(items.chars().all(element_char), "odd element in {line:?}");
                assert_eq!(tail, "", "something after the set in {line:?}");
                format!("{head} }}")
            }
            None => line.to_owned(),
        })
        .collect()
}

/// What every script with the kill switch on looks like, with no data.
fn empty_shape(services: &[&str]) -> Vec<String> {
    shape(&render(&on(), 0, &BTreeSet::new(), services))
}

fn assert_fixed_shape(script: &str, services: &[&str]) {
    assert_eq!(shape(script), empty_shape(services), "{script}");
}

#[test]
fn endpoints_from_hostile_wg_output_cannot_add_rules() {
    let text = [
        "wg0\tK\t1.2.3.4:51820\" accept\n",
        "wg0\tK\t1.2.3.4:51820 }; chain x { accept\n",
        "wg0\tK\t1.2.3.4:51820;flush ruleset\n",
        "wg0\tK\t1.2.3.4:51820 # comment\n",
        "wg0\tK\t[2001:db8::1]:51820}\n",
        "wg0\tK\t{1.2.3.4}:51820\n",
        "wg0\tK\t'1.2.3.4:51820'\n",
        "wg0\tK\t\"[2001:db8::1]:51820\"\n",
        "wg0\tK\t1.2.3.4:51820\\\n",
        "wg0\tK\t1.2.3.4:51820\0\n",
        "wg0\tK\t1.2.3.4\n:51820\n",
        // Hostile interface name and key, honest endpoint: only the address is used.
        "wg0\" accept\n\t\";flush ruleset\t192.0.2.7:51820\n",
        "}{#;\t\"\t198.51.100.9:443\n",
    ]
    .concat();
    let ends = parse_wg_endpoints(&text);
    let got: BTreeSet<String> = ends.iter().map(ToString::to_string).collect();
    assert_eq!(got, set(&["192.0.2.7:51820", "198.51.100.9:443"]));
    let script = render(&on(), 0, &ends, &[]);
    assert_fixed_shape(&script, &[]);
    assert!(script.contains("elements = { 192.0.2.7 . 51820, 198.51.100.9 . 443 }"));
    assert!(!script.contains("flush"));
}

#[test]
fn every_parsed_endpoint_renders_inside_its_set() {
    let mut rng = Rng(0x9e37_79b9_7f4a_7c15);
    for _ in 0..3000 {
        let mut text = String::new();
        for _ in 0..rng.below(6) {
            let endpoint = match rng.below(4) {
                0 => format!("192.0.2.{}:{}", rng.below(256), rng.below(70_000)),
                1 => format!("[2001:db8::{:x}]:{}", rng.below(0xffff), rng.below(70_000)),
                2 => format!("[fe80::1%{}]:51820", rng.below(5)),
                _ => rng.hostile(6),
            };
            let (iface, key) = (rng.hostile(3), rng.hostile(3));
            let _ = writeln!(text, "{iface}\t{key}\t{endpoint}");
        }
        let ends = parse_wg_endpoints(&text);
        for e in &ends {
            assert!(e.port() != 0 && !e.ip().is_unspecified(), "{e}");
        }
        assert_fixed_shape(&render(&on(), 0, &ends, &[]), &[]);
    }
}

#[test]
fn hostile_service_names_never_reach_the_script() {
    let bad = [
        "x.service\" accept",
        "x.service\naccept",
        "x.service;flush ruleset;.service",
        "x.service}",
        "{x}.service",
        "x.service#",
        "#.service",
        "x y.service",
        "../../x.service",
        "/x.service",
        "x\\.service",
        "x'.service",
        "x.service\0",
        "x.service ",
        "x.SERVICE",
        "x.servicex",
        "ї.service",
        "\u{202e}x.service",
        "x.socket",
        "",
    ];
    for name in bad {
        assert!(!is_service_name(name), "{name:?}");
        let script = render(&on(), 0, &BTreeSet::new(), &[name]);
        assert!(!script.contains("cgroupv2"), "{name:?}");
        assert_fixed_shape(&script, &[]);
    }
    let long = format!("{}.service", "a".repeat(121));
    assert_eq!(long.len(), 129);
    assert!(!is_service_name(&long));
    let longest = format!("{}.service", "a".repeat(120));
    assert!(is_service_name(&longest));
    for name in PROVIDER_SERVICES {
        assert!(is_service_name(name), "{name}");
    }
}

#[test]
fn every_service_name_that_passes_is_quoted_safely() {
    let mut rng = Rng(7);
    for _ in 0..5000 {
        let name = format!("{}.service", rng.hostile(4));
        let script = render(&on(), 0, &BTreeSet::new(), &[name.as_str()]);
        if is_service_name(&name) {
            assert!(
                name.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"@._-".contains(&b)),
                "{name:?}"
            );
            let line = format!("    socket cgroupv2 level 2 \"system.slice/{name}\" accept");
            assert!(script.lines().any(|l| l == line), "{name:?}");
            assert_fixed_shape(&script, &[name.as_str()]);
        } else {
            assert_fixed_shape(&script, &[]);
        }
    }
}

#[test]
fn hostile_lan_entries_in_the_state_file_cannot_add_rules() {
    let bad = [
        "192.168.1.0/24\"; accept",
        "192.168.1.0/24 }; chain x { accept",
        "192.168.1.0/24;flush ruleset",
        "192.168.1.0/24 # x",
        "192.168.1.0/24\naccept",
        "192.168.1.0/24/24",
        "192.168.1.0/ 24",
        "{192.168.1.0}/24",
        "192.168.1.0/024x",
        "192.168.1.0/2 4",
        "0.0.0.0/0",
        "8.8.8.0/24",
        "192.0.2.0/24",
        "::/0",
        "::ffff:192.168.1.0/120",
        "::ffff:8.8.8.0/120",
        "2001:db8::/32",
        "fe80::/9",
        "fc00::/6",
        "10.0.0.0/7",
        "10.0.0.0/33",
        "fd00::/129",
        "fd00::%eth0/64",
        "192.168.1.0/4294967320",
    ];
    for entry in bad {
        assert!(Lan::parse(entry).is_err(), "{entry:?} must be refused");
    }
    let json = serde_json::json!({ "enabled": true, "lan": bad }).to_string();
    let state = State::from_json(&json);
    assert!(state.enabled);
    assert!(state.lan.is_empty(), "{:?}", state.lan);
    let script = render(&state, 0, &BTreeSet::new(), &[]);
    assert_fixed_shape(&script, &[]);
    assert!(!script.contains("elements"));
}

#[test]
fn lan_entries_are_rendered_as_the_network_not_as_typed() {
    let state = State::from_json(
        r#"{"enabled": true, "lan": [" 192.168.1.77/24 ", "fd12:3456::abcd/64", "10.1.2.3/8"]}"#,
    );
    let script = render(&state, 0, &BTreeSet::new(), &[]);
    assert_fixed_shape(&script, &[]);
    assert!(script.contains("elements = { 10.0.0.0/8, 192.168.1.0/24 }"));
    assert!(script.contains("elements = { fd12:3456::/64 }"));
}

#[test]
fn the_state_file_is_bounded_and_never_panics() {
    let deep = "[".repeat(100_000);
    assert_eq!(State::from_json(&deep), State::default());
    let deep_lan = format!(r#"{{"enabled": true, "lan": {}}}"#, "[".repeat(100_000));
    assert_eq!(State::from_json(&deep_lan), State::default());
    let many: Vec<String> = (0..10_000)
        .map(|i| format!("10.{}.{}.0/24", i / 256 % 256, i % 256))
        .collect();
    let json = serde_json::json!({ "enabled": true, "lan": many }).to_string();
    let state = State::from_json(&json);
    assert_eq!(state.lan.len(), MAX_LAN);
    assert_fixed_shape(&render(&state, 0, &BTreeSet::new(), &[]), &[]);
    let huge = format!(r#"{{"enabled": true, "x": "{}"}}"#, "a".repeat(1 << 20));
    assert!(State::from_json(&huge).enabled);
    for odd in [
        r#"{"enabled": 1}"#,
        r#"{"enabled": "true"}"#,
        r#"{"enabled": [true]}"#,
        r#"{"enabled": null}"#,
        r#"{"Enabled": true}"#,
    ] {
        assert!(!State::from_json(odd).enabled, "{odd}");
    }
    for odd in [
        r#"{"portal_until": -1}"#,
        r#"{"portal_until": 1.5}"#,
        r#"{"portal_until": 1e30}"#,
        r#"{"portal_until": "99999999999"}"#,
        r#"{"portal_until": 18446744073709551616}"#,
    ] {
        assert_eq!(State::from_json(odd).portal_until, 0, "{odd}");
    }
    // A duplicated key: serde_json keeps the last one, no panic either way.
    let _ = State::from_json(r#"{"enabled": false, "enabled": true}"#);
}

#[test]
fn off_or_in_a_portal_window_the_script_only_removes_the_table() {
    let only_delete = format!("table {TABLE}\ndelete table {TABLE}\n");
    let ends = parse_wg_endpoints(&wg(&["192.0.2.1:51820"]));
    let mut state = State::from_json(r#"{"enabled": false, "lan": ["192.168.1.0/24"]}"#);
    assert_eq!(render(&state, 0, &ends, &PROVIDER_SERVICES), only_delete);
    state.enabled = true;
    state.open_portal(1000, 300).expect("a valid window");
    assert_eq!(render(&state, 1299, &ends, &PROVIDER_SERVICES), only_delete);
    assert_ne!(render(&state, 1300, &ends, &PROVIDER_SERVICES), only_delete);
    assert!(state.open_portal(1000, 301).is_err());
    assert!(state.open_portal(u64::MAX, 300).is_ok());
}

#[test]
fn the_order_of_the_rules_holds_with_any_data() {
    let mut state = on();
    state
        .allow_lan(Lan::parse("192.168.1.0/24").expect("private"))
        .expect("room");
    let ends = parse_wg_endpoints(&wg(&["192.0.2.1:51820", "[2001:db8::1]:51820"]));
    let script = render(&state, 0, &ends, &PROVIDER_SERVICES);
    let at = |needle: &str| script.find(needle).expect(needle);
    assert_eq!(at(&format!("table {TABLE}\ndelete table {TABLE}\n")), 0);
    assert!(at("policy drop;") < at("oifname \"lo\" accept"));
    for t in TUNNELS {
        assert!(
            script.contains(&format!("    oifname \"{t}\" accept\n")),
            "{t}"
        );
    }
    let dns = at("th dport { 53, 853, 5353 } drop");
    assert!(dns < at("ip daddr @lan4 accept"));
    assert!(dns < at("ip6 daddr @lan6 accept"));
    // Braces balance: nothing in the data opened or closed a block.
    let open = script.matches('{').count();
    let close = script.matches('}').count();
    assert_eq!(open, close, "{script}");
}

// ---------------------------------------------------------------------------
// parse_wg_endpoints
// ---------------------------------------------------------------------------

#[test]
fn garbage_gives_no_endpoints_and_no_panic() {
    let big_line = "x".repeat(1 << 20);
    let big_tabs = "\t".repeat(1 << 20);
    let lossy = String::from_utf8_lossy(&[0xff, b'\t', 0xfe, b'\t', 0xc3]).into_owned();
    for text in [
        "",
        "\n\n\n",
        "\t\t\t",
        "wg0",
        "wg0\tK",
        "wg0\tK\t",
        "wg0\tK\t(none)",
        "wg0\tK\toff",
        "wg0 K 192.0.2.1:51820",
        "wg0\tK\t192.0.2.1",
        "wg0\tK\t192.0.2.1:",
        "wg0\tK\t:51820",
        "wg0\tK\t192.0.2.1:51820:1",
        "wg0\tK\t192.0.2.256:51820",
        "wg0\tK\t192.0.2:51820",
        "wg0\tK\t0x7f.0.0.1:51820",
        "wg0\tK\texample.org:51820",
        "wg0\tK\tlocalhost:51820",
        "wg0\tK\t192.0.2.1:-1",
        "wg0\tK\t192.0.2.1:5e4",
        "wg0\tK\t192.0.2.1:٥١٨٢٠",
        "wg0\tK\t１９２.0.2.1:51820",
        &big_line,
        &big_tabs,
        &lossy,
    ] {
        assert!(
            parse_wg_endpoints(text).is_empty(),
            "{:?}",
            &text[..text.len().min(60)]
        );
    }
}

#[test]
fn ipv6_only_in_brackets() {
    let got = parse_wg_endpoints(&wg(&[
        "[2001:db8::1]:51820",
        "[2001:db8::2]:1",
        " [2001:db8::3]:51820 ",
        "2001:db8::4:51820",
        "2001:db8::5",
        "[2001:db8::6]",
        "[2001:db8::7]:",
        "[[2001:db8::8]]:51820",
        "[2001:db8::9:51820",
        "2001:db8::10]:51820",
        "[192.0.2.1]:51820",
        "[::]:51820",
        "[::1]:0",
    ]));
    let got: Vec<String> = got.iter().map(ToString::to_string).collect();
    assert_eq!(
        got,
        [
            "[2001:db8::1]:51820",
            "[2001:db8::2]:1",
            "[2001:db8::3]:51820"
        ]
    );
}

#[test]
fn ports_0_and_65536_are_refused() {
    let got = parse_wg_endpoints(&wg(&[
        "192.0.2.1:0",
        "192.0.2.2:00",
        "192.0.2.3:65536",
        "192.0.2.4:99999999999999999999",
        "[2001:db8::1]:0",
        "[2001:db8::2]:65536",
        "192.0.2.5:1",
        "192.0.2.6:65535",
        "[2001:db8::3]:65535",
        "0.0.0.0:51820",
    ]));
    let got: Vec<String> = got.iter().map(ToString::to_string).collect();
    assert_eq!(
        got,
        ["192.0.2.5:1", "192.0.2.6:65535", "[2001:db8::3]:65535"]
    );
}

#[test]
fn a_megabyte_of_lines_is_cut_at_max_endpoints() {
    let mut text = String::new();
    let mut i = 0u32;
    while text.len() < (1 << 20) {
        let ip = std::net::Ipv4Addr::from(0x0a00_0001 + i);
        let _ = writeln!(text, "wg{}\tAAAAAAAAAAAAAAAA=\t{ip}:51820", i % 4);
        i += 1;
    }
    assert!(i as usize > 10 * MAX_ENDPOINTS);
    let got = parse_wg_endpoints(&text);
    assert_eq!(got.len(), MAX_ENDPOINTS);
    // The first ones are kept, not the last ones.
    let first: SocketAddr = "10.0.0.1:51820".parse().expect("address");
    let last = SocketAddr::from((std::net::Ipv4Addr::from(0x0a00_0001 + i - 1), 51820));
    assert!(got.contains(&first));
    assert!(!got.contains(&last));
    let script = render(&on(), 0, &got, &[]);
    assert_fixed_shape(&script, &[]);
}

#[test]
fn junk_lines_do_not_use_up_the_limit() {
    let mut text = "wg0\tK\tjunk\n".repeat(10 * MAX_ENDPOINTS);
    text.push_str("wg0\tK\t192.0.2.1:51820\n");
    assert_eq!(parse_wg_endpoints(&text).len(), 1);
}

#[test]
fn crlf_output_is_read() {
    let got = parse_wg_endpoints("wg0\tK\t192.0.2.1:51820\r\nwg1\tK\t[2001:db8::1]:51820\r\n");
    assert_eq!(got.len(), 2);
}

// ---------------------------------------------------------------------------
// portal_dns: parse_domains
// ---------------------------------------------------------------------------

#[test]
fn parse_domains_keeps_only_plain_names() {
    let line = "Link 3 (wlan0): hotel.test ~corp.test hotel.test. ~. -x --set=1 ~-x ~~x \
                a;b a\"b a'b a#b a{b} a\\b a/b a,b a=b a:b ї.test münchen.test xn--mnchen-3ya.test \
                \u{202e}x ~ - x\0y _srv.test";
    assert_eq!(
        parse_domains(line),
        [
            "hotel.test",
            "~corp.test",
            "hotel.test.",
            "xn--mnchen-3ya.test",
            "_srv.test"
        ]
    );
}

#[test]
fn parse_domains_splits_on_any_whitespace_so_spaces_cannot_glue_names() {
    let got = parse_domains("Link 3 (wlan0):\thotel.test\u{a0}x   corp.test\u{3000}y\x0bz");
    // NBSP and U+3000 are whitespace for Rust: every word is checked by itself.
    for d in &got {
        assert!(!d.contains(char::is_whitespace), "{d:?}");
    }
    assert!(got.contains(&"hotel.test".to_owned()));
}

#[test]
fn parse_domains_length_limit() {
    let ok = "a".repeat(253);
    let too_long = "a".repeat(254);
    let routing_ok = format!("~{ok}");
    let line = format!("Link 3 (wlan0): {ok} {too_long} {routing_ok} ~{too_long}");
    assert_eq!(parse_domains(&line), [ok.clone(), routing_ok]);
}

#[test]
fn parse_domains_is_bounded() {
    let many: Vec<String> = (0..10_000).map(|i| format!("d{i}.test")).collect();
    let got = parse_domains(&format!("Link 3 (wlan0): {}", many.join(" ")));
    assert_eq!(got.len(), MAX_DOMAINS);
    assert_eq!(got[0], "d0.test");
    let big = format!("Link 3 (wlan0): {}", "a.".repeat(1 << 19));
    assert!(parse_domains(&big).is_empty());
}

#[test]
fn parse_domains_reads_only_the_first_line() {
    assert!(parse_domains("").is_empty());
    assert!(parse_domains("no colon here").is_empty());
    assert!(parse_domains("\nLink 3 (wlan0): hotel.test").is_empty());
    assert_eq!(
        parse_domains("Link 3 (wlan0): a.test\nLink 4 (eth0): b.test"),
        ["a.test"]
    );
    assert_eq!(
        parse_domains("Link 3 (wlan0): a.test\r\nLink 4 (eth0): b.test"),
        ["a.test"]
    );
}

#[test]
fn every_kept_domain_is_safe_as_a_resolvectl_argument() {
    let mut rng = Rng(42);
    for _ in 0..5000 {
        let out = format!("Link 3 (wlan0): {}", rng.hostile(12));
        let got = parse_domains(&out);
        assert!(got.len() <= MAX_DOMAINS);
        for d in got {
            let name = d.strip_prefix('~').unwrap_or(&d);
            assert!(!name.is_empty() && name.len() <= 253, "{d:?}");
            assert!(!name.starts_with('-'), "{d:?}");
            assert_ne!(d, "~.", "the window's own domain is never kept");
            assert!(
                name.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b)),
                "{d:?}"
            );
        }
    }
}

// ---------------------------------------------------------------------------
// portal_dns: default_links and network_of
// ---------------------------------------------------------------------------

#[test]
fn broken_route_json_gives_no_links() {
    let deep_array = "[".repeat(100_000);
    let deep_object = format!(r#"[{{"dev":"eth0","x":{}}}]"#, "[".repeat(100_000));
    let big = format!(r#"[{{"dev":"eth0","x":"{}"}}"#, "a".repeat(1 << 20));
    for out in [
        "",
        "null",
        "{}",
        r#"{"dev":"eth0"}"#,
        "[null, 1, \"eth0\", [], {}]",
        r#"[{"dev":null},{"dev":3},{"dev":["eth0"]},{"dev":{"x":"eth0"}}]"#,
        r#"[{"Dev":"eth0"},{"dev ":"eth0"}]"#,
        r#"[{"dev":"eth0"}"#,
        r#"[{"dev":"eth0"}]]"#,
        r#"[{"dev":"\ud800"}]"#,
        &deep_array,
        &deep_object,
        &big,
    ] {
        assert!(
            default_links(&[out]).is_empty(),
            "{:?}",
            &out[..out.len().min(60)]
        );
        assert!(network_of(&[out]).is_empty());
    }
}

#[test]
fn hostile_link_names_are_left_out() {
    let bad = [
        "",
        "-",
        "--help",
        "-eth0",
        "eth0 eth1",
        "eth0\n",
        "eth0\t",
        "eth0;",
        "eth0\"",
        "eth0'",
        "eth0#",
        "eth0{",
        "eth0}",
        "eth0/1",
        "eth0\\",
        "eth0:1",
        "eth0\0",
        "eth0=1",
        "ethї",
        "\u{202e}eth0",
        "sixteen_chars_xx",
        "wg0",
        "wg0-mullvad",
        "wg",
        "proton0",
        "pvpn-ks",
        "tun0",
        "tun",
    ];
    assert_eq!("sixteen_chars_xx".len(), 16);
    let out = routes(&bad);
    assert!(
        default_links(&[&out]).is_empty(),
        "{:?}",
        default_links(&[&out])
    );
    assert!(network_of(&[&out]).is_empty());
    let good = [
        "eth0",
        "wlp2s0",
        "enp0s31f6",
        "br-lan",
        "eth0.10",
        "veth_1",
        "fifteen_chars_x",
    ];
    assert_eq!(
        default_links(&[&routes(&good)]),
        good.iter()
            .map(|s| (*s).to_owned())
            .collect::<BTreeSet<_>>()
    );
}

#[test]
fn hostile_gateways_do_not_reach_the_network_key() {
    let out = r#"[
        {"dev":"eth0","gateway":"192.0.2.1 x"},
        {"dev":"eth1","gateway":"192.0.2.1\n"},
        {"dev":"eth2","gateway":"fe80::1%eth2"},
        {"dev":"eth3","gateway":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        {"dev":"eth4","gateway":7},
        {"dev":"eth5","gateway":"2001:db8::1"},
        {"dev":"eth6","gateway":"192.0.2.9"}
    ]"#;
    assert_eq!(
        network_of(&[out]),
        set(&[
            "eth0 ",
            "eth1 ",
            "eth2 ",
            "eth3 ",
            "eth4 ",
            "eth5 2001:db8::1",
            "eth6 192.0.2.9"
        ])
    );
}

#[test]
fn links_are_bounded() {
    let many: Vec<String> = (0..1000).map(|i| format!("eth{i}")).collect();
    let many: Vec<&str> = many.iter().map(String::as_str).collect();
    let out = routes(&many);
    assert_eq!(default_links(&[&out, &out]).len(), MAX_LINKS);
    assert_eq!(network_of(&[&out]).len(), MAX_LINKS);
}

#[test]
fn random_route_json_gives_only_plain_non_tunnel_links() {
    let mut rng = Rng(1234);
    for _ in 0..3000 {
        let devs: Vec<String> = (0..rng.below(5)).map(|_| rng.hostile(4)).collect();
        let devs: Vec<&str> = devs.iter().map(String::as_str).collect();
        for link in default_links(&[&routes(&devs)]) {
            assert!(!link.is_empty() && link.len() <= 15, "{link:?}");
            assert!(!link.starts_with('-'), "{link:?}");
            assert!(!is_tunnel(&link), "{link:?}");
            assert!(
                link.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"-_.@".contains(&b)),
                "{link:?}"
            );
        }
    }
}

// ---------------------------------------------------------------------------
// portal_dns: is_tunnel, changes, the file of opened links
// ---------------------------------------------------------------------------

#[test]
fn is_tunnel_on_tricky_names() {
    for yes in [
        "wg0",
        "wg",
        "wg0-mullvad",
        "proton0",
        "proton",
        "pvpn-ks",
        "tun0",
        "tun",
        "tunnel",
        "wg*x",
    ] {
        assert!(is_tunnel(yes), "{yes}");
    }
    for no in [
        "",
        "WG0",
        "Wg0",
        " wg0",
        "\twg0",
        "xwg0",
        "w g0",
        "eth0",
        "wlan0",
        "pvp",
        "prot",
        "tu",
        "ｗg0",
        "wɡ0",
        "\u{200b}wg0",
        "*",
    ] {
        assert!(!is_tunnel(no), "{no:?}");
    }
}

#[test]
fn changes_never_touch_a_tunnel_or_take_an_option() {
    let mut rng = Rng(99);
    for _ in 0..2000 {
        let a: Vec<String> = (0..rng.below(6)).map(|_| rng.hostile(4)).collect();
        let b: Vec<String> = (0..rng.below(6)).map(|_| rng.hostile(4)).collect();
        let a: Vec<&str> = a.iter().map(String::as_str).collect();
        let b: Vec<&str> = b.iter().map(String::as_str).collect();
        let relaxed: BTreeSet<String> = parse_relaxed(&a.join("\n")).into_keys().collect();
        let want = default_links(&[&routes(&b)]);
        for [verb, link, value] in changes(&relaxed, &want) {
            assert_eq!(verb, "dnsovertls");
            assert!(value == "no" || value == "yes");
            assert!(!link.starts_with('-') && !link.is_empty(), "{link:?}");
            assert!(!is_tunnel(&link), "{link:?}");
            assert_eq!(value == "no", want.contains(&link));
        }
    }
}

#[test]
fn changes_close_before_they_open() {
    let got = changes(&set(&["eth0"]), &set(&["wlan0"]));
    assert_eq!(got.len(), 2);
    assert_eq!(got[0][2], "yes");
    assert_eq!(got[1][2], "no");
    assert!(changes(&set(&["eth0"]), &set(&["eth0"])).is_empty());
}

#[test]
fn the_file_of_opened_links_drops_anything_odd() {
    let text = "wlan0\thotel.test ~corp.test\n\
                wg0\thotel.test\n\
                proton0\n\
                -x\tdomain\n\
                eth0 eth1\n\
                \n\
                \t\n\
                eth9\t~. ; -x \"a\" b#c hotel.test\n\
                \x20 enp0s3 \x20\n\
                sixteen_chars_xx\n\
                br0\t\t\t\n";
    let got = parse_relaxed(text);
    let mut want = Relaxed::new();
    want.insert(
        "wlan0".to_owned(),
        Some(vec!["hotel.test".to_owned(), "~corp.test".to_owned()]),
    );
    want.insert("eth9".to_owned(), Some(vec!["hotel.test".to_owned()]));
    want.insert("enp0s3".to_owned(), None);
    want.insert("br0".to_owned(), Some(vec![]));
    assert_eq!(got, want);
}

#[test]
fn the_file_of_opened_links_is_bounded() {
    let text = (0..10_000).fold(String::new(), |mut s, i| {
        let _ = writeln!(s, "eth{i}\thotel.test");
        s
    });
    assert_eq!(parse_relaxed(&text).len(), MAX_LINKS);
    let domains = (0..10_000).fold(String::new(), |mut s, i| {
        let _ = write!(s, "d{i}.test ");
        s
    });
    let got = parse_relaxed(&format!("eth0\t{domains}\n"));
    assert_eq!(got["eth0"].as_ref().map(Vec::len), Some(MAX_DOMAINS));
    let big = "x".repeat(1 << 20);
    assert!(parse_relaxed(&big).is_empty());
}

#[test]
fn parse_render_parse_gives_the_same() {
    let fixed = [
        "",
        "wlan0\n",
        "wlan0\t\n",
        "wlan0\thotel.test ~corp.test\n",
        "wlan0\thotel.test\nwlan0\n",
        "wlan0\nwlan0\tx.test\n",
        "  wlan0 \t  a.test   b.test  \r\neth0\r\n",
        "eth0\t\tb.test\n",
        "eth0\ta.test\tb.test\n",
        "eth0\t~.\n",
    ];
    let mut cases: Vec<String> = fixed.iter().map(|s| (*s).to_owned()).collect();
    let mut rng = Rng(2026);
    for _ in 0..5000 {
        let lines: Vec<String> = (0..rng.below(12))
            .map(|_| match rng.below(3) {
                0 => format!("eth{}\t{}", rng.below(12), rng.hostile(6)),
                1 => format!("eth{}", rng.below(12)),
                _ => rng.hostile(8),
            })
            .collect();
        cases.push(lines.join("\n"));
    }
    for text in cases {
        let first = parse_relaxed(&text);
        let rendered = render_relaxed(&first);
        assert_eq!(parse_relaxed(&rendered), first, "{text:?} -> {rendered:?}");
        // The rendered file has one plain line per link and nothing else.
        assert_eq!(rendered.lines().count(), first.len());
        for line in rendered.lines() {
            assert!(!line.contains('\r') && !line.contains('\0'), "{line:?}");
            assert!(line.matches('\t').count() <= 1, "{line:?}");
        }
    }
}

// ---------------------------------------------------------------------------
// portal_dns: Connectivity and AutoPortal
// ---------------------------------------------------------------------------

#[test]
fn connectivity_takes_only_the_exact_words() {
    assert_eq!(Connectivity::parse("full\n"), Connectivity::Full);
    assert_eq!(Connectivity::parse(" portal "), Connectivity::Blocked);
    assert_eq!(Connectivity::parse("limited"), Connectivity::Blocked);
    for odd in [
        "FULL",
        "full; portal",
        "full\nportal",
        "fu ll",
        "",
        "none",
        "unknown",
        "ful",
    ] {
        assert_eq!(Connectivity::parse(odd), Connectivity::Other, "{odd:?}");
    }
}

#[test]
fn the_automatic_window_survives_extreme_clocks() {
    let wifi = set(&["wlp2s0 192.0.2.1"]);
    let mut auto = AutoPortal::default();
    let step = auto.step(u64::MAX, false, Connectivity::Blocked, &wifi);
    assert!(step.open && step.recheck);
    // The end saturates at u64::MAX, so at u64::MAX it is over.
    assert!(
        !auto
            .step(u64::MAX, false, Connectivity::Blocked, &wifi)
            .open
    );
    let mut auto = AutoPortal::default();
    assert!(auto.step(0, false, Connectivity::Blocked, &wifi).open);
    assert!(!auto.step(1, true, Connectivity::Blocked, &wifi).open);
    assert!(
        !auto
            .step(AUTO_SECONDS, false, Connectivity::Blocked, &wifi)
            .open
    );
}

#[test]
fn the_automatic_window_never_opens_with_the_kill_switch_on() {
    let mut rng = Rng(5);
    let mut auto = AutoPortal::default();
    // A window opened while it was off closes as soon as it is turned on.
    let wifi = set(&["wlp2s0 192.0.2.1"]);
    assert!(auto.step(0, false, Connectivity::Blocked, &wifi).open);
    for t in 0..2000u64 {
        let devs: Vec<String> = (0..rng.below(3))
            .map(|_| format!("eth{}", rng.below(4)))
            .collect();
        let devs: Vec<&str> = devs.iter().map(String::as_str).collect();
        let network = network_of(&[&routes(&devs)]);
        let c = [
            Connectivity::Full,
            Connectivity::Blocked,
            Connectivity::Other,
        ][rng.below(3)];
        let step = auto.step(t * 7, true, c, &network);
        assert!(!step.open && !step.recheck);
    }
}

// ---------------------------------------------------------------------------
// dns
// ---------------------------------------------------------------------------

#[test]
fn resolver_names_are_exact() {
    for odd in [
        "Mullvad",
        "QUAD9",
        " mullvad",
        "mullvad ",
        "mullvad\n",
        "mullvad\nDNS=192.0.2.1",
        "mullvad\0",
        "quad9;",
        "quad9#",
        "mullvad\"",
        "мullvad",
        "",
    ] {
        assert!(Resolver::parse(odd).is_err(), "{odd:?}");
    }
    assert_eq!(Resolver::parse("quad9"), Ok(Resolver::Quad9));
    assert_eq!(Resolver::parse("mullvad"), Ok(Resolver::Mullvad));
    // The error quotes the name with escapes, so it cannot add a line to a log.
    let err = Resolver::parse("x\nERROR forged").expect_err("not a resolver");
    assert!(!err.contains('\n'), "{err:?}");
}

#[test]
fn the_resolved_config_has_a_fixed_shape() {
    assert_eq!(OVERRIDE_FILE, "60-vilna-dns.conf");
    assert_eq!(Resolver::Quad9.override_file(), None);
    let text = Resolver::Mullvad
        .override_file()
        .expect("a file for Mullvad");
    assert!(text.ends_with('\n'));
    let mut keys = Vec::new();
    for line in text.lines() {
        let line = line.trim_start();
        if line.starts_with('#') {
            continue;
        }
        if line == "[Resolve]" {
            keys.push("[Resolve]".to_owned());
            continue;
        }
        let (key, value) = line.split_once('=').expect("key=value");
        assert!(!value.contains('#') || key == "DNS", "{line:?}");
        keys.push(key.to_owned());
    }
    assert_eq!(
        keys,
        ["[Resolve]", "DNS", "Domains", "DNSOverTLS", "FallbackDNS"]
    );
    // The only `#` outside comments is the TLS name of the server.
    let dns = text
        .lines()
        .find_map(|l| l.trim_start().strip_prefix("DNS="))
        .expect("DNS line");
    for server in dns.split(' ') {
        let (addr, name) = server.split_once('#').expect("address#name");
        assert!(addr.parse::<std::net::IpAddr>().is_ok(), "{server}");
        assert_eq!(name, "base.dns.mullvad.net");
    }
}

#[test]
fn reading_the_override_never_panics() {
    let big = "x".repeat(1 << 20);
    let lossy = String::from_utf8_lossy(&[0xff, 0xfe, b'\n', 0xc3]).into_owned();
    for text in [
        "",
        "\0",
        "[Resolve]\nDNS=192.0.2.1\n",
        big.as_str(),
        lossy.as_str(),
    ] {
        assert_eq!(Resolver::from_override(Some(text)), Resolver::Quad9);
    }
    assert_eq!(Resolver::from_override(None), Resolver::Quad9);
    for r in [Resolver::Quad9, Resolver::Mullvad] {
        assert_eq!(Resolver::from_override(r.override_file().as_deref()), r);
        assert_eq!(Resolver::parse(r.name()), Ok(r));
    }
}
