#
# spec file for package ptyxis
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

# Ptyxis (GPL-3.0-or-later), the Vilna terminal, from the GNOME release
# tarball with one Vilna patch: a right click pastes the clipboard when
# org.vilna.Ptyxis right-click-paste is on, and Shift + right click opens
# the context menu (Ptyxis itself has no such setting). The patch is ours
# to keep: follow Tumbleweed's version (_service) and check that it still
# applies. "+vilnaN" keeps this build above Tumbleweed's of the same
# version, so the image takes it.
#
# Built with -Dgeneric=terminal: upstream's own option that names the
# program «Terminal» («Термінал» and so on from its translations) instead
# of «Ptyxis», which says nothing to a person (Denys, 2026-11-25). The
# app id stays org.gnome.Ptyxis, so settings and the dock are unchanged.
# The icon is Vilna's own (vilna-terminal.svg, docs/design/icons).

%global upstream_version 50.1

Name:           ptyxis
Version:        %{upstream_version}+vilna1
Release:        0
Summary:        A container-oriented terminal for GNOME
License:        GPL-3.0-or-later
URL:            https://gitlab.gnome.org/chergert/ptyxis
# Fetched by OBS from download.gnome.org and checked against its SHA-256 (_service).
Source0:        ptyxis-%{upstream_version}.tar.xz
Source1:        org.vilna.Ptyxis.gschema.xml
Source2:        vilna-terminal.svg
Source3:        vilna-terminal-symbolic.svg
Patch0:         vilna-right-click-paste.patch
BuildRequires:  c_compiler
BuildRequires:  gettext-tools
BuildRequires:  glib2-tools
BuildRequires:  meson
BuildRequires:  pkgconfig
BuildRequires:  pkgconfig(gio-unix-2.0)
BuildRequires:  pkgconfig(gtk4)
BuildRequires:  pkgconfig(json-glib-1.0)
BuildRequires:  pkgconfig(libadwaita-1)
BuildRequires:  pkgconfig(libportal-gtk4)
BuildRequires:  pkgconfig(vte-2.91-gtk4)

%description
Ptyxis is a terminal for GNOME that focuses on ease of use with container
workloads such as Podman, Toolbox and Distrobox. This build carries the
Vilna patch for pasting with a right click (org.vilna.Ptyxis).

%lang_package

%prep
%autosetup -p1 -n ptyxis-%{upstream_version}

%build
%meson -Dgeneric=terminal
%meson_build

%install
%meson_install
install -Dm 0644 %{SOURCE1} %{buildroot}%{_datadir}/glib-2.0/schemas/org.vilna.Ptyxis.gschema.xml
install -Dm 0644 %{SOURCE2} %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/org.gnome.Ptyxis.svg
install -Dm 0644 %{SOURCE3} %{buildroot}%{_datadir}/icons/hicolor/symbolic/apps/org.gnome.Ptyxis-symbolic.svg
%find_lang %{name} %{?no_lang_C}

%check
glib-compile-schemas --strict --dry-run %{buildroot}%{_datadir}/glib-2.0/schemas
grep -q 'org.vilna.Ptyxis' src/ptyxis-terminal.c
grep -q '^Name=Terminal$' %{buildroot}%{_datadir}/applications/org.gnome.Ptyxis.desktop
grep -q '^Name\[uk\]=Термінал$' %{buildroot}%{_datadir}/applications/org.gnome.Ptyxis.desktop
cmp %{SOURCE2} %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/org.gnome.Ptyxis.svg

%files
%license COPYING
%doc README.md NEWS
%{_bindir}/ptyxis*
%{_libexecdir}/ptyxis-agent
%{_datadir}/applications/*Ptyxis*.desktop
%{_datadir}/dbus-1/services/*Ptyxis*.service
%{_datadir}/glib-2.0/schemas/org.gnome.Ptyxis.gschema.xml
%{_datadir}/glib-2.0/schemas/org.vilna.Ptyxis.gschema.xml
%{_datadir}/icons/hicolor/*/apps/*Ptyxis*
%{_datadir}/metainfo/*Ptyxis*.metainfo.xml
%{_mandir}/man1/ptyxis.1%{?ext_man}

%files lang -f %{name}.lang

%changelog
