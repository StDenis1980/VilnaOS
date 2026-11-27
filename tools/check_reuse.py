#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the licence bookkeeping of the repo the way REUSE 3.x asks for it.

The checker reads the tracked files (``git ls-files``; nothing else is run, nothing is
written, the network is not used) and verifies:

* ``licence-text-missing``: every SPDX identifier named in a file header (also in a
  ``.license`` file, a ``REUSE.toml`` annotation or a ``.reuse/dep5`` stanza) has a text file
  ``LICENSES/<id>.txt``. In ``A WITH B`` both ``A`` and the exception ``B`` need one; a
  trailing ``+`` is dropped (``Apache-2.0+`` needs ``Apache-2.0.txt``);
* ``licence-unused``: ``LICENSES/`` has no file that nothing uses. A text counts as used when
  a header, a ``.license`` file, an annotation or a ``LICENSES/<id>.txt`` link in
  ``LICENSING.md`` names it (that file explains graphics and sounds that cannot carry a
  header);
* ``licences-bad-entry``: ``LICENSES/`` holds only flat, non-empty ``<id>.txt`` files with a
  well-formed id;
* ``no-licence-info``: a file without a licence header (images, sounds, JSON, ...) has a
  ``.license`` file next to it, a ``REUSE.toml``/``dep5`` annotation, or an entry in
  ``LICENSING.md``: a path in backticks that is a file, a folder (ending in ``/``) or a glob
  that covers it. A tag counts only in the first 4096 bytes and only in a comment line, so a
  string in a test does not count. ``LICENSES/*``, ``LICENSE*``, ``COPYING*``, ``*.license``,
  ``REUSE.toml`` and ``.reuse/dep5`` need no header;
* ``bad-expression`` and ``deprecated-id``: the header value is a well-formed SPDX
  expression (``AND``, ``OR``, ``WITH``, brackets, ``+``); a deprecated identifier such as
  ``GPL-3.0`` is a warning;
* ``orphan-sidecar``, ``sidecar-no-licence``, ``empty-copyright``, ``bad-reuse-toml``,
  ``bad-dep5``: broken helper files and empty tags;
* ``licensing-dead-link``: ``LICENSING.md`` links a ``LICENSES/<id>.txt`` that is missing;
  a path in backticks there that matches no tracked file is a warning (``licensing-stale``);
* ``no-copyright`` (always a warning): REUSE 3.x wants ``SPDX-FileCopyrightText`` for every
  file (in the header, in the ``.license`` file or in an annotation). The repo has none yet,
  so the warnings are counted per top-level folder; ``--verbose`` lists every file.

Known old violations are listed in ``LEGACY`` and are warnings; a new violation is an error,
and an entry that no longer matches anything is a warning (delete it). The references to the
REUSE specification are the author's reading of it and are not legal advice.

Run: python3 tools/check_reuse.py [--verbose] [--root DIR]
Exit code: 0 all good (warnings allowed), 1 problems found, 2 wrong call.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent

ROOT_GROUP = "(top-level files)"
LICENSES_DIR = "LICENSES"
LICENSING_MD = "LICENSING.md"
HEADER_BYTES = 4096  # REUSE reads the beginning of a file only
MAX_EXPRESSION_DEPTH = 32
MIN_LICENCE_TEXT = 100  # characters other than white space
# Identifiers that SPDX has deprecated, with the ones to write instead.
DEPRECATED_IDS = {
    "GPL-2.0": "GPL-2.0-only",
    "GPL-3.0": "GPL-3.0-only",
    "GPL-2.0+": "GPL-2.0-or-later",
    "GPL-3.0+": "GPL-3.0-or-later",
    "LGPL-2.1": "LGPL-2.1-only",
    "LGPL-3.0": "LGPL-3.0-only",
    "LGPL-2.1+": "LGPL-2.1-or-later",
    "LGPL-3.0+": "LGPL-3.0-or-later",
    "AGPL-3.0": "AGPL-3.0-only",
    "AGPL-3.0+": "AGPL-3.0-or-later",
    "GFDL-1.3": "GFDL-1.3-only",
}

# Known old violations. They are reported as warnings, so that the check can run in CI;
# fix the file, then delete the line. Each entry is (path from the repo root, rule).
LEGACY: frozenset[tuple[str, str]] = frozenset(
    {
        # The licence text of the metainfo header is not in LICENSES/ yet.
        (
            "thirdparty/claude-desktop/io.github.stdenis1980.ClaudeDesktop.metainfo.xml",
            "licence-text-missing",
        ),
        # Top-level Markdown files and .gitignore carry no header.
        (".gitignore", "no-licence-info"),
        ("README.es.md", "no-licence-info"),
        ("README.md", "no-licence-info"),
        ("README.uk.md", "no-licence-info"),
        ("SECURITY.md", "no-licence-info"),
        # Documentation and the lock file of the backup tool carry no header.
        ("backup/README.md", "no-licence-info"),
        ("backup/docs/design.md", "no-licence-info"),
        ("backup/uv.lock", "no-licence-info"),
        # Documentation, icon-theme files and drawings that LICENSING.md does not list by path.
        ("docs/README.md", "no-licence-info"),
        ("docs/accessibility-orca.md", "no-licence-info"),
        ("docs/adr/0001-licensing.md", "no-licence-info"),
        ("docs/adr/0002-first-desktop.md", "no-licence-info"),
        ("docs/adr/0003-implementation-language.md", "no-licence-info"),
        ("docs/adr/0004-local-model-engine.md", "no-licence-info"),
        ("docs/adr/0005-gloxi-herkos-channel.md", "no-licence-info"),
        ("docs/adr/0006-conversational-installer.md", "no-licence-info"),
        ("docs/adr/0007-encrypted-backup.md", "no-licence-info"),
        ("docs/adr/0008-default-local-model.md", "no-licence-info"),
        ("docs/adr/0009-languages.md", "no-licence-info"),
        ("docs/adr/0010-keros-local-model.md", "no-licence-info"),
        ("docs/adr/0011-default-office-suite.md", "no-licence-info"),
        ("docs/adr/0012-recommended-apps-and-install-via-gloxi.md", "no-licence-info"),
        ("docs/adr/0013-herkos-rust.md", "no-licence-info"),
        ("docs/adr/0014-remote-wipe.md", "no-licence-info"),
        ("docs/adr/0015-gloxi-voice.md", "no-licence-info"),
        ("docs/adr/0016-gloxi-for-code-editors.md", "no-licence-info"),
        ("docs/adr/0017-third-party-mcp-servers.md", "no-licence-info"),
        ("docs/adr/0018-error-reports-and-feedback.md", "no-licence-info"),
        ("docs/adr/0019-messengers.md", "no-licence-info"),
        ("docs/adr/0021-vilna-skia.md", "no-licence-info"),
        ("docs/adr/0022-call-recording-and-dictation.md", "no-licence-info"),
        ("docs/adr/0023-gloxi-calendar-settings-hotkey.md", "no-licence-info"),
        ("docs/adr/0024-vilna-flatpak-repository.md", "no-licence-info"),
        ("docs/adr/0025-gloxi-system-service.md", "no-licence-info"),
        ("docs/adr/0026-mitos-btrfs-snapshots.md", "no-licence-info"),
        ("docs/adr/0027-vpn-proton-mullvad.md", "no-licence-info"),
        ("docs/adr/0028-image-downloads.md", "no-licence-info"),
        ("docs/adr/0029-herkos-real-input.md", "no-licence-info"),
        ("docs/adr/0030-gloxi-mail.md", "no-licence-info"),
        ("docs/adr/0031-herkos-portals-dbus-atspi.md", "no-licence-info"),
        ("docs/adr/0032-site-hosts-dns.md", "no-licence-info"),
        ("docs/adr/0033-gloxi-ai-act-transparency.md", "no-licence-info"),
        ("docs/adr/README.md", "no-licence-info"),
        ("docs/alpha-freeze.md", "no-licence-info"),
        ("docs/benchmarks.md", "no-licence-info"),
        ("docs/design/brief.md", "no-licence-info"),
        ("docs/design/desktop/README.md", "no-licence-info"),
        ("docs/design/icons/README.md", "no-licence-info"),
        ("docs/design/icons/io.github.stdenis1980.Gloxi-symbolic.svg", "no-licence-info"),
        ("docs/design/icons/io.github.stdenis1980.Gloxi.svg", "no-licence-info"),
        ("docs/design/icons/system/README.md", "no-licence-info"),
        ("docs/design/icons/system/VERSION", "no-licence-info"),
        ("docs/design/icons/system/Vilna-Classic/index.theme", "no-licence-info"),
        (
            "docs/design/icons/system/Vilna-Classic/scalable/devices/drive-harddisk.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna-Classic/scalable/devices/drive-removable-media.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna-Classic/scalable/places/folder-documents.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna-Classic/scalable/places/folder-download.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna-Classic/scalable/places/folder-music.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna-Classic/scalable/places/folder-pictures.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna-Classic/scalable/places/folder-publicshare.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna-Classic/scalable/places/folder-templates.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna-Classic/scalable/places/folder-videos.svg",
            "no-licence-info",
        ),
        ("docs/design/icons/system/Vilna-Classic/scalable/places/folder.svg", "no-licence-info"),
        (
            "docs/design/icons/system/Vilna-Classic/scalable/places/user-desktop.svg",
            "no-licence-info",
        ),
        ("docs/design/icons/system/Vilna-Classic/scalable/places/user-home.svg", "no-licence-info"),
        (
            "docs/design/icons/system/Vilna-Classic/scalable/places/user-trash-full.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna-Classic/scalable/places/user-trash.svg",
            "no-licence-info",
        ),
        ("docs/design/icons/system/Vilna/index.theme", "no-licence-info"),
        ("docs/design/icons/system/Vilna/scalable/devices/drive-harddisk.svg", "no-licence-info"),
        (
            "docs/design/icons/system/Vilna/scalable/devices/drive-removable-media.svg",
            "no-licence-info",
        ),
        ("docs/design/icons/system/Vilna/scalable/places/folder-documents.svg", "no-licence-info"),
        ("docs/design/icons/system/Vilna/scalable/places/folder-download.svg", "no-licence-info"),
        ("docs/design/icons/system/Vilna/scalable/places/folder-music.svg", "no-licence-info"),
        ("docs/design/icons/system/Vilna/scalable/places/folder-pictures.svg", "no-licence-info"),
        (
            "docs/design/icons/system/Vilna/scalable/places/folder-publicshare.svg",
            "no-licence-info",
        ),
        ("docs/design/icons/system/Vilna/scalable/places/folder-templates.svg", "no-licence-info"),
        ("docs/design/icons/system/Vilna/scalable/places/folder-videos.svg", "no-licence-info"),
        ("docs/design/icons/system/Vilna/scalable/places/folder.svg", "no-licence-info"),
        ("docs/design/icons/system/Vilna/scalable/places/user-desktop.svg", "no-licence-info"),
        ("docs/design/icons/system/Vilna/scalable/places/user-home.svg", "no-licence-info"),
        ("docs/design/icons/system/Vilna/scalable/places/user-trash-full.svg", "no-licence-info"),
        ("docs/design/icons/system/Vilna/scalable/places/user-trash.svg", "no-licence-info"),
        (
            "docs/design/icons/system/Vilna/symbolic/actions/vilna-consent-symbolic.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna/symbolic/actions/vilna-key-symbolic.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna/symbolic/actions/vilna-mic-symbolic.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna/symbolic/actions/vilna-model-symbolic.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna/symbolic/status/vilna-cloud-symbolic.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna/symbolic/status/vilna-local-symbolic.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna/symbolic/status/vilna-masked-symbolic.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna/symbolic/status/vilna-risk-0-symbolic.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna/symbolic/status/vilna-risk-1-symbolic.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna/symbolic/status/vilna-risk-2-symbolic.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna/symbolic/status/vilna-risk-3-symbolic.svg",
            "no-licence-info",
        ),
        (
            "docs/design/icons/system/Vilna/symbolic/status/vilna-risk-4-symbolic.svg",
            "no-licence-info",
        ),
        ("docs/design/icons/vilna-backup-symbolic.svg", "no-licence-info"),
        ("docs/design/icons/vilna-backup.svg", "no-licence-info"),
        ("docs/design/icons/vilna-installer-symbolic.svg", "no-licence-info"),
        ("docs/design/icons/vilna-installer.svg", "no-licence-info"),
        ("docs/design/icons/vilna-permissions-symbolic.svg", "no-licence-info"),
        ("docs/design/icons/vilna-permissions.svg", "no-licence-info"),
        ("docs/design/logo/Vilna_icon.svg", "no-licence-info"),
        ("docs/design/logo/Vilna_icon_small.svg", "no-licence-info"),
        ("docs/design/logo/Vilna_icon_small_mono.svg", "no-licence-info"),
        ("docs/design/logo/Vilna_logo_color.svg", "no-licence-info"),
        ("docs/design/logo/Vilna_logo_dark-bg.svg", "no-licence-info"),
        ("docs/design/logo/Vilna_logo_mono.svg", "no-licence-info"),
        ("docs/design/sounds/README.md", "no-licence-info"),
        ("docs/getting-started.es.md", "no-licence-info"),
        ("docs/getting-started.md", "no-licence-info"),
        ("docs/getting-started.uk.md", "no-licence-info"),
        ("docs/roadmap.md", "no-licence-info"),
        # The lock file of the installer carries no header.
        ("installer/README.md", "no-licence-info"),
        ("installer/uv.lock", "no-licence-info"),
        # OBS package data (changelogs, drop-ins, presets, rules) carries no header.
        ("obs/README.md", "no-licence-info"),
        ("obs/images/Vilna-Live/_constraints", "no-licence-info"),
        ("obs/images/Vilna/_constraints", "no-licence-info"),
        ("obs/images/tik-osimage-Vilna/tik-osimage-Vilna.changes", "no-licence-info"),
        ("obs/packages/Vilna-release/Vilna-release.changes", "no-licence-info"),
        ("obs/packages/Vilna-release/os-release", "no-licence-info"),
        ("obs/packages/Vilna-release/vilna-obs.asc", "no-licence-info"),
        ("obs/packages/Vilna-release/vilna.repo", "no-licence-info"),
        (
            "obs/packages/distribution-logos-Vilna/distribution-logos-Vilna.changes",
            "no-licence-info",
        ),
        ("obs/packages/distribution-logos-Vilna/vilna-logo-dark-bg.svg", "no-licence-info"),
        ("obs/packages/distribution-logos-Vilna/vilna-logo-login.svg", "no-licence-info"),
        ("obs/packages/gloxi/gloxi.changes", "no-licence-info"),
        (
            "obs/packages/gnome-shell-extension-dash-to-dock/gnome-shell-extension-dash-to-dock.changes",
            "no-licence-info",
        ),
        (
            "obs/packages/gnome-shell-extension-vilna/gnome-shell-extension-vilna.changes",
            "no-licence-info",
        ),
        ("obs/packages/herkos/herkos.changes", "no-licence-info"),
        ("obs/packages/mullvad-vpn/mullvad-vpn.changes", "no-licence-info"),
        ("obs/packages/patterns-vilna/patterns-vilna.changes", "no-licence-info"),
        ("obs/packages/plymouth-branding-Vilna/plymouth-branding-Vilna.changes", "no-licence-info"),
        ("obs/packages/ptyxis/ptyxis.changes", "no-licence-info"),
        ("obs/packages/ptyxis/vilna-right-click-paste.patch", "no-licence-info"),
        ("obs/packages/ptyxis/vilna-terminal-symbolic.svg", "no-licence-info"),
        ("obs/packages/ptyxis/vilna-terminal.svg", "no-licence-info"),
        (
            "obs/packages/python-vilna-privacy-gateway/python-vilna-privacy-gateway.changes",
            "no-licence-info",
        ),
        ("obs/packages/vilna-backgrounds/vilna-backgrounds.changes", "no-licence-info"),
        ("obs/packages/vilna-backgrounds/vilna-backgrounds.xml", "no-licence-info"),
        ("obs/packages/vilna-backup/vilna-backup.changes", "no-licence-info"),
        ("obs/packages/vilna-branding/30_Vilna.gschema.override", "no-licence-info"),
        ("obs/packages/vilna-branding/49-vilna.rules", "no-licence-info"),
        ("obs/packages/vilna-branding/50-vilna-after-bless-boot.conf", "no-licence-info"),
        ("obs/packages/vilna-branding/50-vilna-desktop.conf", "no-licence-info"),
        ("obs/packages/vilna-branding/50-vilna-dns.conf", "no-licence-info"),
        ("obs/packages/vilna-branding/50-vilna-last-working.conf", "no-licence-info"),
        ("obs/packages/vilna-branding/50-vilna.sudoers", "no-licence-info"),
        ("obs/packages/vilna-branding/87-default-Vilna-user.preset", "no-licence-info"),
        ("obs/packages/vilna-branding/87-default-Vilna.preset", "no-licence-info"),
        ("obs/packages/vilna-branding/90-default-Vilna.preset", "no-licence-info"),
        ("obs/packages/vilna-branding/conncheck-vilna.conf", "no-licence-info"),
        ("obs/packages/vilna-branding/distrobox-upgrade-all.service", "no-licence-info"),
        ("obs/packages/vilna-branding/distrobox-upgrade-all.timer", "no-licence-info"),
        ("obs/packages/vilna-branding/dns-vilna.conf", "no-licence-info"),
        ("obs/packages/vilna-branding/vendor.conf", "no-licence-info"),
        ("obs/packages/vilna-branding/vilna-branding-rpmlintrc", "no-licence-info"),
        ("obs/packages/vilna-branding/vilna-branding.changes", "no-licence-info"),
        ("obs/packages/vilna-branding/vilna-dns.tmpfiles", "no-licence-info"),
        ("obs/packages/vilna-branding/x86_64_v3-transactional-update.service", "no-licence-info"),
        ("obs/packages/vilna-branding/zzz-vilna.conf", "no-licence-info"),
        ("obs/packages/vilna-flatpak-defaults/vilna-flatpak-defaults.changes", "no-licence-info"),
        ("obs/packages/vilna-flatpak-defaults/vilna.flatpakrepo", "no-licence-info"),
        ("obs/packages/vilna-icon-theme/vilna-icon-theme.changes", "no-licence-info"),
        ("obs/packages/vilna-installer/vilna-installer.changes", "no-licence-info"),
        ("obs/packages/vilna-mcp-apps/vilna-mcp-apps.changes", "no-licence-info"),
        ("obs/packages/vilna-mcp-backup/vilna-mcp-backup.changes", "no-licence-info"),
        ("obs/packages/vilna-mcp-calendar/vilna-mcp-calendar.changes", "no-licence-info"),
        ("obs/packages/vilna-mcp-desktop/vilna-mcp-desktop.changes", "no-licence-info"),
        ("obs/packages/vilna-mcp-files/vilna-mcp-files.changes", "no-licence-info"),
        ("obs/packages/vilna-mcp-installer/vilna-mcp-installer.changes", "no-licence-info"),
        ("obs/packages/vilna-mcp-mail/vilna-mcp-mail.changes", "no-licence-info"),
        ("obs/packages/vilna-mcp-settings/vilna-mcp-settings.changes", "no-licence-info"),
        ("obs/packages/vilna-model-default/vilna-model-default.changes", "no-licence-info"),
        ("obs/packages/vilna-presets/50-vilna-user.preset", "no-licence-info"),
        ("obs/packages/vilna-presets/50-vilna.preset", "no-licence-info"),
        ("obs/packages/vilna-presets/vilna-presets.changes", "no-licence-info"),
        ("obs/packages/vilna-software-picks/README.md", "no-licence-info"),
        ("obs/packages/vilna-software-picks/vilna-software-picks.changes", "no-licence-info"),
        ("obs/packages/vilna-sound-themes/vilna-sound-themes.changes", "no-licence-info"),
        # Reports and lock files carry no header.
        ("reports/Cargo.lock", "no-licence-info"),
        ("reports/README.md", "no-licence-info"),
        ("reports/client/README.md", "no-licence-info"),
        ("reports/docs/deploy.md", "no-licence-info"),
        ("reports/docs/operations.md", "no-licence-info"),
        ("reports/docs/protocol.md", "no-licence-info"),
        ("reports/feedback/README.md", "no-licence-info"),
        ("reports/feedback/uv.lock", "no-licence-info"),
        # Test data, metadata and docs of the Shell extension carry no header.
        ("shell-extension/README.md", "no-licence-info"),
        ("shell-extension/package.json", "no-licence-info"),
        ("shell-extension/tests/shell/alert-cap.xml", "no-licence-info"),
        ("shell-extension/tests/shell/alerts.atom", "no-licence-info"),
        ("shell-extension/tests/shell/geocodes.json", "no-licence-info"),
        ("shell-extension/tests/shell/vilna-test@vilna.test/metadata.json", "no-licence-info"),
        ("shell-extension/tests/shell/weather.json", "no-licence-info"),
        ("shell-extension/vilna@stdenis1980.github.io/icons/README.md", "no-licence-info"),
        (
            "shell-extension/vilna@stdenis1980.github.io/icons/vilna-clock-stopwatch-symbolic.svg",
            "no-licence-info",
        ),
        (
            "shell-extension/vilna@stdenis1980.github.io/icons/vilna-clock-timer-symbolic.svg",
            "no-licence-info",
        ),
        (
            "shell-extension/vilna@stdenis1980.github.io/icons/vilna-clock-world-symbolic.svg",
            "no-licence-info",
        ),
        ("shell-extension/vilna@stdenis1980.github.io/metadata.json", "no-licence-info"),
        # Pages, data and images of the site carry no header.
        ("site/README.md", "no-licence-info"),
        ("site/deploy/README.md", "no-licence-info"),
        ("site/dns/README.md", "no-licence-info"),
        ("site/public/.well-known/security.txt", "no-licence-info"),
        ("site/public/_headers", "no-licence-info"),
        ("site/public/_redirects", "no-licence-info"),
        ("site/public/assets/blavet-dark.jpg", "no-licence-info"),
        ("site/public/assets/blavet-light.jpg", "no-licence-info"),
        ("site/public/assets/blavet.svg", "no-licence-info"),
        ("site/public/assets/gloxi.svg", "no-licence-info"),
        ("site/public/assets/vilna-icon.svg", "no-licence-info"),
        ("site/public/assets/vilna-logo.svg", "no-licence-info"),
        ("site/public/robots.txt", "no-licence-info"),
        ("site/src/i18n/cs.json", "no-licence-info"),
        ("site/src/i18n/de.json", "no-licence-info"),
        ("site/src/i18n/en.json", "no-licence-info"),
        ("site/src/i18n/es.json", "no-licence-info"),
        ("site/src/i18n/fr.json", "no-licence-info"),
        ("site/src/i18n/it.json", "no-licence-info"),
        ("site/src/i18n/lt.json", "no-licence-info"),
        ("site/src/i18n/pl.json", "no-licence-info"),
        ("site/src/i18n/ro.json", "no-licence-info"),
        ("site/src/i18n/uk.json", "no-licence-info"),
        ("site/src/pages/404.html", "no-licence-info"),
        ("site/src/pages/blavet.html", "no-licence-info"),
        ("site/src/pages/feedback-thanks.html", "no-licence-info"),
        ("site/src/pages/feedback.html", "no-licence-info"),
        ("site/src/pages/gloxi.html", "no-licence-info"),
        ("site/src/pages/home.html", "no-licence-info"),
        ("site/src/pages/legal.html", "no-licence-info"),
        ("site/src/pages/privacy.html", "no-licence-info"),
        ("site/src/pages/security.html", "no-licence-info"),
        # The third-party desktop file carries no header.
        (
            "thirdparty/claude-desktop/io.github.stdenis1980.ClaudeDesktop.desktop",
            "no-licence-info",
        ),
    }
)

TAG = re.compile(
    r"^(?P<lead>[\s#/*<!\-;%{}(.]*)SPDX-(?P<tag>License-Identifier|FileCopyrightText):(?P<value>.*)$"
)
CLOSER = re.compile(r"\s*(?:\*/|-->|\*\)|#\}|\}\})\s*$")
LICENCE_ID = re.compile(
    r"(?:DocumentRef-[A-Za-z0-9.-]+:)?(?:LicenseRef-[A-Za-z0-9.-]+|[A-Za-z0-9][A-Za-z0-9.-]*)"
)
FILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]*(?::LicenseRef-[A-Za-z0-9.-]+)?")
LICENSES_LINK = re.compile(r"LICENSES/([A-Za-z0-9][A-Za-z0-9._+-]*?)\.txt")
LICENCE_FILE_NAME = re.compile(r"(?:LICENSE|COPYING)(?:[.-].*)?")
BACKTICKS = re.compile(r"`([^`\n]+)`")


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    warning: bool = False
    rule: str = ""

    def __str__(self) -> str:
        kind = "warning: " if self.warning else ""
        return f"{self.path}: {kind}{self.message}"


@dataclass
class Tags:
    """The SPDX tags of one header: (line, value) pairs."""

    licences: list[tuple[int, str]] = field(default_factory=list)
    copyrights: list[tuple[int, str]] = field(default_factory=list)


@dataclass(frozen=True)
class Annotation:
    """One ``REUSE.toml`` annotation or one ``dep5`` stanza."""

    source: str
    pattern: re.Pattern[str]
    licences: tuple[str, ...]
    copyrights: tuple[str, ...]


# ---------------------------------------------------------------------------------------
# SPDX expressions


def parse_expression(expr: str) -> list[str]:
    """Return the identifiers of a valid expression as written, or raise ValueError."""
    tokens = re.findall(r"[()]|[^\s()]+", expr)
    if not tokens:
        raise ValueError("empty licence expression")
    pos = 0
    names: list[str] = []

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def take_id(what: str) -> str:
        nonlocal pos
        token = peek()
        if token is None or token in ("(", ")", "AND", "OR", "WITH"):
            raise ValueError(f"expected {what} in the licence expression, found {token or 'end'}")
        base = token.removesuffix("+")
        if not LICENCE_ID.fullmatch(base) or (what == "exception" and token != base):
            raise ValueError(f"'{token}' is not a valid SPDX identifier")
        if base.startswith(("LicenseRef-", "DocumentRef-")) and token != base:
            raise ValueError(f"'{token}': '+' is not allowed after a LicenseRef")
        pos += 1
        return token

    def simple() -> None:
        nonlocal pos
        names.append(take_id("a licence"))
        if peek() == "WITH":
            pos += 1
            names.append(take_id("exception"))

    def compound(depth: int) -> None:
        nonlocal pos
        if depth > MAX_EXPRESSION_DEPTH:
            raise ValueError("the licence expression is nested too deeply")
        if peek() == "(":
            pos += 1
            compound(depth + 1)
            if peek() != ")":
                raise ValueError("missing ')' in the licence expression")
            pos += 1
        else:
            simple()
        while peek() in ("AND", "OR"):
            pos += 1
            if peek() == "(":
                pos += 1
                compound(depth + 1)
                if peek() != ")":
                    raise ValueError("missing ')' in the licence expression")
                pos += 1
            else:
                simple()

    compound(0)
    if pos != len(tokens):
        raise ValueError(f"unexpected '{tokens[pos]}' in the licence expression")
    return names


# ---------------------------------------------------------------------------------------
# Reading files


def read_head(path: Path) -> str:
    """The first bytes of a file as text (bad bytes replaced); '' when unreadable."""
    try:
        with path.open("rb") as handle:
            return handle.read(HEADER_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def read_text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def scan_tags(text: str) -> Tags:
    """Find the SPDX tags that stand in comment lines."""
    tags = Tags()
    for number, line in enumerate(text.splitlines(), start=1):
        match = TAG.match(line)
        if not match:
            continue
        value = CLOSER.sub("", match["value"]).strip()
        target = tags.licences if match["tag"] == "License-Identifier" else tags.copyrights
        target.append((number, value))
    return tags


def glob_regex(pattern: str, star_crosses_slash: bool) -> re.Pattern[str]:
    """Turn a REUSE.toml (``*`` stops at ``/``) or dep5 (``*`` does not) glob into a regex."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i) and not star_crosses_slash:
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append(".*" if star_crosses_slash else "[^/]*")
            i += 1
        elif char == "?" and star_crosses_slash:
            out.append(".")
            i += 1
        elif char == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out))


def as_list(value: object) -> list[str] | None:
    """A TOML string or a list of strings as a list; None for anything else."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


def load_toml_annotations(rel: str, text: str) -> tuple[list[Annotation], list[Problem]]:
    """The annotations of a ``REUSE.toml``; paths are relative to the folder of the file."""
    base = rel.rpartition("/")[0]
    prefix = f"{base}/" if base else ""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        return [], [Problem(rel, f"invalid TOML: {err}", rule="bad-reuse-toml")]
    problems: list[Problem] = []
    found: list[Annotation] = []
    if data.get("version") != 1:
        problems.append(Problem(rel, "'version' must be 1", rule="bad-reuse-toml"))
    annotations = data.get("annotations", [])
    if not isinstance(annotations, list):
        return [], [
            *problems,
            Problem(rel, "'annotations' must be an array of tables", rule="bad-reuse-toml"),
        ]
    for index, item in enumerate(annotations, start=1):
        paths = as_list(item.get("path")) if isinstance(item, dict) else None
        licences = (
            as_list(item.get("SPDX-License-Identifier", [])) if isinstance(item, dict) else None
        )
        copyrights = (
            as_list(item.get("SPDX-FileCopyrightText", [])) if isinstance(item, dict) else None
        )
        if not paths or licences is None or copyrights is None:
            problems.append(
                Problem(
                    rel,
                    f"annotation {index} needs 'path' and string values for the SPDX keys",
                    rule="bad-reuse-toml",
                )
            )
            continue
        regex = glob_regex(prefix, True)
        for pattern in paths:
            full = glob_regex(pattern.lstrip("/"), False)
            found.append(
                Annotation(
                    rel,
                    re.compile(regex.pattern + full.pattern),
                    tuple(licences),
                    tuple(copyrights),
                )
            )
    return found, problems


def load_dep5(rel: str, text: str) -> tuple[list[Annotation], list[Problem]]:
    """The ``Files`` stanzas of a Debian copyright file (``.reuse/dep5``)."""
    found: list[Annotation] = []
    problems: list[Problem] = []
    stanzas = [s for s in re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n")) if s.strip()]
    if not stanzas or not stanzas[0].startswith("Format:"):
        return [], [Problem(rel, "the first stanza must start with 'Format:'", rule="bad-dep5")]
    for stanza in stanzas[1:]:
        fields: dict[str, str] = {}
        key = ""
        for line in stanza.split("\n"):
            if line[:1] in (" ", "\t") and key:
                fields[key] += "\n" + line.strip()
            elif ":" in line and not line.startswith("#"):
                key, _, value = line.partition(":")
                key = key.strip()
                fields[key] = value.strip()
        if "Files" not in fields:
            continue
        licence = fields.get("License", "").split("\n")[0].strip()
        copyright_text = fields.get("Copyright", "").strip()
        if not licence or not copyright_text:
            problems.append(
                Problem(
                    rel,
                    f"stanza for '{fields['Files'][:40]}' needs Copyright and License",
                    rule="bad-dep5",
                )
            )
            continue
        for pattern in fields["Files"].split():
            found.append(Annotation(rel, glob_regex(pattern, True), (licence,), (copyright_text,)))
    return found, problems


# ---------------------------------------------------------------------------------------
# LICENSING.md


@dataclass
class LicensingDoc:
    paths: list[str] = field(default_factory=list)
    linked_ids: list[str] = field(default_factory=list)

    def covers(self, rel: str, tracked_dirs: frozenset[str]) -> bool:
        for entry in self.paths:
            if entry.endswith("/"):
                if rel.startswith(entry):
                    return True
            elif any(c in entry for c in "*?["):
                if fnmatch.fnmatchcase(rel, entry) and rel.count("/") == entry.count("/"):
                    return True
            elif rel == entry or (entry in tracked_dirs and rel.startswith(entry + "/")):
                return True
        return False


def parse_licensing(text: str) -> LicensingDoc:
    doc = LicensingDoc()
    for match in LICENSES_LINK.finditer(text):
        if match[1] not in doc.linked_ids:
            doc.linked_ids.append(match[1])
    for match in BACKTICKS.finditer(text):
        span = match[1].strip()
        if "/" not in span or " " in span or span.startswith(("/", "http", LICENSES_DIR + "/")):
            continue
        if span.removeprefix("./") not in doc.paths:
            doc.paths.append(span.removeprefix("./"))
    return doc


# ---------------------------------------------------------------------------------------
# The check


def is_ignored(rel: str) -> bool:
    """Files that carry no header by design (the REUSE specification ignores them)."""
    name = rel.rpartition("/")[2]
    return (
        rel.startswith(LICENSES_DIR + "/")
        or LICENCE_FILE_NAME.fullmatch(name) is not None
        or name.endswith(".license")
        or name == "REUSE.toml"
        or rel == ".reuse/dep5"
    )


def matching(annotations: Iterable[Annotation], rel: str) -> list[Annotation]:
    return [a for a in annotations if a.pattern.fullmatch(rel)]


def check_licences_dir(
    files: Sequence[str], root: Path, used: set[str], linked: Sequence[str]
) -> list[Problem]:
    problems: list[Problem] = []
    for rel in files:
        if not rel.startswith(LICENSES_DIR + "/"):
            continue
        name = rel[len(LICENSES_DIR) + 1 :]
        if "/" in name:
            problems.append(
                Problem(rel, "LICENSES/ must be flat: no sub-folders", rule="licences-bad-entry")
            )
            continue
        if not name.endswith(".txt") or not FILE_ID.fullmatch(name[:-4]):
            problems.append(
                Problem(
                    rel,
                    "the name must be '<SPDX id>.txt' (letters, digits, '.', '-', '+')",
                    rule="licences-bad-entry",
                )
            )
            continue
        text = read_text(root / rel)
        if text is None or len("".join(text.split())) < MIN_LICENCE_TEXT:
            problems.append(
                Problem(
                    rel,
                    f"the licence text is empty, unreadable or under {MIN_LICENCE_TEXT} characters",
                    rule="licences-bad-entry",
                )
            )
        if name[:-4] not in used and name[:-4] not in linked:
            problems.append(
                Problem(
                    rel,
                    "nothing uses this licence: no header, .license file, annotation or "
                    "LICENSING.md link names it",
                    rule="licence-unused",
                )
            )
    return problems


def check_repo(
    root: Path,
    files: Sequence[str],
    legacy: frozenset[tuple[str, str]] = LEGACY,
    verbose: bool = False,
) -> list[Problem]:
    """Check the given tracked files (paths relative to ``root``)."""
    fileset = frozenset(files)
    problems: list[Problem] = []
    annotations: list[Annotation] = []
    sidecars: dict[str, Tags] = {}

    for rel in sorted(fileset):
        if rel.rpartition("/")[2] == "REUSE.toml":
            text = read_text(root / rel)
            if text is None:
                problems.append(
                    Problem(rel, "cannot read the file as UTF-8", rule="bad-reuse-toml")
                )
            else:
                found, bad = load_toml_annotations(rel, text)
                annotations += found
                problems += bad
        elif rel == ".reuse/dep5":
            text = read_text(root / rel)
            if text is None:
                problems.append(Problem(rel, "cannot read the file as UTF-8", rule="bad-dep5"))
            else:
                found, bad = load_dep5(rel, text)
                annotations += found
                problems += bad
        elif rel.endswith(".license"):
            target = rel[: -len(".license")]
            if target not in fileset:
                problems.append(
                    Problem(
                        rel,
                        f"no tracked file '{target}' next to this .license file",
                        rule="orphan-sidecar",
                    )
                )
                continue
            tags = scan_tags(read_head(root / rel))
            sidecars[target] = tags
            if not tags.licences:
                problems.append(
                    Problem(
                        rel,
                        "no SPDX-License-Identifier in this .license file",
                        rule="sidecar-no-licence",
                    )
                )

    licensing_text = read_text(root / LICENSING_MD) if LICENSING_MD in fileset else None
    doc = parse_licensing(licensing_text or "")
    tracked_dirs = frozenset(
        "/".join(parts[:i])
        for rel in fileset
        for parts in [rel.split("/")]
        for i in range(1, len(parts))
    )
    used: set[str] = set()
    copyright_missing: dict[str, list[str]] = {}
    checked = 0

    for rel in sorted(fileset):
        if is_ignored(rel):
            continue
        path = root / rel
        if path.is_symlink() or not path.is_file():
            continue
        checked += 1
        own = sidecars.get(rel) or scan_tags(read_head(path))
        where = "" if rel not in sidecars else " (.license file)"
        hits = matching(annotations, rel)
        sources = [(number, value, where) for number, value in own.licences]
        for annotation in hits:
            sources += [(None, value, f" ({annotation.source})") for value in annotation.licences]
        for number, value, origin in sources:
            problems += licence_problems(rel, number, value, origin, fileset, used)
        for number, value in own.copyrights:
            if not value:
                problems.append(
                    Problem(
                        rel,
                        f"line {number}: SPDX-FileCopyrightText is empty{where}",
                        rule="empty-copyright",
                    )
                )
        has_copyright = any(v for _, v in own.copyrights) or any(a.copyrights for a in hits)
        if not has_copyright:
            group = rel.split("/")[0] + "/" if "/" in rel else ROOT_GROUP
            copyright_missing.setdefault(group, []).append(rel)
        if not sources and not doc.covers(rel, tracked_dirs):
            problems.append(
                Problem(
                    rel,
                    "no licence information: no SPDX-License-Identifier in the first "
                    f"{HEADER_BYTES} bytes, no .license file, no REUSE annotation and no "
                    "path in LICENSING.md that covers it",
                    rule="no-licence-info",
                )
            )

    problems += check_licences_dir(files, root, used, doc.linked_ids)
    for name in doc.linked_ids:
        if f"{LICENSES_DIR}/{name}.txt" not in fileset:
            problems.append(
                Problem(
                    LICENSING_MD,
                    f"links LICENSES/{name}.txt, which is not tracked",
                    rule="licensing-dead-link",
                )
            )
    for entry in doc.paths:
        if not any(covers_file(entry, rel, tracked_dirs) for rel in fileset):
            problems.append(
                Problem(
                    LICENSING_MD,
                    f"the path `{entry}` matches no tracked file",
                    warning=True,
                    rule="licensing-stale",
                )
            )

    problems = apply_legacy(problems, legacy)
    problems += copyright_warnings(copyright_missing, checked, verbose)
    return sorted(problems, key=lambda p: (p.path, p.message))


def covers_file(entry: str, rel: str, tracked_dirs: frozenset[str]) -> bool:
    return LicensingDoc([entry]).covers(rel, tracked_dirs)


def licence_problems(
    rel: str, number: int | None, value: str, origin: str, fileset: frozenset[str], used: set[str]
) -> list[Problem]:
    """Check one SPDX expression of a file; record the licences it names in ``used``."""
    at = f"line {number}: " if number else ""
    try:
        names = parse_expression(value)
    except ValueError as err:
        return [Problem(rel, f"{at}{err}{origin}", rule="bad-expression")]
    problems: list[Problem] = []
    for name in names:
        text_name = name.removesuffix("+")
        used.add(text_name)
        if f"{LICENSES_DIR}/{text_name}.txt" not in fileset:
            problems.append(
                Problem(
                    rel,
                    f"{at}licence '{text_name}' has no text file "
                    f"{LICENSES_DIR}/{text_name}.txt{origin}",
                    rule="licence-text-missing",
                )
            )
        if name in DEPRECATED_IDS:
            problems.append(
                Problem(
                    rel,
                    f"{at}'{name}' is a deprecated SPDX identifier, write "
                    f"'{DEPRECATED_IDS[name]}'{origin}",
                    warning=True,
                    rule="deprecated-id",
                )
            )
    return problems


def apply_legacy(problems: list[Problem], legacy: frozenset[tuple[str, str]]) -> list[Problem]:
    """Turn known old violations into warnings; stale entries become warnings too."""
    seen: set[tuple[str, str]] = set()
    out: list[Problem] = []
    for problem in problems:
        key = (problem.path, problem.rule)
        if key in legacy and not problem.warning:
            seen.add(key)
            problem = Problem(problem.path, problem.message, warning=True, rule=problem.rule)
        out.append(problem)
    for path, rule in sorted(legacy - seen):
        out.append(
            Problem(
                path,
                f"stale entry in LEGACY for rule '{rule}': the violation is gone, delete the entry",
                warning=True,
                rule="stale-legacy",
            )
        )
    return out


def copyright_warnings(missing: dict[str, list[str]], checked: int, verbose: bool) -> list[Problem]:
    if not missing:
        return []
    total = sum(len(v) for v in missing.values())
    if verbose:
        return [
            Problem(rel, "no SPDX-FileCopyrightText (REUSE 3.x)", warning=True, rule="no-copyright")
            for names in missing.values()
            for rel in names
        ]
    out = [
        Problem(
            group,
            f"{len(names)} files have no SPDX-FileCopyrightText (REUSE 3.x)",
            warning=True,
            rule="no-copyright",
        )
        for group, names in missing.items()
    ]
    out.append(
        Problem(
            ".",
            f"{total} of {checked} files have no SPDX-FileCopyrightText; --verbose lists them",
            warning=True,
            rule="no-copyright",
        )
    )
    return out


def list_files(root: Path) -> list[str]:
    """Tracked files (``git ls-files``); in a folder that is not a git repo, all files."""
    if (root / ".git").exists():
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, text=True
        ).stdout
        return sorted(p for p in out.split("\0") if p)
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    usage = "usage: python3 tools/check_reuse.py [--verbose] [--root DIR]"
    if args in (["-h"], ["--help"]):
        print(usage)
        return 0
    verbose = False
    root = ROOT
    while args:
        arg = args.pop(0)
        if arg == "--verbose":
            verbose = True
        elif arg == "--root" and args:
            root = Path(args.pop(0))
        else:
            print(usage, file=sys.stderr)
            return 2
    try:
        files = list_files(root)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"cannot list files with git ls-files: {err}", file=sys.stderr)
        return 2
    if not files:
        print("no files found", file=sys.stderr)
        return 2
    legacy = LEGACY if root == ROOT else frozenset()
    problems = check_repo(root, files, legacy=legacy, verbose=verbose)
    for problem in problems:
        print(problem, file=sys.stderr)
    errors = sum(not p.warning for p in problems)
    print(f"{len(files)} files checked: {errors} errors, {len(problems) - errors} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
