// SPDX-License-Identifier: GPL-3.0-or-later
//! What `vilna-reportd` does with crashes and feedback, without the socket
//! and the journal around it, so tests can drive it.

use std::path::PathBuf;

use serde_json::{Value, json};
use vilna_reports_protocol::{
    CrashReceipt, Feedback, FeedbackWire, MAX_SYSTEM_CHARS, report_id, truncate,
};

use crate::consent::{self, Consent};
use crate::coredump::{self, Device, Skip};
use crate::send::{Answer, Transport};
use crate::spool::{Added, MAX_SENT_PER_DAY, Report, Spool, State};

pub struct Collector {
    pub spool: Spool,
    pub transport: Box<dyn Transport>,
    /// Where `etc/vilna/reports.conf` is read from: `/` on a real system.
    pub root: PathBuf,
    pub device: Device,
}

/// Why a request could not be done, in words for the person.
pub type Refusal = String;

impl Collector {
    pub fn consent(&self) -> Option<Consent> {
        consent::read(&self.root)
    }

    /// Keep the crash of one journal entry. `Ok(None)`: nothing to keep.
    pub fn found(
        &self,
        entry: &serde_json::Map<String, Value>,
        now: u64,
    ) -> Result<Option<String>, Refusal> {
        let id = crate::device::new_id().ok_or("the system has no randomness for an id")?;
        let found = match coredump::report(entry, &self.device, &id) {
            Ok(found) => found,
            Err(Skip::NotACrash | Skip::OwnReporter(_)) => return Ok(None),
            Err(Skip::Invalid(why)) => return Err(why.to_string()),
        };
        let found_at = found.when_us.map(|us| us / 1_000_000).unwrap_or(now);
        let report = Report::new(found.crash, found.uid, found_at.min(now));
        match self.spool.add(&report, now).map_err(|e| e.to_string())? {
            Added::Added => Ok(Some(id)),
            Added::Duplicate => Ok(None),
        }
    }

    /// Send one waiting report: the person pressed "Send", or the answer is "auto".
    pub fn send(&self, id: &str, now: u64) -> Result<Report, Refusal> {
        let mut report = self.spool.get(id).ok_or("there is no such report")?;
        if report.state == State::Sent {
            return Ok(report);
        }
        if self.spool.sent_last_day(now) >= MAX_SENT_PER_DAY {
            return Err("20 reports were sent today already; this one waits until tomorrow".into());
        }
        let body = serde_json::to_vec(&report.crash.to_wire()).map_err(|e| e.to_string())?;
        match self.transport.post("/v1/crash", &body) {
            Answer::Taken(json) => {
                report.state = State::Sent;
                report.sent_at = Some(now);
                report.refused = None;
                report.receipt = serde_json::from_value::<CrashReceipt>(json).ok();
                self.spool
                    .record_sent(&report.crash.id, report.uid, "crash", now)
                    .map_err(|e| e.to_string())?;
                self.spool.save(&report).map_err(|e| e.to_string())?;
                Ok(report)
            }
            Answer::Refused(why) => {
                report.state = State::Refused;
                report.refused = Some(why.clone());
                self.spool.save(&report).map_err(|e| e.to_string())?;
                Err(format!("the server did not take the report: {why}"))
            }
            Answer::Later(why) => Err(format!("not sent now, it waits: {why}")),
        }
    }

    /// With the answer "auto", send what waits, oldest first, within the
    /// daily limit; stop at the first "later" (no network).
    pub fn send_waiting(&self, now: u64) -> usize {
        if self.consent() != Some(Consent::Auto) {
            return 0;
        }
        let mut waiting: Vec<Report> = self
            .spool
            .reports()
            .into_iter()
            .filter(|r| r.state == State::Waiting)
            .collect();
        waiting.reverse();
        let mut sent = 0;
        for r in waiting {
            match self.send(&r.crash.id, now) {
                Ok(_) => sent += 1,
                Err(_)
                    if self
                        .spool
                        .get(&r.crash.id)
                        .is_some_and(|r| r.state == State::Refused) => {}
                Err(_) => break,
            }
        }
        sent
    }

    /// Feedback as it would go to the server: the same checks and the same
    /// second scrub as there, with a new random id. The window shows this
    /// under "What will be sent", so the preview is the request.
    pub fn feedback_body(&self, body: &Value) -> Result<(String, Value), Refusal> {
        let mut wire: FeedbackWire =
            serde_json::from_value(body.clone()).map_err(|e| format!("feedback: {e}"))?;
        // The device makes the id, so the person can delete the feedback later.
        wire.id = None;
        wire.schema = Some(vilna_reports_protocol::SCHEMA);
        if !matches!(wire.channel.as_str(), "gloxi" | "window") {
            wire.channel = "window".into();
        }
        let id = crate::device::new_id().ok_or("the system has no randomness for an id")?;
        let feedback = Feedback::from_wire(wire, || id.clone()).map_err(|e| e.to_string())?;
        let mut sent = serde_json::to_value(&feedback).map_err(|e| e.to_string())?;
        if let (Some(contact), Some(obj)) = (&feedback.contact, sent.as_object_mut()) {
            obj.insert("contact".into(), json!(contact));
        }
        Ok((feedback.id, sent))
    }

    /// Feedback from the person `uid` ("Feedback about Vilna", or Gloxi
    /// through Herkos). The copy that stays here is what was sent.
    pub fn feedback(&self, uid: u32, body: &Value, now: u64) -> Result<String, Refusal> {
        let (id, sent) = self.feedback_body(body)?;
        let data = serde_json::to_vec(&sent).map_err(|e| e.to_string())?;
        match self.transport.post("/v1/feedback", &data) {
            Answer::Taken(_) => {
                self.spool
                    .record_sent(&id, Some(uid), "feedback", now)
                    .map_err(|e| e.to_string())?;
                let _ = self.spool.save_feedback(&id, &sent);
                Ok(id)
            }
            Answer::Refused(why) => Err(format!("the server did not take the feedback: {why}")),
            Answer::Later(why) => Err(format!("not sent: {why}. Try again later.")),
        }
    }

    /// Ask the server to delete everything this person sent (GDPR).
    pub fn forget(&self, uid: u32) -> Result<usize, Refusal> {
        let ids = self.spool.ids_of(uid);
        let mut deleted = 0;
        for chunk in ids.chunks(vilna_reports_protocol::MAX_FORGET_IDS) {
            let body = serde_json::to_vec(&json!({ "ids": chunk })).map_err(|e| e.to_string())?;
            match self.transport.post("/v1/forget", &body) {
                Answer::Taken(_) => {
                    self.spool.forgotten(chunk).map_err(|e| e.to_string())?;
                    deleted += chunk.len();
                }
                Answer::Refused(why) | Answer::Later(why) => {
                    return Err(format!("the server did not delete them now: {why}"));
                }
            }
        }
        Ok(deleted)
    }

    /// The block about the system that feedback may carry, if the person agrees.
    pub fn system_text(&self) -> String {
        let d = &self.device;
        let mut parts = Vec::new();
        if let Some(image) = &d.image {
            parts.push(image.clone());
        }
        if let Some(arch) = &d.arch {
            parts.push(arch.clone());
        }
        if let Some(gb) = d
            .memory_gb
            .and_then(vilna_reports_protocol::round_memory_gb)
        {
            parts.push(format!("{gb} GB"));
        }
        if let Some(locale) = &d.locale {
            parts.push(locale.clone());
        }
        truncate(&parts.join(", "), MAX_SYSTEM_CHARS)
    }

    pub fn visible(&self, uid: u32) -> Vec<Report> {
        self.spool
            .reports()
            .into_iter()
            .filter(|r| r.visible_to(uid))
            .collect()
    }

    pub fn get_for(&self, uid: u32, id: &str) -> Result<Report, Refusal> {
        report_id(id)
            .and_then(|id| self.spool.get(&id))
            .filter(|r| r.visible_to(uid))
            .ok_or_else(|| "there is no such report".into())
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use std::sync::Mutex;

    use super::*;
    use crate::spool::tests::{crash, temp_dir};

    #[derive(Default)]
    pub struct Fake {
        pub answers: Mutex<Vec<Answer>>,
        pub posted: Mutex<Vec<(String, Value)>>,
    }

    impl Transport for std::sync::Arc<Fake> {
        fn post(&self, path: &str, body: &[u8]) -> Answer {
            let json = serde_json::from_slice(body).unwrap_or_default();
            if let Ok(mut p) = self.posted.lock() {
                p.push((path.to_owned(), json));
            }
            self.answers
                .lock()
                .ok()
                .and_then(|mut a| (!a.is_empty()).then(|| a.remove(0)))
                .unwrap_or(Answer::Taken(
                    json!({"status": "new", "signature": "0", "id": "x"}),
                ))
        }
    }

    pub fn collector(name: &str, consent: Option<Consent>) -> (Collector, std::sync::Arc<Fake>) {
        let dir = temp_dir(name);
        if let Some(c) = consent {
            consent::write(&dir, c).unwrap();
        }
        let fake = std::sync::Arc::new(Fake::default());
        let c = Collector {
            spool: Spool::open(&dir.join("state")).unwrap(),
            transport: Box::new(fake.clone()),
            root: dir,
            device: Device {
                image: Some("Vilna Blavet 0.1".into()),
                locale: Some("uk".into()),
                arch: Some("x86_64".into()),
                memory_gb: Some(15),
            },
        };
        (c, fake)
    }

    fn waiting(c: &Collector, program: &str, function: &str) -> String {
        let r = Report::new(crash(program, function), Some(1000), 10);
        c.spool.add(&r, 10).unwrap();
        r.crash.id
    }

    #[test]
    fn nothing_is_sent_without_the_answer_auto() {
        for consent in [None, Some(Consent::Ask), Some(Consent::Never)] {
            let (c, fake) = collector("noauto", consent);
            waiting(&c, "gedit", "f");
            assert_eq!(c.send_waiting(100), 0);
            assert!(fake.posted.lock().unwrap().is_empty());
        }
    }

    #[test]
    fn with_auto_waiting_reports_go_and_are_kept_as_sent() {
        let (c, fake) = collector("auto", Some(Consent::Auto));
        let id = waiting(&c, "gedit", "f");
        assert_eq!(c.send_waiting(100), 1);
        let posted = fake.posted.lock().unwrap().clone();
        assert_eq!(posted[0].0, "/v1/crash");
        assert_eq!(posted[0].1["id"], json!(id));
        // The wire form the server reads, not the stored one.
        let wire: vilna_reports_protocol::CrashWire =
            serde_json::from_value(posted[0].1.clone()).unwrap();
        assert!(vilna_reports_protocol::Crash::from_wire(wire).is_ok());
        let r = c.spool.get(&id).unwrap();
        assert_eq!(r.state, State::Sent);
        assert_eq!(r.receipt.unwrap().status, "new");
        assert_eq!(c.spool.ids_of(1000), vec![id]);
    }

    #[test]
    fn no_network_keeps_reports_waiting_and_a_refusal_is_final() {
        let (c, fake) = collector("later", Some(Consent::Auto));
        let a = waiting(&c, "gedit", "f");
        fake.answers
            .lock()
            .unwrap()
            .push(Answer::Later("offline".into()));
        assert_eq!(c.send_waiting(100), 0);
        assert_eq!(c.spool.get(&a).unwrap().state, State::Waiting);
        fake.answers
            .lock()
            .unwrap()
            .push(Answer::Refused("schema".into()));
        assert_eq!(c.send_waiting(100), 0);
        assert_eq!(c.spool.get(&a).unwrap().state, State::Refused);
        assert_eq!(c.send_waiting(100), 0);
        assert_eq!(fake.posted.lock().unwrap().len(), 2);
    }

    #[test]
    fn at_most_twenty_a_day() {
        let (c, _fake) = collector("daily", Some(Consent::Auto));
        for i in 0..25 {
            waiting(&c, "gedit", &format!("f{i}"));
        }
        assert_eq!(c.send_waiting(100), MAX_SENT_PER_DAY);
        assert_eq!(c.send_waiting(200), 0);
        assert_eq!(c.send_waiting(100 + crate::spool::DAY), 5);
    }

    #[test]
    fn a_person_may_send_by_hand_whatever_the_answer() {
        let (c, fake) = collector("byhand", Some(Consent::Never));
        let id = waiting(&c, "gedit", "f");
        assert_eq!(c.send(&id, 100).unwrap().state, State::Sent);
        assert_eq!(fake.posted.lock().unwrap().len(), 1);
    }

    #[test]
    fn feedback_is_checked_sent_and_kept_with_its_own_id() {
        let (c, fake) = collector("feedback", None);
        let id = c
            .feedback(
                1000,
                &json!({"kind": "idea", "text": "Write to anna@example.org", "contact": "anna@example.org",
                        "id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "channel": "site"}),
                5,
            )
            .unwrap();
        assert_ne!(id, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee");
        let posted = fake.posted.lock().unwrap().clone();
        let body = &posted[0].1;
        assert_eq!(posted[0].0, "/v1/feedback");
        assert_eq!(body["channel"], json!("window"));
        assert_eq!(body["contact"], json!("anna@example.org"));
        assert_eq!(body["text"], json!("Write to <email>"));
        assert_eq!(c.spool.ids_of(1000), vec![id]);
        assert!(c.feedback(1000, &json!({"text": "  "}), 5).is_err());
        assert!(
            c.feedback(1000, &json!({"text": "x", "contact": "nope"}), 5)
                .is_err()
        );
    }

    #[test]
    fn the_installer_conversation_goes_only_when_given_and_scrubbed() {
        let (c, fake) = collector("conversation", None);
        let (_, preview) = c
            .feedback_body(&json!({"text": "x",
                "conversation": "[network] gloxi: Wi-Fi SkyNet; router 192.168.1.1"}))
            .unwrap();
        assert_eq!(
            preview["conversation"],
            json!("[network] gloxi: Wi-Fi SkyNet; router <ip>")
        );
        c.feedback(1000, &json!({"text": "x"}), 5).unwrap();
        let posted = fake.posted.lock().unwrap().clone();
        assert!(posted[0].1.get("conversation").is_none());
    }

    #[test]
    fn forget_asks_the_server_for_this_persons_ids_only() {
        let (c, fake) = collector("forget", None);
        c.spool
            .record_sent(
                "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                Some(1000),
                "feedback",
                1,
            )
            .unwrap();
        c.spool
            .record_sent(
                "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                Some(1001),
                "feedback",
                1,
            )
            .unwrap();
        assert_eq!(c.forget(1000).unwrap(), 1);
        let posted = fake.posted.lock().unwrap().clone();
        assert_eq!(
            posted[0].1,
            json!({"ids": ["aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"]})
        );
        assert_eq!(
            c.spool.ids_of(0),
            vec!["bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee".to_owned()]
        );
    }

    #[test]
    fn the_system_block_is_short_and_rounded() {
        let (c, _) = collector("system", None);
        assert_eq!(c.system_text(), "Vilna Blavet 0.1, x86_64, 16 GB, uk");
    }

    #[test]
    fn another_persons_report_is_not_there() {
        let (c, _) = collector("others", None);
        let id = waiting(&c, "gedit", "f");
        assert!(c.get_for(1000, &id).is_ok());
        assert!(c.get_for(1001, &id).is_err());
        assert!(c.visible(1001).is_empty());
    }
}
