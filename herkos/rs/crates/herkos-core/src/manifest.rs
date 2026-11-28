// SPDX-License-Identifier: GPL-3.0-or-later
//! Tool manifests: what each tool operation is allowed to be. Mirrors
//! `manifest.py`; the format is in herkos/docs/protocol.md.
//!
//! Anything a manifest does not name is denied (permissions.md, principle 8).

use std::collections::BTreeMap;
use std::fmt;

use toml::{Table, Value};

use crate::request::is_name;
use crate::risk::Level;

pub const FORBIDDEN: &str = "forbidden";
const OPERATION_KEYS: [&str; 12] = [
    "level",
    "summary",
    "undoable",
    "reversible",
    "returns_external",
    "egress",
    "resource_args",
    "command_arg",
    "command_key",
    "preview",
    "polkit",
    "dbus",
];
/// Every polkit action an operation may name starts with this: Herkos's own
/// actions in `org.vilna.herkos.policy` (permissions.md 9.2).
pub const POLKIT_PREFIX: &str = "org.vilna.herkos.";
/// A group of the program's commands in `[commands]`: `home.*`.
const GROUP_SUFFIX: &str = ".*";
pub const MAX_COMMANDS: usize = 4096;
/// At most this many commands in one call (a batch); more is not read.
pub const MAX_CALL_COMMANDS: usize = 64;

/// The buses an operation's D-Bus calls go to (ADR 0031).
pub const DBUS_BUSES: [&str; 2] = ["session", "system"];
const DBUS_KEYS: [&str; 4] = ["bus", "name", "path", "call"];
pub const MAX_DBUS_PER_OPERATION: usize = 16;
pub const MAX_DBUS_PER_MANIFEST: usize = 64;

/// What Herkos may pass a tool when it starts it, in this order:
/// `backup_dir` -> `--backup-dir DIR`, `roots` -> `--root DIR` per allowed folder.
pub const LAUNCH_ARGS: [&str; 2] = ["backup_dir", "roots"];
const LAUNCH_KEYS: [&str; 7] = [
    "program",
    "module",
    "args",
    "flatpak",
    "command",
    "fixed",
    "root_flags",
];
/// The unit of a program from Flatpak: `vilna-mcp-ext-<name>@<uid>` (ADR 0017, step 2).
pub const EXT_PREFIX: &str = "vilna-mcp-ext-";
const MAX_FIXED: usize = 16;
const MAX_ROOT_FLAGS: usize = 4;

/// The manifests shipped with Herkos: the same files Herkos-lite reads.
const BUILTIN: [(&str, &str); 13] = [
    (
        "org.vilna.gloxi.network.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.gloxi.network.toml"),
    ),
    (
        "org.vilna.mcp.apps.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.mcp.apps.toml"),
    ),
    (
        "org.vilna.mcp.atspi.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.mcp.atspi.toml"),
    ),
    (
        "org.vilna.mcp.backup.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.mcp.backup.toml"),
    ),
    (
        "org.vilna.mcp.calendar.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.mcp.calendar.toml"),
    ),
    (
        "org.vilna.mcp.desktop.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.mcp.desktop.toml"),
    ),
    (
        "org.vilna.mcp.files.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.mcp.files.toml"),
    ),
    (
        "org.vilna.mcp.launcher.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.mcp.launcher.toml"),
    ),
    (
        "org.vilna.mcp.mail.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.mcp.mail.toml"),
    ),
    (
        "org.vilna.mcp.portals.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.mcp.portals.toml"),
    ),
    (
        "org.vilna.mcp.settings.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.mcp.settings.toml"),
    ),
    (
        "org.vilna.mcp.system.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.mcp.system.toml"),
    ),
    (
        "org.vilna.mcp.vpn.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.mcp.vpn.toml"),
    ),
];

/// A manifest is malformed. The tool stays unknown (and so denied).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManifestError(pub String);

impl fmt::Display for ManifestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ManifestError {}

/// One D-Bus method an operation calls (ADR 0031, protocol.md 5).
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct DbusCall {
    /// `session` or `system`.
    pub bus: String,
    /// A well-known name, or a family `org.mpris.MediaPlayer2.*`.
    pub name: String,
    /// An object path, or a subtree `/org/foo/*`.
    pub path: String,
    /// `interface.Method`.
    pub call: String,
}

impl DbusCall {
    /// The rule as xdg-dbus-proxy takes it: `--call=NAME=INTERFACE.METHOD@PATH`.
    pub fn proxy_rule(&self) -> String {
        format!("--call={}={}@{}", self.name, self.call, self.path)
    }
}

/// One element of a bus name: `[A-Za-z_-][A-Za-z0-9_-]*`.
fn is_dbus_element(s: &str, dash: bool) -> bool {
    let ok = |c: char| c.is_ascii_alphanumeric() || c == '_' || (dash && c == '-');
    let mut chars = s.chars();
    chars
        .next()
        .is_some_and(|c| c.is_ascii_alphabetic() || c == '_' || (dash && c == '-'))
        && chars.all(ok)
}

/// A well-known bus name (not a unique `:1.42`, not the bus itself), with
/// an optional `.*` for a family. As `_DBUS_NAME` in manifest.py.
pub fn is_dbus_name(s: &str) -> bool {
    let base = s.strip_suffix(".*").unwrap_or(s);
    let parts: Vec<&str> = base.split('.').collect();
    s.len() <= 255
        && parts.len() >= 2
        && parts.iter().all(|p| is_dbus_element(p, true))
        && base != "org.freedesktop.DBus"
        // A family that holds the bus itself ("org.freedesktop.*").
        && !(s.ends_with(".*") && "org.freedesktop.DBus".starts_with(&s[..s.len() - 1]))
}

/// `/`, `/a/b_c`, `/a/*` or `/*`. As `_DBUS_PATH` in manifest.py.
pub fn is_dbus_path(s: &str) -> bool {
    if s == "/" || s == "/*" {
        return true;
    }
    let base = s.strip_suffix("/*").unwrap_or(s);
    s.len() <= 255
        && base.starts_with('/')
        && base
            .split('/')
            .skip(1)
            .all(|p| !p.is_empty() && p.chars().all(|c| c.is_ascii_alphanumeric() || c == '_'))
}

/// `interface.Method`, three elements or more, no wildcards.
pub fn is_dbus_call(s: &str) -> bool {
    let parts: Vec<&str> = s.split('.').collect();
    s.len() <= 255 && parts.len() >= 3 && parts.iter().all(|p| is_dbus_element(p, false))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Operation {
    pub name: String,
    /// `None`: forbidden even with confirmation.
    pub level: Option<Level>,
    pub summary: String,
    /// The tool takes a backup before the change, so Mitos can undo it.
    pub undoable: bool,
    /// The change loses nothing, so it needs no backup to be undone.
    pub reversible: bool,
    /// The result carries content Herkos did not write: it taints the session.
    pub returns_external: bool,
    /// Data leaves the device.
    pub egress: bool,
    /// Arguments that name files; each must be a declared resource.
    pub resource_args: Vec<String>,
    /// The argument that names the program's command(s) this call runs: a
    /// string, or a list of strings or of tables whose `command_key` names it.
    pub command_arg: Option<String>,
    pub command_key: Option<String>,
    /// An R0 operation of the same tool that Herkos runs with the same
    /// arguments before it asks, to show the person what the action is about
    /// (ADR 0012, section 5: the app, its developer, size and permissions).
    pub preview: Option<String>,
    /// The polkit action (`org.vilna.herkos.*`) the person must also be
    /// authorized for after they said yes in Herkos's window: what changes
    /// the whole computer, not only the person's own things (permissions.md
    /// 9.2). Only for R3-R4 operations; only the system Herkos can check it.
    pub polkit: Option<String>,
    /// The D-Bus methods it calls (ADR 0031): its level is their level, and
    /// the system Herkos lets the tool call nothing else on the buses.
    pub dbus: Vec<DbusCall>,
}

/// `org.vilna.herkos.` and then lowercase words joined by `.` or `-`.
pub fn is_polkit_action(id: &str) -> bool {
    let Some(rest) = id.strip_prefix(POLKIT_PREFIX) else {
        return false;
    };
    id.len() <= 128
        && !rest.is_empty()
        && rest.split('.').all(|w| {
            !w.is_empty()
                && !w.starts_with('-')
                && !w.ends_with('-')
                && w.bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        })
}

impl Operation {
    pub fn forbidden(&self) -> bool {
        self.level.is_none()
    }

    pub fn changes_state(&self) -> bool {
        self.level.is_some_and(|l| l >= Level::R2)
    }
}

/// How Herkos starts the tool (ADR 0017): from the manifest, not from code.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Launch {
    /// On PATH; the system Herkos starts the unit `<program>@<uid>`.
    pub program: String,
    /// The Python module Herkos-lite runs; herkosd does not use it.
    pub module: Option<String>,
    /// Names from `LAUNCH_ARGS`, in that order.
    pub args: Vec<String>,
    /// A program from Flatpak, run in its own sandbox (catalogue only).
    pub flatpak: Option<Flatpak>,
}

/// `flatpak run --command=<command> <app> <fixed...> <root_flags DIR...>`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Flatpak {
    pub app: String,
    pub command: String,
    /// Arguments that never change, such as `mcp`.
    pub fixed: Vec<String>,
    /// How the program is told each allowed folder; each flag gets each folder.
    pub root_flags: Vec<String>,
}

impl Launch {
    pub fn wants(&self, arg: &str) -> bool {
        self.args.iter().any(|a| a == arg)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Manifest {
    pub tool: String,
    pub description: String,
    pub operations: BTreeMap<String, Operation>,
    pub launch: Option<Launch>,
    /// Level of each of the program's commands, by id or group (`home.*`);
    /// `None`: forbidden. A command it does not name is R4 (decision of 2026-11-05).
    pub commands: BTreeMap<String, Option<Level>>,
}

impl Manifest {
    pub fn operation(&self, name: &str) -> Option<&Operation> {
        self.operations.get(name)
    }

    /// Every D-Bus call of its operations, once each, in a stable order.
    pub fn dbus_calls(&self) -> Vec<DbusCall> {
        let calls: std::collections::BTreeSet<&DbusCall> =
            self.operations.values().flat_map(|op| &op.dbus).collect();
        calls.into_iter().cloned().collect()
    }

    /// `(known, level)` of one command: the id itself, else its nearest group.
    pub fn command_level(&self, command: &str) -> (bool, Option<Level>) {
        if let Some(level) = self.commands.get(command) {
            return (true, *level);
        }
        let parts: Vec<&str> = command.split('.').take(MAX_COMMAND_PARTS + 1).collect();
        // A group has at most 8 parts before its ".*": longer prefixes cannot
        // match, and trying each of them took seconds on a long run of parts.
        for n in (1..parts.len()).rev() {
            let group = format!(
                "{}{GROUP_SUFFIX}",
                parts.get(..n).unwrap_or_default().join(".")
            );
            if let Some(level) = self.commands.get(&group) {
                return (true, *level);
            }
        }
        (false, None)
    }
}

const MAX_COMMAND_PARTS: usize = 8;
pub const MAX_SUMMARY: usize = 200;
pub const MAX_DESCRIPTION: usize = 1000;

/// Shown to the person as written: no control, format or line-separator
/// characters. As `_check_shown` in manifest.py.
fn check_shown(text: &str, limit: usize, what: &str) -> Result<(), ManifestError> {
    if text.chars().count() > limit {
        return bad(format!("{what} is longer than {limit} characters"));
    }
    let unshown = |c: char| {
        matches!(c, '\u{0}'..='\u{1f}' | '\u{7f}'..='\u{9f}' | '\u{ad}' | '\u{61c}' | '\u{180e}'
            | '\u{200b}'..='\u{200f}' | '\u{2028}'..='\u{202e}' | '\u{2060}'..='\u{2069}' | '\u{feff}')
    };
    if text.chars().any(unshown) {
        return bad(format!("{what} holds a control or invisible character"));
    }
    Ok(())
}

/// A command id a call may name (no groups: those are only for the manifest):
/// 1 to 8 parts of `[A-Za-z0-9_-]{1,64}` joined by dots.
pub fn is_command(s: &str) -> bool {
    let parts: Vec<&str> = s.split('.').collect();
    parts.len() <= 8
        && parts.iter().all(|p| {
            !p.is_empty()
                && p.len() <= 64
                && p.bytes()
                    .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
        })
}

fn parse_commands(
    value: &Value,
    source: &str,
) -> Result<BTreeMap<String, Option<Level>>, ManifestError> {
    let at = format!("{source}: commands");
    let table = match value {
        Value::Table(t) if !t.is_empty() => t,
        _ => return bad(format!("{at}: must be a table of command ids or groups")),
    };
    if table.len() > MAX_COMMANDS {
        return bad(format!("{at}: more than {MAX_COMMANDS} entries"));
    }
    let mut commands = BTreeMap::new();
    for (key, raw) in table {
        let name = key.strip_suffix(GROUP_SUFFIX).unwrap_or(key);
        if !is_command(name) {
            return bad(format!("{at}: bad command id or group {key:?}"));
        }
        let level = match raw.as_str() {
            Some(FORBIDDEN) => None,
            Some(l) => match Level::parse(l) {
                Some(l) => Some(l),
                None => return bad(format!("{at}.{key}: unknown risk level: {l:?}")),
            },
            None => return bad(format!("{at}.{key}: level must be R0-R4 or 'forbidden'")),
        };
        commands.insert(key.clone(), level);
    }
    Ok(commands)
}

fn bad<T>(msg: String) -> Result<T, ManifestError> {
    Err(ManifestError(msg))
}

fn unknown_keys(table: &Table, known: &[&str]) -> Vec<String> {
    table
        .keys()
        .filter(|k| !known.contains(&k.as_str()))
        .cloned()
        .collect()
}

pub fn parse_manifest_str(text: &str, source: &str) -> Result<Manifest, ManifestError> {
    let table: Table = text
        .parse()
        .map_err(|e| ManifestError(format!("{source}: {e}")))?;
    parse_manifest(&table, source)
}

pub fn parse_manifest(data: &Table, source: &str) -> Result<Manifest, ManifestError> {
    let tool = match data.get("tool").and_then(Value::as_str) {
        Some(t) if is_name(t) => t.to_owned(),
        _ => return bad(format!("{source}: 'tool' must be a reverse-DNS name")),
    };
    let unknown = unknown_keys(
        data,
        &["tool", "description", "operations", "launch", "commands"],
    );
    if !unknown.is_empty() {
        return bad(format!("{source}: unknown keys {unknown:?}"));
    }
    let description = match data.get("description") {
        None => String::new(),
        Some(Value::String(d)) => d.clone(),
        Some(_) => return bad(format!("{source}: 'description' must be a string")),
    };
    check_shown(
        &description,
        MAX_DESCRIPTION,
        &format!("{source}: 'description'"),
    )?;
    let ops = match data.get("operations") {
        Some(Value::Table(t)) if !t.is_empty() => t,
        _ => {
            return bad(format!(
                "{source}: at least one [operations.<name>] is required"
            ));
        }
    };

    let mut operations = BTreeMap::new();
    for (name, op) in ops {
        let at = format!("{source}: operations.{name}");
        if !is_name(name) {
            return bad(format!("{at}: bad operation name"));
        }
        let Value::Table(op) = op else {
            return bad(format!("{at}: must be a table"));
        };
        let unknown = unknown_keys(op, &OPERATION_KEYS);
        if !unknown.is_empty() {
            return bad(format!("{at}: unknown keys {unknown:?}"));
        }
        let level = match op.get("level").and_then(Value::as_str) {
            Some(FORBIDDEN) => None,
            Some(l) => match Level::parse(l) {
                Some(l) => Some(l),
                None => return bad(format!("{at}: unknown risk level: {l:?}")),
            },
            None => return bad(format!("{at}: 'level' is required (R0-R4 or 'forbidden')")),
        };
        let flag = |key: &str| match op.get(key) {
            None => Ok(false),
            Some(Value::Boolean(b)) => Ok(*b),
            Some(_) => bad(format!("{at}: '{key}' must be true or false")),
        };
        let resource_args = match op.get("resource_args") {
            None => Vec::new(),
            Some(Value::Array(items)) => {
                let names: Option<Vec<String>> = items
                    .iter()
                    .map(|a| a.as_str().filter(|s| !s.is_empty()).map(str::to_owned))
                    .collect();
                match names {
                    Some(n) => n,
                    None => {
                        return bad(format!(
                            "{at}: 'resource_args' must be a list of argument names"
                        ));
                    }
                }
            }
            Some(_) => {
                return bad(format!(
                    "{at}: 'resource_args' must be a list of argument names"
                ));
            }
        };
        let summary = match op.get("summary") {
            None => name.clone(),
            Some(Value::String(s)) => s.clone(),
            Some(_) => return bad(format!("{at}: 'summary' must be a string")),
        };
        check_shown(&summary, MAX_SUMMARY, &format!("{at}: 'summary'"))?;
        let arg_name = |key: &str| match op.get(key) {
            None => Ok(None),
            Some(Value::String(s)) if is_name(s) => Ok(Some(s.clone())),
            Some(_) => bad(format!("{at}: '{key}' must be an argument name")),
        };
        let command_arg = arg_name("command_arg")?;
        let command_key = arg_name("command_key")?;
        let preview = arg_name("preview")?;
        let polkit = match op.get("polkit") {
            None => None,
            Some(Value::String(s)) if is_polkit_action(s) => Some(s.clone()),
            Some(_) => {
                return bad(format!(
                    "{at}: 'polkit' must name an action {POLKIT_PREFIX}*"
                ));
            }
        };
        if polkit.is_some() && !level.is_some_and(|l| l >= Level::R3) {
            return bad(format!("{at}: 'polkit' goes only with R3 or R4"));
        }
        if command_key.is_some() && command_arg.is_none() {
            return bad(format!("{at}: 'command_key' goes only with 'command_arg'"));
        }
        if command_arg.is_some() && !data.contains_key("commands") {
            return bad(format!("{at}: 'command_arg' needs a [commands] table"));
        }
        let dbus = match op.get("dbus") {
            None => Vec::new(),
            Some(v) => parse_dbus(v, &at)?,
        };
        if !dbus.is_empty() && level.is_none() {
            return bad(format!("{at}: a forbidden operation makes no D-Bus calls"));
        }
        operations.insert(
            name.clone(),
            Operation {
                name: name.clone(),
                level,
                summary,
                undoable: flag("undoable")?,
                reversible: flag("reversible")?,
                returns_external: flag("returns_external")?,
                egress: flag("egress")?,
                resource_args,
                command_arg,
                command_key,
                preview,
                polkit,
                dbus,
            },
        );
    }
    for op in operations.values() {
        if let Some(p) = &op.preview {
            let at = format!("{source}: operations.{}", op.name);
            match operations.get(p).and_then(|o| o.level) {
                Some(Level::R0) => {}
                _ => {
                    return bad(format!(
                        "{at}: 'preview' must name an R0 operation of this tool"
                    ));
                }
            }
        }
    }
    let launch = match data.get("launch") {
        None => None,
        Some(l) => Some(parse_launch(l, source)?),
    };
    let commands = match data.get("commands") {
        None => BTreeMap::new(),
        Some(c) => parse_commands(c, source)?,
    };
    let calls: usize = operations.values().map(|op| op.dbus.len()).sum();
    if calls > MAX_DBUS_PER_MANIFEST {
        return bad(format!(
            "{source}: more than {MAX_DBUS_PER_MANIFEST} D-Bus calls"
        ));
    }
    if calls > 0 && launch.as_ref().is_some_and(|l| l.flatpak.is_some()) {
        return bad(format!(
            "{source}: a Flatpak program has no 'dbus': Flatpak filters its buses itself"
        ));
    }
    Ok(Manifest {
        tool,
        description,
        operations,
        launch,
        commands,
    })
}

fn parse_dbus(value: &Value, at: &str) -> Result<Vec<DbusCall>, ManifestError> {
    let items = match value {
        Value::Array(items) if (1..=MAX_DBUS_PER_OPERATION).contains(&items.len()) => items,
        _ => {
            return bad(format!(
                "{at}: 'dbus' must be a list of 1 to {MAX_DBUS_PER_OPERATION} calls"
            ));
        }
    };
    let mut calls = Vec::new();
    for (i, item) in items.iter().enumerate() {
        let at = format!("{at}: dbus[{i}]");
        let Value::Table(t) = item else {
            return bad(format!(
                "{at}: must be a table with bus, name, path and call"
            ));
        };
        let unknown = unknown_keys(t, &DBUS_KEYS);
        if !unknown.is_empty() {
            return bad(format!("{at}: unknown keys {unknown:?}"));
        }
        let field = |k: &str| t.get(k).and_then(Value::as_str).unwrap_or_default();
        let (bus, name, path, call) = (field("bus"), field("name"), field("path"), field("call"));
        if !DBUS_BUSES.contains(&bus) {
            return bad(format!("{at}: 'bus' must be one of {DBUS_BUSES:?}"));
        }
        if !is_dbus_name(name) {
            return bad(format!("{at}: 'name' must be a well-known bus name"));
        }
        if !is_dbus_path(path) {
            return bad(format!("{at}: 'path' must be an object path"));
        }
        if !is_dbus_call(call) {
            return bad(format!("{at}: 'call' must be interface.Method"));
        }
        calls.push(DbusCall {
            bus: bus.to_owned(),
            name: name.to_owned(),
            path: path.to_owned(),
            call: call.to_owned(),
        });
    }
    Ok(calls)
}

/// A program on PATH (and its systemd unit): no paths, no spaces.
fn is_program(s: &str) -> bool {
    let mut chars = s.chars();
    s.len() <= 64
        && chars
            .next()
            .is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
}

/// A Flatpak application id, as `_APP_ID` in manifest.py.
fn is_app_id(s: &str) -> bool {
    let parts: Vec<&str> = s.split('.').collect();
    s.len() <= 255
        && parts.len() >= 3
        && parts.iter().enumerate().all(|(i, p)| {
            let mut chars = p.chars();
            chars.next().is_some_and(|c| {
                c.is_ascii_alphabetic() || c == '_' || (i > 0 && c.is_ascii_digit())
            }) && chars.all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
        })
}

/// A fixed argument, as `_FIXED` in manifest.py: `mcp`, `--quiet`, `-v`.
fn is_fixed(s: &str) -> bool {
    let word = s
        .strip_prefix("--")
        .or_else(|| s.strip_prefix('-'))
        .unwrap_or(s);
    word.len() <= 64
        && word
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        && word.chars().all(|c| {
            c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' || c == '_' || c == '.'
        })
}

/// A long option naming a folder, as `_ROOT_FLAG` in manifest.py: `--root`.
fn is_root_flag(s: &str) -> bool {
    s.strip_prefix("--").is_some_and(|w| {
        w.len() <= 64
            && w.chars()
                .next()
                .is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
            && w.chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
    })
}

/// A list of strings that all pass `ok`, at most `max` long.
fn words(value: Option<&Value>, max: usize, ok: fn(&str) -> bool) -> Option<Vec<String>> {
    match value {
        None => Some(Vec::new()),
        Some(Value::Array(items)) if items.len() <= max => items
            .iter()
            .map(|a| a.as_str().filter(|s| ok(s)).map(str::to_owned))
            .collect(),
        Some(_) => None,
    }
}

fn parse_flatpak(t: &Table, program: &str, at: &str) -> Result<Option<Flatpak>, ManifestError> {
    let Some(app) = t.get("flatpak") else {
        if ["command", "fixed", "root_flags"]
            .iter()
            .any(|k| t.contains_key(*k))
        {
            return bad(format!(
                "{at}: 'command', 'fixed' and 'root_flags' go only with 'flatpak'"
            ));
        }
        if program.starts_with(EXT_PREFIX) {
            // The name of a Flatpak program's unit: taken, it hid the real one.
            return bad(format!(
                "{at}: a program named {EXT_PREFIX}… needs 'flatpak'"
            ));
        }
        return Ok(None);
    };
    let Some(app) = app.as_str().filter(|a| is_app_id(a)) else {
        return bad(format!("{at}: 'flatpak' must be a Flatpak application id"));
    };
    if !program.starts_with(EXT_PREFIX) || program.len() == EXT_PREFIX.len() {
        return bad(format!(
            "{at}: a Flatpak program needs 'program' = \"{EXT_PREFIX}<name>\""
        ));
    }
    if t.contains_key("module") {
        return bad(format!("{at}: a Flatpak program has no 'module'"));
    }
    let Some(command) = t
        .get("command")
        .and_then(Value::as_str)
        .filter(|c| is_program(c))
    else {
        return bad(format!(
            "{at}: 'command' must be a program name, not a path"
        ));
    };
    let Some(fixed) = words(t.get("fixed"), MAX_FIXED, is_fixed) else {
        return bad(format!(
            "{at}: 'fixed' must be at most {MAX_FIXED} plain words or options"
        ));
    };
    let root_flags = match words(t.get("root_flags"), MAX_ROOT_FLAGS, is_root_flag) {
        Some(f) if f.is_empty() && !t.contains_key("root_flags") => vec!["--root".to_owned()],
        Some(f) if !f.is_empty() => f,
        _ => {
            return bad(format!(
                "{at}: 'root_flags' must be 1 to {MAX_ROOT_FLAGS} long options"
            ));
        }
    };
    Ok(Some(Flatpak {
        app: app.to_owned(),
        command: command.to_owned(),
        fixed,
        root_flags,
    }))
}

/// A dotted Python module name, as `_MODULE` in manifest.py.
fn is_module(s: &str) -> bool {
    let parts: Vec<&str> = s.split('.').collect();
    parts.len() <= 4
        && parts.iter().all(|p| {
            let mut chars = p.chars();
            p.len() <= 64
                && chars
                    .next()
                    .is_some_and(|c| c.is_ascii_lowercase() || c == '_')
                && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
        })
}

fn parse_launch(value: &Value, source: &str) -> Result<Launch, ManifestError> {
    let at = format!("{source}: launch");
    let Value::Table(t) = value else {
        return bad(format!("{at}: must be a table"));
    };
    let unknown = unknown_keys(t, &LAUNCH_KEYS);
    if !unknown.is_empty() {
        return bad(format!("{at}: unknown keys {unknown:?}"));
    }
    let program = match t.get("program").and_then(Value::as_str) {
        Some(p) if is_program(p) => p.to_owned(),
        _ => {
            return bad(format!(
                "{at}: 'program' must be a program name, not a path"
            ));
        }
    };
    let module = match t.get("module") {
        None => None,
        Some(Value::String(m)) if is_module(m) => Some(m.clone()),
        Some(_) => return bad(format!("{at}: 'module' must be a Python module name")),
    };
    let wrong = || bad(format!("{at}: 'args' must be a list of {LAUNCH_ARGS:?}"));
    let given: Vec<&str> = match t.get("args") {
        None => Vec::new(),
        Some(Value::Array(items)) => {
            let names: Option<Vec<&str>> = items
                .iter()
                .map(|a| a.as_str().filter(|s| LAUNCH_ARGS.contains(s)))
                .collect();
            match names {
                Some(n) => n,
                None => return wrong(),
            }
        }
        Some(_) => return wrong(),
    };
    let args: Vec<String> = LAUNCH_ARGS
        .iter()
        .filter(|a| given.contains(a))
        .map(|a| (*a).to_owned())
        .collect();
    if args.len() != given.len() {
        return wrong(); // a name given twice
    }
    let flatpak = parse_flatpak(t, &program, &at)?;
    if flatpak.is_some() && args.iter().any(|a| a == "backup_dir") {
        return bad(format!("{at}: a Flatpak program gets no 'backup_dir'"));
    }
    Ok(Launch {
        program,
        module,
        args,
        flatpak,
    })
}

/// The manifests Herkos knows. A tool not in here is unknown.
#[derive(Debug, Default, Clone)]
pub struct Registry {
    by_tool: BTreeMap<String, Manifest>,
    builtin: std::collections::BTreeSet<String>,
}

impl Registry {
    pub fn add(&mut self, manifest: Manifest) -> Result<(), ManifestError> {
        if self.by_tool.contains_key(&manifest.tool) {
            return bad(format!("duplicate manifest for {}", manifest.tool));
        }
        if let Some(program) = manifest.launch.as_ref().map(|l| &l.program) {
            let taken = self
                .by_tool
                .values()
                .any(|m| m.launch.as_ref().is_some_and(|l| &l.program == program));
            if taken {
                // Another tool's program would run under this manifest's levels.
                return bad(format!(
                    "{}: the program {program} belongs to another tool",
                    manifest.tool
                ));
            }
        }
        self.by_tool.insert(manifest.tool.clone(), manifest);
        Ok(())
    }

    pub fn get(&self, tool: &str) -> Option<&Manifest> {
        self.by_tool.get(tool)
    }

    /// The manifests, sorted by tool name.
    pub fn iter(&self) -> impl Iterator<Item = &Manifest> {
        self.by_tool.values()
    }

    /// Manifests shipped with Herkos (`herkos/src/herkos/manifests/*.toml`).
    pub fn builtin() -> Result<Registry, ManifestError> {
        let mut r = Registry::default();
        for (name, text) in BUILTIN {
            r.add(parse_manifest_str(text, name)?)?;
        }
        r.builtin = r.by_tool.keys().cloned().collect();
        Ok(r)
    }

    /// Shipped with Herkos itself, not from the catalogue (no pins, ADR 0017).
    pub fn is_builtin(&self, tool: &str) -> bool {
        self.builtin.contains(tool)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_manifest_cannot_take_another_program_or_redraw_the_question() {
        let mut r = Registry::builtin().expect("builtin");
        let helper = parse_manifest_str(
            "tool = \"org.example.helper\"\n[launch]\nprogram = \"vilna-mcp-files\"\n\
             [operations.delete]\nlevel = \"R0\"\n",
            "t",
        )
        .expect("parses");
        assert!(r.add(helper).is_err(), "the program of a built-in tool");
        for text in [
            "tool = \"org.example.0\"\n[launch]\nprogram = \"vilna-mcp-ext-pdf\"\n[operations.a]\nlevel = \"R1\"\n",
            "tool = \"t.x\"\ndescription = \"Notes\\u202Etxt\"\n[operations.a]\nlevel = \"R1\"\n",
            "tool = \"t.x\"\n[operations.a]\nlevel = \"R4\"\nsummary = \"Read (R0)\\n\\u001b[8m\"\n",
            "tool = \"t.x\"\n[operations.a]\nlevel = \"R2\"\n[[operations.a.dbus]]\nbus = \"session\"\n\
             name = \"org.freedesktop.*\"\npath = \"/\"\ncall = \"org.freedesktop.systemd1.Manager.StartUnit\"\n",
        ] {
            assert!(parse_manifest_str(text, "t").is_err(), "{text}");
        }
        let long = format!(
            "tool = \"t.x\"\n[operations.a]\nlevel = \"R1\"\nsummary = \"{}\"\n",
            "x".repeat(201)
        );
        assert!(parse_manifest_str(&long, "t").is_err());
        let m = parse_manifest_str(
            "tool = \"t.x\"\n[operations.a]\nlevel = \"R1\"\n[commands]\n\"a.*\" = \"R1\"\n",
            "t",
        )
        .expect("parses");
        let start = std::time::Instant::now();
        let _ = m.command_level(&vec!["a"; 100_000].join("."));
        assert!(start.elapsed() < std::time::Duration::from_secs(5));
    }

    fn with_launch(launch: &str) -> Result<Manifest, ManifestError> {
        parse_manifest_str(
            &format!(
                "tool = \"org.example.t\"\n[launch]\n{launch}\n[operations.read]\nlevel = \"R1\"\n"
            ),
            "t",
        )
    }

    const PDF: &str = "program = \"vilna-mcp-ext-pdf\"\nflatpak = \"io.github.stdenis1980.tintapdf\"\ncommand = \"pdfcraft-cli\"\nfixed = [\"mcp\"]\nargs = [\"roots\"]";

    #[test]
    fn a_flatpak_program_is_started_through_its_own_unit() {
        let launch = with_launch(PDF).expect("valid").launch.expect("launch");
        assert_eq!(
            launch.flatpak,
            Some(Flatpak {
                app: "io.github.stdenis1980.tintapdf".into(),
                command: "pdfcraft-cli".into(),
                fixed: vec!["mcp".into()],
                root_flags: vec!["--root".into()],
            })
        );
        let two = with_launch(&format!(
            "{PDF}\nroot_flags = [\"--automation-read-root\", \"--automation-write-root\"]"
        ))
        .expect("valid");
        assert_eq!(
            two.launch
                .and_then(|l| l.flatpak)
                .map(|f| f.root_flags.len()),
            Some(2)
        );
    }

    #[test]
    fn flatpak_launches_take_only_safe_words() {
        for wrong in [
            PDF.replace("vilna-mcp-ext-pdf", "vilna-mcp-files"),
            PDF.replace("vilna-mcp-ext-pdf", "vilna-mcp-ext-"),
            PDF.replace("io.github.stdenis1980.tintapdf", "tintapdf"),
            PDF.replace("io.github.stdenis1980.tintapdf", "io.github.x/../y"),
            PDF.replace("pdfcraft-cli", "/usr/bin/pdfcraft-cli"),
            PDF.replace("[\"mcp\"]", "[\"mcp --root /\"]"),
            PDF.replace("[\"mcp\"]", "[\"--filesystem=host\"]"),
            PDF.replace("[\"roots\"]", "[\"backup_dir\", \"roots\"]"),
            format!("{PDF}\nmodule = \"x\""),
            format!("{PDF}\nroot_flags = []"),
            format!("{PDF}\nroot_flags = [\"-r\"]"),
            format!("{PDF}\nroot_flags = [\"--root=x\"]"),
            "program = \"vilna-mcp-ext-pdf\"\ncommand = \"pdfcraft-cli\"".to_owned(),
        ] {
            assert!(with_launch(&wrong).is_err(), "{wrong}");
        }
    }

    #[test]
    fn a_preview_names_an_r0_operation_of_the_same_tool() {
        let base = "tool = \"t.x\"\n[operations.look]\nlevel = \"R0\"\n\
                    [operations.put]\nlevel = \"R3\"\n";
        let ok = parse_manifest_str(&format!("{base}preview = \"look\"\n"), "t").expect("ok");
        assert_eq!(ok.operations["put"].preview.as_deref(), Some("look"));
        for wrong in ["preview = \"put\"", "preview = \"gone\"", "preview = 3"] {
            assert!(
                parse_manifest_str(&format!("{base}{wrong}\n"), "t").is_err(),
                "{wrong}"
            );
        }
        let apps = Registry::builtin().expect("manifests");
        let install = apps
            .get("org.vilna.mcp.apps")
            .and_then(|m| m.operation("install"));
        assert_eq!(install.and_then(|o| o.preview.as_deref()), Some("describe"));
        let add = apps
            .get("org.vilna.mcp.apps")
            .and_then(|m| m.operation("add_remote"));
        assert!(add.is_some_and(Operation::forbidden));
    }

    #[test]
    fn a_polkit_action_is_herkos_s_own_and_only_for_r3_and_r4() {
        let base = "tool = \"t.x\"\n[operations.put]\n";
        let ok = parse_manifest_str(
            &format!("{base}level = \"R3\"\npolkit = \"org.vilna.herkos.apps.change\"\n"),
            "t",
        )
        .expect("ok");
        assert_eq!(
            ok.operations["put"].polkit.as_deref(),
            Some("org.vilna.herkos.apps.change")
        );
        for wrong in [
            "level = \"R2\"\npolkit = \"org.vilna.herkos.apps.change\"",
            "level = \"R3\"\npolkit = \"org.freedesktop.systemd1.manage-units\"",
            "level = \"R3\"\npolkit = \"org.vilna.herkos.\"",
            "level = \"R3\"\npolkit = \"org.vilna.herkos.Apps\"",
            "level = \"R3\"\npolkit = \"org.vilna.herkos..x\"",
            "level = \"R4\"\npolkit = 3",
            "level = \"forbidden\"\npolkit = \"org.vilna.herkos.x\"",
        ] {
            assert!(
                parse_manifest_str(&format!("{base}{wrong}\n"), "t").is_err(),
                "{wrong}"
            );
        }
        // What changes the whole computer names one; the person's own things do not.
        let r = Registry::builtin().expect("manifests");
        let action = |tool: &str, op: &str| {
            r.get(tool)
                .and_then(|m| m.operation(op))
                .and_then(|o| o.polkit.clone())
        };
        let apps = "org.vilna.herkos.apps.change";
        for op in ["install", "update", "uninstall", "restore"] {
            assert_eq!(
                action("org.vilna.mcp.apps", op).as_deref(),
                Some(apps),
                "{op}"
            );
        }
        for op in ["rollback", "restore"] {
            assert_eq!(
                action("org.vilna.mcp.system", op).as_deref(),
                Some("org.vilna.herkos.system.rollback"),
                "{op}"
            );
        }
        for op in ["killswitch_off", "allow_lan", "open_portal"] {
            assert_eq!(
                action("org.vilna.mcp.vpn", op).as_deref(),
                Some("org.vilna.herkos.network.weaken"),
                "{op}"
            );
        }
        assert_eq!(action("org.vilna.mcp.vpn", "killswitch_on"), None);
        assert_eq!(action("org.vilna.mcp.files", "delete"), None);
    }

    #[test]
    fn builtin_manifests_say_how_to_start_their_tools() {
        let r = Registry::builtin().expect("manifests");
        let files = r.get("org.vilna.mcp.files").and_then(|m| m.launch.clone());
        assert_eq!(
            files,
            Some(Launch {
                program: "vilna-mcp-files".into(),
                module: Some("vilna_mcp_files".into()),
                args: vec!["backup_dir".into(), "roots".into()],
                flatpak: None,
            })
        );
        let backup = r.get("org.vilna.mcp.backup").and_then(|m| m.launch.clone());
        assert_eq!(
            backup.map(|l| (l.program, l.args.len())),
            Some(("vilna-mcp-backup".into(), 0))
        );
    }

    #[test]
    fn launch_args_come_out_in_one_order() {
        let m = with_launch("program = \"x\"\nargs = [\"roots\", \"backup_dir\"]").expect("ok");
        assert_eq!(
            m.launch.map(|l| l.args),
            Some(vec!["backup_dir".into(), "roots".into()])
        );
        let m = parse_manifest_str(
            "tool = \"org.example.t\"\n[operations.read]\nlevel = \"R1\"\n",
            "t",
        );
        assert_eq!(m.map(|m| m.launch), Ok(None));
    }

    #[test]
    fn bad_launch_is_rejected() {
        for bad in [
            "",
            "program = \"/usr/bin/sh\"",
            "program = \"../sh\"",
            "program = \"sh -c\"",
            "program = \"X\"",
            "program = \"x\"\nmodule = \"os; import evil\"",
            "program = \"x\"\nmodule = 3",
            "program = \"x\"\nargs = [\"--root\", \"/\"]",
            "program = \"x\"\nargs = [\"roots\", \"roots\"]",
            "program = \"x\"\nargs = \"roots\"",
            "program = \"x\"\ncommand = [\"sh\"]",
        ] {
            assert!(with_launch(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn builtin_manifests_load() {
        let r = Registry::builtin().expect("builtin manifests");
        let files = r.get("org.vilna.mcp.files").expect("files manifest");
        let read = files.operation("read").expect("read");
        assert_eq!(read.level, Some(Level::R1));
        assert!(read.returns_external && read.resource_args == ["path"]);
        assert!(
            files
                .operation("delete")
                .is_some_and(|d| d.undoable && d.level == Some(Level::R3))
        );
        let backup = r.get("org.vilna.mcp.backup").expect("backup manifest");
        assert!(
            backup
                .operation("change_destination")
                .is_some_and(|o| o.egress)
        );
    }

    #[test]
    fn bad_manifests() {
        for text in [
            "tool = \"Bad\"\n[operations.a]\nlevel = \"R0\"",
            "tool = \"a\"",
            "tool = \"a\"\nx = 1\n[operations.a]\nlevel = \"R0\"",
            "tool = \"a\"\n[operations.a]\nsummary = \"no level\"",
            "tool = \"a\"\n[operations.a]\nlevel = \"R9\"",
            "tool = \"a\"\n[operations.a]\nlevel = \"R0\"\nundoable = 1",
            "tool = \"a\"\n[operations.a]\nlevel = \"R0\"\nresource_args = [\"\"]",
            "tool = \"a\"\n[operations.a]\nlevel = \"R0\"\nwhat = true",
        ] {
            assert!(parse_manifest_str(text, "t").is_err(), "{text}");
        }
    }

    #[test]
    fn forbidden_operation() {
        let m = parse_manifest_str("tool = \"a\"\n[operations.b]\nlevel = \"forbidden\"", "t")
            .expect("valid");
        let b = m.operation("b").expect("b");
        assert!(b.forbidden() && !b.changes_state() && b.summary == "b");
    }

    #[test]
    fn duplicates_are_refused() {
        let mut r = Registry::builtin().expect("builtin");
        let again = parse_manifest_str(BUILTIN[1].1, "again").expect("valid");
        assert!(r.add(again).is_err());
    }
    fn with_dbus(call: &str) -> String {
        format!("tool = \"a\"\n[operations.b]\nlevel = \"R1\"\ndbus = [{call}]")
    }

    const NOTIFY: &str = "{ bus = \"session\", name = \"org.freedesktop.Notifications\", path = \"/org/freedesktop/Notifications\", call = \"org.freedesktop.Notifications.Notify\" }";

    #[test]
    fn dbus_calls_are_read_and_become_proxy_rules() {
        let m = parse_manifest_str(&with_dbus(NOTIFY), "t").expect("valid");
        let calls = m.dbus_calls();
        assert_eq!(calls.len(), 1);
        assert_eq!(
            calls[0].proxy_rule(),
            "--call=org.freedesktop.Notifications=org.freedesktop.Notifications.Notify@/org/freedesktop/Notifications"
        );
        // Families of names and subtrees of paths.
        let family = "{ bus = \"session\", name = \"org.mpris.MediaPlayer2.*\", path = \"/org/mpris/*\", call = \"org.mpris.MediaPlayer2.Player.Play\" }";
        assert!(parse_manifest_str(&with_dbus(family), "t").is_ok());
    }

    #[test]
    fn bad_dbus_calls_are_refused() {
        let call = |bus: &str, name: &str, path: &str, call: &str| {
            format!(
                "{{ bus = \"{bus}\", name = \"{name}\", path = \"{path}\", call = \"{call}\" }}"
            )
        };
        let ok = (
            "session",
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications.Notify",
        );
        let bad = [
            call("user", ok.1, ok.2, ok.3),
            call(ok.0, ":1.42", ok.2, ok.3),
            call(ok.0, "org.freedesktop.DBus", ok.2, ok.3),
            call(ok.0, "org.*", ok.2, ok.3),
            call(ok.0, "Notifications", ok.2, ok.3),
            call(ok.0, ok.1, "org/freedesktop", ok.3),
            call(ok.0, ok.1, "/org//x", ok.3),
            call(ok.0, ok.1, "/org/free-desktop", ok.3),
            call(ok.0, ok.1, ok.2, "Notify"),
            call(ok.0, ok.1, ok.2, "org.freedesktop.Notifications.*"),
            call(ok.0, ok.1, ok.2, "org.freedesktop.Notifications.No-tify"),
            format!(
                "{{ bus = \"session\", name = \"{}\", path = \"{}\", call = \"{}\", own = true }}",
                ok.1, ok.2, ok.3
            ),
            "\"session\"".to_owned(),
        ];
        for b in bad {
            assert!(parse_manifest_str(&with_dbus(&b), "t").is_err(), "{b}");
        }
        assert!(parse_manifest_str(&with_dbus(&call(ok.0, ok.1, ok.2, ok.3)), "t").is_ok());
        // Not empty, not too many, not on a forbidden operation.
        assert!(parse_manifest_str(&with_dbus(""), "t").is_err());
        let many = vec![NOTIFY; MAX_DBUS_PER_OPERATION + 1].join(", ");
        assert!(parse_manifest_str(&with_dbus(&many), "t").is_err());
        let forbidden =
            format!("tool = \"a\"\n[operations.b]\nlevel = \"forbidden\"\ndbus = [{NOTIFY}]");
        assert!(parse_manifest_str(&forbidden, "t").is_err());
        // A program from Flatpak: Flatpak filters its buses itself.
        let flatpak = format!(
            "{}\n[launch]\nprogram = \"vilna-mcp-ext-x\"\nflatpak = \"org.example.App\"\ncommand = \"x\"",
            with_dbus(NOTIFY)
        );
        assert!(parse_manifest_str(&flatpak, "t").is_err());
    }

    #[test]
    fn builtin_dbus_tools_name_their_calls() {
        let r = Registry::builtin().expect("builtin");
        for tool in [
            "org.vilna.mcp.desktop",
            "org.vilna.mcp.portals",
            "org.vilna.mcp.atspi",
        ] {
            let m = r.get(tool).expect(tool);
            assert!(!m.dbus_calls().is_empty(), "{tool}");
            // Only the system Herkos runs them: behind the bus filter.
            assert!(
                m.launch.as_ref().is_some_and(|l| l.module.is_none()),
                "{tool}"
            );
        }
    }
}
