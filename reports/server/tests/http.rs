// SPDX-License-Identifier: GPL-3.0-or-later
//! The server end to end, through the router, with a fake GitHub.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use tower::ServiceExt;
use vilna_reports_server::app::{AppState, Clock, router};
use vilna_reports_server::config::Config;
use vilna_reports_server::store::Store;
use vilna_reports_server::tracker::Tracker;

const KEY: &str = "0123456789abcdef0123456789abcdef";
const ID1: &str = "11111111-1111-4111-8111-111111111111";
const ID2: &str = "22222222-2222-4222-8222-222222222222";
const ID3: &str = "33333333-3333-4333-8333-333333333333";

#[derive(Default)]
struct FakeGitHub {
    issues: Mutex<Vec<(String, String)>>,
    comments: Mutex<Vec<(i64, String)>>,
}

struct Fake(Arc<FakeGitHub>);

impl Tracker for Fake {
    fn create(&self, title: &str, body: &str) -> Result<i64, String> {
        let mut issues = self.0.issues.lock().unwrap();
        issues.push((title.into(), body.into()));
        Ok(100 + issues.len() as i64)
    }
    fn comment(&self, issue: i64, body: &str) -> Result<(), String> {
        self.0.comments.lock().unwrap().push((issue, body.into()));
        Ok(())
    }
    fn issue_url(&self, issue: i64) -> String {
        format!("https://github.com/StDenis1980/VilnaOS/issues/{issue}")
    }
}

struct Server {
    state: Arc<AppState>,
    github: Arc<FakeGitHub>,
    clock: Arc<AtomicI64>,
}

fn server_with(config: Config) -> Server {
    let github = Arc::new(FakeGitHub::default());
    let clock = Arc::new(AtomicI64::new(1_800_000_000));
    let c = clock.clone();
    let now: Clock = Arc::new(move || c.load(Ordering::SeqCst));
    let store = Store::open(&config.database).unwrap();
    let state = Arc::new(AppState::new(
        config,
        store,
        Box::new(Fake(github.clone())),
        now,
    ));
    Server {
        state,
        github,
        clock,
    }
}

fn server() -> Server {
    server_with(Config {
        database: ":memory:".into(),
        admin_key: Some(KEY.into()),
        ..Config::default()
    })
}

impl Server {
    async fn send(&self, req: Request<Body>) -> (StatusCode, axum::http::HeaderMap, Value) {
        let res = router(self.state.clone()).oneshot(req).await.unwrap();
        let status = res.status();
        let headers = res.headers().clone();
        let bytes = res.into_body().collect().await.unwrap().to_bytes();
        let body = serde_json::from_slice(&bytes)
            .unwrap_or(Value::String(String::from_utf8_lossy(&bytes).into()));
        (status, headers, body)
    }

    async fn post(&self, path: &str, body: Value) -> (StatusCode, Value) {
        let req = Request::post(path)
            .header(header::CONTENT_TYPE, "application/json")
            .header("x-forwarded-for", "203.0.113.7")
            .body(Body::from(body.to_string()))
            .unwrap();
        let (s, _, b) = self.send(req).await;
        (s, b)
    }

    async fn admin(&self, method: &str, path: &str, body: Option<Value>) -> (StatusCode, Value) {
        let req = Request::builder()
            .method(method)
            .uri(path)
            .header(header::AUTHORIZATION, format!("Bearer {KEY}"))
            .body(body.map(|b| Body::from(b.to_string())).unwrap_or_default())
            .unwrap();
        let (s, _, b) = self.send(req).await;
        (s, b)
    }

    async fn get(&self, path: &str) -> (StatusCode, Value) {
        let (s, _, b) = self
            .send(Request::get(path).body(Body::empty()).unwrap())
            .await;
        (s, b)
    }
}

fn crash(id: &str) -> Value {
    json!({
        "schema": 1,
        "id": id,
        "program": {"name": "org.gnome.Nautilus", "version": "48.2", "packaging": "flatpak"},
        "image": "Vilna Blavet 0.1",
        "kind": "signal",
        "signal": "SIGSEGV",
        "frames": [
            {"module": "libgtk-4.so.1", "build_id": "0a1b2c3d4e5f60718293", "offset": "0x4f2a1"},
            {"module": "nautilus", "build_id": "ffeeddccbbaa99887766", "offset": "0x1200", "function": "nautilus_window_open"}
        ],
        "locale": "uk", "arch": "x86_64", "memory_gb": 16
    })
}

fn form(body: &str) -> Request<Body> {
    Request::post("/v1/feedback")
        .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
        .body(Body::from(body.to_owned()))
        .unwrap()
}

#[tokio::test]
async fn a_crash_is_stored_once_and_counted_per_signature() {
    let s = server();
    let (status, body) = s.post("/v1/crash", crash(ID1)).await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    assert_eq!(body["status"], "new");
    let signature = body["signature"].as_str().unwrap().to_owned();

    // The device retried: same answer, not counted again.
    let (status, again) = s.post("/v1/crash", crash(ID1)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(again["signature"], signature.as_str());

    let (status, second) = s.post("/v1/crash", crash(ID2)).await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(second["status"], "known");

    let (_, groups) = s.admin("GET", "/admin/v1/signatures", None).await;
    assert_eq!(groups[0]["count"], 2);
    assert_eq!(
        groups[0]["title"],
        "org.gnome.Nautilus: SIGSEGV in libgtk-4.so.1+0x4f2a1"
    );
}

#[tokio::test]
async fn a_bad_report_is_refused_with_the_field_to_fix() {
    let s = server();
    let mut bad = crash(ID1);
    bad["kind"] = json!("usage");
    let (status, body) = s.post("/v1/crash", bad).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["field"], "kind");

    let (status, body) = s.post("/v1/crash", json!("not an object")).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"], "invalid_json");
}

#[tokio::test]
async fn a_body_over_64_kib_is_refused() {
    let s = server();
    let mut big = crash(ID1);
    big["padding"] = json!("x".repeat(70 * 1024));
    let (status, _) = s.post("/v1/crash", big).await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE);
}

#[tokio::test]
async fn hostile_bodies_never_break_the_server() {
    let s = server();
    let junk = [
        json!(null),
        json!([]),
        json!({"schema": "1"}),
        json!({"schema": 1, "id": ID1, "program": {"name": "x"}, "kind": "panic", "frames": [1, 2, 3]}),
        json!({"schema": 1, "id": ID1, "program": {"name": "x"}, "kind": "panic", "memory_gb": -5}),
        json!({"schema": 1, "id": ID1, "program": {"name": "x"}, "kind": "panic", "location": "\u{0}\u{202e}"}),
        json!({"ids": "all"}),
        json!({"text": {"a": 1}}),
    ];
    for path in ["/v1/crash", "/v1/feedback", "/v1/forget"] {
        for body in &junk {
            let (status, _) = s.post(path, body.clone()).await;
            assert!(
                status.is_client_error() || status.is_success(),
                "{path} {body}: {status}"
            );
        }
    }
    let (status, _) = s.get("/v1/status/../../etc/passwd").await;
    assert!(status.is_client_error());
}

#[tokio::test]
async fn feedback_from_a_device_needs_its_own_id_and_keeps_the_contact_apart() {
    let s = server();
    let (status, body) = s
        .post(
            "/v1/feedback",
            json!({"kind": "idea", "text": "Темна тема"}),
        )
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["field"], "id");

    let fb = json!({
        "schema": 1, "id": ID3, "kind": "bug", "channel": "gloxi", "locale": "uk",
        "text": "Копії зупинились, мій номер 0671234567",
        "text_en": "Backups stopped, my number is <n>",
        "contact": "olena@example.org",
        "crash_ids": [ID1]
    });
    let (status, _) = s.post("/v1/feedback", fb.clone()).await;
    assert_eq!(status, StatusCode::CREATED);
    let (status, _) = s.post("/v1/feedback", fb).await;
    assert_eq!(status, StatusCode::OK);

    let (_, list) = s.admin("GET", "/admin/v1/feedback", None).await;
    assert_eq!(list.as_array().unwrap().len(), 1);
    assert_eq!(list[0]["contact"], "olena@example.org");
    assert!(!list[0]["text"].as_str().unwrap().contains("0671234567"));
}

#[tokio::test]
async fn the_site_form_returns_to_the_thanks_page_in_its_language() {
    let s = server();
    let (status, headers, _) = s.send(form("kind=bug&text=%D0%9D%D0%B5+%D0%BF%D1%80%D0%B0%D1%86%D1%8E%D1%94&lang=pl&may_publish=on")).await;
    assert_eq!(status, StatusCode::SEE_OTHER);
    assert_eq!(
        headers[header::LOCATION],
        "https://vilnaos.com/pl/feedback/thanks/"
    );

    let (_, headers, _) = s.send(form("kind=idea&text=+&lang=en")).await;
    assert_eq!(
        headers[header::LOCATION],
        "https://vilnaos.com/feedback/?error=text#error-text"
    );

    // A bot that fills the hidden field is thanked; nothing is kept.
    let (_, headers, _) = s
        .send(form("kind=bug&text=buy+now&website=spam.example"))
        .await;
    assert_eq!(
        headers[header::LOCATION],
        "https://vilnaos.com/feedback/thanks/"
    );

    let (_, list) = s.admin("GET", "/admin/v1/feedback", None).await;
    let list = list.as_array().unwrap();
    assert_eq!(list.len(), 1);
    assert_eq!(list[0]["channel"], "site");
    assert_eq!(list[0]["may_publish"], true);
}

#[tokio::test]
async fn forget_deletes_reports_and_feedback_by_id() {
    let s = server();
    s.post("/v1/crash", crash(ID1)).await;
    s.post("/v1/feedback", json!({"id": ID2, "kind": "bug", "channel": "window", "text": "x", "contact": "a@b.example"})).await;
    let (status, body) = s.post("/v1/forget", json!({"ids": [ID1, ID2, ID3]})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["deleted"], 2);
    let (_, status) = s.get(&format!("/v1/status/{ID1}")).await;
    assert_eq!(status["status"], "unknown");
    let (_, list) = s.admin("GET", "/admin/v1/feedback", None).await;
    assert!(list.as_array().unwrap().is_empty());
}

#[tokio::test]
async fn approving_files_one_issue_and_milestones_comment_on_it() {
    let s = server();
    let (_, first) = s.post("/v1/crash", crash(ID1)).await;
    let signature = first["signature"].as_str().unwrap().to_owned();
    let (_, st) = s.get(&format!("/v1/status/{ID1}")).await;
    assert_eq!(st["status"], "received");
    assert!(st.get("issue_url").is_none());

    let (status, body) = s
        .admin(
            "POST",
            &format!("/admin/v1/signatures/{signature}/approve"),
            None,
        )
        .await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    assert_eq!(body["issue"], 101);
    let (status, body) = s
        .admin(
            "POST",
            &format!("/admin/v1/signatures/{signature}/approve"),
            None,
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["created"], false);
    {
        let issues = s.github.issues.lock().unwrap();
        assert_eq!(issues.len(), 1);
        assert!(
            issues[0]
                .0
                .starts_with("[auto] org.gnome.Nautilus: SIGSEGV")
        );
        assert!(
            issues[0]
                .1
                .contains("nautilus+0x1200 in nautilus_window_open [ffeeddccbbaa99887766]")
        );
    }

    let (_, st) = s.get(&format!("/v1/status/{ID1}")).await;
    assert_eq!(st["status"], "known");
    assert_eq!(
        st["issue_url"],
        "https://github.com/StDenis1980/VilnaOS/issues/101"
    );

    // The second report is milestone 2: one comment, filed in the background.
    s.post("/v1/crash", crash(ID2)).await;
    for _ in 0..100 {
        if !s.github.comments.lock().unwrap().is_empty() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
    let comments = s.github.comments.lock().unwrap().clone();
    assert_eq!(comments.len(), 1);
    assert_eq!(comments[0].0, 101);
    assert!(comments[0].1.starts_with("2 reports so far"));

    let (status, _) = s
        .admin(
            "POST",
            &format!("/admin/v1/signatures/{signature}/fixed"),
            Some(json!({"version": "48.3"})),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    let (_, st) = s.get(&format!("/v1/status/{ID2}")).await;
    assert_eq!(st["status"], "fixed");
    assert_eq!(st["fixed_in"], "48.3");
    let (_, receipt) = s.post("/v1/crash", crash(ID3)).await;
    assert_eq!(receipt["status"], "fixed");
}

#[tokio::test]
async fn new_issues_stop_at_the_daily_limit() {
    let s = server_with(Config {
        database: ":memory:".into(),
        admin_key: Some(KEY.into()),
        max_new_issues_per_day: 1,
        ..Config::default()
    });
    let mut signatures = Vec::new();
    for (id, signal) in [(ID1, "SIGSEGV"), (ID2, "SIGABRT")] {
        let mut c = crash(id);
        c["signal"] = json!(signal);
        let (_, r) = s.post("/v1/crash", c).await;
        signatures.push(r["signature"].as_str().unwrap().to_owned());
    }
    let (status, _) = s
        .admin(
            "POST",
            &format!("/admin/v1/signatures/{}/approve", signatures[0]),
            None,
        )
        .await;
    assert_eq!(status, StatusCode::CREATED);
    let (status, body) = s
        .admin(
            "POST",
            &format!("/admin/v1/signatures/{}/approve", signatures[1]),
            None,
        )
        .await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(body["error"], "issue_quota");
    s.clock.fetch_add(86_400, Ordering::SeqCst);
    let (status, _) = s
        .admin(
            "POST",
            &format!("/admin/v1/signatures/{}/approve", signatures[1]),
            None,
        )
        .await;
    assert_eq!(status, StatusCode::CREATED);
}

#[tokio::test]
async fn admin_needs_the_key_and_is_off_without_one() {
    let s = server();
    let (status, headers, _) = s
        .send(Request::get("/admin").body(Body::empty()).unwrap())
        .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert!(
        headers[header::WWW_AUTHENTICATE]
            .to_str()
            .unwrap()
            .starts_with("Basic")
    );

    use base64::Engine;
    let basic = base64::engine::general_purpose::STANDARD.encode(format!("denys:{KEY}"));
    let req = Request::get("/admin")
        .header(header::AUTHORIZATION, format!("Basic {basic}"))
        .body(Body::empty())
        .unwrap();
    let (status, _, page) = s.send(req).await;
    assert_eq!(status, StatusCode::OK);
    assert!(page.as_str().unwrap().contains("<h1>Crashes</h1>"));

    let wrong = Request::get("/admin/v1/feedback")
        .header(header::AUTHORIZATION, "Bearer nope")
        .body(Body::empty())
        .unwrap();
    assert_eq!(s.send(wrong).await.0, StatusCode::UNAUTHORIZED);

    let off = server_with(Config {
        database: ":memory:".into(),
        admin_key: None,
        ..Config::default()
    });
    assert_eq!(off.get("/admin").await.0, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn one_client_is_rate_limited() {
    let s = server();
    for i in 0..30 {
        let id = format!("{i:08x}-0000-4000-8000-000000000000");
        let (status, _) = s.post("/v1/crash", crash(&id)).await;
        assert!(status.is_success(), "{i}: {status}");
    }
    let (status, _) = s
        .post("/v1/crash", crash("ffffffff-0000-4000-8000-000000000000"))
        .await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS);
}

#[tokio::test]
async fn reports_are_purged_after_the_retention_and_counters_stay() {
    let s = server();
    s.post("/v1/crash", crash(ID1)).await;
    s.post(
        "/v1/feedback",
        json!({"id": ID2, "text": "x", "channel": "window"}),
    )
    .await;
    let now = s.clock.load(Ordering::SeqCst);
    assert_eq!(s.state.store.purge(now - 90 * 86_400).unwrap(), (0, 0));
    assert_eq!(s.state.store.purge(now + 1).unwrap(), (1, 1));
    let (_, groups) = s.admin("GET", "/admin/v1/signatures", None).await;
    assert_eq!(groups[0]["count"], 1);
}

#[tokio::test]
async fn no_ip_address_or_contact_outlives_its_row_in_the_database_file() {
    let path =
        std::env::temp_dir().join(format!("vilna-reports-test-{}.sqlite", std::process::id()));
    let _ = std::fs::remove_file(&path);
    let s = server_with(Config {
        database: path.to_string_lossy().into_owned(),
        admin_key: Some(KEY.into()),
        ..Config::default()
    });
    s.post("/v1/crash", crash(ID1)).await;
    s.post(
        "/v1/feedback",
        json!({"id": ID2, "text": "x", "channel": "window", "contact": "petro@example.org"}),
    )
    .await;
    s.post("/v1/forget", json!({"ids": [ID2]})).await;
    drop(s);
    let mut bytes = std::fs::read(&path).unwrap();
    for suffix in ["-wal", "-shm"] {
        let mut extra = path.clone().into_os_string();
        extra.push(suffix);
        bytes.extend(std::fs::read(&extra).unwrap_or_default());
        let _ = std::fs::remove_file(&extra);
    }
    let _ = std::fs::remove_file(&path);
    let text = String::from_utf8_lossy(&bytes);
    assert!(!text.contains("203.0.113.7"));
    assert!(
        !text.contains("petro@example.org"),
        "secure_delete leaves no copy of a forgotten contact"
    );
}
