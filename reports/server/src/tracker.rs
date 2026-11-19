// SPDX-License-Identifier: GPL-3.0-or-later
//! Public issues for crashes we confirmed (ADR 0018 section 6): one issue
//! with the `auto-report` label per signature, a comment at each milestone.

use std::time::Duration;

use serde::Deserialize;

/// Where issues go. Tests use a fake; the server uses [`GitHub`].
pub trait Tracker: Send + Sync {
    /// Opens an issue and returns its number.
    fn create(&self, title: &str, body: &str) -> Result<i64, String>;
    fn comment(&self, issue: i64, body: &str) -> Result<(), String>;
    /// `https://github.com/<owner>/<repo>/issues/<n>`, for `/v1/status`.
    fn issue_url(&self, issue: i64) -> String;
}

/// No token configured: crashes are grouped and counted, nothing is filed.
pub struct Disabled;

impl Tracker for Disabled {
    fn create(&self, _: &str, _: &str) -> Result<i64, String> {
        Err("no GitHub token is configured (credential github_token)".into())
    }
    fn comment(&self, _: i64, _: &str) -> Result<(), String> {
        Ok(())
    }
    fn issue_url(&self, issue: i64) -> String {
        format!("#{issue}")
    }
}

pub struct GitHub {
    repo: String,
    token: String,
    agent: ureq::Agent,
}

#[derive(Deserialize)]
struct Created {
    number: i64,
}

impl GitHub {
    /// `repo` is `owner/name`; the token needs Issues: read and write on it
    /// and nothing else.
    pub fn new(repo: String, token: String) -> GitHub {
        let config = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(20)))
            .http_status_as_error(true)
            .tls_config(
                ureq::tls::TlsConfig::builder()
                    .root_certs(ureq::tls::RootCerts::PlatformVerifier)
                    .build(),
            )
            .build();
        GitHub {
            repo,
            token,
            agent: config.into(),
        }
    }

    fn post(
        &self,
        path: &str,
        body: serde_json::Value,
    ) -> Result<ureq::http::Response<ureq::Body>, String> {
        self.agent
            .post(&format!(
                "https://api.github.com/repos/{}/{path}",
                self.repo
            ))
            .header("authorization", &format!("Bearer {}", self.token))
            .header("accept", "application/vnd.github+json")
            .header("x-github-api-version", "2022-11-28")
            .header("user-agent", "vilna-reports-server")
            .send_json(body)
            // The error names the status only, never the token.
            .map_err(|e| format!("GitHub {path}: {e}"))
    }
}

impl Tracker for GitHub {
    fn create(&self, title: &str, body: &str) -> Result<i64, String> {
        let mut res = self.post(
            "issues",
            serde_json::json!({"title": title, "body": body, "labels": ["auto-report"]}),
        )?;
        let created: Created = res
            .body_mut()
            .read_json()
            .map_err(|e| format!("GitHub issues: {e}"))?;
        Ok(created.number)
    }

    fn comment(&self, issue: i64, body: &str) -> Result<(), String> {
        self.post(
            &format!("issues/{issue}/comments"),
            serde_json::json!({ "body": body }),
        )?;
        Ok(())
    }

    fn issue_url(&self, issue: i64) -> String {
        format!("https://github.com/{}/issues/{issue}", self.repo)
    }
}
