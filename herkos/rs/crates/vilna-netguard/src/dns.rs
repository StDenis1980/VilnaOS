// SPDX-License-Identifier: GPL-3.0-or-later
//! Which encrypted DNS resolver the computer uses (ADR 0027, section 5).
//!
//! Quad9 is the default (decision of Denys, 2026-11-12) and comes with the
//! system in `/usr/lib/systemd/resolved.conf.d/50-vilna-dns.conf`
//! (NetworkManager-branding-Vilna). Choosing Mullvad's resolver writes
//! `/etc/systemd/resolved.conf.d/60-vilna-dns.conf`, which systemd-resolved
//! reads after the default and which wins; choosing Quad9 again removes it.
//! When a VPN is up, the VPN's own DNS inside the tunnel is used anyway.

/// The file this tool owns; nothing else in the folder is touched.
pub const OVERRIDE_FILE: &str = "60-vilna-dns.conf";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Resolver {
    /// Quad9 Foundation (Zurich): blocks malicious domains.
    Quad9,
    /// Mullvad's `base` resolver (Sweden): also blocks ads and trackers.
    Mullvad,
}

impl Resolver {
    pub fn parse(name: &str) -> Result<Resolver, String> {
        match name {
            "quad9" => Ok(Resolver::Quad9),
            "mullvad" => Ok(Resolver::Mullvad),
            _ => Err(format!("{name:?} is not a resolver: quad9 or mullvad")),
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Resolver::Quad9 => "quad9",
            Resolver::Mullvad => "mullvad",
        }
    }

    /// The contents of the override file, or `None` for the default (no file).
    pub fn override_file(self) -> Option<String> {
        match self {
            Resolver::Quad9 => None,
            Resolver::Mullvad => Some(
                "# Written by vilna-netguard (ADR 0027): the person chose Mullvad's\n\
                 # resolver, which also blocks ads and trackers. Choose Quad9 again\n\
                 # through Herkos to remove this file.\n\
                 [Resolve]\n\
                 DNS=194.242.2.4#base.dns.mullvad.net 2a07:e340::4#base.dns.mullvad.net\n\
                 Domains=~.\n\
                 DNSOverTLS=yes\n\
                 FallbackDNS=\n"
                    .to_owned(),
            ),
        }
    }

    /// Which resolver an existing override file chose; no file is Quad9.
    pub fn from_override(text: Option<&str>) -> Resolver {
        match text {
            Some(t) if t.contains("dns.mullvad.net") => Resolver::Mullvad,
            _ => Resolver::Quad9,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_round_trip() {
        for r in [Resolver::Quad9, Resolver::Mullvad] {
            assert_eq!(Resolver::parse(r.name()).unwrap(), r);
            assert_eq!(Resolver::from_override(r.override_file().as_deref()), r);
        }
        assert!(Resolver::parse("8.8.8.8").is_err());
        assert!(Resolver::parse("").is_err());
    }

    #[test]
    fn mullvad_is_encrypted_and_for_every_name() {
        let text = Resolver::Mullvad.override_file().unwrap();
        assert!(text.contains("DNSOverTLS=yes"));
        assert!(text.contains("Domains=~."));
        assert!(text.contains("FallbackDNS=\n"));
    }
}
