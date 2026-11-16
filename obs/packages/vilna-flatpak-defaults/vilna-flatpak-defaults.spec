#
# spec file for package vilna-flatpak-defaults
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

Name:           vilna-flatpak-defaults
Version:        0.3
Release:        0
Summary:        Installs Vilna's default Flatpak apps on first boot
License:        GPL-3.0-or-later
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        flatpaks.list
Source1:        vilna-flatpak-defaults.sh
Source2:        vilna-flatpak-defaults.service
Source3:        flatpaks-vilna.list
Source4:        gnome-mimeapps.list
# vilna.flatpakrepo (ADR 0024) is not a SourceN: it carries the public key of
# Vilna's Flatpak repository (Vilna Flatpak, ed25519,
# B1932C41A5D58E73F12C8A7825496EF9222B4D2C), copied unchanged from
# https://flatpak.vilnaos.com/vilna.flatpakrepo. A build without it still
# works: Tinta Office then waits (the service says so in its log and tries
# again on the next boot).
%define have_vilna_repo %(test -s %{_sourcedir}/vilna.flatpakrepo && echo 1 || echo 0)
BuildRequires:  systemd-rpm-macros
Requires:       flatpak
BuildArch:      noarch
%{?systemd_requires}

%description
A one-time service that installs Vilna's default apps as system-wide
Flatpaks once the computer is online: Firefox, the GNOME basics and
KeePassXC from Flathub, and Tinta Office (ADR 0011) from Vilna's own
signed Flatpak repository (ADR 0024). It retries on every boot until it
succeeds. It also makes Tinta Office the default for office documents,
PDF, mail, calendar files and contacts in GNOME.

%prep

%build

%install
install -D -m 0644 %{SOURCE0} %{buildroot}%{_datadir}/vilna/flatpaks.list
install -D -m 0755 %{SOURCE1} %{buildroot}%{_libexecdir}/vilna-flatpak-defaults
install -D -m 0644 %{SOURCE2} %{buildroot}%{_unitdir}/vilna-flatpak-defaults.service
install -D -m 0644 %{SOURCE3} %{buildroot}%{_datadir}/vilna/flatpaks-vilna.list
install -D -m 0644 %{SOURCE4} %{buildroot}%{_sysconfdir}/xdg/gnome-mimeapps.list
%if %{have_vilna_repo}
install -D -m 0644 %{_sourcedir}/vilna.flatpakrepo %{buildroot}%{_datadir}/vilna/vilna.flatpakrepo
%endif

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
%{_datadir}/vilna/flatpaks-vilna.list
%if %{have_vilna_repo}
%{_datadir}/vilna/vilna.flatpakrepo
%endif
%dir %{_sysconfdir}/xdg
%config(noreplace) %{_sysconfdir}/xdg/gnome-mimeapps.list

%changelog
