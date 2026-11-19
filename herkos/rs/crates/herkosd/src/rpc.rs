// SPDX-License-Identifier: GPL-3.0-or-later
//! JSON-RPC 2.0 dispatch for one connection (herkos/docs/protocol.md).
//!
//! Mirrors `Connection` in `herkos/src/herkos/server.py`. Each connection
//! is one conversation: its own session state (taint), shared gate.

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use herkos_core::PROTOCOL_VERSION;
use herkos_core::request::parse_request;
use serde_json::{Value, json};

use crate::gate::{Gate, Session};

pub const PARSE_ERROR: i64 = -32700;
pub const INVALID_REQUEST: i64 = -32600;
pub const METHOD_NOT_FOUND: i64 = -32601;
pub const INVALID_PARAMS: i64 = -32602;
pub const INTERNAL_ERROR: i64 = -32603;
pub const HELLO_REQUIRED: i64 = -32001;
pub const PROTOCOL_MISMATCH: i64 = -32002;

pub fn result(id: &Value, result: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": result})
}

pub fn error(id: &Value, code: i64, message: &str) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})
}

/// `n` random bytes as hex. On failure the bytes stay zero: ids built with
/// it also carry a timestamp, and grant ids are checked for duplicates.
pub fn random_hex(n: usize) -> String {
    let mut rnd = vec![0u8; n];
    let _ = rustix::rand::getrandom(&mut rnd, rustix::rand::GetRandomFlags::empty());
    rnd.iter().map(|b| format!("{b:02x}")).collect()
}

/// Time-ordered and unique enough for one journal: ms timestamp + random.
pub fn new_action_id() -> String {
    let ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis());
    format!("{ms:013x}{}", random_hex(6))
}

pub struct Connection {
    gate: Arc<Gate>,
    session: Session,
    hello: bool,
}

impl Connection {
    pub fn new(gate: Arc<Gate>) -> Connection {
        Connection {
            gate,
            session: Session::default(),
            hello: false,
        }
    }

    /// The response to one decoded message, or `None` for a notification.
    pub fn dispatch(&mut self, msg: &Value) -> Option<Value> {
        let null = Value::Null;
        let Some(obj) = msg
            .as_object()
            .filter(|o| o.get("jsonrpc") == Some(&json!("2.0")))
        else {
            return Some(error(&null, INVALID_REQUEST, "not a JSON-RPC 2.0 message"));
        };
        let is_notification = !obj.contains_key("id");
        let id = obj.get("id").unwrap_or(&null);
        let id_ok = match id {
            Value::Null | Value::String(_) => true,
            Value::Number(n) => n.is_i64() || n.is_u64(),
            _ => false,
        };
        if !id_ok {
            return Some(error(
                &null,
                INVALID_REQUEST,
                "id must be a string or an integer",
            ));
        }
        let Some(method) = obj.get("method").and_then(Value::as_str) else {
            return Some(error(id, INVALID_REQUEST, "method must be a string"));
        };
        let empty = json!({});
        let params = obj.get("params").unwrap_or(&empty);
        let response = self.call(id, method, params);
        (!is_notification).then_some(response)
    }

    fn call(&mut self, id: &Value, method: &str, params: &Value) -> Value {
        if method == "herkos.hello" {
            return self.hello(id, params);
        }
        if !self.hello {
            return error(id, HELLO_REQUIRED, "call herkos.hello first");
        }
        match method {
            "herkos.request_action" => match parse_request(params) {
                Err(e) => error(id, INVALID_PARAMS, &e.0),
                Ok(request) => result(id, self.gate.handle(&request, &mut self.session).to_json()),
            },
            "herkos.undo" => {
                let action_id = params.get("action_id").and_then(Value::as_str);
                match action_id {
                    Some(a)
                        if !a.is_empty()
                            && a.chars().count() <= 64
                            && a.chars().all(char::is_alphanumeric) =>
                    {
                        result(id, self.gate.undo(a, &mut self.session).to_json())
                    }
                    _ => error(id, INVALID_PARAMS, "'action_id' must be an action id"),
                }
            }
            "herkos.tools" => result(id, self.gate.listing()),
            "herkos.programs" => result(id, self.gate.program_list()),
            "herkos.set_program" => {
                let tool = params.get("tool").and_then(Value::as_str);
                let on = params.get("enabled").and_then(Value::as_bool);
                let reason = match params.get("reason") {
                    None => Some(""),
                    Some(r) => r.as_str(),
                };
                match (tool, on, reason) {
                    (Some(tool), Some(on), Some(reason)) => {
                        let reason: String = reason.chars().take(500).collect();
                        match self.gate.set_program(tool, on, &reason) {
                            Ok(answer) => result(id, answer),
                            Err(e) => error(id, INVALID_PARAMS, &e),
                        }
                    }
                    _ => error(
                        id,
                        INVALID_PARAMS,
                        "'tool' (text) and 'enabled' (true/false)",
                    ),
                }
            }
            "herkos.session" => {
                // Verdicts that came in meanwhile are part of the answer.
                self.gate.settle(&mut self.session, false);
                result(
                    id,
                    json!({
                        "tainted": self.session.tainted,
                        "taint_sources": self.session.taint_sources,
                        "findings": self.session.keros.findings.iter().map(|(src, f)| {
                            json!({"source": src, "rule": f.rule, "description": f.description})
                        }).collect::<Vec<_>>(),
                    }),
                )
            }
            _ => error(id, METHOD_NOT_FOUND, &format!("unknown method {method}")),
        }
    }

    fn hello(&mut self, id: &Value, params: &Value) -> Value {
        if !params.is_object() {
            return error(id, INVALID_PARAMS, "params must be an object");
        }
        let speaks = match params.get("protocol") {
            Some(Value::String(v)) => v == PROTOCOL_VERSION,
            Some(Value::Array(vs)) => vs.iter().any(|v| v == PROTOCOL_VERSION),
            _ => false,
        };
        if !speaks {
            return error(
                id,
                PROTOCOL_MISMATCH,
                &format!("this Herkos speaks protocol {PROTOCOL_VERSION}"),
            );
        }
        self.hello = true;
        result(
            id,
            json!({
                "protocol": PROTOCOL_VERSION,
                "server": concat!("herkosd/", env!("CARGO_PKG_VERSION")),
                "max_level": self.gate.config.max_level.name(),
            }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::parse_config;
    use crate::gate::{DenyAll, NoTools};
    use crate::grants::{GrantStore, now};
    use crate::mitos::Journal;
    use herkos_core::manifest::Registry;

    fn conn(name: &str) -> Connection {
        let dir = std::env::temp_dir().join(format!("herkosd-rpc-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let mut config = parse_config("", std::path::Path::new("t")).expect("config");
        config.state_dir = dir;
        let journal = Journal::open(&config.journal_path()).expect("journal");
        let grants = GrantStore::new(&config.grants_path(), now);
        let gate = Gate::new(
            config,
            Registry::builtin().expect("manifests"),
            Box::new(NoTools),
            grants,
            journal,
            Box::new(DenyAll),
        );
        Connection::new(Arc::new(gate))
    }

    fn hello(c: &mut Connection) {
        let r = c.dispatch(&json!({"jsonrpc": "2.0", "id": 1, "method": "herkos.hello",
            "params": {"protocol": ["0.1"]}}));
        assert!(r.is_some_and(|r| r["result"]["protocol"] == "0.1"));
    }

    fn code(r: Option<Value>) -> Value {
        r.map_or(Value::Null, |r| r["error"]["code"].clone())
    }

    #[test]
    fn hello_first() {
        let mut c = conn("t1");
        let r = c.dispatch(&json!({"jsonrpc": "2.0", "id": 2, "method": "herkos.session"}));
        assert_eq!(code(r), HELLO_REQUIRED);
        hello(&mut c);
        let r = c.dispatch(&json!({"jsonrpc": "2.0", "id": 2, "method": "herkos.session"}));
        assert!(r.is_some_and(|r| r["result"]["tainted"] == false));
    }

    #[test]
    fn bad_envelopes() {
        let mut c = conn("t2");
        for m in [
            json!([]),
            json!({"id": 1, "method": "x"}),
            json!({"jsonrpc": "2.0", "id": true, "method": "x"}),
            json!({"jsonrpc": "2.0", "id": 1.5, "method": "x"}),
            json!({"jsonrpc": "2.0", "id": 1, "method": 3}),
        ] {
            assert_eq!(code(c.dispatch(&m)), INVALID_REQUEST, "{m}");
        }
    }

    #[test]
    fn notifications_and_ids() {
        let mut c = conn("t3");
        hello(&mut c);
        assert!(
            c.dispatch(&json!({"jsonrpc": "2.0", "method": "herkos.session"}))
                .is_none()
        );
        let r = c.dispatch(&json!({"jsonrpc": "2.0", "id": "s", "method": "nope"}));
        assert!(r.is_some_and(|r| r["id"] == "s" && r["error"]["code"] == METHOD_NOT_FOUND));
    }

    #[test]
    fn actions_go_through_the_gate() {
        let mut c = conn("t4");
        hello(&mut c);
        let r = c.dispatch(
            &json!({"jsonrpc": "2.0", "id": 3, "method": "herkos.request_action",
            "params": {"tool": "org.vilna.mcp.files", "operation": "read", "reason": "r"}}),
        );
        assert!(r.is_some_and(|r| r["result"]["code"] == "denied_mismatch"));
        let r = c.dispatch(
            &json!({"jsonrpc": "2.0", "id": 5, "method": "herkos.request_action",
            "params": {"tool": "org.example.what", "operation": "read", "reason": "r"}}),
        );
        assert!(r.is_some_and(
            |r| r["result"]["code"] == "denied_unknown" && r["result"]["level"] == "R4"
        ));
        let r = c.dispatch(&json!({"jsonrpc": "2.0", "id": 4, "method": "herkos.undo",
            "params": {"action_id": "../x"}}));
        assert_eq!(code(r), INVALID_PARAMS);
    }

    #[test]
    fn action_ids() {
        let (a, b) = (new_action_id(), new_action_id());
        assert_eq!(a.len(), 25);
        assert!(a != b && a.chars().all(|c| c.is_ascii_hexdigit()));
    }
}
