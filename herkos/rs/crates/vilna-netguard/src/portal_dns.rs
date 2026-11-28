// SPDX-License-Identifier: GPL-3.0-or-later
//! DNS of the network during a captive-portal window (ADR 0027, section 3).
//!
//! Hotel and cafe Wi-Fi often block DNS-over-TLS (port 853) until the person
//! logs in, and the login page has a name only the network's own resolver
//! knows. With strict DNS-over-TLS to Quad9 nothing resolves, so the page
//! never opens. For the window (at most 5 minutes) the network links, never
//! the tunnels, take plain DNS from the network (`resolvectl dnsovertls
//! LINK no`) and answer for every name (`resolvectl domain LINK ~.`): the
//! global `Domains=~.` of Vilna would otherwise send every name to Quad9
//! alone, and the login check of GNOME would hang (VM, Build15.2). When the
//! window ends they go back to strict DNS-over-TLS and to the domains
//! NetworkManager gave them, which the service keeps in its file.

use std::collections::{BTreeMap, BTreeSet};

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
        // resolvectl reads "3" as interface number 3, which may be a tunnel.
        && !name.bytes().all(|b| b.is_ascii_digit())
        && name != "."
        && name != ".."
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.@".contains(&b))
}

/// The default routes of the network links, from `ip -j route show default`
/// (and its IPv6 twin): each link with its gateway. Tunnels, odd names and
/// anything past [`MAX_LINKS`] are left out. Output that is not JSON gives
/// none.
fn default_routes(json_outputs: &[&str]) -> BTreeSet<(String, String)> {
    let mut routes = BTreeSet::new();
    for out in json_outputs {
        let Ok(Value::Array(list)) = serde_json::from_str::<Value>(out) else {
            continue;
        };
        for route in list {
            let Some(dev) = route.get("dev").and_then(Value::as_str) else {
                continue;
            };
            let gateway = route
                .get("gateway")
                .and_then(Value::as_str)
                .filter(|g| {
                    g.len() <= 45
                        && g.bytes()
                            .all(|b| b.is_ascii_hexdigit() || b == b'.' || b == b':')
                })
                .unwrap_or("");
            // The limit counts links, not routes: eight gateways of one link
            // must not leave out the next one.
            let links: BTreeSet<&String> = routes.iter().map(|(d, _)| d).collect();
            let room = links.len() < MAX_LINKS || links.iter().any(|d| d.as_str() == dev);
            if plain_name(dev) && !is_tunnel(dev) && room {
                routes.insert((dev.to_owned(), gateway.to_owned()));
            }
        }
    }
    routes
}

/// The network links with a default route.
pub fn default_links(json_outputs: &[&str]) -> BTreeSet<String> {
    default_routes(json_outputs)
        .into_iter()
        .map(|(dev, _)| dev)
        .collect()
}

/// Which network the laptop is on, as far as the window cares: the links
/// with their gateways. The same Wi-Fi after a reconnect is the same
/// network; another hotel on the same `wlan0` is not.
pub fn network_of(json_outputs: &[&str]) -> BTreeSet<String> {
    default_routes(json_outputs)
        .into_iter()
        .map(|(dev, gateway)| format!("{dev} {gateway}"))
        .collect()
}

/// How many search or routing domains of a link are kept at most.
pub const MAX_DOMAINS: usize = 32;

/// A domain `resolvectl domain` may take: a name, or `~name` for routing
/// only; never an option.
fn plain_domain(domain: &str) -> bool {
    let name = domain.strip_prefix('~').unwrap_or(domain);
    !name.is_empty()
        && name.len() <= 253
        && !name.starts_with('-')
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
}

fn domain_list<'a>(words: impl Iterator<Item = &'a str>) -> Vec<String> {
    words
        .filter(|d| *d != "~." && plain_domain(d))
        .take(MAX_DOMAINS)
        .map(str::to_owned)
        .collect()
}

/// The domains of a link, from `resolvectl domain LINK`
/// (`Link 3 (wlan0): hotel.test ~corp`). `~.` is left out: the window adds
/// it and must not keep it afterwards.
pub fn parse_domains(out: &str) -> Vec<String> {
    match out.lines().next().and_then(|l| l.split_once("):")) {
        Some((_, list)) => domain_list(list.split_whitespace()),
        None => Vec::new(),
    }
}

/// The opened links, each with the domains NetworkManager had given it
/// (`None` when they could not be read, so they are not touched on close).
pub type Relaxed = BTreeMap<String, Option<Vec<String>>>;

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

/// The file that lists the opened links, one per line, each with a tab and
/// its old domains (a line with no tab has none to give back). Anything odd
/// in it is dropped, so a broken file can only close fewer links than it
/// should.
pub fn parse_relaxed(text: &str) -> Relaxed {
    text.lines()
        .filter_map(|line| {
            let (name, domains) = match line.split_once('\t') {
                Some((name, list)) => (name.trim(), Some(domain_list(list.split_whitespace()))),
                None => (line.trim(), None),
            };
            (plain_name(name) && !is_tunnel(name)).then(|| (name.to_owned(), domains))
        })
        .take(MAX_LINKS)
        .collect()
}

pub fn render_relaxed(links: &Relaxed) -> String {
    links
        .iter()
        .map(|(link, domains)| match domains {
            Some(d) => format!("{link}\t{}\n", d.join(" ")),
            None => format!("{link}\n"),
        })
        .collect()
}

/// What NetworkManager says about the way to the internet
/// (`nmcli -t -f CONNECTIVITY general`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Connectivity {
    /// The internet answers.
    Full,
    /// The network asks for a login, or names do not resolve (with strict
    /// DNS-over-TLS a portal usually looks like this: the check cannot even
    /// resolve its own address).
    Blocked,
    /// No network, or NetworkManager does not know yet.
    Other,
}

impl Connectivity {
    pub fn parse(out: &str) -> Connectivity {
        match out.trim() {
            "full" => Connectivity::Full,
            "portal" | "limited" => Connectivity::Blocked,
            _ => Connectivity::Other,
        }
    }
}

/// The window the service opens by itself, without Herkos, when the kill
/// switch is off and the network asks for a login (decision of Denys,
/// 2026-11-15). With the kill switch on, only the person opens a window
/// (Herkos, R3). One automatic window per network: it ends when the internet
/// answers or after [`AUTO_SECONDS`], and opens again only after the internet
/// answered or the laptop moved to another network. A link that drops and
/// comes back on the same network does not count (VM, Build15.2: two
/// reconnects kept the DNS open for 15 minutes).
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct AutoPortal {
    until: Option<u64>,
    /// The network the last window was for; `None` once the internet
    /// answered.
    spent_on: Option<BTreeSet<String>>,
}

/// The longest automatic window, the same as a window the person opens.
pub const AUTO_SECONDS: u64 = crate::state::MAX_PORTAL_SECONDS;

/// What the service does after one look at the network.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AutoStep {
    /// The automatic window is open now.
    pub open: bool,
    /// It has just opened: ask NetworkManager to check again, so that GNOME
    /// shows the login page at once and not in five minutes.
    pub recheck: bool,
}

impl AutoPortal {
    /// One look; `network` is [`network_of`] the default routes now, empty
    /// while no link is up.
    pub fn step(
        &mut self,
        now: u64,
        kill_switch: bool,
        connectivity: Connectivity,
        network: &BTreeSet<String>,
    ) -> AutoStep {
        if connectivity == Connectivity::Full {
            self.until = None;
            self.spent_on = None;
        }
        if self.until.is_some_and(|until| until <= now) || kill_switch {
            self.until = None;
        }
        // Another network may have its own window; no network at all (a
        // link down for a moment) changes nothing.
        if !network.is_empty() && self.spent_on.as_ref().is_some_and(|spent| spent != network) {
            self.spent_on = None;
        }
        let mut recheck = false;
        if !kill_switch
            && connectivity == Connectivity::Blocked
            && self.until.is_none()
            && self.spent_on.is_none()
            && !network.is_empty()
        {
            self.until = Some(now.saturating_add(AUTO_SECONDS));
            self.spent_on = Some(network.clone());
            recheck = true;
        }
        AutoStep {
            open: self.until.is_some(),
            recheck,
        }
    }
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
        let mut links = Relaxed::new();
        links.insert("enp0s3".into(), None);
        links.insert(
            "wlp2s0".into(),
            Some(vec!["hotel.test".into(), "~corp".into()]),
        );
        links.insert("eth1".into(), Some(Vec::new()));
        assert_eq!(parse_relaxed(&render_relaxed(&links)), links);
        let junk = parse_relaxed("wlp2s0\n-x\nwg0\n\n a b\nenp1s0\t~. --set x ok.test\n");
        assert_eq!(junk.len(), 2);
        assert_eq!(junk["wlp2s0"], None);
        assert_eq!(junk["enp1s0"], Some(vec!["x".into(), "ok.test".into()]));
    }

    #[test]
    fn the_domains_of_a_link() {
        assert_eq!(
            parse_domains("Link 3 (wlan0): hotel.test ~corp ~.\n"),
            vec!["hotel.test".to_owned(), "~corp".to_owned()]
        );
        assert!(parse_domains("Link 3 (wlan0):\n").is_empty());
        assert!(parse_domains("").is_empty());
        assert!(parse_domains("Link 3 (wlan0): --help a;b ~").is_empty());
        let many = format!("Link 3 (wlan0):{}", " a.test".repeat(100));
        assert_eq!(parse_domains(&many).len(), MAX_DOMAINS);
    }

    #[test]
    fn a_network_is_its_links_with_their_gateways() {
        let hotel = r#"[{"dst":"default","gateway":"10.0.2.2","dev":"wlan0"},{"dev":"wg0"}]"#;
        let other = r#"[{"dst":"default","gateway":"192.168.1.1","dev":"wlan0"}]"#;
        let odd = r#"[{"dst":"default","gateway":"1.2.3.4; rm","dev":"wlan0"}]"#;
        assert_eq!(network_of(&[hotel]), set(&["wlan0 10.0.2.2"]));
        assert_ne!(network_of(&[hotel]), network_of(&[other]));
        assert_eq!(network_of(&[odd]), set(&["wlan0 "]));
        assert_eq!(default_links(&[hotel, other]), set(&["wlan0"]));
    }

    #[test]
    fn connectivity_words() {
        assert_eq!(Connectivity::parse("full\n"), Connectivity::Full);
        assert_eq!(Connectivity::parse("portal"), Connectivity::Blocked);
        assert_eq!(Connectivity::parse("limited"), Connectivity::Blocked);
        for other in ["none", "unknown", "", "FULL", "garbage"] {
            assert_eq!(Connectivity::parse(other), Connectivity::Other);
        }
    }

    #[test]
    fn a_login_page_opens_the_window_once_until_the_internet_answers() {
        let wifi = set(&["wlp2s0"]);
        let mut auto = AutoPortal::default();
        let step = auto.step(1000, false, Connectivity::Blocked, &wifi);
        assert!(step.open && step.recheck);
        let step = auto.step(1002, false, Connectivity::Blocked, &wifi);
        assert!(step.open && !step.recheck);
        // The person logged in: the window closes at once.
        assert!(!auto.step(1030, false, Connectivity::Full, &wifi).open);
        // The same network asks again later (a new day at the hotel): again.
        assert!(auto.step(5000, false, Connectivity::Blocked, &wifi).recheck);
    }

    #[test]
    fn the_window_ends_after_five_minutes_and_does_not_reopen_by_itself() {
        let wifi = set(&["wlp2s0"]);
        let mut auto = AutoPortal::default();
        auto.step(1000, false, Connectivity::Blocked, &wifi);
        assert!(
            auto.step(1000 + AUTO_SECONDS - 1, false, Connectivity::Blocked, &wifi)
                .open
        );
        let late = auto.step(1000 + AUTO_SECONDS, false, Connectivity::Blocked, &wifi);
        assert!(!late.open && !late.recheck);
        assert!(!auto.step(9000, false, Connectivity::Blocked, &wifi).open);
        // Another network (or the same one after reconnecting) may open one.
        let cable = set(&["enp0s3"]);
        assert!(
            auto.step(9002, false, Connectivity::Blocked, &cable)
                .recheck
        );
    }

    #[test]
    fn a_reconnect_to_the_same_network_does_not_open_it_again() {
        let hotel = set(&["wlan0 10.0.2.2"]);
        let none = BTreeSet::new();
        let mut auto = AutoPortal::default();
        assert!(
            auto.step(1000, false, Connectivity::Blocked, &hotel)
                .recheck
        );
        // The Wi-Fi drops and comes back twice during the window.
        assert!(!auto.step(1100, false, Connectivity::Other, &none).recheck);
        assert!(
            !auto
                .step(1110, false, Connectivity::Blocked, &hotel)
                .recheck
        );
        auto.step(1200, false, Connectivity::Other, &none);
        auto.step(1210, false, Connectivity::Blocked, &hotel);
        // Five minutes after it opened it ends, and stays closed.
        assert!(
            !auto
                .step(1000 + AUTO_SECONDS, false, Connectivity::Blocked, &hotel)
                .open
        );
        auto.step(1400, false, Connectivity::Other, &none);
        assert!(!auto.step(1410, false, Connectivity::Blocked, &hotel).open);
        // Another hotel on the same wlan0 has its own window.
        let other = set(&["wlan0 192.168.1.1"]);
        assert!(
            auto.step(1500, false, Connectivity::Blocked, &other)
                .recheck
        );
    }

    #[test]
    fn never_with_the_kill_switch_on() {
        let wifi = set(&["wlp2s0"]);
        let mut auto = AutoPortal::default();
        assert!(!auto.step(1000, true, Connectivity::Blocked, &wifi).open);
        auto.step(1002, false, Connectivity::Blocked, &wifi);
        // The person turned the kill switch on during the window: it closes.
        assert!(!auto.step(1004, true, Connectivity::Blocked, &wifi).open);
    }

    #[test]
    fn nothing_without_a_network_link() {
        let mut auto = AutoPortal::default();
        assert!(
            !auto
                .step(1000, false, Connectivity::Blocked, &BTreeSet::new())
                .open
        );
        assert!(
            !auto
                .step(1000, false, Connectivity::Other, &set(&["wlp2s0"]))
                .open
        );
    }
}
