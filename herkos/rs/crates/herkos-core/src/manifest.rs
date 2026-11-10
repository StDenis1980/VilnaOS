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
const OPERATION_KEYS: [&str; 9] = [
    "level",
    "summary",
    "undoable",
    "reversible",
    "returns_external",
    "egress",
    "resource_args",
    "command_arg",
    "command_key",
];
/// A group of the program's commands in `[commands]`: `home.*`.
const GROUP_SUFFIX: &str = ".*";
pub const MAX_COMMANDS: usize = 4096;
/// At most this many commands in one call (a batch); more is not read.
pub const MAX_CALL_COMMANDS: usize = 64;

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
const BUILTIN: [(&str, &str); 5] = [
    (
        "org.vilna.gloxi.network.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.gloxi.network.toml"),
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
        "org.vilna.mcp.files.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.mcp.files.toml"),
    ),
    (
        "org.vilna.mcp.settings.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.mcp.settings.toml"),
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

    /// `(known, level)` of one command: the id itself, else its nearest group.
    pub fn command_level(&self, command: &str) -> (bool, Option<Level>) {
        if let Some(level) = self.commands.get(command) {
            return (true, *level);
        }
        let parts: Vec<&str> = command.split('.').collect();
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
        let arg_name = |key: &str| match op.get(key) {
            None => Ok(None),
            Some(Value::String(s)) if is_name(s) => Ok(Some(s.clone())),
            Some(_) => bad(format!("{at}: '{key}' must be an argument name")),
        };
        let command_arg = arg_name("command_arg")?;
        let command_key = arg_name("command_key")?;
        if command_key.is_some() && command_arg.is_none() {
            return bad(format!("{at}: 'command_key' goes only with 'command_arg'"));
        }
        if command_arg.is_some() && !data.contains_key("commands") {
            return bad(format!("{at}: 'command_arg' needs a [commands] table"));
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
            },
        );
    }
    let launch = match data.get("launch") {
        None => None,
        Some(l) => Some(parse_launch(l, source)?),
    };
    let commands = match data.get("commands") {
        None => BTreeMap::new(),
        Some(c) => parse_commands(c, source)?,
    };
    Ok(Manifest {
        tool,
        description,
        operations,
        launch,
        commands,
    })
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
}
