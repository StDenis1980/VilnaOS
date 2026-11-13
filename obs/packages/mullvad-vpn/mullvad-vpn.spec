#
# spec file for package mullvad-vpn
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Vilna contributors
#

# Mullvad's own RPM for Fedora, repackaged for Vilna (ADR 0027). The files
# are Mullvad's, unchanged; the scriptlets are ours. Theirs enable and start
# the daemon at once, make mullvad-exclude setuid, and on removal delete
# ~/.config/Mullvad VPN of every user and unregister the device through the
# Mullvad API. Here the daemon stays off until the person chooses Mullvad
# (vilna-presets does not enable it), nothing is setuid (split tunnelling is
# not offered), and removal leaves people's settings alone.

%define upstream_rpm MullvadVPN-%{version}_x86_64.rpm
%define sha256 6cd0de1c42527b67870e2991f9c0a0399ed6a565f866476536a4d240b289af74
%define appdir /opt/Mullvad VPN
%define services mullvad-daemon.service mullvad-early-boot-blocking.service

# Prebuilt binaries: no debug package, no stripping, and the libraries the
# Electron app bundles are neither provided to nor required from the system.
%global debug_package %{nil}
%global __strip /bin/true
%global __provides_exclude_from ^/opt/Mullvad VPN/.*$
%global __requires_exclude ^lib(ffmpeg|EGL|GLESv2|vk_swiftshader|vulkan)\\.so.*$

Name:           mullvad-vpn
Version:        2026.5
Release:        0
Summary:        Mullvad VPN app and daemon (Mullvad's build, Vilna scriptlets)
License:        GPL-3.0-or-later
URL:            https://mullvad.net
# Fetched by OBS from repository.mullvad.net and checked against %%{sha256} (_service).
Source0:        %{upstream_rpm}
BuildRequires:  cpio
BuildRequires:  rpm
BuildRequires:  systemd-rpm-macros
Requires:       libnotify4
Requires:       libXss1
Requires:       libdbus-1-3
%{?systemd_ordering}
ExclusiveArch:  x86_64

%description
The Mullvad VPN app and its system service, as Mullvad builds them. In
Vilna it is the "most anonymity" choice beside Proton VPN: the person
enters a Mullvad account number or activation code in this app only,
never in Vilna or Gloxi. The service stays off until the person chooses
Mullvad, and the Herkos kill switch (vilna-netguard) works with it.

%prep
echo "%{sha256}  %{SOURCE0}" | sha256sum -c -
mkdir -p tree
cd tree
rpm2cpio %{SOURCE0} | cpio -idm --quiet

%build

%install
cp -a tree/. %{buildroot}/
# setuid only through the permissions package on openSUSE; not offered.
find %{buildroot} -type f -perm /6000 -exec chmod ug-s {} +
# Mullvad's AppArmor profile: Vilna uses SELinux.
rm -rf %{buildroot}%{_sysconfdir}/apparmor.d
# The file list, from what Mullvad ships (paths with spaces quoted). Every
# directory is owned too, except the system's own, so OBS's file-list check
# finds no directory without an owner (completion folders and the like).
(cd %{buildroot}
 find . -mindepth 1 \( -type f -o -type l \) -printf '"/%%P"\n'
 find . -mindepth 1 -type d -printf '%%P\n' \
   | grep -vxE 'etc|opt|usr|usr/bin|usr/sbin|usr/lib|usr/lib64|usr/share|usr/lib/systemd|usr/lib/systemd/system|usr/share/applications|usr/share/icons|usr/share/doc|usr/share/licenses|usr/share/man|usr/share/man/man1' \
   | sed 's|.*|%%dir "/&"|') > %{_builddir}/files.list

%pre
%service_add_pre %{services}

%post
%service_add_post %{services}

%preun
%service_del_preun %{services}

%postun
%service_del_postun %{services}

%files -f %{_builddir}/files.list

%changelog
