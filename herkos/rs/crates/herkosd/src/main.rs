// SPDX-License-Identifier: GPL-3.0-or-later
//! herkosd: the Herkos service Gloxi talks to over a Unix socket.
//!
//! Steps 2-3 of the Rust plan (docs/adr/0013-herkos-rust.md): the protocol,
//! the socket, the gate's decisions and the Mitos journal. Tools arrive in
//! step 4; until then a known tool is `denied_no_tool`. Herkos-lite (Python)
//! stays the reference;
//! `herkos/conformance/` checks that both answer the same way.

mod catalogue;
mod config;
mod confirm;
mod ext;
mod gate;
mod grants;
mod keros_model;
mod mcp;
mod mitos;
mod passwd;
mod pins;
mod programs;
mod rpc;
mod snapshots;
mod system;
mod systool;
mod units;
mod vpntool;

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

const USAGE: &str = "usage: herkosd serve [--socket PATH] [--confirm auto|gui|terminal|deny] [--confirm-command PATH]\n       herkosd system [--socket PATH] [--system-config PATH] [--config PATH] [--state DIR] [--passwd PATH] [--tools-dir DIR] [--start-command PATH]\n       herkosd journal verify [--file PATH]\n       herkosd mitos-setup [--system-config PATH] [--passwd PATH]\n       herkosd system-tool UID\n       herkosd vpn-tool UID\n       herkosd ext-unit MANIFEST.toml\n       herkosd ext-exec PROGRAM ARGS-FILE\n       herkosd --version";

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

/// One line of the protocol, as read from a connection.
enum Message {
    Json(Value),
    Bad,
    TooLong,
    End,
}

fn read_message(reader: &mut BufReader<UnixStream>) -> Message {
    let mut line = Vec::new();
    let limit = (MAX_LINE + 1) as u64;
    match reader.take(limit).read_until(b'\n', &mut line) {
        Ok(0) | Err(_) => return Message::End,
        Ok(_) => {}
    }
    if line.len() > MAX_LINE && line.last() != Some(&b'\n') {
        return Message::TooLong;
    }
    serde_json::from_slice(&line).map_or(Message::Bad, Message::Json)
}

/// Gloxi's side of the protocol; the caller has checked who is calling.
fn serve_connection(stream: UnixStream, gate: Arc<gate::Gate>) {
    let Ok(mut writer) = stream.try_clone() else {
        return;
    };
    let mut reader = BufReader::new(stream);
    let mut conn = rpc::Connection::new(gate);
    loop {
        let response = match read_message(&mut reader) {
            Message::End => return,
            Message::TooLong => {
                let msg = rpc::error(&Value::Null, rpc::INVALID_REQUEST, "message too long");
                let _ = writer.write_all(&encode(&msg));
                return;
            }
            Message::Bad => Some(rpc::error(&Value::Null, rpc::PARSE_ERROR, "not valid JSON")),
            Message::Json(msg) => conn.dispatch(&msg),
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
    let tools = start_tools(&config);
    build_gate_from(config, Box::new(tools), confirmer)
}

fn build_gate_from(
    config: config::Config,
    tools: Box<dyn gate::Tools>,
    confirmer: Box<dyn gate::Confirmer>,
) -> Result<gate::Gate, String> {
    let journal = mitos::Journal::open(&config.journal_path())
        .map_err(|e| format!("the journal is broken, refusing to start: {e}"))?;
    let grants = grants::GrantStore::new(&config.grants_path(), grants::now);
    if let Some(e) = &grants.load_error {
        eprintln!("herkosd: permissions ignored: {e}");
    }
    let manifests = catalogue::installed().map_err(|e| e.0)?;
    let model_check = match &config.keros_model {
        None => None,
        Some(url) => match keros_model::Judge::new(url) {
            Ok(judge) => Some(keros_model::ModelCheck::new(
                with_model_key(judge),
                config.keros_budget,
            )),
            Err(e) => {
                eprintln!("herkosd: Keros model check off: {e}");
                None
            }
        },
    };
    Ok(
        gate::Gate::new(config, manifests, tools, grants, journal, confirmer)
            .with_model_check(model_check),
    )
}

/// Where the key of the shared local model is, when it wants one:
/// herkosd.service sets it to vilna-llm's key (ADR 0025).
const KEROS_KEY_FILE_ENV: &str = "HERKOS_KEROS_KEY_FILE";

/// The Keros judge with the model's key, when there is one to read. Without
/// it the check stays on, and a server that wants a key answers
/// "unavailable": rules and levels still hold.
fn with_model_key(judge: keros_model::Judge) -> keros_model::Judge {
    let Some(path) = std::env::var_os(KEROS_KEY_FILE_ENV).filter(|p| !p.is_empty()) else {
        return judge;
    };
    match keros_model::read_key(Path::new(&path)) {
        Ok(key) => judge.with_key(key),
        Err(e) => {
            eprintln!("herkosd: Keros model key not read: {e}");
            judge
        }
    }
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
        Ok(t) => {
            let name = name.to_owned();
            tools.add_relaunching(
                t,
                Box::new(move || mcp::McpStdio::start(&name, &command, mcp::CALL_TIMEOUT)),
            );
        }
        Err(e) => eprintln!("herkosd: {name} failed to start: {e}"),
    }
}

/// The tools whose manifests say how to start them (ADR 0017), if installed.
fn start_tools(config: &config::Config) -> mcp::McpTools {
    let mut tools = mcp::McpTools::default();
    let Ok(manifests) = catalogue::installed() else {
        return tools;
    };
    let roots: Vec<PathBuf> = config
        .allowed_roots
        .iter()
        .filter(|r| r.is_dir())
        .cloned()
        .collect();
    let backups = config.state_dir.join("backups").join("files");
    let programs = programs::ProgramStore::new(&config.state_dir.join("programs.json"));
    for manifest in manifests.iter() {
        if !manifests.is_builtin(&manifest.tool) && !programs.enabled(&manifest.tool) {
            eprintln!(
                "herkosd: {} is turned off in Gloxi; not started",
                manifest.tool
            );
            continue;
        }
        let Some(launch) = &manifest.launch else {
            eprintln!("herkosd: {} has no [launch]; not started", manifest.tool);
            continue;
        };
        if launch.flatpak.is_some() {
            eprintln!(
                "herkosd: {} runs only under the system Herkos; not started",
                manifest.tool
            );
            continue;
        }
        match mcp::launch_args(launch, &backups, &roots) {
            Some(args) => start_tool(&mut tools, &manifest.tool, &launch.program, args),
            None => eprintln!(
                "herkosd: none of allowed_roots exists; no {}",
                manifest.tool
            ),
        }
    }
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
                // Not ours, or unknown: no answer at all.
                match rustix::net::sockopt::socket_peercred(&s) {
                    Ok(cred) if cred.uid.as_raw() == uid => {
                        thread::spawn(move || serve_connection(s, gate));
                    }
                    _ => {}
                }
            }
            Err(e) => eprintln!("herkosd: accept: {e}"),
        }
    }
    Ok(())
}

/// The listening socket systemd passes as stdin (`StandardInput=socket`
/// in `herkosd.service`; adopting fd 3 instead would need `unsafe`).
fn socket_from_stdin() -> Result<UnixListener, String> {
    use std::os::fd::AsFd;
    let stdin = std::io::stdin();
    let fd = stdin.as_fd();
    let is_socket = rustix::fs::fstat(fd).is_ok_and(|st| {
        rustix::fs::FileType::from_raw_mode(st.st_mode) == rustix::fs::FileType::Socket
    });
    if !is_socket || !rustix::net::sockopt::socket_acceptconn(fd).unwrap_or(false) {
        return Err(
            "stdin is not a listening socket: start herkosd.socket, or pass --socket".into(),
        );
    }
    let owned = fd.try_clone_to_owned().map_err(|e| e.to_string())?;
    Ok(UnixListener::from(owned))
}

/// A socket of our own for `system --socket` (tests, development): anybody
/// may connect, as with `herkosd.socket`; the uid decides who gets answers.
fn bind_public(path: &Path) -> Result<UnixListener, String> {
    let folder = path.parent().ok_or("the socket path has no folder")?;
    fs::create_dir_all(folder).map_err(|e| format!("{}: {e}", folder.display()))?;
    if let Ok(m) = fs::symlink_metadata(path) {
        if !m.file_type().is_socket() {
            return Err(format!("{} exists and is not a socket", path.display()));
        }
        fs::remove_file(path).map_err(|e| format!("{}: {e}", path.display()))?;
    }
    let listener = UnixListener::bind(path).map_err(|e| format!("{}: {e}", path.display()))?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o666))
        .map_err(|e| format!("{}: {e}", path.display()))?;
    Ok(listener)
}

fn absolute(flag: &str, value: Option<&String>) -> Result<PathBuf, String> {
    match value {
        Some(p) if Path::new(p).is_absolute() => Ok(PathBuf::from(p)),
        _ => Err(format!("{flag} needs an absolute path")),
    }
}

fn run_system(args: &[String]) -> Result<(), String> {
    let mut socket = None;
    let mut system_config = PathBuf::from(system::SYSTEM_CONFIG);
    let mut config_path = PathBuf::from(system::CONFIG);
    // systemd's StateDirectory=herkos, else the usual place.
    let mut state = std::env::var_os("STATE_DIRECTORY")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .unwrap_or_else(|| PathBuf::from(system::STATE));
    let mut passwd_path = PathBuf::from("/etc/passwd");
    // systemd's RuntimeDirectory=herkos-tools: where the tools connect.
    let mut tools_dir = std::env::var_os("RUNTIME_DIRECTORY")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .unwrap_or_else(|| PathBuf::from("/run/herkos-tools"));
    let mut start = PathBuf::from("systemctl");
    let mut rest = args.iter();
    while let Some(a) = rest.next() {
        match a.as_str() {
            "--socket" => socket = Some(absolute(a, rest.next())?),
            "--system-config" => system_config = absolute(a, rest.next())?,
            "--config" => config_path = absolute(a, rest.next())?,
            "--state" => state = absolute(a, rest.next())?,
            "--passwd" => passwd_path = absolute(a, rest.next())?,
            "--tools-dir" => tools_dir = absolute(a, rest.next())?,
            // A stand-in for systemctl (tests).
            "--start-command" => start = absolute(a, rest.next())?,
            _ => return Err(format!("unknown argument {a}\n{USAGE}")),
        }
    }
    let accounts = passwd::load(&passwd_path)?;
    let people = system::load_system(&system_config, &accounts)?;
    if people.people.is_empty() {
        eprintln!(
            "herkosd: nobody is listed in {}; every caller is refused",
            system_config.display()
        );
    }
    let listener = match &socket {
        Some(p) => bind_public(p)?,
        None => socket_from_stdin()?,
    };
    let units = units::Units {
        dir: tools_dir,
        start,
    };
    let tools: Box<system::ToolsFor> =
        Box::new(move |config, person| units.tools_for(config, person));
    let sys = Arc::new(system::System::new(people, config_path, state, tools));
    eprintln!(
        "herkosd: system service for {} people",
        sys.config.people.len()
    );
    system::serve(&listener, sys);
    Ok(())
}

fn run_mitos_setup(args: &[String]) -> Result<(), String> {
    let mut system_config = PathBuf::from(system::SYSTEM_CONFIG);
    let mut passwd_path = PathBuf::from("/etc/passwd");
    let mut rest = args.iter();
    while let Some(a) = rest.next() {
        match a.as_str() {
            "--system-config" => system_config = absolute(a, rest.next())?,
            "--passwd" => passwd_path = absolute(a, rest.next())?,
            _ => return Err(format!("unknown argument {a}\n{USAGE}")),
        }
    }
    let accounts = passwd::load(&passwd_path)?;
    let people = system::load_system(&system_config, &accounts)?;
    let snapper = ["/usr/bin/snapper", "/usr/sbin/snapper"]
        .into_iter()
        .map(PathBuf::from)
        .find(|p| p.is_file());
    let Some(snapper) = snapper else {
        // Not an error: without snapper, herkosd works as before, without snapshots.
        eprintln!("herkosd mitos-setup: snapper is not installed; no snapshots of homes");
        return Ok(());
    };
    let homes: Vec<passwd::Account> = people.people.into_iter().map(|p| p.account).collect();
    for line in snapshots::setup(&homes, &snapper, Path::new(system::SNAPPER_CONFIGS)) {
        eprintln!("herkosd mitos-setup: {line}");
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
        Some("system") => run_system(&args[1..]),
        // As root, before herkosd starts: snapper configs of people's homes (ADR 0026).
        Some("mitos-setup") => run_mitos_setup(&args[1..]),
        // As root, for one person: the system's saved states (vilna-mcp-system@.service).
        Some("system-tool") => systool::main(&args[1..]),
        // As root, for one person: the VPN kill switch and DNS (vilna-mcp-vpn@.service).
        Some("vpn-tool") => vpntool::main(&args[1..]),
        // In a vilna-mcp-ext-<name>@<uid> unit, as the person (ADR 0017).
        Some("ext-exec") => match (args.get(1), args.get(2), args.len()) {
            (Some(program), Some(file), 3) if Path::new(file).is_absolute() => {
                ext::exec(program, Path::new(file))
            }
            _ => Err("usage: herkosd ext-exec PROGRAM /ABSOLUTE/ARGS-FILE".into()),
        },
        // For the herkos-manifests package: the unit of a manifest's program.
        Some("ext-unit") => match (args.get(1), args.len()) {
            (Some(file), 2) => {
                let text = fs::read_to_string(file).map_err(|e| format!("{file}: {e}"))?;
                let manifest =
                    herkos_core::manifest::parse_manifest_str(&text, file).map_err(|e| e.0)?;
                print!("{}", ext::unit(&manifest)?);
                Ok(())
            }
            _ => Err("usage: herkosd ext-unit MANIFEST.toml".into()),
        },
        Some("journal") if args.get(1).map(String::as_str) == Some("verify") => {
            let path = match args.get(2).map(String::as_str) {
                Some("--file") => absolute("--file", args.get(3))?,
                Some(a) => return Err(format!("unknown argument {a}\n{USAGE}")),
                None => config::load_config(&config::default_config_path())?.journal_path(),
            };
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
