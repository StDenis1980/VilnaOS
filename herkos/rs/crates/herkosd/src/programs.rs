// SPDX-License-Identifier: GPL-3.0-or-later
//! Which catalogue programs the person turned on for Gloxi (ADR 0017,
//! step 6), same rules as `programs.py` in Herkos-lite.
//!
//! A catalogue program is off until the person turns it on, and Herkos asks
//! them first. An off program is not started, not listed in `herkos.tools`
//! and its actions are refused. Built-in tools are always on.

use std::collections::BTreeSet;
use std::fs;
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde_json::{Value, json};

const MAX_PROGRAMS_BYTES: u64 = 256 * 1024;
const MAX_PROGRAMS: usize = 1024;

/// `programs.json` in the state folder: `{"enabled": [tool, ...]}`.
pub struct ProgramStore {
    path: PathBuf,
    enabled: Mutex<BTreeSet<String>>,
    pub load_error: Option<String>,
}

fn load(path: &Path) -> Result<BTreeSet<String>, String> {
    let meta = match fs::metadata(path) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(BTreeSet::new()),
        Err(e) => return Err(e.to_string()),
    };
    if meta.len() > MAX_PROGRAMS_BYTES {
        return Err("too big".into());
    }
    let text = fs::read_to_string(path).map_err(|e| e.to_string())?;
    let data: Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    let Some(list) = data.get("enabled").and_then(Value::as_array) else {
        return Err("'enabled' must be a list".into());
    };
    Ok(list
        .iter()
        .take(MAX_PROGRAMS)
        .filter_map(|t| t.as_str().map(str::to_owned))
        .collect())
}

impl ProgramStore {
    pub fn new(path: &Path) -> ProgramStore {
        // A broken file turns nothing on.
        let (enabled, load_error) = match load(path) {
            Ok(e) => (e, None),
            Err(e) => (BTreeSet::new(), Some(format!("{}: {e}", path.display()))),
        };
        ProgramStore {
            path: path.to_path_buf(),
            enabled: Mutex::new(enabled),
            load_error,
        }
    }

    pub fn enabled(&self, tool: &str) -> bool {
        self.enabled
            .lock()
            .map(|e| e.contains(tool))
            .unwrap_or(false)
    }

    pub fn set(&self, tool: &str, on: bool) -> Result<(), String> {
        let mut enabled = self.enabled.lock().map_err(|_| "programs lock poisoned")?;
        if on {
            if enabled.len() >= MAX_PROGRAMS {
                return Err("too many programs turned on".into());
            }
            enabled.insert(tool.to_owned());
        } else {
            enabled.remove(tool);
        }
        self.save(&enabled)
    }

    fn save(&self, enabled: &BTreeSet<String>) -> Result<(), String> {
        let shown = self.path.display();
        if let Some(dir) = self.path.parent() {
            fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(dir)
                .map_err(|e| format!("{shown}: {e}"))?;
        }
        let text = serde_json::to_string_pretty(&json!({"enabled": enabled}))
            .map_err(|e| e.to_string())?;
        let tmp = self.path.with_extension("json.tmp");
        let mut f = fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&tmp)
            .map_err(|e| format!("{}: {e}", tmp.display()))?;
        f.write_all(text.as_bytes())
            .and_then(|()| f.sync_all())
            .map_err(|e| format!("{}: {e}", tmp.display()))?;
        fs::rename(&tmp, &self.path).map_err(|e| format!("{shown}: {e}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dir(name: &str) -> PathBuf {
        let d =
            std::env::temp_dir().join(format!("herkosd-programs-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).expect("tmp");
        d
    }

    #[test]
    fn off_by_default_and_remembered() {
        let path = dir("remember").join("programs.json");
        let store = ProgramStore::new(&path);
        assert!(!store.enabled("org.example.notes"));
        store.set("org.example.notes", true).expect("save");
        assert!(ProgramStore::new(&path).enabled("org.example.notes"));
        store.set("org.example.notes", false).expect("save");
        assert!(!ProgramStore::new(&path).enabled("org.example.notes"));
    }

    #[test]
    fn a_broken_file_turns_nothing_on() {
        let d = dir("broken");
        for text in ["{", "[]", r#"{"enabled": "org.example.notes"}"#] {
            let path = d.join("programs.json");
            fs::write(&path, text).expect("write");
            let store = ProgramStore::new(&path);
            assert!(store.load_error.is_some(), "{text}");
            assert!(!store.enabled("org.example.notes"));
        }
    }
}
