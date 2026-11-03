// SPDX-License-Identifier: GPL-3.0-or-later
//! herkosd: the Herkos service Gloxi talks to over a Unix socket.
//!
//! Steps 2-3 of the Rust plan (docs/adr/0013-herkos-rust.md): the protocol,
//! the socket, the gate's decisions and the Mitos journal. Tools arrive in
//! step 4; until then a known tool is `denied_no_tool`. Herkos-lite (Python)
//! stays the reference;
//! `herkos/conformance/` checks that both answer the same way.

mod config;
mod confirm;
mod gate;
mod grants;
mod keros_model;
mod mcp;
mod mitos;
mod rpc;

use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Arc;
use std::thread;

use serde_json::Value;

/// A longer message closes the connection (protocol.md).
const MAX_LINE: usize = 1024 * 1024;

const USAGE: &str = "usage: herkosd serve [--socket PATH] [--confirm auto|gui|terminal|deny] [--confirm-command PATH]\n       herkosd journal verify\n       herkosd --version";

fn default_socket() -> Result<PathBuf, String> {
    let runtime = std::env::var_os("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .ok_or("XDG_RUNTIME_DIR is not set to an absolute path")?;
    Ok(runtime.join("vilna").join("herkos.sock"))
}

/// Create the socket's folder as 0700, make sure it is ours, clear a stale socket.
fn prepare_socket_dir(path: &Path) -> Result<(), String> {
    let folder = path.parent().ok_or("the socket path has no folder")?;
    fs::create_dir_all(folder).map_err(|e| format!("{}: {e}", folder.display()))?;
    let meta = fs::symlink_metadata(folder).map_err(|e| format!("{}: {e}", folder.display()))?;
    if !meta.is_dir() {
        return Err(format!("{} is not a folder", folder.display()));
    }
    if meta.uid() != rustix::process::getuid().as_raw() {
        return Err(format!("{} belongs to another user", folder.display()));
    }
    if meta.mode() & 0o077 != 0 {
        fs::set_permissions(folder, fs::Permissions::from_mode(0o700))
            .map_err(|e| format!("{}: {e}", folder.display()))?;
    }
    match fs::symlink_metadata(path) {
        Err(_) => Ok(()),
        Ok(m) if m.file_type().is_socket() => {
            // Stale socket from an earlier run.
            fs::remove_file(path).map_err(|e| format!("{}: {e}", path.display()))
        }
        Ok(_) => Err(format!("{} exists and is not a socket", path.display())),
    }
}

fn encode(msg: &Value) -> Vec<u8> {
    let mut out = serde_json::to_vec(msg).unwrap_or_default();
    out.push(b'\n');
    out
}

fn serve_connection(stream: UnixStream, uid: u32, gate: Arc<gate::Gate>) {
    // Not ours, or unknown: no answer at all.
    match rustix::net::sockopt::socket_peercred(&stream) {
        Ok(cred) if cred.uid.as_raw() == uid => {}
        _ => return,
    }
    let Ok(mut writer) = stream.try_clone() else {
        return;
    };
    let mut reader = BufReader::new(stream);
    let mut conn = rpc::Connection::new(gate);
    let mut line = Vec::new();
    loop {
        line.clear();
        let limit = (MAX_LINE + 1) as u64;
        match (&mut reader).take(limit).read_until(b'\n', &mut line) {
            Ok(0) | Err(_) => return,
            Ok(_) => {}
        }
        if line.len() > MAX_LINE && line.last() != Some(&b'\n') {
            let msg = rpc::error(&Value::Null, rpc::INVALID_REQUEST, "message too long");
            let _ = writer.write_all(&encode(&msg));
            return;
        }
        let response = match serde_json::from_slice::<Value>(&line) {
            Err(_) => Some(rpc::error(&Value::Null, rpc::PARSE_ERROR, "not valid JSON")),
            Ok(msg) => conn.dispatch(&msg),
        };
        if let Some(r) = response {
            if writer.write_all(&encode(&r)).is_err() {
                return;
            }
        }
    }
}

fn build_gate(confirmer: Box<dyn gate::Confirmer>) -> Result<gate::Gate, String> {
    let config = config::load_config(&config::default_config_path())?;
    let journal = mitos::Journal::open(&config.journal_path())
        .map_err(|e| format!("the journal is broken, refusing to start: {e}"))?;
    let grants = grants::GrantStore::new(&config.grants_path(), grants::now);
    if let Some(e) = &grants.load_error {
        eprintln!("herkosd: permissions ignored: {e}");
    }
    let manifests = herkos_core::manifest::Registry::builtin().map_err(|e| e.0)?;
    let tools = start_tools(&config);
    let model_check = match &config.keros_model {
        None => None,
        Some(url) => match keros_model::Judge::new(url) {
            Ok(judge) => Some(keros_model::ModelCheck::new(judge, config.keros_budget)),
            Err(e) => {
                eprintln!("herkosd: Keros model check off: {e}");
                None
            }
        },
    };
    Ok(gate::Gate::new(
        config,
        manifests,
        Box::new(tools),
        grants,
        journal,
        confirmer,
    )
    .with_model_check(model_check))
}

/// An executable on PATH (the tool servers are installed like any program).
fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .map(|dir| dir.join(name))
        .find(|p| fs::metadata(p).is_ok_and(|m| m.is_file() && m.mode() & 0o111 != 0))
}

fn start_tool(tools: &mut mcp::McpTools, name: &str, program: &str, args: Vec<String>) {
    let Some(path) = which(program) else {
        eprintln!("herkosd: {program} is not installed; no {name}");
        return;
    };
    let mut command = vec![path.display().to_string()];
    command.extend(args);
    match mcp::McpStdio::start(name, &command, mcp::CALL_TIMEOUT) {
        Ok(t) => tools.add(t),
        Err(e) => eprintln!("herkosd: {name} failed to start: {e}"),
    }
}

/// The files tool on the allowed folders and the backup tool (ADR 0007), if installed.
fn start_tools(config: &config::Config) -> mcp::McpTools {
    let mut tools = mcp::McpTools::default();
    let roots: Vec<&PathBuf> = config.allowed_roots.iter().filter(|r| r.is_dir()).collect();
    if roots.is_empty() {
        eprintln!("herkosd: none of allowed_roots exists; no files tool");
    } else {
        let backups = config.state_dir.join("backups").join("files");
        let mut args = vec!["--backup-dir".to_owned(), backups.display().to_string()];
        for r in roots {
            args.extend(["--root".to_owned(), r.display().to_string()]);
        }
        start_tool(&mut tools, "org.vilna.mcp.files", "vilna-mcp-files", args);
    }
    start_tool(
        &mut tools,
        "org.vilna.mcp.backup",
        "vilna-mcp-backup",
        Vec::new(),
    );
    tools
}

fn serve(socket: &Path, confirmer: Box<dyn gate::Confirmer>) -> Result<(), String> {
    let gate = Arc::new(build_gate(confirmer)?);
    prepare_socket_dir(socket)?;
    // Born 0600: nobody else gets a window between bind and chmod.
    let old = rustix::process::umask(rustix::fs::Mode::from_raw_mode(0o177));
    let bound = UnixListener::bind(socket);
    rustix::process::umask(old);
    let listener = bound.map_err(|e| format!("{}: {e}", socket.display()))?;
    fs::set_permissions(socket, fs::Permissions::from_mode(0o600))
        .map_err(|e| format!("{}: {e}", socket.display()))?;
    // Until step 6 herkosd serves one person: the uid that started it.
    let uid = rustix::process::getuid().as_raw();
    let start = serde_json::json!({
        "server": concat!("herkosd/", env!("CARGO_PKG_VERSION")),
        "socket": socket.display().to_string(),
    });
    gate.journal()
        .append("start", start, None)
        .map_err(|e| e.0)?;
    eprintln!("herkosd: listening on {}", socket.display());
    for stream in listener.incoming() {
        match stream {
            Ok(s) => {
                let gate = Arc::clone(&gate);
                thread::spawn(move || serve_connection(s, uid, gate));
            }
            Err(e) => eprintln!("herkosd: accept: {e}"),
        }
    }
    Ok(())
}

fn run(args: &[String]) -> Result<(), String> {
    match args.first().map(String::as_str) {
        Some("--version") => {
            println!("herkosd {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
        // For the conformance tests: Keros rules over JSON values, one per line.
        Some("keros-scan") => {
            for line in std::io::stdin().lines() {
                let line = line.map_err(|e| e.to_string())?;
                let value: Value = serde_json::from_str(&line).map_err(|e| e.to_string())?;
                let rules: Vec<String> = herkos_core::keros::scan(&value)
                    .into_iter()
                    .map(|f| f.rule)
                    .collect();
                println!("{}", serde_json::json!(rules));
            }
            Ok(())
        }
        Some("journal") if args.get(1).map(String::as_str) == Some("verify") => {
            let path = config::load_config(&config::default_config_path())?.journal_path();
            match mitos::verify(&path) {
                Ok(v) => {
                    println!("journal OK: {} records ({})", v.records, path.display());
                    Ok(())
                }
                Err((line, why)) => Err(format!("journal BROKEN at line {line}: {why}")),
            }
        }
        Some("serve") => {
            let mut socket = None;
            let mut mode = "auto".to_owned();
            let mut window: Option<PathBuf> = None;
            let mut rest = args[1..].iter();
            while let Some(a) = rest.next() {
                match a.as_str() {
                    "--socket" => socket = rest.next().map(PathBuf::from),
                    "--confirm" => match rest.next().map(String::as_str) {
                        Some(m @ ("auto" | "gui" | "terminal" | "deny")) => mode = m.to_owned(),
                        _ => return Err("--confirm: auto, gui, terminal or deny".into()),
                    },
                    // A window program other than herkos-dialog (for tests).
                    "--confirm-command" => match rest.next() {
                        Some(p) if Path::new(p).is_absolute() => window = Some(PathBuf::from(p)),
                        _ => return Err("--confirm-command needs an absolute path".into()),
                    },
                    _ => return Err(format!("unknown argument {a}\n{USAGE}")),
                }
            }
            let socket = match socket {
                Some(p) if p.is_absolute() => p,
                Some(_) => return Err("--socket must be an absolute path".into()),
                None => default_socket()?,
            };
            let custom = window.is_some();
            let window = window.or_else(|| which("herkos-dialog"));
            let confirmer: Box<dyn gate::Confirmer> = match (mode.as_str(), window) {
                // An explicit window program wins over the mode, as in Herkos-lite.
                (_, Some(w)) if custom => Box::new(confirm::Window::new(&w)),
                ("deny", _) => Box::new(gate::DenyAll),
                ("terminal", _) => Box::new(confirm::Terminal),
                ("gui", Some(w)) => Box::new(confirm::Window::new(&w)),
                ("gui", None) => return Err("--confirm gui: herkos-dialog is not installed".into()),
                (_, w) => confirm::auto(w),
            };
            serve(&socket, confirmer)
        }
        _ => Err(USAGE.into()),
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("herkosd: {e}");
            ExitCode::from(2)
        }
    }
}
