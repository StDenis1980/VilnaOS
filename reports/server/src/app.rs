// SPDX-License-Identifier: GPL-3.0-or-later
//! The HTTP side: `/v1/*` for devices and the site form, `/admin*` for us.
//! `reports/docs/protocol.md` describes every route.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::{ConnectInfo, DefaultBodyLimit, FromRequestParts, Path, State};
use axum::http::StatusCode;
use axum::http::header::{self, HeaderMap, HeaderValue};
use axum::http::request::Parts;
use axum::response::{Html, IntoResponse, Redirect, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use base64::Engine;
use serde::Deserialize;
use serde_json::json;
use sha2::{Digest, Sha256};
use tower_http::limit::RequestBodyLimitLayer;
use tower_http::timeout::TimeoutLayer;
use vilna_reports_protocol::{
    self as protocol, Crash, CrashReceipt, CrashWire, Feedback, FeedbackWire, ForgetWire, Invalid,
    MAX_BODY_BYTES, Status,
};

use crate::config::Config;
use crate::limit::{Bucket, Limiter, Limits};
use crate::store::{Group, MILESTONES, Store};
use crate::tracker::Tracker;

/// The ten languages of ADR 0009: the site has a thank-you page in each.
const SITE_LANGUAGES: &[&str] = &["uk", "en", "es", "pl", "de", "fr", "it", "ro", "cs", "lt"];

pub type Clock = Arc<dyn Fn() -> i64 + Send + Sync>;

pub struct AppState {
    pub config: Config,
    pub store: Store,
    pub tracker: Box<dyn Tracker>,
    pub limiter: Limiter,
    pub now: Clock,
}

impl AppState {
    pub fn new(config: Config, store: Store, tracker: Box<dyn Tracker>, now: Clock) -> AppState {
        AppState {
            config,
            store,
            tracker,
            limiter: Limiter::new(Limits::default()),
            now,
        }
    }
}

type Shared = Arc<AppState>;

pub fn router(state: Shared) -> Router {
    Router::new()
        .route("/", get(index))
        .route("/healthz", get(|| async { "ok" }))
        .route("/v1/crash", post(crash))
        .route("/v1/feedback", post(feedback))
        .route("/v1/forget", post(forget))
        .route("/v1/status/{id}", get(status))
        .route("/admin", get(admin_page))
        .route("/admin/v1/signatures", get(admin_signatures))
        .route("/admin/v1/signatures/{signature}", get(admin_signature))
        .route(
            "/admin/v1/signatures/{signature}/approve",
            post(admin_approve),
        )
        .route("/admin/v1/signatures/{signature}/fixed", post(admin_fixed))
        .route("/admin/v1/feedback", get(admin_feedback))
        .route(
            "/admin/v1/feedback/{id}",
            axum::routing::delete(admin_delete_feedback),
        )
        .fallback(|| async {
            error(
                StatusCode::NOT_FOUND,
                "not_found",
                "no such route; see reports/docs/protocol.md",
            )
        })
        .layer(axum::middleware::map_response(private_headers))
        .layer(DefaultBodyLimit::max(MAX_BODY_BYTES))
        .layer(RequestBodyLimitLayer::new(MAX_BODY_BYTES))
        .layer(TimeoutLayer::with_status_code(
            StatusCode::REQUEST_TIMEOUT,
            Duration::from_secs(15),
        ))
        .with_state(state)
}

/// Nothing a browser or proxy keeps, and no server banner.
async fn private_headers(mut res: Response) -> Response {
    let h = res.headers_mut();
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    h.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    h.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    h.insert("x-robots-tag", HeaderValue::from_static("noindex"));
    res
}

fn error(status: StatusCode, code: &str, message: &str) -> Response {
    (status, Json(json!({"error": code, "message": message}))).into_response()
}

fn refused(e: &Invalid) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({"error": "invalid", "field": e.field, "message": e.to_string()})),
    )
        .into_response()
}

fn storage_failed(e: &dyn std::fmt::Display) -> Response {
    // The message names the SQLite error only; no report content.
    eprintln!("{e}");
    error(
        StatusCode::SERVICE_UNAVAILABLE,
        "storage",
        "the server could not store this now; try again later",
    )
}

fn too_many(retry: i64) -> Response {
    let mut res = (
        StatusCode::TOO_MANY_REQUESTS,
        Json(json!({"error": "rate_limited", "retry_after_seconds": retry})),
    )
        .into_response();
    if let Ok(v) = HeaderValue::from_str(&retry.to_string()) {
        res.headers_mut().insert(header::RETRY_AFTER, v);
    }
    res
}

/// Who is asking, for the request limits only.
pub struct Client(String);

impl FromRequestParts<Shared> for Client {
    type Rejection = std::convert::Infallible;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &Shared,
    ) -> Result<Self, Self::Rejection> {
        let forwarded = state
            .config
            .trust_proxy
            .then(|| {
                parts
                    .headers
                    .get("x-forwarded-for")
                    .and_then(|v| v.to_str().ok())
            })
            .flatten()
            .and_then(|v| v.rsplit(',').next())
            .map(|v| v.trim().to_owned());
        let peer = parts
            .extensions
            .get::<ConnectInfo<SocketAddr>>()
            .map(|c| c.0.ip().to_string());
        Ok(Client(forwarded.or(peer).unwrap_or_default()))
    }
}

impl AppState {
    /// `Some` refusal when the client is over its limit.
    fn limit(&self, client: &Client, bucket: Bucket) -> Option<Response> {
        self.limiter
            .check(&client.0, bucket, (self.now)())
            .err()
            .map(too_many)
    }

    fn day(&self) -> String {
        chrono::DateTime::from_timestamp((self.now)(), 0)
            .map(|t| t.format("%Y-%m-%d").to_string())
            .unwrap_or_default()
    }

    fn crash_status(&self, group: &Group) -> &'static str {
        if group.fixed_in.is_some() {
            "fixed"
        } else if group.issue.is_some() {
            "known"
        } else {
            "received"
        }
    }
}

async fn index() -> &'static str {
    "Vilna crash reports and feedback (ADR 0018). \
     Protocol: https://github.com/StDenis1980/VilnaOS/blob/main/reports/docs/protocol.md\n"
}

// ------------------------------------------------------------------ /v1 --

async fn crash(State(state): State<Shared>, client: Client, body: Bytes) -> Response {
    if let Some(res) = state.limit(&client, Bucket::Crash) {
        return res;
    }
    let wire: CrashWire = match serde_json::from_slice(&body) {
        Ok(w) => w,
        Err(e) => {
            return error(
                StatusCode::BAD_REQUEST,
                "invalid_json",
                &format!("not a crash report: {e}"),
            );
        }
    };
    let crash = match Crash::from_wire(wire) {
        Ok(c) => c,
        Err(e) => return refused(&e),
    };
    let signature = crash.signature();
    let inserted = match state.store.insert_crash(&crash, &signature, (state.now)()) {
        Ok(i) => i,
        Err(e) => return storage_failed(&e),
    };
    let group = inserted.group;
    if inserted.new_report {
        if let Some(issue) = group.issue.filter(|_| MILESTONES.contains(&group.count)) {
            let state = state.clone();
            let body = milestone_comment(&group);
            tokio::task::spawn_blocking(move || {
                if let Err(e) = state.tracker.comment(issue, &body) {
                    eprintln!("{e}");
                }
            });
        }
    }
    let status = if inserted.new_group {
        "new"
    } else if group.fixed_in.is_some() {
        "fixed"
    } else {
        "known"
    };
    let receipt = CrashReceipt {
        id: crash.id,
        signature,
        status: status.to_owned(),
        fixed_in: group.fixed_in,
    };
    let code = if inserted.new_report {
        StatusCode::CREATED
    } else {
        StatusCode::OK
    };
    (code, Json(receipt)).into_response()
}

/// The site's form (`application/x-www-form-urlencoded`, no JavaScript).
#[derive(Debug, Default, Deserialize)]
struct SiteForm {
    #[serde(default)]
    kind: String,
    #[serde(default)]
    text: String,
    #[serde(default)]
    contact: Option<String>,
    #[serde(default)]
    may_publish: Option<String>,
    #[serde(default)]
    lang: Option<String>,
    #[serde(default)]
    program: Option<String>,
    #[serde(default)]
    version: Option<String>,
    #[serde(default)]
    system: Option<String>,
    /// Hidden from people; a bot that fills it in gets "thanks" and nothing is kept.
    #[serde(default)]
    website: Option<String>,
}

fn new_id() -> String {
    let mut b = [0u8; 16];
    if getrandom::fill(&mut b).is_err() {
        // Without randomness there is no id nobody can guess; an empty one
        // is refused by `Feedback::from_wire`.
        return String::new();
    }
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let h: String = b.iter().map(|x| format!("{x:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &h[0..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..32]
    )
}

fn is_form(headers: &HeaderMap) -> bool {
    headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.starts_with("application/x-www-form-urlencoded"))
}

async fn feedback(
    State(state): State<Shared>,
    client: Client,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Some(res) = state.limit(&client, Bucket::Feedback) {
        return res;
    }
    if is_form(&headers) {
        return site_feedback(&state, &body);
    }
    let wire: FeedbackWire = match serde_json::from_slice(&body) {
        Ok(w) => w,
        Err(e) => {
            return error(
                StatusCode::BAD_REQUEST,
                "invalid_json",
                &format!("not feedback: {e}"),
            );
        }
    };
    if wire.id.as_deref().is_none_or(|id| id.trim().is_empty()) {
        // A device keeps the id so it can forget the feedback later.
        return refused(&Invalid {
            field: "id",
            why: "is required: make a random UUID and keep it on the device",
        });
    }
    let fb = match Feedback::from_wire(wire, new_id) {
        Ok(f) => f,
        Err(e) => return refused(&e),
    };
    match state.store.insert_feedback(&fb, (state.now)()) {
        Ok(true) => (StatusCode::CREATED, Json(json!({"id": fb.id}))).into_response(),
        Ok(false) => (StatusCode::OK, Json(json!({"id": fb.id}))).into_response(),
        Err(e) => storage_failed(&e),
    }
}

fn site_feedback(state: &AppState, body: &[u8]) -> Response {
    let form: SiteForm = serde_urlencoded_from(body);
    let lang = form
        .lang
        .as_deref()
        .filter(|l| SITE_LANGUAGES.contains(l))
        .unwrap_or("en");
    let page = |path: &str| {
        let prefix = if lang == "en" {
            String::new()
        } else {
            format!("/{lang}")
        };
        format!("{}{prefix}/feedback/{path}", state.config.site)
    };
    if form.website.as_deref().is_some_and(|w| !w.is_empty()) {
        return Redirect::to(&page("thanks/")).into_response();
    }
    let wire = FeedbackWire {
        schema: Some(protocol::SCHEMA),
        id: None,
        kind: form.kind,
        text: form.text,
        system: form.system,
        channel: "site".into(),
        locale: Some(lang.to_owned()),
        program: form.program,
        version: form.version,
        contact: form.contact,
        may_publish: form
            .may_publish
            .is_some_and(|v| v == "on" || v == "true" || v == "1"),
        ..FeedbackWire::default()
    };
    let fb = match Feedback::from_wire(wire, new_id) {
        Ok(f) => f,
        Err(e) => return Redirect::to(&page(&format!("?error={}", e.field))).into_response(),
    };
    match state.store.insert_feedback(&fb, (state.now)()) {
        Ok(_) => Redirect::to(&page("thanks/")).into_response(),
        Err(e) => {
            eprintln!("{e}");
            Redirect::to(&page("?error=server")).into_response()
        }
    }
}

/// The form decoded leniently: an unknown field is ignored and a missing one
/// left empty, so the checks in [`Feedback::from_wire`] give the reason.
fn serde_urlencoded_from(body: &[u8]) -> SiteForm {
    let pairs: Vec<(String, String)> = serde_urlencoded::from_bytes(body).unwrap_or_default();
    let mut form = SiteForm::default();
    for (key, value) in pairs {
        match key.as_str() {
            "kind" => form.kind = value,
            "text" => form.text = value,
            "contact" => form.contact = Some(value),
            "may_publish" => form.may_publish = Some(value),
            "lang" => form.lang = Some(value),
            "program" => form.program = Some(value),
            "version" => form.version = Some(value),
            "system" => form.system = Some(value),
            "website" => form.website = Some(value),
            _ => {}
        }
    }
    form
}

async fn forget(State(state): State<Shared>, client: Client, body: Bytes) -> Response {
    if let Some(res) = state.limit(&client, Bucket::Other) {
        return res;
    }
    let wire: ForgetWire = match serde_json::from_slice(&body) {
        Ok(w) => w,
        Err(e) => {
            return error(
                StatusCode::BAD_REQUEST,
                "invalid_json",
                &format!("expected {{\"ids\": [...]}}: {e}"),
            );
        }
    };
    let ids = match wire.ids() {
        Ok(ids) => ids,
        Err(e) => return refused(&e),
    };
    match state.store.forget(&ids) {
        Ok(deleted) => Json(json!({"deleted": deleted})).into_response(),
        Err(e) => storage_failed(&e),
    }
}

async fn status(State(state): State<Shared>, client: Client, Path(id): Path<String>) -> Response {
    if let Some(res) = state.limit(&client, Bucket::Other) {
        return res;
    }
    let Some(id) = protocol::report_id(&id) else {
        return refused(&Invalid {
            field: "id",
            why: "must be the UUID of a report this device sent",
        });
    };
    let group = match state.store.group_of_report(&id) {
        Ok(g) => g,
        Err(e) => return storage_failed(&e),
    };
    let answer = match group {
        None => Status {
            id,
            status: "unknown".into(),
            issue_url: None,
            fixed_in: None,
        },
        Some(g) => Status {
            id,
            status: state.crash_status(&g).into(),
            issue_url: g.issue.map(|n| state.tracker.issue_url(n)),
            fixed_in: g.fixed_in,
        },
    };
    Json(answer).into_response()
}

// ---------------------------------------------------------------- admin --

fn digest(s: &str) -> [u8; 32] {
    Sha256::digest(s.as_bytes()).into()
}

/// Bearer `<key>`, or Basic with the key as password (for a browser).
/// `Some` refusal unless the request carries the admin key.
fn admin_refusal(state: &AppState, headers: &HeaderMap) -> Option<Response> {
    let Some(key) = &state.config.admin_key else {
        return Some(error(StatusCode::NOT_FOUND, "not_found", "no such route"));
    };
    let given = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| {
            if let Some(token) = v.strip_prefix("Bearer ") {
                Some(token.trim().to_owned())
            } else {
                let raw = base64::engine::general_purpose::STANDARD
                    .decode(v.strip_prefix("Basic ")?.trim())
                    .ok()?;
                let pair = String::from_utf8(raw).ok()?;
                pair.split_once(':')
                    .map(|(_, password)| password.to_owned())
            }
        });
    // Comparing digests keeps the comparison time independent of the key.
    if given.is_some_and(|g| digest(&g) == digest(key)) {
        return None;
    }
    let mut res = error(
        StatusCode::UNAUTHORIZED,
        "unauthorized",
        "admin key required",
    );
    res.headers_mut().insert(
        header::WWW_AUTHENTICATE,
        HeaderValue::from_static("Basic realm=\"Vilna reports\", charset=\"UTF-8\""),
    );
    Some(res)
}

fn when(ts: i64) -> String {
    chrono::DateTime::from_timestamp(ts, 0)
        .map(|t| t.format("%Y-%m-%d %H:%M UTC").to_string())
        .unwrap_or_default()
}

fn milestone_comment(g: &Group) -> String {
    format!(
        "{} reports so far (last {}). Versions: {}.",
        g.count,
        when(g.last),
        g.versions.join(", ")
    )
}

fn issue_body(g: &Group, latest: Option<&Crash>) -> String {
    let mut lines = vec![
        format!(
            "Automatic crash report for `{}`, sent with the person's consent (ADR 0018).",
            g.program
        ),
        String::new(),
        format!("- Signature: `{}`", g.signature),
        format!(
            "- Seen: {} times, first {}, last {}",
            g.count,
            when(g.first),
            when(g.last)
        ),
        format!(
            "- Versions: {}",
            if g.versions.is_empty() {
                "unknown".into()
            } else {
                g.versions.join(", ")
            }
        ),
    ];
    if let Some(c) = latest {
        lines.push(format!("- Kind: {}", c.kind.as_str()));
        if let Some(s) = c.signal.as_deref().or(c.exception.as_deref()) {
            lines.push(format!("- Signal or exception: `{s}`"));
        }
        if let Some(l) = &c.location {
            lines.push(format!("- Where: `{l}`"));
        }
        let mut about = Vec::new();
        about.extend(c.image.clone());
        about.extend(c.arch.clone());
        about.extend(c.locale.clone());
        if !about.is_empty() {
            lines.push(format!("- Latest report from: {}", about.join(" · ")));
        }
        if !c.frames.is_empty() {
            lines.push(String::new());
            lines.push("Stack of the thread that crashed (latest report):".into());
            lines.push("```".into());
            for f in c.frames.iter().take(30) {
                let place = match (&f.file, &f.module) {
                    (Some(file), _) => match f.line {
                        Some(line) => format!("{file}:{line}"),
                        None => file.clone(),
                    },
                    (None, Some(m)) => format!("{m}+{}", f.offset.as_deref().unwrap_or("?")),
                    (None, None) => format!("?+{}", f.offset.as_deref().unwrap_or("?")),
                };
                let build = f
                    .build_id
                    .as_deref()
                    .map(|b| format!(" [{b}]"))
                    .unwrap_or_default();
                lines.push(format!(
                    "{place} in {}{build}",
                    f.function.as_deref().unwrap_or("??")
                ));
            }
            lines.push("```".into());
        }
    }
    lines.push(String::new());
    lines.push("Repeats are counted in comments. Raw reports stay private on reports.vilnaos.com for 90 days.".into());
    lines.join("\n")
}

async fn admin_signatures(State(state): State<Shared>, headers: HeaderMap) -> Response {
    if let Some(res) = admin_refusal(&state, &headers) {
        return res;
    }
    match state.store.groups(500) {
        Ok(g) => Json(g).into_response(),
        Err(e) => storage_failed(&e),
    }
}

async fn admin_signature(
    State(state): State<Shared>,
    headers: HeaderMap,
    Path(sig): Path<String>,
) -> Response {
    if let Some(res) = admin_refusal(&state, &headers) {
        return res;
    }
    match (state.store.group(&sig), state.store.reports(&sig, 20)) {
        (Ok(Some(group)), Ok(reports)) => {
            Json(json!({"group": group, "reports": reports})).into_response()
        }
        (Ok(None), _) => error(StatusCode::NOT_FOUND, "not_found", "no such signature"),
        (Err(e), _) | (_, Err(e)) => storage_failed(&e),
    }
}

/// We read the stored reports and found nothing personal: file the issue.
async fn admin_approve(
    State(state): State<Shared>,
    headers: HeaderMap,
    Path(sig): Path<String>,
) -> Response {
    if let Some(res) = admin_refusal(&state, &headers) {
        return res;
    }
    let group = match state.store.group(&sig) {
        Ok(Some(g)) => g,
        Ok(None) => return error(StatusCode::NOT_FOUND, "not_found", "no such signature"),
        Err(e) => return storage_failed(&e),
    };
    if let Err(e) = state.store.set_approved(&sig) {
        return storage_failed(&e);
    }
    if let Some(issue) = group.issue {
        return Json(
            json!({"issue": issue, "url": state.tracker.issue_url(issue), "created": false}),
        )
        .into_response();
    }
    let day = state.day();
    match state
        .store
        .take_issue_slot(&day, state.config.max_new_issues_per_day)
    {
        Ok(true) => {}
        Ok(false) => {
            return error(
                StatusCode::TOO_MANY_REQUESTS,
                "issue_quota",
                "20 new issues today already; approve again tomorrow",
            );
        }
        Err(e) => return storage_failed(&e),
    }
    let latest = state.store.reports(&sig, 1).ok().and_then(|mut r| r.pop());
    let title = format!("[auto] {}", group.title);
    let body = issue_body(&group, latest.as_ref());
    let worker = state.clone();
    let created = tokio::task::spawn_blocking(move || worker.tracker.create(&title, &body)).await;
    match created {
        Ok(Ok(issue)) => {
            if let Err(e) = state.store.set_issue(&sig, issue) {
                return storage_failed(&e);
            }
            (
                StatusCode::CREATED,
                Json(
                    json!({"issue": issue, "url": state.tracker.issue_url(issue), "created": true}),
                ),
            )
                .into_response()
        }
        Ok(Err(e)) => {
            let _ = state.store.return_issue_slot(&day);
            error(StatusCode::BAD_GATEWAY, "github", &e)
        }
        Err(_) => {
            let _ = state.store.return_issue_slot(&day);
            error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "github",
                "filing the issue failed",
            )
        }
    }
}

#[derive(Deserialize)]
struct Fixed {
    /// The version with the fix; `null` takes the mark away.
    version: Option<String>,
}

async fn admin_fixed(
    State(state): State<Shared>,
    headers: HeaderMap,
    Path(sig): Path<String>,
    body: Bytes,
) -> Response {
    if let Some(res) = admin_refusal(&state, &headers) {
        return res;
    }
    let Ok(fixed) = serde_json::from_slice::<Fixed>(&body) else {
        return error(
            StatusCode::BAD_REQUEST,
            "invalid_json",
            "expected {\"version\": \"0.2.1\"} or {\"version\": null}",
        );
    };
    let version = fixed
        .version
        .map(|v| protocol::truncate(v.trim(), 64))
        .filter(|v| !v.is_empty());
    match state.store.set_fixed(&sig, version.as_deref()) {
        Ok(true) => Json(json!({"signature": sig, "fixed_in": version})).into_response(),
        Ok(false) => error(StatusCode::NOT_FOUND, "not_found", "no such signature"),
        Err(e) => storage_failed(&e),
    }
}

async fn admin_feedback(State(state): State<Shared>, headers: HeaderMap) -> Response {
    if let Some(res) = admin_refusal(&state, &headers) {
        return res;
    }
    match state.store.feedback(1000) {
        Ok(f) => Json(f).into_response(),
        Err(e) => storage_failed(&e),
    }
}

async fn admin_delete_feedback(
    State(state): State<Shared>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    if let Some(res) = admin_refusal(&state, &headers) {
        return res;
    }
    match state.store.delete_feedback(&id) {
        Ok(true) => Json(json!({"deleted": 1})).into_response(),
        Ok(false) => error(StatusCode::NOT_FOUND, "not_found", "no such feedback"),
        Err(e) => storage_failed(&e),
    }
}

fn esc(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}

/// One plain page: the crashes and the newest feedback. Actions stay in the
/// JSON routes (`curl`), so the page needs no JavaScript and no forms.
async fn admin_page(State(state): State<Shared>, headers: HeaderMap) -> Response {
    if let Some(res) = admin_refusal(&state, &headers) {
        return res;
    }
    let (groups, feedback) = match (state.store.groups(200), state.store.feedback(100)) {
        (Ok(g), Ok(f)) => (g, f),
        (Err(e), _) | (_, Err(e)) => return storage_failed(&e),
    };
    let mut html = String::from(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>\
         <title>Vilna reports</title><style>body{font:14px system-ui;margin:16px}table{border-collapse:collapse}\
         td,th{border:1px solid #ccc;padding:4px 6px;vertical-align:top;text-align:left}\
         pre{white-space:pre-wrap;margin:0}</style><h1>Crashes</h1><table><tr><th>Signature<th>Program<th>Title\
         <th>Count<th>Last<th>Versions<th>Issue<th>Fixed in</tr>",
    );
    for g in &groups {
        html.push_str(&format!(
            "<tr><td><code>{}</code><td>{}<td>{}<td>{}<td>{}<td>{}<td>{}<td>{}</tr>",
            esc(&g.signature),
            esc(&g.program),
            esc(&g.title),
            g.count,
            esc(&when(g.last)),
            esc(&g.versions.join(", ")),
            g.issue
                .map(|n| format!("#{n}"))
                .unwrap_or_else(|| if g.approved {
                    "approved".into()
                } else {
                    "—".into()
                }),
            esc(g.fixed_in.as_deref().unwrap_or("")),
        ));
    }
    html.push_str("</table><h1>Feedback</h1><table><tr><th>Received<th>Kind<th>Channel<th>Text<th>Reply to<th>Publish</tr>");
    for f in &feedback {
        let text = match &f.feedback.text_en {
            Some(en) => format!("{}\n\n— en: {}", f.feedback.text, en),
            None => f.feedback.text.clone(),
        };
        html.push_str(&format!(
            "<tr><td>{}<br><code>{}</code><td>{:?}<td>{:?}<td><pre>{}</pre><td>{}<td>{}</tr>",
            esc(&when(f.received)),
            esc(&f.feedback.id),
            f.feedback.kind,
            f.feedback.channel,
            esc(&text),
            esc(f.contact.as_deref().unwrap_or("")),
            if f.feedback.may_publish { "yes" } else { "no" },
        ));
    }
    html.push_str("</table><p>Approve, mark fixed and delete: see reports/docs/operations.md.</p>");
    Html(html).into_response()
}
