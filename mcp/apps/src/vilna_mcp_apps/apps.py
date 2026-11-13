# SPDX-License-Identifier: Apache-2.0
"""The operations of the "programs" tool (ADR 0012, section 3).

Herkos decides whether an operation may run (search R0, install R3...);
this module only does it, and only in a narrow way of its own:

- apps come only from the remotes in ``ALLOWED_REMOTES``, which must already
  be set up on the computer: the tool never adds a remote, never installs
  from a ``.flatpakref`` or ``.flatpak`` file, and never touches runtimes
  directly (Flatpak brings the ones an app needs);
- removing an app keeps its data in ``~/.var/app`` (removing that is R4 and
  not this tool's job);
- every change returns ``undo_arguments``, so Mitos can undo it through
  ``restore``.
"""

from __future__ import annotations

import configparser
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from vilna_mcp_apps.appstream import Component, is_app_id

# Flathub, and Vilna's own Flatpak repository (ADR 0024, `vilna.flatpakrepo`).
ALLOWED_REMOTES = ("flathub", "vilna")
VILNA_APP_PREFIX = "io.github.stdenis1980."
MAX_RESULTS = 25
MAX_QUERY_CHARS = 200


class AppsError(Exception):
    """An error Gloxi can act on: what was wrong, and what is allowed."""


@dataclass
class RemoteRef:
    """An app as a remote has it right now."""

    commit: str
    download_size: int
    installed_size: int
    metadata: str  # the ref's metadata keyfile: [Application], [Context]...


@dataclass
class Installed:
    id: str
    origin: str
    commit: str
    version: str = ""
    name: str = ""
    installed_size: int = 0


class Backend(Protocol):
    def remotes(self) -> list[str]: ...
    def components(self, remote: str) -> Iterable[Component]: ...
    def remote_ref(self, remote: str, app_id: str) -> RemoteRef | None: ...
    def installed(self) -> list[Installed]: ...
    def install(self, remote: str, app_id: str) -> Installed: ...
    def update(self, app_ids: list[str]) -> list[str]: ...
    def uninstall(self, app_id: str) -> Installed: ...


# What a person should know before saying yes, from the ref's [Context]
# (flatpak-metadata(5)). Only what reaches beyond the app's own sandbox.
_FILESYSTEMS = {
    "host": "all files on the computer",
    "host-os": "the system's programs and libraries",
    "host-etc": "the system's settings (/etc)",
    "home": "your whole home folder",
}
_SOCKETS = {
    "x11": "the X11 display (can see other X11 windows)",
    "session-bus": "every program on your session bus",
    "system-bus": "every service on the system bus",
    "ssh-auth": "your SSH keys (through the agent)",
    "pcsc": "smart cards",
    "cups": "printers",
    "gpg-agent": "your GPG keys (through the agent)",
}
_DEVICES = {"all": "all devices (camera, microphone, USB…)", "input": "input devices"}
_SHARED = {"network": "the internet and local network"}


def permissions(metadata: str) -> list[str]:
    """The Flatpak permissions of an app that reach beyond its sandbox, readable."""
    kf = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        kf.read_string(metadata)
    except configparser.Error:
        return ["its permissions could not be read"]
    if not kf.has_section("Context"):
        return []
    ctx = kf["Context"]

    def items(key: str) -> list[str]:
        return [x.strip() for x in ctx.get(key, "").split(";") if x.strip()]

    out = [_SHARED[x] for x in items("shared") if x in _SHARED]
    out += [_SOCKETS[x] for x in items("sockets") if x in _SOCKETS]
    out += [_DEVICES[x] for x in items("devices") if x in _DEVICES]
    for fs in items("filesystems"):
        base, _, mode = fs.partition(":")
        if base.startswith("!"):
            continue
        what = _FILESYSTEMS.get(base)
        if what is None and base.startswith(("/", "~/", "xdg-")):
            what = f"the folder {base}"
        if what:
            out.append(what + (" (read only)" if mode == "ro" else ""))
    return list(dict.fromkeys(out))


def human_size(n: int) -> str:
    if n <= 0:
        return "unknown"
    size = float(n)
    for unit in ("B", "kB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "kB") else f"{size:.1f} {unit}"
        size /= 1000
    return f"{n} B"


@dataclass
class Apps:
    backend: Backend
    picks: dict[str, str] = field(default_factory=dict)  # app id -> section
    _catalogue: dict[str, dict[str, Component]] | None = None

    # ------------------------------------------------------------ helpers

    def _remotes(self) -> list[str]:
        """The allowed remotes that are set up here, in ALLOWED_REMOTES order."""
        here = set(self.backend.remotes())
        return [r for r in ALLOWED_REMOTES if r in here]

    def catalogue(self) -> dict[str, dict[str, Component]]:
        if self._catalogue is None:
            self._catalogue = {}
            for remote in self._remotes():
                try:
                    self._catalogue[remote] = {c.id: c for c in self.backend.components(remote)}
                except (OSError, ValueError) as e:
                    raise AppsError(f"the catalogue of {remote} cannot be read: {e}") from e
        return self._catalogue

    def _check_id(self, app_id: object) -> str:
        if not is_app_id(app_id):
            raise AppsError(
                f"'{app_id}' is not a Flatpak app id; use an id from search, "
                "such as org.gnome.Calculator"
            )
        return str(app_id)

    def _remote_for(self, app_id: str, remote: str | None) -> tuple[str, Component | None]:
        allowed = self._remotes()
        if remote is not None:
            if remote not in ALLOWED_REMOTES:
                raise AppsError(
                    f"apps come only from {', '.join(ALLOWED_REMOTES)}; another source "
                    "is something the person adds themselves in Software"
                )
            if remote not in allowed:
                raise AppsError(f"the source {remote} is not set up on this computer")
            return remote, self.catalogue().get(remote, {}).get(app_id)
        found = [(r, self.catalogue().get(r, {}).get(app_id)) for r in allowed]
        found = [(r, c) for r, c in found if c is not None]
        if not found:
            # An app the catalogue does not describe yet (a new upload).
            found = [(r, None) for r in allowed if self.backend.remote_ref(r, app_id)]
        if not found:
            raise AppsError(
                f"{app_id} is not in {' or '.join(allowed) or 'any app source here'}; "
                "search for it first"
            )
        # Vilna's own apps (Tinta Office, ADR 0024) come from Vilna's
        # repository; everything else from Flathub first.
        own = app_id.startswith(VILNA_APP_PREFIX)
        found.sort(key=lambda rc: (rc[0] == "vilna") != own)
        return found[0]

    def _installed(self) -> dict[str, Installed]:
        return {i.id: i for i in self.backend.installed()}

    def _entry(self, remote: str, c: Component, installed: set[str], language: str) -> dict:
        return {
            "app_id": c.id,
            "name": c.name(language),
            "summary": c.summary(language),
            "developer": c.developer,
            "license": c.license,
            "remote": remote,
            "verified": c.verified or remote == "vilna",
            "pick": self.picks.get(c.id),
            "installed": c.id in installed,
        }

    # --------------------------------------------------------- operations

    def search(self, query: str, limit: int = 10, language: str = "") -> dict:
        """Apps whose name, summary or keywords match every word of ``query``.

        «Рекомендовано Vilna» comes first (ADR 0012, section 6), then apps
        verified on Flathub, then the rest.
        """
        words = query.casefold().split()
        if not words or len(query) > MAX_QUERY_CHARS:
            raise AppsError(f"say what to look for, in 1 to {MAX_QUERY_CHARS} characters")
        limit = max(1, min(limit, MAX_RESULTS))
        installed = set(self._installed())
        hits = []
        seen: set[str] = set()
        for remote, components in self.catalogue().items():
            for c in components.values():
                if c.id in seen:
                    continue
                text = c.words()
                if all(w in text for w in words):
                    seen.add(c.id)
                    in_name = all(w in c.name(language).casefold() for w in words)
                    rank = (c.id not in self.picks, not (c.verified or remote == "vilna"))
                    hits.append((rank, not in_name, c.name(language).casefold(), remote, c))
        hits.sort(key=lambda h: h[:3])
        apps = [self._entry(r, c, installed, language) for *_, r, c in hits[:limit]]
        return {"apps": apps, "more": max(0, len(hits) - limit)}

    def describe(self, app_id: str, remote: str | None = None, language: str = "") -> dict:
        """Everything the Herkos dialog shows before an install (ADR 0012, section 5)."""
        app_id = self._check_id(app_id)
        installed = self._installed()
        if remote is None and app_id in installed and installed[app_id].origin in ALLOWED_REMOTES:
            remote = installed[app_id].origin
        remote, c = self._remote_for(app_id, remote)
        ref = self.backend.remote_ref(remote, app_id)
        if c is None and ref is None:
            raise AppsError(f"{app_id} is not in {remote}")
        entry = (
            self._entry(remote, c, set(installed), language)
            if c is not None
            else {
                "app_id": app_id,
                "name": app_id,
                "summary": "",
                "developer": "",
                "license": "",
                "remote": remote,
                "verified": remote == "vilna",
                "pick": self.picks.get(app_id),
                "installed": app_id in installed,
            }
        )
        perms = permissions(ref.metadata) if ref else []
        entry.update(
            download_size=ref.download_size if ref else 0,
            installed_size=ref.installed_size if ref else 0,
            permissions=perms,
        )
        entry["facts"] = facts(entry)
        return entry

    def list_installed(self, language: str = "") -> dict:
        apps = []
        for i in sorted(self._installed().values(), key=lambda i: i.id):
            apps.append(
                {
                    "app_id": i.id,
                    "name": i.name or i.id,
                    "version": i.version,
                    "remote": i.origin,
                    "pick": self.picks.get(i.id),
                    "installed_size": i.installed_size,
                }
            )
        return {"apps": apps}

    def install(self, app_id: str, remote: str | None = None) -> dict:
        app_id = self._check_id(app_id)
        if app_id in self._installed():
            raise AppsError(f"{app_id} is already installed; use update to get a newer version")
        remote, _c = self._remote_for(app_id, remote)
        done = self.backend.install(remote, app_id)
        return {
            "installed": app_id,
            "remote": remote,
            "commit": done.commit,
            "undo_arguments": {"action": "uninstall", "app_id": app_id},
        }

    def update(self, app_id: str | None = None) -> dict:
        installed = self._installed()
        if app_id is not None:
            app_id = self._check_id(app_id)
            if app_id not in installed:
                raise AppsError(f"{app_id} is not installed")
            ids = [app_id]
        else:
            ids = sorted(installed)
        ids = [i for i in ids if installed[i].origin in ALLOWED_REMOTES]
        updated = self.backend.update(ids)
        # Flatpak keeps no older version to go back to: an update is undone
        # by the next one, not by Mitos.
        return {"updated": updated}

    def uninstall(self, app_id: str) -> dict:
        app_id = self._check_id(app_id)
        installed = self._installed()
        if app_id not in installed:
            raise AppsError(f"{app_id} is not installed")
        origin = installed[app_id].origin
        done = self.backend.uninstall(app_id)
        undo = {"action": "install", "app_id": app_id}
        if origin in ALLOWED_REMOTES:
            undo["remote"] = origin
        return {
            "uninstalled": app_id,
            "commit": done.commit,
            "data_kept": f"~/.var/app/{app_id}",
            "undo_arguments": undo,
        }

    def restore(self, action: str, app_id: str, remote: str | None = None) -> dict:
        """Undo an install (remove the app again) or a removal (install it again)."""
        if action == "uninstall":
            return self.uninstall(app_id)
        if action == "install":
            return self.install(app_id, remote)
        raise AppsError("action must be install or uninstall")


def facts(entry: dict) -> dict:
    """The lines of the Herkos dialog, in the order a person reads them."""
    verified = "yes" if entry["verified"] else "no: Flathub has not checked who makes it"
    if entry["remote"] == "vilna":
        verified = "a Vilna app"
    pick = f"yes ({entry['pick']})" if entry.get("pick") else "no: Vilna has not checked this app"
    return {
        "App": f"{entry['name']} ({entry['app_id']})",
        "Developer": entry["developer"] or "not given",
        "Verified": verified,
        "In Recommended by Vilna": pick,
        "Source": entry["remote"],
        "Download": human_size(entry.get("download_size", 0)),
        "License": entry["license"] or "not given",
        "Can reach": "; ".join(entry.get("permissions") or []) or "only its own files",
    }


class MemoryBackend:
    """A computer with Flathub and Vilna set up, in memory (tests, demos)."""

    def __init__(
        self,
        catalogues: dict[str, list[Component]],
        refs: dict[tuple[str, str], RemoteRef] | None = None,
        installed: list[Installed] | None = None,
    ) -> None:
        self._catalogues = catalogues
        self._refs = refs or {}
        self._installed = {i.id: i for i in installed or []}
        self.log: list[tuple[str, ...]] = []

    def remotes(self) -> list[str]:
        return list(self._catalogues)

    def components(self, remote: str) -> list[Component]:
        return self._catalogues[remote]

    def remote_ref(self, remote: str, app_id: str) -> RemoteRef | None:
        if (remote, app_id) in self._refs:
            return self._refs[(remote, app_id)]
        if any(c.id == app_id for c in self._catalogues.get(remote, [])):
            return RemoteRef("c0ffee", 50_000_000, 150_000_000, "[Application]\n")
        return None

    def installed(self) -> list[Installed]:
        return list(self._installed.values())

    def install(self, remote: str, app_id: str) -> Installed:
        ref = self.remote_ref(remote, app_id)
        if ref is None:
            raise AppsError(f"{app_id} is not in {remote}")
        self.log.append(("install", remote, app_id))
        i = Installed(app_id, remote, ref.commit, "1.0", app_id, ref.installed_size)
        self._installed[app_id] = i
        return i

    def update(self, app_ids: list[str]) -> list[str]:
        self.log.append(("update", *app_ids))
        return []

    def uninstall(self, app_id: str) -> Installed:
        self.log.append(("uninstall", app_id))
        return self._installed.pop(app_id)
