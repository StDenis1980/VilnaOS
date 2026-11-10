// SPDX-License-Identifier: GPL-3.0-or-later
//! Permissions the person gave (permissions.md 5.3). Same file and rules as
//! Herkos-lite (`grants.py`), so `herkos permissions list|revoke` works for
//! both.
//!
//! A permission covers one class of actions (tool + operation) within the
//! allowed folders it was given for, for a duration: this session or
//! permanently (R1-R2 only). Gloxi has no protocol method to create or revoke
//! them.

use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use herkos_core::risk::Level;
use serde_json::{Map, Value, json};

use crate::config::SESSION_IDLE_SECONDS;
use crate::rpc::random_hex;

pub const PERMANENT_MAX_LEVEL: Level = Level::R2;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Duration {
    Session,
    Permanent,
}

impl Duration {
    pub fn name(self) -> &'static str {
        match self {
            Duration::Session => "session",
            Duration::Permanent => "permanent",
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Grant {
    pub id: String,
    pub tool: String,
    pub operation: String,
    /// Allowed folders the permission is limited to.
    pub roots: Vec<String>,
    /// Level the person saw when giving it.
    pub level: Level,
    pub duration: Duration,
    pub created: f64,
}

impl Grant {
    pub fn covers(
        &self,
        tool: &str,
        operation: &str,
        roots: &[Option<PathBuf>],
        level: Level,
    ) -> bool {
        if tool != self.tool || operation != self.operation {
            return false;
        }
        if level > self.level {
            return false; // the action got riskier than what the person allowed
        }
        roots.iter().all(|r| {
            r.as_ref()
                .is_some_and(|r| self.roots.iter().any(|g| Path::new(g) == r.as_path()))
        })
    }

    fn to_json(&self) -> Value {
        json!({
            "id": self.id,
            "tool": self.tool,
            "operation": self.operation,
            "roots": self.roots,
            "level": self.level.name(),
            "duration": self.duration.name(),
            "created": self.created,
        })
    }

    fn from_json(item: &Value) -> Option<Grant> {
        let obj = item.as_object()?;
        const KEYS: [&str; 7] = [
            "id",
            "tool",
            "operation",
            "roots",
            "level",
            "duration",
            "created",
        ];
        if obj.len() != KEYS.len() || !KEYS.iter().all(|k| obj.contains_key(*k)) {
            return None;
        }
        let s = |k: &str| obj.get(k).and_then(Value::as_str).map(str::to_owned);
        let roots = obj
            .get("roots")?
            .as_array()?
            .iter()
            .map(|r| r.as_str().map(str::to_owned))
            .collect::<Option<Vec<_>>>()?;
        let duration = match obj.get("duration")?.as_str()? {
            "session" => Duration::Session,
            "permanent" => Duration::Permanent,
            _ => return None,
        };
        Some(Grant {
            id: s("id")?,
            tool: s("tool")?,
            operation: s("operation")?,
            roots,
            level: Level::parse(obj.get("level")?.as_str()?)?,
            duration,
            created: obj.get("created")?.as_f64()?,
        })
    }
}

pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}

pub struct GrantStore {
    path: PathBuf,
    clock: fn() -> f64,
    /// id -> (grant, last use)
    session: BTreeMap<String, (Grant, f64)>,
    permanent: BTreeMap<String, Grant>,
    stamp: Option<(i64, i64, u64)>,
    /// The file could not be read: no permanent permissions, not all of them.
    pub load_error: Option<String>,
}

impl GrantStore {
    pub fn new(path: &Path, clock: fn() -> f64) -> GrantStore {
        let mut g = GrantStore {
            path: path.to_path_buf(),
            clock,
            session: BTreeMap::new(),
            permanent: BTreeMap::new(),
            stamp: None,
            load_error: None,
        };
        g.refresh();
        g
    }

    fn file_stamp(&self) -> Option<(i64, i64, u64)> {
        fs::metadata(&self.path)
            .ok()
            .map(|m| (m.mtime(), m.mtime_nsec(), m.size()))
    }

    /// Re-read the file if it changed, e.g. after `herkos permissions revoke`.
    fn refresh(&mut self) {
        let stamp = self.file_stamp();
        if stamp != self.stamp || stamp.is_none() {
            self.load_error = None;
            self.permanent = self.load();
            self.stamp = stamp;
        }
    }

    fn load(&mut self) -> BTreeMap<String, Grant> {
        let text = match fs::read_to_string(&self.path) {
            Ok(t) => t,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return BTreeMap::new(),
            Err(e) => {
                self.load_error = Some(format!("{}: {e}", self.path.display()));
                return BTreeMap::new();
            }
        };
        let parsed = serde_json::from_str::<Value>(&text).ok().and_then(|raw| {
            let items = match raw.get("permissions") {
                None => Vec::new(),
                Some(p) => p.as_array()?.clone(),
            };
            items
                .iter()
                .map(Grant::from_json)
                .collect::<Option<Vec<_>>>()
        });
        match parsed {
            // Fail closed: an unreadable file means no permissions, not all.
            None => {
                self.load_error = Some(format!(
                    "{}: not a valid permissions file",
                    self.path.display()
                ));
                BTreeMap::new()
            }
            Some(grants) => grants
                .into_iter()
                .filter(|g| g.duration == Duration::Permanent && g.level <= PERMANENT_MAX_LEVEL)
                .map(|g| (g.id.clone(), g))
                .collect(),
        }
    }

    fn save(&mut self) -> std::io::Result<()> {
        let folder = self.path.parent().unwrap_or(Path::new("/"));
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(folder)?;
        let data = json!({
            "version": 1,
            "permissions": self.permanent.values().map(Grant::to_json).collect::<Vec<_>>(),
        });
        let tmp = folder.join(format!(".permissions-{}", random_hex(6)));
        let result = (|| {
            let mut f = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&tmp)?;
            f.write_all(
                serde_json::to_string_pretty(&data)
                    .unwrap_or_default()
                    .as_bytes(),
            )?;
            f.sync_all()?;
            fs::rename(&tmp, &self.path)
        })();
        if result.is_err() {
            let _ = fs::remove_file(&tmp);
        }
        result?;
        self.stamp = self.file_stamp();
        Ok(())
    }

    pub fn find(
        &mut self,
        tool: &str,
        operation: &str,
        roots: &[Option<PathBuf>],
        level: Level,
    ) -> Option<Grant> {
        let now = (self.clock)();
        self.refresh();
        self.session
            .retain(|_, (_, last)| now - *last <= SESSION_IDLE_SECONDS);
        for (g, last) in self.session.values_mut() {
            if g.covers(tool, operation, roots, level) {
                *last = now;
                return Some(g.clone());
            }
        }
        self.permanent
            .values()
            .find(|g| g.covers(tool, operation, roots, level))
            .cloned()
    }

    pub fn add(
        &mut self,
        tool: &str,
        operation: &str,
        roots: &[PathBuf],
        level: Level,
        duration: Duration,
    ) -> Result<Grant, String> {
        if duration == Duration::Permanent && level > PERMANENT_MAX_LEVEL {
            return Err(format!(
                "a permanent permission is not possible for {level}"
            ));
        }
        let mut roots: Vec<String> = roots.iter().map(|r| r.display().to_string()).collect();
        roots.sort();
        roots.dedup();
        let g = Grant {
            id: random_hex(6),
            tool: tool.to_owned(),
            operation: operation.to_owned(),
            roots,
            level,
            duration,
            created: (self.clock)(),
        };
        match duration {
            Duration::Session => {
                self.session
                    .insert(g.id.clone(), (g.clone(), (self.clock)()));
            }
            Duration::Permanent => {
                self.refresh();
                self.permanent.insert(g.id.clone(), g.clone());
                if let Err(e) = self.save() {
                    self.permanent.remove(&g.id);
                    return Err(format!("{}: {e}", self.path.display()));
                }
            }
        }
        Ok(g)
    }

    pub fn revoke(&mut self, id: &str) -> bool {
        if self.session.remove(id).is_some() {
            return true;
        }
        self.refresh();
        if self.permanent.remove(id).is_some() {
            return self.save().is_ok();
        }
        false
    }

    pub fn all(&mut self) -> Vec<Grant> {
        self.refresh();
        self.session
            .values()
            .map(|(g, _)| g.clone())
            .chain(self.permanent.values().cloned())
            .collect()
    }
}

/// For the journal's `grant` record.
pub fn grant_record(g: &Grant) -> Value {
    let mut m = Map::new();
    m.insert("grant_id".into(), json!(g.id));
    m.insert("tool".into(), json!(g.tool));
    m.insert("operation".into(), json!(g.operation));
    m.insert("roots".into(), json!(g.roots));
    m.insert("level".into(), json!(g.level.name()));
    m.insert("duration".into(), json!(g.duration.name()));
    Value::Object(m)
}

/// For the person's own window (`herkos.permissions`): the journal fields
/// plus when it was given.
pub fn grant_listing(g: &Grant) -> Value {
    let mut v = grant_record(g);
    if let Some(m) = v.as_object_mut() {
        m.insert("created".into(), json!(g.created));
    }
    v
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("herkosd-grants-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        dir.join("permissions.json")
    }

    fn root(p: &str) -> Vec<Option<PathBuf>> {
        vec![Some(PathBuf::from(p))]
    }

    #[test]
    fn covers_class_roots_and_level() {
        let mut s = GrantStore::new(&temp("covers"), now);
        let g = s
            .add(
                "t",
                "write",
                &[PathBuf::from("/d")],
                Level::R2,
                Duration::Session,
            )
            .expect("add");
        assert!(g.covers("t", "write", &root("/d"), Level::R2));
        assert!(g.covers("t", "write", &root("/d"), Level::R1));
        assert!(!g.covers("t", "write", &root("/d"), Level::R3));
        assert!(!g.covers("t", "read", &root("/d"), Level::R1));
        assert!(!g.covers("t", "write", &root("/e"), Level::R2));
        assert!(!g.covers("t", "write", &[None], Level::R2));
        assert!(s.find("t", "write", &root("/d"), Level::R2).is_some());
        assert!(s.revoke(&g.id) && s.find("t", "write", &root("/d"), Level::R2).is_none());
    }

    #[test]
    fn permanent_survives_and_is_capped() {
        let path = temp("permanent");
        let mut s = GrantStore::new(&path, now);
        assert!(
            s.add("t", "x", &[], Level::R3, Duration::Permanent)
                .is_err()
        );
        let g = s
            .add(
                "t",
                "x",
                &[PathBuf::from("/d")],
                Level::R1,
                Duration::Permanent,
            )
            .expect("add");
        let mut again = GrantStore::new(&path, now);
        assert_eq!(again.all(), vec![g.clone()]);
        assert!(again.revoke(&g.id));
        assert!(
            s.find("t", "x", &root("/d"), Level::R1).is_none(),
            "re-read after revoke"
        );
    }

    #[test]
    fn broken_file_means_none() {
        let path = temp("broken");
        fs::create_dir_all(path.parent().expect("parent")).expect("mkdir");
        fs::write(&path, "{\"permissions\": [{\"id\": 1}]}").expect("write");
        let mut s = GrantStore::new(&path, now);
        assert!(s.all().is_empty() && s.load_error.is_some());
    }

    #[test]
    fn reads_herkos_lite_file() {
        let path = temp("lite");
        fs::create_dir_all(path.parent().expect("parent")).expect("mkdir");
        fs::write(
            &path,
            r#"{"version": 1, "permissions": [{"id": "ab12", "tool": "t", "operation": "x",
               "roots": ["/d"], "level": "R2", "duration": "permanent", "created": 1.5},
              {"id": "cd34", "tool": "t", "operation": "y", "roots": [], "level": "R3",
               "duration": "permanent", "created": 2}]}"#,
        )
        .expect("write");
        let mut s = GrantStore::new(&path, now);
        let all = s.all();
        assert_eq!(all.len(), 1, "R3 is never permanent");
        assert_eq!(all[0].id, "ab12");
    }

    #[test]
    fn session_lapses_when_idle() {
        fn t0() -> f64 {
            1000.0
        }
        fn later() -> f64 {
            1000.0 + SESSION_IDLE_SECONDS + 1.0
        }
        let mut s = GrantStore::new(&temp("idle"), t0);
        s.add(
            "t",
            "x",
            &[PathBuf::from("/d")],
            Level::R1,
            Duration::Session,
        )
        .expect("add");
        s.clock = later;
        assert!(s.find("t", "x", &root("/d"), Level::R1).is_none());
    }
}
