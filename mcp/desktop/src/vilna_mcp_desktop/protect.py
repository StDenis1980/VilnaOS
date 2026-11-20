# SPDX-License-Identifier: Apache-2.0
"""Which apps Gloxi never reads or touches through AT-SPI (ADR 0031, 4).

An app is known by its process, which the accessibility bus gives from the
kernel (``GetConnectionUnixProcessID``), not by the name it calls itself:
the program file, its command line and, for Flatpak, the app id in its
cgroup. A process that cannot be told is protected. A program can pretend
to be a protected one; that only hides it from Gloxi.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Security and secrets: polkit's and the keyring's windows live in GNOME
# Shell, so the whole Shell (also the lock screen and notifications) is out.
SECURITY = {
    "gnome-shell",
    "gnome-shell-portal-helper",
    "gcr-prompter",
    "gcr-viewer",
    "polkit-gnome-authentication-agent-1",
    "polkit-kde-authentication-agent-1",
    "polkit-mate-authentication-agent-1",
    "gdm",
    "gdm-session-worker",
    "gnome-initial-setup",
    "pinentry",
    "pinentry-gnome3",
    "pinentry-gtk-2",
    "pinentry-qt",
    "ssh-askpass",
    "gnome-ssh-askpass",
    "ksshaskpass",
    "seahorse",
    "secrets",
    "keepassxc",
    "bitwarden",
    "1password",
    "proton-pass",
    "authenticator",
    "kwalletmanager5",
    "kwalletmanager6",
}
# Administration, where a click could go around Herkos: settings, software,
# app permissions, disks, the settings database, extensions, terminals.
ADMINISTRATION = {
    "gnome-control-center",
    "gnome-software",
    "myrlyn",
    "yast2",
    "y2start",
    "flatseal",
    "gnome-disks",
    "gparted",
    "gpartedbin",
    "dconf-editor",
    "extension-manager",
    "gnome-extensions-app",
    "gnome-terminal",
    "gnome-terminal-server",
    "kgx",
    "ptyxis",
    "ptyxis-agent",
    "blackbox",
    "konsole",
    "xterm",
    "uxterm",
    "alacritty",
    "kitty",
    "wezterm",
    "wezterm-gui",
    "foot",
    "tilix",
    "terminator",
    "ghostty",
    "systemsettings",
    "discover",
}
FLATPAKS = {
    "org.gnome.seahorse.Application",
    "org.gnome.World.Secrets",
    "org.keepassxc.KeePassXC",
    "com.bitwarden.desktop",
    "com.onepassword.OnePassword",
    "me.proton.Pass",
    "com.belmoussaoui.Authenticator",
    "com.github.tchx84.Flatseal",
    "ca.desrt.dconf-editor",
    "com.mattjakeman.ExtensionManager",
    "org.gnome.Extensions",
    "org.gnome.Settings",
    "org.gnome.Software",
    "org.gnome.Console",
    "org.gnome.Ptyxis",
    "app.devsuite.Ptyxis",
    "com.raggesilver.BlackBox",
    "org.wezfurlong.wezterm",
    "com.mitchellh.ghostty",
    "org.kde.konsole",
    "io.github.stdenis1980.Gloxi",
}
# Vilna's own windows: Herkos (confirmation, agent, «Дозволи»), Gloxi, the
# installer, «Копії»... each has a tool of its own through Herkos. Any
# program or Python module whose name starts with one of these.
OWN_PREFIXES = ("herkos", "gloxi", "vilna", "io.github.stdenis1980.vilna")
# Programs that run a script or module: their name says nothing, the script does.
_INTERPRETER = re.compile(r"(python[0-9.]*|ruby[0-9.]*|perl|gjs|node|nodejs|bash|sh|dash)")
_SCOPE = re.compile(r"app-flatpak-([A-Za-z0-9_.-]+?)-[0-9]+\.scope")
MAX_ARGV = 8


@dataclass(frozen=True)
class Process:
    pid: int
    uid: int | None
    exe: str  # basename of the program file, "" if unknown
    argv: tuple[str, ...] = ()
    flatpak: str | None = None
    names: frozenset[str] = field(default_factory=frozenset)

    @property
    def label(self) -> str:
        """How the person knows the app: its Flatpak id, else its program."""
        return self.flatpak or self.exe or (self.argv[0] if self.argv else "?")


def identify(pid: int, proc: Path = Path("/proc")) -> Process | None:
    """Who runs ``pid``, from /proc; None if it cannot be told."""
    if pid <= 0:
        return None
    base = proc / str(pid)
    try:
        exe = os.path.basename(os.readlink(base / "exe"))
    except OSError:
        exe = ""
    try:
        raw = (base / "cmdline").read_bytes()[:4096]
        argv = tuple(a.decode("utf-8", "replace") for a in raw.split(b"\0") if a)[:MAX_ARGV]
    except OSError:
        argv = ()
    uid = None
    try:
        for line in (base / "status").read_text(errors="replace").splitlines():
            if line.startswith("Uid:"):
                uid = int(line.split()[1])
                break
    except (OSError, ValueError, IndexError):
        pass
    flatpak = None
    try:
        m = _SCOPE.search((base / "cgroup").read_text(errors="replace"))
        flatpak = m.group(1) if m else None
    except OSError:
        pass
    if not exe and not argv:
        return None
    exe = exe.removesuffix(" (deleted)")
    # The program, and for an interpreter its script or "-m module"; not the
    # files it opened (an editor with "vilna-notes.txt" stays usable).
    names = {exe}
    first = os.path.basename(argv[0]) if argv else ""
    names.add(first)
    if _INTERPRETER.fullmatch(exe) or _INTERPRETER.fullmatch(first):
        rest = list(argv[1:])
        while rest:
            a = rest.pop(0)
            if a == "-m" and rest:
                names.add(rest.pop(0))
                break
            if not a.startswith("-"):
                names.add(os.path.basename(a))
                break
    names.discard("")
    return Process(pid, uid, exe, argv, flatpak, frozenset(names))


def protected(
    p: Process | None, own_uid: int | None = None, own_pid: int | None = None
) -> str | None:
    """Why the app is out of Gloxi's reach, or None if Gloxi may use it."""
    if p is None:
        return "the app could not be told"
    own_uid = os.getuid() if own_uid is None else own_uid
    own_pid = os.getpid() if own_pid is None else own_pid
    if p.pid == own_pid:
        return "this tool itself"
    if p.uid is None or p.uid != own_uid:
        return "an app of another user"
    if p.flatpak and (p.flatpak in FLATPAKS or p.flatpak.lower().startswith(OWN_PREFIXES)):
        return "a protected app"
    for name in p.names:
        lowered = name.lower()
        if lowered in SECURITY or lowered in ADMINISTRATION or lowered.startswith(OWN_PREFIXES):
            return "a protected app"
    return None
