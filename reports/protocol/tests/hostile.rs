// SPDX-License-Identifier: GPL-3.0-or-later
//! Hostile input for the reports protocol (ADR 0018): every value that
//! arrives is written by someone who wants to break the server, slip personal
//! data past the scrubber or forge a crash signature. These tests use only the
//! public API, the way the server and `vilna-reportd` do.
//!
//! All personal data here is made up (`example.org`, documentation IP ranges,
//! the 00:00:5e documentation MAC prefix, test card and phone numbers).

use vilna_reports_protocol::{
    Crash, CrashKind, CrashReceipt, CrashWire, Feedback, FeedbackWire, ForgetWire, Frame, Invalid,
    MAX_CRASH_IDS, MAX_FORGET_IDS, MAX_FRAMES, MAX_TEXT_CHARS, SIGNATURE_FRAMES, Status, report_id,
    round_memory_gb, scrub, tail, truncate,
};

const ID: &str = "3f2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9b";
const OTHER_ID: &str = "0b2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9b";
const NEW_ID: &str = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";

/// A good crash report with `extra` fields put over it. `Err` is the serde
/// error text when the hostile JSON does not even parse.
fn crash_wire(extra: serde_json::Value) -> Result<CrashWire, String> {
    let mut base = serde_json::json!({
        "schema": 1,
        "id": ID,
        "program": {"name": "gloxi", "version": "0.2.0", "packaging": "flatpak"},
        "kind": "exception",
        "exception": "KeyError",
        "frames": [{"file": "gloxi/chat.py", "line": 120, "function": "answer"}],
        "locale": "uk",
        "arch": "x86_64",
        "memory_gb": 12
    });
    if let (Some(base), Some(extra)) = (base.as_object_mut(), extra.as_object()) {
        for (k, v) in extra {
            base.insert(k.clone(), v.clone());
        }
    }
    serde_json::from_value(base).map_err(|e| e.to_string())
}

fn crash(extra: serde_json::Value) -> Result<Crash, String> {
    let wire = crash_wire(extra)?;
    Crash::from_wire(wire).map_err(|e| e.to_string())
}

fn feedback_wire(extra: serde_json::Value) -> Result<FeedbackWire, String> {
    let mut base = serde_json::json!({
        "schema": 1, "id": ID, "kind": "bug", "channel": "gloxi",
        "text": "Копії зупинились на 40 %", "locale": "uk"
    });
    if let (Some(base), Some(extra)) = (base.as_object_mut(), extra.as_object()) {
        for (k, v) in extra {
            base.insert(k.clone(), v.clone());
        }
    }
    serde_json::from_value(base).map_err(|e| e.to_string())
}

fn feedback(extra: serde_json::Value) -> Result<Result<Feedback, Invalid>, String> {
    Ok(Feedback::from_wire(feedback_wire(extra)?, || NEW_ID.into()))
}

/// Deterministic xorshift64*, so a failure can be replayed; no new dependency.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_f491_4f6c_dd1d)
    }

    fn below(&mut self, n: usize) -> usize {
        usize::try_from(self.next() % u64::try_from(n.max(1)).unwrap_or(1)).unwrap_or(0)
    }

    /// A string built from pieces that each poke at one rule of the scrubber
    /// or the field checks.
    fn hostile(&mut self, max_pieces: usize) -> String {
        const PIECES: &[&str] = &[
            "/",
            "\\",
            ":",
            ".",
            "@",
            "-",
            "_",
            "~/",
            "://",
            ":\\",
            "\\\\",
            "..",
            "src/",
            "crates/",
            ".rs:",
            "0",
            "7",
            "123456",
            "ff",
            "a",
            "Я",
            "ї",
            "é",
            "e\u{301}",
            "👩‍💻",
            "\u{200b}",
            "\u{202e}",
            "\u{feff}",
            "\u{0}",
            "\u{7f}",
            "\r\n",
            "\n",
            "\t",
            " ",
            "\u{a0}",
            "\u{3000}",
            "<",
            ">",
            "|",
            "!",
            "%",
            "\"",
            "'",
            "anna",
            "example.org",
            "192.0.2.",
            "2001:db8::",
            "00:00:5e:00:53:",
            "０",
            "٣",
            "𝟗",
            "\u{fffd}",
            "\u{10ffff}",
        ];
        let n = self.below(max_pieces + 1);
        let mut s = String::new();
        for _ in 0..n {
            s.push_str(PIECES[self.below(PIECES.len())]);
        }
        s
    }
}

// ------------------------------------------------------------- scrubbing --

#[test]
fn scrub_hides_every_documented_form() {
    for (input, hidden, marker) in [
        // e-mail
        ("пишіть на anna.k+bugs@mail.example.org!", "anna", "<email>"),
        ("ANNA@EXAMPLE.ORG", "ANNA", "<email>"),
        ("олена@приклад.укр", "олена", "<email>"),
        ("mailto:anna@example.org", "anna", "<email>"),
        ("<anna@example.org>,", "anna", "<email>"),
        // IPv4, with port, mask or punctuation around it
        ("192.0.2.20", "192.0", "<ip>"),
        ("192.0.2.20:8080", "192.0", "<ip>"),
        ("198.51.100.7/24", "198.51", "<ip>"),
        ("(203.0.113.9),", "203.0", "<ip>"),
        ("050.123.45.67", "123.45", "<ip>"),
        // IPv6
        ("2001:0db8:85a3:0000:0000:8a2e:0370:7334", "8a2e", "<ip>"),
        ("fe80::1ff:fe23:4567:890a", "fe23", "<ip>"),
        ("[2001:db8::1]:443", "db8", "<ip>"),
        ("fe80::1%eth0", "fe80", "<ip>"),
        ("::ffff:192.0.2.20", "192.0", "<ip>"),
        // MAC
        ("00:00:5e:00:53:af", "53:af", "<mac>"),
        ("00-00-5E-00-53-AF", "53-AF", "<mac>"),
        // paths
        ("/home/anna/Документи/лист.odt", "anna", "<path>"),
        ("~/anna-notes.txt", "anna", "<path>"),
        ("C:\\Users\\anna", "anna", "<path>"),
        ("D:/Users/anna/x", "anna", "<path>"),
        ("\\\\server\\anna", "anna", "<path>"),
        ("https://example.org/u/anna", "anna", "<path>"),
        ("file=/home/anna/x", "anna", "<path>"),
        ("'/home/anna/x'", "anna", "<path>"),
        // six or more digits, in any script
        ("тел. 0501234567", "0501234567", "<n>"),
        ("id380501234567", "380501234567", "<n>"),
        ("код 123456.", "123456", "<n>"),
        ("٠٥٠١٢٣٤٥٦٧", "٠٥٠١٢٣٤٥٦٧", "<n>"),
        ("０５０１２３４５６７", "０５０１２３４５６７", "<n>"),
        ("4111111111111111", "4111111111111111", "<n>"),
    ] {
        let out = scrub(input, 1000);
        assert!(!out.contains(hidden), "{input:?} -> {out:?}");
        assert!(out.contains(marker), "{input:?} -> {out:?}");
    }
}

#[test]
fn scrub_keeps_our_own_places_and_plain_words() {
    assert_eq!(scrub("in gloxi/chat.py", 100), "in gloxi/chat.py");
    assert_eq!(
        scrub("at crates/herkosd/src/gate.rs:12:5", 100),
        "at crates/herkosd/src/gate.rs:12:5"
    );
    // The build machine's folders before `crates/` or `src/` go.
    assert_eq!(
        scrub("/home/anna/build/crates/herkosd/src/gate.rs:12", 100),
        "crates/herkosd/src/gate.rs:12"
    );
    assert_eq!(
        scrub("C:\\Users\\anna\\proj/src/main.rs:5", 100),
        "src/main.rs:5"
    );
    assert_eq!(
        scrub("Копії зупинились на 40 %", 100),
        "Копії зупинились на 40 %"
    );
    assert_eq!(
        scrub("версія 0.2.0, 12345 файлів", 100),
        "версія 0.2.0, 12345 файлів"
    );
}

#[test]
fn scrub_reads_only_the_first_max_chars() {
    for max in [0, 1, 5, 64] {
        let input = format!("{}anna@example.org /home/anna 0501234567", "ї".repeat(max));
        let out = scrub(&input, max);
        assert!(
            !out.contains("anna") && !out.contains("<"),
            "{max}: {out:?}"
        );
        assert_eq!(out.chars().count(), max);
    }
    assert_eq!(scrub("anything", 0), "");
    assert_eq!(scrub("", 1000), "");
    // A long input with a huge limit is not a problem either.
    let big = "a ".repeat(50_000);
    assert_eq!(scrub(&big, usize::MAX), big);
}

#[test]
fn scrub_keeps_whitespace_and_hostile_unicode_without_panicking() {
    for input in [
        "\u{0}\u{0}\u{0}",
        "\u{202e}gpj.exe",
        "\u{feff}\u{200b}\u{200d}",
        "e\u{301}\u{301}\u{301}",
        "👩‍💻👩‍💻",
        "\r\n\t\u{a0}\u{3000}\u{2028}",
        "\u{10ffff}\u{fffd}",
    ] {
        let out = scrub(input, 1000);
        assert_eq!(out, input, "{input:?}");
    }
    // Whitespace of every kind splits tokens and is kept as it was.
    assert_eq!(scrub("a\u{3000}/x\u{a0}b", 100), "a\u{3000}<path>\u{a0}b");
    // Cutting inside a grapheme keeps valid UTF-8 (whole chars, not bytes).
    assert_eq!(scrub("e\u{301}x", 1), "e");
}

#[test]
fn scrub_output_is_stable_when_scrubbed_again() {
    let mut rng = Rng(0x5eed_0001);
    for _ in 0..3000 {
        let input = rng.hostile(40);
        let once = scrub(&input, 100_000);
        assert_eq!(scrub(&once, 100_000), once, "{input:?}");
    }
}

#[test]
fn scrub_never_panics_on_random_hostile_text() {
    let mut rng = Rng(0x5eed_0002);
    for _ in 0..5000 {
        let input = rng.hostile(60);
        let max = rng.below(80);
        let out = scrub(&input, max);
        // Whatever comes out, nothing past the limit was read.
        let rest: String = input.chars().skip(max).collect();
        if rest.chars().count() > 30 && !out.is_empty() {
            assert!(!out.ends_with(rest.as_str()), "{input:?} -> {out:?}");
        }
    }
}

#[test]
fn scrub_hides_forms_glued_to_punctuation_quotes_and_urls() {
    for (input, hidden) in [
        ("x:anna@example.org;y", "anna"),
        ("\"anna@example.org\"", "anna"),
        ("(anna@example.org)", "anna"),
        ("ip=192.0.2.1;mac=00:00:5e:00:53:af", "192.0"),
        ("ip=192.0.2.1;mac=00:00:5e:00:53:af", "53:af"),
        ("\"192.0.2.1\"", "192.0"),
        ("[00:00:5e:00:53:af]", "5e:00"),
        ("id=0501234567,", "0501234567"),
        ("\"/home/anna\"", "anna"),
        ("(C:\\Users\\anna)", "anna"),
        ("/HOME/ANNA", "ANNA"),
        ("/home/анна", "анна"),
        ("HTTP://EXAMPLE.ORG/anna", "anna"),
        ("url=https://example.org/?u=anna@example.org", "anna"),
        ("C:\\Users\\Anna Kowalska\\Documents\\x.odt", "Kowalska"),
    ] {
        let out = scrub(input, 1000);
        assert!(!out.contains(hidden), "{input:?} -> {out:?}");
    }
}

#[test]
fn scrub_handles_a_megabyte_of_text() {
    let line = "anna@example.org /home/anna 192.0.2.1 0501234567 ї👩‍💻\n";
    let big = line.repeat((1 << 20) / line.len() + 1);
    assert!(big.len() > 1 << 20);
    let out = scrub(&big, usize::MAX);
    assert!(!out.contains("anna") && !out.contains("192.0") && !out.contains("0501234567"));
    let cut = scrub(&big, 1000);
    assert!(cut.chars().count() <= 1000);
    let blank = " ".repeat(1 << 20);
    assert_eq!(scrub(&blank, usize::MAX), blank);
}

#[test]
fn scrub_takes_lossy_decoded_bytes_and_control_characters() {
    // Broken UTF-8 (a lone surrogate, a stray byte, a cut sequence) arrives
    // as U+FFFD after `from_utf8_lossy`.
    let lossy = String::from_utf8_lossy(&[0xed, 0xa0, 0x80, b'a', 0xff, 0xc3]).into_owned();
    assert_eq!(scrub(&lossy, 1000), lossy);
    assert_eq!(scrub(&lossy, 3).chars().count(), 3);
    let controls: String = (0u8..0x20).chain([0x7f]).map(char::from).collect();
    let out = scrub(&controls, 1000);
    assert_eq!(out.chars().count(), controls.chars().count());
    let out = scrub("\u{1b}[31manna@example.org\u{1b}[0m", 1000);
    assert!(!out.contains("anna"), "{out:?}");
}

// --------------------------------------------------- truncate, tail, ids --

#[test]
fn truncate_and_tail_count_characters_not_bytes() {
    let s = "аб👩‍💻в";
    assert_eq!(truncate(s, 0), "");
    assert_eq!(truncate(s, 2), "аб");
    assert_eq!(truncate(s, 3), "аб👩");
    assert_eq!(truncate(s, usize::MAX), s);
    assert_eq!(tail(s, 0), "");
    assert_eq!(tail(s, 1), "в");
    assert_eq!(tail(s, 2), "💻в");
    assert_eq!(tail(s, usize::MAX), s);
    assert_eq!(tail("", 5), "");
    assert_eq!(truncate("", 5), "");
}

#[test]
fn report_ids_are_only_lowercase_random_uuids() {
    assert_eq!(report_id(ID).as_deref(), Some(ID));
    assert_eq!(report_id(&ID.to_uppercase()).as_deref(), Some(ID));
    assert_eq!(report_id(&format!(" \t{ID}\n")).as_deref(), Some(ID));
    for bad in [
        "",
        "3f2b8c1e-9a4d-1e7f-8b21-0c5d6e7f8a9b", // version 1
        "3f2b8c1e-9a4d-5e7f-8b21-0c5d6e7f8a9b", // version 5
        "3f2b8c1e-9a4d-4e7f-cb21-0c5d6e7f8a9b", // wrong variant
        "3f2b8c1e9a4d4e7f8b210c5d6e7f8a9b",     // no hyphens
        "{3f2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9b}",
        "urn:uuid:3f2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9b",
        "3f2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9b0",
        "3f2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9b\u{0}",
        "3f2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9b\nDROP",
        "3f2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9g",
        "３f2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9b",
        "*",
        "../3f2b8c1e-9a4d-4e7f-8b21-0c5d6e7f8a9b",
    ] {
        assert_eq!(report_id(bad), None, "{bad:?}");
    }
}

#[test]
fn memory_is_rounded_and_bounded() {
    for (gb, rounded) in [
        (0, None),
        (1, Some(1)),
        (3, Some(4)),
        (12, Some(16)),
        (4096, Some(4096)),
        (4097, None),
        (u32::MAX, None),
    ] {
        assert_eq!(round_memory_gb(gb), rounded, "{gb}");
    }
}

// ------------------------------------------------------- crash: parsing --

#[test]
fn malformed_json_is_an_error_not_a_panic() {
    for body in [
        "",
        "null",
        "[]",
        "\"crash\"",
        "{",
        "{\"schema\": null}",
        "{\"schema\": -1}",
        "{\"schema\": 4294967296}",
        "{\"schema\": 1.5}",
        "{\"schema\": \"1\"}",
        "{\"schema\": 1, \"schema\": 1}",
        "{\"frames\": {}}",
        "{\"frames\": [1]}",
        "{\"frames\": [{\"line\": -1}]}",
        "{\"frames\": [{\"line\": 4294967296}]}",
        "{\"memory_gb\": -8}",
        "{\"memory_gb\": 1e9999}",
        "{\"program\": \"gloxi\"}",
        "{\"id\": \"\\ud800\"}",
    ] {
        assert!(serde_json::from_str::<CrashWire>(body).is_err(), "{body:?}");
    }
    // Deep nesting stops at serde_json's recursion limit instead of the stack.
    let deep = format!("{{\"frames\": {}", "[".repeat(100_000));
    assert!(serde_json::from_str::<CrashWire>(&deep).is_err());
    // A deep unknown field is skipped without recursion, so it neither
    // overflows the stack nor refuses the body.
    let deep_unknown = format!(
        "{{\"schema\": 1, \"x\": {}1{}}}",
        "{\"a\":".repeat(100_000),
        "}".repeat(100_000)
    );
    let wire = serde_json::from_str::<CrashWire>(&deep_unknown).unwrap();
    assert_eq!(wire.schema, 1);
}

#[test]
fn unknown_and_huge_unknown_fields_are_ignored() {
    let c = crash(serde_json::json!({
        "__proto__": {"admin": true},
        "constructor": "x",
        "future_field": "a".repeat(100_000),
        "nested": [[[[[[[[[[1]]]]]]]]]]
    }))
    .unwrap();
    assert_eq!(c.program, "gloxi");
    let stored = serde_json::to_string(&c).unwrap();
    assert!(
        !stored.contains("proto") && !stored.contains("future"),
        "{stored}"
    );
}

#[test]
fn required_fields_refuse_hostile_values() {
    for (extra, field) in [
        (serde_json::json!({"schema": 0}), "schema"),
        (serde_json::json!({"schema": 2}), "schema"),
        (serde_json::json!({"schema": u32::MAX}), "schema"),
        (serde_json::json!({"id": ""}), "id"),
        (serde_json::json!({"id": "' OR 1=1 --"}), "id"),
        (serde_json::json!({"program": {}}), "program.name"),
        (serde_json::json!({"program": {"name": ""}}), "program.name"),
        (
            serde_json::json!({"program": {"name": "-rf"}}),
            "program.name",
        ),
        (
            serde_json::json!({"program": {"name": ".hidden"}}),
            "program.name",
        ),
        (
            serde_json::json!({"program": {"name": "../etc/passwd"}}),
            "program.name",
        ),
        (
            serde_json::json!({"program": {"name": "glo\nxi"}}),
            "program.name",
        ),
        (
            serde_json::json!({"program": {"name": "glоxi"}}),
            "program.name",
        ), // Cyrillic о
        (
            serde_json::json!({"program": {"name": "a".repeat(129)}}),
            "program.name",
        ),
        (
            serde_json::json!({"program": {"name": "gloxi; rm -rf /"}}),
            "program.name",
        ),
        (serde_json::json!({"kind": ""}), "kind"),
        (serde_json::json!({"kind": "Signal"}), "kind"),
        (serde_json::json!({"kind": " panic"}), "kind"),
    ] {
        let err = crash(extra.clone()).unwrap_err();
        assert!(err.starts_with(&format!("{field}: ")), "{extra}: {err}");
    }
    // The limits themselves are fine; the name is trimmed first.
    assert_eq!(
        crash(serde_json::json!({"program": {"name": "a".repeat(128)}}))
            .unwrap()
            .program
            .len(),
        128
    );
    assert_eq!(
        crash(serde_json::json!({"program": {"name": "  gloxi\t"}}))
            .unwrap()
            .program,
        "gloxi"
    );
}

#[test]
fn hostile_optional_fields_are_dropped_not_refused() {
    let c = crash(serde_json::json!({
        "program": {"name": "gloxi", "version": "0.2.0; rm -rf /", "packaging": "RPM"},
        "image": "Vilna\n2026",
        "exception": "Key Error",
        "signal": "SIGSEGV",
        "location": "/home/anna/src/a.rs:1",
        "locale": "uk_UA.UTF-8",
        "arch": "x86-64",
        "memory_gb": 5000
    }))
    .unwrap();
    assert_eq!(c.version, None);
    assert_eq!(c.packaging, vilna_reports_protocol::Packaging::Other);
    assert_eq!(c.image, None);
    assert_eq!(c.exception, None);
    // Fields of another kind of crash do not stay.
    assert_eq!(c.signal, None);
    assert_eq!(c.location, None);
    assert_eq!(c.locale, None);
    assert_eq!(c.arch, None);
    assert_eq!(c.memory_gb, None);

    for (extra, ok) in [
        (serde_json::json!({"image": "a".repeat(64)}), true),
        (serde_json::json!({"image": "a".repeat(65)}), false),
        (
            serde_json::json!({"program": {"name": "gloxi", "version": "1".repeat(64)}}),
            true,
        ),
        (
            serde_json::json!({"program": {"name": "gloxi", "version": "1".repeat(65)}}),
            false,
        ),
        (serde_json::json!({"locale": "uk-UA"}), true),
        (serde_json::json!({"locale": "UK"}), false),
        (serde_json::json!({"arch": "a".repeat(17)}), false),
    ] {
        let c = crash(extra.clone()).unwrap();
        let kept = c.image.is_some()
            || c.version.is_some_and(|v| v.len() > 10)
            || c.locale.is_some_and(|l| l != "uk")
            || c.arch.as_deref() != Some("x86_64") && c.arch.is_some();
        assert_eq!(kept, ok, "{extra}");
    }
}

#[test]
fn a_signal_report_keeps_only_signal_fields() {
    let c = crash(serde_json::json!({
        "kind": "signal", "signal": "SIGSEGV", "exception": "KeyError",
        "location": "src/a.rs:1"
    }))
    .unwrap();
    assert_eq!(c.kind, CrashKind::Signal);
    assert_eq!(c.signal.as_deref(), Some("SIGSEGV"));
    assert_eq!(c.exception, None);
    assert_eq!(c.location, None);
    for bad in [
        "sigsegv",
        "SIG",
        "SIGSEGV ",
        "SIG_SEGV",
        "SIGABCDEFGHIJK",
        "SIGSEGV\n",
    ] {
        let c = crash(serde_json::json!({"kind": "signal", "signal": bad})).unwrap();
        // " SIGSEGV " trims to a good one; the others are dropped.
        assert!(c.signal.is_none() || bad.trim() == "SIGSEGV", "{bad:?}");
    }
}

#[test]
fn a_panic_location_is_scrubbed_and_bounded() {
    let c = crash(serde_json::json!({
        "kind": "panic",
        "location": "/home/anna/.cargo/proj/crates/herkosd/src/gate.rs:10:9"
    }))
    .unwrap();
    assert_eq!(
        c.location.as_deref(),
        Some("crates/herkosd/src/gate.rs:10:9")
    );
    let c = crash(serde_json::json!({
        "kind": "panic",
        "location": "called by anna@example.org on 192.0.2.20 id 0501234567"
    }))
    .unwrap();
    assert_eq!(
        c.location.as_deref(),
        Some("called by <email> on <ip> id <n>")
    );
    for blank in ["", "   ", "\n\t"] {
        let c = crash(serde_json::json!({"kind": "panic", "location": blank})).unwrap();
        assert_eq!(c.location, None, "{blank:?}");
    }
    // A long location is cut before scrubbing: plain text stays within 300.
    let c = crash(serde_json::json!({"kind": "panic", "location": "x".repeat(10_000)})).unwrap();
    assert_eq!(c.location.map(|l| l.chars().count()), Some(300));
}

// -------------------------------------------------------- crash: frames --

#[test]
fn hostile_frame_fields_are_dropped() {
    let frames = serde_json::json!([
        {"file": "gloxi/../../etc/shadow", "line": 1},
        {"file": "..", "line": 1},
        {"file": "gloxi/..", "line": 1},
        {"file": "/etc/passwd", "line": 1},
        {"file": "C:/Windows/x.py", "line": 1},
        {"file": "gloxi\\chat.py", "line": 1},
        {"file": "./chat.py", "line": 1},
        {"file": "a".repeat(201), "line": 1},
        {"module": "/usr/lib64/libgtk-4.so.1", "line": 1},
        {"module": "lib gtk", "line": 1},
        {"build_id": "abcdef0", "line": 1},
        {"build_id": "a".repeat(129), "line": 1},
        {"build_id": "abcdefgh", "line": 1},
        {"offset": "0x", "line": 1},
        {"offset": "1f", "line": 1},
        {"offset": format!("0x{}", "f".repeat(17)), "line": 1},
        {"offset": "-0x1", "line": 1},
        {"line": 0},
        {"function": "   "},
        {}
    ]);
    let c = crash(serde_json::json!({ "frames": frames })).unwrap();
    // Every hostile value goes; a frame left with only a line number stays,
    // a frame left with nothing goes.
    for f in &c.frames {
        assert_eq!(
            Frame {
                line: f.line,
                ..Frame::default()
            },
            *f
        );
    }
    assert_eq!(c.frames.len(), 17, "{:?}", c.frames);
}

#[test]
fn good_frame_fields_at_their_limits_stay() {
    let c = crash(serde_json::json!({
        "frames": [
            {"file": format!("gloxi/{}", "a".repeat(194)), "line": u32::MAX},
            {"module": "libgtk-4.so.1", "build_id": "ABCDEF01", "offset": "0X1F"},
            {"build_id": "a".repeat(128), "offset": format!("0x{}", "f".repeat(16))}
        ]
    }))
    .unwrap();
    assert_eq!(c.frames.len(), 3);
    assert_eq!(c.frames[0].file.as_ref().map(String::len), Some(200));
    assert_eq!(c.frames[0].line, Some(u32::MAX));
    assert_eq!(c.frames[1].build_id.as_deref(), Some("abcdef01"));
    assert_eq!(c.frames[1].offset.as_deref(), Some("0x1f"));
    assert!(c.frames[2].build_id.is_some() && c.frames[2].offset.is_some());
}

#[test]
fn frame_functions_are_scrubbed() {
    let c = crash(serde_json::json!({
        "frames": [{"file": "gloxi/chat.py", "function": "send_to anna@example.org /home/anna/x 0501234567"}]
    }))
    .unwrap();
    assert_eq!(
        c.frames[0].function.as_deref(),
        Some("send_to <email> <path> <n>")
    );
}

#[test]
fn the_frame_cap_counts_frames_as_sent() {
    // The cap is applied before bad frames are dropped: 64 empty frames in
    // front push the real ones out. The report is still accepted.
    let mut frames: Vec<_> = (0..MAX_FRAMES).map(|_| serde_json::json!({})).collect();
    frames.push(serde_json::json!({"file": "gloxi/chat.py", "line": 1}));
    let c = crash(serde_json::json!({ "frames": frames })).unwrap();
    assert!(c.frames.is_empty());
    let many: Vec<_> = (0..100_000)
        .map(|_| serde_json::json!({"file": "gloxi/chat.py", "line": 1}))
        .collect();
    let c = crash(serde_json::json!({ "frames": many })).unwrap();
    assert_eq!(c.frames.len(), MAX_FRAMES);
}

// -------------------------------------------- crash: signature and title --

fn is_signature(s: &str) -> bool {
    s.len() == 16
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

#[test]
fn the_signature_ignores_what_differs_between_devices() {
    let a = crash(serde_json::json!({})).unwrap();
    let b = crash(serde_json::json!({
        "id": OTHER_ID,
        "program": {"name": "gloxi", "version": "9.9.9", "packaging": "rpm"},
        "image": "Vilna 2026",
        "locale": "en",
        "arch": "aarch64",
        "memory_gb": 64,
        "frames": [{"file": "gloxi/chat.py", "line": 999, "function": "answer"}]
    }))
    .unwrap();
    assert_eq!(a.signature(), b.signature());
    assert!(is_signature(&a.signature()));
}

#[test]
fn the_signature_uses_only_the_top_frames() {
    let top: Vec<_> = (0..SIGNATURE_FRAMES)
        .map(|i| serde_json::json!({"file": format!("gloxi/f{i}.py"), "function": "f"}))
        .collect();
    let mut deeper = top.clone();
    deeper.push(serde_json::json!({"file": "gloxi/deep.py", "function": "g"}));
    let a = crash(serde_json::json!({ "frames": top })).unwrap();
    let b = crash(serde_json::json!({ "frames": deeper })).unwrap();
    assert_eq!(a.signature(), b.signature());
    let c = crash(serde_json::json!({"exception": "ValueError"})).unwrap();
    assert_ne!(a.signature(), c.signature());
}

#[test]
fn the_signature_changes_with_what_the_crash_is() {
    let base = crash(serde_json::json!({})).unwrap().signature();
    for extra in [
        serde_json::json!({"program": {"name": "herkosd"}}),
        serde_json::json!({"exception": "ValueError"}),
        serde_json::json!({"kind": "panic", "exception": null}),
        serde_json::json!({"frames": [{"file": "gloxi/chat.py", "function": "ask"}]}),
        serde_json::json!({"frames": [{"file": "gloxi/router.py", "function": "answer"}]}),
        serde_json::json!({"frames": [
            {"file": "gloxi/chat.py", "function": "answer"},
            {"file": "gloxi/router.py", "function": "route"}
        ]}),
        serde_json::json!({"frames": []}),
    ] {
        let other = crash(extra.clone()).unwrap().signature();
        assert_ne!(other, base, "{extra}");
    }
    // A native frame without a function is told apart by its offset.
    let at = |offset: &str| {
        crash(serde_json::json!({"kind": "signal", "signal": "SIGSEGV",
            "frames": [{"module": "libgtk-4.so.1", "offset": offset}]}))
        .map(|c| c.signature())
    };
    assert_ne!(at("0x10").unwrap(), at("0x20").unwrap());
    assert_eq!(at("0x10").unwrap(), at("0X10").unwrap());
    // Panic places differ by file, not by line or column.
    let place = |l: &str| {
        crash(serde_json::json!({"kind": "panic", "location": l, "frames": []}))
            .map(|c| c.signature())
    };
    assert_eq!(
        place("src/a.rs:1:1").unwrap(),
        place("/build/x/src/a.rs:99:7").unwrap()
    );
    assert_ne!(place("src/a.rs:1").unwrap(), place("src/b.rs:1").unwrap());
}

#[test]
fn titles_stay_short() {
    for extra in [
        serde_json::json!({"kind": "panic", "location": "ї".repeat(1000)}),
        serde_json::json!({"frames": [{"file": "gloxi/chat.py", "function": "👩‍💻".repeat(500)}]}),
        serde_json::json!({"frames": []}),
        serde_json::json!({"kind": "signal", "frames": []}),
    ] {
        let c = crash(extra.clone()).unwrap();
        assert!(c.title().chars().count() <= 120, "{extra}");
        assert!(c.title().starts_with("gloxi: "), "{extra}");
    }
    let bare = crash(serde_json::json!({"kind": "signal", "frames": []})).unwrap();
    assert_eq!(bare.title(), "gloxi: crash in unknown place");
    let panic = crash(serde_json::json!({"kind": "panic", "frames": []})).unwrap();
    assert_eq!(panic.title(), "gloxi: panic in unknown place");
}

#[test]
fn a_stored_hostile_report_goes_back_on_the_wire_unchanged() {
    for extra in [
        serde_json::json!({"kind": "panic", "location": "anna@example.org at /home/anna/src/a.rs:1"}),
        serde_json::json!({"frames": [{"file": "gloxi/chat.py", "function": "f 192.0.2.1 ї👩‍💻"}]}),
        serde_json::json!({"kind": "panic", "location": "x".repeat(5000)}),
        serde_json::json!({"memory_gb": 4096, "frames": []}),
    ] {
        let c = crash(extra.clone()).unwrap();
        let wire: CrashWire = serde_json::from_value(c.to_wire()).unwrap();
        assert_eq!(Crash::from_wire(wire).unwrap(), c, "{extra}");
    }
}

// ---------------------------------------------------------- random crash --

#[test]
fn random_hostile_crash_reports_never_panic() {
    let mut rng = Rng(0x5eed_0003);
    let kinds = ["signal", "exception", "panic", "", "x"];
    for _ in 0..2000 {
        let frames: Vec<_> = (0..rng.below(12))
            .map(|_| {
                serde_json::json!({
                    "module": rng.hostile(8), "build_id": rng.hostile(8),
                    "offset": rng.hostile(4), "file": rng.hostile(8),
                    "line": rng.below(3), "function": rng.hostile(30)
                })
            })
            .collect();
        let extra = serde_json::json!({
            "id": if rng.below(2) == 0 { ID.to_owned() } else { rng.hostile(10) },
            "program": {"name": if rng.below(2) == 0 { "gloxi".to_owned() } else { rng.hostile(5) },
                        "version": rng.hostile(5), "packaging": rng.hostile(2)},
            "kind": kinds[rng.below(kinds.len())],
            "image": rng.hostile(5), "signal": rng.hostile(3), "exception": rng.hostile(3),
            "location": rng.hostile(60), "locale": rng.hostile(3), "arch": rng.hostile(3),
            "frames": frames
        });
        let Ok(wire) = crash_wire(extra) else {
            continue;
        };
        if let Ok(c) = Crash::from_wire(wire) {
            assert!(is_signature(&c.signature()));
            assert!(c.title().chars().count() <= 120);
            assert!(c.frames.len() <= MAX_FRAMES);
            let _ = serde_json::to_string(&c).unwrap();
            let _ = c.to_wire();
        }
    }
}

// --------------------------------------------------------------- feedback --

#[test]
fn feedback_refuses_hostile_required_fields() {
    for (extra, field) in [
        (serde_json::json!({"schema": 2}), "schema"),
        (serde_json::json!({"schema": 0}), "schema"),
        (serde_json::json!({"id": "1"}), "id"),
        (
            serde_json::json!({"id": "3f2b8c1e-9a4d-1e7f-8b21-0c5d6e7f8a9b"}),
            "id",
        ),
        (serde_json::json!({"kind": "Bug"}), "kind"),
        (serde_json::json!({"kind": "bug "}), "kind"),
        (serde_json::json!({"channel": "email"}), "channel"),
        (serde_json::json!({"channel": "Site"}), "channel"),
        (serde_json::json!({"text": ""}), "text"),
        (serde_json::json!({"text": " \n\t\u{a0}\u{3000} "}), "text"),
        (
            serde_json::json!({"text": "а".repeat(MAX_TEXT_CHARS + 1)}),
            "text",
        ),
        // Whitespace counts toward the limit before trimming.
        (
            serde_json::json!({"text": format!("{} ", "а".repeat(MAX_TEXT_CHARS))}),
            "text",
        ),
        (serde_json::json!({"crash_ids": ["*"]}), "crash_ids"),
        (serde_json::json!({"crash_ids": [ID, "../x"]}), "crash_ids"),
    ] {
        let err = feedback(extra.clone()).unwrap().unwrap_err();
        assert_eq!(err.field, field, "{extra}");
        assert!(!err.why.is_empty());
    }
}

#[test]
fn feedback_text_limits_count_characters() {
    // 8000 four-byte characters are 32000 bytes and still fine.
    let f = feedback(serde_json::json!({"text": "👩".repeat(MAX_TEXT_CHARS)}))
        .unwrap()
        .unwrap();
    assert_eq!(f.text.chars().count(), MAX_TEXT_CHARS);
}

#[test]
fn hostile_contacts_are_refused_and_never_stored() {
    for bad in [
        "ні",
        "anna@example.org\r\nBcc: x@example.org",
        "anna@example.org\nX: y",
        "Anna <anna@example.org>",
        "anna@example.org, b@example.org",
        "a@b@example.org",
        "anna@localhost",
        "anna@example..org",
        "anna@example.org\u{0}",
        "@example.org",
        "anna@",
    ] {
        let err = feedback(serde_json::json!({ "contact": bad }))
            .unwrap()
            .unwrap_err();
        assert_eq!(err.field, "contact", "{bad:?}");
    }
    let long_local = format!("{}@example.org", "a".repeat(65));
    assert!(
        feedback(serde_json::json!({ "contact": long_local }))
            .unwrap()
            .is_err()
    );
    let long = format!("a@{}.org", "b".repeat(250));
    assert!(
        feedback(serde_json::json!({ "contact": long }))
            .unwrap()
            .is_err()
    );

    let f = feedback(serde_json::json!({"contact": "  anna@example.org \n"}))
        .unwrap()
        .unwrap();
    assert_eq!(f.contact.as_deref(), Some("anna@example.org"));
    assert!(!serde_json::to_string(&f).unwrap().contains("anna"));
    for empty in ["", "   "] {
        let f = feedback(serde_json::json!({ "contact": empty }))
            .unwrap()
            .unwrap();
        assert_eq!(f.contact, None);
    }
}

#[test]
fn every_free_text_field_is_scrubbed() {
    let dirty = "anna@example.org 192.0.2.20 00:00:5e:00:53:af /home/anna/x 0501234567";
    let clean = "<email> <ip> <mac> <path> <n>";
    let f = feedback(serde_json::json!({
        "text": dirty, "text_en": dirty, "system": dirty, "conversation": dirty
    }))
    .unwrap()
    .unwrap();
    assert_eq!(f.text, clean);
    assert_eq!(f.text_en.as_deref(), Some(clean));
    assert_eq!(f.system.as_deref(), Some(clean));
    assert_eq!(f.conversation.as_deref(), Some(clean));
    assert!(!serde_json::to_string(&f).unwrap().contains("anna"));
}

#[test]
fn optional_feedback_fields_are_dropped_when_blank_or_wrong() {
    let f = feedback(serde_json::json!({
        "text_en": "  ", "system": "\n", "conversation": "\t",
        "locale": "uk'; --", "program": "../gloxi", "version": "1 2"
    }))
    .unwrap()
    .unwrap();
    assert_eq!(f.text_en, None);
    assert_eq!(f.system, None);
    assert_eq!(f.conversation, None);
    assert_eq!(f.locale, None);
    assert_eq!(f.program, None);
    assert_eq!(f.version, None);
}

#[test]
fn crash_ids_beyond_the_limit_are_not_read() {
    let ids: Vec<String> = (0..MAX_CRASH_IDS + 5).map(|_| ID.to_owned()).collect();
    let f = feedback(serde_json::json!({ "crash_ids": ids }))
        .unwrap()
        .unwrap();
    assert_eq!(f.crash_ids.len(), MAX_CRASH_IDS);
    let upper = feedback(serde_json::json!({ "crash_ids": [ID.to_uppercase()] }))
        .unwrap()
        .unwrap();
    assert_eq!(upper.crash_ids, vec![ID.to_owned()]);
}

#[test]
fn a_broken_id_maker_is_an_error_not_a_panic() {
    let wire = feedback_wire(serde_json::json!({"id": null})).unwrap();
    let err = Feedback::from_wire(wire, || "not-a-uuid".into()).unwrap_err();
    assert_eq!(err.field, "id");
    let wire = feedback_wire(serde_json::json!({"id": "  "})).unwrap();
    let f = Feedback::from_wire(wire, || NEW_ID.to_uppercase()).unwrap();
    assert_eq!(f.id, NEW_ID);
    // A sender's own id wins; the maker is not asked.
    let wire = feedback_wire(serde_json::json!({})).unwrap();
    let f = Feedback::from_wire(wire, String::new).unwrap();
    assert_eq!(f.id, ID);
}

#[test]
fn random_hostile_feedback_never_panics() {
    let mut rng = Rng(0x5eed_0004);
    for _ in 0..2000 {
        let extra = serde_json::json!({
            "text": rng.hostile(80), "text_en": rng.hostile(20), "system": rng.hostile(20),
            "conversation": rng.hostile(80), "contact": rng.hostile(6),
            "locale": rng.hostile(3), "program": rng.hostile(3), "version": rng.hostile(3),
            "crash_ids": [rng.hostile(3)]
        });
        let Ok(result) = feedback(extra) else {
            continue;
        };
        if let Ok(f) = result {
            assert!(f.text.chars().count() > 0);
            let _ = serde_json::to_string(&f).unwrap();
        }
    }
}

// ---------------------------------------------------- answers and errors --

#[test]
fn hostile_answers_parse_or_fail_without_panicking() {
    for body in [
        "{}",
        "{\"id\": 1}",
        "{\"id\": \"x\", \"status\": \"fixed\", \"issue_url\": null}",
        "{\"id\": \"x\", \"status\": \"\\u0000\", \"fixed_in\": [1]}",
    ] {
        let _ = serde_json::from_str::<Status>(body);
        let _ = serde_json::from_str::<CrashReceipt>(body);
    }
    let s: Status =
        serde_json::from_str("{\"id\": \"x\", \"status\": \"unknown\", \"extra\": 1}").unwrap();
    assert_eq!(s.issue_url, None);
    assert!(!serde_json::to_string(&s).unwrap().contains("issue_url"));
}

#[test]
fn refusals_read_as_field_and_reason() {
    let err = crash(serde_json::json!({"kind": "telemetry"})).unwrap_err();
    assert_eq!(err, "kind: must be signal, exception or panic");
    for kind in [CrashKind::Signal, CrashKind::Exception, CrashKind::Panic] {
        let json = serde_json::to_string(&kind).unwrap();
        assert_eq!(json, format!("\"{}\"", kind.as_str()));
    }
}

// ----------------------------------------------------------------- forget --

#[test]
fn forget_takes_only_real_ids_within_the_limit() {
    let ok = ForgetWire {
        ids: vec![format!(" {} ", ID.to_uppercase()); MAX_FORGET_IDS],
    };
    assert_eq!(ok.ids().unwrap().len(), MAX_FORGET_IDS);
    assert!(ok.ids().unwrap().iter().all(|id| id == ID));
    for ids in [
        vec![],
        vec![ID.to_owned(); MAX_FORGET_IDS + 1],
        vec![ID.to_owned(), "*".to_owned()],
        vec![ID.to_owned(), String::new()],
        vec!["%".to_owned()],
    ] {
        assert!(ForgetWire { ids }.ids().is_err());
    }
    for body in ["{\"ids\": \"*\"}", "{\"ids\": [1]}", "{\"ids\": null}"] {
        assert!(serde_json::from_str::<ForgetWire>(body).is_err(), "{body}");
    }
    let empty: ForgetWire = serde_json::from_str("{}").unwrap();
    assert_eq!(empty.ids().unwrap_err().field, "ids");
}
