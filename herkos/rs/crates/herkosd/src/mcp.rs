// SPDX-License-Identifier: GPL-3.0-or-later
//! Minimal MCP client over stdio: Herkos starts a tool server and calls it.
//! Mirrors `mcp_client.py`.
//!
//! Herkos, not Gloxi, owns the tool processes (permissions.md 9.5). One
//! request at a time per server; a server that dies, is slow or answers
//! nonsense is stopped and the call fails, which is a denial for Gloxi, never
//! a bypass. Herkos then starts a fresh server for the next request, at most
//! once per `RETRY_AFTER`; the broken one never answers again.

use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{Receiver, RecvTimeoutError, sync_channel};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use herkos_core::request::ActionRequest;
use serde_json::{Map, Value, json};

use crate::gate::Tools;

pub const MCP_PROTOCOL_VERSION: &str = "2025-06-18";
const MAX_RESPONSE: usize = 16 * 1024 * 1024;
pub const CALL_TIMEOUT: Duration = Duration::from_secs(60);
/// How often Herkos tries to start again a tool that stopped.
pub const RETRY_AFTER: Duration = Duration::from_secs(30);

enum Line {
    Message(Vec<u8>),
    TooLong,
}

struct Running {
    /// The server process, when this Herkos started it itself (else systemd did).
    child: Option<Child>,
    stdin: Box<dyn Write + Send>,
    lines: Receiver<Line>,
    next_id: u64,
}

impl Running {
    fn stop(self) {
        // End of input: a server stops by itself.
        drop(self.stdin);
        let Some(mut child) = self.child else {
            return;
        };
        for _ in 0..20 {
            if matches!(child.try_wait(), Ok(Some(_))) {
                return;
            }
            thread::sleep(Duration::from_millis(100));
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

pub struct McpStdio {
    name: String,
    timeout: Duration,
    state: Mutex<Option<Running>>,
    annotations: BTreeMap<String, Map<String, Value>>,
    fingerprints: BTreeMap<String, String>,
    entries: BTreeMap<String, Value>,
}

/// Read lines on a thread of their own, so a silent server cannot hang Herkos.
fn reader(stdout: impl Read + Send + 'static) -> Receiver<Line> {
    let (tx, rx) = sync_channel(4);
    thread::spawn(move || {
        let mut r = BufReader::new(stdout);
        loop {
            let mut line = Vec::new();
            match (&mut r)
                .take(MAX_RESPONSE as u64 + 1)
                .read_until(b'\n', &mut line)
            {
                Ok(0) | Err(_) => return,
                Ok(_) => {}
            }
            let msg = if line.len() > MAX_RESPONSE {
                Line::TooLong
            } else {
                Line::Message(line)
            };
            let too_long = matches!(msg, Line::TooLong);
            if tx.send(msg).is_err() || too_long {
                return;
            }
        }
    });
    rx
}

fn rpc(run: &mut Running, method: &str, params: Value, timeout: Duration) -> Result<Value, String> {
    run.next_id += 1;
    let want = run.next_id;
    send(
        run,
        &json!({"jsonrpc": "2.0", "id": want, "method": method, "params": params}),
    )?;
    loop {
        let line = match run.lines.recv_timeout(timeout) {
            Ok(Line::Message(l)) => l,
            Ok(Line::TooLong) => return Err("the tool's answer is too long".into()),
            Err(RecvTimeoutError::Timeout | RecvTimeoutError::Disconnected) => {
                return Err("the tool did not answer".into());
            }
        };
        let msg: Value = serde_json::from_slice(&line)
            .map_err(|_| "the tool's answer is not JSON".to_owned())?;
        if !msg.is_object() {
            return Err("the tool's answer is not a JSON-RPC message".into());
        }
        // Requests or notifications from the server are not supported: ignore.
        if msg.get("id") != Some(&json!(want)) {
            continue;
        }
        if let Some(err) = msg.get("error") {
            let text = err
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("error");
            return Err(format!("{method}: {text}"));
        }
        return Ok(msg.get("result").cloned().unwrap_or(Value::Null));
    }
}

fn send(run: &mut Running, msg: &Value) -> Result<(), String> {
    let mut line = serde_json::to_vec(msg).unwrap_or_default();
    line.push(b'\n');
    run.stdin
        .write_all(&line)
        .and_then(|()| run.stdin.flush())
        .map_err(|e| format!("the tool stopped: {e}"))
}

impl McpStdio {
    pub fn start(name: &str, command: &[String], timeout: Duration) -> Result<McpStdio, String> {
        let (program, args) = command.split_first().ok_or("empty tool command")?;
        let mut child = Command::new(program)
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("cannot start {program}: {e}"))?;
        let (Some(stdin), Some(stdout)) = (child.stdin.take(), child.stdout.take()) else {
            let _ = child.kill();
            return Err("no pipes to the tool".into());
        };
        let run = Running {
            child: Some(child),
            stdin: Box::new(stdin),
            lines: reader(stdout),
            next_id: 0,
        };
        McpStdio::setup(name, run, timeout)
    }

    /// A server that systemd started and connected to us (`StandardInput=file:`
    /// of a socket, see herkos/systemd/vilna-mcp-files@.service).
    pub fn over_socket(
        name: &str,
        stream: UnixStream,
        timeout: Duration,
    ) -> Result<McpStdio, String> {
        let reading = stream.try_clone().map_err(|e| e.to_string())?;
        let run = Running {
            child: None,
            stdin: Box::new(stream),
            lines: reader(reading),
            next_id: 0,
        };
        McpStdio::setup(name, run, timeout)
    }

    fn setup(name: &str, mut run: Running, timeout: Duration) -> Result<McpStdio, String> {
        let setup = (|| {
            let init = json!({
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "herkosd", "version": env!("CARGO_PKG_VERSION")},
            });
            if !rpc(&mut run, "initialize", init, timeout)?.is_object() {
                return Err("bad initialize answer".to_owned());
            }
            send(
                &mut run,
                &json!({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            )?;
            let tools = rpc(&mut run, "tools/list", json!({}), timeout)?;
            let mut annotations = BTreeMap::new();
            let mut fingerprints = BTreeMap::new();
            let mut entries = BTreeMap::new();
            for tool in tools
                .get("tools")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                if let Some(op) = tool.get("name").and_then(Value::as_str) {
                    let ann = tool
                        .get("annotations")
                        .and_then(Value::as_object)
                        .cloned()
                        .unwrap_or_default();
                    annotations.insert(op.to_owned(), ann);
                    fingerprints.insert(op.to_owned(), crate::pins::fingerprint(tool));
                    entries.insert(op.to_owned(), tool.clone());
                }
            }
            Ok((annotations, fingerprints, entries))
        })();
        match setup {
            Ok((annotations, fingerprints, entries)) => Ok(McpStdio {
                name: name.to_owned(),
                timeout,
                state: Mutex::new(Some(run)),
                annotations,
                fingerprints,
                entries,
            }),
            Err(e) => {
                run.stop();
                Err(e)
            }
        }
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn annotations(&self, operation: &str) -> Option<Map<String, Value>> {
        self.annotations.get(operation).cloned()
    }

    pub fn fingerprints(&self) -> BTreeMap<String, String> {
        self.fingerprints.clone()
    }

    pub fn entry(&self, operation: &str) -> Option<Value> {
        self.entries.get(operation).cloned()
    }

    /// Whether the server still answers: false once a call broke it.
    pub fn alive(&self) -> bool {
        self.state.lock().is_ok_and(|s| s.is_some())
    }

    pub fn execute(&self, request: &ActionRequest) -> Result<Value, String> {
        if !self.annotations.contains_key(&request.operation) {
            return Err(format!("the tool has no operation {}", request.operation));
        }
        let mut state = self
            .state
            .lock()
            .map_err(|_| "the tool is not running".to_owned())?;
        let Some(run) = state.as_mut() else {
            return Err("the tool is not running".into());
        };
        let params = json!({"name": request.operation, "arguments": request.arguments});
        let out = match rpc(run, "tools/call", params, self.timeout) {
            Ok(v) => v,
            Err(e) => {
                // A broken server stays stopped: no half-answered calls later.
                if let Some(r) = state.take() {
                    r.stop();
                }
                return Err(e);
            }
        };
        let Some(obj) = out.as_object() else {
            return Err("bad answer from the tool".into());
        };
        if obj.get("isError") == Some(&Value::Bool(true)) {
            let texts: Vec<&str> = obj
                .get("content")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(|c| c.get("text").and_then(Value::as_str))
                .filter(|t| !t.is_empty())
                .collect();
            return Err(if texts.is_empty() {
                "the tool failed".into()
            } else {
                texts.join("; ")
            });
        }
        if let Some(s) = obj.get("structuredContent") {
            return Ok(s.clone());
        }
        Ok(json!({"content": obj.get("content").cloned().unwrap_or_else(|| json!([]))}))
    }
}

impl Drop for McpStdio {
    fn drop(&mut self) {
        if let Some(r) = self.state.get_mut().ok().and_then(Option::take) {
            r.stop();
        }
    }
}

/// Starts a tool server again after the old one stopped.
pub type Relaunch = Box<dyn Fn() -> Result<McpStdio, String> + Send + Sync>;

struct Slot {
    current: Mutex<Arc<McpStdio>>,
    relaunch: Option<Relaunch>,
    /// When we last tried to start it again; held while we try.
    last_try: Mutex<Option<Instant>>,
}

/// The arguments a tool's `[launch]` asks for (ADR 0017), or `None` when
/// it needs allowed folders and there are none. Mirrors `launch_command` in
/// `herkos/src/herkos/cli.py`.
pub fn launch_args(
    launch: &herkos_core::manifest::Launch,
    backups: &std::path::Path,
    roots: &[std::path::PathBuf],
) -> Option<Vec<String>> {
    let mut args = Vec::new();
    if launch.wants("backup_dir") {
        args.extend(["--backup-dir".to_owned(), backups.display().to_string()]);
    }
    if launch.wants("roots") {
        if roots.is_empty() {
            return None;
        }
        for r in roots {
            args.extend(["--root".to_owned(), r.display().to_string()]);
        }
    }
    Some(args)
}

/// The tool servers this Herkos started, by tool name.
pub struct McpTools {
    tools: Vec<(String, Slot)>,
    retry_after: Duration,
}

impl Default for McpTools {
    fn default() -> McpTools {
        McpTools {
            tools: Vec::new(),
            retry_after: RETRY_AFTER,
        }
    }
}

impl McpTools {
    #[cfg(test)]
    pub fn add(&mut self, tool: McpStdio) {
        self.add_slot(tool, None);
    }

    /// A tool that is started again when it stops (killed, out of memory, broken).
    pub fn add_relaunching(&mut self, tool: McpStdio, relaunch: Relaunch) {
        self.add_slot(tool, Some(relaunch));
    }

    fn add_slot(&mut self, tool: McpStdio, relaunch: Option<Relaunch>) {
        let slot = Slot {
            current: Mutex::new(Arc::new(tool)),
            relaunch,
            last_try: Mutex::new(None),
        };
        self.tools.push((slot_name(&slot), slot));
    }

    fn get(&self, name: &str) -> Option<Arc<McpStdio>> {
        let (_, slot) = self.tools.iter().find(|(n, _)| n == name)?;
        let current = Arc::clone(&*slot.current.lock().ok()?);
        let Some(relaunch) = &slot.relaunch else {
            return Some(current);
        };
        if current.alive() {
            return Some(current);
        }
        let mut last_try = slot.last_try.lock().ok()?;
        // Another request may have started it while we waited for the lock.
        let now_current = Arc::clone(&*slot.current.lock().ok()?);
        if now_current.alive() {
            return Some(now_current);
        }
        if last_try.is_some_and(|t| t.elapsed() < self.retry_after) {
            return Some(current);
        }
        *last_try = Some(Instant::now());
        match relaunch() {
            Ok(fresh) => {
                eprintln!("herkosd: {name} started again");
                let fresh = Arc::new(fresh);
                *slot.current.lock().ok()? = Arc::clone(&fresh);
                Some(fresh)
            }
            Err(e) => {
                eprintln!("herkosd: {name} did not start again: {e}");
                Some(current)
            }
        }
    }
}

fn slot_name(slot: &Slot) -> String {
    slot.current
        .lock()
        .map(|t| t.name().to_owned())
        .unwrap_or_default()
}

impl Tools for McpTools {
    fn running(&self, tool: &str) -> bool {
        self.get(tool).is_some()
    }

    fn annotations(&self, tool: &str, operation: &str) -> Option<Map<String, Value>> {
        self.get(tool)?.annotations(operation)
    }

    fn fingerprints(&self, tool: &str) -> BTreeMap<String, String> {
        self.get(tool).map(|t| t.fingerprints()).unwrap_or_default()
    }

    fn entry(&self, tool: &str, operation: &str) -> Option<Value> {
        self.get(tool)?.entry(operation)
    }

    fn execute(&self, request: &ActionRequest) -> Result<Value, String> {
        match self.get(&request.tool) {
            Some(t) => t.execute(request),
            None => Err(format!("{} is not running", request.tool)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use herkos_core::request::parse_request;

    /// A tiny MCP server in sh: answers initialize, tools/list and one call.
    fn fake(script: &str) -> Vec<String> {
        vec!["sh".into(), "-c".into(), script.into()]
    }

    const SERVER: &str = r#"
        read l; echo '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}'
        read l
        read l; echo '{"jsonrpc":"2.0","method":"notifications/x"}'
        echo '{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"read","annotations":{"readOnlyHint":true}},{"name":"bad"}]}}'
        read l; echo '{"jsonrpc":"2.0","id":3,"result":{"structuredContent":{"text":"hi"}}}'
        read l; echo '{"jsonrpc":"2.0","id":4,"result":{"isError":true,"content":[{"type":"text","text":"no such file"}]}}'
        read l; sleep 5
    "#;

    fn req(op: &str) -> ActionRequest {
        parse_request(&json!({"tool": "t", "operation": op, "reason": "r"})).expect("valid")
    }

    #[test]
    fn talks_mcp() {
        let t = McpStdio::start("t", &fake(SERVER), Duration::from_secs(2)).expect("start");
        assert_eq!(
            t.annotations("read")
                .and_then(|a| a.get("readOnlyHint").cloned()),
            Some(json!(true))
        );
        assert_eq!(t.execute(&req("read")), Ok(json!({"text": "hi"})));
        assert_eq!(t.execute(&req("bad")), Err("no such file".into()));
        assert!(
            t.execute(&req("write"))
                .is_err_and(|e| e.contains("no operation"))
        );
        // The server goes silent: the call times out and the tool is stopped for good.
        assert_eq!(
            t.execute(&req("read")),
            Err("the tool did not answer".into())
        );
        assert!(!t.alive());
        assert_eq!(
            t.execute(&req("read")),
            Err("the tool is not running".into())
        );
    }

    /// Answers setup and one call, then exits.
    const ONE_CALL: &str = r#"
        read l; echo '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}'
        read l
        read l; echo '{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"read"}]}}'
        read l; echo '{"jsonrpc":"2.0","id":3,"result":{"structuredContent":{"n":1}}}'
    "#;

    #[test]
    fn a_stopped_tool_is_started_again() {
        let start = || McpStdio::start("t", &fake(ONE_CALL), Duration::from_secs(2));
        let starts = Arc::new(Mutex::new(0));
        let counted = Arc::clone(&starts);
        let mut tools = McpTools {
            retry_after: Duration::ZERO,
            ..McpTools::default()
        };
        tools.add_relaunching(
            start().expect("start"),
            Box::new(move || {
                *counted.lock().expect("count") += 1;
                start()
            }),
        );
        assert_eq!(tools.execute(&req("read")), Ok(json!({"n": 1})));
        // The server exited: this call fails, and it is not retried.
        assert!(tools.execute(&req("read")).is_err());
        assert_eq!(*starts.lock().expect("count"), 0);
        // The next request finds it stopped and gets a fresh server.
        assert!(tools.running("t"));
        assert_eq!(tools.execute(&req("read")), Ok(json!({"n": 1})));
        assert_eq!(*starts.lock().expect("count"), 1);

        // Without a relaunch, a stopped tool stays stopped; and not more
        // often than retry_after.
        let mut once = McpTools::default();
        once.add_relaunching(start().expect("start"), Box::new(|| Err("no".into())));
        assert!(once.execute(&req("read")).is_ok());
        assert!(once.execute(&req("read")).is_err());
        assert_eq!(
            once.execute(&req("read")),
            Err("the tool is not running".into())
        );
        let mut plain = McpTools::default();
        plain.add(start().expect("start"));
        assert!(plain.execute(&req("read")).is_ok());
        assert!(plain.execute(&req("read")).is_err());
        assert_eq!(
            plain.execute(&req("read")),
            Err("the tool is not running".into())
        );
    }

    #[test]
    fn refuses_broken_servers() {
        assert!(
            McpStdio::start("t", &fake("read l; echo nonsense"), Duration::from_secs(2)).is_err()
        );
        assert!(McpStdio::start("t", &fake("exit 0"), Duration::from_secs(2)).is_err());
        assert!(McpStdio::start("t", &["/no/such/tool".into()], Duration::from_secs(2)).is_err());
    }
}
