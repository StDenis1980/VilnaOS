// SPDX-License-Identifier: GPL-3.0-or-later
//! New `systemd-coredump` entries from the journal, through `journalctl`.
//!
//! `--cursor-file` remembers where the last read stopped, so each crash is
//! read once, across restarts. The first time (no cursor yet) only the last
//! day is read: crashes from before the person answered are not dug up.

use std::io::{BufRead, BufReader, Read};
use std::path::Path;
use std::process::{Command, Stdio};

use serde_json::{Map, Value};

use crate::coredump::COREDUMP_MESSAGE_ID;

/// Entries read in one go; the rest waits for the next round.
pub const MAX_ENTRIES: usize = 200;
/// A coredump entry with its stack is a few KiB; this bounds a broken one.
const MAX_LINE: u64 = 1024 * 1024;

/// Read journal lines (`journalctl -o json`) into entries, at most `MAX_ENTRIES`.
pub fn entries(output: impl Read) -> Vec<Map<String, Value>> {
    let mut out = Vec::new();
    let mut reader = BufReader::new(output);
    while out.len() < MAX_ENTRIES {
        let mut line = String::new();
        match (&mut reader).take(MAX_LINE).read_line(&mut line) {
            Ok(0) | Err(_) => break,
            Ok(_) => {}
        }
        if let Ok(Value::Object(entry)) = serde_json::from_str(&line) {
            out.push(entry);
        }
    }
    out
}

/// The coredump entries written since the last call.
pub fn new_crashes(cursor_file: &Path) -> Result<Vec<Map<String, Value>>, String> {
    let mut cmd = Command::new("journalctl");
    cmd.args(["--output=json", "--no-pager", "--quiet"])
        .arg(format!("--cursor-file={}", cursor_file.display()))
        .arg(format!("MESSAGE_ID={COREDUMP_MESSAGE_ID}"));
    if !cursor_file.exists() {
        cmd.arg("--since=-1d");
    }
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("journalctl did not start: {e}"))?;
    let found = child.stdout.take().map(entries).unwrap_or_default();
    let _ = child.wait();
    Ok(found)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_lines_become_entries_and_junk_is_skipped() {
        let text = "{\"MESSAGE_ID\":\"x\"}\nnot json\n[1]\n{\"A\":\"b\"}\n";
        let found = entries(text.as_bytes());
        assert_eq!(found.len(), 2);
    }

    #[test]
    fn at_most_max_entries_at_once() {
        let text = "{\"A\":\"b\"}\n".repeat(MAX_ENTRIES + 10);
        assert_eq!(entries(text.as_bytes()).len(), MAX_ENTRIES);
    }
}
