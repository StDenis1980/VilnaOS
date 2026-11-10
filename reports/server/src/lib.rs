// SPDX-License-Identifier: GPL-3.0-or-later
//! reports.vilnaos.com (ADR 0018 section 6): takes crash reports and
//! feedback from Vilna devices and the site, keeps them in SQLite for 90
//! days, counts repeats per signature and, once we confirmed a crash holds
//! nothing personal, files one `auto-report` issue for it.

pub mod app;
pub mod config;
pub mod limit;
pub mod store;
pub mod tracker;
