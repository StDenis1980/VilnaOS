// SPDX-License-Identifier: GPL-3.0-or-later
//! Mitos snapshots of a person's home (ADR 0026, section 2).
//!
//! Each person's home is a btrfs subvolume with a snapper config of its own,
//! `mitos-<uid>` (made as root by `herkosd mitos-setup`). Its `ALLOW_USERS`
//! lets the user `herkos` create and compare snapshots through snapperd on
//! D-Bus; herkosd never reads the person's files itself.
//!
//! Before an action that changes files and that the tool cannot undo by
//! itself, the gate takes a `pre` snapshot; after it, a `post` one. Undo asks
//! snapper what changed between the two and has the files tool, running as
//! the person, copy those files back from the `pre` snapshot.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use herkos_core::request::normpath;
use serde_json::Value;

use crate::passwd::Account;

/// The most changed paths an undo works with; more and it refuses.
pub const MAX_CHANGES: usize = 1000;
/// Snapper's output read at most, in bytes.
const MAX_OUTPUT: u64 = 4 * 1024 * 1024;
/// A snapshot is quick; a comparison of a busy home may take a while.
const CREATE_TIMEOUT: Duration = Duration::from_secs(30);
const STATUS_TIMEOUT: Duration = Duration::from_secs(120);
/// How long Mitos keeps a pair of snapshots: as the files tool keeps its
/// backups and OneDrive its versions (ADR 0026, section 4). With little free
/// space snapper removes them sooner, but none younger than a day.
pub const KEEP_DAYS: u64 = 30;
const PRUNE_EVERY: Duration = Duration::from_secs(6 * 3600);
/// What `herkosd mitos-setup` sets in each `mitos-<uid>` config.
pub const CONFIG_VALUES: [&str; 11] = [
    "SYNC_ACL=yes",
    "TIMELINE_CREATE=no",
    "TIMELINE_CLEANUP=no",
    "NUMBER_CLEANUP=yes",
    "NUMBER_MIN_AGE=86400",
    "NUMBER_LIMIT=2-1000",
    "NUMBER_LIMIT_IMPORTANT=0",
    "FREE_LIMIT=0.2",
    "SPACE_LIMIT=0.5",
    "EMPTY_PRE_POST_CLEANUP=yes",
    "EMPTY_PRE_POST_MIN_AGE=1800",
];

/// What changed between two snapshots, from `snapper status`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Change {
    /// `+` created, `-` deleted, `c` content changed, `t` type changed,
    /// `.` only metadata.
    pub kind: char,
    pub path: String,
}

pub trait Snapshots: Send + Sync {
    /// The subvolume the snapshots are of: the person's home.
    fn subvolume(&self) -> &Path;
    /// A `pre` snapshot, before the action `action_id`. Its number.
    fn before(&self, action_id: &str) -> Result<u64, String>;
    /// The `post` snapshot that pairs with `pre`.
    fn after(&self, pre: u64, action_id: &str) -> Result<u64, String>;
    /// What changed from snapshot `from` to `to` (`0`: the home as it is now).
    fn changes(&self, from: u64, to: u64) -> Result<Vec<Change>, String>;
}

/// `snapper -c mitos-<uid>`, through snapperd.
pub struct Snapper {
    pub program: PathBuf,
    pub config: String,
    pub subvolume: PathBuf,
    /// When Mitos last removed its pairs older than `KEEP_DAYS`.
    pruned: Mutex<Option<Instant>>,
}

/// The snapper config of a person's home.
pub fn config_name(uid: u32) -> String {
    format!("mitos-{uid}")
}

impl Snapper {
    /// The person's snapper config, if `herkosd mitos-setup` made one.
    pub fn for_person(uid: u32, home: &Path, configs: &Path) -> Option<Snapper> {
        let config = config_name(uid);
        if !configs.join(&config).is_file() {
            return None;
        }
        let program = ["/usr/bin/snapper", "/usr/sbin/snapper"]
            .into_iter()
            .map(PathBuf::from)
            .find(|p| p.is_file())?;
        Some(Snapper {
            program,
            config,
            subvolume: home.to_path_buf(),
            pruned: Mutex::new(None),
        })
    }

    /// Removes Mitos snapshots older than `KEEP_DAYS`, at most every few hours.
    fn prune_now_and_then(&self) {
        let Ok(mut last) = self.pruned.lock() else {
            return;
        };
        if last.is_some_and(|t| t.elapsed() < PRUNE_EVERY) {
            return;
        }
        *last = Some(Instant::now());
        drop(last);
        let listing = match self.run(
            &["--jsonout", "list", "--disable-used-space"],
            STATUS_TIMEOUT,
        ) {
            Ok(text) => text,
            Err(e) => return eprintln!("herkosd: Mitos: cannot list snapshots: {e}"),
        };
        let old = expired(&listing, &self.config, &cutoff_day(now_ms(), KEEP_DAYS));
        for chunk in old.chunks(100) {
            let numbers: Vec<String> = chunk.iter().map(u64::to_string).collect();
            let mut args = vec!["delete"];
            args.extend(numbers.iter().map(String::as_str));
            if let Err(e) = self.run(&args, STATUS_TIMEOUT) {
                eprintln!("herkosd: Mitos: cannot remove old snapshots: {e}");
            }
        }
    }

    fn run(&self, args: &[&str], timeout: Duration) -> Result<String, String> {
        let mut command = Command::new(&self.program);
        command.arg("-c").arg(&self.config).args(args);
        run(command, timeout)
    }

    fn create(&self, args: &[&str]) -> Result<u64, String> {
        let out = self.run(args, CREATE_TIMEOUT)?;
        out.trim()
            .parse()
            .map_err(|_| format!("snapper gave no snapshot number: {}", out.trim()))
    }
}

impl Snapshots for Snapper {
    fn subvolume(&self) -> &Path {
        &self.subvolume
    }

    fn before(&self, action_id: &str) -> Result<u64, String> {
        self.prune_now_and_then();
        let description = format!("Mitos: before {action_id}");
        let userdata = format!("mitos={action_id}");
        self.create(&[
            "create",
            "--type",
            "pre",
            "--print-number",
            "--cleanup-algorithm",
            "number",
            "--description",
            &description,
            "--userdata",
            &userdata,
        ])
    }

    fn after(&self, pre: u64, action_id: &str) -> Result<u64, String> {
        let pre = pre.to_string();
        let description = format!("Mitos: after {action_id}");
        let userdata = format!("mitos={action_id}");
        self.create(&[
            "create",
            "--type",
            "post",
            "--pre-number",
            &pre,
            "--print-number",
            "--cleanup-algorithm",
            "number",
            "--description",
            &description,
            "--userdata",
            &userdata,
        ])
    }

    fn changes(&self, from: u64, to: u64) -> Result<Vec<Change>, String> {
        let range = format!("{from}..{to}");
        parse_status(&self.run(&["status", &range], STATUS_TIMEOUT)?)
    }
}

fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis())
}

/// `YYYY-MM-DD`, `days` before `now_ms` (UTC; snapper writes local time,
/// and a few hours either way do not matter for a month).
pub fn cutoff_day(now_ms: u128, days: u64) -> String {
    let back = u128::from(days) * 86_400_000;
    crate::mitos::format_utc(now_ms.saturating_sub(back))
        .chars()
        .take(10)
        .collect()
}

/// Numbers of the Mitos snapshots (userdata `mitos`) from before `cutoff`,
/// in `snapper --jsonout list` of `config`.
pub fn expired(listing: &str, config: &str, cutoff: &str) -> Vec<u64> {
    let Ok(v) = serde_json::from_str::<Value>(listing) else {
        return Vec::new();
    };
    let Some(items) = v.get(config).and_then(Value::as_array) else {
        return Vec::new();
    };
    items
        .iter()
        .filter(|i| i.get("userdata").and_then(|u| u.get("mitos")).is_some())
        .filter(|i| {
            i.get("date")
                .and_then(Value::as_str)
                .and_then(|d| d.get(..10))
                .is_some_and(|day| day.len() == 10 && day < cutoff)
        })
        .filter_map(|i| i.get("number").and_then(Value::as_u64))
        .filter(|n| *n > 0)
        .collect()
}

/// Makes or updates the snapper config of each person's home (ADR 0026,
/// section 1). Runs as root (`ExecStartPre=+` of herkosd.service). A home
/// that is not a btrfs subvolume of its own is skipped: that person's
/// actions stay as they were, without snapshots. Returns what it did.
pub fn setup(people: &[Account], snapper: &Path, configs: &Path) -> Vec<String> {
    let mut report = Vec::new();
    for person in people {
        let config = config_name(person.uid);
        let home = person.home.display().to_string();
        let snap = |args: &[&str]| {
            let mut c = Command::new(snapper);
            c.args(["--no-dbus", "-c", &config]).args(args);
            run(c, STATUS_TIMEOUT)
        };
        if !configs.join(&config).is_file() {
            if let Err(e) = snap(&["create-config", "--fstype", "btrfs", &home]) {
                report.push(format!(
                    "{}: no snapshots ({home} is not a btrfs subvolume of its own?): {e}",
                    person.name
                ));
                continue;
            }
        }
        let allow = format!("ALLOW_USERS=herkos {}", person.name);
        let mut values: Vec<&str> = vec![&allow];
        values.extend(CONFIG_VALUES);
        let mut args = vec!["set-config"];
        args.extend(values);
        match snap(&args) {
            Ok(_) => report.push(format!("{}: snapshots of {home} ({config})", person.name)),
            Err(e) => report.push(format!("{}: cannot set up {config}: {e}", person.name)),
        }
    }
    report
}

/// Runs a command with a deadline; stdout, at most `MAX_OUTPUT` bytes.
pub(crate) fn run(mut command: Command, timeout: Duration) -> Result<String, String> {
    let name = command.get_program().to_string_lossy().into_owned();
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("{name}: {e}"))?;
    let reader = |pipe: Option<Box<dyn Read + Send>>| {
        thread::spawn(move || {
            let mut buf = Vec::new();
            if let Some(p) = pipe {
                let _ = p.take(MAX_OUTPUT).read_to_end(&mut buf);
            }
            buf
        })
    };
    let out = reader(
        child
            .stdout
            .take()
            .map(|p| Box::new(p) as Box<dyn Read + Send>),
    );
    let err = reader(
        child
            .stderr
            .take()
            .map(|p| Box::new(p) as Box<dyn Read + Send>),
    );
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(20)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("{name} took longer than {}s", timeout.as_secs()));
            }
            Err(e) => return Err(format!("{name}: {e}")),
        }
    };
    let out = out.join().unwrap_or_default();
    let err = err.join().unwrap_or_default();
    if !status.success() {
        let why = String::from_utf8_lossy(&err);
        let why = why.trim().lines().last().unwrap_or("").to_owned();
        return Err(format!("{name} failed ({status}): {why}"));
    }
    String::from_utf8(out).map_err(|_| format!("{name}: output is not UTF-8"))
}

/// `snapper status` lines: a status string of five or six characters, a
/// space, an absolute path. A line that is not like that (a file name with
/// a line break in it, for one) makes the whole answer untrusted.
pub fn parse_status(text: &str) -> Result<Vec<Change>, String> {
    let mut out = Vec::new();
    for line in text.lines().filter(|l| !l.is_empty()) {
        let Some((status, path)) = line.split_once(' ') else {
            return Err(format!("snapper status: unexpected line {line:?}"));
        };
        let good = (5..=6).contains(&status.len())
            && status.chars().all(|c| "+-ctpugxa.".contains(c))
            && path.starts_with('/')
            && normpath(path) == path;
        if !good {
            return Err(format!("snapper status: unexpected line {line:?}"));
        }
        if out.len() >= MAX_CHANGES {
            return Err(format!("more than {MAX_CHANGES} files changed"));
        }
        let kind = status.chars().next().unwrap_or('.');
        out.push(Change {
            kind,
            path: path.to_owned(),
        });
    }
    Ok(out)
}

/// `path` is `root` or inside it (both normalised, absolute).
pub fn within(path: &str, root: &Path) -> bool {
    let root = normpath(&root.to_string_lossy());
    let root = root.trim_end_matches('/');
    let path = normpath(path);
    root.is_empty() || path == root || path.starts_with(&format!("{root}/"))
}

/// A path inside a snapshot folder: never restored, never counted.
pub fn in_snapshots(path: &str) -> bool {
    path.split('/').any(|part| part == ".snapshots")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_lines() {
        let text = "c..... /home/anna/Documents/a b.txt\n+..... /home/anna/Documents/new\n\
                    -.... /home/anna/old\n....x. /home/anna/meta\n";
        let changes = parse_status(text).expect("parse");
        let kinds: Vec<char> = changes.iter().map(|c| c.kind).collect();
        assert_eq!(kinds, vec!['c', '+', '-', '.']);
        assert_eq!(changes[0].path, "/home/anna/Documents/a b.txt");
        assert_eq!(parse_status("").expect("empty"), Vec::new());
    }

    #[test]
    fn strange_status_is_refused() {
        for bad in [
            "c..... relative/path",
            "c.....",
            "zzzzzz /home/a",
            "c..... /home/a/../b",
            "c..... /home/a\nnot-a-status line",
        ] {
            assert!(parse_status(bad).is_err(), "{bad:?}");
        }
        let many: String = (0..=MAX_CHANGES)
            .map(|i| format!("+..... /home/a/{i}\n"))
            .collect();
        assert!(parse_status(&many).is_err());
    }

    #[test]
    fn paths_inside() {
        let root = Path::new("/home/anna/Documents");
        assert!(within("/home/anna/Documents", root));
        assert!(within("/home/anna/Documents/a", root));
        assert!(!within("/home/anna/Documents2/a", root));
        assert!(!within("/home/anna", root));
        assert!(in_snapshots("/home/anna/.snapshots/4/snapshot/a"));
        assert!(!in_snapshots("/home/anna/snapshots/a"));
    }

    #[test]
    fn old_mitos_snapshots_only() {
        let listing = r#"{"mitos-1000": [
            {"number": 0, "date": "", "userdata": null},
            {"number": 3, "type": "pre", "date": "2026-09-01 10:00:00", "userdata": {"mitos": "a"}},
            {"number": 4, "type": "post", "date": "2026-09-01 10:00:01", "userdata": {"mitos": "a"}},
            {"number": 5, "type": "single", "date": "2026-09-01 10:00:00", "userdata": {"other": "x"}},
            {"number": 6, "type": "pre", "date": "2026-11-09 10:00:00", "userdata": {"mitos": "b"}}
        ]}"#;
        let cutoff = cutoff_day(1_793_658_481_123, KEEP_DAYS);
        assert_eq!(cutoff, "2026-10-03");
        assert_eq!(expired(listing, "mitos-1000", &cutoff), vec![3, 4]);
        assert_eq!(expired(listing, "mitos-1001", &cutoff), Vec::<u64>::new());
        assert_eq!(
            expired("not json", "mitos-1000", &cutoff),
            Vec::<u64>::new()
        );
    }

    #[test]
    fn setup_runs_snapper_for_each_person() {
        let dir = std::env::temp_dir().join(format!("herkosd-mitos-setup-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let configs = dir.join("configs");
        std::fs::create_dir_all(&configs).expect("mkdir");
        let log = dir.join("log");
        // A stand-in for snapper: logs its arguments; /home/bohdan is "not a subvolume".
        let fake = dir.join("snapper");
        std::fs::write(
            &fake,
            format!(
                r#"#!/bin/sh
echo "$@" >> {}
case "$*" in *create-config*bohdan*) echo 'Invalid subvolume.' >&2; exit 1;; esac
"#,
                log.display()
            ),
        )
        .expect("write");
        std::fs::set_permissions(&fake, std::os::unix::fs::PermissionsExt::from_mode(0o755))
            .expect("chmod");
        std::fs::write(configs.join("mitos-1002"), "").expect("config");
        let people = crate::passwd::parse(
            "anna:x:1000:1000::/home/anna:/bin/sh\nbohdan:x:1001:1001::/home/bohdan:/bin/sh\nola:x:1002:1002::/home/ola:/bin/sh\n",
        );
        let report = setup(&people, &fake, &configs);
        assert_eq!(report.len(), 3);
        assert!(report[1].contains("no snapshots"), "{report:?}");
        let log = std::fs::read_to_string(&log).expect("log");
        let lines: Vec<&str> = log.lines().collect();
        assert_eq!(
            lines[0],
            "--no-dbus -c mitos-1000 create-config --fstype btrfs /home/anna"
        );
        assert!(lines[1].starts_with(
            "--no-dbus -c mitos-1000 set-config ALLOW_USERS=herkos anna SYNC_ACL=yes"
        ));
        assert!(lines[2].contains("create-config --fstype btrfs /home/bohdan"));
        // An existing config is only updated.
        assert!(lines[3].starts_with("--no-dbus -c mitos-1002 set-config ALLOW_USERS=herkos ola"));
        assert_eq!(lines.len(), 4);
    }

    #[test]
    fn a_command_that_hangs_is_stopped() {
        let mut c = Command::new("sleep");
        c.arg("5");
        let started = Instant::now();
        assert!(run(c, Duration::from_millis(200)).is_err());
        assert!(started.elapsed() < Duration::from_secs(3));
        let mut ok = Command::new("echo");
        ok.arg("41");
        assert_eq!(run(ok, Duration::from_secs(5)).expect("echo").trim(), "41");
        let mut fails = Command::new("sh");
        fails.args(["-c", "echo nope >&2; exit 3"]);
        let err = run(fails, Duration::from_secs(5)).expect_err("fails");
        assert!(err.contains("nope"), "{err}");
    }
}
