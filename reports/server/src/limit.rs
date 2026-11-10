// SPDX-License-Identifier: GPL-3.0-or-later
//! Request limits kept in memory only (ADR 0018 section 6: no IP address is
//! stored). A client is a salted hash of its address; the salt and all
//! counters are replaced every window, so nothing outlives an hour.

use std::collections::HashMap;
use std::sync::Mutex;

use sha2::{Digest, Sha256};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Bucket {
    Crash,
    Feedback,
    Other,
}

#[derive(Debug, Clone, Copy)]
pub struct Limits {
    pub window_secs: i64,
    pub crash_per_client: u32,
    pub feedback_per_client: u32,
    pub other_per_client: u32,
    /// For all clients together: a flood from many addresses still stops.
    pub crash_total: u32,
    pub feedback_total: u32,
}

impl Default for Limits {
    fn default() -> Self {
        // A device sends at most 20 reports a day (ADR 0018 section 4).
        Limits {
            window_secs: 3600,
            crash_per_client: 30,
            feedback_per_client: 10,
            other_per_client: 120,
            crash_total: 5000,
            feedback_total: 500,
        }
    }
}

struct Window {
    start: i64,
    salt: [u8; 16],
    clients: HashMap<([u8; 16], Bucket), u32>,
    totals: HashMap<Bucket, u32>,
}

pub struct Limiter {
    limits: Limits,
    window: Mutex<Window>,
}

impl Limiter {
    pub fn new(limits: Limits) -> Limiter {
        Limiter {
            limits,
            window: Mutex::new(Window {
                start: i64::MIN,
                salt: [0; 16],
                clients: HashMap::new(),
                totals: HashMap::new(),
            }),
        }
    }

    /// `Ok` when the request may go on, else the seconds until the window ends.
    pub fn check(&self, client: &str, bucket: Bucket, now: i64) -> Result<(), i64> {
        let mut w = self.window.lock().unwrap_or_else(|p| p.into_inner());
        if now.saturating_sub(w.start) >= self.limits.window_secs || now < w.start {
            w.start = now;
            // Without randomness the salt stays zero: limits still work, and
            // the hashes still leave memory never.
            let _ = getrandom::fill(&mut w.salt);
            w.clients.clear();
            w.totals.clear();
        }
        let retry = w
            .start
            .saturating_add(self.limits.window_secs)
            .saturating_sub(now)
            .max(1);
        let (per_client, total) = match bucket {
            Bucket::Crash => (self.limits.crash_per_client, Some(self.limits.crash_total)),
            Bucket::Feedback => (
                self.limits.feedback_per_client,
                Some(self.limits.feedback_total),
            ),
            Bucket::Other => (self.limits.other_per_client, None),
        };
        let used_total = w.totals.get(&bucket).copied().unwrap_or(0);
        if total.is_some_and(|t| used_total >= t) {
            return Err(retry);
        }
        let mut hasher = Sha256::new();
        hasher.update(w.salt);
        hasher.update(client.as_bytes());
        let mut key = [0u8; 16];
        key.copy_from_slice(&hasher.finalize()[..16]);
        let used = w.clients.entry((key, bucket)).or_insert(0);
        if *used >= per_client {
            return Err(retry);
        }
        *used += 1;
        *w.totals.entry(bucket).or_insert(0) += 1;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_client_is_stopped_and_the_window_resets() {
        let l = Limiter::new(Limits {
            feedback_per_client: 2,
            ..Limits::default()
        });
        assert!(l.check("a", Bucket::Feedback, 100).is_ok());
        assert!(l.check("a", Bucket::Feedback, 101).is_ok());
        assert_eq!(l.check("a", Bucket::Feedback, 102), Err(3598));
        assert!(l.check("b", Bucket::Feedback, 102).is_ok());
        assert!(l.check("a", Bucket::Crash, 102).is_ok());
        assert!(l.check("a", Bucket::Feedback, 3700).is_ok());
    }

    #[test]
    fn the_total_stops_a_flood_from_many_addresses() {
        let l = Limiter::new(Limits {
            crash_total: 3,
            ..Limits::default()
        });
        for i in 0..3 {
            assert!(l.check(&i.to_string(), Bucket::Crash, 0).is_ok());
        }
        assert!(l.check("new", Bucket::Crash, 0).is_err());
    }
}
