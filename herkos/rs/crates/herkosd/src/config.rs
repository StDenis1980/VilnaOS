// SPDX-License-Identifier: GPL-3.0-or-later
//! Settings and file locations: the same `config.toml` and state folder as
//! Herkos-lite (`config.py`), so the two can replace each other.

use std::path::{Path, PathBuf};

use herkos_core::risk::Level;
use toml::{Table, Value};

/// Until `herkosd` runs as its own system user (step 6 of ADR 0013) it
/// isolates nothing more than Herkos-lite, so it carries out R0-R2 only.
pub const MAX_LEVEL: Level = Level::R2;
/// A "session" permission lapses after this much idle time (permissions.md 5.3).
pub const SESSION_IDLE_SECONDS: f64 = 30.0 * 60.0;

#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    pub allowed_roots: Vec<PathBuf>,
    pub bulk_threshold: usize,
    pub confirm_timeout: f64,
    pub max_level: Level,
    /// `None`: off. Used by Keros in step 4.
    pub keros_model: Option<String>,
    pub keros_budget: f64,
    pub state_dir: PathBuf,
}

impl Config {
    pub fn journal_path(&self) -> PathBuf {
        self.state_dir.join("journal.jsonl")
    }

    pub fn grants_path(&self) -> PathBuf {
        self.state_dir.join("permissions.json")
    }
}

fn home() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/"))
}

fn xdg(var: &str, fallback: &str) -> PathBuf {
    match std::env::var_os(var).map(PathBuf::from) {
        Some(p) if p.is_absolute() => p,
        _ => home().join(fallback),
    }
}

pub fn default_config_path() -> PathBuf {
    xdg("XDG_CONFIG_HOME", ".config")
        .join("herkos")
        .join("config.toml")
}

fn expand_user(path: &str) -> PathBuf {
    if path == "~" {
        home()
    } else if let Some(rest) = path.strip_prefix("~/") {
        home().join(rest)
    } else {
        PathBuf::from(path)
    }
}

fn defaults() -> Config {
    Config {
        allowed_roots: vec![expand_user("~/Documents")],
        bulk_threshold: 20,
        confirm_timeout: 120.0,
        max_level: MAX_LEVEL,
        keros_model: Some("http://127.0.0.1:8080/v1".to_owned()),
        keros_budget: 20.0,
        state_dir: xdg("XDG_STATE_HOME", ".local/state").join("herkos"),
    }
}

fn number(v: &Value) -> Option<f64> {
    match v {
        Value::Integer(i) => Some(*i as f64),
        Value::Float(f) => Some(*f),
        _ => None,
    }
}

/// Accept only http(s) URLs on this machine: content must not leave it
/// (ADR 0010). The full check moves here with Keros in step 4.
fn require_loopback(url: &str) -> Result<String, String> {
    let rest = url
        .strip_prefix("http://")
        .or_else(|| url.strip_prefix("https://"))
        .ok_or_else(|| format!("not an http URL: {url:?}"))?;
    let authority = &rest[..rest.find('/').unwrap_or(rest.len())];
    if authority.contains('@') {
        return Err("the model URL must not carry credentials".into());
    }
    let host = match authority.strip_prefix('[') {
        Some(v6) => &v6[..v6.find(']').unwrap_or(v6.len())],
        None => &authority[..authority.rfind(':').unwrap_or(authority.len())],
    };
    let loopback = host.eq_ignore_ascii_case("localhost")
        || host
            .parse::<std::net::IpAddr>()
            .is_ok_and(|ip| ip.is_loopback());
    if !loopback {
        return Err(format!(
            "the Keros model must run on this device (loopback), not {host:?}"
        ));
    }
    Ok(url.trim_end_matches('/').to_owned())
}

pub fn parse_config(text: &str, source: &Path) -> Result<Config, String> {
    let at = source.display();
    let data: Table = text.parse().map_err(|e| format!("{at}: {e}"))?;
    let known = [
        "allowed_roots",
        "bulk_threshold",
        "confirm_timeout",
        "keros_model",
        "keros_budget",
    ];
    let mut unknown: Vec<&String> = data
        .keys()
        .filter(|k| !known.contains(&k.as_str()))
        .collect();
    if !unknown.is_empty() {
        unknown.sort();
        return Err(format!("{at}: unknown keys {unknown:?}"));
    }
    let mut c = defaults();
    if let Some(roots) = data.get("allowed_roots") {
        let list: Option<Vec<PathBuf>> = roots.as_array().and_then(|items| {
            items
                .iter()
                .map(|r| r.as_str().filter(|s| !s.is_empty()).map(expand_user))
                .collect()
        });
        let list = list.ok_or_else(|| format!("{at}: allowed_roots must be a list of folders"))?;
        if !list.iter().all(|r| r.is_absolute()) {
            return Err(format!(
                "{at}: allowed_roots must be absolute or start with ~"
            ));
        }
        c.allowed_roots = list;
    }
    if let Some(n) = data.get("bulk_threshold") {
        c.bulk_threshold = n
            .as_integer()
            .filter(|n| *n >= 1)
            .and_then(|n| usize::try_from(n).ok())
            .ok_or_else(|| format!("{at}: bulk_threshold must be a positive integer"))?;
    }
    if let Some(t) = data.get("confirm_timeout") {
        c.confirm_timeout = number(t)
            .filter(|t| (1.0..=3600.0).contains(t))
            .ok_or_else(|| format!("{at}: confirm_timeout must be 1-3600 seconds"))?;
    }
    if let Some(m) = data.get("keros_model") {
        c.keros_model = match m.as_str() {
            Some("off") => None,
            Some(url) => {
                Some(require_loopback(url).map_err(|e| format!("{at}: keros_model: {e}"))?)
            }
            None => return Err(format!("{at}: keros_model must be a URL or \"off\"")),
        };
    }
    if let Some(b) = data.get("keros_budget") {
        c.keros_budget = number(b)
            .filter(|b| (1.0..=300.0).contains(b))
            .ok_or_else(|| format!("{at}: keros_budget must be 1-300 seconds"))?;
    }
    Ok(c)
}

pub fn load_config(path: &Path) -> Result<Config, String> {
    match std::fs::read_to_string(path) {
        Ok(text) => parse_config(&text, path),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(defaults()),
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(text: &str) -> Result<Config, String> {
        parse_config(text, Path::new("config.toml"))
    }

    #[test]
    fn values() {
        let c = parse(
            "allowed_roots = [\"/srv/a\"]\nbulk_threshold = 5\nconfirm_timeout = 30\nkeros_model = \"off\"",
        )
        .expect("valid");
        assert_eq!(c.allowed_roots, vec![PathBuf::from("/srv/a")]);
        assert_eq!(
            (c.bulk_threshold, c.confirm_timeout, c.keros_model),
            (5, 30.0, None)
        );
        assert_eq!(c.max_level, Level::R2);
    }

    #[test]
    fn bad_values() {
        for text in [
            "what = 1",
            "allowed_roots = [\"relative\"]",
            "allowed_roots = \"/a\"",
            "bulk_threshold = 0",
            "bulk_threshold = true",
            "confirm_timeout = 0.5",
            "keros_model = \"http://192.168.1.2:8080/v1\"",
            "keros_model = \"http://u:p@127.0.0.1/v1\"",
            "keros_model = \"ftp://127.0.0.1/\"",
            "keros_budget = 1000",
        ] {
            assert!(parse(text).is_err(), "{text}");
        }
    }

    #[test]
    fn loopback() {
        for ok in [
            "http://127.0.0.1:8080/v1/",
            "http://localhost/v1",
            "http://[::1]:8080/v1",
        ] {
            assert!(require_loopback(ok).is_ok(), "{ok}");
        }
        assert!(require_loopback("http://127.0.0.1.example.org/v1").is_err());
    }
}
