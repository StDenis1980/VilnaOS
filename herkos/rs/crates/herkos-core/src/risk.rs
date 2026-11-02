// SPDX-License-Identifier: GPL-3.0-or-later
//! Risk levels R0-R4 (permissions.md, section 4). Mirrors `risk.py`.

use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Level {
    /// Neutral.
    R0,
    /// Reading personal data.
    R1,
    /// Reversible changes.
    R2,
    /// Hard to reverse or system-wide.
    R3,
    /// Irreversible, or acting outwards on the person's behalf.
    R4,
}

impl Level {
    pub const ALL: [Level; 5] = [Level::R0, Level::R1, Level::R2, Level::R3, Level::R4];

    pub fn parse(text: &str) -> Option<Level> {
        let t = text.trim();
        Level::ALL
            .into_iter()
            .find(|l| l.name().eq_ignore_ascii_case(t))
    }

    pub fn name(self) -> &'static str {
        match self {
            Level::R0 => "R0",
            Level::R1 => "R1",
            Level::R2 => "R2",
            Level::R3 => "R3",
            Level::R4 => "R4",
        }
    }

    /// This level raised by one step, capped at R4.
    #[must_use]
    pub fn raised(self) -> Level {
        match self {
            Level::R0 => Level::R1,
            Level::R1 => Level::R2,
            Level::R2 => Level::R3,
            Level::R3 | Level::R4 => Level::R4,
        }
    }
}

impl fmt::Display for Level {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.name())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_and_raise() {
        assert_eq!(Level::parse(" r2 "), Some(Level::R2));
        assert_eq!(Level::parse("R5"), None);
        assert_eq!(Level::R3.raised(), Level::R4);
        assert_eq!(Level::R4.raised(), Level::R4);
        assert!(Level::R1 < Level::R2);
    }
}
