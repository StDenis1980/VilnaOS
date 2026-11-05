// SPDX-License-Identifier: GPL-3.0-or-later
//! Tools that act as the person, started by systemd (ADR 0013, step 6).
//!
//! The system `herkosd` has no right to become anybody. For each person it
//! asks systemd to start fixed template units, `vilna-mcp-files@<uid>.service`
//! and `vilna-mcp-backup@<uid>.service`, which run the tool as that uid in a
//! sandbox (`herkos/systemd/`). The polkit rule lets the user `herkos` start
//! and stop only these units, and only for people's uids.
//!
//! The tool's stdin and stdout are a connection to a socket `herkosd` listens
//! on in `/run/herkos-tools/<uid>/` (`StandardInput=file:` of the unit). The
//! socket is `0600`, so the person's own programs cannot pose as the tool.
//! The files tool reads its allowed folders from `files.args` next to it
//! (`@file` arguments): readable by the tool, writable only by `herkosd`.

use std::fs;
use std::io::ErrorKind;
use std::os::unix::fs::{FileTypeExt, PermissionsExt};
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use crate::config::Config;
use crate::gate::Tools;
use crate::mcp::{self, McpStdio, McpTools};
use crate::system::Person;

/// How long systemd may take to start a tool and connect it.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Clone)]
pub struct Units {
    /// `RuntimeDirectory=herkos-tools` of herkosd.service.
    pub dir: PathBuf,
    /// Runs as `<start> start --no-block <unit>`: `systemctl`, or a stand-in in tests.
    pub start: PathBuf,
}

fn private_dir(path: &Path, mode: u32) -> Result<(), String> {
    fs::create_dir_all(path).map_err(|e| format!("{}: {e}", path.display()))?;
    fs::set_permissions(path, fs::Permissions::from_mode(mode))
        .map_err(|e| format!("{}: {e}", path.display()))
}

/// One argument per line, as argparse reads `@file`.
fn args_file(path: &Path, args: &[String]) -> Result<(), String> {
    if args.iter().any(|a| a.contains('\n') || a.contains('\r')) {
        return Err("a folder name with a line break".into());
    }
    let text: String = args.iter().map(|a| format!("{a}\n")).collect();
    let tmp = path.with_extension("tmp");
    fs::write(&tmp, text).map_err(|e| format!("{}: {e}", tmp.display()))?;
    fs::set_permissions(&tmp, fs::Permissions::from_mode(0o644))
        .map_err(|e| format!("{}: {e}", tmp.display()))?;
    fs::rename(&tmp, path).map_err(|e| format!("{}: {e}", path.display()))
}

impl Units {
    /// The files and backup tools of this person; a tool that does not come
    /// up is left out (its actions are `denied_no_tool`).
    pub fn tools_for(&self, config: &Config, person: &Person) -> Box<dyn Tools> {
        let mut tools = McpTools::default();
        let uid = person.account.uid;
        let dir = self.dir.join(uid.to_string());
        // 0711: the tool may read its files.args, nobody may list the folder.
        if let Err(e) = private_dir(&self.dir, 0o711).and_then(|()| private_dir(&dir, 0o711)) {
            eprintln!("herkosd: no tools for {}: {e}", person.account.name);
            return Box::new(tools);
        }
        // Undo backups belong to the person: they are copies of their files.
        let backups = person
            .account
            .home
            .join(".local/state/herkos/backups/files");
        let Ok(manifests) = crate::catalogue::installed() else {
            return Box::new(tools);
        };
        // From the manifests (ADR 0017). polkit lets herkos start only the
        // units it knows, so a manifest cannot make it start anything else.
        for manifest in manifests.iter() {
            let name = manifest.tool.as_str();
            let Some(launch) = &manifest.launch else {
                continue;
            };
            let unit = launch.program.as_str();
            let short = unit.trim_start_matches("vilna-mcp-");
            // A program from Flatpak gets only its folders, one per line (ext.rs).
            let args = if launch.flatpak.is_some() {
                let roots = &config.allowed_roots;
                (!roots.is_empty()).then(|| roots.iter().map(|r| r.display().to_string()).collect())
            } else {
                mcp::launch_args(launch, &backups, &config.allowed_roots)
            };
            let started = match args {
                None => Err("no allowed folders".to_owned()),
                // The unit reads its arguments from <short>.args (vilna-mcp-files@.service).
                Some(args) if !args.is_empty() => {
                    args_file(&dir.join(format!("{short}.args")), &args)
                        .and_then(|()| self.launch(name, unit, uid, &dir))
                }
                Some(_) => self.launch(name, unit, uid, &dir),
            };
            match started {
                Ok(t) => tools.add_relaunching(t, self.relaunch(name, unit, uid, &dir)),
                Err(e) => eprintln!("herkosd: {name} for {}: {e}", person.account.name),
            }
        }
        Box::new(tools)
    }

    /// Stops what is left of a tool that broke (it may hang rather than
    /// exit), then starts its unit again. polkit lets `herkos` stop and start
    /// these units, not restart them.
    fn relaunch(&self, name: &str, unit: &str, uid: u32, dir: &Path) -> mcp::Relaunch {
        let units = self.clone();
        let (name, unit, dir) = (name.to_owned(), unit.to_owned(), dir.to_owned());
        Box::new(move || {
            let _ = Command::new(&units.start)
                .args(["stop", &format!("{unit}@{uid}.service")])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .status();
            units.launch(&name, &unit, uid, &dir)
        })
    }

    fn launch(&self, name: &str, unit: &str, uid: u32, dir: &Path) -> Result<McpStdio, String> {
        let short = unit.trim_start_matches("vilna-mcp-");
        let socket = dir.join(format!("{short}.sock"));
        match fs::symlink_metadata(&socket) {
            Ok(m) if m.file_type().is_socket() => {
                fs::remove_file(&socket).map_err(|e| format!("{}: {e}", socket.display()))?;
            }
            Ok(_) => return Err(format!("{} is not a socket", socket.display())),
            Err(_) => {}
        }
        // Born 0600: only herkosd and systemd (root) can connect.
        let old = rustix::process::umask(rustix::fs::Mode::from_raw_mode(0o177));
        let bound = UnixListener::bind(&socket);
        rustix::process::umask(old);
        let listener = bound.map_err(|e| format!("{}: {e}", socket.display()))?;
        listener.set_nonblocking(true).map_err(|e| e.to_string())?;

        let unit_name = format!("{unit}@{uid}.service");
        let status = Command::new(&self.start)
            .args(["start", "--no-block", &unit_name])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .status()
            .map_err(|e| format!("{}: {e}", self.start.display()))?;
        if !status.success() {
            return Err(format!("could not start {unit_name} ({status})"));
        }
        let deadline = Instant::now() + CONNECT_TIMEOUT;
        let stream = loop {
            match listener.accept() {
                Ok((s, _)) => break s,
                Err(e) if e.kind() == ErrorKind::WouldBlock && Instant::now() < deadline => {
                    thread::sleep(Duration::from_millis(50));
                }
                Err(e) if e.kind() == ErrorKind::WouldBlock => {
                    return Err(format!("{unit_name} did not connect"));
                }
                Err(e) => return Err(e.to_string()),
            }
        };
        let _ = fs::remove_file(&socket);
        stream.set_nonblocking(false).map_err(|e| e.to_string())?;
        // systemd opens the socket for the unit (as root) before it becomes the person.
        let peer = rustix::net::sockopt::socket_peercred(&stream).map_err(|e| e.to_string())?;
        let peer = peer.uid.as_raw();
        if peer != 0 && peer != uid {
            return Err(format!("{unit_name}: connected from uid {peer}"));
        }
        McpStdio::over_socket(name, stream, mcp::CALL_TIMEOUT)
    }
}
