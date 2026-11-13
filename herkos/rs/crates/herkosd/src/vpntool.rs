// SPDX-License-Identifier: GPL-3.0-or-later
//! The "VPN" tool of Herkos (ADR 0027, sections 3-5): the kill switch of
//! Vilna and the encrypted DNS resolver, through `vilna-netguard`.
//!
//! `herkosd vpn-tool <uid>` is an MCP server over stdio, started for a person
//! by `vilna-mcp-vpn@<uid>.service` as root: herkosd itself may not change
//! nftables or systemd-resolved. Herkos asks the person first (R2 for what
//! makes protection stronger, R3 every time for what weakens it); this tool
//! only checks the arguments and runs `vilna-netguard`, which checks them
//! again. The person's VPN account never passes through here.

use std::io::{BufRead, Read, Write};
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

use serde_json::{Map, Value, json};

use crate::snapshots::run;

const PROTOCOL_VERSIONS: [&str; 3] = ["2025-06-18", "2025-03-26", "2024-11-05"];
const TIMEOUT: Duration = Duration::from_secs(30);
const MAX_LINE: u64 = 64 * 1024;
/// ADR 0027, section 4: a captive-portal window is 5 minutes.
const PORTAL_SECONDS: &str = "300";

pub struct Vpn {
    pub netguard: PathBuf,
}

impl Vpn {
    pub fn new() -> Vpn {
        Vpn {
            netguard: PathBuf::from("/usr/bin/vilna-netguard"),
        }
    }

    fn netguard(&self, args: &[&str]) -> Result<Value, String> {
        let mut c = Command::new(&self.netguard);
        c.args(args);
        let out = run(c, TIMEOUT)?;
        serde_json::from_str(out.trim())
            .map_err(|_| "vilna-netguard gave no answer it could read".to_owned())
    }

    pub fn call(&self, name: &str, args: &Value) -> Result<Value, String> {
        let obj = args.as_object().ok_or("arguments must be an object")?;
        match name {
            "status" => {
                only(obj, &[])?;
                self.netguard(&["status"])
            }
            "killswitch_on" => {
                only(obj, &[])?;
                self.netguard(&["on"])
            }
            "killswitch_off" => {
                only(obj, &[])?;
                self.netguard(&["off"])
            }
            "open_portal" => {
                only(obj, &[])?;
                let mut v = self.netguard(&["portal", PORTAL_SECONDS])?;
                v["closes_by_itself_after_seconds"] = json!(300);
                Ok(v)
            }
            "allow_lan" | "forget_lan" => {
                only(obj, &["network"])?;
                let network = text_arg(obj, "network", 64)?;
                let verb = if name == "allow_lan" {
                    "allow-lan"
                } else {
                    "forget-lan"
                };
                self.netguard(&[verb, network])
            }
            "set_dns" => {
                only(obj, &["resolver"])?;
                let resolver = text_arg(obj, "resolver", 16)?;
                if !["quad9", "mullvad"].contains(&resolver) {
                    return Err("'resolver' must be quad9 or mullvad".into());
                }
                let before = self
                    .netguard(&["status"])
                    .ok()
                    .and_then(|s| s.get("dns").and_then(Value::as_str).map(str::to_owned));
                let mut v = self.netguard(&["dns", resolver])?;
                if let Some(before) = before.filter(|b| b != resolver) {
                    v["undo_arguments"] = json!({"resolver": before});
                }
                Ok(v)
            }
            "start_provider" => {
                only(obj, &["provider"])?;
                let provider = text_arg(obj, "provider", 16)?;
                if provider != "mullvad" {
                    return Err("'provider' must be mullvad".into());
                }
                self.netguard(&["provider", provider])
            }
            other => Err(format!("unknown tool {other}")),
        }
    }
}

fn only(obj: &Map<String, Value>, allowed: &[&str]) -> Result<(), String> {
    match obj.keys().find(|k| !allowed.contains(&k.as_str())) {
        Some(k) => Err(format!("unknown argument {k}")),
        None => Ok(()),
    }
}

/// A short, plain argument: no option-like or control characters reach the command line.
fn text_arg<'a>(obj: &'a Map<String, Value>, key: &str, max: usize) -> Result<&'a str, String> {
    obj.get(key)
        .and_then(Value::as_str)
        .filter(|s| {
            !s.is_empty()
                && s.len() <= max
                && !s.starts_with('-')
                && s.chars()
                    .all(|c| c.is_ascii_alphanumeric() || ".:/".contains(c))
        })
        .ok_or_else(|| format!("'{key}' is missing or not valid"))
}

fn tools() -> Value {
    let empty = json!({"type": "object", "properties": {}, "additionalProperties": false});
    let network = json!({
        "type": "object",
        "properties": {"network": {"type": "string", "description": "A private network, e.g. 192.168.1.0/24"}},
        "required": ["network"],
        "additionalProperties": false,
    });
    let change = json!({"readOnlyHint": false, "destructiveHint": false, "idempotentHint": true});
    json!([
        {"name": "status", "description": "The state of the VPN kill switch and which encrypted DNS is used",
         "inputSchema": empty, "annotations": {"readOnlyHint": true}},
        {"name": "killswitch_on", "description": "Let traffic leave only through a VPN tunnel",
         "inputSchema": empty, "annotations": change},
        {"name": "killswitch_off", "description": "Turn the VPN kill switch off",
         "inputSchema": empty, "annotations": change},
        {"name": "allow_lan", "description": "Let a private local network (printer, NAS) through the kill switch",
         "inputSchema": network, "annotations": change},
        {"name": "forget_lan", "description": "Stop letting a local network through the kill switch",
         "inputSchema": network, "annotations": change},
        {"name": "open_portal", "description": "Open the kill switch for 5 minutes to sign in to a hotel or cafe Wi-Fi",
         "inputSchema": empty, "annotations": change},
        {"name": "set_dns", "description": "Choose the encrypted DNS: quad9 (default) or mullvad (also blocks ads and trackers)",
         "inputSchema": {
            "type": "object",
            "properties": {"resolver": {"type": "string", "enum": ["quad9", "mullvad"]}},
            "required": ["resolver"],
            "additionalProperties": false,
         },
         "annotations": change},
        {"name": "start_provider", "description": "Start the service of the chosen VPN provider (mullvad) and keep it on after a reboot",
         "inputSchema": {
            "type": "object",
            "properties": {"provider": {"type": "string", "enum": ["mullvad"]}},
            "required": ["provider"],
            "additionalProperties": false,
         },
         "annotations": change},
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

pub fn handle(vpn: &Vpn, msg: &Value) -> Option<Value> {
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
                "serverInfo": {"name": "vilna-mcp-vpn", "version": env!("CARGO_PKG_VERSION")},
            })
        }
        Some("ping") => json!({}),
        Some("tools/list") => json!({"tools": tools()}),
        Some("tools/call") => {
            let args = params
                .get("arguments")
                .cloned()
                .unwrap_or_else(|| json!({}));
            let name = params.get("name").and_then(Value::as_str).unwrap_or("");
            tool_result(vpn.call(name, &args))
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

pub fn serve<R: BufRead>(vpn: &Vpn, mut input: R, mut output: impl Write) -> Result<(), String> {
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
            Ok(msg) => handle(vpn, &msg),
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

/// `herkosd vpn-tool <uid>`: as root, for one person (vilna-mcp-vpn@.service).
/// The uid only says whose Herkos asked; the kill switch is the computer's.
pub fn main(args: &[String]) -> Result<(), String> {
    let uid: u32 = match args {
        [uid] => uid.parse().map_err(|_| "usage: herkosd vpn-tool UID")?,
        _ => return Err("usage: herkosd vpn-tool UID".into()),
    };
    if !(1000..60000).contains(&uid) {
        return Err(format!("uid {uid} is not a person"));
    }
    let vpn = Vpn::new();
    let stdin = std::io::stdin();
    serve(&vpn, stdin.lock(), std::io::stdout())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use std::path::Path;

    /// A stand-in for vilna-netguard that logs its arguments and answers JSON.
    fn vpn(name: &str) -> (Vpn, PathBuf) {
        let dir =
            std::env::temp_dir().join(format!("herkosd-vpntool-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("mkdir");
        let p = dir.join("vilna-netguard");
        std::fs::write(
            &p,
            format!(
                "#!/bin/sh\necho \"$@\" >> {d}/log\ncase \"$1\" in\n  status) echo '{{\"enabled\":false,\"dns\":\"quad9\"}}';;\n  dns) echo \"{{\\\"dns\\\":\\\"$2\\\"}}\";;\n  *) echo '{{\"enabled\":true}}';;\nesac\n",
                d = dir.display()
            ),
        )
        .expect("write");
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).expect("chmod");
        (Vpn { netguard: p }, dir)
    }

    fn log(dir: &Path) -> String {
        std::fs::read_to_string(dir.join("log")).unwrap_or_default()
    }

    #[test]
    fn each_tool_runs_one_command() {
        let (v, dir) = vpn("each");
        v.call("killswitch_on", &json!({})).expect("on");
        v.call("killswitch_off", &json!({})).expect("off");
        v.call("open_portal", &json!({})).expect("portal");
        v.call("allow_lan", &json!({"network": "192.168.1.0/24"}))
            .expect("lan");
        v.call("forget_lan", &json!({"network": "192.168.1.0/24"}))
            .expect("forget");
        v.call("start_provider", &json!({"provider": "mullvad"}))
            .expect("provider");
        assert_eq!(
            log(&dir),
            "on\noff\nportal 300\nallow-lan 192.168.1.0/24\nforget-lan 192.168.1.0/24\nprovider mullvad\n"
        );
    }

    #[test]
    fn set_dns_can_be_undone() {
        let (v, _dir) = vpn("dns");
        let out = v
            .call("set_dns", &json!({"resolver": "mullvad"}))
            .expect("dns");
        assert_eq!(out["dns"], "mullvad");
        assert_eq!(out["undo_arguments"], json!({"resolver": "quad9"}));
        let same = v
            .call("set_dns", &json!({"resolver": "quad9"}))
            .expect("dns");
        assert!(same.get("undo_arguments").is_none());
    }

    #[test]
    fn odd_arguments_never_reach_the_command() {
        let (v, dir) = vpn("odd");
        for (name, args) in [
            ("killswitch_off", json!({"force": true})),
            ("killswitch_on", json!([])),
            ("allow_lan", json!({})),
            ("allow_lan", json!({"network": "--state=/etc/passwd"})),
            ("allow_lan", json!({"network": "192.168.1.0/24 8.8.8.0/24"})),
            ("allow_lan", json!({"network": 5})),
            ("set_dns", json!({"resolver": "8.8.8.8"})),
            ("set_dns", json!({"resolver": "quad9", "x": 1})),
            ("start_provider", json!({})),
            ("start_provider", json!({"provider": "proton"})),
            ("start_provider", json!({"provider": "sshd"})),
            ("flush_ruleset", json!({})),
        ] {
            assert!(v.call(name, &args).is_err(), "{name} {args}");
        }
        assert_eq!(log(&dir), "");
    }

    #[test]
    fn speaks_mcp() {
        let (v, _dir) = vpn("mcp");
        let input = concat!(
            r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}"#,
            "\n",
            r#"{"jsonrpc":"2.0","id":2,"method":"tools/list"}"#,
            "\n",
            r#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"status","arguments":{}}}"#,
            "\n",
            "not json\n",
        );
        let mut out = Vec::new();
        serve(&v, input.as_bytes(), &mut out).expect("serve");
        let replies: Vec<Value> = String::from_utf8(out)
            .expect("utf-8")
            .lines()
            .map(|l| serde_json::from_str(l).expect("json"))
            .collect();
        assert_eq!(replies.len(), 4);
        assert_eq!(
            replies[1]["result"]["tools"].as_array().map(Vec::len),
            Some(8)
        );
        assert_eq!(replies[2]["result"]["structuredContent"]["dns"], "quad9");
        assert_eq!(replies[3]["error"]["code"], -32700);
    }
}
