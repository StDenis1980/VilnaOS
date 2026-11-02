#
# spec file for package Vilna-release
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

Name:           Vilna-release
Version:        0.1
Release:        0
Summary:        Vilna operating system identification
License:        GPL-3.0-or-later
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        os-release
BuildArch:      noarch
# One release package per system: replaces openSUSE-release / Aeon-release.
Provides:       distribution-release
Conflicts:      Aeon-release
Conflicts:      Kalpa-release
Conflicts:      MicroOS-release
Conflicts:      openSUSE-release

%description
Identifies the system as Vilna: /usr/lib/os-release and the
/etc/os-release link. Vilna is based on openSUSE Aeon.

%prep

%build
sed 's/@VERSION@/%{version}/g' %{SOURCE0} > os-release

%install
install -D -m 0644 os-release %{buildroot}%{_prefix}/lib/os-release
install -d %{buildroot}%{_sysconfdir}
ln -s ../usr/lib/os-release %{buildroot}%{_sysconfdir}/os-release

%files
%{_prefix}/lib/os-release
%{_sysconfdir}/os-release

%changelog
