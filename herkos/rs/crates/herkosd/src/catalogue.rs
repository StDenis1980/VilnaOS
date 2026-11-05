// SPDX-License-Identifier: GPL-3.0-or-later
//! The system manifest catalogue (ADR 0017, step 1), same rules as
//! `ManifestRegistry.installed` in Herkos-lite.
//!
//! A catalogue file is read only if nobody but root or this herkosd can change
//! it. A file that is not trusted, malformed, too big, or names a tool Herkos
//! already knows is skipped and reported: the tool stays unknown, so denied.
//! Built-in manifests always win.

use std::fs::{self, Metadata, OpenOptions};
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

use herkos_core::manifest::{ManifestError, Registry, parse_manifest_str};

pub const SYSTEM_MANIFEST_DIR: &str = "/usr/share/herkos/manifests";
pub const MAX_MANIFEST_FILES: usize = 256;
pub const MAX_MANIFEST_BYTES: u64 = 64 * 1024;

/// The built-in manifests plus the catalogue (`HERKOS_MANIFEST_DIR`, or the system one).
pub fn installed() -> Result<Registry, ManifestError> {
    let dir = std::env::var_os("HERKOS_MANIFEST_DIR")
        .filter(|d| !d.is_empty())
        .map_or_else(|| PathBuf::from(SYSTEM_MANIFEST_DIR), PathBuf::from);
    let mut registry = Registry::builtin()?;
    for problem in add_catalogue(&mut registry, &dir) {
        eprintln!("herkosd: manifest catalogue: {problem}");
    }
    Ok(registry)
}

/// Only root or this herkosd own it, and neither group nor others may write.
fn trusted(meta: &Metadata) -> bool {
    let euid = rustix::process::geteuid().as_raw();
    (meta.uid() == 0 || meta.uid() == euid) && meta.mode() & 0o022 == 0
}

/// Add the trusted `*.toml` files of `dir`; return what was skipped.
pub fn add_catalogue(registry: &mut Registry, dir: &Path) -> Vec<String> {
    let shown = dir.display();
    let meta = match fs::symlink_metadata(dir) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Vec::new(),
        Err(e) => return vec![format!("{shown}: {e}")],
    };
    if !meta.is_dir() || !trusted(&meta) {
        return vec![format!(
            "{shown}: not a folder that only root or Herkos can change"
        )];
    }
    let entries = match fs::read_dir(dir) {
        Ok(e) => e,
        Err(e) => return vec![format!("{shown}: {e}")],
    };
    let mut names: Vec<String> = entries
        .filter_map(Result::ok)
        .filter_map(|e| e.file_name().into_string().ok())
        .filter(|n| n.ends_with(".toml"))
        .collect();
    names.sort();
    let mut problems = Vec::new();
    if names.len() > MAX_MANIFEST_FILES {
        problems.push(format!(
            "{shown}: more than {MAX_MANIFEST_FILES} manifests, the rest skipped"
        ));
        names.truncate(MAX_MANIFEST_FILES);
    }
    for name in names {
        let path = dir.join(&name);
        let added = read_trusted(&path)
            .and_then(|text| parse_manifest_str(&text, &name).map_err(|e| e.0))
            .and_then(|m| registry.add(m).map_err(|e| e.0));
        if let Err(e) = added {
            problems.push(format!("{}: {e}", path.display()));
        }
    }
    problems
}

/// The file's text, if it is a regular, trusted file of at most 64 KiB (no symlinks).
fn read_trusted(path: &Path) -> Result<String, String> {
    let flags = rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK;
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(i32::try_from(flags.bits()).unwrap_or(0))
        .open(path)
        .map_err(|e| e.to_string())?;
    let meta = file.metadata().map_err(|e| e.to_string())?;
    if !meta.is_file() || !trusted(&meta) {
        return Err("not a file that only root or Herkos can change".to_owned());
    }
    let mut bytes = Vec::new();
    file.take(MAX_MANIFEST_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() as u64 > MAX_MANIFEST_BYTES {
        return Err(format!("larger than {MAX_MANIFEST_BYTES} bytes"));
    }
    String::from_utf8(bytes).map_err(|_| "not UTF-8".to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{PermissionsExt, symlink};

    const GOOD: &str = "tool = \"org.example.notes\"\ndescription = \"Notes\"\n[operations.read]\nlevel = \"R1\"\n";

    fn temp(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("herkosd-catalogue-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("temp dir");
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o755)).expect("chmod");
        dir
    }

    fn write(dir: &Path, name: &str, text: &str, mode: u32) {
        let path = dir.join(name);
        fs::write(&path, text).expect("write");
        fs::set_permissions(&path, fs::Permissions::from_mode(mode)).expect("chmod");
    }

    #[test]
    fn a_trusted_manifest_is_added_after_the_builtin_ones() {
        let dir = temp("good");
        write(&dir, "notes.toml", GOOD, 0o644);
        write(&dir, "README", "not a manifest", 0o644);
        let mut r = Registry::builtin().expect("builtin");
        let problems = add_catalogue(&mut r, &dir);
        assert!(problems.is_empty(), "{problems:?}");
        assert!(r.get("org.example.notes").is_some());
        assert!(r.get("org.vilna.mcp.files").is_some());
    }

    #[test]
    fn a_missing_folder_is_no_problem() {
        let mut r = Registry::builtin().expect("builtin");
        assert!(add_catalogue(&mut r, Path::new("/nonexistent/herkos")).is_empty());
    }

    #[test]
    fn builtin_manifests_win() {
        let dir = temp("dup");
        write(
            &dir,
            "files.toml",
            "tool = \"org.vilna.mcp.files\"\n[operations.delete]\nlevel = \"R1\"\n",
            0o644,
        );
        let mut r = Registry::builtin().expect("builtin");
        let before = format!("{:?}", r.get("org.vilna.mcp.files"));
        assert_eq!(add_catalogue(&mut r, &dir).len(), 1);
        assert_eq!(format!("{:?}", r.get("org.vilna.mcp.files")), before);
    }

    #[test]
    fn untrusted_or_bad_files_are_skipped() {
        let big = format!("{GOOD}# {}\n", "x".repeat(70 * 1024));
        for (name, text, mode) in [
            ("writable.toml", GOOD.to_owned(), 0o666),
            ("group-writable.toml", GOOD.to_owned(), 0o664),
            ("broken.toml", "tool = [".to_owned(), 0o644),
            ("unknown-key.toml", format!("{GOOD}bogus = 1\n"), 0o644),
            ("big.toml", big, 0o644),
            ("binary.toml", "tool = \"\u{0}\"".to_owned(), 0o644),
        ] {
            let dir = temp(name.trim_end_matches(".toml"));
            write(&dir, name, &text, mode);
            let mut r = Registry::builtin().expect("builtin");
            assert_eq!(add_catalogue(&mut r, &dir).len(), 1, "{name}");
            assert!(r.get("org.example.notes").is_none(), "{name}");
        }
    }

    #[test]
    fn symlinks_are_not_followed() {
        let dir = temp("link");
        let elsewhere = temp("link-target");
        write(&elsewhere, "notes.toml", GOOD, 0o644);
        symlink(elsewhere.join("notes.toml"), dir.join("notes.toml")).expect("symlink");
        let mut r = Registry::builtin().expect("builtin");
        assert_eq!(add_catalogue(&mut r, &dir).len(), 1);
        assert!(r.get("org.example.notes").is_none());
    }

    #[test]
    fn a_folder_others_can_write_is_ignored() {
        let dir = temp("open");
        write(&dir, "notes.toml", GOOD, 0o644);
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o777)).expect("chmod");
        let mut r = Registry::builtin().expect("builtin");
        assert_eq!(add_catalogue(&mut r, &dir).len(), 1);
        assert!(r.get("org.example.notes").is_none());
    }
}
