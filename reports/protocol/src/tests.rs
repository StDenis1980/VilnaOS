// SPDX-License-Identifier: GPL-3.0-or-later
use super::*;

const ID: &str = "3f2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9b";
const NEW_ID: &str = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";

fn crash_json(extra: serde_json::Value) -> CrashWire {
    let mut base = serde_json::json!({
        "schema": 1,
        "id": ID,
        "program": {"name": "gloxi", "version": "0.2.0", "packaging": "flatpak"},
        "kind": "exception",
        "exception": "KeyError",
        "frames": [
            {"file": "gloxi/chat.py", "line": 120, "function": "answer"},
            {"file": "gloxi/router.py", "line": 44, "function": "route"}
        ],
        "locale": "uk",
        "arch": "x86_64",
        "memory_gb": 12
    });
    if let (Some(base), Some(extra)) = (base.as_object_mut(), extra.as_object()) {
        for (k, v) in extra {
            base.insert(k.clone(), v.clone());
        }
    }
    serde_json::from_value(base).unwrap()
}

#[test]
fn every_constant_pattern_compiles() {
    for p in [
        &UUID_V4,
        &PROGRAM,
        &VERSION,
        &IMAGE,
        &SIGNAL,
        &EXCEPTION,
        &ARCH,
        &LOCALE,
        &MODULE,
        &BUILD_ID,
        &OFFSET,
        &SOURCE_FILE,
        &EMAIL,
        &SCRUB_EMAIL,
        &SCRUB_IPV4,
        &SCRUB_IPV6,
        &SCRUB_MAC,
        &SCRUB_DIGITS,
        &RUST_PLACE,
        &LINE_NUMBERS,
    ] {
        assert!(p.is_some());
    }
}

#[test]
fn scrub_hides_personal_data_and_keeps_our_places() {
    let s = scrub(
        "mail anna.k@example.org from 192.168.1.20 mac 00:1a:2b:3c:4d:5e \
         file /home/anna/Документи/лист.odt or C:\\Users\\anna id 380501234567 \
         at crates/herkosd/src/gate.rs:12:5 in gloxi/chat.py",
        1000,
    );
    assert!(!s.contains("anna"), "{s}");
    assert!(!s.contains("192.168"), "{s}");
    assert!(!s.contains("4d:5e"), "{s}");
    assert!(!s.contains("380501234567"), "{s}");
    assert!(
        s.contains("<email>") && s.contains("<ip>") && s.contains("<mac>"),
        "{s}"
    );
    assert!(s.contains("<path>") && s.contains("<n>"), "{s}");
    assert!(s.contains("crates/herkosd/src/gate.rs:12:5"), "{s}");
    assert!(s.contains("gloxi/chat.py"), "{s}");
}

#[test]
fn scrub_cuts_at_a_character_boundary() {
    let s = scrub(&"ї".repeat(50), 10);
    assert_eq!(s.chars().count(), 10);
}

#[test]
fn a_good_report_is_kept_and_rounded() {
    let c = Crash::from_wire(crash_json(serde_json::json!({}))).unwrap();
    assert_eq!(c.id, ID);
    assert_eq!(c.program, "gloxi");
    assert_eq!(c.packaging, Packaging::Flatpak);
    assert_eq!(c.kind, CrashKind::Exception);
    assert_eq!(c.exception.as_deref(), Some("KeyError"));
    assert_eq!(c.memory_gb, Some(16));
    assert_eq!(c.frames.len(), 2);
    assert_eq!(c.title(), "gloxi: KeyError in gloxi/chat.py!answer");
}

#[test]
fn required_fields_are_refused_with_a_reason() {
    for (extra, field) in [
        (serde_json::json!({"schema": 2}), "schema"),
        (serde_json::json!({"id": "1"}), "id"),
        (
            serde_json::json!({"id": "3f2b8c1e-9a4d-1e7f-8b21-0c5d6e7f8a9b"}),
            "id",
        ),
        (
            serde_json::json!({"program": {"name": "../etc/passwd"}}),
            "program.name",
        ),
        (serde_json::json!({"kind": "telemetry"}), "kind"),
    ] {
        assert_eq!(
            Crash::from_wire(crash_json(extra)).unwrap_err().field,
            field
        );
    }
}

#[test]
fn bad_optional_fields_are_dropped_not_refused() {
    let c = Crash::from_wire(crash_json(serde_json::json!({
        "locale": "uk; drop table",
        "arch": "X86 64",
        "memory_gb": 0,
        "signal": "SIGSEGV",
        "frames": [
            {"module": "/usr/lib64/libgtk-4.so.1", "build_id": "ABCDEF0123456789", "offset": "0x1F"},
            {"file": "../../etc/shadow"},
            {}
        ]
    })))
    .unwrap();
    assert_eq!(c.locale, None);
    assert_eq!(c.arch, None);
    assert_eq!(c.memory_gb, None);
    // A signal on an exception report does not belong there.
    assert_eq!(c.signal, None);
    // A module with a path loses the module but keeps what is fine.
    assert_eq!(
        c.frames,
        vec![Frame {
            build_id: Some("abcdef0123456789".into()),
            offset: Some("0x1f".into()),
            ..Frame::default()
        }]
    );
}

#[test]
fn frames_are_capped() {
    let frames: Vec<_> = (0..500)
        .map(|i| serde_json::json!({"file": "gloxi/x.py", "line": i + 1}))
        .collect();
    let c = Crash::from_wire(crash_json(serde_json::json!({ "frames": frames }))).unwrap();
    assert_eq!(c.frames.len(), MAX_FRAMES);
}

#[test]
fn the_signature_ignores_versions_lines_and_deep_frames() {
    let a = Crash::from_wire(crash_json(serde_json::json!({}))).unwrap();
    let b = Crash::from_wire(crash_json(serde_json::json!({
        "id": "0b2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9b",
        "program": {"name": "gloxi", "version": "0.2.1"},
        "frames": [
            {"file": "gloxi/chat.py", "line": 133, "function": "answer"},
            {"file": "gloxi/router.py", "line": 47, "function": "route"}
        ]
    })))
    .unwrap();
    assert_eq!(a.signature(), b.signature());
    assert_eq!(a.signature().len(), 16);
    let other =
        Crash::from_wire(crash_json(serde_json::json!({"exception": "ValueError"}))).unwrap();
    assert_ne!(a.signature(), other.signature());
}

#[test]
fn a_panic_keeps_its_place_without_the_line_in_the_signature() {
    let wire = |line: u32| {
        crash_json(serde_json::json!({
            "program": {"name": "herkosd"}, "kind": "panic",
            "location": format!("/build/src/crates/herkosd/src/gate.rs:{line}:9"), "frames": []
        }))
    };
    let a = Crash::from_wire(wire(10)).unwrap();
    let b = Crash::from_wire(wire(11)).unwrap();
    assert_eq!(
        a.location.as_deref(),
        Some("crates/herkosd/src/gate.rs:10:9")
    );
    assert_eq!(a.signature(), b.signature());
}

fn feedback(extra: serde_json::Value) -> Result<Feedback, Invalid> {
    let mut base = serde_json::json!({
        "schema": 1, "id": ID, "kind": "bug", "channel": "gloxi",
        "text": "Копії зупинились на 40 %", "locale": "uk"
    });
    if let (Some(base), Some(extra)) = (base.as_object_mut(), extra.as_object()) {
        for (k, v) in extra {
            base.insert(k.clone(), v.clone());
        }
    }
    Feedback::from_wire(serde_json::from_value(base).unwrap(), || NEW_ID.into())
}

#[test]
fn feedback_is_checked_and_scrubbed() {
    let f = feedback(serde_json::json!({
        "text": "Не працює, пишіть на taras@example.com, тел. 0501234567",
        "contact": "taras@example.com",
        "crash_ids": [ID]
    }))
    .unwrap();
    assert_eq!(f.kind, FeedbackKind::Bug);
    assert_eq!(f.channel, Channel::Gloxi);
    assert!(
        !f.text.contains("taras") && !f.text.contains("0501234567"),
        "{}",
        f.text
    );
    assert_eq!(f.contact.as_deref(), Some("taras@example.com"));
    // The contact is never part of the stored body.
    assert!(!serde_json::to_string(&f).unwrap().contains("taras"));
}

#[test]
fn feedback_refusals_say_what_to_fix() {
    assert_eq!(
        feedback(serde_json::json!({"text": "  "}))
            .unwrap_err()
            .field,
        "text"
    );
    assert_eq!(
        feedback(serde_json::json!({"contact": "ні"}))
            .unwrap_err()
            .field,
        "contact"
    );
    assert_eq!(
        feedback(serde_json::json!({"kind": "spam"}))
            .unwrap_err()
            .field,
        "kind"
    );
    assert_eq!(
        feedback(serde_json::json!({"crash_ids": ["x"]}))
            .unwrap_err()
            .field,
        "crash_ids"
    );
    let long = "а".repeat(MAX_TEXT_CHARS + 1);
    assert_eq!(
        feedback(serde_json::json!({ "text": long }))
            .unwrap_err()
            .field,
        "text"
    );
}

#[test]
fn the_site_form_gets_a_new_id() {
    let f = feedback(serde_json::json!({"id": null, "channel": "site"})).unwrap();
    assert_eq!(f.id, NEW_ID);
    assert_eq!(f.channel, Channel::Site);
}

#[test]
fn forget_needs_real_ids() {
    let ok = ForgetWire {
        ids: vec![ID.to_uppercase()],
    };
    assert_eq!(ok.ids().unwrap(), vec![ID.to_owned()]);
    assert!(ForgetWire { ids: vec![] }.ids().is_err());
    assert!(
        ForgetWire {
            ids: vec!["*".into()]
        }
        .ids()
        .is_err()
    );
    assert!(
        ForgetWire {
            ids: vec![ID.into(); 101]
        }
        .ids()
        .is_err()
    );
}
