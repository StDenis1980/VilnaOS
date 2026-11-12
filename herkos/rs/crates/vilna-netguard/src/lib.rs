// SPDX-License-Identifier: GPL-3.0-or-later
//! The VPN kill switch of Vilna OS (docs/adr/0027-vpn-proton-mullvad.md,
//! section 3).
//!
//! The kill switch belongs to Vilna, not to the VPN provider: one nftables
//! table, `inet vilna_killswitch`, lets traffic leave the computer only
//! through a tunnel, to the servers the tunnels talk to, and for the few
//! things a computer needs to get an address. It works the same with Proton
//! VPN, Mullvad and a WireGuard `.conf` the person imported.
//!
//! This crate has no I/O apart from `main.rs`: [`state`] is what the person
//! decided (through Herkos), [`endpoints`] reads the servers of the tunnels
//! that are up, and [`ruleset`] turns both into one nftables transaction.
#![deny(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::unimplemented,
    clippy::todo,
    clippy::unreachable
)]
// Tests may unwrap: a failed unwrap is a failed test.
#![cfg_attr(test, allow(clippy::unwrap_used, clippy::expect_used))]

pub mod endpoints;
pub mod ruleset;
pub mod state;
