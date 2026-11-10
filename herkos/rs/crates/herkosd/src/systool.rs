// SPDX-License-Identifier: GPL-3.0-or-later
//! The "system" tool of Herkos (ADR 0026, section 5): the saved states of
//! the whole system and a return to one of them.
//!
//! On openSUSE Aeon every change of the system goes through
//! `transactional-update`, which makes a btrfs snapshot of the root (snapper
//! config `root`) and boots it next time. Going back is the same tool's
//! `transactional-update rollback N`: it makes snapshot N the one that
//! starts, and the computer runs it after a restart. Snapper's own
//! `snapper rollback` is not for transactional systems; this tool never
//! calls it.
//!
//! `herkosd system-tool <uid>` is an MCP server over stdio, started for a
//! person by `vilna-mcp-system@<uid>.service` as root: herkosd itself has no
//! right to change the system. The uid comes from the unit's name, not from
//! the request, and only a person in the group `wheel` (an administrator of
//! the computer) may return the system to a saved state.

use std::io::{BufRead, Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use serde_json::{Value, json};

use crate::snapshots::run;

const PROTOCOL_VERSIONS: [&str; 3] = ["2025-06-18", "2025-03-26", "2024-11-05"];
const ROOT_CONFIG: &str = "root";
/// The newest saved states shown to Gloxi and the person.
const MAX_LISTED: usize = 30;
const LIST_TIMEOUT: Duration = Duration::from_secs(60);
const ROLLBACK_TIMEOUT: Duration = Duration::from_secs(300);
const MAX_LINE: u64 = 64 * 1024;

/// One saved state of the system, as the tool reports it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct State {
    pub number: u64,
    pub date: String,
    pub description: String,
    /// It starts next time.
    pub default: bool,
    /// It is running now.
    pub active: bool,
    pub important: bool,
}

impl State {
    fn to_json(&self) -> Value {
        json!({
            "number": self.number,
            "date": self.date,
            "description": self.description,
            "starts_next": self.default,
            "running": self.active,
            "important": self.important,
        })
    }
}

/// Root snapshots from `snapper --jsonout -c root list`, without 0 ("current").
pub fn parse_list(text: &str) -> Result<Vec<State>, String> {
    let v: Value =
        serde_json::from_str(text).map_err(|_| "snapper gave no list of snapshots".to_owned())?;
    let items = v
        .get(ROOT_CONFIG)
        .and_then(Value::as_array)
        .ok_or("snapper gave no list of snapshots")?;
    let text_of = |i: &Value, k: &str| i.get(k).and_then(Value::as_str).unwrap_or("").to_owned();
    Ok(items
        .iter()
        .filter_map(|i| {
            let number = i.get("number").and_then(Value::as_u64).filter(|n| *n > 0)?;
            Some(State {
                number,
                date: text_of(i, "date"),
                description: text_of(i, "description").chars().take(200).collect(),
                default: i.get("default") == Some(&Value::Bool(true)),
                active: i.get("active") == Some(&Value::Bool(true)),
                important: i
                    .get("userdata")
                    .and_then(|u| u.get("important"))
                    .and_then(Value::as_str)
                    == Some("yes"),
            })
        })
        .collect())
}

/// How the tool reaches the system: real commands, or stand-ins in tests.
pub struct System {
    pub snapper: PathBuf,
    pub transactional_update: PathBuf,
    pub group: PathBuf,
    pub passwd: PathBuf,
    pub uid: u32,
}

impl System {
    pub fn new(uid: u32) -> System {
        System {
            snapper: PathBuf::from("/usr/bin/snapper"),
            transactional_update: PathBuf::from("/usr/sbin/transactional-update"),
            group: PathBuf::from("/etc/group"),
            passwd: PathBuf::from("/etc/passwd"),
            uid,
        }
    }

    fn states(&self) -> Result<Vec<State>, String> {
        let mut c = Command::new(&self.snapper);
        c.args([
            "--jsonout",
            "-c",
            ROOT_CONFIG,
            "list",
            "--disable-used-space",
        ]);
        parse_list(&run(c, LIST_TIMEOUT)?)
    }

    /// The person may change the whole computer: they are in `wheel`.
    fn is_admin(&self) -> Result<bool, String> {
        let accounts = crate::passwd::load(&self.passwd)?;
        let Some(name) = accounts
            .iter()
            .find(|a| a.uid == self.uid)
            .map(|a| a.name.clone())
        else {
            return Ok(false);
        };
        let groups = std::fs::read_to_string(&self.group)
            .map_err(|e| format!("{}: {e}", self.group.display()))?;
        let primary = std::fs::read_to_string(&self.passwd)
            .map_err(|e| format!("{}: {e}", self.passwd.display()))?
            .lines()
            .map(|l| l.split(':').collect::<Vec<_>>())
            .find(|f| f.len() == 7 && f[0] == name)
            .and_then(|f| f.get(3).map(|g| (*g).to_owned()));
        Ok(in_wheel(&groups, &name, primary.as_deref()))
    }

    pub fn list(&self) -> Result<Value, String> {
        let states = self.states()?;
        let skip = states.len().saturating_sub(MAX_LISTED);
        let listed: Vec<Value> = states.iter().skip(skip).map(State::to_json).collect();
        Ok(json!({"states": listed, "more": skip}))
    }

    /// `transactional-update rollback N`, for an administrator. What it
    /// replaced goes back with `restore`: Mitos undoes it like any change.
    pub fn rollback(&self, args: &Value) -> Result<Value, String> {
        let number = snapshot_arg(args)?;
        if !self.is_admin()? {
            return Err(
                "only an administrator of this computer may return the system to a saved state"
                    .into(),
            );
        }
        let states = self.states()?;
        if !states.iter().any(|s| s.number == number) {
            return Err(format!(
                "there is no saved state {number} (see list_snapshots)"
            ));
        }
        let previous = states
            .iter()
            .find(|s| s.default)
            .map(|s| s.number)
            .ok_or("snapper does not say which state starts next")?;
        if previous == number {
            return Err(format!("saved state {number} already starts next"));
        }
        let mut c = Command::new(&self.transactional_update);
        c.args(["--quiet", "rollback", &number.to_string()]);
        run(c, ROLLBACK_TIMEOUT)?;
        let now = self.states()?;
        if !now.iter().any(|s| s.default && s.number == number) {
            return Err(format!(
                "transactional-update finished, but saved state {number} does not start next"
            ));
        }
        Ok(json!({
            "starts_next": number,
            "replaced": previous,
            "restart_needed": true,
            "undo_arguments": {"snapshot": previous},
        }))
    }
}

/// `name` is in the group `wheel`, as a member or by its primary group.
pub fn in_wheel(groups: &str, name: &str, primary_gid: Option<&str>) -> bool {
    groups.lines().any(|line| {
        let f: Vec<&str> = line.split(':').collect();
        f.len() == 4
            && f[0] == "wheel"
            && (f[3].split(',').any(|m| m == name) || primary_gid == Some(f[2]))
    })
}

fn snapshot_arg(args: &Value) -> Result<u64, String> {
    let obj = args.as_object().ok_or("arguments must be an object")?;
    if let Some(k) = obj.keys().find(|k| k.as_str() != "snapshot") {
        return Err(format!("unknown argument {k}"));
    }
    obj.get("snapshot")
        .and_then(Value::as_u64)
        .filter(|n| *n > 0)
        .ok_or_else(|| "'snapshot' must be the number of a saved state".to_owned())
}

fn tools() -> Value {
    let number = json!({
        "type": "object",
        "properties": {"snapshot": {"type": "integer", "minimum": 1}},
        "required": ["snapshot"],
        "additionalProperties": false,
    });
    json!([
        {
            "name": "list_snapshots",
            "description": "The saved states of the system (snapshots of transactional-update)",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": false},
            "annotations": {"readOnlyHint": true},
        },
        {
            "name": "rollback",
            "description": "Make a saved state of the system the one that starts after a restart",
            "inputSchema": number,
            "annotations": {"readOnlyHint": false, "destructiveHint": true, "idempotentHint": true},
        },
        {
            "name": "restore",
            "description": "Undo a return of the system to a saved state",
            "inputSchema": number,
            "annotations": {"readOnlyHint": false, "destructiveHint": true, "idempotentHint": true},
        },
    ])
}

fn tool_result(out: Result<Value, String>) -> Value {
    match out {
        Ok(v) => json!({
            "content": [{"type": "text", "text": v.to_string()}],
            "structuredContent": v,
            "isError": false,
        }),
        Err(e) => json!({"content": [{"type": "text", "text": e}], "isError": true}),
    }
}

pub fn handle(system: &System, msg: &Value) -> Option<Value> {
    let id = msg.get("id")?.clone();
    if msg.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
        return Some(
            json!({"jsonrpc": "2.0", "id": id, "error": {"code": -32600, "message": "not JSON-RPC 2.0"}}),
        );
    }
    let params = msg.get("params").cloned().unwrap_or(Value::Null);
    let result = match msg.get("method").and_then(Value::as_str) {
        Some("initialize") => {
            let asked = params.get("protocolVersion").and_then(Value::as_str);
            let version = asked
                .filter(|v| PROTOCOL_VERSIONS.contains(v))
                .unwrap_or(PROTOCOL_VERSIONS[0]);
            json!({
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "vilna-mcp-system", "version": env!("CARGO_PKG_VERSION")},
            })
        }
        Some("ping") => json!({}),
        Some("tools/list") => json!({"tools": tools()}),
        Some("tools/call") => {
            let args = params
                .get("arguments")
                .cloned()
                .unwrap_or_else(|| json!({}));
            tool_result(match params.get("name").and_then(Value::as_str) {
                Some("list_snapshots") => system.list(),
                Some("rollback" | "restore") => system.rollback(&args),
                other => Err(format!("unknown tool {}", other.unwrap_or(""))),
            })
        }
        other => {
            let message = format!("unknown method {}", other.unwrap_or(""));
            return Some(
                json!({"jsonrpc": "2.0", "id": id, "error": {"code": -32601, "message": message}}),
            );
        }
    };
    Some(json!({"jsonrpc": "2.0", "id": id, "result": result}))
}

pub fn serve<R: BufRead>(
    system: &System,
    mut input: R,
    mut output: impl Write,
) -> Result<(), String> {
    loop {
        let mut line = Vec::new();
        let n = Read::by_ref(&mut input)
            .take(MAX_LINE)
            .read_until(b'\n', &mut line)
            .map_err(|e| e.to_string())?;
        if n == 0 {
            return Ok(());
        }
        if line.last() != Some(&b'\n') && n as u64 >= MAX_LINE {
            return Err("message too long".into());
        }
        let response = match serde_json::from_slice::<Value>(&line) {
            Ok(msg) => handle(system, &msg),
            Err(_) if line.iter().all(u8::is_ascii_whitespace) => None,
            Err(_) => Some(
                json!({"jsonrpc": "2.0", "id": null, "error": {"code": -32700, "message": "not valid JSON"}}),
            ),
        };
        if let Some(r) = response {
            let mut text = r.to_string();
            text.push('\n');
            output
                .write_all(text.as_bytes())
                .and_then(|()| output.flush())
                .map_err(|e| e.to_string())?;
        }
    }
}

/// `herkosd system-tool <uid>`: as root, for one person (vilna-mcp-system@.service).
pub fn main(args: &[String]) -> Result<(), String> {
    let uid: u32 = match args {
        [uid] => uid.parse().map_err(|_| "usage: herkosd system-tool UID")?,
        _ => return Err("usage: herkosd system-tool UID".into()),
    };
    if !(1000..60000).contains(&uid) {
        return Err(format!("uid {uid} is not a person"));
    }
    let system = System::new(uid);
    if !Path::new(&system.transactional_update).is_file() {
        eprintln!("herkosd system-tool: transactional-update is not installed; rollback will fail");
    }
    let stdin = std::io::stdin();
    serve(&system, stdin.lock(), std::io::stdout())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    const LIST: &str = r#"{"root": [
        {"number": 0, "default": false, "active": false, "date": "", "description": "current", "userdata": null},
        {"number": 1, "default": false, "active": false, "date": "2026-11-01 08:00:00", "description": "first root filesystem", "userdata": {"important": "yes"}},
        {"number": 7, "default": true, "active": true, "date": "2026-11-09 09:00:00", "description": "Snapshot Update of #1", "userdata": null}
    ]}"#;

    #[test]
    fn list_and_admins() {
        let states = parse_list(LIST).expect("list");
        assert_eq!(
            states.iter().map(|s| s.number).collect::<Vec<_>>(),
            vec![1, 7]
        );
        assert!(states[0].important && states[1].default && states[1].active);
        assert!(parse_list("[]").is_err());
        let groups = "root:x:0:\nwheel:x:497:anna,ola\nusers:x:100:bohdan\n";
        assert!(in_wheel(groups, "anna", Some("1000")));
        assert!(!in_wheel(groups, "bohdan", Some("100")));
        assert!(in_wheel(groups, "bohdan", Some("497")));
        assert!(!in_wheel(groups, "ann", None));
    }

    fn fake(dir: &Path, name: &str, script: &str) -> PathBuf {
        let p = dir.join(name);
        std::fs::write(&p, format!("#!/bin/sh\n{script}\n")).expect("write");
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).expect("chmod");
        p
    }

    fn system(name: &str, uid: u32) -> (System, PathBuf) {
        let dir =
            std::env::temp_dir().join(format!("herkosd-systool-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("mkdir");
        let state = dir.join("list.json");
        std::fs::write(&state, LIST).expect("list");
        // After a rollback to 1, snapshot 1 starts next.
        let after = LIST
            .replace(
                r#""number": 7, "default": true"#,
                r#""number": 7, "default": false"#,
            )
            .replace(
                r#""number": 1, "default": false"#,
                r#""number": 1, "default": true"#,
            );
        std::fs::write(dir.join("after.json"), after).expect("after");
        let snapper = fake(&dir, "snapper", &format!("cat {}", state.display()));
        let tu = fake(
            &dir,
            "transactional-update",
            &format!(
                "echo \"$@\" > {d}/tu.log; cp {d}/after.json {d}/list.json",
                d = dir.display()
            ),
        );
        std::fs::write(
            dir.join("passwd"),
            "anna:x:1000:1000::/home/anna:/bin/sh\nbohdan:x:1001:100::/home/bohdan:/bin/sh\n",
        )
        .expect("passwd");
        std::fs::write(dir.join("group"), "wheel:x:497:anna\nusers:x:100:\n").expect("group");
        let s = System {
            snapper,
            transactional_update: tu,
            group: dir.join("group"),
            passwd: dir.join("passwd"),
            uid,
        };
        (s, dir)
    }

    #[test]
    fn an_administrator_returns_the_system() {
        let (s, dir) = system("admin", 1000);
        assert_eq!(s.list().expect("list")["states"][1]["starts_next"], true);
        assert!(s.rollback(&json!({"snapshot": 99})).is_err());
        assert!(
            s.rollback(&json!({"snapshot": 7}))
                .expect_err("already")
                .contains("already")
        );
        assert!(s.rollback(&json!({"snapshot": "1"})).is_err());
        assert!(s.rollback(&json!({"snapshot": 1, "x": 1})).is_err());
        let out = s.rollback(&json!({"snapshot": 1})).expect("rollback");
        assert_eq!(out["undo_arguments"], json!({"snapshot": 7}));
        assert_eq!(out["restart_needed"], true);
        let log = std::fs::read_to_string(dir.join("tu.log")).expect("log");
        assert_eq!(log.trim(), "--quiet rollback 1");
    }

    #[test]
    fn others_may_only_look() {
        let (s, dir) = system("other", 1001);
        assert!(s.list().is_ok());
        let e = s.rollback(&json!({"snapshot": 1})).expect_err("not admin");
        assert!(e.contains("administrator"), "{e}");
        assert!(!dir.join("tu.log").exists());
    }

    #[test]
    fn speaks_mcp() {
        let (s, _dir) = system("mcp", 1000);
        let input = concat!(
            r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}"#,
            "\n",
            r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#,
            "\n",
            r#"{"jsonrpc":"2.0","id":2,"method":"tools/list"}"#,
            "\n",
            r#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_snapshots","arguments":{}}}"#,
            "\n",
            r#"{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"format_disk","arguments":{}}}"#,
            "\n",
            "not json\n",
        );
        let mut out = Vec::new();
        serve(&s, input.as_bytes(), &mut out).expect("serve");
        let replies: Vec<Value> = String::from_utf8(out)
            .expect("utf-8")
            .lines()
            .map(|l| serde_json::from_str(l).expect("json"))
            .collect();
        assert_eq!(replies.len(), 5);
        assert_eq!(replies[0]["result"]["protocolVersion"], "2025-03-26");
        assert_eq!(
            replies[1]["result"]["tools"].as_array().map(Vec::len),
            Some(3)
        );
        assert_eq!(
            replies[2]["result"]["structuredContent"]["states"][0]["number"],
            1
        );
        assert_eq!(replies[3]["result"]["isError"], true);
        assert_eq!(replies[4]["error"]["code"], -32700);
    }
}
