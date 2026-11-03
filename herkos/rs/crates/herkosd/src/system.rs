// SPDX-License-Identifier: GPL-3.0-or-later
//! The system service (step 6 of ADR 0013): one `herkosd` for everybody.
//!
//! It runs as the system user `herkos` and tells callers apart by the uid
//! the kernel reports for the socket (`SO_PEERCRED`). `/etc/herkos/system.toml`
//! lists the people and, for each, the system user their Gloxi runs as:
//!
//! ```toml
//! [[person]]
//! user = "denys"          # the person
//! gloxi = "gloxi-denys"   # the system user of their Gloxi
//! ```
//!
//! A connection from a person's Gloxi asks for actions on that person's
//! behalf; a connection from the person is their session agent
//! (`herkos-agent`), which shows Herkos's questions on their screen. Anybody
//! else gets no answer. Each person has their own permissions, journal and
//! sessions in `<state>/<uid>/`, so one person's "always" never covers another.

use std::collections::HashMap;
use std::io::{BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{Sender, channel};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde_json::{Value, json};
use toml::Table;

use crate::config::{Config, Owner};
use crate::gate::{Answer, Confirmer, Gate, Question, Tools};
use crate::passwd::{self, Account};
use crate::rpc;

pub const SYSTEM_CONFIG: &str = "/etc/herkos/system.toml";
pub const CONFIG: &str = "/etc/herkos/config.toml";
pub const STATE: &str = "/var/lib/herkos";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Person {
    pub account: Account,
    pub gloxi_uid: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct SystemConfig {
    pub people: Vec<Person>,
}

/// Who is on the other end of a connection.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role<'a> {
    /// A person's Gloxi: asks for actions for this person.
    Gloxi(&'a Person),
    /// The person themself: their session agent.
    Agent(&'a Person),
}

impl SystemConfig {
    pub fn role(&self, uid: u32) -> Option<Role<'_>> {
        if let Some(p) = self.people.iter().find(|p| p.gloxi_uid == uid) {
            return Some(Role::Gloxi(p));
        }
        self.people
            .iter()
            .find(|p| p.account.uid == uid)
            .map(Role::Agent)
    }
}

pub fn parse_system(
    text: &str,
    source: &Path,
    accounts: &[Account],
) -> Result<SystemConfig, String> {
    let at = source.display();
    let data: Table = text.parse().map_err(|e| format!("{at}: {e}"))?;
    if let Some(k) = data.keys().find(|k| k.as_str() != "person") {
        return Err(format!("{at}: unknown key {k}"));
    }
    let mut people: Vec<Person> = Vec::new();
    let entries = match data.get("person") {
        None => &Vec::new(),
        Some(toml::Value::Array(a)) => a,
        Some(_) => return Err(format!("{at}: person must be a list ([[person]])")),
    };
    for entry in entries {
        let t = entry
            .as_table()
            .ok_or_else(|| format!("{at}: each [[person]] is a table"))?;
        if let Some(k) = t.keys().find(|k| !["user", "gloxi"].contains(&k.as_str())) {
            return Err(format!("{at}: unknown key person.{k}"));
        }
        let field = |name: &str| {
            t.get(name)
                .and_then(toml::Value::as_str)
                .ok_or_else(|| format!("{at}: person.{name} must be a user name"))
        };
        let (user, gloxi) = (field("user")?, field("gloxi")?);
        let account = passwd::find(accounts, user)
            .ok_or_else(|| format!("{at}: no such user {user}"))?
            .clone();
        let gloxi_uid = passwd::find(accounts, gloxi)
            .ok_or_else(|| format!("{at}: no such user {gloxi}"))?
            .uid;
        if account.uid == 0 || gloxi_uid == 0 {
            return Err(format!("{at}: root is neither a person nor a Gloxi here"));
        }
        if account.uid == gloxi_uid {
            return Err(format!("{at}: {user}'s Gloxi must be a user of its own"));
        }
        let all: Vec<u32> = people
            .iter()
            .flat_map(|p| [p.account.uid, p.gloxi_uid])
            .collect();
        if all.contains(&account.uid) || all.contains(&gloxi_uid) {
            return Err(format!("{at}: {user} or {gloxi} is listed twice"));
        }
        people.push(Person { account, gloxi_uid });
    }
    Ok(SystemConfig { people })
}

pub fn load_system(path: &Path, accounts: &[Account]) -> Result<SystemConfig, String> {
    match std::fs::read_to_string(path) {
        Ok(text) => parse_system(&text, path, accounts),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(SystemConfig::default()),
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}

/// The session agent of one person, while it is connected.
struct Link {
    serial: u64,
    writer: Arc<Mutex<UnixStream>>,
    pending: Arc<Mutex<HashMap<String, Sender<Value>>>>,
}

/// The registered session agents, one per person (the latest wins: a new
/// login replaces the agent of the old one).
#[derive(Default)]
pub struct Agents {
    links: Mutex<HashMap<u32, Link>>,
    next: AtomicU64,
}

fn send(writer: &Mutex<UnixStream>, msg: &Value) -> bool {
    let Ok(mut w) = writer.lock() else {
        return false;
    };
    w.write_all(&crate::encode(msg)).is_ok()
}

impl Agents {
    fn register(
        &self,
        uid: u32,
        writer: Arc<Mutex<UnixStream>>,
        pending: Arc<Mutex<HashMap<String, Sender<Value>>>>,
    ) -> u64 {
        let serial = self.next.fetch_add(1, Ordering::Relaxed);
        if let Ok(mut links) = self.links.lock() {
            links.insert(
                uid,
                Link {
                    serial,
                    writer,
                    pending,
                },
            );
        }
        serial
    }

    fn unregister(&self, uid: u32, serial: u64) {
        if let Ok(mut links) = self.links.lock() {
            if links.get(&uid).is_some_and(|l| l.serial == serial) {
                links.remove(&uid);
            }
        }
    }

    /// Show the question on the person's screen and wait for the answer.
    /// No agent, a broken one, silence or an answer that was not offered: "no".
    pub fn ask(&self, uid: u32, question: &Question, timeout: f64) -> Answer {
        let (writer, pending) = match self.links.lock() {
            Ok(links) => match links.get(&uid) {
                Some(l) => (Arc::clone(&l.writer), Arc::clone(&l.pending)),
                None => return Answer::Deny,
            },
            Err(_) => return Answer::Deny,
        };
        let id = format!("ask-{}", self.next.fetch_add(1, Ordering::Relaxed));
        let (tx, rx) = channel();
        if let Ok(mut p) = pending.lock() {
            p.insert(id.clone(), tx);
        }
        let request = json!({"jsonrpc": "2.0", "id": id, "method": "herkos.agent.ask", "params": question.to_json()});
        let reply = if send(&writer, &request) {
            rx.recv_timeout(Duration::from_secs_f64(timeout.max(0.0)))
                .ok()
        } else {
            None
        };
        if let Ok(mut p) = pending.lock() {
            p.remove(&id);
        }
        let Some(reply) = reply else {
            // Take the window away: the answer no longer counts.
            let cancel =
                json!({"jsonrpc": "2.0", "method": "herkos.agent.cancel", "params": {"id": id}});
            send(&writer, &cancel);
            return Answer::Deny;
        };
        let answer = reply
            .get("result")
            .and_then(|r| r.get("answer"))
            .and_then(Value::as_str)
            .and_then(Answer::parse);
        match answer {
            Some(a) if question.choices.contains(&a) => a,
            _ => Answer::Deny,
        }
    }
}

/// Ask a person through their session agent.
pub struct AgentConfirmer {
    pub uid: u32,
    pub agents: Arc<Agents>,
}

impl Confirmer for AgentConfirmer {
    fn ask(&self, question: &Question, timeout: f64) -> Answer {
        self.agents.ask(self.uid, question, timeout)
    }
}

/// Builds the tools a person's Gloxi may use.
pub type ToolsFor = dyn Fn(&Config, &Person) -> Box<dyn Tools> + Send + Sync;

pub struct System {
    pub config: SystemConfig,
    pub config_path: PathBuf,
    pub state_root: PathBuf,
    pub agents: Arc<Agents>,
    pub tools: Box<ToolsFor>,
    gates: Mutex<HashMap<u32, Arc<Gate>>>,
}

impl System {
    pub fn new(
        config: SystemConfig,
        config_path: PathBuf,
        state_root: PathBuf,
        tools: Box<ToolsFor>,
    ) -> System {
        System {
            config,
            config_path,
            state_root,
            agents: Arc::new(Agents::default()),
            tools,
            gates: Mutex::new(HashMap::new()),
        }
    }

    pub fn owner(&self, person: &Person) -> Owner {
        Owner {
            home: person.account.home.clone(),
            state_dir: self.state_root.join(person.account.uid.to_string()),
        }
    }

    /// The person's own gate, made the first time their Gloxi connects.
    fn gate(&self, person: &Person) -> Result<Arc<Gate>, String> {
        let uid = person.account.uid;
        let mut gates = self.gates.lock().map_err(|_| "gates poisoned".to_owned())?;
        if let Some(g) = gates.get(&uid) {
            return Ok(Arc::clone(g));
        }
        let mut config = crate::config::load_config_for(&self.config_path, &self.owner(person))?;
        // The system Herkos isolates Gloxi from the person: it carries out
        // R3-R4 too, asking every time (ADR 0013, step 7).
        config.max_level = herkos_core::risk::Level::R4;
        let tools = (self.tools)(&config, person);
        let confirmer = AgentConfirmer {
            uid,
            agents: Arc::clone(&self.agents),
        };
        let gate = Arc::new(crate::build_gate_from(config, tools, Box::new(confirmer))?);
        let start = json!({
            "server": concat!("herkosd/", env!("CARGO_PKG_VERSION")),
            "mode": "system",
            "person": person.account.name,
        });
        gate.journal()
            .append("start", start, None)
            .map_err(|e| e.0)?;
        gates.insert(uid, Arc::clone(&gate));
        Ok(gate)
    }
}

pub fn serve(listener: &UnixListener, system: Arc<System>) {
    for stream in listener.incoming() {
        match stream {
            Ok(s) => {
                let system = Arc::clone(&system);
                thread::spawn(move || connection(s, &system));
            }
            Err(e) => eprintln!("herkosd: accept: {e}"),
        }
    }
}

fn connection(stream: UnixStream, system: &System) {
    let Ok(cred) = rustix::net::sockopt::socket_peercred(&stream) else {
        return;
    };
    match system.config.role(cred.uid.as_raw()) {
        Some(Role::Gloxi(person)) => match system.gate(person) {
            Ok(gate) => crate::serve_connection(stream, gate),
            Err(e) => eprintln!("herkosd: {}: {e}", person.account.name),
        },
        Some(Role::Agent(person)) => agent(stream, person.account.uid, &system.agents),
        // Not one of ours: no answer at all.
        None => {}
    }
}

/// A session agent's connection: it says hello, registers, then answers
/// `herkos.agent.ask` requests. It cannot ask for actions.
fn agent(stream: UnixStream, uid: u32, agents: &Agents) {
    let Ok(w) = stream.try_clone() else {
        return;
    };
    let writer = Arc::new(Mutex::new(w));
    let pending: Arc<Mutex<HashMap<String, Sender<Value>>>> = Arc::default();
    let mut reader = BufReader::new(stream);
    let mut hello = false;
    let mut serial = None;
    loop {
        let msg = match crate::read_message(&mut reader) {
            crate::Message::End => break,
            crate::Message::TooLong => {
                let e = rpc::error(&Value::Null, rpc::INVALID_REQUEST, "message too long");
                send(&writer, &e);
                break;
            }
            crate::Message::Bad => {
                send(
                    &writer,
                    &rpc::error(&Value::Null, rpc::PARSE_ERROR, "not valid JSON"),
                );
                continue;
            }
            crate::Message::Json(v) => v,
        };
        // An answer to one of our questions.
        if msg.get("method").is_none() {
            if let Some(id) = msg.get("id").and_then(Value::as_str) {
                let waiting = pending.lock().ok().and_then(|mut p| p.remove(id));
                if let Some(tx) = waiting {
                    let _ = tx.send(msg);
                }
            }
            continue;
        }
        let Some(id) = msg.get("id").cloned() else {
            continue; // notifications from the agent mean nothing yet
        };
        let method = msg.get("method").and_then(Value::as_str).unwrap_or("");
        let reply = match method {
            "herkos.hello" => {
                let speaks = match msg.get("params").and_then(|p| p.get("protocol")) {
                    Some(Value::String(v)) => v == herkos_core::PROTOCOL_VERSION,
                    Some(Value::Array(vs)) => vs.iter().any(|v| v == herkos_core::PROTOCOL_VERSION),
                    _ => false,
                };
                if speaks {
                    hello = true;
                    rpc::result(
                        &id,
                        json!({"protocol": herkos_core::PROTOCOL_VERSION, "server": concat!("herkosd/", env!("CARGO_PKG_VERSION")), "role": "agent"}),
                    )
                } else {
                    rpc::error(
                        &id,
                        rpc::PROTOCOL_MISMATCH,
                        &format!(
                            "this Herkos speaks protocol {}",
                            herkos_core::PROTOCOL_VERSION
                        ),
                    )
                }
            }
            _ if !hello => rpc::error(&id, rpc::HELLO_REQUIRED, "call herkos.hello first"),
            "herkos.agent.register" => {
                if serial.is_none() {
                    serial = Some(agents.register(uid, Arc::clone(&writer), Arc::clone(&pending)));
                }
                rpc::result(&id, json!({"registered": true}))
            }
            _ => rpc::error(
                &id,
                rpc::METHOD_NOT_FOUND,
                &format!("unknown method {method}"),
            ),
        };
        if !send(&writer, &reply) {
            break;
        }
    }
    if let Some(s) = serial {
        agents.unregister(uid, s);
    }
    // Questions still waiting get "no" now, not at their timeout.
    if let Ok(mut p) = pending.lock() {
        p.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn accounts() -> Vec<Account> {
        passwd::parse(
            "root:x:0:0::/root:/bin/sh\nherkos:x:990:990::/var/lib/herkos:/sbin/nologin\n\
             gloxi-a:x:991:991::/var/lib/gloxi:/sbin/nologin\ngloxi-b:x:992:992::/var/lib/gloxi:/sbin/nologin\n\
             anna:x:1000:1000::/home/anna:/bin/sh\nbohdan:x:1001:1001::/home/bohdan:/bin/sh\n",
        )
    }

    fn parse(text: &str) -> Result<SystemConfig, String> {
        parse_system(text, Path::new("system.toml"), &accounts())
    }

    #[test]
    fn people_and_roles() {
        let s = parse(
            "[[person]]\nuser = \"anna\"\ngloxi = \"gloxi-a\"\n[[person]]\nuser = \"1001\"\ngloxi = \"gloxi-b\"\n",
        )
        .expect("valid");
        assert_eq!(s.people.len(), 2);
        let name = |r: Option<Role<'_>>| match r {
            Some(Role::Gloxi(p)) => format!("gloxi of {}", p.account.name),
            Some(Role::Agent(p)) => format!("agent of {}", p.account.name),
            None => "nobody".into(),
        };
        assert_eq!(name(s.role(991)), "gloxi of anna");
        assert_eq!(name(s.role(1000)), "agent of anna");
        assert_eq!(name(s.role(992)), "gloxi of bohdan");
        assert_eq!(name(s.role(1001)), "agent of bohdan");
        assert_eq!(name(s.role(990)), "nobody");
        assert_eq!(name(s.role(0)), "nobody");
        assert_eq!(parse("").expect("empty"), SystemConfig::default());
    }

    #[test]
    fn bad_system_configs() {
        for text in [
            "what = 1",
            "person = 1",
            "[[person]]\nuser = \"anna\"",
            "[[person]]\nuser = \"anna\"\ngloxi = \"nobody\"",
            "[[person]]\nuser = \"root\"\ngloxi = \"gloxi-a\"",
            "[[person]]\nuser = \"anna\"\ngloxi = \"anna\"",
            "[[person]]\nuser = \"anna\"\ngloxi = \"gloxi-a\"\nextra = 1",
            "[[person]]\nuser = \"anna\"\ngloxi = \"gloxi-a\"\n[[person]]\nuser = \"bohdan\"\ngloxi = \"gloxi-a\"",
            "[[person]]\nuser = \"anna\"\ngloxi = \"gloxi-a\"\n[[person]]\nuser = \"gloxi-a\"\ngloxi = \"gloxi-b\"",
        ] {
            assert!(parse(text).is_err(), "{text}");
        }
    }

    use crate::gate::Answer;
    use herkos_core::risk::Level;
    use std::io::BufRead;

    fn question(choices: Vec<Answer>) -> Question {
        Question {
            summary: "Read a file".into(),
            reason: "you asked".into(),
            tool: "org.vilna.mcp.files".into(),
            operation: "read".into(),
            resources: vec!["file:///home/anna/Documents/a".into()],
            level: Level::R1,
            undoable: true,
            raises: Vec::new(),
            taint_sources: Vec::new(),
            choices,
            content: None,
        }
    }

    /// A session agent on the other end of a socket pair, registered for uid 1000.
    struct FakeAgent {
        reader: std::io::BufReader<UnixStream>,
        writer: UnixStream,
    }

    impl FakeAgent {
        fn start(agents: &Arc<Agents>) -> FakeAgent {
            let (ours, theirs) = UnixStream::pair().expect("pair");
            let a = Arc::clone(agents);
            thread::spawn(move || agent(theirs, 1000, &a));
            let mut fake = FakeAgent {
                reader: std::io::BufReader::new(ours.try_clone().expect("clone")),
                writer: ours,
            };
            fake.send(&json!({"jsonrpc": "2.0", "id": 1, "method": "herkos.agent.register"}));
            assert_eq!(fake.read()["error"]["code"], rpc::HELLO_REQUIRED);
            fake.send(&json!({"jsonrpc": "2.0", "id": 2, "method": "herkos.hello", "params": {"protocol": herkos_core::PROTOCOL_VERSION}}));
            assert_eq!(fake.read()["result"]["role"], "agent");
            // An agent cannot ask for actions.
            fake.send(&json!({"jsonrpc": "2.0", "id": 3, "method": "herkos.request_action", "params": {}}));
            assert_eq!(fake.read()["error"]["code"], rpc::METHOD_NOT_FOUND);
            fake.send(&json!({"jsonrpc": "2.0", "id": 4, "method": "herkos.agent.register"}));
            assert_eq!(fake.read()["result"]["registered"], true);
            fake
        }

        fn send(&mut self, msg: &Value) {
            self.writer.write_all(&crate::encode(msg)).expect("send");
        }

        fn read(&mut self) -> Value {
            let mut line = String::new();
            self.reader.read_line(&mut line).expect("read");
            serde_json::from_str(&line).expect("json")
        }

        /// Wait for a question and give this answer.
        fn answer(mut self, answer: &'static str) -> thread::JoinHandle<Value> {
            thread::spawn(move || {
                let q = self.read();
                let id = q["id"].clone();
                self.send(&json!({"jsonrpc": "2.0", "id": id, "result": {"answer": answer}}));
                let _keep = self.writer.try_clone();
                q
            })
        }
    }

    #[test]
    fn asks_the_person_through_their_agent() {
        let agents = Arc::new(Agents::default());
        let all = || vec![Answer::Once, Answer::Session, Answer::Always];
        // No agent yet: "no".
        assert_eq!(agents.ask(1000, &question(all()), 1.0), Answer::Deny);

        let seen = FakeAgent::start(&agents).answer("session");
        assert_eq!(agents.ask(1000, &question(all()), 5.0), Answer::Session);
        let q = seen.join().expect("agent");
        assert_eq!(q["method"], "herkos.agent.ask");
        assert_eq!(q["params"]["summary"], "Read a file");
        // Another person has no agent: their questions are not shown here.
        assert_eq!(agents.ask(1001, &question(all()), 1.0), Answer::Deny);

        // Not offered: a "no".
        let seen = FakeAgent::start(&agents).answer("always");
        assert_eq!(
            agents.ask(1000, &question(vec![Answer::Once]), 5.0),
            Answer::Deny
        );
        seen.join().expect("agent");
        let seen = FakeAgent::start(&agents).answer("yes");
        assert_eq!(agents.ask(1000, &question(all()), 5.0), Answer::Deny);
        seen.join().expect("agent");
    }

    #[test]
    fn silence_and_disconnects_are_no() {
        let agents = Arc::new(Agents::default());
        let mut fake = FakeAgent::start(&agents);
        let start = std::time::Instant::now();
        assert_eq!(
            agents.ask(1000, &question(vec![Answer::Once]), 0.5),
            Answer::Deny
        );
        assert!(start.elapsed() < Duration::from_secs(3));
        let ask = fake.read();
        let cancel = fake.read();
        assert_eq!(cancel["method"], "herkos.agent.cancel");
        assert_eq!(cancel["params"]["id"], ask["id"]);
        // A late answer changes nothing.
        fake.send(&json!({"jsonrpc": "2.0", "id": ask["id"], "result": {"answer": "once"}}));

        // The agent goes away while a question waits.
        let waiting = {
            let a = Arc::clone(&agents);
            thread::spawn(move || a.ask(1000, &question(vec![Answer::Once]), 10.0))
        };
        let _ = fake.read();
        drop(fake);
        assert_eq!(waiting.join().expect("ask"), Answer::Deny);
        let start = std::time::Instant::now();
        while agents.links.lock().is_ok_and(|l| l.contains_key(&1000)) {
            assert!(
                start.elapsed() < Duration::from_secs(3),
                "agent still registered"
            );
            thread::sleep(Duration::from_millis(20));
        }
    }
}
