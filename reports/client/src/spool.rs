// SPDX-License-Identifier: GPL-3.0-or-later
//! Reports kept on the device (ADR 0018 section 4), in
//! `/var/lib/vilna-reports/`:
//!
//! - `reports/<id>.json`: each crash report as it is or was sent, with whose
//!   program crashed and whether it is waiting, sent or refused by the server;
//! - `feedback/<id>.json`: feedback as it was sent, so the person can read it;
//! - `sent.json`: the ids of everything sent and whose it was, kept 90 days
//!   (as long as the server keeps it) so the person can ask to delete it.
//!
//! Limits: at most 50 reports wait, at most 20 are sent a day, the same crash
//! (signature and version) at most once a day, and reports older than 30
//! days are deleted.

use std::fs;
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use vilna_reports_protocol::{Crash, CrashReceipt, report_id};

pub const MAX_WAITING: usize = 50;
pub const MAX_SENT_PER_DAY: usize = 20;
pub const DAY: u64 = 86_400;
pub const KEEP: u64 = 30 * DAY;
/// The server keeps reports 90 days; their ids stay that long for `/v1/forget`.
pub const KEEP_IDS: u64 = 90 * DAY;
/// Below this uid a program is a system service: every person may see its
/// crashes. Above it, only the person whose program it was (and root).
pub const FIRST_PERSON_UID: u32 = 1000;

/// The longest report file read.
const MAX_REPORT_FILE: u64 = 1024 * 1024;

fn flags(f: rustix::fs::OFlags) -> i32 {
    i32::try_from(f.bits()).unwrap_or(0)
}

/// A regular file of at most `MAX_REPORT_FILE`: a pipe named `*.json` would
/// never answer and hold every reader.
fn read_plain(path: &Path) -> Option<Vec<u8>> {
    use std::io::Read;
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(flags(
            rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK,
        ))
        .open(path)
        .ok()?;
    if !file.metadata().ok()?.is_file() {
        return None;
    }
    let mut bytes = Vec::new();
    file.take(MAX_REPORT_FILE).read_to_end(&mut bytes).ok()?;
    Some(bytes)
}

/// Write a file so a reader sees the old or the new text, never half.
pub fn write_atomic(path: &Path, data: &[u8], mode: u32) -> std::io::Result<()> {
    let dir = path.parent().unwrap_or(Path::new("."));
    fs::create_dir_all(dir)?;
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    // A new name each time, never an existing file or a link: a link put at a
    // name known in advance made this overwrite another file.
    let random = crate::device::new_id().unwrap_or_else(|| format!("{}", std::process::id()));
    let tmp = dir.join(format!(".{name}.{random}.tmp"));
    let result = (|| {
        let mut f = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(mode)
            .custom_flags(flags(rustix::fs::OFlags::NOFOLLOW))
            .open(&tmp)?;
        f.set_permissions(fs::Permissions::from_mode(mode))?;
        f.write_all(data)?;
        f.sync_all()?;
        fs::rename(&tmp, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum State {
    /// Waiting: for the answer "ask", for the person; for "auto", for the network.
    Waiting,
    Sent,
    /// The server refused it (400 or 413); it is not sent again.
    Refused,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Report {
    pub crash: Crash,
    pub signature: String,
    /// Whose program crashed; `None` when the journal did not say.
    pub uid: Option<u32>,
    /// When it was found, seconds since 1970.
    pub found: u64,
    pub state: State,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sent_at: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub receipt: Option<CrashReceipt>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub refused: Option<String>,
}

impl Report {
    pub fn new(crash: Crash, uid: Option<u32>, found: u64) -> Report {
        Report {
            signature: crash.signature(),
            crash,
            uid,
            found,
            state: State::Waiting,
            sent_at: None,
            receipt: None,
            refused: None,
        }
    }

    /// Whether the person `uid` may see, send or discard this report.
    pub fn visible_to(&self, uid: u32) -> bool {
        uid == 0 || self.uid.is_none_or(|u| u < FIRST_PERSON_UID || u == uid)
    }

    fn same_crash(&self, other: &Report) -> bool {
        self.signature == other.signature && self.crash.version == other.crash.version
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SentId {
    pub id: String,
    pub uid: Option<u32>,
    pub at: u64,
    /// `crash` or `feedback`.
    pub kind: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Added {
    Added,
    /// The same crash is already waiting, or was sent in the last day.
    Duplicate,
}

pub struct Spool {
    dir: PathBuf,
}

impl Spool {
    pub fn open(dir: &Path) -> std::io::Result<Spool> {
        for sub in ["reports", "feedback"] {
            fs::create_dir_all(dir.join(sub))?;
        }
        Ok(Spool {
            dir: dir.to_path_buf(),
        })
    }

    fn report_path(&self, id: &str) -> Option<PathBuf> {
        // Only a well-formed id names a file: never a path from a request.
        report_id(id).map(|id| self.dir.join("reports").join(format!("{id}.json")))
    }

    /// All reports, newest first. A file that cannot be read is skipped.
    pub fn reports(&self) -> Vec<Report> {
        let mut out: Vec<Report> = fs::read_dir(self.dir.join("reports"))
            .into_iter()
            .flatten()
            .flatten()
            .filter(|e| e.path().extension().is_some_and(|x| x == "json"))
            .filter_map(|e| Some((e.path(), read_plain(&e.path())?)))
            .filter_map(|(path, b)| {
                let r: Report = serde_json::from_slice(&b).ok()?;
                // Only a file named by its own id: send() finds it by that name.
                (self.report_path(&r.crash.id).as_deref() == Some(path.as_path())).then_some(r)
            })
            .collect();
        out.sort_by_key(|r: &Report| std::cmp::Reverse(r.found));
        out
    }

    pub fn get(&self, id: &str) -> Option<Report> {
        let bytes = read_plain(&self.report_path(id)?)?;
        serde_json::from_slice(&bytes).ok()
    }

    pub fn save(&self, report: &Report) -> std::io::Result<()> {
        let path = self
            .report_path(&report.crash.id)
            .ok_or_else(|| std::io::Error::other("a report id must be a UUID"))?;
        let data = serde_json::to_vec_pretty(report).map_err(std::io::Error::other)?;
        write_atomic(&path, &data, 0o600)
    }

    pub fn remove(&self, id: &str) -> bool {
        self.report_path(id)
            .is_some_and(|p| fs::remove_file(p).is_ok())
    }

    /// Keep a new report, unless the same crash is already here; then make
    /// room by dropping the oldest waiting reports beyond `MAX_WAITING`.
    pub fn add(&self, report: &Report, now: u64) -> std::io::Result<Added> {
        let all = self.reports();
        let duplicate = all.iter().any(|r| {
            r.same_crash(report)
                && match r.state {
                    State::Waiting => true,
                    State::Sent => r.sent_at.is_some_and(|t| now.saturating_sub(t) < DAY),
                    State::Refused => false,
                }
        });
        if duplicate {
            return Ok(Added::Duplicate);
        }
        self.save(report)?;
        let mut waiting: Vec<&Report> = all.iter().filter(|r| r.state == State::Waiting).collect();
        // `all` is newest first, so the oldest are at the end.
        while waiting.len() + 1 > MAX_WAITING {
            if let Some(old) = waiting.pop() {
                self.remove(&old.crash.id);
            }
        }
        Ok(Added::Added)
    }

    pub fn sent_ids(&self) -> Vec<SentId> {
        fs::read(self.dir.join("sent.json"))
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default()
    }

    fn save_sent_ids(&self, ids: &[SentId]) -> std::io::Result<()> {
        let data = serde_json::to_vec_pretty(ids).map_err(std::io::Error::other)?;
        write_atomic(&self.dir.join("sent.json"), &data, 0o600)
    }

    pub fn record_sent(
        &self,
        id: &str,
        uid: Option<u32>,
        kind: &str,
        now: u64,
    ) -> std::io::Result<()> {
        let mut ids = self.sent_ids();
        if !ids.iter().any(|s| s.id == id) {
            ids.push(SentId {
                id: id.to_owned(),
                uid,
                at: now,
                kind: kind.to_owned(),
            });
        }
        self.save_sent_ids(&ids)
    }

    /// Crash reports sent in the last 24 hours (the daily limit).
    pub fn sent_last_day(&self, now: u64) -> usize {
        self.sent_ids()
            .iter()
            .filter(|s| s.kind == "crash" && now.saturating_sub(s.at) < DAY)
            .count()
    }

    /// The ids `uid` may ask the server to delete: their own (root: all).
    pub fn ids_of(&self, uid: u32) -> Vec<String> {
        self.sent_ids()
            .into_iter()
            .filter(|s| uid == 0 || s.uid == Some(uid))
            .map(|s| s.id)
            .collect()
    }

    /// After the server deleted them: forget the ids and the local copies.
    pub fn forgotten(&self, ids: &[String]) -> std::io::Result<()> {
        let left: Vec<SentId> = self
            .sent_ids()
            .into_iter()
            .filter(|s| !ids.contains(&s.id))
            .collect();
        for id in ids {
            self.remove(id);
            if let Some(id) = report_id(id) {
                let _ = fs::remove_file(self.dir.join("feedback").join(format!("{id}.json")));
            }
        }
        self.save_sent_ids(&left)
    }

    /// Feedback as it was sent, for the person to read later.
    pub fn save_feedback(&self, id: &str, body: &serde_json::Value) -> std::io::Result<()> {
        let id =
            report_id(id).ok_or_else(|| std::io::Error::other("a feedback id must be a UUID"))?;
        let data = serde_json::to_vec_pretty(body).map_err(std::io::Error::other)?;
        write_atomic(
            &self.dir.join("feedback").join(format!("{id}.json")),
            &data,
            0o600,
        )
    }

    /// Delete reports and feedback older than 30 days and ids older than 90.
    pub fn prune(&self, now: u64) -> std::io::Result<()> {
        for r in self.reports() {
            if now.saturating_sub(r.found) > KEEP {
                self.remove(&r.crash.id);
            }
        }
        // Files the list does not show (another name, a copy) go by their age.
        for entry in [self.dir.join("reports"), self.dir.join("feedback")]
            .iter()
            .flat_map(fs::read_dir)
            .flatten()
            .flatten()
        {
            let name = entry.file_name();
            let named_by_id = name.to_str().is_some_and(|n| {
                n.strip_suffix(".json")
                    .and_then(report_id)
                    .is_some_and(|id| format!("{id}.json") == n)
            });
            if named_by_id && entry.path().parent() == Some(self.dir.join("reports").as_path()) {
                continue; // aged by when it was found, above
            }
            let old = fs::symlink_metadata(entry.path())
                .and_then(|m| m.modified())
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .is_some_and(|t| now.saturating_sub(t.as_secs()) > KEEP);
            if old {
                let _ = fs::remove_file(entry.path());
            }
        }
        let ids = self.sent_ids();
        let kept: Vec<SentId> = ids
            .iter()
            .filter(|s| now.saturating_sub(s.at) <= KEEP_IDS)
            .cloned()
            .collect();
        if kept.len() != ids.len() {
            self.save_sent_ids(&kept)?;
        }
        Ok(())
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use vilna_reports_protocol::CrashWire;

    pub fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "vilna-reportd-{name}-{}-{}",
            std::process::id(),
            crate::device::new_id().unwrap_or_default()
        ));
        let _ = fs::remove_dir_all(&dir);
        dir
    }

    pub fn crash(program: &str, function: &str) -> Crash {
        let id = crate::device::new_id().unwrap();
        let wire: CrashWire = serde_json::from_value(serde_json::json!({
            "schema": 1, "id": id, "program": {"name": program, "version": "1.0"},
            "kind": "signal", "signal": "SIGSEGV",
            "frames": [{"module": "libx.so", "offset": "0x10", "function": function}]
        }))
        .unwrap();
        Crash::from_wire(wire).unwrap()
    }

    #[test]
    fn the_same_crash_waits_once_and_is_sent_once_a_day() {
        let spool = Spool::open(&temp_dir("dup")).unwrap();
        let first = Report::new(crash("gedit", "f"), Some(1000), 100);
        assert_eq!(spool.add(&first, 100).unwrap(), Added::Added);
        let again = Report::new(crash("gedit", "f"), Some(1000), 200);
        assert_eq!(spool.add(&again, 200).unwrap(), Added::Duplicate);
        let mut sent = spool.get(&first.crash.id).unwrap();
        sent.state = State::Sent;
        sent.sent_at = Some(300);
        spool.save(&sent).unwrap();
        assert_eq!(spool.add(&again, 400).unwrap(), Added::Duplicate);
        assert_eq!(spool.add(&again, 300 + DAY).unwrap(), Added::Added);
        let other = Report::new(crash("gedit", "g"), Some(1000), 500);
        assert_eq!(spool.add(&other, 500).unwrap(), Added::Added);
    }

    #[test]
    fn at_most_fifty_wait_and_the_oldest_go() {
        let spool = Spool::open(&temp_dir("cap")).unwrap();
        let mut first = None;
        for i in 0..(MAX_WAITING as u64 + 5) {
            let r = Report::new(crash("gedit", &format!("f{i}")), None, i);
            first.get_or_insert(r.crash.id.clone());
            spool.add(&r, i).unwrap();
        }
        assert_eq!(spool.reports().len(), MAX_WAITING);
        assert!(spool.get(first.as_deref().unwrap()).is_none());
    }

    #[test]
    fn old_reports_and_ids_are_deleted() {
        let spool = Spool::open(&temp_dir("prune")).unwrap();
        let r = Report::new(crash("gedit", "f"), None, 0);
        spool.add(&r, 0).unwrap();
        spool.record_sent(&r.crash.id, None, "crash", 0).unwrap();
        spool.prune(KEEP).unwrap();
        assert!(spool.get(&r.crash.id).is_some());
        spool.prune(KEEP + 1).unwrap();
        assert!(spool.get(&r.crash.id).is_none());
        assert_eq!(
            spool.sent_ids().len(),
            1,
            "ids stay as long as the server keeps them"
        );
        spool.prune(KEEP_IDS + 1).unwrap();
        assert!(spool.sent_ids().is_empty());
    }

    #[test]
    fn a_person_sees_their_own_and_the_systems_crashes() {
        let mine = Report::new(crash("gedit", "f"), Some(1000), 0);
        let system = Report::new(crash("gdm", "f"), Some(42), 0);
        let unknown = Report::new(crash("x", "f"), None, 0);
        assert!(mine.visible_to(1000) && mine.visible_to(0) && !mine.visible_to(1001));
        assert!(system.visible_to(1001) && unknown.visible_to(1001));
    }

    #[test]
    fn ids_from_requests_never_name_other_files() {
        let spool = Spool::open(&temp_dir("ids")).unwrap();
        assert!(spool.get("../sent").is_none());
        assert!(!spool.remove("../../etc/passwd"));
        assert!(spool.save_feedback("../x", &serde_json::json!({})).is_err());
    }

    #[test]
    fn forgetting_removes_the_ids_and_the_copies() {
        let spool = Spool::open(&temp_dir("forget")).unwrap();
        let r = Report::new(crash("gedit", "f"), Some(1000), 0);
        spool.add(&r, 0).unwrap();
        spool
            .record_sent(&r.crash.id, Some(1000), "crash", 0)
            .unwrap();
        spool
            .record_sent(
                "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                Some(1001),
                "feedback",
                0,
            )
            .unwrap();
        assert_eq!(spool.ids_of(1000), vec![r.crash.id.clone()]);
        assert_eq!(spool.ids_of(0).len(), 2);
        spool.forgotten(&spool.ids_of(1000)).unwrap();
        assert!(spool.get(&r.crash.id).is_none());
        assert_eq!(spool.ids_of(0).len(), 1);
    }
}
