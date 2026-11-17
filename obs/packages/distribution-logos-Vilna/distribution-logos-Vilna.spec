#
# spec file for package distribution-logos-Vilna
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

Name:           distribution-logos-Vilna
Version:        0.4
Release:        0
Summary:        Vilna logos
License:        GPL-3.0-or-later AND CC-BY-SA-4.0
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        vilna-logo.svg
Source1:        vilna-logo-symbolic.svg
# The final logo for dark backgrounds (docs/design/logo/Vilna_logo_dark-bg.svg):
# the logo of the login screen (org.gnome.login-screen logo).
Source2:        vilna-logo-dark-bg.svg
Source3:        vilna-logo-dark.svg
Source4:        vilna-logo-text.svg
Source5:        vilna-logo-text-dark.svg
Source6:        vilna-logo-login.svg
Source7:        LICENSE.CC-BY-SA-4.0
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
used by the login screen until vilna-branding 0.9; vilna-logo-login is
the logo and the word Vilna in one row for the bottom of the login
screen. Settings → About shows the wide
vilna-logo-text icons (logo, the word Vilna and the sign and name of the
release, Blavet), vilna-logo-text-dark in a dark style; their Blavet sign is by Dmytro
Sizmin, CC-BY-SA-4.0.

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
cp %{SOURCE7} .

%build

%install
install -D -m 0644 %{SOURCE0} %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/vilna-logo.svg
install -m 0644 %{SOURCE3} %{SOURCE4} %{SOURCE5} %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/
install -D -m 0644 %{SOURCE1} %{buildroot}%{_datadir}/icons/hicolor/symbolic/apps/vilna-logo-symbolic.svg
install -d %{buildroot}%{_datadir}/pixmaps/distribution-logos
ln -s ../../icons/hicolor/scalable/apps/vilna-logo.svg %{buildroot}%{_datadir}/pixmaps/distribution-logos/square-hicolor.svg
ln -s ../../icons/hicolor/symbolic/apps/vilna-logo-symbolic.svg %{buildroot}%{_datadir}/pixmaps/distribution-logos/square-symbolic.svg
install -D -m 0644 %{SOURCE0} %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/distributor.svg
install -m 0644 %{SOURCE2} %{buildroot}%{_datadir}/pixmaps/distribution-logos/vilna-logo-dark-bg.svg
install -m 0644 %{SOURCE6} %{buildroot}%{_datadir}/pixmaps/distribution-logos/vilna-logo-login.svg

%files
%license LICENSE.CC-BY-SA-4.0
%{_datadir}/icons/hicolor/scalable/apps/vilna-logo.svg
%{_datadir}/icons/hicolor/scalable/apps/vilna-logo-dark.svg
%{_datadir}/icons/hicolor/scalable/apps/vilna-logo-text.svg
%{_datadir}/icons/hicolor/scalable/apps/vilna-logo-text-dark.svg
%{_datadir}/icons/hicolor/symbolic/apps/vilna-logo-symbolic.svg
%dir %{_datadir}/pixmaps/distribution-logos
%{_datadir}/pixmaps/distribution-logos/square-hicolor.svg
%{_datadir}/pixmaps/distribution-logos/square-symbolic.svg
%{_datadir}/pixmaps/distribution-logos/vilna-logo-dark-bg.svg
%{_datadir}/pixmaps/distribution-logos/vilna-logo-login.svg

%files -n hicolor-icon-theme-branding-Vilna
%{_datadir}/icons/hicolor/scalable/apps/distributor.svg

%changelog
