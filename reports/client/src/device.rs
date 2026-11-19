// SPDX-License-Identifier: GPL-3.0-or-later
//! What a report says about the device, and nothing more (ADR 0018 section
//! 2): the Vilna image, the language of the interface (only the language,
//! not the region), the architecture and the memory, rounded. Plus random
//! report ids: there is no device id.

use std::path::Path;

use crate::coredump::Device;

/// `VILNA_IMAGE` (written by the image build), else `NAME VERSION_ID` of
/// `/etc/os-release`.
pub fn image(os_release: &str) -> Option<String> {
    let mut name = None;
    let mut version = None;
    for line in os_release.lines() {
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let value = value.trim().trim_matches('"').trim_matches('\'').to_owned();
        match key.trim() {
            "VILNA_IMAGE" if !value.is_empty() => return Some(value),
            "NAME" => name = Some(value),
            "VERSION_ID" => version = Some(value),
            _ => {}
        }
    }
    match (name, version) {
        (Some(n), Some(v)) => Some(format!("{n} {v}")),
        (Some(n), None) => Some(n),
        _ => None,
    }
}

/// The language of `LANG` in `/etc/locale.conf`: `uk` for `uk_UA.UTF-8`.
pub fn language(locale_conf: &str) -> Option<String> {
    locale_conf
        .lines()
        .filter_map(|l| l.trim().strip_prefix("LANG="))
        .map(|v| v.trim().trim_matches('"'))
        .map(|v| {
            v.split(['_', '.', '@'])
                .next()
                .unwrap_or_default()
                .to_owned()
        })
        .find(|v| (2..=3).contains(&v.len()) && v.chars().all(|c| c.is_ascii_lowercase()))
}

/// `MemTotal` of `/proc/meminfo` in whole GiB, rounded up (the protocol then
/// rounds to a power of two: 15.5 GiB of a 16 GB machine is 16).
pub fn memory_gb(meminfo: &str) -> Option<u32> {
    let kib: u64 = meminfo
        .lines()
        .find_map(|l| l.strip_prefix("MemTotal:"))?
        .split_whitespace()
        .next()?
        .parse()
        .ok()?;
    let gib = kib.div_ceil(1024 * 1024);
    u32::try_from(gib).ok().filter(|g| *g > 0)
}

fn read(path: &Path) -> String {
    std::fs::read_to_string(path).unwrap_or_default()
}

/// This device, read from `root` (`/` on a real system).
pub fn this_device(root: &Path) -> Device {
    Device {
        image: image(&read(&root.join("etc/os-release")))
            .or_else(|| image(&read(&root.join("usr/lib/os-release")))),
        locale: language(&read(&root.join("etc/locale.conf"))),
        arch: Some(std::env::consts::ARCH.to_owned()),
        memory_gb: memory_gb(&read(&root.join("proc/meminfo"))),
    }
}

/// A new random (version 4) UUID, or `None` if the system has no randomness.
pub fn new_id() -> Option<String> {
    let mut b = [0u8; 16];
    getrandom::fill(&mut b).ok()?;
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let hex: String = b.iter().map(|x| format!("{x:02x}")).collect();
    Some(format!(
        "{}-{}-{}-{}-{}",
        hex.get(0..8)?,
        hex.get(8..12)?,
        hex.get(12..16)?,
        hex.get(16..20)?,
        hex.get(20..32)?
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_image_comes_from_os_release() {
        assert_eq!(
            image("NAME=\"Vilna\"\nVERSION_ID=\"0.1\"\n").as_deref(),
            Some("Vilna 0.1")
        );
        assert_eq!(
            image("NAME=Vilna\nVERSION_ID=0.1\nVILNA_IMAGE=\"Vilna Blavet 0.1\"").as_deref(),
            Some("Vilna Blavet 0.1")
        );
        assert_eq!(image(""), None);
    }

    #[test]
    fn only_the_language_of_the_locale() {
        assert_eq!(
            language("LANG=uk_UA.UTF-8\nLC_TIME=de_DE.UTF-8").as_deref(),
            Some("uk")
        );
        assert_eq!(language("LANG=\"pl_PL.UTF-8\"").as_deref(), Some("pl"));
        assert_eq!(language("LANG=C.UTF-8"), None);
        assert_eq!(language(""), None);
    }

    #[test]
    fn memory_in_whole_gib() {
        assert_eq!(memory_gb("MemTotal:       16252928 kB\n"), Some(16));
        assert_eq!(memory_gb("MemTotal: 7900000 kB"), Some(8));
        assert_eq!(memory_gb("MemTotal: x kB"), None);
        assert_eq!(memory_gb(""), None);
    }

    #[test]
    fn ids_are_random_version_4_uuids() {
        let a = new_id().unwrap();
        let b = new_id().unwrap();
        assert_ne!(a, b);
        assert!(vilna_reports_protocol::report_id(&a).is_some(), "{a}");
    }
}
