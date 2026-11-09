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
# «Screen reader» turns on Orca, as GNOME Settings does.
Recommends:     orca
BuildArch:      noarch

%description
The Vilna OS installer (image/docs/installer.md), started in the live
image. So far step 0, the first screen: the ten languages of Vilna and
every other language of the system, the screen reader, and going on with
the Gloxi assistant or without it. The screen writes the start of the
installation plan; the conversation with Gloxi, the plan check and
Herkos's final confirmation come next.

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
%{python_sitelib}/vilna_installer
%{python_sitelib}/vilna_installer-*.dist-info
%{_datadir}/applications/%{appid}.desktop
%{_datadir}/icons/hicolor/scalable/apps/vilna-installer.svg
%{_datadir}/icons/hicolor/symbolic/apps/vilna-installer-symbolic.svg

%changelog
