#
# spec file for package vilna-sound-themes
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

# Version comes from sounds/VERSION; obs/tools/obs_sync.py fills it in.
Name:           vilna-sound-themes
Version:        0.1
Release:        0
Summary:        Vilna sound themes
License:        CC-BY-SA-4.0 AND GPL-3.0-or-later
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        %{name}-%{version}.tar.gz
# Sounds a theme leaves out come from GNOME's default theme.
Requires:       sound-theme-freedesktop
# The login sound: vilna-login-sound.desktop in the autostart folder.
Requires:       /usr/bin/canberra-gtk-play
Requires:       /usr/bin/gsettings
BuildRequires:  desktop-file-utils
BuildArch:      noarch

%description
Seven sound themes of Vilna OS after the freedesktop Sound Theme spec:
wood, bell, water, air, birds, glass and star. Every event has the same
short motif in each theme, only the timbre changes, so a sound keeps its
meaning after a change of theme. Besides the usual desktop events there
are a login sound and signals for Gloxi and Herkos. Wood, bell, star,
water and air are synthesised by make_sounds.py; birds and glass use
short excerpts of CC0 and public-domain recordings (SOURCES.md). The
sounds are CC-BY-SA-4.0, the scripts GPL-3.0-or-later. The Vilna GNOME
Shell extension picks the theme and turns single sounds off;
vilna-login-sound.desktop plays the login sound after login.

%prep
%autosetup -p1

%build

%install
install -d %{buildroot}%{_datadir}/sounds
cp -a themes/vilna-* %{buildroot}%{_datadir}/sounds/
find %{buildroot}%{_datadir}/sounds -type d -exec chmod 0755 {} +
find %{buildroot}%{_datadir}/sounds -type f -exec chmod 0644 {} +
install -D -m 0644 vilna-login-sound.desktop %{buildroot}%{_sysconfdir}/xdg/autostart/vilna-login-sound.desktop

%check
desktop-file-validate %{buildroot}%{_sysconfdir}/xdg/autostart/vilna-login-sound.desktop
# Every theme is complete: its index and one sound for each of the 18 events.
for theme in wood bell water air birds glass star; do
    dir=%{buildroot}%{_datadir}/sounds/vilna-$theme
    grep -q '^Inherits=freedesktop$' $dir/index.theme
    test "$(ls $dir/stereo/*.oga | wc -l)" -eq 18
done

%files
%license LICENSE LICENSE.CC-BY-SA-4.0
%doc SOURCES.md
%dir %{_datadir}/sounds
%{_datadir}/sounds/vilna-*
%dir %{_sysconfdir}/xdg/autostart
%config %{_sysconfdir}/xdg/autostart/vilna-login-sound.desktop

%changelog
