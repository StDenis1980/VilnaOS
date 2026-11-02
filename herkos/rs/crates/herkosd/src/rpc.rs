// SPDX-License-Identifier: GPL-3.0-or-later
//! JSON-RPC 2.0 dispatch for one connection (herkos/docs/protocol.md).
//!
//! Mirrors `Connection` in `herkos/src/herkos/server.py`. This skeleton knows
//! no tools yet: every action is denied with `denied_unknown`, which is what
//! the protocol prescribes for a tool without a manifest.

use std::time::{SystemTime, UNIX_EPOCH};

use herkos_core::PROTOCOL_VERSION;
use herkos_core::request::parse_request;
use serde_json::{Value, json};

pub const PARSE_ERROR: i64 = -32700;
pub const INVALID_REQUEST: i64 = -32600;
pub const METHOD_NOT_FOUND: i64 = -32601;
pub const INVALID_PARAMS: i64 = -32602;
pub const HELLO_REQUIRED: i64 = -32001;
pub const PROTOCOL_MISMATCH: i64 = -32002;

/// The highest risk level this build can grant (R0 until decisions land).
const MAX_LEVEL: &str = "R0";

pub fn result(id: &Value, result: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": result})
}

pub fn error(id: &Value, code: i64, message: &str) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})
}

/// Time-ordered and unique enough for one journal: ms timestamp + random.
pub fn new_action_id() -> String {
    let ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis());
    let mut rnd = [0u8; 6];
    // On failure the buffer stays zero; the timestamp alone still orders ids.
    let _ = rustix::rand::getrandom(&mut rnd, rustix::rand::GetRandomFlags::empty());
    let hex: String = rnd.iter().map(|b| format!("{b:02x}")).collect();
    format!("{ms:013x}{hex}")
}

fn denied(code: &str, message: &str) -> Value {
    json!({
        "action_id": new_action_id(),
        "decision": "denied",
        "code": code,
        "level": "forbidden",
        "message": message,
    })
}

#[derive(Default)]
pub struct Connection {
    hello: bool,
}

impl Connection {
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
                Ok(request) => result(
                    id,
                    denied("denied_unknown", &format!("unknown tool {}", request.tool)),
                ),
            },
            "herkos.undo" => {
                let action_id = params.get("action_id").and_then(Value::as_str);
                match action_id {
                    Some(a)
                        if !a.is_empty()
                            && a.chars().count() <= 64
                            && a.chars().all(char::is_alphanumeric) =>
                    {
                        result(id, denied("denied_unknown", "nothing to undo"))
                    }
                    _ => error(id, INVALID_PARAMS, "'action_id' must be an action id"),
                }
            }
            "herkos.session" => result(
                id,
                json!({"tainted": false, "taint_sources": [], "findings": []}),
            ),
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
                "max_level": MAX_LEVEL,
            }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
        let mut c = Connection::default();
        let r = c.dispatch(&json!({"jsonrpc": "2.0", "id": 2, "method": "herkos.session"}));
        assert_eq!(code(r), HELLO_REQUIRED);
        hello(&mut c);
        let r = c.dispatch(&json!({"jsonrpc": "2.0", "id": 2, "method": "herkos.session"}));
        assert!(r.is_some_and(|r| r["result"]["tainted"] == false));
    }

    #[test]
    fn bad_envelopes() {
        let mut c = Connection::default();
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
        let mut c = Connection::default();
        hello(&mut c);
        assert!(
            c.dispatch(&json!({"jsonrpc": "2.0", "method": "herkos.session"}))
                .is_none()
        );
        let r = c.dispatch(&json!({"jsonrpc": "2.0", "id": "s", "method": "nope"}));
        assert!(r.is_some_and(|r| r["id"] == "s" && r["error"]["code"] == METHOD_NOT_FOUND));
    }

    #[test]
    fn actions_are_denied_unknown() {
        let mut c = Connection::default();
        hello(&mut c);
        let r = c.dispatch(
            &json!({"jsonrpc": "2.0", "id": 3, "method": "herkos.request_action",
            "params": {"tool": "org.vilna.mcp.files", "operation": "read", "reason": "r"}}),
        );
        assert!(r.is_some_and(|r| r["result"]["code"] == "denied_unknown"));
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
