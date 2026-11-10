// SPDX-License-Identifier: GPL-3.0-or-later
//! SQLite storage. Reports and feedback are kept `retention` (90 days, ADR
//! 0018 section 6); after that only the per-signature counters in `groups`
//! remain. No table has a column for an IP address or any request metadata.

use std::sync::{Mutex, MutexGuard};

use rusqlite::{Connection, OptionalExtension, params};
use serde::Serialize;
use vilna_reports_protocol::{Crash, Feedback};

/// Repeat counts at which a tracked crash's issue gets a comment, so a
/// frequent crash stands out without noise (as on report.tintaoffice.com).
pub const MILESTONES: &[i64] = &[2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000];
/// Versions remembered per signature.
const MAX_VERSIONS: usize = 10;

#[derive(Debug)]
pub struct StoreError(pub String);

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "storage: {}", self.0)
    }
}

impl std::error::Error for StoreError {}

impl From<rusqlite::Error> for StoreError {
    fn from(e: rusqlite::Error) -> Self {
        StoreError(e.to_string())
    }
}

impl From<serde_json::Error> for StoreError {
    fn from(e: serde_json::Error) -> Self {
        StoreError(e.to_string())
    }
}

pub type Result<T> = std::result::Result<T, StoreError>;

/// One distinct crash: what we know about all reports with one signature.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Group {
    pub signature: String,
    pub program: String,
    pub kind: String,
    pub title: String,
    pub first: i64,
    pub last: i64,
    pub count: i64,
    pub versions: Vec<String>,
    /// The public issue, once we confirmed the reports hold nothing personal.
    pub issue: Option<i64>,
    pub approved: bool,
    pub fixed_in: Option<String>,
}

pub struct Inserted {
    pub group: Group,
    /// False when this report id was already stored (the device retried).
    pub new_report: bool,
    pub new_group: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct StoredFeedback {
    pub received: i64,
    pub contact: Option<String>,
    #[serde(flatten)]
    pub feedback: Feedback,
}

pub struct Store {
    db: Mutex<Connection>,
}

const SCHEMA_SQL: &str = "
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA secure_delete = ON;
CREATE TABLE IF NOT EXISTS crashes (
    id TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    received INTEGER NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS crashes_signature ON crashes (signature, received);
CREATE INDEX IF NOT EXISTS crashes_received ON crashes (received);
CREATE TABLE IF NOT EXISTS groups (
    signature TEXT PRIMARY KEY,
    program TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    first INTEGER NOT NULL,
    last INTEGER NOT NULL,
    count INTEGER NOT NULL,
    versions TEXT NOT NULL,
    issue INTEGER,
    approved INTEGER NOT NULL DEFAULT 0,
    fixed_in TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    received INTEGER NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS feedback_received ON feedback (received);
-- The address for a reply lives apart from the text and goes with it.
CREATE TABLE IF NOT EXISTS contacts (
    feedback_id TEXT PRIMARY KEY REFERENCES feedback (id) ON DELETE CASCADE,
    email TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issue_quota (
    day TEXT PRIMARY KEY,
    count INTEGER NOT NULL
);
";

fn group_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Group> {
    let versions: String = row.get(7)?;
    Ok(Group {
        signature: row.get(0)?,
        program: row.get(1)?,
        kind: row.get(2)?,
        title: row.get(3)?,
        first: row.get(4)?,
        last: row.get(5)?,
        count: row.get(6)?,
        versions: serde_json::from_str(&versions).unwrap_or_default(),
        issue: row.get(8)?,
        approved: row.get::<_, i64>(9)? != 0,
        fixed_in: row.get(10)?,
    })
}

const GROUP_COLUMNS: &str =
    "signature, program, kind, title, first, last, count, versions, issue, approved, fixed_in";

impl Store {
    pub fn open(path: &str) -> Result<Store> {
        let db = if path == ":memory:" {
            Connection::open_in_memory()?
        } else {
            Connection::open(path)?
        };
        db.busy_timeout(std::time::Duration::from_secs(5))?;
        db.execute_batch(SCHEMA_SQL)?;
        Ok(Store { db: Mutex::new(db) })
    }

    fn db(&self) -> MutexGuard<'_, Connection> {
        // A panic while holding the lock leaves SQLite consistent (every
        // change is one transaction), so a poisoned lock is safe to reuse.
        self.db
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    pub fn group(&self, signature: &str) -> Result<Option<Group>> {
        let db = self.db();
        let sql = format!("SELECT {GROUP_COLUMNS} FROM groups WHERE signature = ?1");
        Ok(db.query_row(&sql, [signature], group_from_row).optional()?)
    }

    pub fn groups(&self, limit: u32) -> Result<Vec<Group>> {
        let db = self.db();
        let sql = format!("SELECT {GROUP_COLUMNS} FROM groups ORDER BY last DESC LIMIT ?1");
        let mut stmt = db.prepare(&sql)?;
        let rows = stmt.query_map([limit], group_from_row)?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    /// Stores one report and counts it in its group. A report id seen before
    /// is not counted again.
    pub fn insert_crash(&self, crash: &Crash, signature: &str, now: i64) -> Result<Inserted> {
        let mut db = self.db();
        let tx = db.transaction()?;
        let existing: Option<String> = tx
            .query_row(
                "SELECT signature FROM crashes WHERE id = ?1",
                [&crash.id],
                |r| r.get(0),
            )
            .optional()?;
        let sql = format!("SELECT {GROUP_COLUMNS} FROM groups WHERE signature = ?1");
        if let Some(stored_signature) = existing {
            let group = tx.query_row(&sql, [&stored_signature], group_from_row)?;
            return Ok(Inserted {
                group,
                new_report: false,
                new_group: false,
            });
        }
        tx.execute(
            "INSERT INTO crashes (id, signature, received, body) VALUES (?1, ?2, ?3, ?4)",
            params![crash.id, signature, now, serde_json::to_string(crash)?],
        )?;
        let found = tx.query_row(&sql, [signature], group_from_row).optional()?;
        let new_group = found.is_none();
        let mut group = found.unwrap_or_else(|| Group {
            signature: signature.to_owned(),
            program: crash.program.clone(),
            kind: crash.kind.as_str().to_owned(),
            title: crash.title(),
            first: now,
            last: now,
            count: 0,
            versions: Vec::new(),
            issue: None,
            approved: false,
            fixed_in: None,
        });
        group.count = group.count.saturating_add(1);
        group.last = now;
        if let Some(v) = &crash.version {
            if !group.versions.contains(v) {
                group.versions.push(v.clone());
                let extra = group.versions.len().saturating_sub(MAX_VERSIONS);
                group.versions.drain(..extra);
            }
        }
        tx.execute(
            "INSERT INTO groups (signature, program, kind, title, first, last, count, versions)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
             ON CONFLICT (signature) DO UPDATE SET last = ?6, count = ?7, versions = ?8",
            params![
                group.signature,
                group.program,
                group.kind,
                group.title,
                group.first,
                group.last,
                group.count,
                serde_json::to_string(&group.versions)?
            ],
        )?;
        tx.commit()?;
        Ok(Inserted {
            group,
            new_report: true,
            new_group,
        })
    }

    /// The group of one stored report, if the report is still here.
    pub fn group_of_report(&self, id: &str) -> Result<Option<Group>> {
        let signature: Option<String> = self
            .db()
            .query_row("SELECT signature FROM crashes WHERE id = ?1", [id], |r| {
                r.get(0)
            })
            .optional()?;
        match signature {
            Some(s) => self.group(&s),
            None => Ok(None),
        }
    }

    /// The newest stored reports of one group, newest first.
    pub fn reports(&self, signature: &str, limit: u32) -> Result<Vec<Crash>> {
        let db = self.db();
        let mut stmt = db.prepare(
            "SELECT body FROM crashes WHERE signature = ?1 ORDER BY received DESC LIMIT ?2",
        )?;
        let rows = stmt.query_map(params![signature, limit], |r| r.get::<_, String>(0))?;
        let mut out = Vec::new();
        for body in rows {
            if let Ok(crash) = serde_json::from_str(&body?) {
                out.push(crash);
            }
        }
        Ok(out)
    }

    pub fn set_approved(&self, signature: &str) -> Result<bool> {
        Ok(self.db().execute(
            "UPDATE groups SET approved = 1 WHERE signature = ?1",
            [signature],
        )? > 0)
    }

    pub fn set_issue(&self, signature: &str, issue: i64) -> Result<()> {
        self.db().execute(
            "UPDATE groups SET issue = ?2 WHERE signature = ?1",
            params![signature, issue],
        )?;
        Ok(())
    }

    pub fn set_fixed(&self, signature: &str, version: Option<&str>) -> Result<bool> {
        Ok(self.db().execute(
            "UPDATE groups SET fixed_in = ?2 WHERE signature = ?1",
            params![signature, version],
        )? > 0)
    }

    /// Takes one of the day's new-issue slots; false when none is left.
    pub fn take_issue_slot(&self, day: &str, max: i64) -> Result<bool> {
        let mut db = self.db();
        let tx = db.transaction()?;
        let used: i64 = tx
            .query_row("SELECT count FROM issue_quota WHERE day = ?1", [day], |r| {
                r.get(0)
            })
            .optional()?
            .unwrap_or(0);
        if used >= max {
            return Ok(false);
        }
        tx.execute(
            "INSERT INTO issue_quota (day, count) VALUES (?1, 1)
             ON CONFLICT (day) DO UPDATE SET count = count + 1",
            [day],
        )?;
        tx.execute(
            "DELETE FROM issue_quota WHERE day < date(?1, '-7 days')",
            [day],
        )?;
        tx.commit()?;
        Ok(true)
    }

    /// Gives back a slot taken for an issue that GitHub then refused.
    pub fn return_issue_slot(&self, day: &str) -> Result<()> {
        self.db().execute(
            "UPDATE issue_quota SET count = max(count - 1, 0) WHERE day = ?1",
            [day],
        )?;
        Ok(())
    }

    /// Stores feedback; false when this id was already stored.
    pub fn insert_feedback(&self, feedback: &Feedback, now: i64) -> Result<bool> {
        let mut db = self.db();
        let tx = db.transaction()?;
        let added = tx.execute(
            "INSERT OR IGNORE INTO feedback (id, received, body) VALUES (?1, ?2, ?3)",
            params![feedback.id, now, serde_json::to_string(feedback)?],
        )?;
        if added > 0 {
            if let Some(email) = &feedback.contact {
                tx.execute(
                    "INSERT INTO contacts (feedback_id, email) VALUES (?1, ?2)",
                    params![feedback.id, email],
                )?;
            }
        }
        tx.commit()?;
        Ok(added > 0)
    }

    pub fn feedback(&self, limit: u32) -> Result<Vec<StoredFeedback>> {
        let db = self.db();
        let mut stmt = db.prepare(
            "SELECT f.received, f.body, c.email FROM feedback f
             LEFT JOIN contacts c ON c.feedback_id = f.id ORDER BY f.received DESC LIMIT ?1",
        )?;
        let rows = stmt.query_map([limit], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, Option<String>>(2)?,
            ))
        })?;
        let mut out = Vec::new();
        for row in rows {
            let (received, body, contact) = row?;
            if let Ok(feedback) = serde_json::from_str(&body) {
                out.push(StoredFeedback {
                    received,
                    contact,
                    feedback,
                });
            }
        }
        Ok(out)
    }

    pub fn delete_feedback(&self, id: &str) -> Result<bool> {
        Ok(self
            .db()
            .execute("DELETE FROM feedback WHERE id = ?1", [id])?
            > 0)
    }

    /// Deletes the reports and feedback with these ids (`/v1/forget`). Group
    /// counters stay: they hold nothing about the device.
    pub fn forget(&self, ids: &[String]) -> Result<usize> {
        let mut db = self.db();
        let tx = db.transaction()?;
        let mut deleted = 0;
        for id in ids {
            deleted += tx.execute("DELETE FROM crashes WHERE id = ?1", [id])?;
            deleted += tx.execute("DELETE FROM feedback WHERE id = ?1", [id])?;
        }
        tx.commit()?;
        Ok(deleted)
    }

    /// Deletes everything received before `before`; returns how many reports
    /// and how much feedback went.
    pub fn purge(&self, before: i64) -> Result<(usize, usize)> {
        let mut db = self.db();
        let tx = db.transaction()?;
        let crashes = tx.execute("DELETE FROM crashes WHERE received < ?1", [before])?;
        let feedback = tx.execute("DELETE FROM feedback WHERE received < ?1", [before])?;
        tx.commit()?;
        Ok((crashes, feedback))
    }
}
