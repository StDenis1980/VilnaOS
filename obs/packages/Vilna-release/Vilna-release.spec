#
# spec file for package Vilna-release
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

Name:           Vilna-release
Version:        0.3
Release:        0
Summary:        Vilna operating system identification
License:        GPL-3.0-or-later
URL:            https://github.com/StDenis1980/VilnaOS
Source0:        os-release
# Vilna's own packages: the OBS project home:StDenis1980:vilna and its
# signing key (fingerprint 0DC0 DF20 38AD A1C1 ABFD 2A67 D5CA DF01 0638 6694).
Source1:        vilna.repo
Source2:        vilna-obs.asc
BuildArch:      noarch
# No Requires on openSUSE-repos-Tumbleweed (the Tumbleweed repositories
# as a zypp service): KIWI installs this package in its bootstrap, and
# a service in the image root makes every later zypper call of the
# build refresh it, which fails on OBS (no network). patterns-vilna-base
# pulls it in during the image phase instead.
# One release package per system: replaces openSUSE-release / Aeon-release.
Provides:       distribution-release
Conflicts:      Aeon-release
Conflicts:      Kalpa-release
Conflicts:      MicroOS-release
Conflicts:      openSUSE-release

%description
Identifies the system as Vilna: /usr/lib/os-release and the
/etc/os-release link. Vilna is based on openSUSE Aeon.

Also adds Vilna's package repository and its signing key; the
openSUSE Tumbleweed repositories come from openSUSE-repos-Tumbleweed
(required by patterns-vilna-base).
The image imports the key into the rpm database (config.sh): rpm
cannot import it from %post, its database is locked during the
transaction.

%prep

%build
sed 's/@VERSION@/%{version}/g' %{SOURCE0} > os-release

%install
install -D -m 0644 os-release %{buildroot}%{_prefix}/lib/os-release
install -d %{buildroot}%{_sysconfdir}
ln -s ../usr/lib/os-release %{buildroot}%{_sysconfdir}/os-release
install -D -m 0644 %{SOURCE1} %{buildroot}%{_sysconfdir}/zypp/repos.d/vilna.repo
install -D -m 0644 %{SOURCE2} %{buildroot}%{_prefix}/lib/rpm/gnupg/keys/gpg-pubkey-06386694.asc

%files
%{_prefix}/lib/os-release
%{_sysconfdir}/os-release
%dir %{_sysconfdir}/zypp
%dir %{_sysconfdir}/zypp/repos.d
%config(noreplace) %{_sysconfdir}/zypp/repos.d/vilna.repo
%dir %{_prefix}/lib/rpm/gnupg
%dir %{_prefix}/lib/rpm/gnupg/keys
%{_prefix}/lib/rpm/gnupg/keys/gpg-pubkey-06386694.asc

%changelog
