#
# spec file for package gnome-software
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 SUSE LLC and contributors
# Copyright (c) 2026 Vilna contributors
#
# Based on the openSUSE:Factory spec of gnome-software (GPL-2.0-or-later,
# the licence of the package), kept close to it so the two stay comparable.
#

# GNOME Software (GPL-2.0-or-later), the Vilna store, from the GNOME
# release tarball and built with the same options and subpackages as
# Tumbleweed, plus one Vilna patch: when a search finds nothing, it is
# tried again as the same keys would type in the person's other keyboard
# layouts, so "ашкуащч" typed with the Ukrainian layout on finds Firefox
# (Denys, 2026-11-27). The conversion is vilna-layout-search.c: no letter
# tables, xkbcommon reads each layout from xkeyboard-config. The patch is
# ours to keep: follow Tumbleweed's version (_service) and check that it
# still applies. "+vilnaN" keeps this build above Tumbleweed's of the
# same version, so the image takes it.

%define gs_plugin_api 23
%global upstream_version 51.1

Name:           gnome-software
Version:        %{upstream_version}+vilna1
Release:        0
Summary:        GNOME Software Store
License:        GPL-2.0-or-later
Group:          System/GUI/GNOME
URL:            https://apps.gnome.org/Software/
# Fetched by OBS from download.gnome.org and checked against its SHA-256 (_service).
Source0:        %{name}-%{upstream_version}.tar.xz
Source1:        vilna-layout-search.c
Source2:        vilna-layout-search.h
Source3:        vilna-layout-search-test.c
Patch0:         vilna-layout-search.patch

BuildRequires:  c_compiler
BuildRequires:  fdupes
BuildRequires:  gtk-doc
BuildRequires:  itstool
BuildRequires:  meson >= 0.58.0
BuildRequires:  pkgconfig
BuildRequires:  suse-xsl-stylesheets
# For the %%check of vilna-layout-search.c: the layouts it reads.
BuildRequires:  xkeyboard-config
BuildRequires:  pkgconfig(appstream) >= 0.16.4
BuildRequires:  pkgconfig(flatpak) >= 1.14.1
BuildRequires:  pkgconfig(fwupd) >= 1.6.2
BuildRequires:  pkgconfig(gdk-pixbuf-2.0) >= 2.32.0
BuildRequires:  pkgconfig(gio-unix-2.0) >= 2.56.0
BuildRequires:  pkgconfig(glib-testing-0) >= 0.2.0
BuildRequires:  pkgconfig(gmodule-2.0)
BuildRequires:  pkgconfig(gsettings-desktop-schemas) >= 3.18.0
BuildRequires:  pkgconfig(gspell-1)
BuildRequires:  pkgconfig(gtk4) >= 4.17.5
BuildRequires:  pkgconfig(gudev-1.0)
BuildRequires:  pkgconfig(json-glib-1.0) >= 1.6.0
BuildRequires:  pkgconfig(libadwaita-1) >= 1.8.alpha
BuildRequires:  pkgconfig(libsecret-1)
BuildRequires:  pkgconfig(libsoup-3.0) >= 3.0
BuildRequires:  pkgconfig(ostree-1)
BuildRequires:  pkgconfig(packagekit-glib2) >= 1.2.5
BuildRequires:  pkgconfig(polkit-gobject-1)
BuildRequires:  pkgconfig(sqlite3)
BuildRequires:  pkgconfig(xkbcommon)
BuildRequires:  pkgconfig(xmlb) >= 0.3.22
# boo#1090042
Requires:       fwupd
Requires:       iso-codes
# The layouts a search is retried in; without them it only finds what was typed.
Recommends:     xkeyboard-config
Recommends:     %{name}-plugin-packagekit
Recommends:     flatpak

%description
AppStore like management of Applications for your GNOME Desktop.
This build carries the Vilna patch that also searches for what the same
keys type in the other keyboard layouts.

%package devel
Summary:        Development files for the GNOME software store
Group:          Development/Libraries/GNOME
Requires:       %{name} = %{version}

%description devel
This subpackage contains the header files for developing
GNOME software store plugins.

%package plugin-packagekit
Summary:        GNOME Software Store - PackageKit plugin
Group:          System/GUI/GNOME
Requires:       %{name} = %{version}
Requires:       PackageKit
Requires:       (libzypp-plugin-appdata if PackageKit-backend-zypp)

%description plugin-packagekit
This subpackage provides the packagekit plugin used by
the GNOME software store.

%lang_package

%prep
%autosetup -p1 -n %{name}-%{upstream_version}
cp %{SOURCE1} %{SOURCE2} src/

%build
# As Tumbleweed (malcontent is off there too).
%meson \
	-D tests=false \
	-D malcontent=false \
	-D sysprof=disabled \
	-D opensuse-distro-upgrade=true \
	-D hardcoded_curated=true \
	-D default_featured_apps=true \
	%{nil}
%meson_build

%install
%meson_install
%find_lang %{name}
%fdupes %{buildroot}%{_datadir}/icons/hicolor/scalable/categories

# Remove any piece of doc that ends up in non-standard locations and use the doc macro instead
rm %{buildroot}%{_datadir}/doc/%{name}/README.md

cat >> %{buildroot}%{_datadir}/glib-2.0/schemas/20_org.gnome.software-opensuse.gschema.override << FOE
[org.gnome.software]
official-repos = [ 'repo-oss', 'repo-update', 'repo-non-oss', 'download.opensuse.org-oss', 'download.opensuse.org-tumbleweed', 'download.opensuse.org-non-oss' ]
FOE

%check
# The layout conversion against this system's xkeyboard-config, and that
# the search page really calls it.
gcc %{optflags} -Isrc -o vilna-layout-search-test %{SOURCE3} src/vilna-layout-search.c \
	$(pkg-config --cflags --libs gio-2.0 xkbcommon)
./vilna-layout-search-test
grep -q 'vilna_layout_search_alternatives (self->value)' src/gs-search-page.c

%files
%license COPYING
%doc NEWS README.md
%{_bindir}/%{name}
%{_datadir}/metainfo/org.gnome.Software.Plugin.Epiphany.metainfo.xml
%{_datadir}/metainfo/org.gnome.Software.Plugin.Flatpak.metainfo.xml
%{_datadir}/metainfo/org.gnome.Software.Plugin.Fwupd.metainfo.xml
%{_datadir}/metainfo/org.gnome.Software.metainfo.xml
%{_datadir}/applications/gnome-software-local-file-flatpak.desktop
%{_datadir}/applications/gnome-software-local-file-fwupd.desktop
%{_datadir}/applications/gnome-software-local-file-metainfo.desktop
%{_datadir}/applications/gnome-software-local-file-packagekit.desktop
%{_datadir}/applications/org.gnome.Software.desktop
%{_datadir}/bash-completion/completions/gnome-software
%{_datadir}/dbus-1/services/org.gnome.Software.service
%{_datadir}/dbus-1/services/org.freedesktop.PackageKit.service
%{_datadir}/glib-2.0/schemas/org.gnome.software.gschema.xml
%{_datadir}/glib-2.0/schemas/20_org.gnome.software-opensuse.gschema.override
%dir %{_datadir}/gnome-shell
%dir %{_datadir}/gnome-shell/search-providers
%{_datadir}/gnome-shell/search-providers/org.gnome.Software-search-provider.ini
%{_datadir}/icons/hicolor/*/*/*.svg
%dir %{_libdir}/gnome-software
%{_libexecdir}/gnome-software-cmd
%{_mandir}/man1/%{name}.1%{?ext_man}
%{_userunitdir}/gnome-software.service

%dir %{_libdir}/gnome-software/plugins-%{gs_plugin_api}/
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_dpkg.so
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_dummy.so
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_epiphany.so
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_fedora-langpacks.so
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_fedora-pkgdb-collections.so
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_flatpak.so
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_fwupd.so
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_generic-updates.so
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_hardcoded-blocklist.so
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_modalias.so
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_os-release.so
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_opensuse-distro-upgrade.so
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_provenance-license.so
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_provenance.so
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_repos.so
%{_libdir}/gnome-software/libgnomesoftware.so.%{gs_plugin_api}
%dir %{_datadir}/swcatalog/
%dir %{_datadir}/swcatalog/xml/
%{_datadir}/swcatalog/xml/gnome-pwa-list-foss.xml
%{_datadir}/swcatalog/xml/gnome-pwa-list-proprietary.xml
%{_datadir}/swcatalog/xml/org.gnome.Software.Curated.xml
%{_datadir}/swcatalog/xml/org.gnome.Software.Featured.xml

%files devel
%doc AUTHORS
%dir %{_includedir}/%{name}
%{_datadir}/gtk-doc/html/%{name}/
%dir %{_datadir}/doc/gnome-software
%{_includedir}/%{name}/*.h
%{_libdir}/pkgconfig/gnome-software.pc
%{_libdir}/gnome-software/libgnomesoftware.so

%files plugin-packagekit
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_packagekit.so
%{_libdir}/gnome-software/plugins-%{gs_plugin_api}/libgs_plugin_appstream.so

%files lang -f %{name}.lang

%changelog
