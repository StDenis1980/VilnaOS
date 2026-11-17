// SPDX-License-Identifier: GPL-3.0-or-later
//! The second question for what changes the whole computer (permissions.md
//! 9.2): after the person said yes in Herkos's window, polkit decides whether
//! they may do it at all, with Herkos's own actions `org.vilna.herkos.*`
//! (`herkos/systemd/org.vilna.herkos.policy`).
//!
//! Herkos's window says what and why; polkit says who. The subject polkit
//! judges is the person's session agent (`herkos-agent`), so polkit sees their
//! session: whether they sit at the computer (active, local) and whether they
//! are an administrator. When an action needs a password, polkit's own
//! authentication agent of the desktop (GNOME Shell) asks for it, not Herkos
//! and not Gloxi: Herkos never sees the password. The polkit policy names the
//! user `herkos` as the owner of these actions, which lets `herkosd` (not
//! root) ask polkit about another person's process.
//!
//! `herkosd` asks through `pkcheck` rather than D-Bus, as it runs `systemctl`:
//! no D-Bus library (ADR 0013 keeps the dependencies to serde_json and
//! rustix).

use std::io::Read;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::system::Agents;

/// What polkit said.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Verdict {
    Yes,
    /// Not authorized, or the person closed polkit's window.
    No(String),
    /// Nobody answered polkit's window in time.
    TimedOut,
    /// polkit could not be asked: no session agent, no polkit, a broken reply.
    Error(String),
}

pub trait Authority: Send + Sync {
    /// Whether the person may carry out `action`; may show polkit's window
    /// and wait up to `timeout` seconds for it. `details` are shown to
    /// polkit's rules (`action.lookup`) and may be named in its message.
    fn check(&self, action: &str, details: &[(&str, &str)], timeout: f64) -> Verdict;
}

/// At most this much of what `pkcheck` says goes into the message.
const MAX_STDERR: u64 = 2048;
const POLL: Duration = Duration::from_millis(100);

/// Asks polkit with `pkcheck` about the person's session agent.
pub struct Pkcheck {
    /// `pkcheck`, or a stand-in in tests.
    pub program: PathBuf,
    pub uid: u32,
    pub agents: Arc<Agents>,
}

impl Authority for Pkcheck {
    fn check(&self, action: &str, details: &[(&str, &str)], timeout: f64) -> Verdict {
        let Some(pid) = self.agents.pid(self.uid) else {
            return Verdict::Error(
                "the person's session agent is not running, so polkit cannot ask them".into(),
            );
        };
        let start = match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
            Ok(stat) => match start_time(&stat) {
                Some(s) => s,
                None => return Verdict::Error(format!("cannot read /proc/{pid}/stat")),
            },
            Err(e) => return Verdict::Error(format!("the session agent is gone: {e}")),
        };
        let mut cmd = Command::new(&self.program);
        cmd.arg("--action-id")
            .arg(action)
            // pid, start time and uid: polkit checks all three, so a pid
            // reused by another process is not taken for the agent.
            .arg("--process")
            .arg(format!("{pid},{start},{}", self.uid))
            .arg("--allow-user-interaction");
        for (k, v) in details {
            cmd.arg("--detail").arg(k).arg(v);
        }
        run(cmd, timeout)
    }
}

/// Runs `pkcheck` and reads its exit status (pkcheck(1)): 0 authorized,
/// 1 not authorized, 2 a challenge (only without user interaction),
/// 3 dismissed, anything else an error.
fn run(mut cmd: Command, timeout: f64) -> Verdict {
    cmd.stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return Verdict::Error(format!("cannot run pkcheck: {e}")),
    };
    let timeout = if timeout.is_finite() && timeout > 0.0 {
        Duration::from_secs_f64(timeout.min(86_400.0))
    } else {
        Duration::ZERO
    };
    let begun = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) if begun.elapsed() >= timeout => {
                // polkit drops the question when its caller goes away.
                let _ = child.kill();
                let _ = child.wait();
                return Verdict::TimedOut;
            }
            Ok(None) => std::thread::sleep(POLL),
            Err(e) => {
                let _ = child.kill();
                return Verdict::Error(format!("pkcheck: {e}"));
            }
        }
    };
    let mut said = String::new();
    if let Some(err) = child.stderr.take() {
        let _ = err.take(MAX_STDERR).read_to_string(&mut said);
    }
    let said = said.trim();
    match status.code() {
        Some(0) => Verdict::Yes,
        Some(1) | Some(2) => Verdict::No(if said.is_empty() {
            "not authorized".into()
        } else {
            format!("not authorized: {said}")
        }),
        Some(3) => Verdict::No("the authentication window was closed".into()),
        _ => Verdict::Error(if said.is_empty() {
            format!("pkcheck failed ({status})")
        } else {
            format!("pkcheck failed: {said}")
        }),
    }
}

/// Field 22 of `/proc/<pid>/stat`, the start time in clock ticks. The
/// command name (field 2) may hold spaces and parentheses, so the fields are
/// counted after its last `)`.
pub fn start_time(stat: &str) -> Option<u64> {
    let (_, rest) = stat.rsplit_once(')')?;
    rest.split_whitespace().nth(19)?.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn the_start_time_is_found_after_the_command_name() {
        let stat =
            "4242 (a (b) c) S 1 4242 4242 0 -1 4194560 100 0 0 0 1 2 0 0 20 0 1 0 987654 1000 10";
        assert_eq!(start_time(stat), Some(987_654));
        assert_eq!(start_time("4242 (x"), None);
        assert_eq!(start_time("4242 (x) S 1"), None);
        let own = std::fs::read_to_string("/proc/self/stat").expect("stat");
        assert!(start_time(&own).is_some_and(|s| s > 0));
    }

    fn fake(dir: &std::path::Path, script: &str) -> PathBuf {
        std::fs::create_dir_all(dir).expect("dir");
        let p = dir.join("pkcheck");
        std::fs::write(&p, format!("#!/bin/sh\n{script}\n")).expect("write");
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).expect("chmod");
        p
    }

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("herkosd-polkit-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        d
    }

    #[test]
    fn exit_statuses_mean_what_pkcheck_1_says() {
        let d = tmp("codes");
        for (code, want) in [
            ("0", Verdict::Yes),
            ("1", Verdict::No("not authorized: nope".into())),
            (
                "3",
                Verdict::No("the authentication window was closed".into()),
            ),
            ("4", Verdict::Error("pkcheck failed: nope".into())),
        ] {
            let p = fake(&d.join(code), &format!("echo nope >&2; exit {code}"));
            assert_eq!(run(Command::new(p), 5.0), want, "{code}");
        }
        let missing = Command::new(d.join("none"));
        assert!(matches!(run(missing, 5.0), Verdict::Error(_)));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn a_window_nobody_answers_times_out() {
        let d = tmp("slow");
        let p = fake(&d, "exec sleep 30");
        let begun = Instant::now();
        assert_eq!(run(Command::new(p), 0.3), Verdict::TimedOut);
        assert!(begun.elapsed() < Duration::from_secs(5));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn asks_about_the_session_agent_with_its_start_time() {
        let d = tmp("args");
        let out = d.join("args");
        let p = fake(&d, &format!("printf '%s\\n' \"$@\" > {}", out.display()));
        let agents = Arc::new(Agents::default());
        let check = Pkcheck {
            program: p,
            uid: 1000,
            agents: Arc::clone(&agents),
        };
        // No agent: polkit has nobody to ask.
        assert!(matches!(
            check.check("org.vilna.herkos.apps.change", &[], 5.0),
            Verdict::Error(_)
        ));
        let pid = std::process::id();
        let serial = agents.register_pid_for_tests(1000, pid as i32);
        let v = check.check(
            "org.vilna.herkos.apps.change",
            &[("operation", "install")],
            5.0,
        );
        assert_eq!(v, Verdict::Yes);
        let args = std::fs::read_to_string(&out).expect("args");
        let args: Vec<&str> = args.lines().collect();
        let start =
            start_time(&std::fs::read_to_string("/proc/self/stat").expect("stat")).expect("start");
        assert_eq!(
            args,
            [
                "--action-id",
                "org.vilna.herkos.apps.change",
                "--process",
                &format!("{pid},{start},1000"),
                "--allow-user-interaction",
                "--detail",
                "operation",
                "install",
            ]
        );
        agents.unregister_for_tests(1000, serial);
        let _ = std::fs::remove_dir_all(&d);
    }
}
