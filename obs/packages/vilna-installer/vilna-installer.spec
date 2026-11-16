#
# spec file for package vilna-installer
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

# A program, not a library: built for the primary Python only.
%define pythons python3
%global appid io.github.stdenis1980.VilnaInstaller
# Version comes from installer/pyproject.toml; obs/tools/obs_sync.py fills it in.
Name:           vilna-installer
Version:        0.1.0~dev0
Release:        0
Summary:        Vilna OS installer, run from the live image
License:        GPL-3.0-or-later
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        %{name}-%{version}.tar.gz
BuildRequires:  %{python_module base >= 3.11}
BuildRequires:  %{python_module hatchling >= 1.26}
BuildRequires:  %{python_module pip}
BuildRequires:  %{python_module pytest >= 8}
BuildRequires:  %{python_module wheel}
BuildRequires:  desktop-file-utils
BuildRequires:  fdupes
BuildRequires:  hicolor-icon-theme
BuildRequires:  python-rpm-macros
# %%check runs the conversation tests against the installer's MCP server.
BuildRequires:  timezone
BuildRequires:  vilna-mcp-installer
Requires:       python3 >= 3.11
Requires:       python3-gobject
Requires:       typelib(Adw) = 1
Requires:       typelib(Gtk) = 4.0
# The wallpaper is loaded and cropped with GdkPixbuf.
Requires:       typelib(GdkPixbuf) = 2.0
# «Other languages»: every language's own name, as GNOME Settings shows
# it, comes from iso-codes through GnomeDesktop.
Requires:       iso-codes
Requires:       typelib(GnomeDesktop) = 4.0
# The first screen stands on the Blavet wallpaper.
Requires:       vilna-backgrounds >= 0.1
# The conversation with Gloxi (steps 1-13): the installer's MCP server, the
# plan format and its validator; Gloxi is the local model of vilna-llm.service
# (gloxi, vilna-model-default), which the live image has through vilna_base.
Requires:       vilna-mcp-installer
# The Herkos screen asks the TPM whether it can keep the disk key
# (tpm2_getcap).
Requires:       tpm2.0-tools
# The install engine (vilna-installer-engine, run as root after the Herkos
# screen): systemd-repart lays out and encrypts the disk, the rest opens,
# grows and sets up the copied system; sdbootutil runs inside the new one.
Requires:       btrfsprogs
Requires:       cryptsetup
Requires:       dosfstools
Requires:       efibootmgr
Requires:       keyutils
Requires:       sudo
# systemd-repart is in udev on Tumbleweed. Not the file: OBS resolves a
# file dependency only with a FileProvides line in the project config,
# which openSUSE:Factory has not for this file (Vilna-Live was unresolvable).
Requires:       udev
Requires:       util-linux
# TPM2: systemd-cryptenroll and systemd-creds load the TSS libraries
# themselves (dlopen), so nothing pulls them in on its own.
Requires:       libtss2-esys0
Requires:       libtss2-mu0
Requires:       libtss2-rc0
Requires:       libtss2-tcti-device0
# «Screen reader» turns on Orca, as GNOME Settings does.
Recommends:     orca
BuildArch:      noarch

%description
The Vilna OS installer (image/docs/installer.md), started in the live
image. Step 0, the first screen: the ten languages of Vilna and
every other language of the system, the screen reader, and going on with
the Gloxi assistant or without it; then steps 1-13 as a conversation with
Gloxi, with the installation plan beside it. Gloxi only fills the plan;
Herkos's confirmation screen shows every change and the recovery key
of the encrypted disk; then the install engine writes the Vilna disk
image to the disk (systemd-repart, LUKS2 with the recovery key and TPM2
or a passphrase), copies the model and sets up the account, and the
progress screen offers to restart.
Without the assistant, the classic wizard goes through the same steps
and fills the same plan with no model, including manual layout in its
partition editor.

%prep
%autosetup -p1

%build
%pyproject_wheel

%install
%pyproject_install
%python_expand %fdupes %{buildroot}%{$python_sitelib}
install -D -m 0644 data/%{appid}.desktop %{buildroot}%{_datadir}/applications/%{appid}.desktop
install -D -m 0644 data/vilna-installer.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/vilna-installer.svg
install -D -m 0644 data/vilna-installer-symbolic.svg %{buildroot}%{_datadir}/icons/hicolor/symbolic/apps/vilna-installer-symbolic.svg

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/%{appid}.desktop
%pytest

%files
%license LICENSE
%doc README.md
%{_bindir}/vilna-installer
%{_bindir}/vilna-installer-chat
%{_bindir}/vilna-installer-wizard
%{_bindir}/vilna-installer-confirm
%{_bindir}/vilna-installer-engine
%{python_sitelib}/vilna_installer
%{python_sitelib}/vilna_installer-*.dist-info
%{_datadir}/applications/%{appid}.desktop
%{_datadir}/icons/hicolor/scalable/apps/vilna-installer.svg
%{_datadir}/icons/hicolor/symbolic/apps/vilna-installer-symbolic.svg

%changelog
