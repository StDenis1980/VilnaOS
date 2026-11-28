// SPDX-License-Identifier: GPL-3.0-or-later
//! Hostile input for the crash report collector `vilna-reportd` (ADR 0018):
//! a journal that someone else writes into, `systemd-coredump` metadata with
//! paths, passwords and gigantic fields, a report queue full of broken files,
//! symbolic links and thousands of entries, and an answer ("auto", "ask",
//! "never") that the person changes between the crash and the sending.
//!
//! The tests use only the public API of the crate, the way the service does,
//! with a fake transport: nothing here touches the network, `journalctl` or
//! `curl`. Every folder is a fresh temporary folder that is deleted again.
//!
//! All personal data here is made up (`anna`, `example.org`, documentation IP
//! addresses, test phone numbers).

use std::collections::HashSet;
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime};

use serde_json::{Map, Value, json};
use vilna_reportd::collector::Collector;
use vilna_reportd::consent::{self, Consent};
use vilna_reportd::control;
use vilna_reportd::coredump::{self, COREDUMP_MESSAGE_ID, Device, Found, Skip};
use vilna_reportd::device;
use vilna_reportd::journal;
use vilna_reportd::send::{Answer, Transport, answer};
use vilna_reportd::spool::{
    Added, DAY, KEEP, KEEP_IDS, MAX_SENT_PER_DAY, MAX_WAITING, Report, Spool, State,
};
use vilna_reports_protocol::{Crash, CrashWire, MAX_BODY_BYTES, MAX_FRAMES};

const ID: &str = "3f2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9b";
/// "Now" for the tests: late 2026, in seconds.
const NOW: u64 = 1_796_000_000;
/// Nothing in a test may take longer than this, however large the input.
const BUDGET: Duration = Duration::from_secs(30);

// ------------------------------------------------------------- helpers --

static COUNTER: AtomicUsize = AtomicUsize::new(0);

/// A fresh folder, deleted when the test ends.
struct TempDir(PathBuf);

impl TempDir {
    fn new(name: &str) -> TempDir {
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!(
            "vilna-hostile-client-{name}-{}-{n}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        assert!(std::fs::create_dir_all(&dir).is_ok());
        TempDir(dir)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// What the fake server saw and what it will answer next.
#[derive(Default)]
struct Server {
    answers: Mutex<Vec<Answer>>,
    posted: Mutex<Vec<(String, Vec<u8>)>>,
}

impl Server {
    fn count(&self) -> usize {
        self.posted.lock().map(|p| p.len()).unwrap_or(0)
    }

    fn bodies(&self, path: &str) -> Vec<Value> {
        self.posted
            .lock()
            .map(|p| {
                p.iter()
                    .filter(|(to, _)| to == path)
                    .filter_map(|(_, body)| serde_json::from_slice(body).ok())
                    .collect()
            })
            .unwrap_or_default()
    }

    fn will_answer(&self, answers: Vec<Answer>) {
        if let Ok(mut a) = self.answers.lock() {
            *a = answers;
        }
    }
}

struct Wire(Arc<Server>);

impl Transport for Wire {
    fn post(&self, path: &str, body: &[u8]) -> Answer {
        if let Ok(mut p) = self.0.posted.lock() {
            p.push((path.to_owned(), body.to_vec()));
        }
        let next = self
            .0
            .answers
            .lock()
            .ok()
            .and_then(|mut a| (!a.is_empty()).then(|| a.remove(0)));
        next.unwrap_or(Answer::Taken(
            json!({"status": "new", "signature": "0", "id": "x"}),
        ))
    }
}

struct Rig {
    dir: TempDir,
    c: Collector,
    server: Arc<Server>,
}

impl Rig {
    fn new(name: &str, answer: Option<Consent>) -> Rig {
        let dir = TempDir::new(name);
        if let Some(a) = answer {
            assert!(consent::write(dir.path(), a).is_ok());
        }
        let server = Arc::new(Server::default());
        let c = Collector {
            spool: open_spool(&dir.path().join("state")),
            transport: Box::new(Wire(server.clone())),
            root: dir.path().to_path_buf(),
            device: Device {
                image: Some("Vilna Blavet 0.1".into()),
                locale: Some("uk".into()),
                arch: Some("x86_64".into()),
                memory_gb: Some(15),
            },
        };
        Rig { dir, c, server }
    }

    fn reports_dir(&self) -> PathBuf {
        self.dir.path().join("state/reports")
    }

    fn state_dir(&self) -> PathBuf {
        self.dir.path().join("state")
    }

    fn answer(&self, a: Consent) {
        assert!(consent::write(self.dir.path(), a).is_ok());
    }

    /// Keep a waiting report of `program` crashing in `function`.
    fn wait(&self, program: &str, function: &str, uid: Option<u32>, found: u64) -> String {
        let r = Report::new(crash(program, function), uid, found);
        assert!(self.c.spool.add(&r, found).is_ok());
        r.crash.id
    }
}

fn open_spool(dir: &Path) -> Spool {
    let spool = Spool::open(dir);
    assert!(spool.is_ok(), "the queue did not open in {dir:?}");
    match spool {
        Ok(spool) => spool,
        Err(_) => std::process::abort(),
    }
}

fn new_id() -> String {
    device::new_id().unwrap_or_else(|| ID.to_owned())
}

fn crash(program: &str, function: &str) -> Crash {
    let wire: Result<CrashWire, _> = serde_json::from_value(json!({
        "schema": 1, "id": new_id(),
        "program": {"name": program, "version": "1.0", "packaging": "rpm"},
        "kind": "signal", "signal": "SIGSEGV",
        "frames": [{"module": "libx.so", "offset": "0x10", "function": function}]
    }));
    assert!(wire.is_ok());
    let crash = Crash::from_wire(wire.unwrap_or_default());
    assert!(crash.is_ok(), "{crash:?}");
    match crash {
        Ok(crash) => crash,
        Err(_) => std::process::abort(),
    }
}

const STACK: &str = "Stack trace of thread 1:\n#0  0x00007f0f8a0ac884 f (libx.so + 0x10)\n";

/// A good coredump entry with `extra` fields put over it.
fn entry(extra: Value) -> Map<String, Value> {
    let mut base = json!({
        "MESSAGE_ID": COREDUMP_MESSAGE_ID,
        "MESSAGE": STACK,
        "COREDUMP_SIGNAL_NAME": "SIGSEGV",
        "COREDUMP_UID": "1000",
        "COREDUMP_EXE": "/usr/bin/app",
        "COREDUMP_PACKAGE_NAME": "app",
        "COREDUMP_PACKAGE_VERSION": "1.0-1",
        "COREDUMP_TIMESTAMP": "1796000000000000",
    });
    if let (Some(b), Some(e)) = (base.as_object_mut(), extra.as_object()) {
        for (k, v) in e {
            b.insert(k.clone(), v.clone());
        }
    }
    base.as_object().cloned().unwrap_or_default()
}

fn message(frames: &[&str]) -> String {
    let mut m = String::from("Stack trace of thread 1:\n");
    for (i, f) in frames.iter().enumerate() {
        m.push_str(&format!("#{i}  0x00007f0f8a0ac884 {f}\n"));
    }
    m
}

fn report_of(e: &Map<String, Value>) -> Result<Found, Skip> {
    coredump::report(e, &Device::default(), ID)
}

/// A report that is expected to exist.
fn kept(e: &Map<String, Value>) -> Found {
    let got = report_of(e);
    assert!(got.is_ok(), "{got:?}");
    match got {
        Ok(found) => found,
        Err(_) => std::process::abort(),
    }
}

/// Everything a report says about itself, as text: what is sent and what is
/// shown to the person.
fn text_of(found: &Found) -> String {
    format!("{}\n{:?}", found.crash.to_wire(), found)
}

fn functions(found: &Found) -> Vec<String> {
    found
        .crash
        .frames
        .iter()
        .filter_map(|f| f.function.clone())
        .collect()
}

fn write_raw(dir: &Path, name: &str, bytes: &[u8]) {
    assert!(std::fs::write(dir.join(name), bytes).is_ok());
}

fn mode(path: &Path) -> u32 {
    std::fs::metadata(path)
        .map(|m| m.permissions().mode() & 0o777)
        .unwrap_or(0)
}

fn my_uid() -> u32 {
    std::fs::metadata("/proc/self")
        .map(|m| m.uid())
        .unwrap_or(65534)
}

fn within_budget(started: Instant) {
    assert!(
        started.elapsed() < BUDGET,
        "took {:?}, more than {BUDGET:?}",
        started.elapsed()
    );
}

fn file_names(dir: &Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(dir)
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    names
}

// -------------------------------------------- journal entry -> report --

/// Every field that names the person, the computer, a path or an argument.
/// Each gets a marker; none of the markers may come out in a report.
const PERSONAL_FIELDS: &[&str] = &[
    "COREDUMP_CMDLINE",
    "COREDUMP_ENVIRON",
    "COREDUMP_CWD",
    "COREDUMP_ROOT",
    "COREDUMP_HOSTNAME",
    "COREDUMP_USER_UNIT",
    "COREDUMP_UNIT",
    "COREDUMP_SLICE",
    "COREDUMP_SESSION",
    "COREDUMP_OWNER_UID",
    "COREDUMP_OPEN_FDS",
    "COREDUMP_PROC_STATUS",
    "COREDUMP_PROC_MAPS",
    "COREDUMP_PROC_LIMITS",
    "COREDUMP_PROC_CGROUP",
    "COREDUMP_PROC_MOUNTINFO",
    "COREDUMP_FILENAME",
    "COREDUMP_CONTAINER_CMDLINE",
    "_HOSTNAME",
    "_MACHINE_ID",
    "_BOOT_ID",
    "_CMDLINE",
    "_EXE",
    "_SYSTEMD_CGROUP",
    "_SYSTEMD_UNIT",
    "_SELINUX_CONTEXT",
    "USER",
    "HOME",
    "SYSLOG_IDENTIFIER",
];

#[test]
fn personal_fields_of_the_entry_never_reach_the_report() {
    let mut extra = Map::new();
    for name in PERSONAL_FIELDS {
        extra.insert(
            (*name).to_owned(),
            json!(format!(
                "CANARY-{name} anna --password=hunter2 /home/anna/secret"
            )),
        );
    }
    let text = text_of(&kept(&entry(Value::Object(extra))));
    for forbidden in ["CANARY", "hunter2", "/home/anna", "anna "] {
        assert!(!text.contains(forbidden), "{forbidden} leaked: {text}");
    }
}

#[test]
fn the_program_path_never_reaches_the_report_only_its_file_name() {
    for exe in [
        "/home/anna/bin/notes",
        "/var/home/anna/.local/bin/notes",
        "/home/Anna Kowalska/bin/notes",
        "/run/media/anna/usb/notes",
        "/tmp/anna@example.org/notes",
        "/home/anna//bin///notes",
        "notes",
    ] {
        let mut e = entry(json!({"COREDUMP_EXE": exe}));
        e.remove("COREDUMP_PACKAGE_NAME");
        let found = kept(&e);
        assert_eq!(found.crash.program, "notes", "{exe}");
        let text = text_of(&found);
        assert!(!text.contains("anna") && !text.contains("Anna"), "{exe}");
        assert!(
            !text.contains("/home") && !text.contains("/run/media"),
            "{exe}"
        );
    }
}

#[test]
fn without_a_usable_name_there_is_no_report() {
    let long = "a".repeat(129);
    for name in [
        "",
        " ",
        "/home/anna/app",
        "anna@example.org",
        "a b",
        "-app",
        ".app",
        "app\0",
        "додаток",
        "app;rm",
        "../app",
        "\u{202e}ppa",
        long.as_str(),
    ] {
        let mut e = entry(json!({"COREDUMP_PACKAGE_NAME": name}));
        e.remove("COREDUMP_EXE");
        let got = report_of(&e);
        assert!(matches!(got, Err(Skip::Invalid(_))), "{name:?}: {got:?}");
    }
    // A folder only, a trailing slash and no name at all.
    for exe in ["/usr/bin/", "/", ""] {
        let mut e = entry(json!({"COREDUMP_EXE": exe}));
        e.remove("COREDUMP_PACKAGE_NAME");
        assert!(matches!(report_of(&e), Err(Skip::Invalid(_))), "{exe:?}");
    }
    let mut e = entry(json!({}));
    for key in ["COREDUMP_EXE", "COREDUMP_PACKAGE_NAME"] {
        e.remove(key);
    }
    assert!(matches!(report_of(&e), Err(Skip::Invalid(_))));
}

#[test]
fn the_words_of_a_refusal_do_not_repeat_the_hostile_value() {
    let rig = Rig::new("refusal-words", None);
    for name in [
        "/home/anna/secret-app",
        "anna@example.org",
        "tel+380501234567 anna",
    ] {
        let mut e = entry(json!({"COREDUMP_PACKAGE_NAME": name}));
        e.remove("COREDUMP_EXE");
        let got = rig.c.found(&e, NOW);
        assert!(got.is_err(), "{name}: kept a report");
        let why = got.err().unwrap_or_default();
        assert!(
            !why.contains("anna") && !why.contains("380501234567"),
            "{why}"
        );
    }
}

#[test]
fn a_flatpak_id_comes_only_from_the_last_part_of_the_cgroup() {
    let id = |cgroup: &str| coredump::flatpak_id(cgroup);
    assert_eq!(
        id("/user.slice/app.slice/app-flatpak-org.gnome.Nautilus-12345.scope").as_deref(),
        Some("org.gnome.Nautilus")
    );
    assert_eq!(id("app-flatpak-a.b-1.scope").as_deref(), Some("a.b"));
    for hostile in [
        "",
        "/",
        "app-flatpak-.scope",
        "app-flatpak-a.b-x1.scope",
        "app-flatpak-a.b-1.service",
        "/app-flatpak-evil/../x-1.scope",
        "/a/app-flatpak-a.b-1.scope/",
        "xapp-flatpak-a.b-1.scope",
        "app-flatpak-a.b.scope",
    ] {
        assert_eq!(id(hostile), None, "{hostile:?}");
    }
    // A Flatpak id with a space or a path is no program name: no report.
    for cgroup in [
        "/a/app-flatpak-a b-12.scope",
        "/a/app-flatpak-..%2f..-12.scope",
    ] {
        let got = report_of(&entry(json!({"COREDUMP_CGROUP": cgroup})));
        assert!(matches!(got, Err(Skip::Invalid(_))), "{cgroup:?}: {got:?}");
    }
    let ok = kept(&entry(json!({
        "COREDUMP_CGROUP": "/a/app-flatpak-org.gnome.Calculator-7.scope"
    })));
    assert_eq!(ok.crash.program, "org.gnome.Calculator");
}

#[test]
fn a_journal_field_is_read_as_the_journal_writes_it() {
    let get = |v: Value| {
        let mut m = Map::new();
        m.insert("F".into(), v);
        coredump::field(&m, "F")
    };
    assert_eq!(get(json!("text")).as_deref(), Some("text"));
    assert_eq!(get(json!(["first", "second"])).as_deref(), Some("first"));
    assert_eq!(get(json!([104, 105])).as_deref(), Some("hi"));
    assert_eq!(get(json!([255, 104])).as_deref(), Some("\u{fffd}h"));
    for odd in [
        json!(null),
        json!(5),
        json!(true),
        json!({"a": "b"}),
        json!([[1], [2]]),
        json!([null, "x"]),
        json!([{"a": 1}, "x"]),
    ] {
        assert_eq!(get(odd.clone()), None, "{odd}");
    }
    // Bytes that are no bytes are dropped, never wrapped round into letters.
    assert_eq!(
        get(json!([300, 104, 4_294_967_393_u64])).as_deref(),
        Some("h")
    );
    let m = Map::new();
    assert_eq!(coredump::field(&m, "MISSING"), None);
}

#[test]
fn only_the_coredump_message_id_makes_a_crash() {
    let with = |v: Value| {
        let mut e = entry(json!({}));
        e.insert("MESSAGE_ID".into(), v);
        report_of(&e)
    };
    assert!(with(json!(COREDUMP_MESSAGE_ID)).is_ok());
    assert!(with(json!([COREDUMP_MESSAGE_ID, "other"])).is_ok());
    for other in [
        json!("other"),
        json!(COREDUMP_MESSAGE_ID.to_ascii_uppercase()),
        json!(format!("{COREDUMP_MESSAGE_ID} ")),
        json!(["other", COREDUMP_MESSAGE_ID]),
        json!(5),
        json!(null),
        json!([]),
    ] {
        assert!(
            matches!(with(other.clone()), Err(Skip::NotACrash)),
            "{other}"
        );
    }
    let mut none = entry(json!({}));
    none.remove("MESSAGE_ID");
    assert!(matches!(report_of(&none), Err(Skip::NotACrash)));
}

#[test]
fn programs_that_report_their_own_crashes_are_never_reported_here() {
    let own = [
        "io.github.stdenis1980.tintapdf",
        "io.github.stdenis1980.tintatexto",
        "io.github.stdenis1980.tintacalculo",
        "io.github.stdenis1980.tintapresenta",
        "io.github.stdenis1980.ibercorreo",
        "tintapdf",
        "tintatexto",
        "tintacalculo",
        "tintapresenta",
        "ibercorreo",
        "vilna-reportd",
    ];
    for name in own {
        let cgroup = format!("/a/app-flatpak-{name}-9.scope");
        let mut by_exe = entry(json!({"COREDUMP_EXE": format!("/home/anna/bin/{name}")}));
        by_exe.remove("COREDUMP_PACKAGE_NAME");
        let mut by_comm = entry(json!({"COREDUMP_COMM": name}));
        by_comm.remove("COREDUMP_PACKAGE_NAME");
        by_comm.remove("COREDUMP_EXE");
        let routes = [
            entry(json!({"COREDUMP_CGROUP": cgroup})),
            entry(json!({"COREDUMP_PACKAGE_NAME": name})),
            by_exe,
            by_comm,
        ];
        for e in routes {
            assert!(
                matches!(report_of(&e), Err(Skip::OwnReporter(_))),
                "{name}: {e:?}"
            );
        }
    }
    // The skip does not leave a report behind.
    let rig = Rig::new("own", None);
    let e = entry(json!({"COREDUMP_PACKAGE_NAME": "tintapdf"}));
    assert_eq!(rig.c.found(&e, NOW), Ok(None));
    assert!(rig.c.spool.reports().is_empty());
}

#[test]
fn the_signal_is_a_signal_name_or_nothing() {
    let signal = |name: Option<&str>, number: Option<&str>| {
        let mut e = entry(json!({}));
        e.remove("COREDUMP_SIGNAL_NAME");
        if let Some(n) = name {
            e.insert("COREDUMP_SIGNAL_NAME".into(), json!(n));
        }
        if let Some(n) = number {
            e.insert("COREDUMP_SIGNAL".into(), json!(n));
        }
        report_of(&e).ok().and_then(|f| f.crash.signal)
    };
    assert_eq!(signal(Some("SIGABRT"), None).as_deref(), Some("SIGABRT"));
    assert_eq!(signal(None, Some("11")).as_deref(), Some("SIGSEGV"));
    assert_eq!(signal(None, Some(" 6 ")).as_deref(), Some("SIGABRT"));
    for hostile in [
        "SIGSEGV\nSIGABRT",
        "sigsegv",
        "SIG",
        "SIGTOOLONGNAME123",
        "SIG SEGV",
        "<script>",
        "/home/anna",
        "",
    ] {
        assert_eq!(signal(Some(hostile), None), None, "{hostile:?}");
    }
    for number in [
        "",
        "-11",
        "1e1",
        "99999999999999999999",
        "abc",
        "0",
        "9999",
        "11;6",
    ] {
        assert_eq!(signal(None, Some(number)), None, "{number:?}");
    }
}

#[test]
fn whose_program_it_was_is_a_user_number_or_unknown() {
    let uid = |v: Value| {
        let mut e = entry(json!({}));
        e.insert("COREDUMP_UID".into(), v);
        report_of(&e).ok().and_then(|f| f.uid)
    };
    assert_eq!(uid(json!("1000")), Some(1000));
    assert_eq!(uid(json!(" 1000\n")), Some(1000));
    assert_eq!(uid(json!("0")), Some(0));
    assert_eq!(uid(json!("4294967295")), Some(u32::MAX));
    for hostile in ["", "abc", "-1", "4294967296", "1e3", "0x3e8", "1000;0", "٣"] {
        assert_eq!(uid(json!(hostile)), None, "{hostile:?}");
    }
    assert_eq!(uid(json!(1000)), None);
    assert_eq!(uid(json!(null)), None);
}

#[test]
fn the_time_of_a_crash_is_never_in_the_future() {
    let rig = Rig::new("times", None);
    let mut n = 0;
    let mut found_at = |timestamp: Option<Value>, realtime: Option<Value>| {
        // A different place each time, so no crash is taken for a duplicate.
        n += 1;
        let place = format!("place{n} (libx.so + 0x10)");
        let mut e = entry(json!({"MESSAGE": message(&[place.as_str()])}));
        e.remove("COREDUMP_TIMESTAMP");
        if let Some(t) = timestamp {
            e.insert("COREDUMP_TIMESTAMP".into(), t);
        }
        if let Some(t) = realtime {
            e.insert("__REALTIME_TIMESTAMP".into(), t);
        }
        let id = rig.c.found(&e, NOW);
        assert!(matches!(id, Ok(Some(_))), "{id:?}");
        let id = id.ok().flatten().unwrap_or_default();
        rig.c.spool.get(&id).map(|r| r.found)
    };
    let us = |s: u64| json!((s * 1_000_000).to_string());
    assert_eq!(found_at(Some(us(NOW - 100)), None), Some(NOW - 100));
    assert_eq!(found_at(Some(us(NOW + DAY)), None), Some(NOW));
    assert_eq!(
        found_at(Some(json!("18446744073709551615")), None),
        Some(NOW)
    );
    assert_eq!(found_at(Some(json!("0")), None), Some(0));
    for junk in ["", "abc", "-5", "1e9", "99999999999999999999999", "12 34"] {
        assert_eq!(found_at(Some(json!(junk)), None), Some(NOW), "{junk:?}");
    }
    // The journal's own time is used when the dump has none.
    assert_eq!(found_at(None, Some(us(NOW - 5))), Some(NOW - 5));
    assert_eq!(found_at(None, None), Some(NOW));
}

#[test]
fn the_same_journal_entry_twice_is_one_report() {
    let rig = Rig::new("twice", None);
    let e = entry(json!({}));
    assert!(matches!(rig.c.found(&e, NOW), Ok(Some(_))));
    assert_eq!(rig.c.found(&e, NOW + 1), Ok(None));
    assert_eq!(rig.c.spool.reports().len(), 1);
}

#[test]
fn only_the_stack_of_the_crashed_thread_is_read_and_at_most_max_frames() {
    let mut m = String::from("Process 1 (app) of user 1000 dumped core.\n\n");
    m.push_str("Stack trace of thread 7:\n");
    for i in 0..(MAX_FRAMES * 3) {
        m.push_str(&format!("#{i}  0x1 first{i} (libx.so + 0x{i:x})\n"));
    }
    m.push_str("\nStack trace of thread 8:\n#0  0x1 second (liby.so + 0x1)\n");
    let found = kept(&entry(json!({"MESSAGE": m})));
    assert_eq!(found.crash.frames.len(), MAX_FRAMES);
    assert!(functions(&found).iter().all(|f| f.starts_with("first")));
    // A line that only looks like the next thread cuts the stack: nothing
    // after it is read, and nothing before it is lost.
    let m = "Stack trace of thread 1:\n#0  0x1 a (libx.so + 0x1)\nStack trace of thread 2:\n#0  0x1 b (libx.so + 0x2)\n";
    assert_eq!(functions(&kept(&entry(json!({"MESSAGE": m})))), ["a"]);
}

#[test]
fn broken_stack_lines_are_skipped_without_a_crash() {
    let lines = [
        "#",
        "# ",
        "#0",
        "#0 0x1",
        "#0 0x1 f",
        "#0 0x1 f (",
        "#0 0x1 f )",
        "#0 0x1 f ()",
        "#0 0x1 f (libx.so)",
        "#0 0x1 f (libx.so + )",
        "#0 0x1 (libx.so + 0x1)",
        "#0 0x1 f (libx.so + 0xZZ)",
        "#0 0x1 f (+ 0x1)",
        "#0 0x1 ((((( + )))))",
        "#0 0x1 f (libx.so + 0x1) trailing (liby.so + 0x2)",
        "#0 0x1 ф (лібх.so + 0x1)",
        "#0 0x1 \u{202e}f\u{0}g (libx.so + 0x1)",
        "#0\t0x1\tf\t(libx.so\t+\t0x1)",
        "#-1 0x1 f (libx.so + 0x1)",
    ];
    for line in lines {
        let m = format!("Stack trace of thread 1:\n{line}\n#1 0x2 ok (libx.so + 0x2)\n");
        let found = report_of(&entry(json!({"MESSAGE": m})));
        assert!(found.is_ok(), "{line:?}: {found:?}");
    }
}

#[test]
fn text_in_stack_lines_is_scrubbed_before_it_is_kept() {
    let secrets = [
        ("anna@example.org", "anna@example.org"),
        ("/home/anna/project/main", "anna"),
        ("/var/home/anna/x", "anna"),
        ("C:\\Users\\anna\\x", "anna"),
        ("192.0.2.17", "192.0.2.17"),
        ("2001:db8::17", "2001:db8"),
        ("00:00:5e:00:53:af", "00:00:5e"),
        ("+380501234567", "380501234567"),
        ("4111111111111111", "4111111111111111"),
        ("4111-1111-1111-1111", "4111-1111"),
        ("DE89370400440532013000", "DE8937"),
        ("987654321012", "987654321012"),
        ("a\u{200b}nna@\u{200b}example.org", "example.org"),
        ("anna%40example.org", "example.org"),
        ("~/Documents/taxes", "Documents"),
        ("https://example.org/anna", "example.org"),
    ];
    for (secret, marker) in secrets {
        let m = message(&[
            &format!("id={secret} (libx.so + 0x10)"),
            &format!("{secret} (libx.so + 0x11)"),
        ]);
        let text = text_of(&kept(&entry(json!({"MESSAGE": m}))));
        assert!(!text.contains(marker), "{secret} left {marker} in {text}");
    }
}

#[test]
fn a_function_name_is_one_short_line() {
    let long = "ф".repeat(5000);
    let ugly = format!("a\u{202e}b\u{2066}c\u{2028}d\u{2029}e\u{7}f\u{0}g\u{85}h\ti{long}");
    let m = message(&[&format!("{ugly} (libx.so + 0x1)")]);
    let names = functions(&kept(&entry(json!({"MESSAGE": m}))));
    assert_eq!(names.len(), 1);
    for name in names {
        assert!(name.chars().count() <= 200, "{}", name.chars().count());
        for bad in [
            '\n', '\r', '\0', '\u{202e}', '\u{2066}', '\u{2028}', '\u{2029}', '\u{7}', '\u{85}',
            '\t',
        ] {
            assert!(!name.contains(bad), "{bad:?} in {name:?}");
        }
    }
}

#[test]
fn a_module_is_a_file_name_never_a_folder() {
    let cases = [
        (
            "/home/anna/.local/lib/libsecret-1.so.0",
            Some("libsecret-1.so.0"),
        ),
        ("/usr/lib64/libc.so.6", Some("libc.so.6")),
        ("libx.so", Some("libx.so")),
        // Not a file name at all: the module is dropped, never kept as it is.
        ("C:\\Users\\anna\\x.dll", None),
        ("anna@example.org", None),
        ("/home/anna/", None),
        ("lib x.so", None),
        ("lib\u{202e}x.so", None),
    ];
    for (module, expected) in cases {
        let m = message(&[&format!("f ({module} + 0x10)")]);
        let found = kept(&entry(json!({"MESSAGE": m})));
        let got = found.crash.frames.first().and_then(|f| f.module.as_deref());
        assert_eq!(got, expected, "{module}");
        assert!(!text_of(&found).contains("anna"), "{module}");
    }
}

#[test]
fn build_ids_come_from_the_metadata_and_the_message_in_file_names() {
    let package =
        r#"{"/usr/lib/libc.so.6":{"buildId":"AABBCCDD11"},"libx.so":{"buildId":"0011223344"}}"#;
    let msg = "Module /home/anna/lib/liba.so with build-id 0A0B0C0D0E\nModule libx.so with build-id ffffffffff\n";
    let ids = coredump::build_ids(Some(package), msg);
    assert_eq!(ids.get("libc.so.6").map(String::as_str), Some("aabbccdd11"));
    assert_eq!(ids.get("liba.so").map(String::as_str), Some("0a0b0c0d0e"));
    // The package metadata wins over the message for the same module.
    assert_eq!(ids.get("libx.so").map(String::as_str), Some("0011223344"));
    assert!(ids.keys().all(|k| !k.contains('/')));
    for broken in [
        "",
        "null",
        "[]",
        "[1,2]",
        "\"text\"",
        "5",
        "{",
        "{\"a\":",
        r#"{"a":5}"#,
        r#"{"a":{"buildId":5}}"#,
        r#"{"a":{"buildId":null}}"#,
        r#"{"a":[{"buildId":"aa"}]}"#,
        "\u{feff}{}",
    ] {
        let ids = coredump::build_ids(Some(broken), "");
        assert!(ids.is_empty(), "{broken:?}: {ids:?}");
    }
    assert!(coredump::build_ids(None, "").is_empty());
    for line in [
        "Module",
        "Module  with build-id ",
        "Module x with build-id",
        "module x with build-id aa",
    ] {
        assert!(coredump::build_ids(None, line).len() <= 1, "{line:?}");
    }
}

#[test]
fn a_build_id_that_is_no_hex_number_is_not_kept() {
    for (id, is_kept) in [
        ("0A1B2C3D4E5F6071", true),
        ("0a1b2c3d", true),
        ("0a1b2c3", false),
        ("0a1b2c3d;rm -rf", false),
        ("zzzzzzzzzzzz", false),
        ("anna@example.org", false),
        ("0a1b2c3d\u{202e}", false),
    ] {
        let m = format!("Module libx.so with build-id {id}\n{STACK}");
        let found = kept(&entry(json!({"MESSAGE": m})));
        let got = found.crash.frames.first().and_then(|f| f.build_id.clone());
        assert_eq!(got.is_some(), is_kept, "{id}: {got:?}");
        if let Some(got) = got {
            assert_eq!(got, id.to_ascii_lowercase());
        }
    }
}

#[test]
fn huge_entries_are_read_in_bounded_time_and_give_a_bounded_report() {
    let started = Instant::now();
    // Hundreds of thousands of module lines, a megabyte line, then the stack.
    let mut m = String::new();
    for i in 0..300_000 {
        m.push_str(&format!("Module m{i}.so with build-id aabbccdd{i:04x}\n"));
    }
    m.push_str(&"x".repeat(1 << 20));
    m.push('\n');
    m.push_str(STACK);
    assert!(kept(&entry(json!({"MESSAGE": m}))).crash.frames.len() <= MAX_FRAMES);
    // Fields of a megabyte each.
    let big = "z".repeat(1 << 20);
    let found = kept(&entry(json!({
        "COREDUMP_CMDLINE": big, "COREDUMP_ENVIRON": big, "COREDUMP_COMM": big,
        "COREDUMP_PACKAGE_JSON": big, "COREDUMP_PACKAGE_VERSION": big,
    })));
    // The version is too long to keep; the report is kept without it.
    assert!(found.crash.version.is_none());
    // A package name or program name of a megabyte is no name.
    let mut e = entry(json!({"COREDUMP_PACKAGE_NAME": big}));
    e.remove("COREDUMP_EXE");
    assert!(matches!(report_of(&e), Err(Skip::Invalid(_))));
    // A stack with nothing but junk lines, a million of them.
    let junk = format!(
        "Stack trace of thread 1:\n{}",
        "no frame here\n".repeat(1_000_000)
    );
    assert!(report_of(&entry(json!({"MESSAGE": junk}))).is_ok());
    // Metadata nested a hundred thousand levels deep is refused by the parser.
    let deep = "[".repeat(100_000);
    assert!(report_of(&entry(json!({"COREDUMP_PACKAGE_JSON": deep}))).is_ok());
    let deep = format!("{}1{}", "{\"a\":".repeat(100_000), "}".repeat(100_000));
    assert!(report_of(&entry(json!({"COREDUMP_PACKAGE_JSON": deep}))).is_ok());
    within_budget(started);
}

#[test]
fn the_longest_honest_report_still_fits_the_server_limit() {
    let mut m = String::from("Stack trace of thread 1:\n");
    let module = format!("{}.so", "m".repeat(120));
    for i in 0..MAX_FRAMES {
        let function = format!("f{i}{}", "n".repeat(190));
        m.push_str(&format!(
            "#{i}  0x1 {function} ({module} + 0xffffffffffffffff)\n"
        ));
    }
    m.push_str(&format!(
        "Module {module} with build-id {}\n",
        "a".repeat(128)
    ));
    let found = kept(&entry(json!({"MESSAGE": m})));
    assert_eq!(found.crash.frames.len(), MAX_FRAMES);
    let body = serde_json::to_vec(&found.crash.to_wire()).unwrap_or_default();
    assert!(
        body.len() <= MAX_BODY_BYTES,
        "{} bytes, the server takes {MAX_BODY_BYTES}",
        body.len()
    );
}

#[test]
fn what_the_device_sends_is_what_the_server_stores() {
    let corpus = [
        "a@b.c d",
        "<path> x",
        "x <email> y <ip> <n>",
        "fe80:: ::1",
        "1234567 89",
        "+380 50 123 45 67",
        "~/a/b",
        "a|b\\c",
        "std::vec::Vec<T>::push",
        "\u{200b}a\u{200b}@\u{200b}b.com",
        "foo.rs:12:3",
        "/src/main.rs:12",
        "www.example.org/y",
        "  spaced   out  ",
        "tab\there",
        "ab\u{0301}",
        "<<<<>>>>",
        "'\"`$(rm -rf /)`",
    ];
    for function in corpus {
        let m = message(&[&format!("{function} (libx.so + 0x10)")]);
        let Ok(found) = report_of(&entry(json!({"MESSAGE": m}))) else {
            continue;
        };
        let wire: Result<CrashWire, _> = serde_json::from_value(found.crash.to_wire());
        assert!(wire.is_ok(), "{function:?}");
        let again = Crash::from_wire(wire.unwrap_or_default());
        assert_eq!(again.as_ref().ok(), Some(&found.crash), "{function:?}");
    }
}

#[test]
fn what_the_device_says_about_itself_is_checked_like_everything_else() {
    let report = |d: Device| coredump::report(&entry(json!({})), &d, ID).map(|f| f.crash);
    let c = report(Device {
        image: Some("Vilna Blavet 0.1".into()),
        locale: Some("uk".into()),
        arch: Some("x86_64".into()),
        memory_gb: Some(15),
    });
    assert!(matches!(&c, Ok(c) if c.memory_gb == Some(16) && c.locale.as_deref() == Some("uk")));
    let c = report(Device {
        image: Some("Vilna\nanna@example.org".into()),
        locale: Some("uk_UA\n/home/anna".into()),
        arch: Some("x86 64".into()),
        memory_gb: Some(u32::MAX),
    });
    assert!(
        matches!(&c, Ok(c) if c.image.is_none() && c.locale.is_none()
            && c.arch.is_none() && c.memory_gb.is_none()),
        "{c:?}"
    );
    let c = report(Device {
        memory_gb: Some(0),
        ..Device::default()
    });
    assert!(matches!(&c, Ok(c) if c.memory_gb.is_none()));
    let c = report(Device {
        memory_gb: Some(4096),
        ..Device::default()
    });
    assert!(matches!(&c, Ok(c) if c.memory_gb == Some(4096)));
}

#[test]
fn the_device_files_are_read_with_care() {
    assert_eq!(
        device::image("NAME=\"Vilna\"\nVERSION_ID=0.1\n").as_deref(),
        Some("Vilna 0.1")
    );
    assert_eq!(
        device::image("NAME=Other\nVILNA_IMAGE='Vilna Blavet'\nVERSION_ID=9").as_deref(),
        Some("Vilna Blavet")
    );
    assert_eq!(device::image("no equals sign\n\n=\n"), None);
    assert_eq!(device::image(""), None);
    assert_eq!(
        device::language("LANG=uk_UA.UTF-8\n").as_deref(),
        Some("uk")
    );
    assert_eq!(device::language("LANG=\"pl_PL\"").as_deref(), Some("pl"));
    assert_eq!(
        device::language("# LANG=uk\nLANG=de@euro").as_deref(),
        Some("de")
    );
    for hostile in [
        "",
        "LANG=C",
        "LANG=",
        "LANG=UK",
        "LANG=u",
        "LANG=ukrainian",
        "LANG=../x",
        "LANG=ук",
    ] {
        assert_eq!(device::language(hostile), None, "{hostile:?}");
    }
    assert_eq!(device::memory_gb("MemTotal: 16384000 kB\n"), Some(16));
    assert_eq!(device::memory_gb("MemTotal:  1 kB"), Some(1));
    for hostile in [
        "",
        "MemTotal:",
        "MemTotal: kB",
        "MemTotal: 0 kB",
        "MemTotal: -5 kB",
        "MemTotal: 99999999999999999999 kB",
        "MemTotal: 4611686018427387904 kB",
        "MemFree: 5 kB",
        "MemTotal 5 kB",
    ] {
        assert_eq!(device::memory_gb(hostile), None, "{hostile:?}");
    }
    // A computer with no such files: only the architecture is known.
    let nothing = TempDir::new("device");
    let d = device::this_device(nothing.path());
    assert!(d.image.is_none() && d.locale.is_none() && d.memory_gb.is_none());
    assert!(d.arch.is_some());
}

#[test]
fn report_ids_are_random_version_4_uuids_and_do_not_repeat() {
    let mut seen = HashSet::new();
    for _ in 0..5000 {
        let id = device::new_id().unwrap_or_default();
        assert_eq!(
            vilna_reports_protocol::report_id(&id).as_deref(),
            Some(id.as_str())
        );
        assert!(seen.insert(id));
    }
}

// ------------------------------------------------------------- journal --

#[test]
fn journal_lines_that_are_not_objects_are_skipped() {
    let text = [
        "",
        "not json",
        "[1,2]",
        "\"text\"",
        "5",
        "null",
        "{",
        "{\"a\":",
        "{\"A\":\"b\"}",
        "{\"A\":\"b\"} trailing",
        "\u{feff}{\"A\":\"b\"}",
        "{\"B\":1}\r",
        "{\"C\":\"\\u0000\"}",
    ]
    .join("\n");
    let found = journal::entries(text.as_bytes());
    let keys: Vec<String> = found.iter().flat_map(|e| e.keys().cloned()).collect();
    assert_eq!(keys, ["A", "B", "C"]);
}

#[test]
fn at_most_max_entries_are_taken_however_much_junk_comes_between() {
    let mut text = String::new();
    for i in 0..5000 {
        text.push_str("junk\n");
        text.push_str(&format!("{{\"N\":{i}}}\n"));
    }
    let found = journal::entries(text.as_bytes());
    assert_eq!(found.len(), journal::MAX_ENTRIES);
    // The first ones, in order: the rest waits for the next round.
    assert_eq!(found.first().and_then(|e| e.get("N")), Some(&json!(0)));
    assert_eq!(
        found.last().and_then(|e| e.get("N")),
        Some(&json!(journal::MAX_ENTRIES - 1))
    );
}

#[test]
fn a_line_of_megabytes_does_not_hide_the_entries_after_it() {
    let mut text = String::from("{\"A\":\"");
    text.push_str(&"x".repeat(3 << 20));
    text.push_str("\"}\n{\"B\":\"c\"}\n{\"C\":\"d\"}\n");
    let started = Instant::now();
    let found = journal::entries(text.as_bytes());
    within_budget(started);
    let keys: Vec<String> = found.iter().flat_map(|e| e.keys().cloned()).collect();
    assert_eq!(keys, ["B", "C"]);
}

#[test]
fn deeply_nested_lines_are_refused_without_a_stack_overflow() {
    let mut text = "[".repeat(1 << 20);
    text.push('\n');
    text.push_str(&"{\"a\":".repeat(150_000));
    text.push('\n');
    text.push_str("{\"ok\":true}\n");
    let found = journal::entries(text.as_bytes());
    assert_eq!(found.len(), 1);
}

/// Hands out its bytes in pieces of `step`, then fails.
struct Flaky {
    data: Vec<u8>,
    at: usize,
    step: usize,
}

impl Read for Flaky {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        if self.at >= self.data.len() {
            return Err(std::io::Error::from(std::io::ErrorKind::BrokenPipe));
        }
        let n = self.step.min(buf.len()).min(self.data.len() - self.at);
        buf[..n].copy_from_slice(&self.data[self.at..self.at + n]);
        self.at += n;
        Ok(n)
    }
}

#[test]
fn a_journal_that_stops_or_trickles_gives_what_was_read() {
    let data = b"{\"A\":1}\n{\"B\":2}\n{\"C\":".to_vec();
    for step in [1, 2, 7, 4096] {
        let found = journal::entries(Flaky {
            data: data.clone(),
            at: 0,
            step,
        });
        assert_eq!(found.len(), 2, "step {step}");
    }
    assert!(journal::entries(&b""[..]).is_empty());
}

// --------------------------------------------------------------- spool --

#[test]
fn thousands_of_files_in_the_queue_are_cut_to_the_waiting_limit() {
    let rig = Rig::new("thousands", None);
    for i in 0..3000u64 {
        let r = Report::new(crash("gedit", &format!("f{i}")), Some(1000), 1000 + i);
        write_raw(
            &rig.reports_dir(),
            &format!("{}.json", r.crash.id),
            &serde_json::to_vec(&r).unwrap_or_default(),
        );
    }
    let started = Instant::now();
    assert_eq!(rig.c.spool.reports().len(), 3000);
    let newest = Report::new(crash("gedit", "newest"), Some(1000), 5000);
    assert_eq!(rig.c.spool.add(&newest, 5000).ok(), Some(Added::Added));
    let left = rig.c.spool.reports();
    within_budget(started);
    assert!(left.len() <= MAX_WAITING, "{}", left.len());
    assert!(left.iter().any(|r| r.crash.id == newest.crash.id));
    // Newest first, and the ones that stayed are the newest ones.
    assert!(left.windows(2).all(|w| w[0].found >= w[1].found));
    let oldest_allowed = 1000 + 3000 - MAX_WAITING as u64;
    assert!(
        left.iter()
            .all(|r| r.found >= oldest_allowed || r.found == 5000)
    );
}

#[test]
fn two_hundred_different_crashes_leave_fifty_waiting_and_the_newest() {
    let rig = Rig::new("two-hundred", None);
    let mut ids = Vec::new();
    for i in 0..200u64 {
        ids.push(rig.wait("gedit", &format!("f{i}"), Some(1000), 100 + i));
    }
    assert_eq!(rig.c.spool.reports().len(), MAX_WAITING);
    for id in &ids[ids.len() - MAX_WAITING..] {
        assert!(rig.c.spool.get(id).is_some());
    }
    for id in &ids[..ids.len() - MAX_WAITING] {
        assert!(rig.c.spool.get(id).is_none());
    }
}

#[test]
fn reports_that_were_sent_do_not_count_against_the_waiting_limit() {
    let rig = Rig::new("sent-not-waiting", None);
    let sent: Vec<String> = (0..10)
        .map(|i| rig.wait("gedit", &format!("s{i}"), Some(1000), 10 + i))
        .collect();
    for id in &sent {
        let got = rig.c.spool.get(id);
        assert!(got.is_some());
        let mut r = got.unwrap_or_else(|| std::process::abort());
        r.state = State::Sent;
        r.sent_at = Some(20);
        assert!(rig.c.spool.save(&r).is_ok());
    }
    for i in 0..120u64 {
        rig.wait("gedit", &format!("w{i}"), Some(1000), 100 + i);
    }
    for id in &sent {
        assert!(rig.c.spool.get(id).is_some(), "a sent report was dropped");
    }
    let waiting = rig
        .c
        .spool
        .reports()
        .iter()
        .filter(|r| r.state == State::Waiting)
        .count();
    assert_eq!(waiting, MAX_WAITING);
}

#[test]
fn broken_files_in_the_queue_are_skipped_and_do_not_hide_good_reports() {
    let rig = Rig::new("broken", None);
    let dir = rig.reports_dir();
    let good = rig.wait("gedit", "f", Some(1000), 100);
    let valid = serde_json::to_string(&rig.c.spool.get(&good)).unwrap_or_default();
    let bad_state = valid.replace("\"waiting\"", "\"weird\"");
    let bad_uid = valid.replace("\"uid\":1000", "\"uid\":-1");
    let bad_found = valid.replace("\"found\":100", "\"found\":\"yesterday\"");
    let with_bom = format!("\u{feff}{valid}");
    let files: Vec<(&str, Vec<u8>)> = vec![
        ("empty.json", vec![]),
        ("brace.json", b"{".to_vec()),
        ("null.json", b"null".to_vec()),
        ("array.json", b"[]".to_vec()),
        ("number.json", b"5".to_vec()),
        ("garbage.json", (0..=255u8).cycle().take(100_000).collect()),
        ("bom.json", with_bom.into_bytes()),
        ("state.json", bad_state.into_bytes()),
        ("uid.json", bad_uid.into_bytes()),
        ("found.json", bad_found.into_bytes()),
        (
            "truncated.json",
            valid.as_bytes()[..valid.len() / 2].to_vec(),
        ),
        ("nested.json", "[".repeat(5 << 20).into_bytes()),
        ("upper.JSON", valid.clone().into_bytes()),
        ("no-extension", valid.clone().into_bytes()),
        (".hidden.tmp", valid.clone().into_bytes()),
        ("x.json.bak", valid.clone().into_bytes()),
        ("тест.json", b"{}".to_vec()),
    ];
    for (name, bytes) in &files {
        write_raw(&dir, name, bytes);
    }
    assert!(std::fs::create_dir(dir.join("a-folder.json")).is_ok());
    let started = Instant::now();
    let reports = rig.c.spool.reports();
    within_budget(started);
    let ids: Vec<&str> = reports.iter().map(|r| r.crash.id.as_str()).collect();
    assert_eq!(ids, [good.as_str()], "only the good report is listed");
    // A new report is still kept next to the junk.
    rig.wait("gedit", "another", Some(1000), 200);
    assert!(rig.c.spool.reports().iter().any(|r| r.found == 200));
}

#[test]
fn a_file_name_that_is_not_text_does_not_break_the_queue() {
    use std::os::unix::ffi::OsStrExt;
    let rig = Rig::new("bytes-name", None);
    let name = std::ffi::OsStr::from_bytes(b"\xff\xfe\xfd.json");
    assert!(std::fs::write(rig.reports_dir().join(name), b"{}").is_ok());
    let id = rig.wait("gedit", "f", Some(1000), 10);
    assert_eq!(rig.c.spool.reports().len(), 1);
    assert!(rig.c.spool.prune(NOW).is_ok());
    assert!(rig.c.spool.get(&id).is_none(), "pruned: older than 30 days");
}

#[test]
fn ids_from_requests_never_reach_files_outside_the_queue() {
    let rig = Rig::new("hostile-ids", None);
    let canary = rig.state_dir().join("canary.txt");
    write_raw(&rig.state_dir(), "canary.txt", b"keep");
    write_raw(rig.dir.path(), "outside.json", b"keep");
    let long = "a".repeat(100_000);
    for id in [
        "../canary.txt",
        "../../outside",
        "..",
        ".",
        "",
        " ",
        "\0",
        "/etc/passwd",
        "reports/../canary",
        "3f2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9b/../../canary",
        "3f2b8c1e-9a4d-1e7f-8b21-0c5d6e7f8a9b",
        "3f2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9",
        "3f2b8c1e9a4d4e7f8b210c5d6e7f8a9b",
        "ｆ2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9b",
        long.as_str(),
    ] {
        assert!(rig.c.spool.get(id).is_none(), "{id:?}");
        assert!(!rig.c.spool.remove(id), "{id:?}");
        assert!(rig.c.spool.save_feedback(id, &json!({})).is_err(), "{id:?}");
        assert!(rig.c.spool.forgotten(&[id.to_owned()]).is_ok());
    }
    assert!(canary.exists() && rig.dir.path().join("outside.json").exists());
    assert_eq!(std::fs::read(&canary).ok().as_deref(), Some(&b"keep"[..]));
    // A well-formed id is taken in any case and with spaces round it.
    let r = Report::new(crash("gedit", "f"), Some(1000), 10);
    assert!(rig.c.spool.save(&r).is_ok());
    let shouted = format!(" {}\n", r.crash.id.to_ascii_uppercase());
    assert!(rig.c.spool.get(&shouted).is_some());
}

#[test]
fn a_report_cannot_be_saved_under_an_id_that_is_not_a_uuid() {
    let rig = Rig::new("bad-id-save", None);
    let mut r = Report::new(crash("gedit", "f"), Some(1000), 10);
    r.crash.id = "../../canary".into();
    assert!(rig.c.spool.save(&r).is_err());
    assert!(rig.c.spool.add(&r, 10).is_err());
    assert!(!rig.dir.path().join("canary.json").exists());
    assert!(rig.c.spool.reports().is_empty());
}

#[test]
fn a_symbolic_link_in_the_queue_is_replaced_or_removed_never_followed() {
    use std::os::unix::fs::symlink;
    let rig = Rig::new("links", None);
    let outside = rig.dir.path().join("outside.txt");
    write_raw(rig.dir.path(), "outside.txt", b"precious");
    let precious = || std::fs::read(&outside).ok();
    let r = Report::new(crash("gedit", "f"), Some(1000), 10);
    let link = rig.reports_dir().join(format!("{}.json", r.crash.id));
    assert!(symlink(&outside, &link).is_ok());
    assert!(rig.c.spool.save(&r).is_ok());
    assert_eq!(precious().as_deref(), Some(&b"precious"[..]));
    assert!(std::fs::symlink_metadata(&link).is_ok_and(|m| m.is_file()));
    assert!(rig.c.spool.get(&r.crash.id).is_some());
    // Removing a link takes the link, not what it points to.
    let r2 = Report::new(crash("gedit", "g"), Some(1000), 11);
    let link2 = rig.reports_dir().join(format!("{}.json", r2.crash.id));
    assert!(symlink(&outside, &link2).is_ok());
    assert!(rig.c.spool.remove(&r2.crash.id));
    assert!(std::fs::symlink_metadata(&link2).is_err());
    assert_eq!(precious().as_deref(), Some(&b"precious"[..]));
    // Forgetting works the same way for the report and its feedback copy.
    let r3 = Report::new(crash("gedit", "h"), Some(1000), 12);
    let link3 = rig
        .state_dir()
        .join("feedback")
        .join(format!("{}.json", r3.crash.id));
    assert!(symlink(&outside, &link3).is_ok());
    assert!(
        rig.c
            .spool
            .forgotten(std::slice::from_ref(&r3.crash.id))
            .is_ok()
    );
    assert_eq!(precious().as_deref(), Some(&b"precious"[..]));
    assert!(std::fs::symlink_metadata(&link3).is_err());
}

#[test]
fn broken_and_looping_links_in_the_queue_are_skipped() {
    use std::os::unix::fs::symlink;
    let rig = Rig::new("bad-links", None);
    let dir = rig.reports_dir();
    assert!(symlink(dir.join("missing.json"), dir.join("dangling.json")).is_ok());
    assert!(symlink(dir.join("loop-b.json"), dir.join("loop-a.json")).is_ok());
    assert!(symlink(dir.join("loop-a.json"), dir.join("loop-b.json")).is_ok());
    assert!(symlink(dir.join("loop-self.json"), dir.join("loop-self.json")).is_ok());
    assert!(symlink(&dir, dir.join("to-folder.json")).is_ok());
    assert!(symlink("/nonexistent/x/y", dir.join("far.json")).is_ok());
    let id = rig.wait("gedit", "f", Some(1000), 10);
    let started = Instant::now();
    assert_eq!(rig.c.spool.reports().len(), 1);
    assert!(rig.c.spool.get(&id).is_some());
    assert!(rig.c.spool.prune(NOW).is_ok());
    within_budget(started);
}

#[test]
fn saved_files_are_private_and_leave_no_temporary_file() {
    let rig = Rig::new("modes", Some(Consent::Auto));
    let id = rig.wait("gedit", "f", Some(1000), 10);
    assert!(
        rig.c
            .spool
            .record_sent(&id, Some(1000), "crash", 10)
            .is_ok()
    );
    let feedback = new_id();
    assert!(
        rig.c
            .spool
            .save_feedback(&feedback, &json!({"text": "x"}))
            .is_ok()
    );
    assert_eq!(mode(&rig.reports_dir().join(format!("{id}.json"))), 0o600);
    assert_eq!(mode(&rig.state_dir().join("sent.json")), 0o600);
    let copy = rig
        .state_dir()
        .join("feedback")
        .join(format!("{feedback}.json"));
    assert_eq!(mode(&copy), 0o600);
    for dir in [
        rig.reports_dir(),
        rig.state_dir(),
        rig.state_dir().join("feedback"),
    ] {
        assert!(
            file_names(&dir).iter().all(|n| !n.ends_with(".tmp")),
            "{dir:?}"
        );
    }
    // The answer files are for everyone to read, and only they.
    for (rel, _) in consent::files(Consent::Auto) {
        assert_eq!(mode(&rig.dir.path().join(rel)), 0o644, "{rel}");
    }
}

#[test]
fn a_save_that_fails_leaves_no_temporary_file() {
    let rig = Rig::new("failed-save", None);
    let r = Report::new(crash("gedit", "f"), Some(1000), 10);
    // A folder that cannot be replaced by a file stands where the report goes.
    let target = rig.reports_dir().join(format!("{}.json", r.crash.id));
    assert!(std::fs::create_dir_all(target.join("inside")).is_ok());
    assert!(rig.c.spool.save(&r).is_err());
    assert!(
        file_names(&rig.reports_dir())
            .iter()
            .all(|n| !n.ends_with(".tmp"))
    );
}

#[test]
fn the_same_crash_is_kept_once_while_it_waits() {
    let rig = Rig::new("dups", None);
    let first = Report::new(crash("gedit", "f"), Some(1000), 100);
    assert_eq!(rig.c.spool.add(&first, 100).ok(), Some(Added::Added));
    for i in 0..50u64 {
        let again = Report::new(crash("gedit", "f"), Some(1000), 101 + i);
        assert_eq!(
            rig.c.spool.add(&again, 101 + i).ok(),
            Some(Added::Duplicate)
        );
    }
    assert_eq!(rig.c.spool.reports().len(), 1);
    // A different version of the program is a different crash.
    let mut other = Report::new(crash("gedit", "f"), Some(1000), 200);
    other.crash.version = Some("2.0".into());
    assert_eq!(rig.c.spool.add(&other, 200).ok(), Some(Added::Added));
}

#[test]
fn counting_what_was_sent_today_is_exact_at_the_edges() {
    let rig = Rig::new("sent-today", None);
    let s = &rig.c.spool;
    assert!(s.record_sent(&new_id(), Some(1000), "crash", 1000).is_ok());
    assert!(
        s.record_sent(&new_id(), Some(1000), "feedback", 1000)
            .is_ok()
    );
    assert_eq!(s.sent_last_day(1000), 1, "feedback is not a crash report");
    assert_eq!(s.sent_last_day(1000 + DAY - 1), 1);
    assert_eq!(s.sent_last_day(1000 + DAY), 0);
    // A clock that went backwards still counts it, and nothing overflows.
    assert_eq!(s.sent_last_day(0), 1);
    assert_eq!(s.sent_last_day(u64::MAX), 0);
    // The same id is kept once.
    let id = new_id();
    for _ in 0..5 {
        assert!(s.record_sent(&id, Some(1000), "crash", 2000).is_ok());
    }
    assert_eq!(s.sent_ids().iter().filter(|x| x.id == id).count(), 1);
}

#[test]
fn pruning_keeps_what_is_not_old_and_survives_any_clock() {
    let rig = Rig::new("prune", None);
    let s = &rig.c.spool;
    let id = rig.wait("gedit", "f", Some(1000), 1000);
    let future = rig.wait("gedit", "future", Some(1000), u64::MAX);
    assert!(s.record_sent(&new_id(), Some(1000), "crash", 1000).is_ok());
    for now in [0, 1, 999, 1000, 1000 + KEEP] {
        assert!(s.prune(now).is_ok());
        assert!(s.get(&id).is_some(), "now {now}");
    }
    assert!(s.prune(1000 + KEEP + 1).is_ok());
    assert!(s.get(&id).is_none());
    assert_eq!(s.sent_ids().len(), 1);
    assert!(s.prune(1000 + KEEP_IDS).is_ok());
    assert_eq!(s.sent_ids().len(), 1);
    assert!(s.prune(1000 + KEEP_IDS + 1).is_ok());
    assert!(s.sent_ids().is_empty());
    // A report from the far future is never "old", and nothing overflows.
    assert!(s.prune(u64::MAX).is_ok());
    assert!(s.get(&future).is_some());
}

#[test]
fn feedback_copies_are_pruned_by_age() {
    let rig = Rig::new("feedback-age", None);
    let (old, new) = (new_id(), new_id());
    for id in [&old, &new] {
        assert!(rig.c.spool.save_feedback(id, &json!({"text": "x"})).is_ok());
    }
    let path = rig.state_dir().join("feedback").join(format!("{old}.json"));
    let aged = SystemTime::now() - Duration::from_secs(KEEP + 3 * DAY);
    let file = std::fs::OpenOptions::new().write(true).open(&path);
    assert!(file.is_ok_and(|f| f.set_modified(aged).is_ok()));
    let now = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(NOW);
    assert!(rig.c.spool.prune(now).is_ok());
    let left = file_names(&rig.state_dir().join("feedback"));
    assert_eq!(left, [format!("{new}.json")]);
}

#[test]
fn a_damaged_list_of_sent_ids_is_an_empty_list_not_a_crash() {
    let rig = Rig::new("sent-json", None);
    let id = rig.wait("gedit", "f", Some(1000), 10);
    for junk in [
        &b""[..],
        b"{",
        b"null",
        b"{\"id\":\"x\"}",
        b"[1,2,3]",
        &[0xff, 0xfe, 0x00],
        b"[{\"id\":5}]",
    ] {
        write_raw(&rig.state_dir(), "sent.json", junk);
        assert!(rig.c.spool.sent_ids().is_empty());
        assert_eq!(rig.c.spool.sent_last_day(NOW), 0);
        assert!(rig.c.spool.ids_of(1000).is_empty());
        assert!(rig.c.spool.prune(10).is_ok());
        assert!(
            rig.c
                .spool
                .record_sent(&id, Some(1000), "crash", 10)
                .is_ok()
        );
        assert_eq!(rig.c.spool.sent_ids().len(), 1);
    }
}

#[test]
fn a_person_sees_their_own_reports_and_the_systems_only() {
    let mine = Report::new(crash("a", "f"), Some(1000), 0);
    let system = Report::new(crash("b", "f"), Some(999), 0);
    let unknown = Report::new(crash("c", "f"), None, 0);
    let other = Report::new(crash("d", "f"), Some(1001), 0);
    let max = Report::new(crash("e", "f"), Some(u32::MAX), 0);
    for uid in [1000, 1001, u32::MAX, 5000] {
        assert_eq!(mine.visible_to(uid), uid == 1000, "{uid}");
        assert_eq!(other.visible_to(uid), uid == 1001, "{uid}");
        assert_eq!(max.visible_to(uid), uid == u32::MAX, "{uid}");
        assert!(system.visible_to(uid), "{uid}");
    }
    for r in [&mine, &system, &unknown, &other, &max] {
        assert!(r.visible_to(0));
    }
}

// ------------------------------------------------------------- consent --

#[test]
fn only_exactly_one_of_three_words_is_an_answer() {
    let none = [
        "",
        "\n",
        "crash_reports=",
        "crash_reports=yes",
        "crash_reports=AUTO",
        "crash_reports=Auto",
        "crash_reports=auto auto",
        "crash_reports=auto=never",
        "crash_reports=auto # yes",
        "crash_reports=auto;",
        "crash_reports=\"auto\"",
        "crash_reports='auto'",
        "crash_reports=auto\0",
        "crash_reports=ａｕｔｏ",
        "crash_reports＝auto",
        "сrash_reports=auto",
        "\u{feff}crash_reports=auto",
        "crash_reports\u{200b}=auto",
        "crash_reports=\u{200b}auto",
        "export crash_reports=auto",
        "x_crash_reports=auto",
        "crash_reports_x=auto",
        "crash_reports.auto",
        "#crash_reports=auto",
        ";crash_reports=auto",
        "  # crash_reports=auto",
        "[crash_reports]\nauto",
        "crash_reports=auto\\\nauto",
        "CRASH_REPORTS=auto",
    ];
    for text in none {
        assert_eq!(consent::parse_config(text), None, "{text:?}");
    }
    for (text, expected) in [
        ("crash_reports=auto", Consent::Auto),
        ("crash_reports=auto\n", Consent::Auto),
        ("crash_reports=auto\r\n", Consent::Auto),
        ("  crash_reports = ask  \n", Consent::Ask),
        ("\tcrash_reports\t=\tnever\t", Consent::Never),
        (
            "# c\n; d\ncrash_reports=never\n#crash_reports=auto",
            Consent::Never,
        ),
        ("other=1\ncrash_reports=auto\nmore=2", Consent::Auto),
    ] {
        assert_eq!(consent::parse_config(text), Some(expected), "{text:?}");
    }
}

#[test]
fn a_later_line_that_is_not_an_answer_takes_the_answer_away() {
    for text in [
        "crash_reports=auto\ncrash_reports=\n",
        "crash_reports=auto\ncrash_reports=maybe\n",
        "crash_reports=auto\ncrash_reports=AUTO\n",
        "crash_reports=ask\ncrash_reports=auto auto\n",
    ] {
        assert_eq!(consent::parse_config(text), None, "{text:?}");
    }
    assert_eq!(
        consent::parse_config("crash_reports=auto\ncrash_reports=never\n"),
        Some(Consent::Never)
    );
    assert_eq!(
        consent::parse_config("crash_reports=never\ncrash_reports=ask\n"),
        Some(Consent::Ask)
    );
}

#[test]
fn an_answer_file_that_cannot_be_read_is_no_answer() {
    let dir = TempDir::new("consent-files");
    let path = dir.path().join(consent::CONFIG);
    assert!(std::fs::create_dir_all(path.parent().unwrap_or(dir.path())).is_ok());
    assert_eq!(consent::read(dir.path()), None, "missing");
    write_raw(dir.path(), consent::CONFIG, b"");
    assert_eq!(consent::read(dir.path()), None, "empty");
    write_raw(
        dir.path(),
        consent::CONFIG,
        b"crash_reports=auto\n\xff\xfe\n",
    );
    assert_eq!(consent::read(dir.path()), None, "not UTF-8");
    let junk = "x\n".repeat(5_000_000);
    write_raw(dir.path(), consent::CONFIG, junk.as_bytes());
    assert_eq!(consent::read(dir.path()), None, "ten megabytes of junk");
    assert!(std::fs::remove_file(&path).is_ok());
    assert!(std::fs::create_dir(&path).is_ok());
    assert_eq!(consent::read(dir.path()), None, "a folder");
    assert!(std::fs::remove_dir(&path).is_ok());
    assert!(std::os::unix::fs::symlink(dir.path().join("nowhere"), &path).is_ok());
    assert_eq!(consent::read(dir.path()), None, "a broken link");
    // A good answer after a long file of comments is still read.
    assert!(std::fs::remove_file(&path).is_ok());
    let mut text = "# comment\n".repeat(200_000);
    text.push_str("crash_reports=ask\n");
    write_raw(dir.path(), consent::CONFIG, text.as_bytes());
    assert_eq!(consent::read(dir.path()), Some(Consent::Ask));
}

#[test]
fn a_new_answer_goes_to_all_three_files_and_to_tinta_as_yes_or_no() {
    let dir = TempDir::new("consent-three");
    for answer in [
        Consent::Auto,
        Consent::Never,
        Consent::Ask,
        Consent::Auto,
        Consent::Ask,
    ] {
        let written = consent::write(dir.path(), answer);
        assert_eq!(written.map(|w| w.len()).ok(), Some(3));
        assert_eq!(consent::read(dir.path()), Some(answer));
        let yes = if answer == Consent::Auto {
            "true"
        } else {
            "false"
        };
        let env = std::fs::read_to_string(dir.path().join(consent::TINTA_ENVIRONMENT))
            .unwrap_or_default();
        let conf =
            std::fs::read_to_string(dir.path().join(consent::TINTA_CONFIG)).unwrap_or_default();
        assert!(
            env.contains(&format!("TINTA_OFFICE_REPORTS={yes}\n")),
            "{answer:?}: {env}"
        );
        assert!(
            conf.contains(&format!("enabled={yes}\n")),
            "{answer:?}: {conf}"
        );
        assert_eq!(answer.tinta(), answer == Consent::Auto);
        assert_eq!(Consent::parse(answer.as_str()), Some(answer));
    }
    for sub in ["etc/vilna", "etc/environment.d", "etc/xdg/tinta-office"] {
        let names = file_names(&dir.path().join(sub));
        assert!(names.iter().all(|n| !n.ends_with(".tmp")), "{sub}");
    }
}

#[test]
fn nothing_is_sent_before_there_is_an_answer_whatever_waits() {
    for config in [
        None,
        Some(&b""[..]),
        Some(&b"crash_reports=maybe\n"[..]),
        Some(&b"\xff"[..]),
    ] {
        let rig = Rig::new("no-answer", None);
        if let Some(bytes) = config {
            assert!(std::fs::create_dir_all(rig.dir.path().join("etc/vilna")).is_ok());
            write_raw(rig.dir.path(), consent::CONFIG, bytes);
        }
        for i in 0..30u64 {
            rig.wait("gedit", &format!("f{i}"), Some(1000), 10 + i);
        }
        for round in 0..3 {
            assert_eq!(rig.c.send_waiting(NOW + round), 0);
        }
        assert_eq!(rig.server.count(), 0);
        assert!(
            rig.c
                .spool
                .reports()
                .iter()
                .all(|r| r.state == State::Waiting)
        );
        // Crashes are still kept on the device while there is no answer.
        assert!(matches!(rig.c.found(&entry(json!({})), NOW), Ok(Some(_))));
    }
}

#[test]
fn an_answer_taken_back_after_the_crash_stops_the_sending() {
    // Collected under "auto", then the person changes the answer.
    for later in [Consent::Ask, Consent::Never] {
        let rig = Rig::new("revoked", Some(Consent::Auto));
        assert!(matches!(rig.c.found(&entry(json!({})), NOW), Ok(Some(_))));
        rig.answer(later);
        assert_eq!(rig.c.send_waiting(NOW + 60), 0);
        assert_eq!(rig.server.count(), 0, "{later:?}");
        let waiting = rig.c.spool.reports();
        assert!(waiting.len() == 1 && waiting[0].state == State::Waiting);
    }
    // The file taken away, or overwritten with something that is no answer.
    let rig = Rig::new("revoked-gone", Some(Consent::Auto));
    rig.wait("gedit", "f", Some(1000), 10);
    assert!(std::fs::remove_file(rig.dir.path().join(consent::CONFIG)).is_ok());
    assert_eq!(rig.c.send_waiting(NOW), 0);
    write_raw(rig.dir.path(), consent::CONFIG, b"crash_reports=\n");
    assert_eq!(rig.c.send_waiting(NOW), 0);
    assert_eq!(rig.server.count(), 0);
    // Said "auto" again: it is sent then, and once.
    rig.answer(Consent::Auto);
    assert_eq!(rig.c.send_waiting(NOW), 1);
    assert_eq!(rig.c.send_waiting(NOW), 0);
    assert_eq!(rig.server.count(), 1);
}

#[test]
fn the_answer_is_read_again_for_every_round_and_every_status() {
    let rig = Rig::new("live", Some(Consent::Never));
    let status = |rig: &Rig| control::handle(&rig.c, my_uid(), &json!({"op": "status"}), NOW);
    for answer in [Consent::Auto, Consent::Ask, Consent::Never, Consent::Auto] {
        rig.answer(answer);
        assert_eq!(rig.c.consent(), Some(answer));
        assert_eq!(status(&rig)["consent"], json!(answer.as_str()));
    }
}

#[test]
fn with_the_answer_never_a_person_can_still_send_one_report_by_hand() {
    let rig = Rig::new("by-hand", Some(Consent::Never));
    let id = rig.wait("gedit", "f", Some(1000), 10);
    let other = rig.wait("gedit", "g", Some(1000), 11);
    assert_eq!(rig.c.send_waiting(NOW), 0);
    let sent = rig.c.send(&id, NOW);
    assert!(matches!(&sent, Ok(r) if r.state == State::Sent), "{sent:?}");
    assert_eq!(rig.server.count(), 1);
    // Only the one that was chosen.
    let left = rig.c.spool.get(&other);
    assert!(left.is_some_and(|r| r.state == State::Waiting));
}

// ------------------------------------------------------------ sending --

#[test]
fn the_body_sent_is_the_stored_report_and_passes_the_servers_checks() {
    let rig = Rig::new("body", Some(Consent::Auto));
    let m = message(&[
        "call_anna@example.org (libx.so + 0x10)",
        "g (libx.so + 0x11)",
    ]);
    let id = rig
        .c
        .found(&entry(json!({"MESSAGE": m})), NOW)
        .ok()
        .flatten()
        .unwrap_or_default();
    assert_eq!(rig.c.send_waiting(NOW), 1);
    let bodies = rig.server.bodies("/v1/crash");
    assert_eq!(bodies.len(), 1);
    let stored = rig.c.spool.get(&id);
    assert!(
        stored
            .as_ref()
            .is_some_and(|r| r.crash.to_wire() == bodies[0])
    );
    let wire: Result<CrashWire, _> = serde_json::from_value(bodies[0].clone());
    assert!(wire.is_ok());
    let again = Crash::from_wire(wire.unwrap_or_default());
    assert_eq!(again.ok().as_ref(), stored.as_ref().map(|r| &r.crash));
    assert!(!bodies[0].to_string().contains("anna"));
}

#[test]
fn a_report_that_was_sent_is_not_posted_again() {
    let rig = Rig::new("once", Some(Consent::Auto));
    let id = rig.wait("gedit", "f", Some(1000), 10);
    for round in 0..5 {
        assert!(rig.c.send(&id, NOW + round).is_ok());
        let _ = rig.c.send_waiting(NOW + round);
    }
    assert_eq!(rig.server.count(), 1);
    assert_eq!(rig.c.spool.sent_ids().len(), 1);
    // The same crash again within the day is not kept.
    let again = Report::new(crash("gedit", "f"), Some(1000), NOW + 10);
    assert_eq!(
        rig.c.spool.add(&again, NOW + 10).ok(),
        Some(Added::Duplicate)
    );
}

#[test]
fn no_network_stops_the_round_and_the_reports_wait() {
    let rig = Rig::new("later", Some(Consent::Auto));
    for i in 0..5u64 {
        rig.wait("gedit", &format!("f{i}"), Some(1000), 10 + i);
    }
    rig.server.will_answer(vec![
        Answer::Taken(json!({"status": "new", "signature": "0", "id": "x"})),
        Answer::Later("no network".into()),
    ]);
    assert_eq!(rig.c.send_waiting(NOW), 1);
    assert_eq!(
        rig.server.count(),
        2,
        "the round stops at the first 'later'"
    );
    let reports = rig.c.spool.reports();
    let count = |s: State| reports.iter().filter(|r| r.state == s).count();
    assert_eq!(count(State::Sent), 1);
    assert_eq!(count(State::Waiting), 4);
    assert_eq!(
        rig.c.spool.sent_ids().len(),
        1,
        "only what was taken is recorded"
    );
    // The oldest went first.
    let sent = reports.iter().find(|r| r.state == State::Sent);
    assert!(sent.is_some_and(|r| r.found == 10));
}

#[test]
fn a_refusal_is_final_and_does_not_stop_the_others() {
    let rig = Rig::new("refused", Some(Consent::Auto));
    for i in 0..4u64 {
        rig.wait("gedit", &format!("f{i}"), Some(1000), 10 + i);
    }
    rig.server
        .will_answer(vec![Answer::Refused("frames: too many".into())]);
    assert_eq!(rig.c.send_waiting(NOW), 3);
    assert_eq!(rig.server.count(), 4);
    let refused: Vec<Report> = rig
        .c
        .spool
        .reports()
        .into_iter()
        .filter(|r| r.state == State::Refused)
        .collect();
    assert_eq!(refused.len(), 1);
    assert_eq!(refused[0].refused.as_deref(), Some("frames: too many"));
    assert_eq!(rig.c.send_waiting(NOW + 1), 0);
    assert_eq!(
        rig.server.count(),
        4,
        "a refused report is not posted again"
    );
    assert_eq!(
        rig.c.spool.sent_ids().len(),
        3,
        "a refused report has no id to forget"
    );
}

#[test]
fn at_most_twenty_reports_are_sent_a_day_and_the_rest_wait_for_tomorrow() {
    let rig = Rig::new("daily", Some(Consent::Auto));
    for i in 0..25u64 {
        rig.wait("gedit", &format!("f{i}"), Some(1000), 10 + i);
    }
    assert_eq!(rig.c.send_waiting(NOW), MAX_SENT_PER_DAY);
    assert_eq!(rig.server.count(), MAX_SENT_PER_DAY);
    assert_eq!(rig.c.send_waiting(NOW + 3600), 0);
    assert_eq!(rig.server.count(), MAX_SENT_PER_DAY);
    // By hand, the limit holds as well.
    let waiting = rig
        .c
        .spool
        .reports()
        .into_iter()
        .find(|r| r.state == State::Waiting);
    let id = waiting.map(|r| r.crash.id).unwrap_or_default();
    assert!(rig.c.send(&id, NOW + 3600).is_err());
    assert_eq!(rig.server.count(), MAX_SENT_PER_DAY);
    assert_eq!(rig.c.send_waiting(NOW + DAY), 5);
}

#[test]
fn whatever_the_server_takes_the_report_state_stays_sane() {
    for taken in [
        json!(null),
        json!([1, 2]),
        json!("text"),
        json!(5),
        json!({"id": 5}),
        json!({"status": ["x"]}),
        json!({}),
    ] {
        let rig = Rig::new("receipt", Some(Consent::Auto));
        let id = rig.wait("gedit", "f", Some(1000), 10);
        rig.server.will_answer(vec![Answer::Taken(taken.clone())]);
        let sent = rig.c.send(&id, NOW);
        assert!(
            matches!(&sent, Ok(r) if r.state == State::Sent && r.sent_at == Some(NOW)),
            "{taken}: {sent:?}"
        );
        assert!(rig.c.spool.get(&id).is_some_and(|r| r.state == State::Sent));
    }
    let rig = Rig::new("receipt-ok", Some(Consent::Auto));
    let id = rig.wait("gedit", "f", Some(1000), 10);
    rig.server.will_answer(vec![Answer::Taken(
        json!({"id": id, "signature": "ab", "status": "fixed", "fixed_in": "0.2"}),
    )]);
    let sent = rig.c.send(&id, NOW);
    assert!(
        matches!(&sent, Ok(r) if r.receipt.as_ref().is_some_and(|x| x.status == "fixed")),
        "{sent:?}"
    );
}

#[test]
fn answers_of_the_server_are_read_without_a_crash() {
    assert!(matches!(answer(200, b"{\"a\":1}"), Answer::Taken(_)));
    assert!(matches!(answer(201, b"{}"), Answer::Taken(_)));
    for status in [429, 500, 502, 503, 504, 599] {
        assert!(matches!(answer(status, b""), Answer::Later(_)), "{status}");
    }
    for status in [400, 413] {
        assert!(
            matches!(answer(status, b""), Answer::Refused(_)),
            "{status}"
        );
    }
    let huge = vec![b'{'; 70_000];
    let bodies: Vec<&[u8]> = vec![
        b"",
        b"[1,2]",
        b"null",
        b"\xff\xfe",
        b"{\"message\":5}",
        b"{\"message\":null,\"field\":5}",
        b"{\"field\":\"f\"}",
        b"{\"message\":\"m\",\"field\":[]}",
        &huge,
    ];
    for body in bodies {
        for status in [200, 400, 413, 429, 503, 0, 99, 999, 65535] {
            let _ = answer(status, body);
        }
    }
    assert_eq!(
        answer(400, b"{\"field\":\"text\",\"message\":\"is empty\"}"),
        Answer::Refused("text: is empty".into())
    );
    assert_eq!(
        answer(400, b"{\"message\":\"is empty\"}"),
        Answer::Refused("is empty".into())
    );
}

#[test]
fn asking_the_server_to_forget_goes_in_chunks_and_only_for_the_asking_person() {
    let rig = Rig::new("forget", None);
    let mine: Vec<String> = (0..250).map(|_| new_id()).collect();
    let theirs: Vec<String> = (0..10).map(|_| new_id()).collect();
    for id in &mine {
        assert!(
            rig.c
                .spool
                .record_sent(id, Some(1000), "crash", NOW)
                .is_ok()
        );
    }
    for id in &theirs {
        let kind = "feedback";
        assert!(rig.c.spool.record_sent(id, Some(1001), kind, NOW).is_ok());
    }
    assert_eq!(rig.c.forget(1000).ok(), Some(250));
    let posted = rig.server.bodies("/v1/forget");
    assert_eq!(posted.len(), 3);
    let mut seen = HashSet::new();
    for body in &posted {
        let ids = body["ids"].as_array().cloned().unwrap_or_default();
        assert!(ids.len() <= vilna_reports_protocol::MAX_FORGET_IDS);
        for id in ids {
            assert!(seen.insert(id.as_str().unwrap_or_default().to_owned()));
        }
    }
    assert_eq!(seen, mine.iter().cloned().collect::<HashSet<_>>());
    assert_eq!(
        rig.c.spool.sent_ids().len(),
        10,
        "their ids are still there"
    );
    // Nothing of theirs is posted for 1000; for nobody there is nothing to do.
    assert_eq!(rig.c.forget(1000).ok(), Some(0));
    assert_eq!(rig.server.count(), 3);
    // The server stops at the second chunk: the first is forgotten, the rest kept.
    let rig = Rig::new("forget-partial", None);
    for _ in 0..250 {
        let id = new_id();
        assert!(
            rig.c
                .spool
                .record_sent(&id, Some(1000), "crash", NOW)
                .is_ok()
        );
    }
    rig.server
        .will_answer(vec![Answer::Taken(json!({})), Answer::Later("down".into())]);
    assert!(rig.c.forget(1000).is_err());
    assert_eq!(rig.c.spool.sent_ids().len(), 150);
    // Root asks for everyone's.
    assert_eq!(rig.c.forget(0).ok(), Some(150));
}

#[test]
fn feedback_is_scrubbed_the_same_in_the_preview_the_request_and_the_kept_copy() {
    let rig = Rig::new("feedback", Some(Consent::Never));
    let body = json!({
        "kind": "bug", "channel": "site", "id": ID,
        "text": "Пишіть anna@example.org, ip 192.0.2.17, /home/anna/x, +380501234567",
        "text_en": "see /home/anna/y",
        "system": "Vilna, 4111111111111111",
        "contact": "anna@example.org",
        "crash_ids": [ID],
    });
    let preview = control::handle(
        &rig.c,
        1000,
        &json!({"op": "preview", "feedback": body}),
        NOW,
    );
    assert_eq!(rig.server.count(), 0, "a preview sends nothing");
    let shown = preview["feedback"].clone();
    let sent = control::handle(
        &rig.c,
        1000,
        &json!({"op": "feedback", "feedback": body}),
        NOW,
    );
    let id = sent["id"].as_str().unwrap_or_default().to_owned();
    assert!(
        !id.is_empty() && id != ID,
        "the device makes the id: {sent}"
    );
    let posted = rig.server.bodies("/v1/feedback");
    assert_eq!(posted.len(), 1);
    for field in ["text", "text_en", "system"] {
        assert_eq!(posted[0][field], shown[field], "{field}");
        let text = posted[0][field].as_str().unwrap_or_default();
        for secret in ["anna", "192.0.2.17", "380501234567", "4111111111111111"] {
            assert!(!text.contains(secret), "{field}: {text}");
        }
    }
    assert_eq!(
        posted[0]["channel"],
        json!("window"),
        "the channel is the device's, not the sender's"
    );
    let path = rig.state_dir().join("feedback").join(format!("{id}.json"));
    let kept: Option<Value> = std::fs::read(path)
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok());
    assert_eq!(kept.as_ref(), posted.first());
    let recorded = rig.c.spool.sent_ids();
    assert!(recorded.iter().any(|s| s.id == id && s.kind == "feedback"));
}

#[test]
fn feedback_that_is_wrong_is_refused_before_anything_is_posted() {
    let rig = Rig::new("feedback-bad", None);
    let long = "я".repeat(8001);
    let bodies = [
        json!({"text": ""}),
        json!({"text": "   \n"}),
        json!({"text": long}),
        json!({"text": "x", "kind": "complaint"}),
        json!({"text": "x", "contact": "not an address"}),
        json!({"text": "x", "contact": "a@b"}),
        json!({"text": "x", "crash_ids": ["../x"]}),
        json!({"text": 5}),
        json!({"text": "x", "may_publish": "yes"}),
        json!("text"),
        json!([1]),
        json!(null),
    ];
    for body in bodies {
        let got = rig.c.feedback(1000, &body, NOW);
        assert!(got.is_err(), "{body}");
    }
    assert_eq!(rig.server.count(), 0);
    assert!(file_names(&rig.state_dir().join("feedback")).is_empty());
    // The server not taking it leaves nothing kept and no id to forget.
    rig.server.will_answer(vec![Answer::Later("down".into())]);
    assert!(rig.c.feedback(1000, &json!({"text": "x"}), NOW).is_err());
    rig.server.will_answer(vec![Answer::Refused("no".into())]);
    assert!(rig.c.feedback(1000, &json!({"text": "x"}), NOW).is_err());
    assert!(file_names(&rig.state_dir().join("feedback")).is_empty());
    assert!(rig.c.spool.sent_ids().is_empty());
}

#[test]
fn the_text_about_the_computer_holds_only_what_is_known_and_sane() {
    let mut rig = Rig::new("system", None);
    assert_eq!(rig.c.system_text(), "Vilna Blavet 0.1, x86_64, 16 GB, uk");
    rig.c.device = Device {
        image: None,
        locale: None,
        arch: None,
        memory_gb: Some(0),
    };
    assert_eq!(rig.c.system_text(), "");
    rig.c.device.memory_gb = Some(u32::MAX);
    assert_eq!(rig.c.system_text(), "");
}

// ------------------------------------------------------ control socket --

#[test]
fn a_person_reaches_only_their_own_reports_through_every_operation() {
    let rig = Rig::new("isolation", Some(Consent::Ask));
    let mine = rig.wait("gedit", "mine", Some(1000), 10);
    let theirs = rig.wait("gedit", "theirs", Some(1001), 11);
    let system = rig.wait("gdm", "system", Some(42), 12);
    let unknown = rig.wait("x", "unknown", None, 13);
    let call = |uid: u32, request: Value| control::handle(&rig.c, uid, &request, NOW);
    let listed = |uid: u32| -> HashSet<String> {
        call(uid, json!({"op": "list"}))["reports"]
            .as_array()
            .cloned()
            .unwrap_or_default()
            .iter()
            .filter_map(|r| r["id"].as_str().map(str::to_owned))
            .collect()
    };
    let set = |ids: &[&String]| -> HashSet<String> { ids.iter().map(|s| (*s).clone()).collect() };
    assert_eq!(listed(1000), set(&[&mine, &system, &unknown]));
    assert_eq!(listed(1001), set(&[&theirs, &system, &unknown]));
    assert_eq!(listed(0).len(), 4);
    assert_eq!(listed(65534), set(&[&system, &unknown]));
    for op in ["show", "send", "discard"] {
        let got = call(1000, json!({"op": op, "id": theirs}));
        assert!(got["error"].is_string(), "{op}: {got}");
        let got = call(1000, json!({"op": op, "id": theirs.to_ascii_uppercase()}));
        assert!(got["error"].is_string(), "{op} (upper case): {got}");
    }
    assert_eq!(rig.server.count(), 0);
    assert!(rig.c.spool.get(&theirs).is_some());
    assert_eq!(call(1000, json!({"op": "status"}))["waiting"], json!(3));
    // Their ids are not offered to be forgotten by someone else.
    assert!(
        rig.c
            .spool
            .record_sent(&new_id(), Some(1001), "crash", NOW)
            .is_ok()
    );
    assert_eq!(call(1000, json!({"op": "forget"}))["deleted"], json!(0));
    assert_eq!(rig.server.count(), 0);
    assert_eq!(rig.c.spool.sent_ids().len(), 1);
}

#[test]
fn what_show_says_is_what_send_posts_and_a_sent_report_cannot_be_discarded() {
    let rig = Rig::new("show-send", Some(Consent::Ask));
    let id = rig.wait("gedit", "f", Some(1000), 10);
    let call = |request: Value| control::handle(&rig.c, 1000, &request, NOW);
    let shown = call(json!({"op": "show", "id": id}));
    let sent = call(json!({"op": "send", "id": id}));
    assert_eq!(sent["state"], json!("sent"), "{sent}");
    let posted = rig.server.bodies("/v1/crash");
    assert_eq!(posted.len(), 1);
    assert_eq!(shown["report"], posted[0]);
    assert!(call(json!({"op": "discard", "id": id}))["error"].is_string());
    assert!(rig.c.spool.get(&id).is_some());
}

#[test]
fn hostile_requests_get_words_and_change_nothing() {
    let rig = Rig::new("requests", Some(Consent::Never));
    let id = rig.wait("gedit", "f", Some(1000), 10);
    let long = "x".repeat(1 << 20);
    let mut deep = json!(1);
    for _ in 0..100 {
        deep = json!([deep]);
    }
    let requests = [
        json!(null),
        json!(5),
        json!("send"),
        json!([]),
        json!({}),
        json!({"op": null}),
        json!({"op": 5}),
        json!({"op": ["send"]}),
        json!({"op": "SEND", "id": id}),
        json!({"op": "send "}),
        json!({"op": "send\0", "id": id}),
        json!({"op": "send", "id": null}),
        json!({"op": "send", "id": [id]}),
        json!({"op": "send", "id": {"x": id}}),
        json!({"op": "show", "id": long}),
        json!({"op": "discard", "id": "../../x"}),
        json!({"op": long}),
        json!({"op": "feedback", "feedback": deep}),
        json!({"op": "feedback", "feedback": {"text": long}}),
        json!({"op": "preview", "feedback": null}),
        json!({"op": "nope", "id": id}),
        json!({"id": id}),
    ];
    for request in requests {
        let answer = control::handle(&rig.c, 1000, &request, NOW);
        assert!(answer.is_object(), "{request}");
        assert!(answer["error"].is_string(), "{request}: {answer}");
        assert_eq!(rig.server.count(), 0, "{request}");
    }
    let left = rig.c.spool.get(&id);
    assert!(left.is_some_and(|r| r.state == State::Waiting));
}

/// Talks to `serve_one` over a socket pair; the answers come back as lines.
fn talk(c: &Arc<Mutex<Collector>>, input: Vec<u8>) -> Vec<String> {
    let (client, server) = UnixStream::pair().unwrap_or_else(|_| std::process::abort());
    let served = c.clone();
    let worker = std::thread::spawn(move || control::serve_one(&served, server));
    let mut writer = client.try_clone().unwrap_or_else(|_| std::process::abort());
    let _ = client.set_read_timeout(Some(Duration::from_secs(20)));
    let feeder = std::thread::spawn(move || {
        for chunk in input.chunks(8192) {
            if writer.write_all(chunk).is_err() {
                return;
            }
        }
        let _ = writer.shutdown(std::net::Shutdown::Write);
    });
    let mut out = Vec::new();
    let mut reader = client;
    let _ = reader.read_to_end(&mut out);
    let _ = feeder.join();
    assert!(worker.join().is_ok());
    String::from_utf8_lossy(&out)
        .lines()
        .map(str::to_owned)
        .collect()
}

#[test]
fn the_socket_answers_each_line_and_survives_junk_between_them() {
    let rig = Rig::new("socket", Some(Consent::Never));
    let Rig { dir, c, server } = rig;
    let shared = Arc::new(Mutex::new(c));
    let mut input = Vec::new();
    input.extend_from_slice(b"{\"op\":\"status\"}\n");
    input.extend_from_slice(b"not json\n");
    input.extend_from_slice(b"\n");
    input.extend_from_slice(b"[1,2,3]\n");
    input.extend_from_slice(b"{\"op\":\"system\"}\n");
    input.extend_from_slice(b"{\"op\":\"status\"}");
    let lines = talk(&shared, input);
    assert_eq!(lines.len(), 6, "{lines:?}");
    assert!(lines[0].contains("\"consent\":\"never\""), "{}", lines[0]);
    for bad in [&lines[1], &lines[2], &lines[3]] {
        assert!(bad.contains("error"), "{bad}");
    }
    assert!(lines[4].contains("system"), "{}", lines[4]);
    assert!(lines[5].contains("never"), "{}", lines[5]);
    assert_eq!(server.count(), 0);
    drop(dir);
}

#[test]
fn bytes_that_are_not_text_end_the_conversation_and_not_the_service() {
    let rig = Rig::new("socket-bytes", Some(Consent::Never));
    let Rig { dir, c, .. } = rig;
    let shared = Arc::new(Mutex::new(c));
    let mut input = b"{\"op\":\"status\"}\n".to_vec();
    input.extend_from_slice(&[0xff, 0xfe, 0x00, b'\n']);
    input.extend_from_slice(b"{\"op\":\"status\"}\n");
    let lines = talk(&shared, input);
    assert!(!lines.is_empty() && lines[0].contains("never"), "{lines:?}");
    // The next person who connects is served as before.
    let again = talk(&shared, b"{\"op\":\"status\"}\n".to_vec());
    assert_eq!(again.len(), 1);
    drop(dir);
}

#[test]
fn a_request_longer_than_the_limit_is_answered_once_and_the_connection_closed() {
    let rig = Rig::new("socket-long", Some(Consent::Never));
    let Rig { dir, c, .. } = rig;
    let shared = Arc::new(Mutex::new(c));
    let mut input = vec![b'{'];
    input.extend(std::iter::repeat_n(
        b' ',
        control::MAX_REQUEST as usize + 10,
    ));
    input.extend_from_slice(b"}\n{\"op\":\"status\"}\n");
    let lines = talk(&shared, input);
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(lines[0].contains("too long"), "{}", lines[0]);
    drop(dir);
}

#[test]
fn a_flood_of_requests_on_one_connection_ends_by_itself() {
    let rig = Rig::new("socket-flood", Some(Consent::Never));
    let Rig { dir, c, .. } = rig;
    let shared = Arc::new(Mutex::new(c));
    let line = b"{\"op\":\"status\"}\n";
    let input: Vec<u8> = line.iter().copied().cycle().take(3 << 20).collect();
    let started = Instant::now();
    let lines = talk(&shared, input);
    within_budget(started);
    assert!(!lines.is_empty());
    assert!(lines.len() <= (control::MAX_REQUEST as usize * 16) / line.len() + 1);
    assert!(lines.iter().all(|l| l.contains("consent")));
    drop(dir);
}

// ---------------------------------------------------- the whole pipeline --

#[test]
fn from_a_hostile_journal_to_the_server_nothing_personal_goes_out() {
    let rig = Rig::new("pipeline", Some(Consent::Auto));
    let personal = json!({
        "COREDUMP_CMDLINE": "notes --password=hunter2 /home/anna/taxes.ods",
        "COREDUMP_ENVIRON": "HOME=/home/anna\nTOKEN=abc123",
        "COREDUMP_CWD": "/home/anna/Documents",
        "COREDUMP_HOSTNAME": "annas-laptop",
        "_HOSTNAME": "annas-laptop",
        "COREDUMP_EXE": "/home/anna/bin/notes",
    });
    let mut lines = Vec::new();
    for i in 0..30u64 {
        let m = message(&[
            &format!("anna_at_example_org{i} (libx.so + 0x10)"),
            &format!("call_anna@example.org_{i} (/home/anna/lib/liby.so + 0x{i:x})"),
            "main (/home/anna/bin/notes + 0x1)",
        ]);
        let mut e = entry(json!({"MESSAGE": m}));
        for (k, v) in personal.as_object().cloned().unwrap_or_default() {
            e.insert(k, v);
        }
        e.remove("COREDUMP_PACKAGE_NAME");
        lines.push(serde_json::to_string(&Value::Object(e)).unwrap_or_default());
        lines.push(format!("{{\"junk\": {i}"));
    }
    let text = lines.join("\n");
    let entries = journal::entries(text.as_bytes());
    assert_eq!(entries.len(), 30);
    for e in &entries {
        let _ = rig.c.found(e, NOW);
    }
    assert!(rig.c.spool.reports().len() <= MAX_WAITING);
    let sent = rig.c.send_waiting(NOW);
    assert_eq!(sent, MAX_SENT_PER_DAY.min(rig.c.spool.reports().len()));
    assert!(sent > 0);
    for body in rig.server.bodies("/v1/crash") {
        let text = body.to_string();
        for forbidden in [
            "hunter2",
            "abc123",
            "/home",
            "anna@",
            "annas-laptop",
            "taxes",
            "Documents",
        ] {
            assert!(!text.contains(forbidden), "{forbidden} in {text}");
        }
        assert!(text.len() <= MAX_BODY_BYTES);
        assert_eq!(body["program"]["name"], json!("notes"));
    }
    // What the person can read about it on the device holds nothing more.
    for r in rig.c.spool.reports() {
        let kept = serde_json::to_string(&r).unwrap_or_default();
        for forbidden in ["hunter2", "abc123", "annas-laptop", "taxes"] {
            assert!(!kept.contains(forbidden), "{forbidden} in {kept}");
        }
    }
}
