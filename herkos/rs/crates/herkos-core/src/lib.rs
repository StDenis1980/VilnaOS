// SPDX-License-Identifier: GPL-3.0-or-later
//! Herkos decisions without I/O: what Gloxi may ask for and how it is judged.
//!
//! The contracts live in `herkos/docs/` (protocol, action request, manifests);
//! Herkos-lite in Python is their reference implementation and the
//! conformance tests in `herkos/conformance/` check both.

pub mod keros;
pub mod manifest;
pub mod policy;
pub mod request;
pub mod risk;

/// Version of the Gloxi <-> Herkos protocol (herkos/docs/protocol.md).
pub const PROTOCOL_VERSION: &str = "0.1";
