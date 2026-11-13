# SPDX-License-Identifier: Apache-2.0
"""The system Flatpak installation through libflatpak (PyGObject), the same
library GNOME Software uses (ADR 0012, section 3).

The tool runs as the system user ``vilna-apps`` (vilna-mcp-apps@.service).
Changes go through flatpak-system-helper, which asks polkit; the polkit rule
(mcp/apps/data/50-vilna-apps.rules) lets ``vilna-apps`` install, update and
remove apps from Flathub and Vilna, and nothing else: no new remotes, no
bundles, no configuration. So even a broken tool cannot add a source of
programs.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import gi

gi.require_version("Flatpak", "1.0")
from gi.repository import Flatpak, GLib  # noqa: E402

from vilna_mcp_apps.apps import AppsError, Installed, RemoteRef  # noqa: E402
from vilna_mcp_apps.appstream import Component, read_components  # noqa: E402

# Flathub publishes apps on "stable"; Vilna's repository on "master"
# (gloxi/tools/flatpak_repo.py). A remote's own DefaultBranch wins.
BRANCHES = {"flathub": "stable", "vilna": "master"}


class FlatpakBackend:
    def __init__(self) -> None:
        self.inst = Flatpak.Installation.new_system(None)
        self.arch = Flatpak.get_default_arch()

    def remotes(self) -> list[str]:
        return [r.get_name() for r in self.inst.list_remotes() if not r.get_disabled()]

    def components(self, remote: str) -> Iterator[Component]:
        r = self.inst.get_remote_by_name(remote, None)
        folder = Path(r.get_appstream_dir(self.arch).get_path())
        files = [folder / "appstream.xml.gz", folder / "appstream.xml"]
        if not any(f.exists() for f in files):
            # First use: fetch the catalogue (polkit: appstream-update).
            try:
                self.inst.update_appstream_sync(remote, self.arch, None, None)
            except GLib.Error as e:
                raise OSError(f"cannot fetch the catalogue of {remote}: {e.message}") from e
        for f in files:
            if f.exists():
                return read_components(f)
        return iter(())

    def _branch(self, remote: str) -> str:
        try:
            own = self.inst.get_remote_by_name(remote, None).get_default_branch()
        except GLib.Error:
            own = None
        return own or BRANCHES.get(remote, "stable")

    def _app_ref(self, remote: str, app_id: str) -> str:
        return f"app/{app_id}/{self.arch}/{self._branch(remote)}"

    def remote_ref(self, remote: str, app_id: str) -> RemoteRef | None:
        try:
            ref = self.inst.fetch_remote_ref_sync(
                remote, Flatpak.RefKind.APP, app_id, self.arch, self._branch(remote), None
            )
        except GLib.Error:
            return None
        meta = ref.get_metadata()
        return RemoteRef(
            commit=ref.get_commit() or "",
            download_size=int(ref.get_download_size() or 0),
            installed_size=int(ref.get_installed_size() or 0),
            metadata=meta.get_data().decode("utf-8", "replace") if meta else "",
        )

    def _refs(self) -> dict:
        return {
            ref.get_name(): ref
            for ref in self.inst.list_installed_refs_by_kind(Flatpak.RefKind.APP, None)
            if ref.get_arch() == self.arch
        }

    def installed(self) -> list[Installed]:
        out = []
        for ref in self._refs().values():
            out.append(
                Installed(
                    id=ref.get_name(),
                    origin=ref.get_origin() or "",
                    commit=ref.get_commit() or "",
                    version=ref.get_appdata_version() or "",
                    name=ref.get_appdata_name() or "",
                    installed_size=int(ref.get_installed_size() or 0),
                )
            )
        return out

    def _run(self, add) -> None:
        t = Flatpak.Transaction.new_for_installation(self.inst, None)
        t.set_no_interaction(True)
        # Runtimes an app needs come from where the remote says (RuntimeRepo
        # of vilna.flatpakrepo is Flathub); never from a new remote.
        t.add_default_dependency_sources()
        t.connect("add-new-remote", lambda *_: False)
        add(t)
        try:
            t.run(None)
        except GLib.Error as e:
            raise AppsError(f"Flatpak: {e.message}") from e
        finally:
            _tidy_system_cache()

    def _get(self, app_id: str) -> Installed:
        for i in self.installed():
            if i.id == app_id:
                return i
        raise AppsError(f"{app_id} is not installed")

    def install(self, remote: str, app_id: str) -> Installed:
        ref = self._app_ref(remote, app_id)
        self._run(lambda t: t.add_install(remote, ref, None))
        return self._get(app_id)

    def update(self, app_ids: list[str]) -> list[str]:
        if not app_ids:
            return []
        before = {i.id: i.commit for i in self.installed()}

        refs = self._refs()

        def add(t):
            for app_id in app_ids:
                if app_id in refs:
                    t.add_update(refs[app_id].format_ref(), None, None)

        self._run(add)
        after = {i.id: i.commit for i in self.installed()}
        return [a for a in app_ids if after.get(a) != before.get(a)]

    def uninstall(self, app_id: str) -> Installed:
        was = self._get(app_id)
        ref = self._refs()[app_id].format_ref()
        self._run(lambda t: t.add_uninstall(ref))
        return was


def _tidy_system_cache() -> None:
    """Remove the empty mount points Flatpak leaves in its system cache.

    vilna-mcp-apps@.service turns revokefs-fuse off, and Flatpak does not
    remove the folder it made for it when it falls back to copying. Only empty
    folders go; everything else is Flatpak's own.
    """
    cache = os.environ.get("FLATPAK_SYSTEM_CACHE_DIR")
    if not cache:
        return
    try:
        entries = list(Path(cache).iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink():
                entry.rmdir()
        except OSError:
            pass  # not empty, or gone already
