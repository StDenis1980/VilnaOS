// SPDX-License-Identifier: GPL-3.0-or-later
//! What a tool said about each operation when it was first trusted
//! (ADR 0017, step 3), same rules as `pins.py` in Herkos-lite.
//!
//! The first time Herkos runs a tool from the catalogue it keeps the
//! fingerprint of each operation's `tools/list` entry. After an update a new
//! or changed operation counts as one without a manifest (permissions.md
//! 4.1.3) until the person allows it once; then the new fingerprint is kept.

use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::mitos::canonical;

const MAX_PINS_BYTES: u64 = 4 * 1024 * 1024;

type Pins = BTreeMap<String, BTreeMap<String, String>>;

/// sha256 of the entry's name, description, inputSchema and annotations.
pub fn fingerprint(entry: &Value) -> String {
    let mut keep = Map::new();
    for key in ["name", "description", "inputSchema", "annotations"] {
        if let Some(v) = entry.get(key) {
            keep.insert(key.to_owned(), v.clone());
        }
    }
    let hash = Sha256::digest(canonical(&Value::Object(keep)));
    let hex: String = hash.iter().map(|b| format!("{b:02x}")).collect();
    format!("sha256:{hex}")
}

/// `tool-pins.json` in the state folder: `{tool: {operation: fingerprint}}`.
pub struct PinStore {
    path: PathBuf,
    pins: Mutex<Pins>,
    /// The file was there but unreadable: nothing is trusted.
    broken: bool,
    pub load_error: Option<String>,
}

fn load(path: &Path) -> Result<Pins, String> {
    let meta = match fs::metadata(path) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Pins::new()),
        Err(e) => return Err(e.to_string()),
    };
    if meta.len() > MAX_PINS_BYTES {
        return Err("too big".into());
    }
    let text = fs::read_to_string(path).map_err(|e| e.to_string())?;
    let Value::Object(data) = serde_json::from_str(&text).map_err(|e| e.to_string())? else {
        return Err("not an object".into());
    };
    let mut pins = Pins::new();
    for (tool, ops) in data {
        if let Value::Object(ops) = ops {
            let ops = ops
                .into_iter()
                .filter_map(|(o, f)| f.as_str().map(|f| (o, f.to_owned())))
                .collect();
            pins.insert(tool, ops);
        }
    }
    Ok(pins)
}

impl PinStore {
    pub fn new(path: &Path) -> PinStore {
        let (pins, broken, load_error) = match load(path) {
            Ok(p) => (p, false, None),
            Err(e) => (Pins::new(), true, Some(format!("{}: {e}", path.display()))),
        };
        PinStore {
            path: path.to_path_buf(),
            pins: Mutex::new(pins),
            broken,
            load_error,
        }
    }

    /// Whether this operation differs from what was trusted. A tool seen for
    /// the first time is trusted as it is now, unless the file was unreadable.
    pub fn changed(
        &self,
        tool: &str,
        operation: &str,
        current: &BTreeMap<String, String>,
    ) -> Result<bool, String> {
        let mut pins = self.pins.lock().map_err(|_| "pins lock poisoned")?;
        let Some(pinned) = pins.get(tool) else {
            if self.broken {
                return Ok(true);
            }
            pins.insert(tool.to_owned(), current.clone());
            self.save(&pins)?;
            return Ok(false);
        };
        Ok(current.get(operation) != pinned.get(operation) || !current.contains_key(operation))
    }

    pub fn pin(&self, tool: &str, operation: &str, fp: &str) -> Result<(), String> {
        let mut pins = self.pins.lock().map_err(|_| "pins lock poisoned")?;
        pins.entry(tool.to_owned())
            .or_default()
            .insert(operation.to_owned(), fp.to_owned());
        self.save(&pins)
    }

    fn save(&self, pins: &Pins) -> Result<(), String> {
        let shown = self.path.display();
        if let Some(dir) = self.path.parent() {
            fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(dir)
                .map_err(|e| format!("{shown}: {e}"))?;
        }
        let text = serde_json::to_string_pretty(pins).map_err(|e| e.to_string())?;
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
    use serde_json::json;

    fn temp(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("herkosd-pins-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        dir.join("tool-pins.json")
    }

    #[test]
    fn fingerprints_cover_what_the_server_says_and_nothing_else() {
        let a = json!({"name": "read", "description": "Read", "inputSchema": {"type": "object"}});
        let mut b = a.clone();
        b["title"] = json!("x");
        assert_eq!(fingerprint(&a), fingerprint(&b));
        b["description"] = json!("Read, then send it away");
        assert_ne!(fingerprint(&a), fingerprint(&b));
        // The same canonical JSON as Herkos-lite (json.dumps, sorted, no spaces).
        assert_eq!(
            fingerprint(&json!({"name": "read"})),
            format!("sha256:{:x}", Sha256::digest(br#"{"name":"read"}"#))
        );
    }

    #[test]
    fn first_trusted_then_changes_show_until_pinned() {
        let path = temp("flow");
        let pins = PinStore::new(&path);
        let mut now = BTreeMap::from([("read".to_owned(), "sha256:a".to_owned())]);
        assert_eq!(pins.changed("t", "read", &now), Ok(false));
        now.insert("read".into(), "sha256:b".into());
        now.insert("share".into(), "sha256:c".into());
        let again = PinStore::new(&path);
        assert_eq!(again.changed("t", "read", &now), Ok(true));
        assert_eq!(again.changed("t", "share", &now), Ok(true));
        again.pin("t", "read", "sha256:b").expect("pin");
        assert_eq!(PinStore::new(&path).changed("t", "read", &now), Ok(false));
    }

    #[test]
    fn an_unreadable_file_trusts_nothing() {
        let path = temp("broken");
        fs::create_dir_all(path.parent().expect("dir")).expect("dir");
        fs::write(&path, "{broken").expect("write");
        let pins = PinStore::new(&path);
        assert!(pins.load_error.is_some());
        let now = BTreeMap::from([("read".to_owned(), "sha256:a".to_owned())]);
        assert_eq!(pins.changed("t", "read", &now), Ok(true));
    }
}
