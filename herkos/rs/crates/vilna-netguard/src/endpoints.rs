// SPDX-License-Identifier: GPL-3.0-or-later
//! The servers the WireGuard tunnels of this computer talk to.
//!
//! They come from `wg show all endpoints`, that is from the tunnels that
//! already exist (NetworkManager made them for Proton VPN or an imported
//! `.conf`, or the Mullvad service did), never from a request: nothing Gloxi
//! or a web page says can add a server to the list.

use std::collections::BTreeSet;
use std::net::SocketAddr;

/// More peers than any VPN uses; a longer output is cut, not trusted.
pub const MAX_ENDPOINTS: usize = 256;

/// `wg show all endpoints`: one line per peer, `interface<TAB>public key<TAB>endpoint`,
/// where the endpoint is `1.2.3.4:51820`, `[2001:db8::1]:51820` or `(none)`.
/// Lines that do not look like that are skipped.
pub fn parse_wg_endpoints(text: &str) -> BTreeSet<SocketAddr> {
    let mut out = BTreeSet::new();
    let lines = text.lines().filter_map(|line| {
        let fields: Vec<&str> = line.split('\t').collect();
        (fields.len() == 3).then(|| fields[2])
    });
    for endpoint in lines {
        let Ok(addr) = endpoint.trim().parse::<SocketAddr>() else {
            continue;
        };
        if addr.port() != 0 && is_server(addr.ip()) {
            out.insert(addr);
            // Distinct servers count: copies of one line do not push out the next.
            if out.len() >= MAX_ENDPOINTS {
                break;
            }
        }
    }
    out
}

/// A server on the internet, not this computer, a group or everybody on a network.
fn is_server(ip: std::net::IpAddr) -> bool {
    let broadcast = matches!(ip, std::net::IpAddr::V4(v4) if v4.is_broadcast());
    !ip.is_unspecified() && !ip.is_multicast() && !ip.is_loopback() && !broadcast
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_ipv4_and_ipv6_peers() {
        let text = "proton0\tAAAA=\t185.159.157.1:51820\n\
                    wg0-mullvad\tBBBB=\t[2a03:1b20:3:f011::a01f]:51820\n\
                    wg1\tCCCC=\t(none)\n";
        let got: Vec<String> = parse_wg_endpoints(text)
            .iter()
            .map(ToString::to_string)
            .collect();
        assert_eq!(
            got,
            ["185.159.157.1:51820", "[2a03:1b20:3:f011::a01f]:51820"]
        );
    }

    #[test]
    fn hostile_lines_are_skipped() {
        let text = "wg0\tAAAA=\t1.2.3.4:51820; flush ruleset\n\
                    wg0\tAAAA=\t0.0.0.0:51820\n\
                    wg0\tAAAA=\t1.2.3.4:0\n\
                    wg0 1.2.3.4:51820\n\
                    \t\t\n\
                    wg0\tAAAA=\t999.1.1.1:1\n";
        assert!(parse_wg_endpoints(text).is_empty());
    }

    #[test]
    fn the_list_is_bounded() {
        let text: String = (0..1000)
            .map(|i| format!("wg0\tK\t10.{}.{}.1:51820\n", i / 250, i % 250))
            .collect();
        assert_eq!(parse_wg_endpoints(&text).len(), MAX_ENDPOINTS);
    }
}
