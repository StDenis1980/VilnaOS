// SPDX-License-Identifier: GPL-3.0-or-later
//! The wire format of Vilna crash reports and feedback (ADR 0018), shared by
//! the server (`reports.vilnaos.com`) and the device-side collector
//! `vilna-reportd`. `reports/docs/protocol.md` describes it in words.
//!
//! Every value that arrives is hostile: [`Crash::from_wire`] and
//! [`Feedback::from_wire`] keep only known fields, bound each one, drop
//! optional fields that do not look right, and scrub free text a second time
//! (the device scrubbed it first). What they return is what the server stores.

use std::sync::LazyLock;

use regex::Regex;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// The only schema version this crate reads and writes.
pub const SCHEMA: u32 = 1;
/// The largest request body the server reads (ADR 0018 section 4).
pub const MAX_BODY_BYTES: usize = 64 * 1024;
pub const MAX_FRAMES: usize = 64;
pub const MAX_TEXT_CHARS: usize = 8000;
pub const MAX_SYSTEM_CHARS: usize = 4000;
pub const MAX_CRASH_IDS: usize = 10;
pub const MAX_FORGET_IDS: usize = 100;
/// Frames that make up the signature: deeper frames differ between callers.
pub const SIGNATURE_FRAMES: usize = 8;

/// Why a request was refused, worded so the sender can fix it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Invalid {
    pub field: &'static str,
    pub why: &'static str,
}

impl std::fmt::Display for Invalid {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.field, self.why)
    }
}

impl std::error::Error for Invalid {}

fn invalid(field: &'static str, why: &'static str) -> Invalid {
    Invalid { field, why }
}

/// A compiled constant pattern. `None` only if the pattern itself is broken,
/// which the tests rule out; callers then fail closed.
fn re(pattern: &str) -> Option<Regex> {
    Regex::new(pattern).ok()
}

fn matches(pattern: &LazyLock<Option<Regex>>, value: &str) -> bool {
    pattern.as_ref().is_some_and(|r| r.is_match(value))
}

static UUID_V4: LazyLock<Option<Regex>> =
    LazyLock::new(|| re(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"));
static PROGRAM: LazyLock<Option<Regex>> =
    LazyLock::new(|| re(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$"));
static VERSION: LazyLock<Option<Regex>> =
    LazyLock::new(|| re(r"^[A-Za-z0-9][A-Za-z0-9._+~:-]{0,63}$"));
static IMAGE: LazyLock<Option<Regex>> =
    LazyLock::new(|| re(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}$"));
static SIGNAL: LazyLock<Option<Regex>> = LazyLock::new(|| re(r"^SIG[A-Z0-9]{2,10}$"));
static EXCEPTION: LazyLock<Option<Regex>> = LazyLock::new(|| re(r"^[A-Za-z_][A-Za-z0-9_.]{0,99}$"));
static ARCH: LazyLock<Option<Regex>> = LazyLock::new(|| re(r"^[a-z0-9_]{1,16}$"));
static LOCALE: LazyLock<Option<Regex>> = LazyLock::new(|| re(r"^[a-z]{2,3}([_-][A-Za-z]{2,4})?$"));
static MODULE: LazyLock<Option<Regex>> =
    LazyLock::new(|| re(r"^[A-Za-z0-9_][A-Za-z0-9._+-]{0,127}$"));
static BUILD_ID: LazyLock<Option<Regex>> = LazyLock::new(|| re(r"^[0-9a-f]{8,128}$"));
static OFFSET: LazyLock<Option<Regex>> = LazyLock::new(|| re(r"^0x[0-9a-f]{1,16}$"));
static SOURCE_FILE: LazyLock<Option<Regex>> =
    LazyLock::new(|| re(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}$"));
static EMAIL: LazyLock<Option<Regex>> =
    LazyLock::new(|| re(r"^[^\s@<>]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$"));

// Scrubbing patterns, applied in this order.
static SCRUB_EMAIL: LazyLock<Option<Regex>> = LazyLock::new(|| re(r"[\w.+-]+@[\w-]+(\.[\w-]+)+"));
static SCRUB_IPV4: LazyLock<Option<Regex>> = LazyLock::new(|| re(r"\b\d{1,3}(\.\d{1,3}){3}\b"));
static SCRUB_IPV6: LazyLock<Option<Regex>> =
    LazyLock::new(|| re(r"\b[0-9A-Fa-f]{1,4}(:[0-9A-Fa-f]{0,4}){2,7}\b"));
static SCRUB_MAC: LazyLock<Option<Regex>> =
    LazyLock::new(|| re(r"\b[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}\b"));
static SCRUB_DIGITS: LazyLock<Option<Regex>> = LazyLock::new(|| re(r"\d{6,}"));
static RUST_PLACE: LazyLock<Option<Regex>> =
    LazyLock::new(|| re(r"[A-Za-z0-9_./-]*\.rs:\d+(:\d+)?$"));
static LINE_NUMBERS: LazyLock<Option<Regex>> = LazyLock::new(|| re(r":\d+(:\d+)?"));

/// The first `max` characters of `text`, cut at a character boundary.
pub fn truncate(text: &str, max: usize) -> String {
    text.chars().take(max).collect()
}

fn replace(pattern: &LazyLock<Option<Regex>>, text: &str, with: &str) -> Option<String> {
    pattern
        .as_ref()
        .map(|r| r.replace_all(text, with).into_owned())
}

fn scrub_token(token: &str) -> String {
    // A Rust source place stays from its `crates/` or `src/` folder on, as
    // `crates/x/src/y.rs:12`; the build machine's folders before it go.
    if let Some(place) = RUST_PLACE.as_ref().and_then(|r| r.find(token)) {
        let place = place.as_str();
        let start = place.find("crates/").or_else(|| place.find("src/"));
        if let Some(rest) = start.and_then(|i| place.get(i..)) {
            return rest.to_owned();
        }
    }
    let looks_like_path = token.starts_with('/')
        || token.starts_with("~/")
        || token.starts_with("\\\\")
        || token.contains(":\\")
        || token.contains("://")
        || token.matches('/').count() >= 2
        || token.matches('\\').count() >= 2;
    if looks_like_path {
        "<path>".to_owned()
    } else {
        token.to_owned()
    }
}

/// The server's second pass over free text: e-mail addresses, IP and MAC
/// addresses, paths and runs of six or more digits (ids, phone and account
/// numbers) are replaced by `<email>`, `<ip>`, `<mac>`, `<path>` and `<n>`.
/// One relative file inside our own code (`gloxi/chat.py`) stays.
pub fn scrub(text: &str, max_chars: usize) -> String {
    let text = truncate(text, max_chars);
    let step =
        |t: Option<String>, p: &LazyLock<Option<Regex>>, w: &str| t.and_then(|t| replace(p, &t, w));
    let mut out = Some(text);
    out = step(out, &SCRUB_EMAIL, "<email>");
    out = step(out, &SCRUB_MAC, "<mac>");
    out = step(out, &SCRUB_IPV4, "<ip>");
    out = step(out, &SCRUB_IPV6, "<ip>");
    let Some(text) = out else {
        return "<scrubbed>".to_owned();
    };
    let mut joined = String::with_capacity(text.len());
    let mut token = String::new();
    for c in text.chars() {
        if c.is_whitespace() {
            joined.push_str(&scrub_token(&token));
            token.clear();
            joined.push(c);
        } else {
            token.push(c);
        }
    }
    joined.push_str(&scrub_token(&token));
    replace(&SCRUB_DIGITS, &joined, "<n>").unwrap_or_else(|| "<scrubbed>".to_owned())
}

/// A lowercase random (version 4) UUID, or `None`.
pub fn report_id(value: &str) -> Option<String> {
    let value = value.trim().to_ascii_lowercase();
    matches(&UUID_V4, &value).then_some(value)
}

fn checked(value: Option<String>, pattern: &LazyLock<Option<Regex>>) -> Option<String> {
    value
        .map(|v| v.trim().to_owned())
        .filter(|v| matches(pattern, v))
}

/// Memory rounded up to a power of two, so it does not single out a device.
pub fn round_memory_gb(gb: u32) -> Option<u32> {
    if gb == 0 || gb > 4096 {
        None
    } else {
        gb.checked_next_power_of_two()
    }
}

// ---------------------------------------------------------------- crashes --

/// How the program reached the device.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Packaging {
    Rpm,
    Flatpak,
    Other,
}

/// What kind of failure the report describes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CrashKind {
    /// A native program died on a signal (`systemd-coredump`).
    Signal,
    /// An unhandled Python exception (Gloxi, Herkos-lite).
    Exception,
    /// A Rust panic (`herkosd`).
    Panic,
}

impl CrashKind {
    pub fn as_str(self) -> &'static str {
        match self {
            CrashKind::Signal => "signal",
            CrashKind::Exception => "exception",
            CrashKind::Panic => "panic",
        }
    }
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct ProgramWire {
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub version: Option<String>,
    #[serde(default)]
    pub packaging: Option<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct FrameWire {
    pub module: Option<String>,
    pub build_id: Option<String>,
    pub offset: Option<String>,
    pub file: Option<String>,
    pub line: Option<u32>,
    pub function: Option<String>,
}

/// A crash report as a device sends it (`POST /v1/crash`). Unknown fields are
/// ignored, so newer devices can add fields without being refused.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct CrashWire {
    #[serde(default)]
    pub schema: u32,
    #[serde(default)]
    pub id: String,
    #[serde(default)]
    pub program: ProgramWire,
    pub image: Option<String>,
    #[serde(default)]
    pub kind: String,
    pub signal: Option<String>,
    pub exception: Option<String>,
    pub location: Option<String>,
    #[serde(default)]
    pub frames: Vec<FrameWire>,
    pub locale: Option<String>,
    pub arch: Option<String>,
    pub memory_gb: Option<u32>,
}

/// One stack frame. Native frames carry `module`, `build_id` and `offset`
/// (the server adds `function` from debug data later); Python and Rust frames
/// carry `file`, `line` and `function`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Frame {
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub module: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub build_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub offset: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub file: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub line: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub function: Option<String>,
}

impl Frame {
    fn from_wire(w: FrameWire) -> Option<Frame> {
        let module = checked(w.module, &MODULE);
        let build_id = checked(w.build_id.map(|b| b.to_ascii_lowercase()), &BUILD_ID);
        let offset = checked(w.offset.map(|o| o.to_ascii_lowercase()), &OFFSET);
        let file = checked(w.file, &SOURCE_FILE).filter(|f| !f.split('/').any(|part| part == ".."));
        let line = w.line.filter(|l| *l > 0);
        let function = w
            .function
            .map(|f| scrub(f.trim(), 200))
            .filter(|f| !f.is_empty());
        let frame = Frame {
            module,
            build_id,
            offset,
            file,
            line,
            function,
        };
        (frame != Frame::default()).then_some(frame)
    }

    /// The part of the frame that stays the same between builds.
    fn signature_part(&self) -> String {
        let place = self
            .file
            .as_deref()
            .or(self.module.as_deref())
            .unwrap_or("?");
        match (&self.function, &self.offset) {
            (Some(function), _) => format!("{place}!{function}"),
            (None, Some(offset)) => format!("{place}+{offset}"),
            (None, None) => place.to_owned(),
        }
    }
}

/// A crash report as the server stores it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Crash {
    pub schema: u32,
    pub id: String,
    pub program: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub version: Option<String>,
    pub packaging: Packaging,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub image: Option<String>,
    pub kind: CrashKind,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub signal: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub exception: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub location: Option<String>,
    pub frames: Vec<Frame>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub locale: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub arch: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub memory_gb: Option<u32>,
}

impl Crash {
    /// Checks and bounds a report. Refuses it only when a required field
    /// (schema, id, program name, kind) is missing or wrong; an optional field
    /// that does not look right is dropped.
    pub fn from_wire(w: CrashWire) -> Result<Crash, Invalid> {
        if w.schema != SCHEMA {
            return Err(invalid("schema", "must be 1"));
        }
        let id = report_id(&w.id).ok_or(invalid(
            "id",
            "must be a random (version 4) UUID made for this report",
        ))?;
        let program = checked(Some(w.program.name), &PROGRAM).ok_or(invalid(
            "program.name",
            "must be an RPM package name or a Flatpak id (letters, digits, . _ + -, up to 128)",
        ))?;
        let kind = match w.kind.as_str() {
            "signal" => CrashKind::Signal,
            "exception" => CrashKind::Exception,
            "panic" => CrashKind::Panic,
            _ => return Err(invalid("kind", "must be signal, exception or panic")),
        };
        let packaging = match w.program.packaging.as_deref() {
            Some("rpm") => Packaging::Rpm,
            Some("flatpak") => Packaging::Flatpak,
            _ => Packaging::Other,
        };
        let frames = w
            .frames
            .into_iter()
            .take(MAX_FRAMES)
            .filter_map(Frame::from_wire)
            .collect();
        Ok(Crash {
            schema: SCHEMA,
            id,
            program,
            version: checked(w.program.version, &VERSION),
            packaging,
            image: checked(w.image, &IMAGE),
            kind,
            signal: checked(w.signal, &SIGNAL).filter(|_| kind == CrashKind::Signal),
            exception: checked(w.exception, &EXCEPTION).filter(|_| kind == CrashKind::Exception),
            location: w
                .location
                .map(|l| scrub(l.trim(), 300))
                .filter(|l| !l.is_empty() && kind == CrashKind::Panic),
            frames,
            locale: checked(w.locale, &LOCALE),
            arch: checked(w.arch, &ARCH),
            memory_gb: w.memory_gb.and_then(round_memory_gb),
        })
    }

    /// Sixteen hex digits naming "the same crash": program, kind, signal or
    /// exception, place and the top frames, without line numbers, versions or
    /// addresses that move between builds of the same code.
    pub fn signature(&self) -> String {
        let place = self
            .location
            .as_deref()
            .map(|l| replace(&LINE_NUMBERS, l, "").unwrap_or_default());
        let top: Vec<String> = self
            .frames
            .iter()
            .take(SIGNATURE_FRAMES)
            .map(Frame::signature_part)
            .collect();
        let key = [
            self.program.as_str(),
            self.kind.as_str(),
            self.signal
                .as_deref()
                .or(self.exception.as_deref())
                .unwrap_or(""),
            place.as_deref().unwrap_or(""),
            &top.join("|"),
        ]
        .join("\n");
        hex16(&key)
    }

    /// A one-line title for the issue: `gloxi: KeyError in gloxi/chat.py!run`.
    pub fn title(&self) -> String {
        let what = self
            .signal
            .as_deref()
            .or(self.exception.as_deref())
            .unwrap_or(if self.kind == CrashKind::Panic {
                "panic"
            } else {
                "crash"
            });
        let place = self
            .location
            .clone()
            .or_else(|| self.frames.first().map(Frame::signature_part))
            .unwrap_or_else(|| "unknown place".to_owned());
        truncate(&format!("{}: {what} in {place}", self.program), 120)
    }
}

fn hex16(key: &str) -> String {
    let digest = Sha256::digest(key.as_bytes());
    digest.iter().take(8).map(|b| format!("{b:02x}")).collect()
}

// --------------------------------------------------------------- feedback --

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum FeedbackKind {
    Bug,
    Idea,
    Other,
}

/// Where the feedback was written (ADR 0018 section 5).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Channel {
    /// Gloxi in the Vilna image, sent as an R4 action through Herkos.
    Gloxi,
    /// The "Feedback about Vilna" window.
    Window,
    /// The form on vilnaos.com, including the one Flatpak Gloxi fills in.
    Site,
}

/// Feedback as it arrives (`POST /v1/feedback`), as JSON or as the site's
/// form. The site form has no `id`; the server makes one then.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct FeedbackWire {
    #[serde(default)]
    pub schema: Option<u32>,
    pub id: Option<String>,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub text: String,
    pub text_en: Option<String>,
    pub system: Option<String>,
    #[serde(default)]
    pub crash_ids: Vec<String>,
    #[serde(default)]
    pub channel: String,
    pub locale: Option<String>,
    pub program: Option<String>,
    pub version: Option<String>,
    pub contact: Option<String>,
    #[serde(default)]
    pub may_publish: bool,
}

/// Feedback as the server stores it. The contact address is kept apart from
/// the text (`contact` is taken out before storing the body).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Feedback {
    pub schema: u32,
    pub id: String,
    pub kind: FeedbackKind,
    pub channel: Channel,
    pub text: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub text_en: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub system: Option<String>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub crash_ids: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub locale: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub program: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub version: Option<String>,
    pub may_publish: bool,
    #[serde(skip)]
    pub contact: Option<String>,
}

impl Feedback {
    /// Checks feedback. `new_id` is used when the sender gave none (the site
    /// form); a JSON sender must give its own, so it can later forget it.
    pub fn from_wire(
        w: FeedbackWire,
        new_id: impl FnOnce() -> String,
    ) -> Result<Feedback, Invalid> {
        if w.schema.is_some_and(|s| s != SCHEMA) {
            return Err(invalid("schema", "must be 1"));
        }
        let id = match w.id.as_deref().map(str::trim) {
            None | Some("") => report_id(&new_id())
                .ok_or(invalid("id", "the server could not make an id; try again"))?,
            Some(id) => report_id(id).ok_or(invalid(
                "id",
                "must be a random (version 4) UUID made for this feedback",
            ))?,
        };
        let kind = match w.kind.as_str() {
            "bug" => FeedbackKind::Bug,
            "idea" => FeedbackKind::Idea,
            "other" | "" => FeedbackKind::Other,
            _ => return Err(invalid("kind", "must be bug, idea or other")),
        };
        let channel = match w.channel.as_str() {
            "gloxi" => Channel::Gloxi,
            "window" => Channel::Window,
            "site" | "" => Channel::Site,
            _ => return Err(invalid("channel", "must be gloxi, window or site")),
        };
        if w.text.trim().is_empty() {
            return Err(invalid(
                "text",
                "is empty: write what happened or what you suggest",
            ));
        }
        if w.text.chars().count() > MAX_TEXT_CHARS {
            return Err(invalid("text", "is longer than 8000 characters"));
        }
        let contact = match w.contact.as_deref().map(str::trim) {
            None | Some("") => None,
            Some(c) if c.len() <= 254 && matches(&EMAIL, c) => Some(c.to_owned()),
            Some(_) => {
                return Err(invalid(
                    "contact",
                    "is not an e-mail address; leave it empty for no reply",
                ));
            }
        };
        let mut crash_ids = Vec::new();
        for id in w.crash_ids.iter().take(MAX_CRASH_IDS) {
            crash_ids.push(report_id(id).ok_or(invalid(
                "crash_ids",
                "must be the ids of crash reports (UUIDs)",
            ))?);
        }
        let optional_text =
            |t: Option<String>, max| t.map(|t| scrub(t.trim(), max)).filter(|t| !t.is_empty());
        Ok(Feedback {
            schema: SCHEMA,
            id,
            kind,
            channel,
            text: scrub(w.text.trim(), MAX_TEXT_CHARS),
            text_en: optional_text(w.text_en, MAX_TEXT_CHARS),
            system: optional_text(w.system, MAX_SYSTEM_CHARS),
            crash_ids,
            locale: checked(w.locale, &LOCALE),
            program: checked(w.program, &PROGRAM),
            version: checked(w.version, &VERSION),
            may_publish: w.may_publish,
            contact,
        })
    }
}

// ------------------------------------------------------- forget and status --

/// `POST /v1/forget`: delete these reports and feedback (GDPR erasure). The
/// ids are random and kept only on the device that sent them, so knowing one
/// is what proves the request comes from that device.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct ForgetWire {
    #[serde(default)]
    pub ids: Vec<String>,
}

impl ForgetWire {
    pub fn ids(&self) -> Result<Vec<String>, Invalid> {
        if self.ids.is_empty() {
            return Err(invalid(
                "ids",
                "is empty: give the ids of the reports to delete",
            ));
        }
        if self.ids.len() > MAX_FORGET_IDS {
            return Err(invalid(
                "ids",
                "holds more than 100 ids; send them in several requests",
            ));
        }
        self.ids
            .iter()
            .map(|id| report_id(id).ok_or(invalid("ids", "must be UUIDs")))
            .collect()
    }
}

/// The answer to `POST /v1/crash`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CrashReceipt {
    pub id: String,
    pub signature: String,
    /// `new`: the first report of this crash; `known`: we have seen it before;
    /// `fixed`: it is fixed in `fixed_in`.
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub fixed_in: Option<String>,
}

/// The answer to `GET /v1/status/<id>`: what we know about this one report's
/// crash, and nothing about other people.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Status {
    pub id: String,
    /// `unknown` (no such report, or older than 90 days), `received`, `known`
    /// (tracked in a public issue) or `fixed`.
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub issue_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub fixed_in: Option<String>,
}

#[cfg(test)]
mod tests;
