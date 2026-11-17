// SPDX-License-Identifier: GPL-3.0-or-later
//! `vilna-reportd`, the crash report collector on the device (ADR 0018
//! section 4). It reads crashes from `systemd-coredump`, keeps each report
//! on the device where the person can read it, and sends it to
//! reports.vilnaos.com only as the person answered in the installer: by
//! itself ("auto"), when they press "Send" ("ask", and "never" for one they
//! choose), never before an answer. The core dump never leaves the device.
#![deny(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::unimplemented,
    clippy::todo,
    clippy::unreachable
)]

pub mod collector;
pub mod consent;
pub mod control;
pub mod coredump;
pub mod device;
pub mod journal;
pub mod send;
pub mod spool;
