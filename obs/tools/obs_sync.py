#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Assemble Vilna's OBS packages from this repository and upload them.

Each folder in ``obs/packages/`` is one OBS package: its files go to OBS
as they are. Each folder in ``obs/images/`` is one KIWI image, built by OBS
in the project's ``images`` repository from the packages of the
``openSUSE_Tumbleweed`` one. Packages built from code in this repository
also get a source tarball made here (see ``TARBALLS``), and their spec ``Version:``
is taken from the code, so the spec never drifts from it.

    obs_sync.py assemble OUT            # write every package to OUT/<name>/
    obs_sync.py upload --project P --user U [--message M] [--dry-run]

``upload`` needs ``osc`` with credentials for build.opensuse.org. It
creates or updates the project (``obs/project/_meta``, ``_prjconf``), then
each package, and commits only packages whose files changed.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import lzma
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
OBS = ROOT / "obs"
PACKAGES = OBS / "packages"
IMAGES = OBS / "images"
# OBS repositories (obs/project/_meta): RPMs build in the first, images in the second.
RPM_REPOSITORY = "openSUSE_Tumbleweed"
IMAGE_REPOSITORY = "images"
PROJECT = OBS / "project"


@dataclass(frozen=True)
class Tarball:
    """A source tarball made from a folder of this repository."""

    folder: str  # repository folder with the code
    version: Callable[[], str]  # RPM version of the code
    extra: tuple[tuple[str, str], ...] = ()  # (repo path, path in tarball)
    # Cargo.lock of Rust code: its crates go to vendor.tar.xz (OBS builds offline).
    cargo: str | None = None


def pep440_to_rpm(version: str) -> str:
    """0.1.0.dev0 -> 0.1.0~dev0, 1.0rc1 -> 1.0~rc1, so pre-releases sort first."""
    return re.sub(r"\.?(dev|a|b|rc)(\d*)", r"~\1\2", version)


def pyproject_version(folder: str) -> Callable[[], str]:
    def read() -> str:
        data = tomllib.loads((ROOT / folder / "pyproject.toml").read_text(encoding="utf-8"))
        return pep440_to_rpm(data["project"]["version"])

    return read


def extension_version(folder: str) -> Callable[[], str]:
    """The "version-name" of a GNOME Shell extension's metadata.json."""

    def read() -> str:
        data = json.loads((ROOT / folder / "metadata.json").read_text(encoding="utf-8"))
        return str(data["version-name"])

    return read


def file_version(path: str) -> Callable[[], str]:
    """The version written alone in a file (image/plymouth/VERSION)."""

    def read() -> str:
        return (ROOT / path).read_text(encoding="utf-8").strip()

    return read


TARBALLS: dict[str, Tarball] = {
    "gloxi": Tarball("gloxi", pyproject_version("gloxi"), extra=(("LICENSE", "LICENSE"),)),
    "herkos": Tarball(
        "herkos",
        pyproject_version("herkos"),
        extra=(("LICENSE", "LICENSE"),),
        cargo="herkos/rs/Cargo.toml",
    ),
    "vilna-mcp-backup": Tarball(
        "mcp/backup",
        pyproject_version("mcp/backup"),
        extra=(("mcp/LICENSE", "LICENSE"),),
    ),
    "vilna-mcp-files": Tarball(
        "mcp/files", pyproject_version("mcp/files"), extra=(("mcp/LICENSE", "LICENSE"),)
    ),
    "vilna-mcp-settings": Tarball(
        "mcp/settings", pyproject_version("mcp/settings"), extra=(("mcp/LICENSE", "LICENSE"),)
    ),
    "vilna-mcp-calendar": Tarball(
        "mcp/calendar", pyproject_version("mcp/calendar"), extra=(("mcp/LICENSE", "LICENSE"),)
    ),
    "python-vilna-privacy-gateway": Tarball(
        "privacy-gateway", pyproject_version("privacy-gateway")
    ),
    "gnome-shell-extension-vilna": Tarball(
        "shell-extension",
        extension_version("shell-extension/vilna@stdenis1980.github.io"),
        extra=(("LICENSE", "LICENSE"),),
    ),
    "plymouth-branding-Vilna": Tarball(
        "image/plymouth",
        file_version("image/plymouth/VERSION"),
        extra=(
            ("LICENSE", "LICENSE"),
            ("docs/design/logo/Vilna_logo_dark-bg.svg", "Vilna_logo_dark-bg.svg"),
        ),
    ),
}


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def tracked_files(folder: str) -> list[str]:
    return sorted(git("ls-files", "-z", "--", folder).split("\0")[:-1])


def source_date() -> int:
    """Commit time of HEAD, so the same commit gives the same tarball."""
    return int(git("log", "-1", "--format=%ct").strip())


def make_tarball(name: str, spec: Tarball, version: str, out: Path) -> Path:
    prefix = f"{name}-{version}"
    mtime = source_date()
    files = [(rel, rel[len(spec.folder) + 1 :]) for rel in tracked_files(spec.folder)]
    files += list(spec.extra)
    path = out / f"{prefix}.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for src, dst in sorted(files, key=lambda f: f[1]):
            data = (ROOT / src).read_bytes()
            info = tarfile.TarInfo(f"{prefix}/{dst}")
            info.size = len(data)
            info.mtime = mtime
            info.mode = 0o755 if (ROOT / src).stat().st_mode & 0o111 else 0o644
            info.uname = info.gname = "root"
            tar.addfile(info, io.BytesIO(data))
    with path.open("wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", mtime=mtime) as gz:
        gz.write(buf.getvalue())
    return path


def make_vendor(manifest: str, out: Path) -> Path:
    """The crates of a Cargo.lock as vendor.tar.xz, the same for the same lock."""
    if shutil.which("cargo") is None:
        raise SystemExit("cargo is not installed: it is needed to vendor Rust crates")
    mtime = source_date()
    path = out / "vendor.tar.xz"
    with tempfile.TemporaryDirectory(prefix="vilna-vendor-") as tmp:
        subprocess.run(
            ["cargo", "vendor", "--locked", "--versioned-dirs", "--manifest-path",
             str(ROOT / manifest), str(Path(tmp) / "vendor")],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        files = sorted(p for p in Path(tmp).rglob("*") if p.is_file())
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for f in files:
                data = f.read_bytes()
                info = tarfile.TarInfo(f.relative_to(tmp).as_posix())
                info.size = len(data)
                info.mtime = mtime
                info.mode = 0o755 if f.stat().st_mode & 0o111 else 0o644
                info.uname = info.gname = "root"
                tar.addfile(info, io.BytesIO(data))
        path.write_bytes(lzma.compress(buf.getvalue(), preset=6))
    return path


def set_spec_version(spec: Path, version: str) -> None:
    text = spec.read_text(encoding="utf-8")
    new, n = re.subn(r"(?m)^(Version:\s*)\S+$", rf"\g<1>{version}", text, count=1)
    if n != 1:
        raise SystemExit(f"{spec}: no Version: line")
    spec.write_text(new, encoding="utf-8")


def package_names() -> list[str]:
    return sorted(p.name for p in PACKAGES.iterdir() if p.is_dir())


def image_names() -> list[str]:
    if not IMAGES.is_dir():
        return []
    return sorted(p.name for p in IMAGES.iterdir() if p.is_dir())


def assemble(out: Path) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    names = package_names()
    images = image_names()
    if clash := set(names) & set(images):
        raise SystemExit(f"both a package and an image: {', '.join(sorted(clash))}")
    for name in images:
        dest = out / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(IMAGES / name, dest)
        print(f"{name}: image, {len(list(dest.iterdir()))} files")
    for name in names:
        dest = out / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(PACKAGES / name, dest)
        if name in TARBALLS:
            spec = TARBALLS[name]
            version = spec.version()
            set_spec_version(dest / f"{name}.spec", version)
            tar = make_tarball(name, spec, version, dest)
            print(f"{name}: {tar.name}")
            if spec.cargo:
                print(f"{name}: {make_vendor(spec.cargo, dest).name}")
        else:
            print(f"{name}: {len(list(dest.iterdir()))} files")
    return images + names


def render_meta(project: str, user: str) -> str:
    text = (PROJECT / "_meta").read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->\s*", "", text, flags=re.DOTALL)
    return text.replace("@PROJECT@", project).replace("@USER@", user)


def package_meta(project: str, name: str, folder: Path) -> str:
    """Package meta; an RPM builds only in the RPM repository, an image only in images."""
    summary = ""
    spec = folder / f"{name}.spec"
    kiwi = folder / f"{name}.kiwi"
    if spec.is_file():
        other = IMAGE_REPOSITORY
        for line in spec.read_text(encoding="utf-8").splitlines():
            if line.startswith("Summary:"):
                summary = line.split(":", 1)[1].strip()
                break
    elif kiwi.is_file():
        other = RPM_REPOSITORY
        found = re.search(r"<specification>(.*?)</specification>", kiwi.read_text(encoding="utf-8"))
        summary = found.group(1).strip() if found else name
    else:
        raise SystemExit(f"{folder}: neither {spec.name} nor {kiwi.name}")
    return (
        f'<package name="{escape(name)}" project="{escape(project)}">\n'
        f"  <title>{escape(summary)}</title>\n"
        f"  <description>{escape(summary)}</description>\n"
        f'  <build><disable repository="{other}"/></build>\n'
        "</package>\n"
    )


class Osc:
    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run

    def __call__(self, *args: str, cwd: Path | None = None, stdin: str | None = None) -> str:
        print("+ osc " + " ".join(args), flush=True)
        if self.dry_run and args[0] in {"meta", "commit", "ci"}:
            return ""
        result = subprocess.run(
            ["osc", *args],
            check=False,
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            sys.stderr.write(result.stdout + result.stderr)
            raise SystemExit(f"osc {args[0]} failed with exit code {result.returncode}")
        return result.stdout


def upload(project: str, user: str, message: str, dry_run: bool) -> None:
    if shutil.which("osc") is None:
        raise SystemExit("osc is not installed (zypper in osc, or pip install osc)")
    osc = Osc(dry_run)
    with tempfile.TemporaryDirectory(prefix="vilna-obs-") as tmp:
        work = Path(tmp)
        (work / "_meta").write_text(render_meta(project, user), encoding="utf-8")
        osc("meta", "prj", project, "-F", str(work / "_meta"))
        osc("meta", "prjconf", project, "-F", str(PROJECT / "_prjconf"))

        built = work / "assembled"
        names = assemble(built)
        for name in names:
            meta = work / f"{name}.meta"
            meta.write_text(package_meta(project, name, built / name))
            osc("meta", "pkg", project, name, "-F", str(meta))
            if dry_run:
                continue
            co = work / "co" / name
            co.parent.mkdir(exist_ok=True)  # osc creates only the last directory
            osc("checkout", "--output-dir", str(co), project, name)
            for old in co.iterdir():
                if old.name != ".osc":
                    old.unlink()
            for new in (built / name).iterdir():
                shutil.copy2(new, co / new.name)
            osc("addremove", cwd=co)
            if osc("status", cwd=co).strip():
                osc("commit", "-m", message, cwd=co)
            else:
                print(f"{name}: unchanged")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("assemble", help="write all packages to a folder")
    a.add_argument("out", type=Path)
    u = sub.add_parser("upload", help="create/update the OBS project and commit packages")
    u.add_argument("--project", required=True, help="e.g. home:<obs-user>:vilna")
    u.add_argument("--user", required=True, help="OBS user who maintains the project")
    u.add_argument("--message", default=None, help="commit message for changed packages")
    u.add_argument("--dry-run", action="store_true", help="show osc calls, change nothing")
    args = parser.parse_args(argv)

    if args.command == "assemble":
        assemble(args.out)
    else:
        message = args.message or "Update from git " + git("rev-parse", "--short", "HEAD").strip()
        upload(args.project, args.user, message, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
