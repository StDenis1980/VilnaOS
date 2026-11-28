// SPDX-License-Identifier: GPL-3.0-or-later
//! Settings and file locations: the same `config.toml` and state folder as
//! Herkos-lite (`config.py`), so the two can replace each other.

use std::path::{Path, PathBuf};

use herkos_core::risk::Level;
use toml::{Table, Value};

/// `herkosd serve` runs in the person's session, like Herkos-lite, and so
/// isolates nothing more: R0-R2 only. `herkosd system` raises it to R4.
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

fn expand_user(path: &str, home: &Path) -> PathBuf {
    if path == "~" {
        home.to_path_buf()
    } else if let Some(rest) = path.strip_prefix("~/") {
        home.join(rest)
    } else {
        PathBuf::from(path)
    }
}

/// Whose settings these are: `~` in `allowed_roots` is this person's home,
/// and Herkos keeps their permissions and journal in `state_dir`.
pub struct Owner {
    pub home: PathBuf,
    pub state_dir: PathBuf,
}

impl Owner {
    /// The person running this herkosd, as Herkos-lite does it.
    pub fn current() -> Owner {
        Owner {
            home: home(),
            state_dir: xdg("XDG_STATE_HOME", ".local/state").join("herkos"),
        }
    }
}

fn defaults(owner: &Owner) -> Config {
    Config {
        allowed_roots: vec![expand_user("~/Documents", &owner.home)],
        bulk_threshold: 20,
        confirm_timeout: 120.0,
        max_level: MAX_LEVEL,
        keros_model: Some("http://127.0.0.1:8080/v1".to_owned()),
        keros_budget: 60.0,
        state_dir: owner.state_dir.clone(),
    }
}

fn number(v: &Value) -> Option<f64> {
    match v {
        Value::Integer(i) => Some(*i as f64),
        Value::Float(f) => Some(*f),
        _ => None,
    }
}

#[cfg(test)]
pub fn parse_config(text: &str, source: &Path) -> Result<Config, String> {
    parse_config_for(text, source, &Owner::current())
}

pub fn parse_config_for(text: &str, source: &Path, owner: &Owner) -> Result<Config, String> {
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
    let mut c = defaults(owner);
    if let Some(roots) = data.get("allowed_roots") {
        let list: Option<Vec<PathBuf>> = roots.as_array().and_then(|items| {
            items
                .iter()
                .map(|r| {
                    r.as_str()
                        .filter(|s| !s.is_empty())
                        .map(|s| expand_user(s, &owner.home))
                })
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
            Some(url) => Some(
                crate::keros_model::require_loopback(url)
                    .map_err(|e| format!("{at}: keros_model: {e}"))?,
            ),
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

/// The longest settings or state file herkosd reads.
pub const MAX_FILE_BYTES: u64 = 16 * 1024 * 1024;

/// A settings or state file of at most `limit` bytes. Only a plain file is
/// read: a link to `/dev/zero` filled the memory and a pipe never answered.
pub fn read_small(path: &Path, limit: u64) -> std::io::Result<String> {
    use std::io::{Error, ErrorKind, Read};
    use std::os::unix::fs::OpenOptionsExt;
    let nonblock = rustix::fs::OFlags::NONBLOCK.bits();
    let file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(i32::try_from(nonblock).unwrap_or(0))
        .open(path)?;
    if !file.metadata()?.is_file() {
        return Err(Error::new(ErrorKind::InvalidData, "not a regular file"));
    }
    let mut text = String::new();
    file.take(limit + 1).read_to_string(&mut text)?;
    if u64::try_from(text.len()).unwrap_or(u64::MAX) > limit {
        return Err(Error::new(
            ErrorKind::InvalidData,
            format!("larger than {limit} bytes"),
        ));
    }
    Ok(text)
}

pub fn load_config(path: &Path) -> Result<Config, String> {
    load_config_for(path, &Owner::current())
}

pub fn load_config_for(path: &Path, owner: &Owner) -> Result<Config, String> {
    match read_small(path, MAX_FILE_BYTES) {
        Ok(text) => parse_config_for(&text, path, owner),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(defaults(owner)),
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_pipe_or_a_device_is_not_read_as_a_settings_file() {
        let dir = std::env::temp_dir().join(format!("herkosd-small-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("dir");
        let fifo = dir.join("config.toml");
        rustix::fs::mknodat(
            rustix::fs::CWD,
            &fifo,
            rustix::fs::FileType::Fifo,
            rustix::fs::Mode::from_raw_mode(0o600),
            0,
        )
        .expect("fifo");
        assert!(read_small(&fifo, 100).is_err(), "a pipe never answers");
        let zero = dir.join("permissions.json");
        std::os::unix::fs::symlink("/dev/zero", &zero).expect("link");
        assert!(read_small(&zero, 100).is_err(), "a device has no end");
        let big = dir.join("big");
        std::fs::write(&big, "x".repeat(101)).expect("write");
        assert!(read_small(&big, 100).is_err());
        assert!(read_small(&big, 101).is_ok());
        let _ = std::fs::remove_dir_all(&dir);
    }

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
}
