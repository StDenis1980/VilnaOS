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
%meson
%meson_build

%install
%meson_install
install -Dm 0644 %{SOURCE1} %{buildroot}%{_datadir}/glib-2.0/schemas/org.vilna.Ptyxis.gschema.xml
%find_lang %{name} %{?no_lang_C}

%check
glib-compile-schemas --strict --dry-run %{buildroot}%{_datadir}/glib-2.0/schemas
grep -q 'org.vilna.Ptyxis' src/ptyxis-terminal.c

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
%{_datadir}/ptyxis/
%{_mandir}/man1/ptyxis.1%{?ext_man}

%files lang -f %{name}.lang

%changelog
