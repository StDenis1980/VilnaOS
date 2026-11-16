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
use vilna_netguard::portal_dns::{self, Connectivity};
use vilna_netguard::ruleset::{self, PROVIDER_SERVICES, TABLE};
use vilna_netguard::state::{Lan, State};

const USAGE: &str = "usage: vilna-netguard [--state PATH] [--nft PATH] [--wg PATH] [--cgroup-root DIR] [--resolved-dir DIR] [--systemctl PATH] [--ip PATH] [--resolvectl PATH] [--nmcli PATH] [--portal-dns FILE] COMMAND\n\
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
    ip: PathBuf,
    resolvectl: PathBuf,
    nmcli: PathBuf,
    /// The links whose DNS a portal window opened (see `portal_dns`).
    portal_dns: PathBuf,
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
        ip: PathBuf::from("/usr/sbin/ip"),
        resolvectl: PathBuf::from("/usr/bin/resolvectl"),
        nmcli: PathBuf::from("/usr/bin/nmcli"),
        portal_dns: PathBuf::from("/var/lib/vilna-netguard/portal-dns"),
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
            "--ip" => paths.ip = value,
            "--resolvectl" => paths.resolvectl = value,
            "--nmcli" => paths.nmcli = value,
            "--portal-dns" => paths.portal_dns = value,
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
            v["portal_dns"] = relaxed_links(paths).into_iter().collect::<Vec<_>>().into();
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
    let mut auto = portal_dns::AutoPortal::default();
    loop {
        let state = load(&paths.state);
        let now = now();
        let next = script(paths, &state, now);
        if next != last {
            match load_script(paths, &next) {
                Ok(()) => last = next,
                Err(e) => eprintln!("vilna-netguard: {e}"),
            }
        }
        if let Err(e) = portal_tick(paths, &state, now, &mut auto) {
            eprintln!("vilna-netguard: {e}");
        }
        thread::sleep(every);
    }
}

/// One look at the captive-portal side of things: the person's window
/// (Herkos), the automatic one (kill switch off, the network asks for a
/// login), and the DNS of the network links that follows from both.
fn portal_tick(
    paths: &Paths,
    state: &State,
    now: u64,
    auto: &mut portal_dns::AutoPortal,
) -> Result<(), String> {
    let person = state.portal_open(now);
    // With the kill switch on and no window, only a leftover needs closing.
    if state.enabled && !person && relaxed_links(paths).is_empty() {
        auto.step(now, true, Connectivity::Other, &BTreeSet::new());
        return Ok(());
    }
    let links = network_links(paths);
    let connectivity = if state.enabled {
        Connectivity::Other
    } else {
        run_command(
            Command::new(&paths.nmcli).args(["-t", "-f", "CONNECTIVITY", "general"]),
            None,
        )
        .map(|out| Connectivity::parse(&out))
        .unwrap_or(Connectivity::Other)
    };
    let step = auto.step(now, state.enabled, connectivity, &links);
    let want = if person || step.open {
        links
    } else {
        BTreeSet::new()
    };
    sync_portal_dns(paths, &want)?;
    if step.recheck {
        // NetworkManager checks again with the network's DNS, finds the
        // login page and GNOME opens it. It may take a few seconds.
        run_command(
            Command::new(&paths.nmcli).args(["networking", "connectivity", "check"]),
            None,
        )?;
    }
    Ok(())
}

/// The network links with a default route now (never the tunnels).
fn network_links(paths: &Paths) -> BTreeSet<String> {
    let routes: Vec<String> = ["-4", "-6"]
        .iter()
        .filter_map(|family| {
            run_command(
                Command::new(&paths.ip).args(["-j", family, "route", "show", "default"]),
                None,
            )
            .ok()
        })
        .collect();
    let routes: Vec<&str> = routes.iter().map(String::as_str).collect();
    portal_dns::default_links(&routes)
}

fn relaxed_links(paths: &Paths) -> BTreeSet<String> {
    fs::File::open(&paths.portal_dns)
        .ok()
        .and_then(|f| {
            let mut t = String::new();
            f.take(MAX_STATE).read_to_string(&mut t).ok().map(|_| t)
        })
        .map(|t| portal_dns::parse_relaxed(&t))
        .unwrap_or_default()
}

/// During a portal window the network links take plain DNS from the
/// network, so the login page can open; after it they are strict again.
/// Only the service does this, every tick: a link that came up during the
/// window is opened too, and a window that ended closes everything, even
/// after a restart (the list is kept in a file).
fn sync_portal_dns(paths: &Paths, want: &BTreeSet<String>) -> Result<(), String> {
    let relaxed = relaxed_links(paths);
    if want.is_empty() && relaxed.is_empty() {
        return Ok(());
    }
    let mut now_relaxed = relaxed.clone();
    let mut failed = None;
    for args in portal_dns::changes(&relaxed, want) {
        match run_command(Command::new(&paths.resolvectl).args(&args), None) {
            Ok(_) if args[2] == "no" => {
                now_relaxed.insert(args[1].clone());
            }
            Ok(_) => {
                now_relaxed.remove(&args[1]);
            }
            // A link that went away is strict again when it comes back.
            Err(_) if args[2] == "yes" && !link_exists(&args[1]) => {
                now_relaxed.remove(&args[1]);
            }
            Err(e) => failed = Some(e),
        }
    }
    if now_relaxed != relaxed {
        write_atomic(
            &paths.portal_dns,
            &portal_dns::render_relaxed(&now_relaxed),
            0o600,
        )?;
    }
    failed.map_or(Ok(()), Err)
}

fn link_exists(name: &str) -> bool {
    Path::new("/sys/class/net").join(name).exists()
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
            ip: dir.join("ip"),
            resolvectl: dir.join("resolvectl"),
            nmcli: dir.join("nmcli"),
            portal_dns: dir.join("portal-dns"),
        };
        (p, dir)
    }

    fn script_file(path: &Path, body: &str) {
        fs::write(path, format!("#!/bin/sh\n{body}\n")).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    }

    /// A Wi-Fi link and a Mullvad tunnel have default routes; resolvectl logs.
    fn network(name: &str) -> (Paths, PathBuf) {
        let (p, dir) = paths(name);
        script_file(
            &p.ip,
            r#"case "$2" in -4) echo '[{"dst":"default","dev":"wlp2s0"},{"dst":"default","dev":"wg0-mullvad"}]';; *) echo '[]';; esac"#,
        );
        script_file(
            &p.resolvectl,
            &format!("echo \"$@\" >> {}/resolvectl.log", dir.display()),
        );
        let d = dir.display();
        script_file(
            &p.nmcli,
            &format!(
                "if [ \"$1\" = -t ]; then cat {d}/connectivity 2>/dev/null || echo full; else echo \"$@\" >> {d}/nmcli.log; fi"
            ),
        );
        (p, dir)
    }

    fn nmcli_log(dir: &Path) -> String {
        fs::read_to_string(dir.join("nmcli.log")).unwrap_or_default()
    }

    fn resolvectl_log(dir: &Path) -> String {
        fs::read_to_string(dir.join("resolvectl.log")).unwrap_or_default()
    }

    fn tick(p: &Paths, state: &State, now: u64, auto: &mut portal_dns::AutoPortal) {
        portal_tick(p, state, now, auto).unwrap();
    }

    fn connectivity(dir: &Path, word: &str) {
        fs::write(dir.join("connectivity"), word).unwrap();
    }

    #[test]
    fn a_portal_window_opens_the_network_dns_and_closes_it_after() {
        let (p, dir) = network("portal-dns");
        let mut auto = portal_dns::AutoPortal::default();
        let mut state = State {
            enabled: true,
            ..State::default()
        };
        state.open_portal(1000, 300).unwrap();
        tick(&p, &state, 1000, &mut auto);
        assert_eq!(resolvectl_log(&dir), "dnsovertls wlp2s0 no\n");
        assert_eq!(relaxed_links(&p).len(), 1);
        // Nothing more while the window lasts.
        tick(&p, &state, 1100, &mut auto);
        assert_eq!(resolvectl_log(&dir), "dnsovertls wlp2s0 no\n");
        // The window ended: strict again, and the list is empty.
        tick(&p, &state, 1300, &mut auto);
        assert_eq!(
            resolvectl_log(&dir),
            "dnsovertls wlp2s0 no\ndnsovertls wlp2s0 yes\n"
        );
        assert!(relaxed_links(&p).is_empty());
        // With the kill switch on, NetworkManager is never asked.
        assert!(nmcli_log(&dir).is_empty());
    }

    #[test]
    fn with_the_kill_switch_off_a_login_page_opens_the_dns_by_itself() {
        let (p, dir) = network("auto");
        let mut auto = portal_dns::AutoPortal::default();
        let state = State::default();
        tick(&p, &state, 1000, &mut auto);
        assert!(resolvectl_log(&dir).is_empty(), "full: nothing to do");
        connectivity(&dir, "limited");
        tick(&p, &state, 1002, &mut auto);
        assert_eq!(resolvectl_log(&dir), "dnsovertls wlp2s0 no\n");
        assert!(nmcli_log(&dir).contains("networking connectivity check"));
        // The person logged in.
        connectivity(&dir, "full");
        tick(&p, &state, 1040, &mut auto);
        assert!(resolvectl_log(&dir).ends_with("dnsovertls wlp2s0 yes\n"));
        assert!(relaxed_links(&p).is_empty());
    }

    #[test]
    fn with_the_kill_switch_on_a_login_page_waits_for_the_person() {
        let (p, dir) = network("auto-off");
        let mut auto = portal_dns::AutoPortal::default();
        let state = State {
            enabled: true,
            ..State::default()
        };
        connectivity(&dir, "portal");
        tick(&p, &state, 1000, &mut auto);
        assert!(resolvectl_log(&dir).is_empty());
        assert!(nmcli_log(&dir).is_empty());
    }

    #[test]
    fn without_a_window_nothing_is_touched() {
        let (p, dir) = network("no-portal");
        tick(&p, &State::default(), 1000, &mut Default::default());
        assert!(resolvectl_log(&dir).is_empty());
        assert!(!p.portal_dns.exists());
    }

    #[test]
    fn a_failed_close_is_tried_again() {
        let (p, dir) = network("retry");
        // A window opened earlier for a link that exists in every test
        // machine, the loopback; resolvectl fails for now.
        fs::write(&p.portal_dns, "lo\n").unwrap();
        script_file(&p.resolvectl, "exit 1");
        let mut auto = portal_dns::AutoPortal::default();
        assert!(portal_tick(&p, &State::default(), 1300, &mut auto).is_err());
        assert_eq!(relaxed_links(&p).len(), 1, "kept, to try again");
        script_file(
            &p.resolvectl,
            &format!("echo \"$@\" >> {}/resolvectl.log", dir.display()),
        );
        tick(&p, &State::default(), 1302, &mut auto);
        assert_eq!(resolvectl_log(&dir), "dnsovertls lo yes\n");
        assert!(relaxed_links(&p).is_empty());
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
