#
# spec file for package vilna-flatpak-defaults
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

Name:           vilna-flatpak-defaults
Version:        0.1
Release:        0
Summary:        Installs Vilna's default Flatpak apps on first boot
License:        GPL-3.0-or-later
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        flatpaks.list
Source1:        vilna-flatpak-defaults.sh
Source2:        vilna-flatpak-defaults.service
BuildRequires:  systemd-rpm-macros
Requires:       flatpak
BuildArch:      noarch
%{?systemd_requires}

%description
A one-time service that installs Vilna's default apps (Firefox, the
GNOME basics, KeePassXC) from Flathub as system-wide Flatpaks once the
computer is online. It retries on every boot until it succeeds.

%prep

%build

%install
install -D -m 0644 %{SOURCE0} %{buildroot}%{_datadir}/vilna/flatpaks.list
install -D -m 0755 %{SOURCE1} %{buildroot}%{_libexecdir}/vilna-flatpak-defaults
install -D -m 0644 %{SOURCE2} %{buildroot}%{_unitdir}/vilna-flatpak-defaults.service

%pre
%service_add_pre vilna-flatpak-defaults.service

%post
%service_add_post vilna-flatpak-defaults.service

%preun
%service_del_preun vilna-flatpak-defaults.service

%postun
%service_del_postun_without_restart vilna-flatpak-defaults.service

%files
%dir %{_datadir}/vilna
%{_datadir}/vilna/flatpaks.list
%{_libexecdir}/vilna-flatpak-defaults
%{_unitdir}/vilna-flatpak-defaults.service

%changelog
