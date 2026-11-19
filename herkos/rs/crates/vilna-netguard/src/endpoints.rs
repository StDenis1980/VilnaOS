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
    text.lines()
        .filter_map(|line| line.split('\t').nth(2))
        .filter_map(|endpoint| endpoint.trim().parse::<SocketAddr>().ok())
        .filter(|addr| addr.port() != 0 && !addr.ip().is_unspecified())
        .take(MAX_ENDPOINTS)
        .collect()
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
