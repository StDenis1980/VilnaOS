#
# spec file for package distribution-logos-Vilna
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

Name:           distribution-logos-Vilna
Version:        0.3
Release:        0
Summary:        Vilna logos
License:        GPL-3.0-or-later
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        vilna-logo.svg
Source1:        vilna-logo-symbolic.svg
# The final logo for dark backgrounds (docs/design/logo/Vilna_logo_dark-bg.svg):
# the logo of the login screen (org.gnome.login-screen logo).
Source2:        vilna-logo-dark-bg.svg
BuildRequires:  hicolor-icon-theme
BuildArch:      noarch
# Replaces distribution-logos-openSUSE-Aeon (openSUSE trademark guidelines).
Provides:       distribution-logos
Conflicts:      distribution-logos-openSUSE-Aeon
Conflicts:      distribution-logos-openSUSE-Kalpa
Conflicts:      distribution-logos-openSUSE-MicroOS
Conflicts:      distribution-logos-openSUSE-Tumbleweed

%description
The Vilna logo as the "vilna-logo" icon (named by LOGO= in os-release)
and in /usr/share/pixmaps/distribution-logos for programs that look for
the distribution logo there. The final logo for dark backgrounds is
the logo of the login screen.

The vilna-logo icon is still a placeholder.

%package -n hicolor-icon-theme-branding-Vilna
Summary:        Vilna logo as the hicolor "distributor" icon
Provides:       hicolor-icon-theme-branding
Conflicts:      otherproviders(hicolor-icon-theme-branding)
Requires:       hicolor-icon-theme

%description -n hicolor-icon-theme-branding-Vilna
The Vilna logo as the "distributor" icon of the hicolor theme, which
programs use for the distribution logo (replaces
hicolor-icon-theme-branding-openSUSE).

%prep

%build

%install
install -D -m 0644 %{SOURCE0} %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/vilna-logo.svg
install -D -m 0644 %{SOURCE1} %{buildroot}%{_datadir}/icons/hicolor/symbolic/apps/vilna-logo-symbolic.svg
install -d %{buildroot}%{_datadir}/pixmaps/distribution-logos
ln -s ../../icons/hicolor/scalable/apps/vilna-logo.svg %{buildroot}%{_datadir}/pixmaps/distribution-logos/square-hicolor.svg
ln -s ../../icons/hicolor/symbolic/apps/vilna-logo-symbolic.svg %{buildroot}%{_datadir}/pixmaps/distribution-logos/square-symbolic.svg
%{_datadir}/pixmaps/distribution-logos/vilna-logo-dark-bg.svg
install -D -m 0644 %{SOURCE0} %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/distributor.svg
install -m 0644 %{SOURCE2} %{buildroot}%{_datadir}/pixmaps/distribution-logos/vilna-logo-dark-bg.svg

%files
%{_datadir}/icons/hicolor/scalable/apps/vilna-logo.svg
%{_datadir}/icons/hicolor/symbolic/apps/vilna-logo-symbolic.svg
%dir %{_datadir}/pixmaps/distribution-logos
%{_datadir}/pixmaps/distribution-logos/square-hicolor.svg
%{_datadir}/pixmaps/distribution-logos/square-symbolic.svg

%files -n hicolor-icon-theme-branding-Vilna
%{_datadir}/icons/hicolor/scalable/apps/distributor.svg

%changelog
