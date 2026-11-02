// SPDX-License-Identifier: GPL-3.0-or-later
//! herkosd: the Herkos service Gloxi talks to over a Unix socket.
//!
//! Step 2 of the Rust plan (docs/adr/0013-herkos-rust.md): the protocol and the
//! socket, without decisions yet. Herkos-lite (Python) stays the reference;
//! `herkos/conformance/` checks that both answer the same way.

mod rpc;

use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::thread;

use serde_json::Value;

/// A longer message closes the connection (protocol.md).
const MAX_LINE: usize = 1024 * 1024;

const USAGE: &str =
    "usage: herkosd serve [--socket PATH] [--confirm deny]\n       herkosd --version";

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

fn serve_connection(stream: UnixStream, uid: u32) {
    // Not ours, or unknown: no answer at all.
    match rustix::net::sockopt::socket_peercred(&stream) {
        Ok(cred) if cred.uid.as_raw() == uid => {}
        _ => return,
    }
    let Ok(mut writer) = stream.try_clone() else {
        return;
    };
    let mut reader = BufReader::new(stream);
    let mut conn = rpc::Connection::default();
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

fn serve(socket: &Path) -> Result<(), String> {
    prepare_socket_dir(socket)?;
    // Born 0600: nobody else gets a window between bind and chmod.
    let old = rustix::process::umask(rustix::fs::Mode::from_raw_mode(0o177));
    let bound = UnixListener::bind(socket);
    rustix::process::umask(old);
    let listener = bound.map_err(|e| format!("{}: {e}", socket.display()))?;
    fs::set_permissions(socket, fs::Permissions::from_mode(0o600))
        .map_err(|e| format!("{}: {e}", socket.display()))?;
    // Herkos-lite and this skeleton serve one person: the uid that started them.
    let uid = rustix::process::getuid().as_raw();
    eprintln!("herkosd: listening on {}", socket.display());
    for stream in listener.incoming() {
        match stream {
            Ok(s) => {
                thread::spawn(move || serve_connection(s, uid));
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
        Some("serve") => {
            let mut socket = None;
            let mut rest = args[1..].iter();
            while let Some(a) = rest.next() {
                match a.as_str() {
                    "--socket" => socket = rest.next().map(PathBuf::from),
                    // No decisions yet, so nothing to confirm: every action is denied.
                    "--confirm" => match rest.next().map(String::as_str) {
                        Some("deny") => {}
                        _ => return Err("--confirm: only 'deny' is supported for now".into()),
                    },
                    _ => return Err(format!("unknown argument {a}\n{USAGE}")),
                }
            }
            let socket = match socket {
                Some(p) if p.is_absolute() => p,
                Some(_) => return Err("--socket must be an absolute path".into()),
                None => default_socket()?,
            };
            serve(&socket)
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
