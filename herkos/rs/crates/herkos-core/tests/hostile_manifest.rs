// SPDX-License-Identifier: GPL-3.0-or-later
//! Hostile manifests for herkos-core: every manifest here is written by
//! someone who wants Herkos to panic, to run out of stack, or to accept an
//! operation that is broader than its limits allow. Only the public API is
//! used, the way herkosd and its catalogue use it.
//!
//! Each case checks two things: the answer is `Err` (or `Ok` where the input
//! is valid) instead of a panic, and a manifest that is accepted really keeps
//! every limit (`assert_holds_limits`).

use herkos_core::manifest::{
    DBUS_BUSES, EXT_PREFIX, FORBIDDEN, MAX_CALL_COMMANDS, MAX_COMMANDS, MAX_DBUS_PER_MANIFEST,
    MAX_DBUS_PER_OPERATION, Manifest, ManifestError, Operation, POLKIT_PREFIX, Registry,
    is_command, is_dbus_call, is_dbus_name, is_dbus_path, is_polkit_action, parse_manifest,
    parse_manifest_str,
};
use herkos_core::policy::called_commands;
use herkos_core::request::is_name;
use herkos_core::risk::Level;
use serde_json::{Map, Value as Json, json};
use toml::{Table, Value};

/// The deepest nesting tried: far beyond any sane manifest.
const DEEP: usize = 100_000;

const BASE: &str = "tool = \"org.example.t\"\n[operations.read]\nlevel = \"R1\"\n";

fn parse(text: &str) -> Result<Manifest, ManifestError> {
    let got = parse_manifest_str(text, "hostile.toml");
    if let Ok(m) = &got {
        assert_holds_limits(m);
    }
    got
}

fn rejects(text: &str) {
    let got = parse(text);
    assert!(got.is_err(), "accepted: {text:?}");
}

fn accepts(text: &str) -> Manifest {
    match parse(text) {
        Ok(m) => m,
        Err(e) => panic!("rejected: {text:?}: {e}"),
    }
}

/// A one-operation manifest whose operation has `extra` lines.
fn op_with(extra: &str) -> String {
    format!("tool = \"org.example.t\"\n[operations.run]\nlevel = \"R3\"\n{extra}\n")
}

/// A manifest with a `[launch]` table made of `launch` lines.
fn launch_with(launch: &str) -> String {
    format!("tool = \"org.example.t\"\n[launch]\n{launch}\n[operations.read]\nlevel = \"R1\"\n")
}

const PDF: &str = "program = \"vilna-mcp-ext-pdf\"\nflatpak = \"io.github.example.pdf\"\n\
                   command = \"pdf-cli\"\nfixed = [\"mcp\"]\nargs = [\"roots\"]";

/// One D-Bus call as an inline table.
fn dbus_call(bus: &str, name: &str, path: &str, call: &str) -> String {
    format!(
        "{{ bus = {}, name = {}, path = {}, call = {} }}",
        toml_str(bus),
        toml_str(name),
        toml_str(path),
        toml_str(call)
    )
}

fn good_call() -> String {
    dbus_call(
        "session",
        "org.example.Player",
        "/org/example/Player",
        "org.example.Player.Play",
    )
}

/// `n` operations with `per_op` D-Bus calls each, the last one with `last`.
fn dbus_manifest(ops: usize, per_op: usize, last: usize) -> String {
    let mut text = String::from("tool = \"org.example.t\"\n");
    for i in 0..ops {
        let n = if i + 1 == ops { last } else { per_op };
        let calls: Vec<String> = (0..n)
            .map(|j| {
                dbus_call(
                    "session",
                    &format!("org.example.N{i}x{j}"),
                    "/",
                    "org.example.I.M",
                )
            })
            .collect();
        text.push_str(&format!(
            "[operations.op{i}]\nlevel = \"R2\"\ndbus = [{}]\n",
            calls.join(", ")
        ));
    }
    text
}

/// A TOML basic string with every character escaped that needs it.
fn toml_str(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if c.is_control() => out.push_str(&format!("\\u{:04X}", u32::from(c))),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// What every accepted manifest must keep, whatever it was made from.
fn assert_holds_limits(m: &Manifest) {
    assert!(is_name(&m.tool), "tool {:?}", m.tool);
    assert!(!m.operations.is_empty());
    let mut total = 0;
    for (name, op) in &m.operations {
        assert_eq!(name, &op.name);
        assert!(is_name(name), "operation {name:?}");
        assert!(
            op.dbus.len() <= MAX_DBUS_PER_OPERATION,
            "{name}: {}",
            op.dbus.len()
        );
        total += op.dbus.len();
        if op.level.is_none() {
            assert!(op.dbus.is_empty(), "{name}: forbidden with D-Bus calls");
            assert!(op.polkit.is_none(), "{name}: forbidden with polkit");
        }
        for call in &op.dbus {
            assert!(DBUS_BUSES.contains(&call.bus.as_str()), "{call:?}");
            assert!(is_dbus_name(&call.name), "{call:?}");
            assert!(is_dbus_path(&call.path), "{call:?}");
            assert!(is_dbus_call(&call.call), "{call:?}");
            assert!(call.name.len() <= 255 && call.path.len() <= 255 && call.call.len() <= 255);
            assert_ne!(call.name.trim_end_matches(".*"), "org.freedesktop.DBus");
        }
        if let Some(p) = &op.polkit {
            assert!(p.starts_with(POLKIT_PREFIX) && p.len() <= 128, "{p:?}");
            assert!(is_polkit_action(p), "{p:?}");
            assert!(
                op.level.is_some_and(|l| l >= Level::R3),
                "{name}: polkit below R3"
            );
        }
        for arg in [&op.command_arg, &op.command_key, &op.preview]
            .into_iter()
            .flatten()
        {
            assert!(is_name(arg), "{name}: argument {arg:?}");
        }
        if op.command_key.is_some() {
            assert!(op.command_arg.is_some());
        }
        if op.command_arg.is_some() {
            assert!(
                !m.commands.is_empty(),
                "{name}: command_arg without [commands]"
            );
        }
        if let Some(p) = &op.preview {
            let level = m.operations.get(p).and_then(|o| o.level);
            assert_eq!(level, Some(Level::R0), "{name}: preview {p:?}");
        }
        assert!(op.resource_args.iter().all(|a| !a.is_empty()));
    }
    assert!(total <= MAX_DBUS_PER_MANIFEST, "{total} D-Bus calls");
    assert!(m.dbus_calls().len() <= total);
    assert!(m.commands.len() <= MAX_COMMANDS);
    for key in m.commands.keys() {
        assert!(
            is_command(key.strip_suffix(".*").unwrap_or(key)),
            "command {key:?}"
        );
    }
    if let Some(launch) = &m.launch {
        let p = &launch.program;
        assert!(!p.is_empty() && p.len() <= 64, "program {p:?}");
        assert!(
            p.bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-'),
            "program {p:?}"
        );
        assert!(
            launch
                .args
                .iter()
                .all(|a| a == "backup_dir" || a == "roots")
        );
        if let Some(f) = &launch.flatpak {
            assert!(
                p.starts_with(EXT_PREFIX) && p.len() > EXT_PREFIX.len(),
                "{p:?}"
            );
            assert!(launch.module.is_none());
            assert!(!launch.wants("backup_dir"));
            assert!(f.fixed.len() <= 16);
            assert!((1..=4).contains(&f.root_flags.len()));
            assert!(f.root_flags.iter().all(|r| r.starts_with("--")));
            assert!(!f.command.contains('/') && !f.app.contains('/'));
            assert_eq!(total, 0, "a Flatpak program with D-Bus calls");
        }
    }
}

// --- The manifests Herkos ships -------------------------------------------

#[test]
fn every_builtin_manifest_keeps_the_limits() {
    let registry = match Registry::builtin() {
        Ok(r) => r,
        Err(e) => panic!("built-in manifests: {e}"),
    };
    let mut n = 0;
    for m in registry.iter() {
        assert_holds_limits(m);
        assert!(registry.is_builtin(&m.tool));
        n += 1;
    }
    assert!(n >= 13);
    assert!(registry.get("org.example.nothing").is_none());
    assert!(!registry.is_builtin("org.example.nothing"));
}

// --- Size and nesting -----------------------------------------------------

#[test]
fn deep_nesting_is_an_error_not_a_stack_overflow() {
    let open_close =
        |open: &str, close: &str| format!("{}1{}", open.repeat(DEEP), close.repeat(DEEP));
    let arrays = open_close("[", "]");
    let tables = open_close("{a=", "}");
    let dotted = vec!["a"; DEEP].join(".");
    for text in [
        format!("{BASE}x = {arrays}\n"),
        format!("{BASE}x = {tables}\n"),
        format!("description = {arrays}\n{BASE}"),
        format!("tool = {tables}\n[operations.read]\nlevel = \"R1\"\n"),
        format!("tool = \"t.x\"\n[operations.read]\nlevel = {arrays}\n"),
        format!("tool = \"t.x\"\n[operations.read]\nlevel = \"R1\"\ndbus = {arrays}\n"),
        format!("{BASE}{dotted} = 1\n"),
        format!("{BASE}[{dotted}]\n"),
        format!("{BASE}[[{dotted}]]\n"),
        format!("tool = \"t.x\"\n[operations.{dotted}]\nlevel = \"R1\"\n"),
        format!("{BASE}[commands]\n{dotted} = \"R0\"\n"),
    ] {
        rejects(&text);
    }
}

#[test]
fn moderate_nesting_in_unknown_places_is_rejected_by_name() {
    for depth in [1, 10, 40] {
        let arrays = format!("{}1{}", "[".repeat(depth), "]".repeat(depth));
        let tables = format!("{}1{}", "{a=".repeat(depth), "}".repeat(depth));
        for text in [
            format!("{BASE}x = {arrays}\n"),
            format!("{BASE}x = {tables}\n"),
            format!("x = {tables}\n{BASE}"),
            format!("{BASE}summary = {arrays}\n"),
            format!("{BASE}egress = {tables}\n"),
        ] {
            match parse(&text) {
                Ok(_) => panic!("accepted depth {depth}: {text}"),
                Err(e) => assert!(!e.0.is_empty()),
            }
        }
    }
}

#[test]
fn huge_input_does_not_panic_and_keeps_its_text() {
    // One megabyte of summary: no panic, and nothing silently cut or changed.
    let long = "ж".repeat(500_000);
    if let Ok(m) = parse(&format!("{BASE}summary = \"{long}\"\n")) {
        let got = m.operation("read").map(|o| o.summary.as_str());
        assert_eq!(got, Some(long.as_str()));
    }
    // Megabytes of garbage, of keys, of brackets.
    rejects(&"\u{0}".repeat(1 << 20));
    rejects(&"[".repeat(1 << 20));
    rejects(&"]".repeat(1 << 20));
    rejects(&"\"".repeat(1 << 20));
    rejects(&"a=1\n".repeat(1 << 18));
    let mut unknown = String::new();
    for i in 0..50_000 {
        unknown.push_str(&format!("k{i} = {i}\n"));
    }
    rejects(&format!("{unknown}{BASE}"));
    // Many operations: whatever the answer, each is still checked.
    let mut many = String::from("tool = \"t.x\"\n");
    for i in 0..20_000 {
        many.push_str(&format!("[operations.o{i}]\nlevel = \"R1\"\n"));
    }
    many.push_str("[operations.bad]\nlevel = \"R9\"\n");
    rejects(&many);
}

#[test]
fn toml_syntax_traps_are_errors() {
    for text in [
        "",
        " ",
        "\u{feff}",
        "tool = \"t.x\"\ntool = \"t.y\"\n[operations.read]\nlevel = \"R1\"\n",
        "tool = \"t.x\"\n[operations.read]\nlevel = \"R1\"\n[operations.read]\nlevel = \"R4\"\n",
        "tool = \"t.x\"\n[operations.read]\nlevel = \"R1\"\nlevel = \"R0\"\n",
        "tool = \"t.x\"\n[operations.read]\nlevel = \"R1\"\n[operations.\"read\"]\nsummary=\"x\"\n",
        "tool = \"t.x\"\n[operations.read]\nlevel = \"R1\u{0}\"\n",
        "tool = \"t.x\u{0}\"\n[operations.read]\nlevel = \"R1\"\n",
        "tool = \"t.x\"\n[operations.read]\nlevel = \"\\uD800\"\n",
        "tool = \"t.x\"\n[operations.read]\nlevel = \"\\U00110000\"\n",
        "tool = 't.x\n[operations.read]\nlevel = \"R1\"\n",
        "tool = \"t.x\"\n[[operations]]\nlevel = \"R1\"\n",
        "tool = \"t.x\"\noperations = [{read = {level = \"R1\"}}]\n",
        "tool = \"t.x\"\n[operations]\nread = \"R1\"\n",
        "tool = \"t.x\"\n[operations.read]\nlevel = 'R1'\nlevel.x = 1\n",
    ] {
        rejects(text);
    }
}

// --- Unknown fields and wrong types --------------------------------------

#[test]
fn unknown_fields_are_rejected_everywhere_even_look_alikes() {
    for key in [
        "x",
        "Level",
        "LEVEL",
        "\"level \"",
        "\" level\"",
        "\"lеvel\"", // Cyrillic е
        "\"level\u{200b}\"",
        "\"\"",
        "\"undoable\u{0}\"",
        "dbus_extra",
    ] {
        rejects(&format!("{BASE}{key} = true\n"));
        rejects(&format!("{key} = true\n{BASE}"));
    }
    for extra in ["x = 1", "\"Tool\" = \"t.x\"", "\"tool \" = \"t.x\""] {
        rejects(&format!("{extra}\n{BASE}"));
    }
    rejects(&launch_with("program = \"p\"\nenv = \"LD_PRELOAD=/tmp/x\""));
    rejects(&launch_with("program = \"p\"\nProgram = \"q\""));
    rejects(&op_with(
        "dbus = [{ bus = \"session\", name = \"a.b\", path = \"/\", call = \"a.b.C\", \
         flags = 1 }]",
    ));
}

#[test]
fn wrong_types_are_rejected_for_every_field() {
    let values = [
        "1",
        "-1",
        "1.5",
        "nan",
        "inf",
        "true",
        "1979-05-27T07:32:00Z",
        "[]",
        "[1]",
        "{}",
        "{ a = 1 }",
    ];
    for v in values {
        rejects(&format!("tool = {v}\n[operations.read]\nlevel = \"R1\"\n"));
        rejects(&format!("description = {v}\n{BASE}"));
        rejects(&format!("tool = \"t.x\"\noperations = {v}\n"));
        rejects(&format!("tool = \"t.x\"\n[operations.read]\nlevel = {v}\n"));
        rejects(&format!("{BASE}summary = {v}\n"));
        rejects(&format!(
            "{BASE}command_arg = {v}\n[commands]\na = \"R0\"\n"
        ));
        rejects(&format!("{BASE}command_key = {v}\n"));
        rejects(&format!("{BASE}preview = {v}\n"));
        rejects(&op_with(&format!("polkit = {v}")));
        rejects(&op_with(&format!("dbus = {v}")));
        rejects(&format!("{BASE}[commands]\na = {v}\n"));
        rejects(&format!("{BASE}commands = {v}\n"));
        rejects(&format!("launch = {v}\n{BASE}"));
        rejects(&launch_with(&format!("program = {v}")));
        rejects(&launch_with(&format!("program = \"p\"\nmodule = {v}")));
        rejects(&launch_with(&format!("{PDF}\nroot_flags = {v}")));
        for flag in ["undoable", "reversible", "returns_external", "egress"] {
            if v != "true" {
                rejects(&format!("{BASE}{flag} = {v}\n"));
            }
        }
        for key in ["bus", "name", "path", "call"] {
            let call =
                good_call().replacen(&format!("{key} = "), &format!("{key} = {v}, x{key} = "), 1);
            rejects(&op_with(&format!("dbus = [{call}]")));
        }
    }
    for flag in ["undoable", "reversible", "returns_external", "egress"] {
        for v in ["\"true\"", "\"yes\"", "1", "0"] {
            rejects(&format!("{BASE}{flag} = {v}\n"));
        }
    }
    for v in [
        "\"a\"",
        "[\"\"]",
        "[1]",
        "[[\"a\"]]",
        "[\"a\", 2]",
        "{ a = \"b\" }",
    ] {
        rejects(&format!("{BASE}resource_args = {v}\n"));
    }
    for v in [
        "\"roots\"",
        "[\"roots\", \"roots\"]",
        "[\"ROOTS\"]",
        "[\"root\"]",
        "[\"env\"]",
    ] {
        rejects(&launch_with(&format!("program = \"p\"\nargs = {v}")));
    }
}

#[test]
fn a_table_built_in_code_is_checked_like_text() {
    let mut op = Table::new();
    op.insert("level".into(), Value::Float(f64::NAN));
    let mut ops = Table::new();
    ops.insert("read".into(), Value::Table(op));
    let mut data = Table::new();
    data.insert("tool".into(), Value::String("t.x".into()));
    data.insert("operations".into(), Value::Table(ops.clone()));
    assert!(parse_manifest(&data, "built").is_err());

    let mut op = Table::new();
    op.insert("level".into(), Value::String("R1".into()));
    ops.insert("read".into(), Value::Table(op));
    // Keys TOML text could never hold as bare words.
    for name in ["", " ", "\u{0}", "a\nb", "../x", "read/../x"] {
        let mut ops = ops.clone();
        ops.insert(name.into(), Value::Table(Table::new()));
        let mut data = data.clone();
        data.insert("operations".into(), Value::Table(ops));
        assert!(parse_manifest(&data, "built").is_err(), "{name:?}");
    }
    data.insert("operations".into(), Value::Table(ops));
    match parse_manifest(&data, "built") {
        Ok(m) => assert_holds_limits(&m),
        Err(e) => panic!("{e}"),
    }
}

// --- Names ----------------------------------------------------------------

/// Names Herkos must never take for a tool or an operation.
fn hostile_names() -> Vec<String> {
    let mut names: Vec<String> = [
        "",
        " ",
        "a b",
        " a",
        "a ",
        "A",
        "Org.example.t",
        "org.Example.t",
        ".a",
        "-a",
        "_a",
        "/a",
        "a/b",
        "a\\b",
        "a:b",
        "a@b",
        "a*",
        "*",
        "a\u{0}",
        "a\n",
        "a\r",
        "a\t",
        "a\u{7f}",
        "a\u{1b}[31m",
        "a\u{202e}",
        "a\u{200b}",
        "a\u{feff}",
        "а",             // Cyrillic
        "оrg.example.t", // Cyrillic о
        "ｏrg.example.t",
        "e\u{301}",
        "é",
        "ß",
        "ǆ",
        "🦀",
        "org.example.t\u{e0041}",
    ]
    .into_iter()
    .map(str::to_owned)
    .collect();
    names.push("a".repeat(129));
    names.push(format!("a{}", "\u{301}".repeat(10_000)));
    names.push("ж".repeat(64));
    names
}

#[test]
fn tool_and_operation_names_hold_the_name_rule() {
    for name in hostile_names() {
        assert!(!is_name(&name), "{name:?}");
        let quoted = toml_str(&name);
        rejects(&format!(
            "tool = {quoted}\n[operations.read]\nlevel = \"R1\"\n"
        ));
        rejects(&format!(
            "tool = \"t.x\"\n[operations.{quoted}]\nlevel = \"R1\"\n"
        ));
        rejects(&format!(
            "{BASE}command_arg = {quoted}\n[commands]\na = \"R0\"\n"
        ));
        rejects(&format!(
            "{BASE}command_arg = \"c\"\ncommand_key = {quoted}\n[commands]\na = \"R0\"\n"
        ));
        rejects(&format!(
            "tool = \"t.x\"\n[operations.{quoted}]\nlevel = \"R0\"\n\
             [operations.put]\nlevel = \"R3\"\npreview = {quoted}\n"
        ));
    }
    // Exactly at the limit is fine, one more is not.
    let max = format!("a{}", "b".repeat(127));
    let m = accepts(&format!(
        "tool = \"{max}\"\n[operations.{max}]\nlevel = \"R1\"\n"
    ));
    assert_eq!(m.tool.len(), 128);
    assert!(m.operation(&max).is_some());
    rejects(&format!(
        "tool = \"{max}b\"\n[operations.read]\nlevel = \"R1\"\n"
    ));
}

#[test]
fn levels_are_only_r0_to_r4_or_forbidden() {
    for level in [
        "",
        " ",
        "R",
        "R5",
        "R-1",
        "R 4",
        "R04",
        "R4R4",
        "Ｒ4",
        "R４",
        "Р4", // Cyrillic Р
        "R4\u{0}",
        "R\u{200b}4",
        "4",
        "r",
        "forbidden!",
        "Forbidden",
        "FORBIDDEN",
        " forbidden",
        "forbidden ",
        "forbid",
        "deny",
        "none",
        "null",
    ] {
        let quoted = toml_str(level);
        rejects(&format!(
            "tool = \"t.x\"\n[operations.read]\nlevel = {quoted}\n"
        ));
        rejects(&format!("{BASE}[commands]\na = {quoted}\n"));
    }
    for (text, want) in [
        ("R0", Some(Level::R0)),
        ("R4", Some(Level::R4)),
        (FORBIDDEN, None),
    ] {
        let m = accepts(&format!(
            "tool = \"t.x\"\n[operations.read]\nlevel = \"{text}\"\n"
        ));
        assert_eq!(m.operation("read").map(|o| o.level), Some(want));
        assert_eq!(
            m.operation("read").map(Operation::forbidden),
            Some(want.is_none())
        );
    }
}

// --- Commands, MAX_COMMANDS and MAX_CALL_COMMANDS -------------------------

#[test]
fn command_ids_hold_their_shape() {
    let part = "a".repeat(64);
    for ok in [
        "a".to_owned(),
        "A-b_9.c".to_owned(),
        part.clone(),
        ["a"; 8].join("."),
        [part.as_str(); 8].join("."),
    ] {
        assert!(is_command(&ok), "{ok:?}");
    }
    for bad in [
        String::new(),
        ".".to_owned(),
        "a.".to_owned(),
        ".a".to_owned(),
        "a..b".to_owned(),
        "a.*".to_owned(),
        "*".to_owned(),
        "a b".to_owned(),
        "a/b".to_owned(),
        "a:b".to_owned(),
        "ж".to_owned(),
        "a\u{0}".to_owned(),
        "a\n".to_owned(),
        "ａ".to_owned(),
        format!("{part}a"),
        ["a"; 9].join("."),
        vec!["a"; 10_000].join("."),
    ] {
        assert!(!is_command(&bad), "{bad:?}");
    }
}

#[test]
fn the_commands_table_holds_max_commands() {
    let table = |n: usize| -> String {
        let mut t = String::from("[commands]\n");
        for i in 0..n {
            t.push_str(&format!("c{i} = \"R1\"\n"));
        }
        t
    };
    let m = accepts(&format!("{BASE}{}", table(MAX_COMMANDS)));
    assert_eq!(m.commands.len(), MAX_COMMANDS);
    rejects(&format!("{BASE}{}", table(MAX_COMMANDS + 1)));
    rejects(&format!("{BASE}{}", table(MAX_COMMANDS * 4)));
    rejects(&format!("{BASE}[commands]\n"));

    for key in [
        "\"*\"",
        "\".*\"",
        "\"a.*.*\"",
        "\"a*\"",
        "\"a.b*\"",
        "\"*.a\"",
        "\"a.**\"",
        "\"\"",
        "\"a b\"",
        "\"а\"",
        "\"a\\u0000\"",
    ] {
        rejects(&format!("{BASE}[commands]\n{key} = \"R0\"\n"));
    }
    let group = ["a"; 8].join(".");
    let m = accepts(&format!("{BASE}[commands]\n\"{group}.*\" = \"R2\"\n"));
    assert!(m.commands.contains_key(&format!("{group}.*")));
    rejects(&format!("{BASE}[commands]\n\"{group}.a.*\" = \"R2\"\n"));
}

#[test]
fn command_level_finds_the_nearest_group_and_nothing_else() {
    let m = accepts(&format!(
        "{BASE}[commands]\n\"file.*\" = \"R2\"\n\"file.export.*\" = \"R3\"\n\
         \"file.print\" = \"forbidden\"\nview = \"R0\"\n"
    ));
    assert_eq!(m.command_level("file.save"), (true, Some(Level::R2)));
    assert_eq!(
        m.command_level("file.export.pdf.now"),
        (true, Some(Level::R3))
    );
    assert_eq!(m.command_level("file.print"), (true, None));
    assert_eq!(m.command_level("view"), (true, Some(Level::R0)));
    for unknown in [
        "",
        ".",
        "..",
        "file",
        "File.save",
        "view.zoom",
        ".*",
        "*",
        "file .save",
        "fіle.save", // Cyrillic і
    ] {
        assert_eq!(m.command_level(unknown), (false, None), "{unknown:?}");
    }
    // The same words as a group name, but not the group itself.
    assert_eq!(m.command_level("file.*.x"), (true, Some(Level::R2)));
}

fn execute_op(command_key: bool) -> (Manifest, Operation) {
    let key = if command_key {
        "command_key = \"id\"\n"
    } else {
        ""
    };
    let m = accepts(&format!(
        "tool = \"t.x\"\n[operations.execute]\nlevel = \"R1\"\ncommand_arg = \"commands\"\n{key}\
         [commands]\n\"a.*\" = \"R1\"\n"
    ));
    let op = match m.operation("execute") {
        Some(op) => op.clone(),
        None => panic!("no execute"),
    };
    (m, op)
}

fn args(value: Json) -> Map<String, Json> {
    let mut map = Map::new();
    map.insert("commands".into(), value);
    map
}

#[test]
fn a_call_names_at_most_max_call_commands() {
    let (_, op) = execute_op(false);
    let ids = |n: usize| Json::Array((0..n).map(|i| json!(format!("a.c{i}"))).collect());
    assert_eq!(
        called_commands(&op, &args(ids(MAX_CALL_COMMANDS))).map(|c| c.len()),
        Some(MAX_CALL_COMMANDS)
    );
    assert_eq!(
        called_commands(&op, &args(ids(MAX_CALL_COMMANDS + 1))),
        None
    );
    assert_eq!(called_commands(&op, &args(ids(100_000))), None);
    assert_eq!(called_commands(&op, &args(json!([]))), None);
    assert_eq!(
        called_commands(&op, &args(json!("a.one"))),
        Some(vec!["a.one".to_owned()])
    );
    assert_eq!(called_commands(&op, &Map::new()), None);

    let nine = ["a"; 9].join(".");
    let mut nested = json!("a.x");
    for _ in 0..1000 {
        nested = json!([nested]);
    }
    for hostile in [
        json!(null),
        json!(1),
        json!(true),
        json!({}),
        json!({"id": "a.x"}),
        json!([["a.x"]]),
        json!(["a.x", 1]),
        json!(["a.x", null]),
        json!(["a.x", "a..x"]),
        json!(["a.x", "a.*"]),
        json!(["a.x", ""]),
        json!(["a.x", "а.x"]),
        json!(["a.x", "a.x\u{0}"]),
        json!(["a.x", nine]),
        json!(["a.x", "a".repeat(65)]),
        json!(["a.x", "a.".repeat(100_000)]),
        nested,
    ] {
        assert_eq!(
            called_commands(&op, &args(hostile.clone())),
            None,
            "{hostile}"
        );
    }
}

#[test]
fn a_batch_of_tables_reads_only_the_command_key() {
    let (m, op) = execute_op(true);
    let tables = |n: usize| {
        Json::Array(
            (0..n)
                .map(|i| json!({"id": format!("a.c{i}"), "x": 1}))
                .collect(),
        )
    };
    let got = called_commands(&op, &args(tables(MAX_CALL_COMMANDS)));
    assert_eq!(got.as_ref().map(Vec::len), Some(MAX_CALL_COMMANDS));
    for id in got.unwrap_or_default() {
        assert!(is_command(&id));
        assert_eq!(m.command_level(&id), (true, Some(Level::R1)));
    }
    assert_eq!(
        called_commands(&op, &args(tables(MAX_CALL_COMMANDS + 1))),
        None
    );
    for hostile in [
        json!([{"ID": "a.x"}]),
        json!([{"id ": "a.x"}]),
        json!([{"id": 1}]),
        json!([{"id": ["a.x"]}]),
        json!([{"id": {"id": "a.x"}}]),
        json!([{"id": "a.x"}, {}]),
        json!([{"id": "a x"}]),
    ] {
        assert_eq!(
            called_commands(&op, &args(hostile.clone())),
            None,
            "{hostile}"
        );
    }
}

// --- D-Bus ----------------------------------------------------------------

#[test]
fn dbus_calls_per_operation_hold_the_limit() {
    let calls = |n: usize| vec![good_call(); n].join(", ");
    let m = accepts(&op_with(&format!(
        "dbus = [{}]",
        calls(MAX_DBUS_PER_OPERATION)
    )));
    assert_eq!(
        m.operation("run").map(|o| o.dbus.len()),
        Some(MAX_DBUS_PER_OPERATION)
    );
    // The same call repeated counts every time, but is listed once.
    assert_eq!(m.dbus_calls().len(), 1);
    rejects(&op_with(&format!(
        "dbus = [{}]",
        calls(MAX_DBUS_PER_OPERATION + 1)
    )));
    rejects(&op_with(&format!("dbus = [{}]", calls(10_000))));
    rejects(&op_with("dbus = []"));
}

#[test]
fn dbus_calls_per_manifest_hold_the_limit() {
    let full = MAX_DBUS_PER_MANIFEST / MAX_DBUS_PER_OPERATION;
    let m = accepts(&dbus_manifest(
        full,
        MAX_DBUS_PER_OPERATION,
        MAX_DBUS_PER_OPERATION,
    ));
    assert_eq!(m.dbus_calls().len(), MAX_DBUS_PER_MANIFEST);
    rejects(&dbus_manifest(full + 1, MAX_DBUS_PER_OPERATION, 1));
    let m = accepts(&dbus_manifest(MAX_DBUS_PER_MANIFEST, 1, 1));
    assert_eq!(m.dbus_calls().len(), MAX_DBUS_PER_MANIFEST);
    rejects(&dbus_manifest(MAX_DBUS_PER_MANIFEST + 1, 1, 1));
    rejects(&dbus_manifest(
        200,
        MAX_DBUS_PER_OPERATION,
        MAX_DBUS_PER_OPERATION,
    ));
    // Repeats count towards the limit too.
    let same = vec![good_call(); MAX_DBUS_PER_OPERATION].join(", ");
    let mut text = String::from("tool = \"t.x\"\n");
    for i in 0..=full {
        text.push_str(&format!(
            "[operations.o{i}]\nlevel = \"R2\"\ndbus = [{same}]\n"
        ));
    }
    rejects(&text);
}

#[test]
fn dbus_bus_names_follow_the_specification() {
    let el = |n: usize| "a".repeat(n);
    let at_limit = format!("{}.{}", el(127), el(127)); // 255
    assert_eq!(at_limit.len(), 255);
    for ok in [
        "org.example.Player".to_owned(),
        "a.b".to_owned(),
        "_a.-b".to_owned(),
        "org.mpris.MediaPlayer2.*".to_owned(),
        "org.freedesktop.DBus2".to_owned(),
        at_limit.clone(),
        format!("{}.{}.*", el(126), el(126)),
    ] {
        assert!(is_dbus_name(&ok), "{ok:?}");
        accepts(&op_with(&format!(
            "dbus = [{}]",
            dbus_call("session", &ok, "/", "a.b.C")
        )));
    }
    for bad in [
        String::new(),
        "a".to_owned(),
        "a.*".to_owned(),
        "*".to_owned(),
        ".*".to_owned(),
        "*.a".to_owned(),
        "a.*.b".to_owned(),
        "a.b*".to_owned(),
        "a.b.**".to_owned(),
        "a..b".to_owned(),
        ".a.b".to_owned(),
        "a.b.".to_owned(),
        "1a.b".to_owned(),
        "a.1b".to_owned(),
        ":1.42".to_owned(),
        ":a.b".to_owned(),
        "org.freedesktop.DBus".to_owned(),
        "org.freedesktop.DBus.*".to_owned(),
        "a.b c".to_owned(),
        "a.b/c".to_owned(),
        "a.b\u{0}".to_owned(),
        "a.b\n".to_owned(),
        "а.b".to_owned(),
        "a.ｂ".to_owned(),
        format!("{at_limit}a"),
        format!("{at_limit}.*"),
        format!("{}.{}", el(1000), el(1000)),
        vec!["a"; 100_000].join("."),
    ] {
        assert!(!is_dbus_name(&bad), "{bad:?}");
        rejects(&op_with(&format!(
            "dbus = [{}]",
            dbus_call("session", &bad, "/", "a.b.C")
        )));
    }
}

#[test]
fn dbus_paths_follow_the_specification() {
    let at_limit = format!("/{}", "a".repeat(254));
    for ok in [
        "/".to_owned(),
        "/*".to_owned(),
        "/a".to_owned(),
        "/a/b_c/D9".to_owned(),
        "/a/*".to_owned(),
        "/9".to_owned(),
        at_limit.clone(),
        format!("/{}/*", "a".repeat(252)),
    ] {
        assert!(is_dbus_path(&ok), "{ok:?}");
        accepts(&op_with(&format!(
            "dbus = [{}]",
            dbus_call("system", "a.b", &ok, "a.b.C")
        )));
    }
    for bad in [
        String::new(),
        "a".to_owned(),
        "*".to_owned(),
        "//".to_owned(),
        "/a/".to_owned(),
        "/a//b".to_owned(),
        "/a-b".to_owned(),
        "/a.b".to_owned(),
        "/a b".to_owned(),
        "/*/a".to_owned(),
        "/a/**".to_owned(),
        "/a*".to_owned(),
        "/**".to_owned(),
        "/..".to_owned(),
        "/a/../b".to_owned(),
        "/ж".to_owned(),
        "/a\u{0}".to_owned(),
        "/a\n".to_owned(),
        "\\a".to_owned(),
        format!("{at_limit}a"),
        format!("{at_limit}/*"),
        "/a".repeat(100_000),
    ] {
        assert!(!is_dbus_path(&bad), "{bad:?}");
        rejects(&op_with(&format!(
            "dbus = [{}]",
            dbus_call("system", "a.b", &bad, "a.b.C")
        )));
    }
}

#[test]
fn dbus_methods_and_buses_follow_the_specification() {
    let at_limit = format!("a.b.{}", "c".repeat(251));
    for ok in [
        "a.b.C",
        "org.example.Player.Play",
        "_a._b._c",
        at_limit.as_str(),
    ] {
        assert!(is_dbus_call(ok), "{ok:?}");
    }
    for bad in [
        String::new(),
        "a".to_owned(),
        "a.B".to_owned(),
        "a.b.*".to_owned(),
        "*.b.C".to_owned(),
        "a.b.C*".to_owned(),
        "a..b.C".to_owned(),
        "a.b.C.".to_owned(),
        ".a.b.C".to_owned(),
        "1a.b.C".to_owned(),
        "a.b.1C".to_owned(),
        "a.b-c.D".to_owned(),
        "a.b.C-d".to_owned(),
        "a.b.C D".to_owned(),
        "a.b.C()".to_owned(),
        "a.b.C@/x".to_owned(),
        "a.b.C=x".to_owned(),
        "a.b.Ж".to_owned(),
        "a.b.C\u{0}".to_owned(),
        format!("{at_limit}c"),
        vec!["a"; 100_000].join("."),
    ] {
        assert!(!is_dbus_call(&bad), "{bad:?}");
        rejects(&op_with(&format!(
            "dbus = [{}]",
            dbus_call("session", "a.b", "/", &bad)
        )));
    }
    for bus in [
        "",
        "Session",
        "SYSTEM",
        " system",
        "system ",
        "user",
        "starter",
        "session\u{0}",
    ] {
        rejects(&op_with(&format!(
            "dbus = [{}]",
            dbus_call(bus, "a.b", "/", "a.b.C")
        )));
    }
}

#[test]
fn dbus_entries_must_be_complete_and_allowed() {
    for entry in [
        "{ name = \"a.b\", path = \"/\", call = \"a.b.C\" }",
        "{ bus = \"session\", path = \"/\", call = \"a.b.C\" }",
        "{ bus = \"session\", name = \"a.b\", call = \"a.b.C\" }",
        "{ bus = \"session\", name = \"a.b\", path = \"/\" }",
        "{}",
        "\"session a.b / a.b.C\"",
        "[\"session\", \"a.b\", \"/\", \"a.b.C\"]",
    ] {
        rejects(&op_with(&format!("dbus = [{entry}]")));
    }
    // A forbidden operation calls nothing; a Flatpak program filters its own buses.
    rejects(&format!(
        "tool = \"t.x\"\n[operations.run]\nlevel = \"forbidden\"\ndbus = [{}]\n",
        good_call()
    ));
    rejects(&format!(
        "tool = \"t.x\"\n[launch]\n{PDF}\n[operations.run]\nlevel = \"R2\"\ndbus = [{}]\n",
        good_call()
    ));
    // What is accepted reaches xdg-dbus-proxy as one argument with no room to spare.
    let m = accepts(&op_with(&format!("dbus = [{}]", good_call())));
    let rules: Vec<String> = m.dbus_calls().iter().map(|c| c.proxy_rule()).collect();
    assert_eq!(
        rules,
        ["--call=org.example.Player=org.example.Player.Play@/org/example/Player"]
    );
}

// --- polkit ---------------------------------------------------------------

#[test]
fn polkit_actions_stay_under_the_herkos_prefix() {
    let at_limit = format!("{POLKIT_PREFIX}{}", "a".repeat(128 - POLKIT_PREFIX.len()));
    for ok in [
        "org.vilna.herkos.install",
        "org.vilna.herkos.system.update-all",
        "org.vilna.herkos.a9.b-c-d",
        at_limit.as_str(),
    ] {
        assert!(is_polkit_action(ok), "{ok:?}");
        let m = accepts(&op_with(&format!("polkit = \"{ok}\"")));
        assert_eq!(
            m.operation("run").and_then(|o| o.polkit.as_deref()),
            Some(ok)
        );
    }
    for bad in [
        String::new(),
        POLKIT_PREFIX.to_owned(),
        "org.vilna.herkos".to_owned(),
        "org.vilna.herkosx".to_owned(),
        "org.vilna.herkos-x".to_owned(),
        "ORG.vilna.herkos.x".to_owned(),
        "org.Vilna.herkos.x".to_owned(),
        "org.vilna.herkos.X".to_owned(),
        "org.vilna.herkos..x".to_owned(),
        "org.vilna.herkos.x.".to_owned(),
        "org.vilna.herkos.x..y".to_owned(),
        "org.vilna.herkos.-x".to_owned(),
        "org.vilna.herkos.x-".to_owned(),
        "org.vilna.herkos.x_y".to_owned(),
        "org.vilna.herkos.x y".to_owned(),
        "org.vilna.herkos./x".to_owned(),
        "org.vilna.herkos.x/../../org.freedesktop.systemd1.manage-units".to_owned(),
        "org.vilna.herkos.*".to_owned(),
        "org.vilna.herkos.ж".to_owned(),
        "org.vilna.herkos.x\u{0}".to_owned(),
        "org.vilna.herkos.x\n".to_owned(),
        " org.vilna.herkos.x".to_owned(),
        "оrg.vilna.herkos.x".to_owned(), // Cyrillic о
        "org.freedesktop.systemd1.manage-units".to_owned(),
        "org.freedesktop.policykit.exec".to_owned(),
        "org.vilna.herkos".to_owned() + "\u{2024}x", // one dot leader, not a dot
        format!("{at_limit}a"),
        format!("{POLKIT_PREFIX}{}", "a".repeat(100_000)),
    ] {
        assert!(!is_polkit_action(&bad), "{bad:?}");
        rejects(&op_with(&format!("polkit = {}", toml_str(&bad))));
    }
}

#[test]
fn polkit_goes_only_with_r3_and_r4() {
    for level in ["R0", "R1", "R2", FORBIDDEN] {
        rejects(&format!(
            "tool = \"t.x\"\n[operations.run]\nlevel = \"{level}\"\npolkit = \"org.vilna.herkos.x\"\n"
        ));
    }
    for level in ["R3", "R4"] {
        accepts(&format!(
            "tool = \"t.x\"\n[operations.run]\nlevel = \"{level}\"\npolkit = \"org.vilna.herkos.x\"\n"
        ));
    }
}

// --- Launch and EXT_PREFIX ------------------------------------------------

#[test]
fn a_flatpak_program_must_use_the_ext_prefix_exactly() {
    let m = accepts(&launch_with(PDF));
    assert_eq!(
        m.launch.as_ref().map(|l| l.program.as_str()),
        Some("vilna-mcp-ext-pdf")
    );
    let long_ok = format!("{EXT_PREFIX}{}", "a".repeat(64 - EXT_PREFIX.len()));
    accepts(&launch_with(&PDF.replace("vilna-mcp-ext-pdf", &long_ok)));
    for program in [
        String::new(),
        EXT_PREFIX.to_owned(),
        "vilna-mcp-ext".to_owned(),
        "vilna-mcp-pdf".to_owned(),
        "Vilna-mcp-ext-pdf".to_owned(),
        "VILNA-MCP-EXT-PDF".to_owned(),
        "vilna-mcp-ext-PDF".to_owned(),
        "vilna_mcp_ext_pdf".to_owned(),
        "vilna-mcp-ext-..".to_owned(),
        "vilna-mcp-ext-../pdf".to_owned(),
        "../vilna-mcp-ext-pdf".to_owned(),
        "vilna-mcp-ext-a/b".to_owned(),
        "/usr/bin/vilna-mcp-ext-pdf".to_owned(),
        "vilna-mcp-ext- pdf".to_owned(),
        " vilna-mcp-ext-pdf".to_owned(),
        "vilna-mcp-ext-pdf ".to_owned(),
        "vilna-mcp-ext-pdf\t".to_owned(),
        "vilna-mcp-ext-pdf\n".to_owned(),
        "vilna-mcp-ext-pdf\u{0}".to_owned(),
        "vilna-mcp-ext-pdf@1000".to_owned(),
        "vilna-mcp-ext-pdf.service".to_owned(),
        "vilna-mcp-ext-pdf;rm".to_owned(),
        "vilna-mcp-ext-pdf$x".to_owned(),
        "vilna-mcp-еxt-pdf".to_owned(), // Cyrillic е
        "vilna-mcp-ext-pdf\u{200b}".to_owned(),
        "vilna-mcp-ext-ｐdf".to_owned(),
        format!("{long_ok}a"),
        format!("{EXT_PREFIX}{}", "a".repeat(100_000)),
    ] {
        rejects(&launch_with(
            &PDF.replace("\"vilna-mcp-ext-pdf\"", &toml_str(&program)),
        ));
    }
}

#[test]
fn flatpak_launch_fields_take_only_safe_words() {
    let fixed = |n: usize| vec!["\"mcp\""; n].join(", ");
    accepts(&launch_with(
        &PDF.replace("[\"mcp\"]", &format!("[{}]", fixed(16))),
    ));
    rejects(&launch_with(
        &PDF.replace("[\"mcp\"]", &format!("[{}]", fixed(17))),
    ));
    let flags = |n: usize| vec!["\"--root\""; n].join(", ");
    accepts(&launch_with(&format!("{PDF}\nroot_flags = [{}]", flags(4))));
    for (from, to) in [
        ("\"io.github.example.pdf\"", "\"io.github\""),
        ("\"io.github.example.pdf\"", "\"io.github..pdf\""),
        ("\"io.github.example.pdf\"", "\"io.github.example/../pdf\""),
        (
            "\"io.github.example.pdf\"",
            "\"io.github.example.pdf --filesystem=host\"",
        ),
        ("\"io.github.example.pdf\"", "\"9o.github.example\""),
        ("\"io.github.example.pdf\"", "\"io.github.ж.pdf\""),
        ("\"pdf-cli\"", "\"/usr/bin/pdf-cli\""),
        ("\"pdf-cli\"", "\"pdf cli\""),
        ("\"pdf-cli\"", "\"PDF\""),
        ("\"pdf-cli\"", "\"\""),
        ("[\"mcp\"]", "[\"--filesystem=host\"]"),
        ("[\"mcp\"]", "[\"mcp --root /\"]"),
        ("[\"mcp\"]", "[\"--\"]"),
        ("[\"mcp\"]", "[\"-\"]"),
        ("[\"mcp\"]", "[\"---x\"]"),
        ("[\"mcp\"]", "[\"\"]"),
        ("[\"mcp\"]", "[\"MCP\"]"),
        ("[\"mcp\"]", "[\"mcp\\u0000\"]"),
        ("[\"roots\"]", "[\"backup_dir\", \"roots\"]"),
    ] {
        rejects(&launch_with(&PDF.replace(from, to)));
    }
    for flags in [
        "[]",
        "[\"--root\", \"--root\", \"--root\", \"--root\", \"--root\"]",
        "[\"-r\"]",
        "[\"--root=x\"]",
        "[\"--Root\"]",
        "[\"--\"]",
        "[\"--ж\"]",
        "[\"root\"]",
    ] {
        rejects(&format!(
            "tool = \"t.x\"\n[launch]\n{PDF}\nroot_flags = {flags}\n[operations.read]\nlevel = \"R1\"\n"
        ));
    }
    // Flatpak words without 'flatpak', and 'module' with it.
    for extra in [
        "command = \"x\"",
        "fixed = [\"mcp\"]",
        "root_flags = [\"--root\"]",
    ] {
        rejects(&launch_with(&format!("program = \"p\"\n{extra}")));
    }
    rejects(&launch_with(&format!("{PDF}\nmodule = \"x\"")));
}

#[test]
fn launch_programs_and_modules_are_plain_names() {
    accepts(&launch_with(
        "program = \"vilna-mcp-files\"\nmodule = \"herkos.tools.files\"",
    ));
    for program in [
        "", "/bin/sh", "sh -c", "Files", "-x", "a_b", "a.b", "ж", "a\u{0}",
    ] {
        rejects(&launch_with(&format!("program = {}", toml_str(program))));
    }
    rejects(&launch_with(&format!("program = \"{}\"", "a".repeat(65))));
    for module in [
        "",
        "a..b",
        "a.b.c.d.e",
        "A",
        "1a",
        "a-b",
        "a/b",
        "os;x",
        "ж",
    ] {
        rejects(&launch_with(&format!(
            "program = \"p\"\nmodule = {}",
            toml_str(module)
        )));
    }
}

// --- Cross-field rules ----------------------------------------------------

#[test]
fn cross_field_rules_hold() {
    rejects(&format!("{BASE}command_key = \"id\"\n"));
    rejects(&format!("{BASE}command_arg = \"c\"\n"));
    rejects("tool = \"t.x\"\n");
    rejects("tool = \"t.x\"\noperations = {}\n");
    rejects("[operations.read]\nlevel = \"R1\"\n");
    let base = "tool = \"t.x\"\n[operations.look]\nlevel = \"R0\"\n\
                [operations.gone]\nlevel = \"forbidden\"\n[operations.put]\nlevel = \"R3\"\n";
    for preview in [
        "\"put\"",
        "\"gone\"",
        "\"missing\"",
        "\"Look\"",
        "\"look \"",
    ] {
        rejects(&format!("{base}preview = {preview}\n"));
    }
    let m = accepts(&format!("{base}preview = \"look\"\n"));
    assert_eq!(
        m.operation("put").and_then(|o| o.preview.as_deref()),
        Some("look")
    );
}

#[test]
fn registry_refuses_a_second_manifest_for_the_same_tool() {
    let one = accepts(BASE);
    let mut registry = Registry::default();
    assert!(registry.add(one.clone()).is_ok());
    assert!(registry.add(one).is_err());
    assert_eq!(registry.iter().count(), 1);
    let builtin = match Registry::builtin() {
        Ok(r) => r,
        Err(e) => panic!("{e}"),
    };
    let mut registry = builtin.clone();
    for m in builtin.iter() {
        assert!(registry.add(m.clone()).is_err(), "{}", m.tool);
    }
}

// --- Random hostile manifests ---------------------------------------------

/// A small deterministic generator, so a failure can be replayed.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }

    fn below(&mut self, n: usize) -> usize {
        usize::try_from(self.next() % (n as u64)).unwrap_or(0)
    }
}

#[test]
fn random_mutations_never_panic_and_never_break_a_limit() {
    let seed = format!(
        "tool = \"org.example.t\"\ndescription = \"T\"\n\
         [launch]\nprogram = \"vilna-mcp-t\"\nargs = [\"roots\"]\n\
         [commands]\n\"a.*\" = \"R1\"\n\"a.b\" = \"forbidden\"\n\
         [operations.look]\nlevel = \"R0\"\n\
         [operations.run]\nlevel = \"R3\"\nsummary = \"Run\"\nundoable = true\n\
         resource_args = [\"path\"]\ncommand_arg = \"c\"\ncommand_key = \"id\"\n\
         preview = \"look\"\npolkit = \"org.vilna.herkos.run\"\ndbus = [{}]\n",
        good_call()
    );
    accepts(&seed);
    let pieces = [
        "\"",
        "'",
        "[",
        "]",
        "{",
        "}",
        "=",
        ".",
        ",",
        "\n",
        "*",
        "/",
        "-",
        "R4",
        "R9",
        "forbidden",
        "true",
        "1",
        "\u{0}",
        "\u{202e}",
        "ж",
        "..",
        "org.vilna.herkos.",
        EXT_PREFIX,
        "org.freedesktop.DBus",
        "\\u0000",
        "[[",
        "]]",
        " ",
        "#",
    ];
    let mut rng = Rng(0x9e37_79b9_7f4a_7c15);
    let mut accepted = 0;
    for _ in 0..4000 {
        let mut text: Vec<char> = seed.chars().collect();
        for _ in 0..=rng.below(4) {
            let at = rng.below(text.len() + 1);
            match rng.below(3) {
                0 if at < text.len() => {
                    let len = rng.below(8).min(text.len() - at);
                    text.drain(at..at + len);
                }
                1 => {
                    let piece = pieces[rng.below(pieces.len())];
                    text.splice(at..at, piece.chars());
                }
                _ => {
                    let c = char::from_u32(u32::try_from(rng.below(0x3000)).unwrap_or(0))
                        .unwrap_or('?');
                    text.insert(at.min(text.len()), c);
                }
            }
        }
        let text: String = text.into_iter().collect();
        if parse(&text).is_ok() {
            accepted += 1;
        }
    }
    // Some mutations hit comments or values and stay valid; most must not.
    assert!(accepted < 4000);
}
