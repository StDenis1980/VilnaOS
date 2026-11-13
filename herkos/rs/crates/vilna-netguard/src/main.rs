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

use vilna_netguard::dns::{OVERRIDE_FILE, Resolver};
use vilna_netguard::endpoints::parse_wg_endpoints;
use vilna_netguard::ruleset::{self, PROVIDER_SERVICES, TABLE};
use vilna_netguard::state::{Lan, State};

const USAGE: &str = "usage: vilna-netguard [--state PATH] [--nft PATH] [--wg PATH] [--cgroup-root DIR] [--resolved-dir DIR] [--systemctl PATH] COMMAND\n\
commands:\n  status\n  apply\n  watch [SECONDS]\n  on | off\n  allow-lan CIDR | forget-lan CIDR\n  portal SECONDS\n  dns quad9|mullvad\n  provider mullvad";

const COMMAND_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_OUTPUT: u64 = 1024 * 1024;
const MAX_STATE: u64 = 64 * 1024;

struct Paths {
    state: PathBuf,
    nft: PathBuf,
    wg: PathBuf,
    cgroup_root: PathBuf,
    resolved_dir: PathBuf,
    systemctl: PathBuf,
}

fn main() -> ExitCode {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let mut paths = Paths {
        state: PathBuf::from("/var/lib/vilna-netguard/state.json"),
        nft: PathBuf::from("/usr/sbin/nft"),
        wg: PathBuf::from("/usr/bin/wg"),
        cgroup_root: PathBuf::from("/sys/fs/cgroup"),
        resolved_dir: PathBuf::from("/etc/systemd/resolved.conf.d"),
        systemctl: PathBuf::from("/usr/bin/systemctl"),
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
            "--resolved-dir" => paths.resolved_dir = value,
            "--systemctl" => paths.systemctl = value,
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
            v["dns"] = current_resolver(paths).name().into();
            // How many WireGuard servers the tunnels talk to now: 0 means no VPN is up,
            // and turning the kill switch on would leave no way out.
            v["tunnels"] = run_command(
                Command::new(&paths.wg).args(["show", "all", "endpoints"]),
                None,
            )
            .map(|out| parse_wg_endpoints(&out).len())
            .unwrap_or(0)
            .into();
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
        ["dns", name] => {
            let resolver = Resolver::parse(name)?;
            set_resolver(paths, resolver)?;
            Ok(serde_json::json!({ "dns": resolver.name() }).to_string())
        }
        ["provider", "mullvad"] => {
            // The person chose Mullvad (ADR 0027, section 2): its service,
            // off until now, starts and comes back after a reboot. Proton
            // needs nothing here: its app runs in the person's session.
            run_command(
                Command::new(&paths.systemctl).args(["enable", "--now", "mullvad-daemon.service"]),
                None,
            )?;
            Ok(serde_json::json!({ "provider": "mullvad", "service": "started" }).to_string())
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

/// Write next to its place and rename it there: a crash leaves either the
/// old file or the new one.
fn write_atomic(path: &Path, text: &str, mode: u32) -> Result<(), String> {
    let folder = path.parent().ok_or("the path has no folder")?;
    fs::create_dir_all(folder).map_err(|e| format!("{}: {e}", folder.display()))?;
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(".new");
    let tmp = PathBuf::from(tmp);
    let mut f = fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(mode)
        .open(&tmp)
        .map_err(|e| format!("{}: {e}", tmp.display()))?;
    f.write_all(text.as_bytes())
        .and_then(|()| f.sync_all())
        .map_err(|e| format!("{}: {e}", tmp.display()))?;
    fs::rename(&tmp, path).map_err(|e| format!("{}: {e}", path.display()))
}

fn save(path: &Path, state: &State) -> Result<(), String> {
    write_atomic(path, &state.to_json().to_string(), 0o600)
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

fn current_resolver(paths: &Paths) -> Resolver {
    let text = fs::File::open(paths.resolved_dir.join(OVERRIDE_FILE))
        .ok()
        .and_then(|f| {
            let mut t = String::new();
            f.take(MAX_STATE).read_to_string(&mut t).ok().map(|_| t)
        });
    Resolver::from_override(text.as_deref())
}

/// Write or remove the override and let systemd-resolved read it again.
fn set_resolver(paths: &Paths, resolver: Resolver) -> Result<(), String> {
    let path = paths.resolved_dir.join(OVERRIDE_FILE);
    match resolver.override_file() {
        Some(text) => write_atomic(&path, &text, 0o644)?,
        None => match fs::remove_file(&path) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(format!("{}: {e}", path.display())),
        },
    }
    run_command(
        Command::new(&paths.systemctl).args(["try-reload-or-restart", "systemd-resolved.service"]),
        None,
    )
    .map(|_| ())
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

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    /// Paths whose systemctl is a script that logs its arguments.
    fn paths(name: &str) -> (Paths, PathBuf) {
        let dir = std::env::temp_dir().join(format!("netguard-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let systemctl = dir.join("systemctl");
        fs::write(
            &systemctl,
            format!("#!/bin/sh\necho \"$@\" >> {}/log\n", dir.display()),
        )
        .unwrap();
        fs::set_permissions(&systemctl, fs::Permissions::from_mode(0o755)).unwrap();
        let p = Paths {
            state: dir.join("state.json"),
            nft: dir.join("nft"),
            wg: dir.join("wg"),
            cgroup_root: dir.join("cgroup"),
            resolved_dir: dir.join("resolved"),
            systemctl,
        };
        (p, dir)
    }

    #[test]
    fn choosing_mullvad_starts_and_enables_its_service() {
        let (p, dir) = paths("provider");
        let out = run(&p, &["provider", "mullvad"]).unwrap();
        assert!(out.contains("\"started\""));
        let log = fs::read_to_string(dir.join("log")).unwrap();
        assert_eq!(log, "enable --now mullvad-daemon.service\n");
    }

    #[test]
    fn no_other_service_can_be_started() {
        let (p, dir) = paths("other");
        for words in [
            &["provider", "proton"][..],
            &["provider", "sshd"],
            &["provider"],
            &["provider", "mullvad", "x"],
        ] {
            assert!(run(&p, words).is_err(), "{words:?}");
        }
        assert!(!dir.join("log").exists());
    }
}
