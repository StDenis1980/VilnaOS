#
# spec file for package gnome-shell-extension-vilna
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

%global uuid vilna@stdenis1980.github.io
# Version comes from shell-extension/<uuid>/metadata.json ("version-name");
# obs/tools/obs_sync.py fills it in.
Name:           gnome-shell-extension-vilna
Version:        0.1
Release:        0
Summary:        Vilna integration for GNOME Shell
License:        GPL-3.0-or-later
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        %{name}-%{version}.tar.gz
BuildRequires:  glib2-tools
Requires:       gnome-shell >= 46
BuildArch:      noarch

%description
The GNOME Shell extension of Vilna OS. In password fields (login and lock
screens, Wi-Fi, encrypted disks, administrator prompts and GTK password
entries) it switches to a Latin keyboard layout, shows which layout is on
and switches back afterwards. It keeps the Gloxi assistant above other
windows, and the Herkos confirmation dialog always. It can reserve a strip
of the screen for the Gloxi panel (experimental, off by default).

%prep
%autosetup -p1

%build

%install
dest=%{buildroot}%{_datadir}/gnome-shell/extensions/%{uuid}
install -d "$dest/lib"
install -m 0644 %{uuid}/metadata.json %{uuid}/stylesheet.css %{uuid}/*.js "$dest/"
install -m 0644 %{uuid}/lib/*.js "$dest/lib/"
# System-wide schema: compiled by the glib2 file trigger, found by
# Extension.getSettings() when the extension has no schemas/ folder.
install -D -m 0644 %{uuid}/schemas/org.gnome.shell.extensions.vilna.gschema.xml \
    %{buildroot}%{_datadir}/glib-2.0/schemas/org.gnome.shell.extensions.vilna.gschema.xml

%check
glib-compile-schemas --strict --dry-run %{uuid}/schemas

%files
%license LICENSE
%doc README.md
# OBS fails a package whose directories no package owns; gnome-shell
# owns them only at runtime, so own them here as other extensions do.
%dir %{_datadir}/gnome-shell
%dir %{_datadir}/gnome-shell/extensions
%{_datadir}/gnome-shell/extensions/%{uuid}/
%{_datadir}/glib-2.0/schemas/org.gnome.shell.extensions.vilna.gschema.xml

%changelog
