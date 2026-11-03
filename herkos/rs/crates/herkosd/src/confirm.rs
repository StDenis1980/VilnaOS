// SPDX-License-Identifier: GPL-3.0-or-later
//! Asking the person (permissions.md 5.2). Mirrors `confirm.py`.
//!
//! The question is shown by Herkos, never by Gloxi, so the model cannot change
//! its wording or answer it. `herkosd` asks through a window program it starts
//! itself for each question (the agent: `herkos-dialog`, the GTK window of
//! Herkos-lite), on the terminal it runs in, or not at all. No answer within
//! the timeout is a "no", and so is anything the window says that is not one
//! of the offered choices.
//!
//! The window protocol: the question as one JSON object on the program's
//! stdin (`Question::to_json`), the answer as the last line of its stdout:
//! `deny`, `once`, `session` or `always`.

use std::fs::OpenOptions;
use std::io::{BufRead, BufReader, IsTerminal, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc::channel;
use std::thread;
use std::time::Duration;

use herkos_core::policy::Raise;

use crate::gate::{Answer, Confirmer, Question};

/// At most this much of the window's output is read.
const MAX_OUTPUT: u64 = 64 * 1024;

/// Ask in a window: a separate program per question.
pub struct Window {
    program: PathBuf,
}

impl Window {
    pub fn new(program: &Path) -> Window {
        Window {
            program: program.to_path_buf(),
        }
    }
}

/// Start the window program. "Text file busy" means a program was just
/// written and another thread forked meanwhile (still holding it open):
/// that passes in a moment, so try again a few times.
fn spawn(program: &Path) -> Option<std::process::Child> {
    for _ in 0..10 {
        match Command::new(program)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
        {
            Ok(child) => return Some(child),
            Err(e) if e.kind() == std::io::ErrorKind::ExecutableFileBusy => {
                thread::sleep(Duration::from_millis(20));
            }
            Err(_) => return None,
        }
    }
    None
}

impl Confirmer for Window {
    fn ask(&self, question: &Question, timeout: f64) -> Answer {
        let Some(mut child) = spawn(&self.program) else {
            return Answer::Deny;
        };
        let (Some(mut stdin), Some(stdout)) = (child.stdin.take(), child.stdout.take()) else {
            let _ = child.kill();
            let _ = child.wait();
            return Answer::Deny;
        };
        let input = serde_json::to_vec(&question.to_json()).unwrap_or_default();
        let (tx, rx) = channel();
        thread::spawn(move || {
            let _ = stdin.write_all(&input);
            drop(stdin);
            let mut out = String::new();
            let _ = stdout.take(MAX_OUTPUT).read_to_string(&mut out);
            let _ = tx.send(out);
        });
        let out = rx.recv_timeout(Duration::from_secs_f64(timeout.max(0.0)));
        // Answered or not, the window goes away now.
        let _ = child.kill();
        let _ = child.wait();
        let Ok(out) = out else {
            return Answer::Deny;
        };
        let last = out
            .lines()
            .map(str::trim)
            .rfind(|l| !l.is_empty())
            .unwrap_or("");
        match Answer::parse(last) {
            Some(a) if question.choices.contains(&a) => a,
            _ => Answer::Deny,
        }
    }
}

fn label(a: Answer) -> &'static str {
    match a {
        Answer::Once => "[o] allow once",
        Answer::Session => "[s] allow for this session",
        Answer::Always => "[a] always allow",
        Answer::Deny => "[n] deny",
    }
}

/// The text Herkos-lite shows on the terminal, untranslated.
pub fn render(q: &Question) -> String {
    let mut lines = vec![
        String::new(),
        "─── Herkos: Gloxi asks for permission ───".to_owned(),
        format!("  {}  ({})", q.summary, q.level),
        format!("  Gloxi's reason: {}", q.reason),
    ];
    lines.extend(q.resources.iter().take(10).map(|r| format!("    • {r}")));
    if q.resources.len() > 10 {
        lines.push(format!("    … and {} more", q.resources.len() - 10));
    }
    lines.push(
        if q.undoable {
            "  Can be undone."
        } else {
            "  Cannot be undone."
        }
        .to_owned(),
    );
    lines.extend(
        q.raises
            .iter()
            .map(|r| format!("  Riskier than usual: {}.", Raise::text(r))),
    );
    if !q.taint_sources.is_empty() {
        lines.push(format!(
            "  Untrusted content came from: {}",
            q.taint_sources.join(", ")
        ));
    }
    let mut options: Vec<&str> = q.choices.iter().map(|c| label(*c)).collect();
    options.push(label(Answer::Deny));
    lines.push(format!("  {}", options.join("   ")));
    lines.join("\n") + "\n> "
}

/// Ask on herkosd's own controlling terminal (not on Gloxi's).
pub struct Terminal;

impl Confirmer for Terminal {
    fn ask(&self, question: &Question, timeout: f64) -> Answer {
        let noctty = rustix::fs::OFlags::NOCTTY.bits();
        let Ok(mut tty) = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(i32::try_from(noctty).unwrap_or(0))
            .open("/dev/tty")
        else {
            return Answer::Deny;
        };
        let _ = tty.write_all(render(question).as_bytes());
        let Ok(reader) = tty.try_clone() else {
            return Answer::Deny;
        };
        let (tx, rx) = channel();
        thread::spawn(move || {
            let mut line = String::new();
            let _ = BufReader::new(reader).take(1024).read_line(&mut line);
            let _ = tx.send(line);
        });
        let Ok(line) = rx.recv_timeout(Duration::from_secs_f64(timeout.max(0.0))) else {
            let _ = tty.write_all("\n  No answer: denied.\n".as_bytes());
            return Answer::Deny;
        };
        let answer = match line.trim().to_lowercase().chars().next() {
            Some('o') => Answer::Once,
            Some('s') => Answer::Session,
            Some('a') => Answer::Always,
            _ => Answer::Deny,
        };
        let answer = if question.choices.contains(&answer) {
            answer
        } else {
            Answer::Deny
        };
        let said = if answer == Answer::Deny {
            "  Denied.\n"
        } else {
            "  Allowed.\n"
        };
        let _ = tty.write_all(said.as_bytes());
        answer
    }
}

/// A desktop session: the window can be shown.
pub fn desktop() -> bool {
    ["WAYLAND_DISPLAY", "DISPLAY"]
        .iter()
        .any(|v| std::env::var_os(v).is_some_and(|x| !x.is_empty()))
}

/// The window if there is a desktop and `herkos-dialog` is installed, else
/// this terminal, else always "no" (as Herkos-lite's `default_confirmer`).
pub fn auto(window: Option<PathBuf>) -> Box<dyn Confirmer> {
    match window {
        Some(w) if desktop() => Box::new(Window::new(&w)),
        _ if std::io::stdin().is_terminal() => Box::new(Terminal),
        _ => Box::new(crate::gate::DenyAll),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use herkos_core::risk::Level;
    use std::os::unix::fs::PermissionsExt;

    fn question(choices: Vec<Answer>) -> Question {
        Question {
            summary: "Read a file".into(),
            reason: "you asked".into(),
            tool: "org.vilna.mcp.files".into(),
            operation: "read".into(),
            resources: vec!["file:///d/a".into()],
            level: Level::R1,
            undoable: true,
            raises: Vec::new(),
            taint_sources: Vec::new(),
            choices,
        }
    }

    fn script(name: &str, body: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("herkosd-confirm-{}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("mkdir");
        let path = dir.join(name);
        std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).expect("write");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).expect("chmod");
        path
    }

    #[test]
    fn window_answers() {
        let all = vec![Answer::Once, Answer::Session, Answer::Always];
        let says = |name, body| Window::new(&script(name, body)).ask(&question(all.clone()), 5.0);
        assert_eq!(says("once", "cat >/dev/null; echo once"), Answer::Once);
        assert_eq!(
            says("noise", "cat >/dev/null; echo hello; echo session"),
            Answer::Session
        );
        assert_eq!(says("bogus", "cat >/dev/null; echo yes"), Answer::Deny);
        assert_eq!(says("silent", "cat >/dev/null"), Answer::Deny);
        // Not offered: "always" for an R4 action is a "no".
        let w = Window::new(&script("always", "cat >/dev/null; echo always"));
        assert_eq!(w.ask(&question(vec![Answer::Once]), 5.0), Answer::Deny);
        assert_eq!(
            Window::new(Path::new("/no/such/window")).ask(&question(all), 5.0),
            Answer::Deny
        );
    }

    #[test]
    fn window_times_out() {
        let w = Window::new(&script("slow", "sleep 10; echo once"));
        let start = std::time::Instant::now();
        assert_eq!(w.ask(&question(vec![Answer::Once]), 0.5), Answer::Deny);
        assert!(start.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn window_gets_the_question() {
        let dir = std::env::temp_dir().join(format!("herkosd-confirm-q-{}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("mkdir");
        let seen = dir.join("seen.json");
        let w = Window::new(&script(
            "record",
            &format!("cat > {}; echo once", seen.display()),
        ));
        assert_eq!(w.ask(&question(vec![Answer::Once]), 5.0), Answer::Once);
        let got: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&seen).expect("seen")).expect("json");
        assert_eq!(got["summary"], "Read a file");
        assert_eq!(got["choices"], serde_json::json!(["once"]));
    }

    #[test]
    fn terminal_text() {
        let text = render(&question(vec![Answer::Once]));
        assert!(
            text.contains("Read a file  (R1)") && text.ends_with("[o] allow once   [n] deny\n> ")
        );
    }
}
