// SPDX-License-Identifier: GPL-3.0-or-later
//! Settings from the environment; secrets from systemd credentials
//! (`LoadCredential=`, read from `$CREDENTIALS_DIRECTORY`), never from the
//! command line or the unit file.

use std::net::SocketAddr;
use std::path::Path;

#[derive(Debug, Clone)]
pub struct Config {
    /// Loopback only: Caddy in front terminates TLS (`deploy/Caddyfile`).
    pub listen: SocketAddr,
    pub database: String,
    pub retention_days: i64,
    /// Take the client address from `X-Forwarded-For` (set by Caddy); only
    /// for the request limits, never stored.
    pub trust_proxy: bool,
    /// `owner/name` of the repository that gets `auto-report` issues.
    pub github_repo: String,
    pub github_token: Option<String>,
    /// Bearer or Basic password for `/admin`; without it `/admin` is off.
    pub admin_key: Option<String>,
    /// Where the site form returns after sending (`/feedback/thanks/`).
    pub site: String,
    pub max_new_issues_per_day: i64,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            listen: SocketAddr::from(([127, 0, 0, 1], 8087)),
            database: "/var/lib/vilna-reports/reports.sqlite".into(),
            retention_days: 90,
            trust_proxy: true,
            github_repo: "StDenis1980/VilnaOS".into(),
            github_token: None,
            admin_key: None,
            site: "https://vilnaos.com".into(),
            max_new_issues_per_day: 20,
        }
    }
}

fn credential(name: &str) -> Option<String> {
    let dir = std::env::var_os("CREDENTIALS_DIRECTORY")?;
    let value = std::fs::read_to_string(Path::new(&dir).join(name)).ok()?;
    let value = value.trim().to_owned();
    (!value.is_empty()).then_some(value)
}

impl Config {
    pub fn from_env() -> Result<Config, String> {
        let mut c = Config::default();
        let var = |name: &str| std::env::var(name).ok().filter(|v| !v.trim().is_empty());
        if let Some(v) = var("VILNA_REPORTS_LISTEN") {
            c.listen = v.parse().map_err(|_| {
                format!("VILNA_REPORTS_LISTEN={v} is not an address like 127.0.0.1:8087")
            })?;
        }
        if let Some(v) = var("VILNA_REPORTS_DB") {
            c.database = v;
        }
        if let Some(v) = var("VILNA_REPORTS_RETENTION_DAYS") {
            c.retention_days = v
                .parse()
                .ok()
                .filter(|d| (1..=90).contains(d))
                .ok_or(format!("VILNA_REPORTS_RETENTION_DAYS={v}: must be 1 to 90 (ADR 0018 promises 90 at most)"))?;
        }
        if let Some(v) = var("VILNA_REPORTS_TRUST_PROXY") {
            c.trust_proxy = v == "1" || v.eq_ignore_ascii_case("true");
        }
        if let Some(v) = var("VILNA_REPORTS_GITHUB_REPO") {
            c.github_repo = v;
        }
        if let Some(v) = var("VILNA_REPORTS_SITE") {
            c.site = v.trim_end_matches('/').to_owned();
        }
        c.github_token = credential("github_token");
        c.admin_key = credential("admin_key");
        if c.admin_key.as_ref().is_some_and(|k| k.len() < 32) {
            return Err(
                "the admin_key credential must be at least 32 characters (openssl rand -hex 32)"
                    .into(),
            );
        }
        Ok(c)
    }
}
