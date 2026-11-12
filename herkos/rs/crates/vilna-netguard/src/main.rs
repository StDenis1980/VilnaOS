// SPDX-License-Identifier: GPL-3.0-or-later
//! `vilna-netguard`: the VPN kill switch of Vilna OS (ADR 0027, section 3).
//!
//! It runs as root. `vilna-netguard watch` is the service
//! (`vilna-netguard.service`): it keeps the nftables table in step with the
//! person's decision and with the tunnels that are up. The other commands
//! change the decision; only Herkos calls them, after the person confirmed
//! (ADR 0027, section 4), through its root tool unit.
#![deny(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::unimplemented,
    clippy::todo,
    clippy::unreachable
)]

use std::collections::BTreeSet;
use std::fs;
use std::io::{Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use vilna_netguard::endpoints::parse_wg_endpoints;
use vilna_netguard::ruleset::{self, PROVIDER_SERVICES, TABLE};
use vilna_netguard::state::{Lan, State};

const USAGE: &str = "usage: vilna-netguard [--state PATH] [--nft PATH] [--wg PATH] [--cgroup-root DIR] COMMAND\n\
commands:\n  status\n  apply\n  watch [SECONDS]\n  on | off\n  allow-lan CIDR | forget-lan CIDR\n  portal SECONDS";

const COMMAND_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_OUTPUT: u64 = 1024 * 1024;
const MAX_STATE: u64 = 64 * 1024;

struct Paths {
    state: PathBuf,
    nft: PathBuf,
    wg: PathBuf,
    cgroup_root: PathBuf,
}

fn main() -> ExitCode {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let mut paths = Paths {
        state: PathBuf::from("/var/lib/vilna-netguard/state.json"),
        nft: PathBuf::from("/usr/sbin/nft"),
        wg: PathBuf::from("/usr/bin/wg"),
        cgroup_root: PathBuf::from("/sys/fs/cgroup"),
    };
    while args.first().is_some_and(|a| a.starts_with("--")) {
        let flag = args.remove(0);
        let Some(value) = (!args.is_empty()).then(|| PathBuf::from(args.remove(0))) else {
            eprintln!("{flag} needs a value\n{USAGE}");
            return ExitCode::from(2);
        };
        match flag.as_str() {
            "--state" => paths.state = value,
            "--nft" => paths.nft = value,
            "--wg" => paths.wg = value,
            "--cgroup-root" => paths.cgroup_root = value,
            _ => {
                eprintln!("unknown option {flag}\n{USAGE}");
                return ExitCode::from(2);
            }
        }
    }
    let words: Vec<&str> = args.iter().map(String::as_str).collect();
    match run(&paths, &words) {
        Ok(out) => {
            if !out.is_empty() {
                println!("{out}");
            }
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("vilna-netguard: {e}");
            ExitCode::FAILURE
        }
    }
}

fn run(paths: &Paths, words: &[&str]) -> Result<String, String> {
    let now = now();
    match words {
        ["status"] => {
            let state = load(&paths.state);
            let mut v = state.to_json();
            v["portal_open"] = state.portal_open(now).into();
            v["applied"] = table_present(paths).into();
            Ok(v.to_string())
        }
        ["apply"] => apply(paths, &load(&paths.state), now).map(|_| String::new()),
        ["watch"] => watch(paths, Duration::from_secs(2)),
        ["watch", seconds] => {
            let s: u64 = seconds
                .parse()
                .ok()
                .filter(|s| (1..=60).contains(s))
                .ok_or("watch takes 1 to 60 seconds")?;
            watch(paths, Duration::from_secs(s))
        }
        ["on"] => change(paths, now, |s| {
            s.enabled = true;
            s.portal_until = 0;
            Ok(())
        }),
        ["off"] => change(paths, now, |s| {
            s.enabled = false;
            Ok(())
        }),
        ["allow-lan", cidr] => {
            let lan = Lan::parse(cidr)?;
            change(paths, now, |s| s.allow_lan(lan))
        }
        ["forget-lan", cidr] => {
            let lan = Lan::parse(cidr)?;
            change(paths, now, |s| {
                s.forget_lan(lan);
                Ok(())
            })
        }
        ["portal", seconds] => {
            let seconds: u64 = seconds.parse().map_err(|_| "portal takes seconds")?;
            change(paths, now, |s| s.open_portal(now, seconds))
        }
        _ => Err(USAGE.to_owned()),
    }
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn load(path: &Path) -> State {
    let mut text = String::new();
    match fs::File::open(path) {
        Ok(f) => {
            if f.take(MAX_STATE).read_to_string(&mut text).is_err() {
                return State::default();
            }
            State::from_json(&text)
        }
        Err(_) => State::default(),
    }
}

/// Write the state next to its place and rename it there: a crash leaves
/// either the old file or the new one.
fn save(path: &Path, state: &State) -> Result<(), String> {
    let folder = path.parent().ok_or("the state path has no folder")?;
    fs::create_dir_all(folder).map_err(|e| format!("{}: {e}", folder.display()))?;
    let tmp = path.with_extension("json.new");
    let mut f = fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&tmp)
        .map_err(|e| format!("{}: {e}", tmp.display()))?;
    f.write_all(state.to_json().to_string().as_bytes())
        .and_then(|()| f.sync_all())
        .map_err(|e| format!("{}: {e}", tmp.display()))?;
    fs::rename(&tmp, path).map_err(|e| format!("{}: {e}", path.display()))
}

fn change(
    paths: &Paths,
    now: u64,
    edit: impl FnOnce(&mut State) -> Result<(), String>,
) -> Result<String, String> {
    let mut state = load(&paths.state);
    edit(&mut state)?;
    save(&paths.state, &state)?;
    apply(paths, &state, now)?;
    Ok(state.to_json().to_string())
}

/// Provider services that are running now: nft refuses a cgroup that does not exist.
fn running_services(cgroup_root: &Path) -> Vec<&'static str> {
    PROVIDER_SERVICES
        .iter()
        .copied()
        .filter(|s| cgroup_root.join("system.slice").join(s).is_dir())
        .collect()
}

fn script(paths: &Paths, state: &State, now: u64) -> String {
    let endpoints = if state.enabled {
        run_command(
            Command::new(&paths.wg).args(["show", "all", "endpoints"]),
            None,
        )
        .map(|out| parse_wg_endpoints(&out))
        .unwrap_or_default()
    } else {
        BTreeSet::new()
    };
    ruleset::render(
        state,
        now,
        &endpoints,
        &running_services(&paths.cgroup_root),
    )
}

fn apply(paths: &Paths, state: &State, now: u64) -> Result<(), String> {
    load_script(paths, &script(paths, state, now))
}

fn load_script(paths: &Paths, script: &str) -> Result<(), String> {
    run_command(Command::new(&paths.nft).args(["-f", "-"]), Some(script)).map(|_| ())
}

fn table_present(paths: &Paths) -> bool {
    let mut parts = TABLE.split(' ');
    let (Some(family), Some(name)) = (parts.next(), parts.next()) else {
        return false;
    };
    run_command(
        Command::new(&paths.nft).args(["list", "table", family, name]),
        None,
    )
    .is_ok()
}

/// The service: apply whenever the script would change (a tunnel came up
/// with a new server, the provider service started, a portal window ended,
/// Herkos changed the decision). A failed apply is retried on the next tick.
fn watch(paths: &Paths, every: Duration) -> Result<String, String> {
    let mut last = String::new();
    loop {
        let state = load(&paths.state);
        let next = script(paths, &state, now());
        if next != last {
            match load_script(paths, &next) {
                Ok(()) => last = next,
                Err(e) => eprintln!("vilna-netguard: {e}"),
            }
        }
        thread::sleep(every);
    }
}

fn run_command(command: &mut Command, input: Option<&str>) -> Result<String, String> {
    let name = command.get_program().to_string_lossy().into_owned();
    let mut child = command
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("{name}: {e}"))?;
    if let (Some(text), Some(mut stdin)) = (input, child.stdin.take()) {
        stdin
            .write_all(text.as_bytes())
            .map_err(|e| format!("{name}: {e}"))?;
    }
    let reader = |pipe: Option<Box<dyn Read + Send>>| {
        thread::spawn(move || {
            let mut buf = Vec::new();
            if let Some(p) = pipe {
                let _ = p.take(MAX_OUTPUT).read_to_end(&mut buf);
            }
            buf
        })
    };
    let out = reader(child.stdout.take().map(|p| Box::new(p) as _));
    let err = reader(child.stderr.take().map(|p| Box::new(p) as _));
    let start = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if start.elapsed() < COMMAND_TIMEOUT => {
                thread::sleep(Duration::from_millis(20));
            }
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("{name} did not finish in time"));
            }
            Err(e) => return Err(format!("{name}: {e}")),
        }
    };
    let out = out.join().unwrap_or_default();
    let err = err.join().unwrap_or_default();
    if status.success() {
        Ok(String::from_utf8_lossy(&out).into_owned())
    } else {
        Err(format!(
            "{name} failed: {}",
            String::from_utf8_lossy(&err).trim()
        ))
    }
}
