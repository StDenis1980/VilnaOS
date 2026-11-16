// SPDX-License-Identifier: GPL-3.0-or-later
//! The control socket of `vilna-reportd`: how the person's programs (the
//! "Feedback about Vilna" window, its crash notifications, Gloxi through
//! Herkos) read and send reports. One JSON request per line, one answer per
//! line. The kernel says who is asking (`SO_PEERCRED`): a person sees, sends
//! and discards only their own crashes and the system's, and asks to delete
//! only what they sent.
//!
//! | `op` | Arguments | Answer |
//! | --- | --- | --- |
//! | `status` | | `consent` (`auto`, `ask`, `never` or `null`), `waiting`, `sent_today` |
//! | `list` | | `reports`: id, program, what, state, found, signature |
//! | `show` | `id` | `report`: exactly what is or was sent, and the server's answer |
//! | `send` | `id` | `report` after sending |
//! | `discard` | `id` | `discarded` |
//! | `feedback` | `feedback`: `kind`, `text`, `text_en`, `system`, `crash_ids`, `contact`, `may_publish`, `channel` | `id` |
//! | `preview` | `feedback`, as above | `feedback`: exactly what `feedback` would send, nothing sent |
//! | `forget` | | `deleted`: how many the server deleted |
//! | `system` | | `system`: the block about this computer feedback may carry |
//!
//! A refusal is `{"error": "…"}` in plain words.

use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::Mutex;
use std::time::Duration;

use serde_json::{Value, json};

use crate::collector::Collector;
use crate::spool::{Report, State};

/// Requests are small: feedback is at most 8000 characters plus a little.
pub const MAX_REQUEST: u64 = 96 * 1024;
const IO_TIMEOUT: Duration = Duration::from_secs(10);

fn state(s: State) -> &'static str {
    match s {
        State::Waiting => "waiting",
        State::Sent => "sent",
        State::Refused => "refused",
    }
}

fn summary(r: &Report) -> Value {
    json!({
        "id": r.crash.id,
        "program": r.crash.program,
        "version": r.crash.version,
        "what": r.crash.title(),
        "state": state(r.state),
        "found": r.found,
        "signature": r.signature,
        "status": r.receipt.as_ref().map(|x| x.status.clone()),
        "fixed_in": r.receipt.as_ref().and_then(|x| x.fixed_in.clone()),
    })
}

fn detail(r: &Report) -> Value {
    let mut v = summary(r);
    if let Some(obj) = v.as_object_mut() {
        // Exactly the body that is or was sent.
        obj.insert("report".into(), r.crash.to_wire());
        obj.insert("refused".into(), json!(r.refused));
    }
    v
}

fn error(why: impl Into<String>) -> Value {
    json!({ "error": why.into() })
}

fn id_of(request: &Value) -> &str {
    request
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or_default()
}

/// One request from the person `uid`.
pub fn handle(c: &Collector, uid: u32, request: &Value, now: u64) -> Value {
    match request
        .get("op")
        .and_then(Value::as_str)
        .unwrap_or_default()
    {
        "status" => {
            let visible = c.visible(uid);
            json!({
                "consent": c.consent().map(|x| x.as_str()),
                "waiting": visible.iter().filter(|r| r.state == State::Waiting).count(),
                "sent_today": c.spool.sent_last_day(now),
            })
        }
        "list" => json!({ "reports": c.visible(uid).iter().map(summary).collect::<Vec<_>>() }),
        "show" => match c.get_for(uid, id_of(request)) {
            Ok(r) => detail(&r),
            Err(e) => error(e),
        },
        "send" => match c.get_for(uid, id_of(request)) {
            Ok(r) => match c.send(&r.crash.id, now) {
                Ok(r) => detail(&r),
                Err(e) => error(e),
            },
            Err(e) => error(e),
        },
        "discard" => match c.get_for(uid, id_of(request)) {
            Ok(r) if r.state == State::Sent => {
                error("this report was sent; to delete it on the server too, use forget")
            }
            Ok(r) => json!({ "discarded": c.spool.remove(&r.crash.id) }),
            Err(e) => error(e),
        },
        "feedback" => match request.get("feedback") {
            Some(body) => match c.feedback(uid, body, now) {
                Ok(id) => json!({ "id": id }),
                Err(e) => error(e),
            },
            None => error("feedback: give the feedback to send"),
        },
        "preview" => match request.get("feedback") {
            Some(body) => match c.feedback_body(body) {
                Ok((_, sent)) => json!({ "feedback": sent }),
                Err(e) => error(e),
            },
            None => error("feedback: give the feedback to preview"),
        },
        "forget" => match c.forget(uid) {
            Ok(n) => json!({ "deleted": n }),
            Err(e) => error(e),
        },
        "system" => json!({ "system": c.system_text() }),
        _ => error(
            "op must be status, list, show, send, discard, feedback, preview, forget or system",
        ),
    }
}

fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Answer one connection: line by line until it closes or is idle.
pub fn serve_one(c: &Mutex<Collector>, stream: UnixStream) {
    let Ok(cred) = rustix::net::sockopt::socket_peercred(&stream) else {
        return;
    };
    let uid = cred.uid.as_raw();
    let _ = stream.set_read_timeout(Some(IO_TIMEOUT));
    let _ = stream.set_write_timeout(Some(IO_TIMEOUT));
    let Ok(mut writer) = stream.try_clone() else {
        return;
    };
    let mut reader = BufReader::new(stream.take(MAX_REQUEST * 16));
    loop {
        let mut line = String::new();
        let mut limited = (&mut reader).take(MAX_REQUEST);
        match limited.read_line(&mut line) {
            Ok(0) | Err(_) => return,
            Ok(_) => {}
        }
        if !line.ends_with('\n') && line.len() as u64 >= MAX_REQUEST {
            let _ = writeln!(writer, "{}", error("the request is too long"));
            return;
        }
        let answer = match serde_json::from_str::<Value>(&line) {
            Ok(request) => match c.lock() {
                Ok(collector) => handle(&collector, uid, &request, now()),
                Err(_) => error("vilna-reportd is restarting; try again"),
            },
            Err(_) => error("each request is one line of JSON"),
        };
        if writeln!(writer, "{answer}").is_err() {
            return;
        }
    }
}

/// Serve the socket: one connection at a time, each bounded in time and size.
pub fn serve(c: &Mutex<Collector>, listener: &UnixListener) {
    for stream in listener.incoming().flatten() {
        serve_one(c, stream);
    }
}

/// The client side, for `vilna-reportd list` and the like.
pub fn ask(socket: &std::path::Path, request: &Value) -> Result<Value, String> {
    let mut stream = UnixStream::connect(socket)
        .map_err(|e| format!("vilna-reportd is not running ({}): {e}", socket.display()))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(60)));
    writeln!(stream, "{request}").map_err(|e| e.to_string())?;
    let mut line = String::new();
    BufReader::new(stream.take(MAX_REQUEST * 4))
        .read_line(&mut line)
        .map_err(|e| e.to_string())?;
    serde_json::from_str(&line).map_err(|e| format!("vilna-reportd answered nonsense: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::collector::tests::collector;
    use crate::consent::Consent;
    use crate::spool::tests::crash;

    fn waiting(c: &Collector, uid: u32) -> String {
        let r = Report::new(crash("gedit", &format!("f{uid}")), Some(uid), 10);
        c.spool.add(&r, 10).unwrap();
        r.crash.id
    }

    #[test]
    fn a_person_lists_shows_and_sends_their_own_reports() {
        let (c, fake) = collector("ctl", Some(Consent::Ask));
        let mine = waiting(&c, 1000);
        let theirs = waiting(&c, 1001);
        let status = handle(&c, 1000, &json!({"op": "status"}), 20);
        assert_eq!(status["consent"], json!("ask"));
        assert_eq!(status["waiting"], json!(1));
        let list = handle(&c, 1000, &json!({"op": "list"}), 20);
        assert_eq!(list["reports"].as_array().unwrap().len(), 1);
        assert_eq!(list["reports"][0]["id"], json!(mine));
        let shown = handle(&c, 1000, &json!({"op": "show", "id": mine}), 20);
        assert_eq!(shown["report"]["program"]["name"], json!("gedit"));
        assert!(handle(&c, 1000, &json!({"op": "show", "id": theirs}), 20)["error"].is_string());
        assert!(handle(&c, 1000, &json!({"op": "send", "id": theirs}), 20)["error"].is_string());
        let sent = handle(&c, 1000, &json!({"op": "send", "id": mine}), 20);
        assert_eq!(sent["state"], json!("sent"));
        assert_eq!(fake.posted.lock().unwrap().len(), 1);
        assert!(handle(&c, 1000, &json!({"op": "discard", "id": mine}), 20)["error"].is_string());
        assert_eq!(
            handle(&c, 1001, &json!({"op": "discard", "id": theirs}), 20)["discarded"],
            json!(true)
        );
    }

    #[test]
    fn a_preview_is_what_would_be_sent_and_sends_nothing() {
        let (c, fake) = collector("ctl-preview", None);
        let body = json!({"kind": "bug", "text": "Пишіть на anna@example.org", "contact": "anna@example.org"});
        let shown = handle(&c, 1000, &json!({"op": "preview", "feedback": body}), 1);
        assert_eq!(shown["feedback"]["text"], json!("Пишіть на <email>"));
        assert_eq!(shown["feedback"]["contact"], json!("anna@example.org"));
        assert_eq!(shown["feedback"]["channel"], json!("window"));
        assert!(fake.posted.lock().unwrap().is_empty());
        let bad = handle(
            &c,
            1000,
            &json!({"op": "preview", "feedback": {"text": ""}}),
            1,
        );
        assert!(bad["error"].is_string());
    }

    #[test]
    fn unknown_and_broken_requests_get_words_not_a_crash() {
        let (c, _) = collector("ctl-bad", None);
        for request in [
            json!({}),
            json!({"op": "nope"}),
            json!({"op": "show"}),
            json!({"op": "show", "id": "../../etc/passwd"}),
            json!({"op": "send", "id": 5}),
            json!({"op": "feedback"}),
            json!({"op": "feedback", "feedback": "text"}),
            json!([1, 2]),
        ] {
            assert!(
                handle(&c, 1000, &request, 1)["error"].is_string(),
                "{request}"
            );
        }
    }

    #[test]
    fn the_socket_answers_line_by_line() {
        let (c, _) = collector("ctl-sock", Some(Consent::Never));
        let path = c.root.join("control");
        let listener = UnixListener::bind(&path).unwrap();
        let c = std::sync::Arc::new(Mutex::new(c));
        let served = c.clone();
        std::thread::spawn(move || {
            if let Some(Ok(stream)) = listener.incoming().next() {
                serve_one(&served, stream);
            }
        });
        let answer = ask(&path, &json!({"op": "status"})).unwrap();
        assert_eq!(answer["consent"], json!("never"));
    }
}
