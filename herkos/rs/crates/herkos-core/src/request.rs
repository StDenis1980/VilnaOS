// SPDX-License-Identifier: GPL-3.0-or-later
//! The action request Gloxi sends to the gate (permissions.md, section 3).
//!
//! Only what Gloxi *proposes* is read. Whatever decides the outcome (risk
//! level, taint, the action id) is set by Herkos; if the model sends such
//! fields, they are ignored. Mirrors `herkos/src/herkos/request.py`.

use std::fmt;

use serde_json::{Map, Value};

pub const MAX_RESOURCES: usize = 1000;
pub const MAX_REASON: usize = 500;
const MAX_URI: usize = 4096;

/// Fields the model may send but Herkos sets itself.
const IGNORED_FIELDS: [&str; 4] = ["action_id", "level", "tainted", "risk"];
const ALLOWED_FIELDS: [&str; 6] = [
    "tool",
    "operation",
    "resources",
    "reason",
    "origin",
    "arguments",
];

/// The request is malformed; it is denied without being assessed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RequestError(pub String);

impl fmt::Display for RequestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for RequestError {}

fn err<T>(msg: impl Into<String>) -> Result<T, RequestError> {
    Err(RequestError(msg.into()))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Resource {
    pub uri: String,
    pub scheme: String,
    /// For `file://` resources: the absolute, normalised path (symlinks are
    /// resolved later by the policy, against the real filesystem).
    pub path: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ActionRequest {
    pub tool: String,
    pub operation: String,
    pub resources: Vec<Resource>,
    pub reason: String,
    pub user_turn: Option<u64>,
    /// Passed through to the tool, never interpreted here.
    pub arguments: Map<String, Value>,
}

/// `^[a-z0-9][a-z0-9_.-]{0,127}$`
pub fn is_name(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= 128
        && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit())
        && b.iter()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || b"_.-".contains(c))
}

pub fn parse_request(params: &Value) -> Result<ActionRequest, RequestError> {
    let Some(obj) = params.as_object() else {
        return err("params must be an object");
    };
    let mut unknown: Vec<&str> = obj
        .keys()
        .map(String::as_str)
        .filter(|k| !ALLOWED_FIELDS.contains(k) && !IGNORED_FIELDS.contains(k))
        .collect();
    if !unknown.is_empty() {
        unknown.sort_unstable();
        return err(format!("unknown fields: {unknown:?}"));
    }

    let tool = match obj.get("tool").and_then(Value::as_str) {
        Some(t) if is_name(t) => t.to_owned(),
        _ => return err("'tool' must be a tool name such as org.vilna.mcp.files"),
    };
    let operation = match obj.get("operation").and_then(Value::as_str) {
        Some(o) if is_name(o) => o.to_owned(),
        _ => return err("'operation' must be an operation name"),
    };

    let resources = match obj.get("resources") {
        None => Vec::new(),
        Some(Value::Array(items)) => {
            if items.len() > MAX_RESOURCES {
                return err(format!("at most {MAX_RESOURCES} resources per request"));
            }
            items.iter().map(parse_resource).collect::<Result<_, _>>()?
        }
        Some(_) => return err("'resources' must be a list of URIs"),
    };

    let reason = match obj.get("reason").and_then(Value::as_str) {
        Some(r) if !r.trim().is_empty() => r,
        _ => return err("'reason' (an explanation for the user) is required"),
    };
    if reason.chars().count() > MAX_REASON {
        return err(format!("'reason' is longer than {MAX_REASON} characters"));
    }

    let user_turn = match obj.get("origin") {
        None => None,
        Some(Value::Object(origin)) => match origin.get("user_turn") {
            None => None,
            Some(v) => match v.as_u64() {
                Some(n) => Some(n),
                None => return err("'origin.user_turn' must be a non-negative integer"),
            },
        },
        Some(_) => return err("'origin' must be an object"),
    };

    let arguments = match obj.get("arguments") {
        None => Map::new(),
        Some(Value::Object(a)) => a.clone(),
        Some(_) => return err("'arguments' must be an object"),
    };

    Ok(ActionRequest {
        tool,
        operation,
        resources,
        reason: reason.trim().to_owned(),
        user_turn,
        arguments,
    })
}

pub fn parse_resource(uri: &Value) -> Result<Resource, RequestError> {
    let Some(uri) = uri.as_str() else {
        return err("each resource must be a non-empty URI string");
    };
    if uri.is_empty() || uri.chars().count() > MAX_URI {
        return err("each resource must be a non-empty URI string");
    }
    if uri.contains('\0') {
        return err("resource URI contains a NUL byte");
    }
    let Some((scheme, rest)) = split_scheme(uri) else {
        return err(format!("resource is not a URI: {uri:?}"));
    };
    let scheme = scheme.to_ascii_lowercase();
    if scheme != "file" {
        return Ok(Resource {
            uri: uri.to_owned(),
            scheme,
            path: None,
        });
    }
    let (netloc, path) = match rest.strip_prefix("//") {
        Some(after) => {
            let end = after.find(['/', '?', '#']).unwrap_or(after.len());
            (&after[..end], &after[end..])
        }
        None => ("", rest),
    };
    if !(netloc.is_empty() || netloc == "localhost") {
        return err(format!("file URI must be local: {uri:?}"));
    }
    let path = &path[..path.find(['?', '#']).unwrap_or(path.len())];
    let path = percent_decode(path);
    if path.contains('\0') || !path.starts_with('/') {
        return err(format!("file URI must hold an absolute path: {uri:?}"));
    }
    Ok(Resource {
        uri: uri.to_owned(),
        scheme,
        path: Some(normpath(&path)),
    })
}

/// `scheme ":" rest`, with the scheme rules of Python's `urlsplit`.
fn split_scheme(uri: &str) -> Option<(&str, &str)> {
    let colon = uri.find(':')?;
    let scheme = &uri[..colon];
    let mut chars = scheme.chars();
    let first = chars.next()?;
    if !first.is_ascii_alphabetic()
        || !chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
    {
        return None;
    }
    Some((scheme, &uri[colon + 1..]))
}

/// `%XX` decoding; invalid UTF-8 becomes U+FFFD, as in Python's `unquote`.
pub fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            if let (Some(h), Some(l)) = (hex(b[i + 1]), hex(b[i + 2])) {
                out.push(h * 16 + l);
                i += 3;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn hex(c: u8) -> Option<u8> {
    match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    }
}

/// Python's `posixpath.normpath` for absolute paths: folds `.`, `..` and
/// repeated slashes lexically; exactly two leading slashes are kept (POSIX).
pub fn normpath(path: &str) -> String {
    let lead = if path.starts_with("//") && !path.starts_with("///") {
        "//"
    } else if path.starts_with('/') {
        "/"
    } else {
        ""
    };
    let mut parts: Vec<&str> = Vec::new();
    for part in path.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                if parts.last().is_some_and(|p| *p != "..") {
                    parts.pop();
                } else if lead.is_empty() {
                    parts.push("..");
                }
            }
            p => parts.push(p),
        }
    }
    let joined = parts.join("/");
    let out = format!("{lead}{joined}");
    if out.is_empty() { ".".to_owned() } else { out }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ok(v: Value) -> ActionRequest {
        parse_request(&v).expect("valid request")
    }

    #[test]
    fn minimal_request() {
        let r = ok(json!({"tool": "org.vilna.mcp.files", "operation": "read", "reason": " why "}));
        assert_eq!(r.tool, "org.vilna.mcp.files");
        assert_eq!(r.reason, "why");
        assert!(r.resources.is_empty() && r.arguments.is_empty() && r.user_turn.is_none());
    }

    #[test]
    fn ignored_fields_are_accepted() {
        ok(json!({"tool": "a", "operation": "b", "reason": "r", "level": "R0", "tainted": false}));
    }

    #[test]
    fn file_resources_are_normalised() {
        let r = ok(json!({"tool": "a", "operation": "b", "reason": "r",
            "resources": ["file:///home/u/Docs/../x%20y.txt", "file://localhost/a//b/./c",
                          "https://example.org/x", "file:/a/b?q#f"]}));
        let paths: Vec<_> = r.resources.iter().map(|x| x.path.clone()).collect();
        assert_eq!(
            paths,
            vec![
                Some("/home/u/x y.txt".to_owned()),
                Some("/a/b/c".to_owned()),
                None,
                Some("/a/b".to_owned())
            ]
        );
        assert_eq!(r.resources[2].scheme, "https");
    }

    #[test]
    fn bad_requests() {
        for v in [
            json!([1]),
            json!({"tool": "a", "operation": "b"}),
            json!({"tool": "A", "operation": "b", "reason": "r"}),
            json!({"tool": "a", "operation": "b", "reason": "r", "x": 1}),
            json!({"tool": "a", "operation": "b", "reason": "r".repeat(501)}),
            json!({"tool": "a", "operation": "b", "reason": "r", "resources": ["file:rel"]}),
            json!({"tool": "a", "operation": "b", "reason": "r", "resources": ["file://h/a"]}),
            json!({"tool": "a", "operation": "b", "reason": "r", "resources": ["/no/scheme"]}),
            json!({"tool": "a", "operation": "b", "reason": "r", "resources": ["file:///a%00"]}),
            json!({"tool": "a", "operation": "b", "reason": "r", "origin": {"user_turn": 1.5}}),
            json!({"tool": "a", "operation": "b", "reason": "r", "origin": {"user_turn": true}}),
            json!({"tool": "a", "operation": "b", "reason": "r", "arguments": "x"}),
        ] {
            assert!(parse_request(&v).is_err(), "{v}");
        }
    }

    #[test]
    fn reason_length_counts_characters() {
        ok(json!({"tool": "a", "operation": "b", "reason": "ї".repeat(500)}));
    }

    #[test]
    fn normpath_like_python() {
        for (input, want) in [
            ("/", "/"),
            ("//a", "//a"),
            ("///a", "/a"),
            ("/a/../../b", "/b"),
            ("/a/./b/", "/a/b"),
            ("/a/b/..", "/a"),
        ] {
            assert_eq!(normpath(input), want, "{input}");
        }
    }

    #[test]
    fn names() {
        assert!(is_name("org.vilna.mcp.files") && is_name("0x") && is_name("a_b-c"));
        assert!(!is_name("") && !is_name("-a") && !is_name("Org") && !is_name(&"a".repeat(129)));
    }
}
