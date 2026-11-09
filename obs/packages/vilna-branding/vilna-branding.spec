#
# spec file for package vilna-branding
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

Name:           vilna-branding
Version:        0.3
Release:        0
Summary:        Vilna branding packages
License:        GPL-3.0-or-later
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        30_Vilna.gschema.override
Source1:        50-vilna.sudoers
Source2:        49-vilna.rules
Source3:        50-vilna-desktop.conf
Source4:        zzz-vilna.conf
Source5:        vendor.conf
Source6:        distrobox-upgrade-all.service
Source7:        distrobox-upgrade-all.timer
Source8:        87-default-Vilna.preset
Source9:        90-default-Vilna.preset
Source10:       87-default-Vilna-user.preset
Source11:       x86_64_v3-transactional-update.service
Source12:       conncheck-vilna.conf
Source13:       50-vilna-initial-setup-stop.conf
BuildRequires:  systemd-rpm-macros
BuildArch:      noarch

%description
The Vilna replacements for the *-branding-Aeon and *-branding-openSUSE
packages of openSUSE Aeon (image/docs/composition.md, section 4.5).
Each subpackage provides the same capability as the package it replaces.

%package -n gnome-branding-Vilna
Summary:        GNOME defaults for Vilna
Provides:       gnome-branding
Conflicts:      gnome-branding-Aeon
Conflicts:      gnome-branding-MicroOS
Requires:       gnome-backgrounds
Requires:       distribution-logos-Vilna
Requires:       gnome-initial-setup
Requires:       polkit
Requires:       sound-theme-freedesktop
Requires:       sudo
Requires:       system-group-wheel
Requires:       transactional-update

%description -n gnome-branding-Vilna
GNOME defaults for Vilna: dark style, the Vilna logo on the login
screen, Gloxi in the dash, administrator rights for wheel (sudo and
polkit), reboot notifications from transactional-update, logind
settings, GNOME Initial Setup pages (and a quick stop of its session
on poweroff) and a daily distrobox upgrade.
The first-boot Flatpak install is vilna-flatpak-defaults.

%package -n systemd-presets-branding-Vilna
Summary:        Vilna systemd presets
Provides:       systemd-presets-branding
Conflicts:      otherproviders(systemd-presets-branding)
Requires:       systemd-presets-common-SUSE

%description -n systemd-presets-branding-Vilna
The services enabled by default on Vilna, after openSUSE Aeon's
presets. vilna-presets adds Vilna's own choices on top.

%package -n x86_64_v3-branding-Vilna
Summary:        Install x86_64_v3 libraries after updates
Requires:       transactional-update
Conflicts:      x86_64_v3-branding-Aeon

%description -n x86_64_v3-branding-Vilna
After each transactional-update, installs the libraries built for
x86_64_v3 on processors that support it, as openSUSE Aeon does.

%package -n NetworkManager-branding-Vilna
Summary:        NetworkManager defaults for Vilna
Provides:       NetworkManager-branding
Conflicts:      otherproviders(NetworkManager-branding)
Requires:       NetworkManager

%description -n NetworkManager-branding-Vilna
NetworkManager defaults for Vilna: a connectivity check, so that GNOME
notices captive portals and opens their login page.

%prep

%build

%install
install -D -m 0644 %{SOURCE0} %{buildroot}%{_datadir}/glib-2.0/schemas/30_Vilna.gschema.override
install -D -m 0440 %{SOURCE1} %{buildroot}%{_prefix}/etc/sudoers.d/50-vilna
install -D -m 0644 %{SOURCE2} %{buildroot}%{_datadir}/polkit-1/rules.d/49-vilna.rules
install -D -m 0644 %{SOURCE3} %{buildroot}%{_prefix}/etc/transactional-update.conf.d/50-vilna-desktop.conf
install -D -m 0644 %{SOURCE4} %{buildroot}%{_prefix}/lib/systemd/logind.conf.d/zzz-vilna.conf
install -D -m 0644 %{SOURCE5} %{buildroot}%{_datadir}/gnome-initial-setup/vendor.conf
install -D -m 0644 %{SOURCE6} %{buildroot}%{_userunitdir}/distrobox-upgrade-all.service
install -D -m 0644 %{SOURCE7} %{buildroot}%{_userunitdir}/distrobox-upgrade-all.timer
install -D -m 0644 %{SOURCE8} %{buildroot}%{_presetdir}/87-default-Vilna.preset
install -D -m 0644 %{SOURCE9} %{buildroot}%{_presetdir}/90-default-Vilna.preset
install -D -m 0644 %{SOURCE10} %{buildroot}%{_userpresetdir}/87-default-Vilna-user.preset
install -D -m 0644 %{SOURCE11} %{buildroot}%{_unitdir}/x86_64_v3-transactional-update.service
install -D -m 0644 %{SOURCE13} %{buildroot}%{_userunitdir}/gnome-session-manager@gnome-initial-setup.service.d/50-vilna-stop.conf
install -D -m 0644 %{SOURCE12} %{buildroot}%{_prefix}/lib/NetworkManager/conf.d/conncheck-vilna.conf

%files -n gnome-branding-Vilna
%dir %{_datadir}/glib-2.0
%dir %{_datadir}/glib-2.0/schemas
%{_datadir}/glib-2.0/schemas/30_Vilna.gschema.override
%dir %{_prefix}/etc/sudoers.d
%{_prefix}/etc/sudoers.d/50-vilna
%dir %{_datadir}/polkit-1
%dir %{_datadir}/polkit-1/rules.d
%{_datadir}/polkit-1/rules.d/49-vilna.rules
%dir %{_prefix}/etc/transactional-update.conf.d
%{_prefix}/etc/transactional-update.conf.d/50-vilna-desktop.conf
%dir %{_prefix}/lib/systemd/logind.conf.d
%{_prefix}/lib/systemd/logind.conf.d/zzz-vilna.conf
%dir %{_datadir}/gnome-initial-setup
%{_datadir}/gnome-initial-setup/vendor.conf
%{_userunitdir}/distrobox-upgrade-all.service
%{_userunitdir}/distrobox-upgrade-all.timer
%dir %{_userunitdir}/gnome-session-manager@gnome-initial-setup.service.d
%{_userunitdir}/gnome-session-manager@gnome-initial-setup.service.d/50-vilna-stop.conf

%files -n systemd-presets-branding-Vilna
%{_presetdir}/87-default-Vilna.preset
%{_presetdir}/90-default-Vilna.preset
%{_userpresetdir}/87-default-Vilna-user.preset

%files -n x86_64_v3-branding-Vilna
%{_unitdir}/x86_64_v3-transactional-update.service

%files -n NetworkManager-branding-Vilna
%dir %{_prefix}/lib/NetworkManager
%dir %{_prefix}/lib/NetworkManager/conf.d
%{_prefix}/lib/NetworkManager/conf.d/conncheck-vilna.conf

%changelog
