#
# spec file for package vilna-backup
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

# A program, not a library: built for the primary Python only.
%define pythons python3
%global appid io.github.stdenis1980.VilnaBackup
# Version comes from backup/pyproject.toml; obs/tools/obs_sync.py fills it in.
Name:           vilna-backup
Version:        0.1.0~dev0
Release:        0
Summary:        Encrypted backups of the person's files on Vilna
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
Requires:       python3 >= 3.11
Requires:       restic >= 0.17
# secret-tool keeps the repository password in GNOME Keyring. Named as a
# package: OBS cannot resolve the file path /usr/bin/secret-tool when it
# builds images, so Vilna and Vilna-Live were unresolvable.
Requires:       secret-tool
# The Backups window.
Requires:       python3-gobject
Requires:       typelib(Adw) = 1
Requires:       typelib(Gtk) = 4.0
BuildArch:      noarch

%description
Encrypted backups of the person's data on Vilna OS, in the restic format:
settings, Gloxi and Herkos data and chosen folders, to an external disk or
one's own server (SFTP, S3, REST). Everything is encrypted on the device;
the repository password stays in GNOME Keyring and a recovery key goes on
a sheet of paper. Backups do not start before that key is typed back.

The "Backups" window (vilna-backup-gui) turns backups on, shows their state,
starts a backup and restores one into a new folder. The command line
(vilna-backup) does the rest; Gloxi reaches it only through Herkos
(vilna-mcp-backup).

%prep
%autosetup -p1

%build
%pyproject_wheel

%install
%pyproject_install
%python_expand %fdupes %{buildroot}%{$python_sitelib}
install -D -m 0644 data/%{appid}.desktop %{buildroot}%{_datadir}/applications/%{appid}.desktop
install -D -m 0644 data/vilna-backup.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/vilna-backup.svg
install -D -m 0644 data/vilna-backup-symbolic.svg %{buildroot}%{_datadir}/icons/hicolor/symbolic/apps/vilna-backup-symbolic.svg

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/%{appid}.desktop
%pytest

%files
%license LICENSE
%doc README.md
%{_bindir}/vilna-backup
%{_bindir}/vilna-backup-gui
%{python_sitelib}/vilna_backup
%{python_sitelib}/vilna_backup-*.dist-info
%{_datadir}/applications/%{appid}.desktop
%{_datadir}/icons/hicolor/scalable/apps/vilna-backup.svg
%{_datadir}/icons/hicolor/symbolic/apps/vilna-backup-symbolic.svg

%changelog
