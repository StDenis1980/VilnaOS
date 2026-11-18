#
# spec file for package gnome-shell-extension-dash-to-dock
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

# Dash to Dock (GPL-2.0-or-later), unchanged, built from its release tag.
# It is the dock of the Vilna desktop (docs/design/desktop/README.md):
# 30_Vilna.gschema.override in vilna-branding enables it and sets it up,
# and the Vilna extension (gnome-shell-extension-vilna) draws it in the
# Vilna colours. Upstream's own theme stays off (apply-custom-theme).

%global uuid dash-to-dock@micxgx.gmail.com
%global tag extensions.gnome.org-v%{version}

Name:           gnome-shell-extension-dash-to-dock
Version:        110
Release:        0
Summary:        A dock for GNOME Shell
License:        GPL-2.0-or-later
URL:            https://micheleg.github.io/dash-to-dock/
# Fetched by OBS from github.com and checked against its SHA-256 (_service).
Source0:        dash-to-dock-%{version}.tar.gz
BuildRequires:  gettext-tools
BuildRequires:  glib2-tools
BuildRequires:  make
BuildRequires:  sassc
Requires:       gnome-shell >= 45
BuildArch:      noarch

%description
Dash to Dock moves the dash out of the overview and turns it into a dock,
for launching applications and switching between windows faster. In Vilna
it sits at the bottom of the screen, hides when a window covers it and
uses the Vilna colours.

%prep
%autosetup -n dash-to-dock-%{tag}

%build
# Compiles the schema, the stylesheet (sassc) and the translations.
%make_build VERSION=%{version}

%install
# DESTDIR set: Makefile's "system" install puts the schema and the
# translations under /usr/share, outside the extension folder.
make install DESTDIR=%{buildroot} VERSION=%{version}
%find_lang dashtodock

%check
glib-compile-schemas --strict --dry-run schemas
grep -q '"uuid": "%{uuid}"' %{buildroot}%{_datadir}/gnome-shell/extensions/%{uuid}/metadata.json

%files -f dashtodock.lang
%license COPYING
%doc README.md
# OBS fails a package whose directories no package owns; gnome-shell
# owns them only at runtime, so own them here as other extensions do.
%dir %{_datadir}/gnome-shell
%dir %{_datadir}/gnome-shell/extensions
%{_datadir}/gnome-shell/extensions/%{uuid}/
%{_datadir}/glib-2.0/schemas/org.gnome.shell.extensions.dash-to-dock.gschema.xml

%changelog
