# SPDX-License-Identifier: GPL-3.0-or-later
"""What goes into a backup: presets, exclusions, sizes (design.md, 2 and 2a).

The system itself is never copied (it comes from the image), nor model
weights, caches or the disk encryption key. Secrets of the system (keyring,
online accounts, SSH and GPG keys) only with a separate consent.
"""

from __future__ import annotations

import configparser
import fnmatch
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from vilna_backup.settings import Settings, bad_path_char, clean_folder, user_folder

# Name patterns never backed up, wherever they are.
EXCLUDE_NAMES = (
    "*.gguf",  # model weights come with the image or are downloaded again
    "*.safetensors",
    ".cache",
    "node_modules",
    ".Trash-*",
    # Mitos snapshots of the home (ADR 0026): already copies, on this disk.
    ".snapshots",
)
_USER_DIR = re.compile(r'^XDG_([A-Z]+)_DIR="(.*)"\s*$')
_DEFAULT_USER_DIRS = {
    "DESKTOP": "Desktop",
    "DOCUMENTS": "Documents",
    "DOWNLOAD": "Downloads",
    "MUSIC": "Music",
    "PICTURES": "Pictures",
    "VIDEOS": "Videos",
}


@dataclass(frozen=True)
class Layout:
    """Where things are in one person's home folder."""

    home: Path
    config: Path
    data: Path
    state: Path
    user_dirs: Mapping[str, Path]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, home: Path | None = None) -> Layout:
        env = os.environ if env is None else env
        home = Path(home or env.get("HOME") or Path.home())

        def xdg(var: str, fallback: str) -> Path:
            value = env.get(var)
            return Path(value) if value and os.path.isabs(value) else home / fallback

        config = xdg("XDG_CONFIG_HOME", ".config")
        return cls(
            home=home,
            config=config,
            data=xdg("XDG_DATA_HOME", ".local/share"),
            state=xdg("XDG_STATE_HOME", ".local/state"),
            user_dirs=read_user_dirs(home, config),
        )

    def user_dir(self, name: str) -> Path:
        return self.user_dirs[name]

    @property
    def export_dir(self) -> Path:
        """Lists made before each run (Flatpak apps, dconf dump); backed up."""
        return self.data / "vilna-backup" / "export"


def read_user_dirs(home: Path, config: Path) -> dict[str, Path]:
    """XDG user folders (``user-dirs.dirs``); in Ukrainian they are e.g. ~/Документи."""
    dirs = {k: home / v for k, v in _DEFAULT_USER_DIRS.items()}
    try:
        text = (config / "user-dirs.dirs").read_text(encoding="utf-8")
    except (OSError, ValueError):  # ValueError: not UTF-8; the defaults stay
        return dirs
    for line in text.splitlines():
        m = _USER_DIR.match(line.strip())
        if not m or m.group(1) not in _DEFAULT_USER_DIRS:
            continue
        value = m.group(2).replace("$HOME", str(home))
        if not os.path.isabs(value) or bad_path_char(value):
            continue
        p = Path(clean_folder(value))
        # "/" or "/etc" as Documents would copy the disk.
        if p != home and user_folder(p, home):
            dirs[m.group(1)] = p
    return dirs


def keepass_databases(layout: Layout) -> list[Path]:
    """KeePassXC databases: the ones it last opened, and any .kdbx in the home folder."""
    found: dict[str, Path] = {}
    inis = [
        layout.config / "keepassxc" / "keepassxc.ini",
        layout.home / ".var/app/org.keepassxc.KeePassXC/config/keepassxc/keepassxc.ini",
        layout.home / ".cache/keepassxc/keepassxc.ini",
    ]
    for ini in inis:
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read(ini, encoding="utf-8")
        except (OSError, ValueError, configparser.Error):
            continue
        for section in parser.sections():
            raw = parser.get(section, "LastOpenedDatabases", fallback="")
            for item in raw.split(","):
                item = item.strip()
                if not os.path.isabs(item) or bad_path_char(item):
                    continue
                p = Path(clean_folder(item))
                if p.suffix == ".kdbx" and user_folder(p, layout.home) and p.is_file():
                    found[str(p)] = p
    for p in layout.home.glob("*.kdbx"):
        if p.is_file():
            found[str(p)] = p
    return sorted(found.values())


def settings_paths(layout: Layout) -> list[Path]:
    """ "Only settings" (design.md 2a, step 2): settings, Gloxi, Herkos, passwords."""
    c, d, s = layout.config, layout.data, layout.state
    return [
        c / "dconf",
        layout.home / ".var" / "app",
        c / "gloxi",
        d / "gloxi",
        c / "herkos",
        s / "herkos",
        c / "vilna-backup",
        layout.export_dir,
        *keepass_databases(layout),
    ]


def secret_paths(layout: Layout) -> list[Path]:
    """System secrets: backed up only with a separate consent (design.md 2)."""
    return [
        layout.data / "keyrings",
        layout.config / "goa-1.0",
        layout.home / ".ssh",
        layout.home / ".gnupg",
    ]


def document_paths(layout: Layout) -> list[Path]:
    return [layout.user_dir(n) for n in ("DOCUMENTS", "DESKTOP", "PICTURES")]


def gloxi_history_path(layout: Layout) -> Path:
    return layout.data / "gloxi" / "history"


@dataclass(frozen=True)
class Plan:
    includes: tuple[Path, ...]
    # Absolute paths, taken literally (with everything under them) ...
    exclude_paths: tuple[Path, ...]
    # ... and name patterns (fnmatch), as restic --exclude understands them.
    exclude_names: tuple[str, ...] = EXCLUDE_NAMES
    missing: tuple[Path, ...] = field(default=())
    # Absolute paths of ours where a part that is just "*" stands for any one name.
    exclude_globs: tuple[str, ...] = ()

    def is_excluded(self, path: Path) -> bool:
        for e in self.exclude_paths:
            if path == e or path.is_relative_to(e):
                return True
        for g in self.exclude_globs:
            if any(_glob_match(q, g) for q in (path, *path.parents)):
                return True
        return any(fnmatch.fnmatch(path.name, pat) for pat in self.exclude_names)

    def all_excluded(self) -> bool:
        return all(self.is_excluded(p) for p in self.includes)

    def exclude_lines(self) -> list[str]:
        """Contents of the restic --exclude-file.

        Folder names are taken literally: restic reads each line as a pattern,
        with ``$`` for environment variables and ``* ? [ \\`` as wildcards.
        """
        return [
            *(literal_pattern(str(p)) for p in self.exclude_paths),
            *(_glob_line(g) for g in self.exclude_globs),
            *self.exclude_names,
        ]


def _glob_line(glob: str) -> str:
    return "/".join("*" if part == "*" else literal_pattern(part) for part in glob.split("/"))


def literal_pattern(text: str, *, file_line: bool = True) -> str:
    """A name as a restic pattern that matches only itself.

    ``file_line``: for an --exclude-file, where restic also expands ``$NAME``
    (``$$`` is a plain dollar). An --exclude argument is not expanded.
    """
    if bad_path_char(text):
        raise ValueError(f"cannot hand this folder name to restic: {text!r}")
    out = []
    for c in text:
        if c in "\\*?[":
            out.append("\\" + c)
        elif c == "$" and file_line:
            out.append("$$")
        else:
            out.append(c)
    line = "".join(out)
    # restic strips spaces at the ends of a line; a class keeps a trailing one.
    stripped = line.rstrip()
    tail = line[len(stripped) :]
    return stripped + "".join(f"[{c}]" for c in tail)


def _glob_match(path: Path, glob: str) -> bool:
    """Like restic: the wildcard stands for one name and never crosses a slash."""
    parts = Path(glob).parts
    return len(path.parts) == len(parts) and all(
        b == "*" or a == b for a, b in zip(path.parts, parts, strict=True)
    )


def build_plan(settings: Settings, layout: Layout) -> Plan:
    wanted: list[Path] = list(settings_paths(layout))
    if settings.preset == "documents":
        wanted += document_paths(layout)
    elif settings.preset == "home":
        wanted = [layout.home, *keepass_databases(layout)]
    wanted += [Path(p) for p in settings.include]
    if settings.system_secrets:
        wanted += secret_paths(layout)

    globs = [str(layout.home / ".var" / "app" / "*" / "cache")]
    excludes: list[Path] = [
        layout.home / ".cache",
        layout.data / "Trash",
        layout.state / "vilna-backup",
        layout.state / "herkos" / "backups",  # undo copies of the files tool
        layout.home / ".local" / "share" / "containers",
        layout.home / ".local" / "share" / "flatpak",  # apps are reinstalled from the list
    ]
    if settings.preset == "home":
        excludes.append(layout.user_dir("DOWNLOAD"))
    if not settings.gloxi_history:
        excludes.append(gloxi_history_path(layout))
    if not settings.system_secrets:
        excludes += secret_paths(layout)
    excludes += [Path(p) for p in settings.exclude]

    includes, missing = [], []
    for p in _dedupe(wanted):
        (includes if p.exists() else missing).append(p)
    return Plan(
        includes=tuple(includes),
        exclude_paths=tuple(_dedupe(excludes)),
        missing=tuple(missing),
        exclude_globs=tuple(globs),
    )


def _dedupe(paths: Iterable[Path]) -> list[Path]:
    """Drop repeats and paths already inside another one, keeping order."""
    paths = list(paths)
    kept: set[Path] = set()
    out: list[Path] = []
    # Shortest first, then a set lookup per parent: linear in the number of paths.
    for p in sorted({Path(clean_folder(str(p))) for p in paths}, key=lambda p: len(p.parts)):
        if p not in kept and not any(q in kept for q in p.parents):
            kept.add(p)
            out.append(p)
    order: dict[str, int] = {}
    for i, p in enumerate(paths):
        order.setdefault(clean_folder(str(p)), i)
    return sorted(out, key=lambda p: order.get(str(p), 0))


# -- sizes -------------------------------------------------------------------


def size_of(path: Path, plan: Plan | None = None) -> int:
    """Bytes under a path, without following symlinks, skipping exclusions."""
    if plan is not None and plan.is_excluded(path):
        return 0
    try:
        st = path.lstat()
    except OSError:
        return 0
    if not path.is_dir() or path.is_symlink():
        return st.st_size
    total = 0
    stack = [path]
    while stack:
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            p = Path(e.path)
            if plan is not None and plan.is_excluded(p):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(p)
                else:
                    total += e.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def plan_size(plan: Plan) -> int:
    return sum(size_of(p, plan) for p in plan.includes)


def overview(layout: Layout, settings: Settings) -> list[dict]:
    """Real sizes for step 2 of the first-run dialog: what each choice would take."""
    base = build_plan(settings.with_changes(preset="settings", include=(), exclude=()), layout)
    rows = [
        {"group": "settings", "paths": [str(p) for p in base.includes], "bytes": plan_size(base)}
    ]
    for name in ("DOCUMENTS", "DESKTOP", "PICTURES", "VIDEOS", "MUSIC", "DOWNLOAD"):
        p = layout.user_dir(name)
        if p.is_dir():
            rows.append({"group": name.lower(), "paths": [str(p)], "bytes": size_of(p, base)})
    home = build_plan(settings.with_changes(preset="home", include=(), exclude=()), layout)
    rows.append({"group": "home", "paths": [str(layout.home)], "bytes": plan_size(home)})
    return rows


def fit(plan: Plan, limit: int | None, layout: Layout) -> dict:
    """Step 4: does the choice fit the limit, and if not, which large folders to leave out."""
    total = plan_size(plan)
    out: dict = {"bytes": total, "limit": limit, "fits": limit is None or total <= limit}
    if out["fits"]:
        out["suggest_exclude"] = []
        return out
    protected = {Path(os.path.normpath(p)) for p in settings_paths(layout)}
    candidates: list[tuple[int, Path]] = []
    for inc in plan.includes:
        if inc in protected or not inc.is_dir():
            continue
        candidates.append((size_of(inc, plan), inc))
        try:
            children = [Path(e.path) for e in os.scandir(inc) if e.is_dir(follow_symlinks=False)]
        except OSError:
            children = []
        for child in children:
            if child not in protected and not plan.is_excluded(child):
                candidates.append((size_of(child, plan), child))
    # Smallest set of large folders: take the largest first, prefer whole
    # subfolders over top folders when one is enough.
    candidates.sort(key=lambda c: c[0], reverse=True)
    chosen: list[Path] = []
    remaining = total
    for size, path in candidates:
        if remaining <= limit:
            break
        if any(path == c or path.is_relative_to(c) or c.is_relative_to(path) for c in chosen):
            continue
        if remaining - size < 0:
            continue
        chosen.append(path)
        remaining -= size
    out["suggest_exclude"] = [str(p) for p in chosen]
    out["bytes_after"] = remaining
    return out
