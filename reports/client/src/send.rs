// SPDX-License-Identifier: GPL-3.0-or-later
//! Sending to reports.vilnaos.com (`reports/docs/protocol.md`).
//!
//! The collector does not carry its own TLS stack: it hands the body to
//! `curl` on stdin (never on the command line, where other processes could
//! read it), HTTPS only, with the system's CA certificates. The answer
//! decides what happens to the report: kept and sent, refused for good
//! (400, 413), or tried again later (429, 5xx, no network).

use std::io::{Read, Write};
use std::process::{Command, Stdio};
use std::time::Duration;

pub const DEFAULT_SERVER: &str = "https://reports.vilnaos.com";
const TIMEOUT: Duration = Duration::from_secs(30);
const MAX_ANSWER: u64 = 64 * 1024;

/// What the server said.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Answer {
    /// 200 or 201, with the JSON body.
    Taken(serde_json::Value),
    /// The server will never take this request (400, 413…): the reason.
    Refused(String),
    /// Not now: no network, too many requests, the server is down.
    Later(String),
}

pub trait Transport: Send + Sync {
    /// POST `body` (JSON) to `path` (`/v1/crash`) and read the answer.
    fn post(&self, path: &str, body: &[u8]) -> Answer;
}

/// The answer for an HTTP status and body.
pub fn answer(status: u16, body: &[u8]) -> Answer {
    let json: serde_json::Value = serde_json::from_slice(body).unwrap_or_default();
    match status {
        200 | 201 => Answer::Taken(json),
        429 | 500..=599 => Answer::Later(format!("the server answered {status}")),
        _ => {
            let why = json
                .get("message")
                .and_then(|m| m.as_str())
                .map(|m| match json.get("field").and_then(|f| f.as_str()) {
                    Some(f) => format!("{f}: {m}"),
                    None => m.to_owned(),
                })
                .unwrap_or_else(|| format!("the server answered {status}"));
            Answer::Refused(why)
        }
    }
}

/// `curl` to the server at `base`. `VILNA_REPORTS_URL` changes the server
/// for tests; plain HTTP is allowed only to this computer.
pub struct Curl {
    base: String,
}

impl Curl {
    pub fn new(base: &str) -> Curl {
        Curl {
            base: base.trim_end_matches('/').to_owned(),
        }
    }

    fn protocols(&self) -> &'static str {
        let local = ["http://127.0.0.1:", "http://localhost:", "http://[::1]:"];
        if local.iter().any(|p| self.base.starts_with(p)) {
            "=http"
        } else {
            "=https"
        }
    }
}

impl Transport for Curl {
    fn post(&self, path: &str, body: &[u8]) -> Answer {
        let url = format!("{}{path}", self.base);
        let child = Command::new("curl")
            .args([
                "--silent",
                "--show-error",
                "--proto",
                self.protocols(),
                "--max-time",
                &TIMEOUT.as_secs().to_string(),
                "--max-filesize",
                &MAX_ANSWER.to_string(),
                "--header",
                "Content-Type: application/json",
                "--user-agent",
                "vilna-reportd",
                "--data-binary",
                "@-",
                "--write-out",
                "\n%{http_code}",
                &url,
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn();
        let mut child = match child {
            Ok(c) => c,
            Err(e) => return Answer::Later(format!("curl did not start: {e}")),
        };
        if let Some(mut stdin) = child.stdin.take()
            && stdin.write_all(body).is_err()
        {
            let _ = child.kill();
            let _ = child.wait();
            return Answer::Later("curl stopped reading".into());
        }
        let mut out = Vec::new();
        if let Some(stdout) = child.stdout.take() {
            let _ = stdout.take(MAX_ANSWER + 16).read_to_end(&mut out);
        }
        let _ = child.wait();
        let text = String::from_utf8_lossy(&out);
        let (body, code) = text.rsplit_once('\n').unwrap_or(("", &text));
        match code.trim().parse::<u16>() {
            Ok(0) | Err(_) => Answer::Later("no answer from the server".into()),
            Ok(status) => answer(status, body.as_bytes()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_status_decides() {
        assert!(matches!(answer(201, b"{\"id\":\"x\"}"), Answer::Taken(_)));
        assert!(matches!(answer(200, b""), Answer::Taken(_)));
        assert!(matches!(answer(429, b""), Answer::Later(_)));
        assert!(matches!(answer(503, b""), Answer::Later(_)));
        assert_eq!(
            answer(
                400,
                br#"{"error":"invalid","field":"text","message":"is empty"}"#
            ),
            Answer::Refused("text: is empty".into())
        );
        assert!(matches!(answer(413, b"too big"), Answer::Refused(_)));
    }

    #[test]
    fn plain_http_only_to_this_computer() {
        assert_eq!(Curl::new(DEFAULT_SERVER).protocols(), "=https");
        assert_eq!(Curl::new("http://127.0.0.1:8087").protocols(), "=http");
        assert_eq!(Curl::new("http://reports.example").protocols(), "=https");
    }
}
