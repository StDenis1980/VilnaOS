// SPDX-License-Identifier: GPL-3.0-or-later
//! `vilna-reportd`: the service (`run`, as `vilna-reportd.service`) and its
//! small command line.

use std::io::Read;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{Value, json};
use vilna_reportd::collector::Collector;
use vilna_reportd::consent::{self, Consent};
use vilna_reportd::send::{Curl, DEFAULT_SERVER};
use vilna_reportd::spool::Spool;
use vilna_reportd::{control, coredump, device, journal};

const USAGE: &str = "\
vilna-reportd: crash reports and feedback of Vilna (ADR 0018)

  vilna-reportd run                 the service: read crashes, send as the person answered
  vilna-reportd consent [auto|ask|never]
                                    show or set the answer (as root)
  vilna-reportd status | list | system | forget
  vilna-reportd show ID | send ID | discard ID
  vilna-reportd feedback < feedback.json
  vilna-reportd parse FILE|-        the report each journal entry (journalctl -o json) gives

Environment: VILNA_REPORTS_URL (server), STATE_DIRECTORY, RUNTIME_DIRECTORY,
VILNA_REPORTD_SOCKET (the control socket, /run/vilna-reportd/control).";

/// How often the journal is read and waiting reports are sent.
const ROUND: Duration = Duration::from_secs(60);

fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn env_path(name: &str, default: &str) -> PathBuf {
    std::env::var_os(name)
        .map(PathBuf::from)
        // systemd may list several directories; the first is ours.
        .map(|p| PathBuf::from(p.to_string_lossy().split(':').next().unwrap_or_default()))
        .filter(|p| p.is_absolute())
        .unwrap_or_else(|| PathBuf::from(default))
}

fn socket_path() -> PathBuf {
    if let Some(p) = std::env::var_os("VILNA_REPORTD_SOCKET") {
        return PathBuf::from(p);
    }
    env_path("RUNTIME_DIRECTORY", "/run/vilna-reportd").join("control")
}

fn server() -> String {
    std::env::var("VILNA_REPORTS_URL").unwrap_or_else(|_| DEFAULT_SERVER.to_owned())
}

fn collector(root: &Path) -> Result<Collector, String> {
    let state = env_path("STATE_DIRECTORY", "/var/lib/vilna-reports");
    Ok(Collector {
        spool: Spool::open(&state).map_err(|e| format!("{}: {e}", state.display()))?,
        transport: Box::new(Curl::new(&server())),
        root: root.to_path_buf(),
        device: device::this_device(root),
    })
}

fn run() -> Result<(), String> {
    let root = Path::new("/");
    let c = Arc::new(Mutex::new(collector(root)?));
    let socket = socket_path();
    let _ = std::fs::remove_file(&socket);
    let listener = UnixListener::bind(&socket).map_err(|e| format!("{}: {e}", socket.display()))?;
    // Every person may connect; the kernel tells who did (SO_PEERCRED).
    std::fs::set_permissions(&socket, std::fs::Permissions::from_mode(0o666))
        .map_err(|e| e.to_string())?;
    let served = c.clone();
    std::thread::spawn(move || control::serve(&served, &listener));
    let state = env_path("STATE_DIRECTORY", "/var/lib/vilna-reports");
    let cursor = state.join("journal.cursor");
    loop {
        let found = journal::new_crashes(&cursor).unwrap_or_else(|e| {
            eprintln!("vilna-reportd: {e}");
            Vec::new()
        });
        if let Ok(c) = c.lock() {
            for entry in &found {
                match c.found(entry, now()) {
                    Ok(Some(id)) => eprintln!("vilna-reportd: kept crash report {id}"),
                    Ok(None) => {}
                    Err(why) => eprintln!("vilna-reportd: a crash gave no report: {why}"),
                }
            }
            if let Err(e) = c.spool.prune(now()) {
                eprintln!("vilna-reportd: could not delete old reports: {e}");
            }
            let sent = c.send_waiting(now());
            if sent > 0 {
                eprintln!("vilna-reportd: sent {sent} crash reports (the answer is auto)");
            }
        }
        std::thread::sleep(ROUND);
    }
}

fn print(value: &Value) -> ExitCode {
    println!(
        "{}",
        serde_json::to_string_pretty(value).unwrap_or_else(|_| value.to_string())
    );
    if value.get("error").is_some() {
        ExitCode::FAILURE
    } else {
        ExitCode::SUCCESS
    }
}

fn ask(request: Value) -> ExitCode {
    match control::ask(&socket_path(), &request) {
        Ok(answer) => print(&answer),
        Err(e) => {
            eprintln!("vilna-reportd: {e}");
            ExitCode::FAILURE
        }
    }
}

fn parse(source: &str) -> ExitCode {
    let entries = if source == "-" {
        journal::entries(std::io::stdin())
    } else {
        match std::fs::File::open(source) {
            Ok(f) => journal::entries(f),
            Err(e) => {
                eprintln!("vilna-reportd: {source}: {e}");
                return ExitCode::FAILURE;
            }
        }
    };
    let dev = device::this_device(Path::new("/"));
    let mut out = Vec::new();
    for entry in &entries {
        let id = device::new_id().unwrap_or_default();
        match coredump::report(entry, &dev, &id) {
            Ok(found) => out.push(found.crash.to_wire()),
            Err(coredump::Skip::NotACrash) => {}
            Err(skip) => out.push(json!({ "skipped": format!("{skip:?}") })),
        }
    }
    print(&json!(out))
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let arg = |i: usize| args.get(i).map(String::as_str).unwrap_or_default();
    match arg(0) {
        "run" => match run() {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                eprintln!("vilna-reportd: {e}");
                ExitCode::FAILURE
            }
        },
        "consent" if args.len() == 1 => {
            let answer = consent::read(Path::new("/"));
            print(&json!({ "consent": answer.map(Consent::as_str) }))
        }
        "consent" => match Consent::parse(arg(1)) {
            Some(answer) => match consent::write(Path::new("/"), answer) {
                Ok(_) => print(&json!({ "consent": answer.as_str() })),
                Err(e) => {
                    eprintln!("vilna-reportd: cannot write the answer (run as root): {e}");
                    ExitCode::FAILURE
                }
            },
            None => {
                eprintln!("vilna-reportd: the answer is auto, ask or never");
                ExitCode::from(2)
            }
        },
        op @ ("status" | "list" | "system" | "forget") => ask(json!({ "op": op })),
        op @ ("show" | "send" | "discard") if args.len() == 2 => {
            ask(json!({ "op": op, "id": arg(1) }))
        }
        "feedback" => {
            let mut text = String::new();
            let read = std::io::stdin()
                .take(control::MAX_REQUEST)
                .read_to_string(&mut text);
            match read
                .ok()
                .and_then(|_| serde_json::from_str::<Value>(&text).ok())
            {
                Some(body) => ask(json!({ "op": "feedback", "feedback": body })),
                None => {
                    eprintln!("vilna-reportd: feedback is one JSON object on stdin");
                    ExitCode::from(2)
                }
            }
        }
        "parse" if args.len() == 2 => parse(arg(1)),
        _ => {
            eprintln!("{USAGE}");
            ExitCode::from(2)
        }
    }
}
