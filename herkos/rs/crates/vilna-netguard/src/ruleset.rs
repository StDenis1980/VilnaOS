// SPDX-License-Identifier: GPL-3.0-or-later
//! One nftables transaction for the kill switch (ADR 0027, section 3).
//!
//! The script always starts by creating and deleting the table, so `nft -f`
//! replaces it in one step whether it existed or not: there is no moment
//! with half the rules. Turned off (or during a captive-portal window) the
//! script only deletes the table.
//!
//! Only the output chain is filtered: the kill switch is about what leaves
//! the computer. Another table (Mullvad's own firewall in lockdown mode) may
//! drop more; a packet leaves only if every table lets it through, and both
//! let the tunnel through.

use std::collections::BTreeSet;
use std::fmt::Write as _;
use std::net::{IpAddr, SocketAddr};

use crate::state::State;

pub const TABLE: &str = "inet vilna_killswitch";

/// Names of tunnel interfaces: Proton VPN (`proton0`, `pvpn*`), Mullvad
/// (`wg0-mullvad`), NetworkManager's WireGuard (`wg*`) and OpenVPN or
/// wireguard-go (`tun*`). These names are fixed here, never taken from input.
pub const TUNNELS: [&str; 4] = ["wg*", "proton*", "pvpn*", "tun*"];

/// System services whose own traffic may leave outside the tunnel: the VPN
/// provider's service has to reach its API and servers to bring the tunnel
/// up. Only services that exist are named in the rules (nft refuses a
/// cgroup that does not exist), so the list is filtered by the caller.
pub const PROVIDER_SERVICES: [&str; 1] = ["mullvad-daemon.service"];

/// A systemd unit name safe to put inside the nft script.
pub fn is_service_name(name: &str) -> bool {
    name.ends_with(".service")
        && name.len() <= 128
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"@._-".contains(&b))
}

/// The nft script for `state` at time `now` (Unix seconds).
///
/// `endpoints`: servers of the tunnels that are up (`endpoints::parse_wg_endpoints`).
/// `services`: provider services that are running now (from [`PROVIDER_SERVICES`]).
pub fn render(
    state: &State,
    now: u64,
    endpoints: &BTreeSet<SocketAddr>,
    services: &[&str],
) -> String {
    let mut s = format!("table {TABLE}\ndelete table {TABLE}\n");
    if !state.enabled || state.portal_open(now) {
        return s;
    }
    let (v4, v6): (Vec<_>, Vec<_>) = endpoints.iter().partition(|a| a.is_ipv4());
    let lan4: Vec<String> = state
        .lan
        .iter()
        .filter(|l| l.addr.is_ipv4())
        .map(ToString::to_string)
        .collect();
    let lan6: Vec<String> = state
        .lan
        .iter()
        .filter(|l| l.addr.is_ipv6())
        .map(ToString::to_string)
        .collect();
    let elements = |items: Vec<String>| {
        if items.is_empty() {
            String::new()
        } else {
            format!(" elements = {{ {} }};", items.join(", "))
        }
    };
    let pairs = |list: &[&SocketAddr]| {
        list.iter()
            .map(|a| match a.ip() {
                IpAddr::V4(ip) => format!("{ip} . {}", a.port()),
                IpAddr::V6(ip) => format!("{ip} . {}", a.port()),
            })
            .collect::<Vec<_>>()
    };

    s.push_str(&format!("table {TABLE} {{\n"));
    let _ = writeln!(
        s,
        "  set vpn4 {{ type ipv4_addr . inet_service;{} }}",
        elements(pairs(&v4))
    );
    let _ = writeln!(
        s,
        "  set vpn6 {{ type ipv6_addr . inet_service;{} }}",
        elements(pairs(&v6))
    );
    let _ = writeln!(
        s,
        "  set lan4 {{ type ipv4_addr; flags interval;{} }}",
        elements(lan4)
    );
    let _ = writeln!(
        s,
        "  set lan6 {{ type ipv6_addr; flags interval;{} }}",
        elements(lan6)
    );
    s.push_str("  chain output {\n");
    s.push_str("    type filter hook output priority filter; policy drop;\n");
    s.push_str("    oifname \"lo\" accept\n");
    for tunnel in TUNNELS {
        let _ = writeln!(s, "    oifname \"{tunnel}\" accept");
    }
    // An address from the network: DHCP, DHCPv6 and neighbour discovery.
    s.push_str("    udp sport 68 udp dport 67 accept\n");
    s.push_str("    udp sport 546 udp dport 547 accept\n");
    s.push_str("    icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert, mld2-listener-report } accept\n");
    // The tunnels' own packets to their servers.
    s.push_str("    ip daddr . udp dport @vpn4 accept\n");
    s.push_str("    ip6 daddr . udp dport @vpn6 accept\n");
    for service in services.iter().filter(|n| is_service_name(n)) {
        let _ = writeln!(
            s,
            "    socket cgroupv2 level 2 \"system.slice/{service}\" accept"
        );
    }
    // No DNS outside the tunnel, not even to the router of a trusted LAN.
    s.push_str("    meta l4proto { tcp, udp } th dport { 53, 853, 5353 } drop\n");
    s.push_str("    ip daddr @lan4 accept\n");
    s.push_str("    ip6 daddr @lan6 accept\n");
    s.push_str("  }\n}\n");
    s
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::endpoints::parse_wg_endpoints;
    use crate::state::Lan;

    fn on() -> State {
        State {
            enabled: true,
            ..State::default()
        }
    }

    #[test]
    fn off_only_removes_the_table() {
        let script = render(&State::default(), 0, &BTreeSet::new(), &[]);
        assert_eq!(
            script,
            "table inet vilna_killswitch\ndelete table inet vilna_killswitch\n"
        );
    }

    #[test]
    fn a_portal_window_opens_and_closes_by_time() {
        let mut s = on();
        s.open_portal(1000, 300).unwrap();
        assert!(!render(&s, 1100, &BTreeSet::new(), &[]).contains("chain output"));
        assert!(render(&s, 1300, &BTreeSet::new(), &[]).contains("chain output"));
    }

    #[test]
    fn on_drops_everything_but_tunnels_and_their_servers() {
        let mut s = on();
        s.allow_lan(Lan::parse("192.168.1.0/24").unwrap()).unwrap();
        let ends = parse_wg_endpoints(
            "proton0\tK\t185.159.157.1:51820\nwg0-mullvad\tK\t[2a03:1b20::1]:51820\n",
        );
        let script = render(&s, 0, &ends, &["mullvad-daemon.service"]);
        assert!(script.contains("policy drop;"));
        assert!(script.contains("elements = { 185.159.157.1 . 51820 }"));
        assert!(script.contains("elements = { 2a03:1b20::1 . 51820 }"));
        assert!(script.contains("elements = { 192.168.1.0/24 }"));
        assert!(script.contains("\"system.slice/mullvad-daemon.service\""));
        // DNS is dropped before the LAN is let through.
        let dns = script.find("dport { 53, 853, 5353 } drop").unwrap();
        let lan = script.find("@lan4 accept").unwrap();
        assert!(dns < lan);
    }

    #[test]
    fn odd_service_names_never_reach_the_script() {
        for bad in [
            "x.service\" accept\n    accept #",
            "../x.service",
            "x service",
            "mullvad",
            "",
        ] {
            assert!(!is_service_name(bad), "{bad:?}");
            let script = render(&on(), 0, &BTreeSet::new(), &[bad]);
            assert!(!script.contains("cgroupv2"), "{bad:?}");
        }
    }

    /// The script must be accepted by the real nft. Runs only where `nft`
    /// is installed and may check rules (`nft -c` needs CAP_NET_ADMIN); it
    /// says so and passes elsewhere.
    #[test]
    fn nft_accepts_the_script() {
        use std::io::Write;
        use std::process::{Command, Stdio};
        let mut s = on();
        s.allow_lan(Lan::parse("192.168.1.0/24").unwrap()).unwrap();
        s.allow_lan(Lan::parse("fd00::/64").unwrap()).unwrap();
        let ends = parse_wg_endpoints("wg0\tK\t1.2.3.4:51820\nwg0\tK\t[2001:db8::1]:51820\n");
        for script in [
            render(&s, 0, &ends, &[]),
            render(&on(), 0, &BTreeSet::new(), &[]),
            render(&State::default(), 0, &BTreeSet::new(), &[]),
        ] {
            let child = Command::new("nft")
                .args(["-c", "-f", "-"])
                .stdin(Stdio::piped())
                .stdout(Stdio::null())
                .stderr(Stdio::piped())
                .spawn();
            let Ok(mut child) = child else {
                eprintln!("nft is not installed: the script was not checked");
                return;
            };
            if let Some(mut stdin) = child.stdin.take() {
                stdin.write_all(script.as_bytes()).unwrap();
            }
            let out = child.wait_with_output().unwrap();
            let err = String::from_utf8_lossy(&out.stderr);
            if err.contains("Operation not permitted") {
                eprintln!("nft may not check rules here: the script was not checked");
                return;
            }
            assert!(out.status.success(), "nft refused:\n{script}\n{err}");
        }
    }
}
