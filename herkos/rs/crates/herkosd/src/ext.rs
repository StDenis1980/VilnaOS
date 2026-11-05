// SPDX-License-Identifier: GPL-3.0-or-later
//! Programs from Flatpak as Herkos tools (ADR 0017, stage 2 step 2).
//!
//! The package `herkos-manifests` ships, next to each manifest with
//! `flatpak` in `[launch]`, a unit `vilna-mcp-ext-<name>@.service` made by
//! `herkosd ext-unit` (polkit lets `herkos` start only `vilna-mcp-*` units,
//! and only root installs units). The unit runs, as the person,
//! `herkosd ext-exec <program> <args file>`: it reads the manifest again from
//! the system catalogue (files of root only, never what `herkosd` says), the
//! allowed folders from the args file `herkosd` wrote, and becomes
//! `flatpak run` of that program, which only sees those folders.

use std::fs;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use herkos_core::manifest::{EXT_PREFIX, Flatpak, Manifest};

use crate::catalogue;

pub const FLATPAK: &str = "/usr/bin/flatpak";
/// At most this many allowed folders, as in config.toml.
const MAX_ROOTS: usize = 64;

/// Folders the program must not see, whatever its own Flatpak permissions say.
const HIDDEN: [&str; 11] = [
    "host",
    "host-os",
    "host-etc",
    "home",
    "xdg-desktop",
    "xdg-documents",
    "xdg-download",
    "xdg-music",
    "xdg-pictures",
    "xdg-public-share",
    "xdg-videos",
];

/// Whether any operation of the tool sends data off the device.
pub fn wants_network(manifest: &Manifest) -> bool {
    manifest.operations.values().any(|op| op.egress)
}

/// The `flatpak run` command line for these folders.
pub fn command(flatpak: &Flatpak, roots: &[PathBuf], network: bool) -> Vec<String> {
    let mut cmd = vec![
        FLATPAK.to_owned(),
        "run".to_owned(),
        format!("--command={}", flatpak.command),
        "--die-with-parent".to_owned(),
    ];
    cmd.extend(HIDDEN.iter().map(|f| format!("--nofilesystem={f}")));
    cmd.extend(["x11", "fallback-x11", "wayland", "pulseaudio"].map(|s| format!("--nosocket={s}")));
    if !network {
        cmd.push("--unshare=network".to_owned());
    }
    cmd.extend(
        roots
            .iter()
            .map(|r| format!("--filesystem={}", r.display())),
    );
    cmd.push(flatpak.app.clone());
    cmd.extend(flatpak.fixed.iter().cloned());
    for flag in &flatpak.root_flags {
        for r in roots {
            cmd.extend([flag.clone(), r.display().to_string()]);
        }
    }
    cmd
}

/// An allowed folder as `flatpak --filesystem=` takes it: absolute, plain,
/// without `:` (it would read as a mode) or `,`.
fn plain_root(line: &str) -> Option<PathBuf> {
    // Checked on the text: Path::components quietly drops "." segments.
    let plain = line
        .strip_prefix('/')
        .is_some_and(|rest| rest.split('/').all(|seg| !matches!(seg, "" | "." | "..")))
        && !line.contains([':', ',', '\0', '\r']);
    plain.then(|| PathBuf::from(line))
}

/// The folders in an args file `herkosd` wrote: one absolute path per line.
pub fn read_roots(text: &str) -> Result<Vec<PathBuf>, String> {
    let roots: Vec<PathBuf> = text
        .lines()
        .filter(|l| !l.is_empty())
        .map(|l| plain_root(l).ok_or_else(|| format!("not a plain absolute folder: {l:?}")))
        .collect::<Result<_, _>>()?;
    if roots.is_empty() || roots.len() > MAX_ROOTS {
        return Err(format!("1 to {MAX_ROOTS} folders needed"));
    }
    Ok(roots)
}

/// The unit that runs this manifest's program for a person (`%i` = uid).
pub fn unit(manifest: &Manifest) -> Result<String, String> {
    let launch = manifest
        .launch
        .as_ref()
        .filter(|l| l.flatpak.is_some())
        .ok_or_else(|| format!("{}: no Flatpak program in [launch]", manifest.tool))?;
    let program = &launch.program;
    let short = program.trim_start_matches("vilna-mcp-");
    let network = if wants_network(manifest) {
        "# The manifest says some operation sends data off the device (egress).\nPrivateNetwork=no\n"
    } else {
        "PrivateNetwork=yes\n"
    };
    Ok(format!(
        "# SPDX-License-Identifier: GPL-3.0-or-later
# Made by `herkosd ext-unit` from the manifest of {tool} (ADR 0017).
# The system Herkos starts it for the person with uid %i (50-herkos.rules).
[Unit]
Description={tool} for Gloxi, uid %i
ConditionPathExists=/run/herkos-tools/%i/{short}.sock
StopWhenUnneeded=no

[Service]
Type=simple
User=%i
Environment=XDG_RUNTIME_DIR=/run/user/%i
ExecStart=/usr/bin/herkosd ext-exec {program} /run/herkos-tools/%i/{short}.args
StandardInput=file:/run/herkos-tools/%i/{short}.sock
StandardOutput=file:/run/herkos-tools/%i/{short}.sock
StandardError=journal
Restart=no

# Flatpak (bubblewrap) makes the program's own sandbox, so the unit leaves it
# namespaces and system calls; the program sees only the allowed folders.
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=-/home -/var/home -/run/user/%i
PrivateTmp=yes
{network}ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
",
        tool = manifest.tool,
    ))
}

/// `herkosd ext-exec <program> <args file>`, in the unit: become `flatpak run`.
pub fn exec(program: &str, args_file: &Path) -> Result<(), String> {
    if !program.starts_with(EXT_PREFIX) {
        return Err(format!("{program}: not a program from Flatpak"));
    }
    let (registry, problems) = catalogue::system_root_only();
    for p in problems {
        eprintln!("herkosd: manifest catalogue: {p}");
    }
    let manifest = registry
        .iter()
        .find(|m| m.launch.as_ref().is_some_and(|l| l.program == program))
        .ok_or_else(|| {
            format!(
                "{program}: no manifest in {}",
                catalogue::SYSTEM_MANIFEST_DIR
            )
        })?;
    let Some(flatpak) = manifest.launch.as_ref().and_then(|l| l.flatpak.as_ref()) else {
        return Err(format!("{program}: no Flatpak program in [launch]"));
    };
    let meta = fs::metadata(args_file).map_err(|e| format!("{}: {e}", args_file.display()))?;
    if meta.len() > 64 * 1024 {
        return Err(format!("{}: too big", args_file.display()));
    }
    let text =
        fs::read_to_string(args_file).map_err(|e| format!("{}: {e}", args_file.display()))?;
    let roots: Vec<PathBuf> = read_roots(&text)?
        .into_iter()
        .filter(|r| r.is_dir())
        .collect();
    if roots.is_empty() {
        return Err("none of the allowed folders exists".into());
    }
    let cmd = command(flatpak, &roots, wants_network(manifest));
    let (first, rest) = cmd.split_first().ok_or("empty command")?;
    // Only returns if it could not start.
    let err = Command::new(first)
        .args(rest)
        .env_remove("LD_PRELOAD")
        .exec();
    Err(format!("{first}: {err}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use herkos_core::manifest::parse_manifest_str;

    const PDF: &str = "tool = \"org.tinta.pdf\"\ndescription = \"PDF\"\n[launch]\nprogram = \"vilna-mcp-ext-pdf\"\nflatpak = \"io.github.stdenis1980.tintapdf\"\ncommand = \"pdfcraft-cli\"\nfixed = [\"mcp\"]\nroot_flags = [\"--automation-read-root\", \"--automation-write-root\"]\nargs = [\"roots\"]\n[operations.read]\nlevel = \"R1\"\n";

    fn pdf() -> Manifest {
        parse_manifest_str(PDF, "pdf").expect("valid")
    }

    #[test]
    fn the_program_sees_only_the_allowed_folders_and_no_network() {
        let m = pdf();
        let flatpak = m
            .launch
            .as_ref()
            .and_then(|l| l.flatpak.as_ref())
            .expect("flatpak");
        let roots = vec![PathBuf::from("/home/ana/Documents")];
        let cmd = command(flatpak, &roots, wants_network(&m));
        let at = |s: &str| cmd.iter().position(|a| a == s).expect(s);
        assert!(cmd.contains(&"--nofilesystem=home".to_owned()));
        assert!(cmd.contains(&"--unshare=network".to_owned()));
        assert!(at("--filesystem=/home/ana/Documents") < at("io.github.stdenis1980.tintapdf"));
        assert_eq!(
            &cmd[at("io.github.stdenis1980.tintapdf")..],
            [
                "io.github.stdenis1980.tintapdf",
                "mcp",
                "--automation-read-root",
                "/home/ana/Documents",
                "--automation-write-root",
                "/home/ana/Documents",
            ]
        );
        assert_eq!(cmd[2], "--command=pdfcraft-cli");
    }

    #[test]
    fn egress_lets_the_program_use_the_network() {
        let m = parse_manifest_str(
            &PDF.replace("level = \"R1\"", "level = \"R3\"\negress = true"),
            "pdf",
        )
        .expect("valid");
        let flatpak = m
            .launch
            .as_ref()
            .and_then(|l| l.flatpak.as_ref())
            .expect("flatpak");
        let cmd = command(flatpak, &[PathBuf::from("/srv/x")], wants_network(&m));
        assert!(!cmd.contains(&"--unshare=network".to_owned()));
        assert!(unit(&m).expect("unit").contains("PrivateNetwork=no"));
    }

    #[test]
    fn args_files_hold_only_plain_absolute_folders() {
        assert_eq!(
            read_roots("/home/ana/Documents\n/srv/share\n"),
            Ok(vec![
                PathBuf::from("/home/ana/Documents"),
                PathBuf::from("/srv/share")
            ])
        );
        for wrong in [
            "",
            "relative/dir\n",
            "/\n",
            "/home/ana/../bob\n",
            "/home/ana/./x\n",
            "/home/ana:ro\n",
            "/home/a,b\n",
            "--filesystem=host\n",
        ] {
            assert!(read_roots(wrong).is_err(), "{wrong:?}");
        }
        assert!(read_roots(&"/x\n".repeat(MAX_ROOTS + 1)).is_err());
    }

    #[test]
    fn the_unit_runs_the_program_as_the_person_without_network() {
        let text = unit(&pdf()).expect("unit");
        assert!(text.contains("User=%i\n"));
        assert!(text.contains(
            "ExecStart=/usr/bin/herkosd ext-exec vilna-mcp-ext-pdf /run/herkos-tools/%i/ext-pdf.args\n"
        ));
        assert!(text.contains("StandardInput=file:/run/herkos-tools/%i/ext-pdf.sock\n"));
        assert!(text.contains("PrivateNetwork=yes\n"));
        let files = herkos_core::manifest::Registry::builtin().expect("builtin");
        let files = files.get("org.vilna.mcp.files").expect("files");
        assert!(unit(files).is_err());
    }

    #[test]
    fn exec_refuses_programs_that_are_not_from_flatpak() {
        assert!(exec("vilna-mcp-files", Path::new("/nonexistent")).is_err());
    }
}
