// SPDX-License-Identifier: GPL-3.0-or-later
//! A crash report from a `systemd-coredump` journal entry (ADR 0018 section 2).
//!
//! The entry is what `journalctl -o json MESSAGE_ID=<coredump>` prints. Only
//! a few fields are read: the signal, the package of the program (or its
//! Flatpak id from the cgroup), the modules' build-ids and the stack of the
//! thread that crashed, taken from the text of `MESSAGE`. Never the core
//! dump itself, the command line, the environment, the working directory,
//! the executable's path, the user or host name: those fields are not read
//! at all, so they cannot leak into a report.

use std::collections::HashMap;

use serde_json::{Map, Value, json};
use vilna_reports_protocol::{Crash, CrashWire, Invalid, MAX_FRAMES};

/// `MESSAGE_ID` of the entry `systemd-coredump` writes for each crash.
pub const COREDUMP_MESSAGE_ID: &str = "fc2e22bc6ee647b6b90729ab34a250b1";

/// Programs that report their own crashes: Tinta Office sends to
/// report.tintaoffice.com with the same answer (ADR 0018 section 3), and
/// this collector must not report itself crashing on its own report.
const OWN_REPORTERS: &[&str] = &[
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

/// What the device knows about itself, added to every report.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Device {
    pub image: Option<String>,
    pub locale: Option<String>,
    pub arch: Option<String>,
    pub memory_gb: Option<u32>,
}

/// A crash found in the journal: the report as it would be sent, and whose
/// program it was (a person sees only their own crashes and the system's).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Found {
    pub crash: Crash,
    pub uid: Option<u32>,
    /// Microseconds since 1970, from the journal.
    pub when_us: Option<u64>,
}

/// Why an entry gave no report.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Skip {
    /// Not a `systemd-coredump` entry.
    NotACrash,
    /// A program that reports its own crashes (Tinta Office, this collector).
    OwnReporter(String),
    /// The entry lacks what a report needs.
    Invalid(Invalid),
}

/// The text of a journal field. `journalctl -o json` writes a string, an
/// array of bytes for text that is not UTF-8, or an array of values when the
/// field repeats (then the first one counts).
pub fn field(entry: &Map<String, Value>, name: &str) -> Option<String> {
    match entry.get(name)? {
        Value::String(s) => Some(s.clone()),
        Value::Array(items) if items.iter().all(Value::is_u64) => {
            let bytes: Vec<u8> = items
                .iter()
                .filter_map(Value::as_u64)
                .filter_map(|b| u8::try_from(b).ok())
                .collect();
            Some(String::from_utf8_lossy(&bytes).into_owned())
        }
        Value::Array(items) => items.first().and_then(|v| match v {
            Value::String(s) => Some(s.clone()),
            _ => None,
        }),
        _ => None,
    }
}

/// The Flatpak id from a cgroup such as
/// `…/app.slice/app-flatpak-org.gnome.Nautilus-12345.scope`.
pub fn flatpak_id(cgroup: &str) -> Option<String> {
    let scope = cgroup.rsplit('/').next()?;
    let rest = scope.strip_prefix("app-flatpak-")?.strip_suffix(".scope")?;
    let (id, pid) = rest.rsplit_once('-')?;
    (!id.is_empty() && pid.chars().all(|c| c.is_ascii_digit())).then(|| id.to_owned())
}

/// The last part of a path: modules are named by file only, never by folder.
fn file_name(path: &str) -> &str {
    path.rsplit('/').next().unwrap_or(path)
}

/// Build-ids by module file name, from `COREDUMP_PACKAGE_JSON` (each module
/// with package metadata has a `buildId`) and from the `Module … with
/// build-id …` lines of the message (modules without metadata).
pub fn build_ids(package_json: Option<&str>, message: &str) -> HashMap<String, String> {
    let mut ids = HashMap::new();
    if let Some(Ok(Value::Object(modules))) = package_json.map(serde_json::from_str::<Value>) {
        for (name, about) in &modules {
            if let Some(id) = about.get("buildId").and_then(Value::as_str) {
                ids.insert(file_name(name).to_owned(), id.to_ascii_lowercase());
            }
        }
    }
    for line in message.lines() {
        let Some(rest) = line.trim().strip_prefix("Module ") else {
            continue;
        };
        if let Some((name, id)) = rest.split_once(" with build-id ") {
            ids.entry(file_name(name.trim()).to_owned())
                .or_insert_with(|| id.trim().to_ascii_lowercase());
        }
    }
    ids
}

/// The frames of the first stack in the message: the thread that crashed.
/// A line looks like `#3  0x000055b5e7a3d1b2 n/a (gnome-calculator + 0x1b2)`.
pub fn frames(message: &str, ids: &HashMap<String, String>) -> Vec<Value> {
    let mut out = Vec::new();
    let mut inside = false;
    for line in message.lines() {
        let line = line.trim();
        if line.starts_with("Stack trace of thread") {
            if inside {
                break; // the next thread: only the one that crashed counts
            }
            inside = true;
            continue;
        }
        if !inside {
            continue;
        }
        if !line.starts_with('#') {
            if out.is_empty() {
                continue;
            }
            break;
        }
        if out.len() >= MAX_FRAMES {
            break;
        }
        if let Some(frame) = frame(line, ids) {
            out.push(frame);
        }
    }
    out
}

fn frame(line: &str, ids: &HashMap<String, String>) -> Option<Value> {
    // "#3  0x… function (module + 0xoffset)"
    let mut words = line.split_whitespace();
    words.next()?; // #3
    words.next()?; // the address: it differs on every run, never sent
    let rest: Vec<&str> = words.collect();
    let rest = rest.join(" ");
    let open = rest.rfind('(')?;
    let function = rest.get(..open)?.trim();
    let inner = rest.get(open + 1..)?.trim_end_matches(')');
    let (module, offset) = inner.split_once(" + ")?;
    let module = file_name(module.trim());
    let mut f = Map::new();
    if module != "n/a" {
        f.insert("module".into(), json!(module));
        if let Some(id) = ids.get(module) {
            f.insert("build_id".into(), json!(id));
        }
    }
    f.insert("offset".into(), json!(offset.trim()));
    if !function.is_empty() && function != "n/a" {
        f.insert("function".into(), json!(function));
    }
    Some(Value::Object(f))
}

/// `SIGSEGV` from the entry's signal name, or from its number.
fn signal(entry: &Map<String, Value>) -> Option<String> {
    if let Some(name) = field(entry, "COREDUMP_SIGNAL_NAME") {
        return Some(name);
    }
    let number: u32 = field(entry, "COREDUMP_SIGNAL")?.trim().parse().ok()?;
    let name = match number {
        4 => "SIGILL",
        5 => "SIGTRAP",
        6 => "SIGABRT",
        7 => "SIGBUS",
        8 => "SIGFPE",
        11 => "SIGSEGV",
        24 => "SIGXCPU",
        25 => "SIGXFSZ",
        31 => "SIGSYS",
        3 => "SIGQUIT",
        _ => return None,
    };
    Some(name.to_owned())
}

/// The report for one journal entry, checked by the same rules as on the
/// server (`Crash::from_wire`): what is returned is exactly what is sent.
pub fn report(entry: &Map<String, Value>, device: &Device, id: &str) -> Result<Found, Skip> {
    if field(entry, "MESSAGE_ID").as_deref() != Some(COREDUMP_MESSAGE_ID) {
        return Err(Skip::NotACrash);
    }
    // The journal itself sets `_UID` and `_COMM`: a program that writes the
    // same MESSAGE_ID is not systemd-coredump and its entry is no crash.
    let from = |name: &str, want: &str| field(entry, name).is_none_or(|v| v == want);
    if !from("_UID", "0") || !from("_COMM", "systemd-coredum") {
        return Err(Skip::NotACrash);
    }
    let flatpak = field(entry, "COREDUMP_CGROUP")
        .as_deref()
        .and_then(flatpak_id);
    let package = field(entry, "COREDUMP_PACKAGE_NAME");
    let (name, packaging, version) = if let Some(app) = flatpak {
        (app, "flatpak", None)
    } else if let Some(package) = package {
        (package, "rpm", field(entry, "COREDUMP_PACKAGE_VERSION"))
    } else {
        // No package: the program's file name (never its folder), or its comm.
        let exe = field(entry, "COREDUMP_EXE").map(|e| file_name(&e).to_owned());
        let name = exe
            .or_else(|| field(entry, "COREDUMP_COMM"))
            .unwrap_or_default();
        (name, "other", None)
    };
    if OWN_REPORTERS.contains(&name.as_str()) {
        return Err(Skip::OwnReporter(name));
    }
    let message = field(entry, "MESSAGE").unwrap_or_default();
    let package_json = field(entry, "COREDUMP_PACKAGE_JSON");
    let ids = build_ids(package_json.as_deref(), &message);
    let wire = json!({
        "schema": vilna_reports_protocol::SCHEMA,
        "id": id,
        "program": {"name": name, "version": version, "packaging": packaging},
        "image": device.image,
        "kind": "signal",
        "signal": signal(entry),
        "frames": frames(&message, &ids),
        "locale": device.locale,
        "arch": device.arch,
        "memory_gb": device.memory_gb,
    });
    let wire: CrashWire = serde_json::from_value(wire).map_err(|_| {
        Skip::Invalid(Invalid {
            field: "entry",
            why: "could not be read",
        })
    })?;
    let crash = Crash::from_wire(wire).map_err(Skip::Invalid)?;
    let uid = field(entry, "COREDUMP_UID").and_then(|u| u.trim().parse().ok());
    let when_us = field(entry, "COREDUMP_TIMESTAMP")
        .or_else(|| field(entry, "__REALTIME_TIMESTAMP"))
        .and_then(|t| t.trim().parse().ok());
    Ok(Found {
        crash,
        uid,
        when_us,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const ID: &str = "3f2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9b";

    const MESSAGE: &str = "Process 3018 (gnome-calculator) of user 1000 dumped core.\n\n\
        Module libc.so.6 from rpm glibc-2.40-1.1.x86_64\n\
        Module gnome-calculator with build-id 0A1B2C3D4E5F60718293\n\
        Stack trace of thread 3018:\n\
        #0  0x00007f0f8a0ac884 __pthread_kill_implementation (libc.so.6 + 0x8e884)\n\
        #1  0x00007f0f8a05bafe raise (/usr/lib64/libc.so.6 + 0x3dafe)\n\
        #2  0x000055b5e7a3d1b2 n/a (gnome-calculator + 0x1b2)\n\
        #3  0x0000000000000000 n/a (n/a + 0x0)\n\
        \n\
        Stack trace of thread 3020:\n\
        #0  0x00007f0f8a12f0bd syscall (libc.so.6 + 0x1110bd)\n\
        ELF object binary architecture: AMD x86-64";

    fn entry(extra: Value) -> Map<String, Value> {
        let mut base = json!({
            "MESSAGE_ID": COREDUMP_MESSAGE_ID,
            "MESSAGE": MESSAGE,
            "COREDUMP_SIGNAL_NAME": "SIGABRT",
            "COREDUMP_SIGNAL": "6",
            "COREDUMP_UID": "1000",
            "COREDUMP_EXE": "/usr/bin/gnome-calculator",
            "COREDUMP_COMM": "gnome-calculat",
            "COREDUMP_CMDLINE": "gnome-calculator --secret-arg anna@example.org",
            "COREDUMP_ENVIRON": "HOME=/home/anna",
            "COREDUMP_CWD": "/home/anna/Documents",
            "COREDUMP_HOSTNAME": "annas-laptop",
            "COREDUMP_PACKAGE_NAME": "gnome-calculator",
            "COREDUMP_PACKAGE_VERSION": "47.1-1.1",
            "COREDUMP_PACKAGE_JSON": "{\"libc.so.6\":{\"type\":\"rpm\",\"name\":\"glibc\",\"buildId\":\"ffeeddccbbaa99887766\"}}",
            "COREDUMP_TIMESTAMP": "1763300000000000",
        });
        if let (Some(b), Some(e)) = (base.as_object_mut(), extra.as_object()) {
            for (k, v) in e {
                b.insert(k.clone(), v.clone());
            }
        }
        base.as_object().cloned().unwrap_or_default()
    }

    fn device() -> Device {
        Device {
            image: Some("Vilna Blavet 0.1".into()),
            locale: Some("uk".into()),
            arch: Some("x86_64".into()),
            memory_gb: Some(16),
        }
    }

    #[test]
    fn a_coredump_entry_becomes_a_report_of_the_crashed_thread_only() {
        let found = report(&entry(json!({})), &device(), ID).unwrap();
        let c = &found.crash;
        assert_eq!(c.program, "gnome-calculator");
        assert_eq!(c.version.as_deref(), Some("47.1-1.1"));
        assert_eq!(c.signal.as_deref(), Some("SIGABRT"));
        assert_eq!(c.image.as_deref(), Some("Vilna Blavet 0.1"));
        assert_eq!(found.uid, Some(1000));
        // Three frames of thread 3018; the "n/a + 0x0" frame keeps only its offset.
        assert_eq!(c.frames.len(), 4);
        assert_eq!(c.frames[0].module.as_deref(), Some("libc.so.6"));
        assert_eq!(
            c.frames[0].build_id.as_deref(),
            Some("ffeeddccbbaa99887766")
        );
        assert_eq!(
            c.frames[0].function.as_deref(),
            Some("__pthread_kill_implementation")
        );
        // A module named by its path is named by its file only.
        assert_eq!(c.frames[1].module.as_deref(), Some("libc.so.6"));
        assert_eq!(c.frames[2].module.as_deref(), Some("gnome-calculator"));
        assert_eq!(
            c.frames[2].build_id.as_deref(),
            Some("0a1b2c3d4e5f60718293")
        );
        assert_eq!(c.frames[2].function, None);
        assert!(
            !c.frames
                .iter()
                .any(|f| f.function.as_deref() == Some("syscall"))
        );
    }

    #[test]
    fn nothing_personal_from_the_entry_reaches_the_report() {
        let found = report(&entry(json!({})), &device(), ID).unwrap();
        let text = serde_json::to_string(&found.crash).unwrap();
        for private in [
            "anna",
            "example.org",
            "secret-arg",
            "Documents",
            "/usr/bin",
            "/usr/lib64",
            "3018",
            "0x00007f0f8a0ac884",
            "1000",
        ] {
            assert!(!text.contains(private), "{private} leaked: {text}");
        }
    }

    #[test]
    fn a_flatpak_is_named_by_its_id() {
        let e = entry(json!({
            "COREDUMP_CGROUP": "/user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-org.gnome.Nautilus-48213.scope",
        }));
        let c = report(&e, &device(), ID).unwrap().crash;
        assert_eq!(c.program, "org.gnome.Nautilus");
        assert_eq!(c.version, None);
        assert_eq!(c.packaging, vilna_reports_protocol::Packaging::Flatpak);
    }

    #[test]
    fn without_a_package_the_file_name_of_the_program_is_used() {
        let mut e = entry(json!({}));
        e.remove("COREDUMP_PACKAGE_NAME");
        let c = report(&e, &device(), ID).unwrap().crash;
        assert_eq!(c.program, "gnome-calculator");
        assert_eq!(c.packaging, vilna_reports_protocol::Packaging::Other);
    }

    #[test]
    fn tinta_office_and_this_collector_report_their_own_crashes() {
        let e = entry(json!({
            "COREDUMP_CGROUP": "/user.slice/app-flatpak-io.github.stdenis1980.tintatexto-77.scope",
        }));
        assert_eq!(
            report(&e, &device(), ID),
            Err(Skip::OwnReporter("io.github.stdenis1980.tintatexto".into()))
        );
        let e = entry(json!({"COREDUMP_PACKAGE_NAME": "vilna-reportd"}));
        assert!(matches!(
            report(&e, &device(), ID),
            Err(Skip::OwnReporter(_))
        ));
    }

    #[test]
    fn other_entries_and_broken_ones_are_skipped() {
        let e = entry(json!({"MESSAGE_ID": "0000"}));
        assert_eq!(report(&e, &device(), ID), Err(Skip::NotACrash));
        let e = entry(json!({"COREDUMP_PACKAGE_NAME": "/bad name"}));
        assert!(matches!(report(&e, &device(), ID), Err(Skip::Invalid(_))));
        assert!(matches!(
            report(&entry(json!({})), &device(), "not-an-id"),
            Err(Skip::Invalid(_))
        ));
    }

    #[test]
    fn the_signal_number_is_named_when_the_name_is_missing() {
        let mut e = entry(json!({"COREDUMP_SIGNAL": "11"}));
        e.remove("COREDUMP_SIGNAL_NAME");
        assert_eq!(
            report(&e, &device(), ID).unwrap().crash.signal.as_deref(),
            Some("SIGSEGV")
        );
    }

    #[test]
    fn binary_and_repeated_fields_are_read() {
        let e = entry(json!({
            "COREDUMP_PACKAGE_NAME": [103, 101, 100, 105, 116],
            "COREDUMP_SIGNAL_NAME": ["SIGSEGV", "SIGABRT"],
        }));
        let c = report(&e, &device(), ID).unwrap().crash;
        assert_eq!(c.program, "gedit");
        assert_eq!(c.signal.as_deref(), Some("SIGSEGV"));
    }

    #[test]
    fn hostile_messages_never_panic_and_stay_bounded() {
        let long = "#0 0x1 f (m + 0x1)\n".repeat(500);
        for message in [
            String::new(),
            "Stack trace of thread 1:".into(),
            "Stack trace of thread 1:\n#".into(),
            "Stack trace of thread 1:\n#0 0x1 ((((\n#1 0x2 f (m)\n#2".into(),
            format!("Stack trace of thread 1:\n{long}"),
            "Stack trace of thread 1:\n#0 0x1 ф (модуль + 0xzz)".into(),
        ] {
            let e = entry(json!({"MESSAGE": message}));
            if let Ok(found) = report(&e, &device(), ID) {
                assert!(found.crash.frames.len() <= MAX_FRAMES);
            }
        }
        assert_eq!(flatpak_id("app-flatpak-.scope"), None);
        assert_eq!(flatpak_id("app-flatpak-org.x-abc.scope"), None);
        assert_eq!(
            flatpak_id("app-flatpak-org.x.Y-12.scope").as_deref(),
            Some("org.x.Y")
        );
    }
}
