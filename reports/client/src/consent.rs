// SPDX-License-Identifier: GPL-3.0-or-later
//! The person's one answer for the whole system (ADR 0018 section 3): "If a
//! program crashes, send us a crash report?" Asked in the installer, with no
//! answer chosen in advance; until there is an answer nothing is sent.
//!
//! `/etc/vilna/reports.conf` holds it as `crash_reports=auto|ask|never`. The
//! same answer goes to Tinta Office, which can only say yes or no: "ask"
//! is "no" for Tinta, whose own settings still let the person change it.
//! The installer's engine writes the same three files
//! (`installer/src/vilna_installer/engine.py`, `system_files`).

use std::path::{Path, PathBuf};

pub const CONFIG: &str = "etc/vilna/reports.conf";
/// Read by the person's session and passed to Flatpak programs.
pub const TINTA_ENVIRONMENT: &str = "etc/environment.d/60-vilna-reports.conf";
/// Read by Tinta Office installed as native packages.
pub const TINTA_CONFIG: &str = "etc/xdg/tinta-office/reports.conf";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Consent {
    /// Reports are sent by themselves; they show in "Reports and feedback".
    Auto,
    /// A notification asks about each crash.
    Ask,
    /// Reports stay on the device; the person may still send one by hand.
    Never,
}

impl Consent {
    pub fn parse(value: &str) -> Option<Consent> {
        match value.trim() {
            "auto" => Some(Consent::Auto),
            "ask" => Some(Consent::Ask),
            "never" => Some(Consent::Never),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Consent::Auto => "auto",
            Consent::Ask => "ask",
            Consent::Never => "never",
        }
    }

    /// Tinta Office knows only yes or no.
    pub fn tinta(self) -> bool {
        self == Consent::Auto
    }
}

/// The answer in the file's text; `None` when there is none (or it is not
/// one of the three): then nothing is sent.
pub fn parse_config(text: &str) -> Option<Consent> {
    let mut answer = None;
    for line in text.lines() {
        let line = line.trim();
        if line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        if let Some((key, value)) = line.split_once('=')
            && key.trim() == "crash_reports"
        {
            answer = Consent::parse(value);
        }
    }
    answer
}

/// The answer on the system under `root` (`/` on a real system).
pub fn read(root: &Path) -> Option<Consent> {
    std::fs::read_to_string(root.join(CONFIG))
        .ok()
        .as_deref()
        .and_then(parse_config)
}

/// The three files and their text for an answer.
pub fn files(consent: Consent) -> Vec<(&'static str, String)> {
    let tinta = consent.tinta();
    vec![
        (
            CONFIG,
            format!(
                "# The answer to \"If a program crashes, send us a crash report?\" (ADR 0018).\n\
                 # auto: send by itself; ask: ask about each crash; never: keep on this computer.\n\
                 crash_reports={}\n",
                consent.as_str()
            ),
        ),
        (
            TINTA_ENVIRONMENT,
            format!(
                "# Vilna's answer about crash reports, for Tinta Office (ADR 0018).\nTINTA_OFFICE_REPORTS={tinta}\n"
            ),
        ),
        (
            TINTA_CONFIG,
            format!("# Vilna's answer about crash reports (ADR 0018).\nenabled={tinta}\n"),
        ),
    ]
}

/// Write the answer under `root`, each file atomically, readable by all.
pub fn write(root: &Path, consent: Consent) -> std::io::Result<Vec<PathBuf>> {
    let mut written = Vec::new();
    for (rel, text) in files(consent) {
        let path = root.join(rel);
        crate::spool::write_atomic(&path, text.as_bytes(), 0o644)?;
        written.push(path);
    }
    Ok(written)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn three_answers_and_no_answer() {
        assert_eq!(parse_config("crash_reports=auto"), Some(Consent::Auto));
        assert_eq!(
            parse_config("# c\ncrash_reports = ask \n"),
            Some(Consent::Ask)
        );
        assert_eq!(parse_config("crash_reports=never"), Some(Consent::Never));
        assert_eq!(parse_config("crash_reports=yes"), None);
        assert_eq!(parse_config(""), None);
        assert_eq!(parse_config("#crash_reports=auto"), None);
    }

    #[test]
    fn the_answer_goes_to_tinta_as_yes_or_no() {
        let dir = std::env::temp_dir().join(format!("vilna-consent-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        for (consent, tinta) in [
            (Consent::Auto, "true"),
            (Consent::Ask, "false"),
            (Consent::Never, "false"),
        ] {
            write(&dir, consent).unwrap();
            assert_eq!(read(&dir), Some(consent));
            let env = std::fs::read_to_string(dir.join(TINTA_ENVIRONMENT)).unwrap();
            assert!(env.contains(&format!("TINTA_OFFICE_REPORTS={tinta}\n")));
            let conf = std::fs::read_to_string(dir.join(TINTA_CONFIG)).unwrap();
            assert!(conf.contains(&format!("enabled={tinta}\n")));
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
