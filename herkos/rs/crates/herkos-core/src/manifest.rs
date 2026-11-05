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
const OPERATION_KEYS: [&str; 7] = [
    "level",
    "summary",
    "undoable",
    "reversible",
    "returns_external",
    "egress",
    "resource_args",
];

/// The manifests shipped with Herkos: the same files Herkos-lite reads.
const BUILTIN: [(&str, &str); 2] = [
    (
        "org.vilna.mcp.backup.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.mcp.backup.toml"),
    ),
    (
        "org.vilna.mcp.files.toml",
        include_str!("../../../../src/herkos/manifests/org.vilna.mcp.files.toml"),
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
}

impl Operation {
    pub fn forbidden(&self) -> bool {
        self.level.is_none()
    }

    pub fn changes_state(&self) -> bool {
        self.level.is_some_and(|l| l >= Level::R2)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Manifest {
    pub tool: String,
    pub description: String,
    pub operations: BTreeMap<String, Operation>,
}

impl Manifest {
    pub fn operation(&self, name: &str) -> Option<&Operation> {
        self.operations.get(name)
    }
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
    let unknown = unknown_keys(data, &["tool", "description", "operations"]);
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
            },
        );
    }
    Ok(Manifest {
        tool,
        description,
        operations,
    })
}

/// The manifests Herkos knows. A tool not in here is unknown.
#[derive(Debug, Default, Clone)]
pub struct Registry {
    by_tool: BTreeMap<String, Manifest>,
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
        Ok(r)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
