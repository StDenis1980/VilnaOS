// SPDX-License-Identifier: GPL-3.0-or-later
//! `vilna-reports-server`: run by `deploy/vilna-reports.service`.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use vilna_reports_server::app::{AppState, Clock, router};
use vilna_reports_server::config::Config;
use vilna_reports_server::store::Store;
use vilna_reports_server::tracker::{Disabled, GitHub, Tracker};

fn now() -> i64 {
    chrono::Utc::now().timestamp()
}

/// systemd stops the service with SIGTERM; Ctrl+C is for a terminal.
async fn stopped() {
    use tokio::signal::unix::{SignalKind, signal};
    match signal(SignalKind::terminate()) {
        Ok(mut term) => {
            tokio::select! {
                _ = term.recv() => {}
                _ = tokio::signal::ctrl_c() => {}
            }
        }
        Err(_) => {
            let _ = tokio::signal::ctrl_c().await;
        }
    }
}

fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("vilna-reports-server: {e}");
            std::process::ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), String> {
    let config = Config::from_env()?;
    let store = Store::open(&config.database).map_err(|e| format!("{}: {e}", config.database))?;
    let tracker: Box<dyn Tracker> = match &config.github_token {
        Some(token) => Box::new(GitHub::new(config.github_repo.clone(), token.clone())),
        None => {
            eprintln!("no github_token credential: crashes are counted, no issues are filed");
            Box::new(Disabled)
        }
    };
    if config.admin_key.is_none() {
        eprintln!("no admin_key credential: /admin is off");
    }
    let listen = config.listen;
    let retention = config.retention_days.saturating_mul(86_400);
    let clock: Clock = Arc::new(now);
    let state = Arc::new(AppState::new(config, store, tracker, clock));
    let runtime = tokio::runtime::Runtime::new().map_err(|e| format!("runtime: {e}"))?;
    runtime.block_on(async move {
        let purger = state.clone();
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(Duration::from_secs(3600));
            loop {
                tick.tick().await;
                match purger.store.purge(now().saturating_sub(retention)) {
                    Ok((0, 0)) => {}
                    Ok((c, f)) => eprintln!(
                        "deleted {c} crash reports and {f} feedback older than the retention"
                    ),
                    Err(e) => eprintln!("purge: {e}"),
                }
            }
        });
        let listener = tokio::net::TcpListener::bind(listen)
            .await
            .map_err(|e| format!("{listen}: {e}"))?;
        eprintln!("listening on {listen}");
        axum::serve(
            listener,
            router(state).into_make_service_with_connect_info::<SocketAddr>(),
        )
        .with_graceful_shutdown(stopped())
        .await
        .map_err(|e| format!("serve: {e}"))
    })
}
