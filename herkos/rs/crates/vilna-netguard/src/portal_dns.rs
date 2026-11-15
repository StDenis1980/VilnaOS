// SPDX-License-Identifier: GPL-3.0-or-later
//! DNS of the network during a captive-portal window (ADR 0027, section 3).
//!
//! Hotel and cafe Wi-Fi often block DNS-over-TLS (port 853) until the person
//! logs in, and the login page has a name only the network's own resolver
//! knows. With strict DNS-over-TLS to Quad9 nothing resolves, so the page
//! never opens. For the window (at most 5 minutes) the network links, never
//! the tunnels, take plain DNS from the network (`resolvectl dnsovertls
//! LINK no`); when the window ends they go back to strict DNS-over-TLS.
//! NetworkManager already gives systemd-resolved the network's resolver for
//! each link, so nothing else changes.

use std::collections::BTreeSet;

use serde_json::Value;

use crate::ruleset::TUNNELS;

/// How many links one window touches at most: a laptop has one or two.
pub const MAX_LINKS: usize = 8;

/// Whether an interface name matches one of the tunnel patterns (`wg*`...).
pub fn is_tunnel(name: &str) -> bool {
    TUNNELS
        .iter()
        .any(|pattern| match pattern.strip_suffix('*') {
            Some(prefix) => name.starts_with(prefix),
            None => name == *pattern,
        })
}

/// A name `resolvectl` may take: what the kernel allows for an interface.
fn plain_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 15
        && !name.starts_with('-')
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.@".contains(&b))
}

/// The network links with a default route, from `ip -j route show default`
/// (and its IPv6 twin, concatenated). Tunnels, odd names and anything past
/// [`MAX_LINKS`] are left out. Output that is not JSON gives no links.
pub fn default_links(json_outputs: &[&str]) -> BTreeSet<String> {
    let mut links = BTreeSet::new();
    for out in json_outputs {
        let Ok(Value::Array(routes)) = serde_json::from_str::<Value>(out) else {
            continue;
        };
        for route in routes {
            let Some(dev) = route.get("dev").and_then(Value::as_str) else {
                continue;
            };
            if plain_name(dev) && !is_tunnel(dev) && links.len() < MAX_LINKS {
                links.insert(dev.to_owned());
            }
        }
    }
    links
}

/// The `resolvectl` arguments that move from `relaxed` (the links opened
/// now) to `want` (the links that should be open): open the new ones, close
/// the ones that are no longer wanted.
pub fn changes(relaxed: &BTreeSet<String>, want: &BTreeSet<String>) -> Vec<[String; 3]> {
    let open = want
        .difference(relaxed)
        .map(|l| ["dnsovertls".to_owned(), l.clone(), "no".to_owned()]);
    let close = relaxed
        .difference(want)
        .map(|l| ["dnsovertls".to_owned(), l.clone(), "yes".to_owned()]);
    close.chain(open).collect()
}

/// The file that lists the opened links, one per line; anything odd in it
/// is dropped, so a broken file can only close fewer links than it should.
pub fn parse_relaxed(text: &str) -> BTreeSet<String> {
    text.lines()
        .map(str::trim)
        .filter(|l| plain_name(l) && !is_tunnel(l))
        .take(MAX_LINKS)
        .map(str::to_owned)
        .collect()
}

pub fn render_relaxed(links: &BTreeSet<String>) -> String {
    links.iter().map(|l| format!("{l}\n")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn set(names: &[&str]) -> BTreeSet<String> {
        names.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn the_network_links_but_never_the_tunnels() {
        let v4 = r#"[{"dst":"default","gateway":"10.0.2.2","dev":"wlp2s0","protocol":"dhcp"},
                     {"dst":"default","dev":"wg0-mullvad"},{"dst":"default","dev":"proton0"}]"#;
        let v6 = r#"[{"dst":"default","gateway":"fe80::1","dev":"enp0s3"},{"dst":"default","dev":"tun0"}]"#;
        assert_eq!(default_links(&[v4, v6]), set(&["enp0s3", "wlp2s0"]));
    }

    #[test]
    fn hostile_output_gives_no_odd_links() {
        assert!(default_links(&["", "not json", "{}", "[1,2]"]).is_empty());
        let odd =
            r#"[{"dev":"--help"},{"dev":"a b"},{"dev":"verylonginterface0"},{"dev":""},{"dev":7}]"#;
        assert!(default_links(&[odd]).is_empty());
        let many: Vec<String> = (0..20).map(|i| format!(r#"{{"dev":"eth{i}"}}"#)).collect();
        let many = format!("[{}]", many.join(","));
        assert_eq!(default_links(&[&many]).len(), MAX_LINKS);
    }

    #[test]
    fn opening_and_closing() {
        let none = BTreeSet::new();
        let wifi = set(&["wlp2s0"]);
        assert_eq!(
            changes(&none, &wifi),
            vec![[
                "dnsovertls".to_owned(),
                "wlp2s0".to_owned(),
                "no".to_owned()
            ]]
        );
        assert!(changes(&wifi, &wifi).is_empty());
        assert_eq!(
            changes(&wifi, &none),
            vec![[
                "dnsovertls".to_owned(),
                "wlp2s0".to_owned(),
                "yes".to_owned()
            ]]
        );
        // Moving from cable to Wi-Fi closes the old link first.
        let moved = changes(&set(&["enp0s3"]), &wifi);
        assert_eq!(moved[0][2], "yes");
        assert_eq!(moved[1][2], "no");
    }

    #[test]
    fn the_file_round_trips_and_drops_junk() {
        let links = set(&["enp0s3", "wlp2s0"]);
        assert_eq!(parse_relaxed(&render_relaxed(&links)), links);
        assert_eq!(parse_relaxed("wlp2s0\n-x\nwg0\n\n a b\n"), set(&["wlp2s0"]));
    }
}
