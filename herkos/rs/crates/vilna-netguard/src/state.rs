// SPDX-License-Identifier: GPL-3.0-or-later
//! What the person decided about the kill switch, kept by root in
//! `/var/lib/vilna-netguard/state.json`.
//!
//! Herkos changes it only after the person confirmed (ADR 0027, section 4):
//! turning the kill switch on is R2, turning it off, letting the local
//! network through and opening a captive-portal window are R3.

use std::fmt;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use serde_json::{Value, json};

/// A captive-portal window is short: the person signs in, the kill switch
/// comes back by itself.
pub const MAX_PORTAL_SECONDS: u64 = 300;
/// Local networks the person let through; more is a mistake, not a home.
pub const MAX_LAN: usize = 16;

/// A local network the person let through the kill switch (a printer, a NAS).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Lan {
    pub addr: IpAddr,
    pub prefix: u8,
}

impl Lan {
    /// `192.168.1.0/24`, `fd00::/64`. Only private ranges: "let the local
    /// network through" must never become "let the internet through".
    pub fn parse(text: &str) -> Result<Lan, String> {
        let (addr, prefix) = text
            .trim()
            .split_once('/')
            .ok_or_else(|| format!("{text:?} is not a network like 192.168.1.0/24"))?;
        let addr: IpAddr = addr
            .parse()
            .map_err(|_| format!("{addr:?} is not an IP address"))?;
        let prefix: u8 = prefix
            .parse()
            .map_err(|_| format!("{prefix:?} is not a prefix length"))?;
        let (max, min) = match addr {
            IpAddr::V4(_) => (32, 8),
            IpAddr::V6(_) => (128, 7),
        };
        if prefix > max || prefix < min {
            return Err(format!("/{prefix} is not a local network size"));
        }
        let lan = Lan {
            addr: network(addr, prefix),
            prefix,
        };
        if !lan.is_private() {
            return Err(format!(
                "{lan} is not a local network: only 10/8, 172.16/12, 192.168/16, 169.254/16, fc00::/7 and fe80::/10 may be let through"
            ));
        }
        Ok(lan)
    }

    fn is_private(&self) -> bool {
        let inside = |base: IpAddr, len: u8| {
            self.prefix >= len && network(self.addr, len) == network(base, len)
        };
        match self.addr {
            IpAddr::V4(_) => [
                (Ipv4Addr::new(10, 0, 0, 0), 8),
                (Ipv4Addr::new(172, 16, 0, 0), 12),
                (Ipv4Addr::new(192, 168, 0, 0), 16),
                (Ipv4Addr::new(169, 254, 0, 0), 16),
            ]
            .iter()
            .any(|(b, l)| inside(IpAddr::V4(*b), *l)),
            IpAddr::V6(_) => [
                (Ipv6Addr::new(0xfc00, 0, 0, 0, 0, 0, 0, 0), 7),
                (Ipv6Addr::new(0xfe80, 0, 0, 0, 0, 0, 0, 0), 10),
            ]
            .iter()
            .any(|(b, l)| inside(IpAddr::V6(*b), *l)),
        }
    }
}

impl fmt::Display for Lan {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}/{}", self.addr, self.prefix)
    }
}

/// The address with the host bits cleared.
fn network(addr: IpAddr, prefix: u8) -> IpAddr {
    match addr {
        IpAddr::V4(a) => {
            let mask = u32::MAX
                .checked_shl(32 - u32::from(prefix.min(32)))
                .unwrap_or(0);
            IpAddr::V4(Ipv4Addr::from(u32::from(a) & mask))
        }
        IpAddr::V6(a) => {
            let mask = u128::MAX
                .checked_shl(128 - u32::from(prefix.min(128)))
                .unwrap_or(0);
            IpAddr::V6(Ipv6Addr::from(u128::from(a) & mask))
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct State {
    pub enabled: bool,
    pub lan: Vec<Lan>,
    /// Unix time until which the kill switch is open for a captive portal; 0 = no window.
    pub portal_until: u64,
}

impl State {
    /// A missing or broken file means "off": the computer must still reach
    /// the network after a bad write, and Herkos shows the state to the person.
    pub fn from_json(text: &str) -> State {
        let Ok(v) = serde_json::from_str::<Value>(text) else {
            return State::default();
        };
        let mut lan: Vec<Lan> = v
            .get("lan")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .filter_map(|s| Lan::parse(s).ok())
                    .take(MAX_LAN)
                    .collect()
            })
            .unwrap_or_default();
        lan.sort();
        lan.dedup();
        State {
            enabled: v.get("enabled") == Some(&Value::Bool(true)),
            lan,
            portal_until: v.get("portal_until").and_then(Value::as_u64).unwrap_or(0),
        }
    }

    pub fn to_json(&self) -> Value {
        json!({
            "enabled": self.enabled,
            "lan": self.lan.iter().map(ToString::to_string).collect::<Vec<_>>(),
            "portal_until": self.portal_until,
        })
    }

    pub fn portal_open(&self, now: u64) -> bool {
        self.portal_until > now
    }

    pub fn allow_lan(&mut self, lan: Lan) -> Result<(), String> {
        if !self.lan.contains(&lan) {
            if self.lan.len() >= MAX_LAN {
                return Err(format!(
                    "{MAX_LAN} local networks are already let through; forget one first"
                ));
            }
            self.lan.push(lan);
            self.lan.sort();
        }
        Ok(())
    }

    pub fn forget_lan(&mut self, lan: Lan) {
        self.lan.retain(|l| *l != lan);
    }

    pub fn open_portal(&mut self, now: u64, seconds: u64) -> Result<(), String> {
        if seconds == 0 || seconds > MAX_PORTAL_SECONDS {
            return Err(format!(
                "a captive-portal window is 1 to {MAX_PORTAL_SECONDS} seconds"
            ));
        }
        self.portal_until = now.saturating_add(seconds);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_networks_only() {
        assert_eq!(
            Lan::parse("192.168.1.7/24").unwrap().to_string(),
            "192.168.1.0/24"
        );
        assert_eq!(
            Lan::parse("fd12:3456::/64").unwrap().to_string(),
            "fd12:3456::/64"
        );
        assert!(Lan::parse("10.0.0.0/8").is_ok());
        for bad in [
            "0.0.0.0/0",
            "8.8.8.0/24",
            "172.32.0.0/16",
            "192.168.0.0/8",
            "::/0",
            "2001:db8::/32",
            "192.168.1.0/33",
            "192.168.1.0",
            "192.168.1.0/-1",
            "x/24",
            "",
        ] {
            assert!(Lan::parse(bad).is_err(), "{bad} must be refused");
        }
    }

    #[test]
    fn a_broken_file_means_off() {
        assert_eq!(State::from_json("{not json"), State::default());
        assert_eq!(State::from_json("[]"), State::default());
        let s = State::from_json(
            r#"{"enabled": "yes", "lan": ["8.8.8.0/24", "192.168.1.0/24", 5], "portal_until": -3}"#,
        );
        assert!(!s.enabled);
        assert_eq!(s.lan, vec![Lan::parse("192.168.1.0/24").unwrap()]);
        assert_eq!(s.portal_until, 0);
    }

    #[test]
    fn round_trip() {
        let mut s = State {
            enabled: true,
            ..State::default()
        };
        s.allow_lan(Lan::parse("192.168.1.0/24").unwrap()).unwrap();
        s.allow_lan(Lan::parse("192.168.1.0/24").unwrap()).unwrap();
        s.open_portal(1000, 300).unwrap();
        assert_eq!(State::from_json(&s.to_json().to_string()), s);
        assert_eq!(s.lan.len(), 1);
        assert!(s.portal_open(1299));
        assert!(!s.portal_open(1300));
    }

    #[test]
    fn portal_windows_are_short() {
        let mut s = State::default();
        assert!(s.open_portal(0, 0).is_err());
        assert!(s.open_portal(0, 301).is_err());
        assert!(s.open_portal(u64::MAX, 300).is_ok());
        assert_eq!(s.portal_until, u64::MAX);
    }

    #[test]
    fn the_lan_list_is_bounded() {
        let mut s = State::default();
        for i in 0..MAX_LAN {
            s.allow_lan(Lan::parse(&format!("10.{i}.0.0/16")).unwrap())
                .unwrap();
        }
        assert!(s.allow_lan(Lan::parse("10.200.0.0/16").unwrap()).is_err());
    }
}
