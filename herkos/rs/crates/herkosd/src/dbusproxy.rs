// SPDX-License-Identifier: GPL-3.0-or-later
//! `herkosd dbus-proxy TOOL DIR` (ADR 0031): in the unit
//! `vilna-dbus-proxy-<name>@<uid>`, as the person, become `xdg-dbus-proxy`
//! that lets the tool make only the D-Bus calls its manifest names.
//!
//! The tool's own unit cannot reach the person's buses (it does not see
//! `/run/user/<uid>/bus`, and `PrivateNetwork=yes` hides abstract sockets);
//! it talks to `DIR/session.bus` and `DIR/system.bus` instead. The rules come
//! from the manifest built into this program, never from a file on disk, so
//! nothing the person's processes can write changes them.

use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::Command;

use herkos_core::manifest::{Manifest, Registry};

/// Where the system bus is when the unit does not say otherwise.
const SYSTEM_BUS: &str = "unix:path=/run/dbus/system_bus_socket";

/// Only plain `unix:path=/…` addresses: no abstract sockets, no TCP, no
/// commas or options that would make the proxy connect anywhere else.
fn plain_unix_address(address: &str) -> bool {
    address.strip_prefix("unix:path=/").is_some_and(|rest| {
        !rest.is_empty()
            && rest
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '/' | '_' | '-' | '.'))
            && !rest.split('/').any(|p| p == "..")
    })
}

/// The arguments for `xdg-dbus-proxy`: one bus after another, each
/// `ADDRESS SOCKET --filter --call=…`. A bus no operation uses is left out.
pub fn proxy_args(
    manifest: &Manifest,
    session: Option<&str>,
    system: &str,
    dir: &Path,
) -> Result<Vec<String>, String> {
    let calls = manifest.dbus_calls();
    if calls.is_empty() {
        return Err(format!(
            "{}: the manifest names no D-Bus calls",
            manifest.tool
        ));
    }
    let mut args = Vec::new();
    for bus in herkos_core::manifest::DBUS_BUSES {
        let rules: Vec<String> = calls
            .iter()
            .filter(|c| c.bus == bus)
            .map(|c| c.proxy_rule())
            .collect();
        if rules.is_empty() {
            continue;
        }
        let address = match bus {
            "session" => session.ok_or("DBUS_SESSION_BUS_ADDRESS is not set")?,
            _ => system,
        };
        if !plain_unix_address(address) {
            return Err(format!(
                "{bus} bus: {address:?} is not a unix:path= address"
            ));
        }
        args.push(address.to_owned());
        args.push(dir.join(format!("{bus}.bus")).display().to_string());
        args.push("--filter".to_owned());
        args.extend(rules);
    }
    Ok(args)
}

pub fn main(args: &[String]) -> Result<(), String> {
    let (tool, dir) = match args {
        [tool, dir] if Path::new(dir).is_absolute() => (tool, Path::new(dir)),
        _ => return Err("usage: herkosd dbus-proxy TOOL /ABSOLUTE/DIR".into()),
    };
    let registry = Registry::builtin().map_err(|e| e.0)?;
    let manifest = registry
        .get(tool)
        .ok_or_else(|| format!("{tool}: not a tool built into Herkos"))?;
    let session = std::env::var("DBUS_SESSION_BUS_ADDRESS").ok();
    let system = std::env::var("DBUS_SYSTEM_BUS_ADDRESS").unwrap_or_else(|_| SYSTEM_BUS.into());
    let proxy_args = proxy_args(manifest, session.as_deref(), &system, dir)?;
    // Only returns if it could not start.
    let err = Command::new("xdg-dbus-proxy")
        .args(&proxy_args)
        .env_remove("LD_PRELOAD")
        .exec();
    Err(format!("xdg-dbus-proxy: {err}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn desktop() -> Manifest {
        Registry::builtin()
            .expect("manifests")
            .get("org.vilna.mcp.desktop")
            .expect("desktop")
            .clone()
    }

    #[test]
    fn the_proxy_gets_exactly_the_calls_of_the_manifest() {
        let dir = Path::new("/run/herkos-bus/1000/desktop");
        let args = proxy_args(
            &desktop(),
            Some("unix:path=/run/user/1000/bus"),
            SYSTEM_BUS,
            dir,
        )
        .expect("args");
        assert_eq!(
            args[..3],
            [
                "unix:path=/run/user/1000/bus",
                "/run/herkos-bus/1000/desktop/session.bus",
                "--filter",
            ]
        );
        assert!(args.contains(
            &"--call=org.freedesktop.Notifications=org.freedesktop.Notifications.Notify@/org/freedesktop/Notifications".to_owned()
        ));
        // The system bus follows, with its own socket and filter.
        let at = args
            .iter()
            .position(|a| a == SYSTEM_BUS)
            .expect("system bus");
        assert_eq!(args[at + 1], "/run/herkos-bus/1000/desktop/system.bus");
        assert_eq!(args[at + 2], "--filter");
        // Nothing but addresses, sockets, --filter and --call rules.
        assert!(args.iter().all(|a| a.starts_with("--call=")
            || a == "--filter"
            || a.starts_with("unix:path=/")
            || a.starts_with("/run/herkos-bus/")));
        // Each call of the manifest is there once, none other.
        let rules = args.iter().filter(|a| a.starts_with("--call=")).count();
        assert_eq!(rules, desktop().dbus_calls().len());
    }

    #[test]
    fn only_plain_unix_sockets() {
        let m = desktop();
        let dir = Path::new("/d");
        for bad in [
            "unix:abstract=/tmp/x",
            "tcp:host=example.org,port=1",
            "unix:path=/run/user/1000/bus,guid=1",
            "unix:path=/run/../etc/x",
            "unix:path=",
        ] {
            assert!(proxy_args(&m, Some(bad), SYSTEM_BUS, dir).is_err(), "{bad}");
            assert!(
                proxy_args(&m, Some("unix:path=/run/user/1/bus"), bad, dir).is_err(),
                "{bad}"
            );
        }
        assert!(proxy_args(&m, None, SYSTEM_BUS, dir).is_err());
    }

    #[test]
    fn a_tool_without_dbus_gets_no_proxy() {
        let files = Registry::builtin()
            .expect("manifests")
            .get("org.vilna.mcp.files")
            .expect("files")
            .clone();
        assert!(proxy_args(&files, Some("unix:path=/b"), SYSTEM_BUS, Path::new("/d")).is_err());
        assert!(main(&["org.example.unknown".into(), "/d".into()]).is_err());
        assert!(main(&["org.vilna.mcp.desktop".into(), "relative".into()]).is_err());
    }
}
