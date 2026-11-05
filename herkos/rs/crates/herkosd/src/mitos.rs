// SPDX-License-Identifier: GPL-3.0-or-later
//! Mitos: the append-only action journal (permissions.md 7.1), in the same
//! format as Herkos-lite (`mitos.py`): `herkos journal verify` checks a
//! journal written by either.
//!
//! One JSON object per line. Every record carries the SHA-256 of the previous
//! record, so changing or deleting a record breaks the chain. The journal
//! holds descriptions and hashes, never file or message contents.
//!
//! Record fields: `seq`, `time` (UTC, ISO 8601), `kind`, `action_id`,
//! `data`, `prev` (hash of the previous record, or 64 zeros), `hash`.
//! Hashes are over canonical JSON: keys sorted, no spaces, UTF-8 unescaped,
//! which is what Python's `json.dumps(sort_keys=True, ensure_ascii=False,
//! separators=(",", ":"))` writes for the strings, integers and booleans
//! the records hold.

use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::{DirBuilderExt, FileExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

pub const GENESIS: &str = "0000000000000000000000000000000000000000000000000000000000000000";
const KINDS: [&str; 6] = ["start", "request", "decision", "result", "grant", "program"];

/// The journal cannot be written or is broken. Herkos then refuses all actions.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JournalError(pub String);

impl std::fmt::Display for JournalError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

pub fn canonical(value: &Value) -> Vec<u8> {
    // serde_json's Map is ordered by key, and it writes no spaces.
    serde_json::to_vec(value).unwrap_or_default()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn record_hash(record: &Map<String, Value>) -> String {
    let mut body = record.clone();
    body.remove("hash");
    hex(&Sha256::digest(canonical(&Value::Object(body))))
}

/// Hash of a value for the journal, in place of the value itself.
pub fn digest(value: &Value) -> String {
    format!(
        "sha256:{}",
        hex(&Sha256::digest(canonical(&json!({"v": value}))))
    )
}

/// `YYYY-MM-DDTHH:MM:SS.mmm+00:00`, as Python's `isoformat(timespec="milliseconds")`.
pub fn utc_now() -> String {
    let ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis());
    format_utc(ms)
}

pub fn format_utc(ms: u128) -> String {
    let secs = (ms / 1000) as i64;
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // Howard Hinnant's days-to-civil.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}+00:00",
        rem / 3600,
        rem % 3600 / 60,
        rem % 60,
        ms % 1000
    )
}

/// Each line of the journal, parsed; `None` for a line that is not JSON.
pub fn read_records(path: &Path) -> std::io::Result<Vec<Option<Value>>> {
    let reader = BufReader::new(File::open(path)?);
    let mut out = Vec::new();
    for line in reader.split(b'\n') {
        out.push(serde_json::from_slice(&line?).ok());
    }
    Ok(out)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Verified {
    pub records: u64,
    pub last_hash: String,
}

/// Check the chain. On failure: the 1-based line and what is wrong with it.
pub fn verify(path: &Path) -> Result<Verified, (usize, String)> {
    let records = match read_records(path) {
        Ok(r) => r,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Vec::new(),
        Err(e) => return Err((0, e.to_string())),
    };
    let mut prev = GENESIS.to_owned();
    let mut count = 0u64;
    for (i, rec) in records.iter().enumerate() {
        let line = i + 1;
        let Some(Value::Object(rec)) = rec else {
            return Err((line, "line is not a JSON object".into()));
        };
        if rec.get("prev").and_then(Value::as_str) != Some(prev.as_str()) {
            return Err((line, "chain broken: a record was changed or removed".into()));
        }
        if rec.get("seq").and_then(Value::as_u64) != Some(count) {
            return Err((line, "sequence number out of order".into()));
        }
        let hash = record_hash(rec);
        if rec.get("hash").and_then(Value::as_str) != Some(hash.as_str()) {
            return Err((line, "record does not match its hash".into()));
        }
        prev = hash;
        count += 1;
    }
    Ok(Verified {
        records: count,
        last_hash: prev,
    })
}

struct Tail {
    seq: u64,
    prev: String,
}

pub struct Journal {
    path: PathBuf,
    tail: Mutex<Tail>,
}

impl Journal {
    pub fn open(path: &Path) -> Result<Journal, JournalError> {
        let v = verify(path).map_err(|(line, why)| {
            JournalError(format!("{}, line {line}: {why}", path.display()))
        })?;
        Ok(Journal {
            path: path.to_path_buf(),
            tail: Mutex::new(Tail {
                seq: v.records,
                prev: v.last_hash,
            }),
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn append(
        &self,
        kind: &str,
        data: Value,
        action_id: Option<&str>,
    ) -> Result<Value, JournalError> {
        if !KINDS.contains(&kind) {
            return Err(JournalError(format!("unknown journal record kind: {kind}")));
        }
        let mut tail = self
            .tail
            .lock()
            .map_err(|_| JournalError("journal lock poisoned".into()))?;
        let mut record = Map::new();
        record.insert("seq".into(), json!(tail.seq));
        record.insert("time".into(), json!(utc_now()));
        record.insert("kind".into(), json!(kind));
        record.insert("action_id".into(), json!(action_id));
        record.insert("data".into(), data);
        record.insert("prev".into(), json!(tail.prev));
        let hash = record_hash(&record);
        record.insert("hash".into(), json!(hash));
        let record = Value::Object(record);
        let mut line = canonical(&record);
        line.push(b'\n');
        self.write(&line, &tail.prev)
            .map_err(|e| JournalError(format!("cannot write {}: {e}", self.path.display())))??;
        tail.seq += 1;
        tail.prev = hash;
        Ok(record)
    }

    fn write(&self, line: &[u8], prev: &str) -> std::io::Result<Result<(), JournalError>> {
        if let Some(parent) = self.path.parent() {
            fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(parent)?;
        }
        let mut f = OpenOptions::new()
            .read(true)
            .append(true)
            .create(true)
            .mode(0o600)
            .open(&self.path)?;
        // Released when the file closes.
        rustix::fs::flock(&f, rustix::fs::FlockOperation::LockExclusive)?;
        if f.metadata()?.len() > 0 && !tail_matches(&f, prev)? {
            return Ok(Err(JournalError(
                "the journal was changed by someone else".into(),
            )));
        }
        f.write_all(line)?;
        f.sync_all()?;
        Ok(Ok(()))
    }
}

/// The last line on disk is still the last record we wrote.
fn tail_matches(f: &File, prev: &str) -> std::io::Result<bool> {
    let size = f.metadata()?.len();
    let chunk = size.min(64 * 1024);
    let mut buf = vec![0u8; usize::try_from(chunk).unwrap_or(0)];
    f.read_exact_at(&mut buf, size - chunk)?;
    while buf.last() == Some(&b'\n') {
        buf.pop();
    }
    let last = buf.rsplit(|b| *b == b'\n').next().unwrap_or(&[]);
    Ok(serde_json::from_slice::<Value>(last)
        .ok()
        .is_some_and(|v| v.get("hash").and_then(Value::as_str) == Some(prev)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("herkosd-mitos-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        dir.join("journal.jsonl")
    }

    #[test]
    fn canonical_like_python() {
        let v = json!({"b": 1, "a": "ї\n\u{1}", "c": [true, null]});
        assert_eq!(
            String::from_utf8(canonical(&v)).expect("utf-8"),
            "{\"a\":\"ї\\n\\u0001\",\"b\":1,\"c\":[true,null]}"
        );
        // Computed with Python's json.dumps + hashlib.
        assert_eq!(
            digest(&json!({"b": [1, "ї"]})),
            "sha256:e2783b2cbadc192dba144f5bae53cc5a76b3826f365e474172ab5b6271706013"
        );
    }

    #[test]
    fn time_format() {
        assert_eq!(format_utc(0), "1970-01-01T00:00:00.000+00:00");
        assert_eq!(
            format_utc(1_793_658_481_123),
            "2026-11-02T22:28:01.123+00:00"
        );
        assert_eq!(format_utc(951_782_400_000), "2000-02-29T00:00:00.000+00:00");
    }

    #[test]
    fn chain_and_tamper() {
        let path = temp("chain");
        let j = Journal::open(&path).expect("open");
        j.append("start", json!({"server": "t"}), None)
            .expect("append");
        j.append("request", json!({"tool": "a"}), Some("id1"))
            .expect("append");
        assert_eq!(verify(&path).map(|v| v.records), Ok(2));
        assert!(j.append("what", json!({}), None).is_err());

        // Reopening continues the chain.
        let j2 = Journal::open(&path).expect("reopen");
        j2.append("decision", json!({"code": "denied_level"}), Some("id1"))
            .expect("append");
        assert_eq!(verify(&path).map(|v| v.records), Ok(3));
        // The first handle no longer holds the tail: it must refuse.
        assert!(j.append("result", json!({}), Some("id1")).is_err());

        let text = fs::read_to_string(&path)
            .expect("read")
            .replace("\"tool\":\"a\"", "\"tool\":\"b\"");
        fs::write(&path, text).expect("write");
        assert_eq!(verify(&path).map_err(|e| e.0), Err(2));
        assert!(Journal::open(&path).is_err());
    }
}
